# Copyright (c) 2026-present Mesh contributors
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from celery import shared_task
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from plane.db.models import (
    AgentExecutionProfile,
    IssueComment,
    MeshAuditEvent,
    MeshLoopRun,
    MeshProjectPolicy,
    MeshRunAttempt,
    MeshStageRun,
)
from plane.mesh.discovery import leave_stage_unassigned
from plane.mesh.agent_cards import validate_agent_endpoint
from plane.mesh.runtime import complete_stage, resume_wait


TERMINAL_STATES = {
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_REJECTED",
    "TASK_STATE_CANCELED",
}


@shared_task(queue="mesh-runner")
def resume_mesh_wait(stage_run_id: str, wait_node_id: str):
    run = resume_wait(stage_run_id=stage_run_id, wait_node_id=wait_node_id)
    return {"status": run.status, "run_id": str(run.id), "current_node_id": run.current_node_id}


@shared_task(queue="mesh-runner")
def cancel_mesh_agent_attempt(attempt_id: str):
    attempt = (
        MeshRunAttempt.objects.select_related("agent", "execution_profile")
        .filter(id=attempt_id, deleted_at__isnull=True)
        .first()
    )
    if not attempt or not attempt.provider_run_id:
        return {"status": "missing", "attempt_id": attempt_id}
    _cancel_agent_task(attempt)
    MeshRunAttempt.objects.filter(id=attempt.id).update(provider_state="TASK_STATE_CANCELED")
    return {"status": "canceled", "attempt_id": attempt_id}


@shared_task(queue="mesh-runner")
def alert_stale_mesh_attempts():
    threshold_seconds = max(int(os.environ.get("MESH_STALE_ATTEMPT_SECONDS", "180")), 60)
    cutoff = timezone.now() - timedelta(seconds=threshold_seconds)
    attempts = (
        MeshRunAttempt.objects.filter(
            status=MeshRunAttempt.Status.RUNNING,
            deleted_at__isnull=True,
        )
        .filter(Q(heartbeat_at__lt=cutoff) | Q(heartbeat_at__isnull=True, started_at__lt=cutoff))
        .select_related("stage_run__loop_run__work_item", "agent")[:100]
    )
    alerted = 0
    for attempt in attempts:
        usage = dict(attempt.usage or {})
        if usage.get("_mesh_stale_alerted_at"):
            continue
        usage["_mesh_stale_alerted_at"] = timezone.now().isoformat()
        MeshRunAttempt.objects.filter(id=attempt.id).update(usage=usage)
        stage = attempt.stage_run
        IssueComment.objects.get_or_create(
            workspace_id=stage.workspace_id,
            project_id=stage.project_id,
            issue_id=stage.loop_run.work_item_id,
            external_source="mesh-runner",
            external_id=f"attempt:{attempt.id}:stale",
            defaults={
                "actor": None,
                "comment_html": "<p><strong>Mesh:</strong> Agent execution heartbeat is stale. The PM should inspect the Gateway and Runner.</p>",
            },
        )
        MeshAuditEvent.objects.create(
            workspace_id=stage.workspace_id,
            project_id=stage.project_id,
            loop_run_id=stage.loop_run_id,
            work_item_id=stage.loop_run.work_item_id,
            actor_agent=attempt.agent,
            event_type="stage.attempt_stale",
            payload={"stage_run_id": str(stage.id), "attempt_id": str(attempt.id), "cutoff": cutoff.isoformat()},
            occurred_at=timezone.now(),
        )
        alerted += 1
    return {"status": "ok", "stale_attempts_alerted": alerted}


@shared_task(queue="mesh-runner", bind=True, max_retries=2, default_retry_delay=30)
def start_mesh_stage(self, stage_run_id: str):
    """Start an explicitly assigned Mesh stage through its Agent endpoint."""
    try:
        stage, attempt = _prepare_attempt(stage_run_id)
    except MeshStageRun.DoesNotExist:
        return {"status": "missing", "stage_run_id": stage_run_id}

    if attempt is None:
        return {"status": stage.status, "stage_run_id": stage_run_id}
    if attempt.provider_run_id:
        poll_mesh_agent_task.apply_async(args=[str(attempt.id)], countdown=_poll_delay(0))
        return {"status": "running", "attempt_id": str(attempt.id), "provider_run_id": attempt.provider_run_id}

    try:
        response = _send_agent_task(stage, attempt)
        provider_run_id = str(response.get("id") or response.get("task_id") or "")
        if not provider_run_id:
            raise ValueError("Agent SendMessage response did not include a Task id")
    except (OSError, ValueError, urllib.error.URLError) as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc)
        _record_start_failure(stage_run_id, attempt.id, str(exc))
        return {"status": "waiting_for_assignee", "error": str(exc)}

    attempt.provider_run_id = provider_run_id
    attempt.provider_session_id = str(response.get("contextId") or response.get("context_id") or "")
    attempt.provider_state = str((response.get("status") or {}).get("state") or "TASK_STATE_SUBMITTED")
    attempt.status = MeshRunAttempt.Status.RUNNING
    attempt.started_at = attempt.started_at or timezone.now()
    attempt.heartbeat_at = timezone.now()
    updated = MeshRunAttempt.objects.filter(
        id=attempt.id, status__in=[MeshRunAttempt.Status.QUEUED, MeshRunAttempt.Status.RUNNING],
    ).update(
        provider_run_id=attempt.provider_run_id, provider_session_id=attempt.provider_session_id,
        provider_state=attempt.provider_state, status=attempt.status,
        started_at=attempt.started_at, heartbeat_at=attempt.heartbeat_at,
    )
    if not updated:
        _cancel_agent_task(attempt)
        return {"status": "canceled", "attempt_id": str(attempt.id)}
    poll_mesh_agent_task.apply_async(args=[str(attempt.id)], countdown=_poll_delay(0))
    return {"status": "running", "attempt_id": str(attempt.id), "provider_run_id": attempt.provider_run_id}


@shared_task(queue="mesh-runner", bind=True, max_retries=3, default_retry_delay=10)
def poll_mesh_agent_task(self, attempt_id: str):
    attempt = (
        MeshRunAttempt.objects.select_related(
            "stage_run__loop_run__definition",
            "stage_run__loop_run__work_item",
            "agent__user",
            "execution_profile",
        )
        .filter(id=attempt_id, deleted_at__isnull=True)
        .first()
    )
    if not attempt:
        return {"status": "missing", "attempt_id": attempt_id}
    if attempt.status in {
        MeshRunAttempt.Status.SUCCEEDED,
        MeshRunAttempt.Status.FAILED,
        MeshRunAttempt.Status.CANCELED,
    }:
        return {"status": attempt.status, "attempt_id": attempt_id}
    if _attempt_timed_out(attempt):
        _cancel_agent_task(attempt)
        return _record_terminal_failure(attempt.id, "timeout", "Agent task exceeded the Project Policy timeout")
    try:
        task = _get_agent_task(attempt)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc)
        return _record_terminal_failure(attempt.id, "poll_error", str(exc))

    provider_state = str((task.get("status") or {}).get("state") or "TASK_STATE_UNSPECIFIED")
    now = timezone.now()
    usage = dict(attempt.usage or {})
    poll_count = int(usage.get("_mesh_poll_count") or 0) + 1
    usage["_mesh_poll_count"] = poll_count
    MeshRunAttempt.objects.filter(id=attempt.id).update(
        provider_state=provider_state,
        last_polled_at=now,
        heartbeat_at=now,
        usage=usage,
    )
    if provider_state not in TERMINAL_STATES:
        poll_mesh_agent_task.apply_async(args=[attempt_id], countdown=_poll_delay(poll_count))
        return {"status": "running", "provider_state": provider_state, "attempt_id": attempt_id}
    if provider_state != "TASK_STATE_COMPLETED":
        message = _task_status_message(task) or f"Agent task ended in {provider_state}"
        return _record_terminal_failure(attempt.id, provider_state.lower(), message)
    try:
        completion = _completion_artifact(task)
        return _record_completion(attempt.id, completion, provider_state)
    except ValueError as exc:
        return _record_terminal_failure(attempt.id, "invalid_evidence", str(exc))


@transaction.atomic
def _prepare_attempt(stage_run_id: str):
    stage = (
        MeshStageRun.objects.select_for_update(of=("self",))
        .select_related("assigned_agent__user", "loop_run__work_item", "functional_role")
        .get(id=stage_run_id, deleted_at__isnull=True)
    )
    if stage.status not in {MeshStageRun.Status.QUEUED, MeshStageRun.Status.RUNNING}:
        return stage, None
    if stage.loop_run.status in {MeshLoopRun.Status.CANCELED, MeshLoopRun.Status.COMPLETED, MeshLoopRun.Status.FAILED}:
        return stage, None
    if not stage.assigned_agent_id:
        leave_stage_unassigned(stage)
        return stage, None

    agent = stage.assigned_agent
    if agent.status != agent.Status.ACTIVE or not (agent.agent_card or {}).get("available", False):
        leave_stage_unassigned(stage)
        _notify_pm(stage, "The selected Agent is unavailable. The work item has returned to Unassigned.")
        return stage, None

    existing = (
        stage.attempts.filter(
            status__in=[MeshRunAttempt.Status.QUEUED, MeshRunAttempt.Status.RUNNING],
            deleted_at__isnull=True,
        )
        .order_by("-created_at")
        .first()
    )
    if existing:
        return stage, existing

    execution_profile = (
        AgentExecutionProfile.objects.filter(agent=agent, is_active=True, deleted_at__isnull=True)
        .order_by("-is_default", "-configuration_version")
        .first()
    )
    provider = execution_profile.provider if execution_profile else agent.runtime_provider
    model = execution_profile.model if execution_profile else "unspecified"
    attempt = MeshRunAttempt.objects.create(
        workspace_id=stage.workspace_id,
        project_id=stage.project_id,
        stage_run=stage,
        agent=agent,
        execution_profile=execution_profile,
        provider=provider,
        model=model,
        configuration_version=execution_profile.configuration_version if execution_profile else 1,
        status=MeshRunAttempt.Status.QUEUED,
        provider_state="TASK_STATE_SUBMITTED",
    )
    stage.status = MeshStageRun.Status.RUNNING
    stage.started_at = stage.started_at or timezone.now()
    stage.save(update_fields=["status", "started_at", "updated_at"])
    return stage, attempt


def _send_agent_task(stage: MeshStageRun, attempt: MeshRunAttempt) -> dict:
    endpoint = (stage.assigned_agent.endpoint_url or "").strip()
    if not endpoint:
        raise ValueError("Assigned Agent has no endpoint_url")
    validate_agent_endpoint(endpoint)
    metadata = {
        "mesh_run_id": str(stage.loop_run_id),
        "mesh_stage_run_id": str(stage.id),
        "work_item_id": str(stage.loop_run.work_item_id),
        "project_id": str(stage.project_id),
        "loop_slug": stage.loop_run.definition.slug,
        "stage_node_id": stage.node_id,
        "work_item_name": stage.loop_run.work_item.name,
        "required_evidence": stage.required_evidence,
        "project_policy_url": f"/api/workspaces/{stage.workspace.slug}/projects/{stage.project_id}/mesh/policy/",
    }
    message = {
        "jsonrpc": "2.0",
        "id": str(attempt.id),
        "method": "SendMessage",
        "params": {
            "message": {
                "role": "ROLE_USER",
                "parts": [{"text": stage.objective or f"Work on {stage.loop_run.work_item.name}"}],
                "messageId": str(attempt.id),
                "contextId": str(stage.loop_run_id),
                "metadata": metadata,
            },
            "configuration": {
                "returnImmediately": True,
                "acceptedOutputModes": ["application/json", "text/plain"],
            },
            "metadata": metadata,
        },
    }
    payload = _a2a_request(endpoint, message, attempt.execution_profile)
    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    if isinstance(result.get("task"), dict):
        return result["task"]
    if isinstance(result.get("message"), dict):
        raise ValueError("Agent returned a direct Message; Mesh stages require a trackable A2A Task")
    return result


def _get_agent_task(attempt: MeshRunAttempt) -> dict:
    endpoint = (attempt.agent.endpoint_url or "").strip()
    validate_agent_endpoint(endpoint)
    payload = _a2a_request(
        endpoint,
        {
            "jsonrpc": "2.0",
            "id": f"poll:{attempt.id}",
            "method": "GetTask",
            "params": {"id": attempt.provider_run_id, "historyLength": 10},
        },
        attempt.execution_profile,
    )
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ValueError("Agent GetTask response did not include a Task")
    return result


def _cancel_agent_task(attempt: MeshRunAttempt) -> None:
    try:
        _a2a_request(
            attempt.agent.endpoint_url,
            {
                "jsonrpc": "2.0",
                "id": f"cancel:{attempt.id}",
                "method": "CancelTask",
                "params": {"id": attempt.provider_run_id},
            },
            attempt.execution_profile,
        )
    except Exception:
        return


def _a2a_request(endpoint: str, message: dict, execution_profile: AgentExecutionProfile | None) -> dict:
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(message).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "A2A-Version": "1.0",
            "Idempotency-Key": str(message.get("id") or ""),
            **_auth_headers(execution_profile),
        },
        method="POST",
    )
    timeout = int(os.environ.get("MESH_AGENT_REQUEST_TIMEOUT_SECONDS", "30"))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Agent endpoint returned a non-object response")
    if payload.get("error"):
        raise ValueError(f"Agent endpoint rejected request: {payload['error']}")
    return payload


def _auth_headers(execution_profile: AgentExecutionProfile | None) -> dict[str, str]:
    if not execution_profile or not execution_profile.secret_reference:
        return {}
    reference = execution_profile.secret_reference
    if not reference.startswith("env:"):
        return {}
    secret = os.environ.get(reference[4:])
    return {"Authorization": f"Bearer {secret}"} if secret else {}


@transaction.atomic
def _record_start_failure(stage_run_id, attempt_id, message):
    stage = MeshStageRun.objects.select_for_update().select_related("loop_run__work_item").get(id=stage_run_id)
    if stage.status not in {MeshStageRun.Status.QUEUED, MeshStageRun.Status.RUNNING}:
        return
    actor_agent = stage.assigned_agent
    MeshRunAttempt.objects.filter(id=attempt_id).update(
        status=MeshRunAttempt.Status.FAILED,
        completed_at=timezone.now(),
        provider_state="START_FAILED",
        failure_code="start_failed",
        failure_message=message[:4000],
        usage={"start_error": message[:2000]},
    )
    leave_stage_unassigned(stage)
    MeshAuditEvent.objects.create(
        workspace_id=stage.workspace_id,
        project_id=stage.project_id,
        loop_run_id=stage.loop_run_id,
        work_item_id=stage.loop_run.work_item_id,
        actor_agent=actor_agent,
        event_type="stage.start_failed",
        payload={"stage_run_id": str(stage.id), "attempt_id": str(attempt_id), "message": message[:2000]},
        occurred_at=timezone.now(),
    )
    _notify_pm(stage, "Agent startup failed. The work item has returned to Unassigned for PM review.")


@transaction.atomic
def _record_completion(attempt_id: str, completion: dict, provider_state: str) -> dict:
    attempt = (
        MeshRunAttempt.objects.select_for_update(of=("self",))
        .select_related("stage_run__loop_run", "agent__user")
        .get(id=attempt_id, deleted_at__isnull=True)
    )
    if attempt.status in {MeshRunAttempt.Status.SUCCEEDED, MeshRunAttempt.Status.FAILED, MeshRunAttempt.Status.CANCELED}:
        return {"status": attempt.status, "attempt_id": str(attempt.id), "idempotent": True}
    usage = completion.get("usage") if isinstance(completion.get("usage"), dict) else {}
    attempt.provider_state = provider_state
    attempt.model = str(completion.get("model") or attempt.model)[:255]
    attempt.input_tokens = _usage_counter(usage.get("input_tokens", usage.get("inputTokens", usage.get("input", 0))))
    attempt.output_tokens = _usage_counter(usage.get("output_tokens", usage.get("outputTokens", usage.get("output", 0))))
    attempt.latency_ms = _usage_counter(completion.get("latency_ms", 0))
    attempt.usage = {
        **dict(attempt.usage or {}),
        "input_tokens": attempt.input_tokens,
        "output_tokens": attempt.output_tokens,
    }
    if isinstance(completion.get("provider"), str):
        attempt.usage["_mesh_model_provider"] = completion["provider"][:128]
    try:
        cost = Decimal(str(completion.get("cost")))
        if cost.is_finite() and 0 <= cost <= Decimal("99999999.999999"):
            attempt.cost = cost.quantize(Decimal("0.000001"))
            attempt.usage["_mesh_cost_reported"] = True
    except InvalidOperation:
        pass
    attempt.heartbeat_at = timezone.now()
    attempt.save(
        update_fields=[
            "provider_state",
            "model",
            "usage",
            "input_tokens",
            "output_tokens",
            "latency_ms",
            "cost",
            "heartbeat_at",
            "updated_at",
        ]
    )
    if completion.get("outcome") == "failed":
        return _record_terminal_failure(attempt.id, "agent_failed", "Agent reported a failed stage outcome")
    try:
        # Keep provider telemetry even when stage validation rolls back.
        with transaction.atomic():
            run = complete_stage(
                stage_run_id=str(attempt.stage_run_id),
                actor_agent=attempt.agent,
                outcome=completion.get("outcome", ""),
                evidence=completion.get("evidence") or [],
                selected_next_node_id=completion.get("selected_next_node_id"),
                handoff_target_agent_id=completion.get("handoff_target_agent_id"),
            )
    except ValueError as exc:
        return _record_terminal_failure(attempt.id, "invalid_evidence", str(exc))
    return {"status": run.status, "attempt_id": str(attempt.id), "run_id": str(run.id)}


def _usage_counter(value):
    return value if type(value) is int and 0 <= value <= 9223372036854775807 else 0


@transaction.atomic
def _record_terminal_failure(attempt_id: str, code: str, message: str) -> dict:
    attempt = (
        MeshRunAttempt.objects.select_for_update(of=("self",))
        .select_related("stage_run__loop_run__work_item")
        .get(id=attempt_id, deleted_at__isnull=True)
    )
    if attempt.status in {MeshRunAttempt.Status.SUCCEEDED, MeshRunAttempt.Status.FAILED, MeshRunAttempt.Status.CANCELED}:
        return {"status": attempt.status, "attempt_id": str(attempt.id), "idempotent": True}
    now = timezone.now()
    attempt.status = MeshRunAttempt.Status.FAILED
    attempt.failure_code = code[:64]
    attempt.failure_message = message[:4000]
    attempt.completed_at = now
    attempt.heartbeat_at = now
    attempt.save(
        update_fields=["status", "failure_code", "failure_message", "completed_at", "heartbeat_at", "updated_at"]
    )
    stage = attempt.stage_run
    maximum = _policy_limits(stage.project_id)[0]
    attempt_count = stage.attempts.filter(deleted_at__isnull=True).count()
    if attempt_count >= maximum:
        stage.status = MeshStageRun.Status.FAILED
        stage.completed_at = now
        stage.save(update_fields=["status", "completed_at", "updated_at"])
        MeshLoopRun.objects.filter(id=stage.loop_run_id).update(status=MeshLoopRun.Status.FAILED, completed_at=now)
        from plane.db.models import IssueAssignee

        IssueAssignee.objects.filter(issue_id=stage.loop_run.work_item_id, deleted_at__isnull=True).delete()
        final_status = "failed"
        _notify_pm(stage, "Agent execution exhausted the Project Policy attempt budget. The Loop has failed.")
    else:
        leave_stage_unassigned(stage)
        final_status = "waiting_for_assignee"
        _notify_pm(stage, "Agent execution failed. The work item is Unassigned and can be explicitly retried or reassigned.")
    MeshAuditEvent.objects.create(
        workspace_id=stage.workspace_id,
        project_id=stage.project_id,
        loop_run_id=stage.loop_run_id,
        work_item_id=stage.loop_run.work_item_id,
        actor_agent=attempt.agent,
        event_type="stage.attempt_failed",
        payload={"stage_run_id": str(stage.id), "attempt_id": str(attempt.id), "code": code, "message": message[:2000]},
        occurred_at=now,
    )
    return {"status": final_status, "attempt_id": str(attempt.id), "failure_code": code}


def _completion_artifact(task: dict) -> dict:
    for artifact in reversed(task.get("artifacts") or []):
        metadata = artifact.get("metadata") if isinstance(artifact.get("metadata"), dict) else {}
        if metadata.get("meshArtifactType") not in {None, "stage_completion"} and metadata.get("mesh_artifact_type") not in {None, "stage_completion"}:
            continue
        for part in artifact.get("parts") or []:
            text = part.get("text") if isinstance(part, dict) else None
            if not text:
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and isinstance(value.get("evidence"), list):
                return value
    raise ValueError("Completed Agent task did not return a mesh-stage-completion Artifact with evidence")


def _task_status_message(task: dict) -> str:
    message = (task.get("status") or {}).get("message") or {}
    return "\n".join(
        str(part.get("text")) for part in message.get("parts") or [] if isinstance(part, dict) and part.get("text")
    )


def _policy_limits(project_id) -> tuple[int, int]:
    policy = MeshProjectPolicy.objects.filter(
        project_id=project_id,
        status=MeshProjectPolicy.Status.PUBLISHED,
        deleted_at__isnull=True,
    ).first()
    budgets = (policy.policy or {}).get("budgets", {}) if policy else {}
    return max(int(budgets.get("default_max_attempts") or 2), 1), max(
        int(budgets.get("default_timeout_seconds") or 3600), 60
    )


def _attempt_timed_out(attempt: MeshRunAttempt) -> bool:
    if not attempt.started_at:
        return False
    timeout = _policy_limits(attempt.project_id)[1]
    return timezone.now() >= attempt.started_at + timedelta(seconds=timeout)


def _poll_delay(poll_count: int) -> int:
    return min(max(2 ** min(poll_count, 5), 2), 30)


def _notify_pm(stage, message: str) -> None:
    IssueComment.objects.create(
        workspace_id=stage.workspace_id,
        project_id=stage.project_id,
        issue_id=stage.loop_run.work_item_id,
        actor=None,
        comment_html=f"<p><strong>Mesh:</strong> {message}</p>",
        external_source="mesh-runner",
        external_id=f"stage:{stage.id}:{stage.status}",
    )
