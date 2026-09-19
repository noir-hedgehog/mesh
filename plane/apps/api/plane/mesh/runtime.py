# Copyright (c) 2026-present Mesh contributors
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from django.db import transaction
from django.conf import settings
from django.utils import timezone
from django.utils.html import escape

from plane.db.models import (
    AgentProfile,
    Issue,
    IssueAssignee,
    IssueComment,
    MeshApproval,
    MeshAuditEvent,
    MeshFunctionalRole,
    MeshLoopDefinition,
    MeshLoopRun,
    MeshRunAttempt,
    MeshStageRun,
    User,
)


def validate_stage_evidence(stage: MeshStageRun, evidence: list) -> list[dict]:
    if not isinstance(evidence, list):
        raise ValueError("evidence must be an array")
    normalized: list[dict] = []
    keys: set[str] = set()
    for index, item in enumerate(evidence):
        if not isinstance(item, dict):
            raise ValueError(f"evidence[{index}] must be an object")
        value = dict(item)
        unknown = set(value) - {"key", "kind", "title", "uri", "summary", "metadata"}
        if unknown:
            raise ValueError(f"evidence[{index}] contains unsupported fields: {', '.join(sorted(unknown))}")
        for field in ("key", "kind", "title"):
            text = value.get(field)
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"evidence[{index}].{field} is required")
            value[field] = text.strip()
        if value["key"] in keys:
            raise ValueError(f"duplicate evidence key: {value['key']}")
        keys.add(value["key"])
        if "uri" in value and value["uri"] is not None and not isinstance(value["uri"], str):
            raise ValueError(f"evidence[{index}].uri must be a string")
        if "summary" in value and value["summary"] is not None and not isinstance(value["summary"], str):
            raise ValueError(f"evidence[{index}].summary must be a string")
        if "metadata" in value and value["metadata"] is not None and not isinstance(value["metadata"], dict):
            raise ValueError(f"evidence[{index}].metadata must be an object")
        normalized.append(value)
    missing = [str(key) for key in stage.required_evidence or [] if str(key) not in keys]
    if missing:
        raise ValueError(f"missing required evidence keys: {', '.join(missing)}")
    return normalized


@transaction.atomic
def start_loop(*, definition: MeshLoopDefinition, work_item: Issue, actor: User) -> tuple[MeshLoopRun, bool]:
    work_item = Issue.objects.select_for_update().get(id=work_item.id)
    existing = MeshLoopRun.objects.filter(
        work_item=work_item,
        status__in=[
            MeshLoopRun.Status.QUEUED,
            MeshLoopRun.Status.RUNNING,
            MeshLoopRun.Status.WAITING_FOR_ASSIGNEE,
            MeshLoopRun.Status.AWAITING_APPROVAL,
        ],
        deleted_at__isnull=True,
    ).first()
    if existing:
        return existing, False
    first_stage = _first_stage_node(definition.graph)
    run = MeshLoopRun.objects.create(
        workspace=definition.workspace,
        project=definition.project,
        work_item=work_item,
        definition=definition,
        definition_version=definition.version,
        status=MeshLoopRun.Status.WAITING_FOR_ASSIGNEE if first_stage else MeshLoopRun.Status.COMPLETED,
        current_node_id=str(first_stage.get("id") if first_stage else ""),
        budget=dict((definition.graph.get("limits") or {}).get("budget") or {}),
        started_at=timezone.now(),
        completed_at=None if first_stage else timezone.now(),
    )
    if first_stage:
        role_keys = list(first_stage.get("roles") or [])
        role = MeshFunctionalRole.objects.filter(
            project_id=definition.project_id,
            key=role_keys[0] if len(role_keys) == 1 else None,
            deleted_at__isnull=True,
        ).first()
        MeshStageRun.objects.create(
            workspace=definition.workspace,
            project=definition.project,
            loop_run=run,
            node_id=str(first_stage["id"]),
            objective=str(first_stage.get("objective") or ""),
            functional_role=role,
            required_evidence=list(first_stage.get("evidence") or []),
        )
        IssueAssignee.objects.filter(issue=work_item, deleted_at__isnull=True).delete()
    MeshAuditEvent.objects.create(
        workspace=definition.workspace,
        project=definition.project,
        loop_run=run,
        work_item=work_item,
        actor_user=actor,
        actor_agent=AgentProfile.objects.filter(
            workspace=definition.workspace, user=actor, deleted_at__isnull=True
        ).first(),
        event_type="loop.started",
        payload={"definition_id": str(definition.id), "definition_version": definition.version},
        occurred_at=timezone.now(),
    )
    return run, True


@transaction.atomic
def cancel_loop(*, run: MeshLoopRun, actor: User, reason: str) -> MeshLoopRun:
    run = MeshLoopRun.objects.select_for_update().select_related("work_item").get(id=run.id)
    if run.status in {MeshLoopRun.Status.COMPLETED, MeshLoopRun.Status.FAILED, MeshLoopRun.Status.CANCELED}:
        return run
    now = timezone.now()
    MeshStageRun.objects.filter(
        loop_run=run,
        status__in=[
            MeshStageRun.Status.WAITING_FOR_ASSIGNEE,
            MeshStageRun.Status.QUEUED,
            MeshStageRun.Status.RUNNING,
            MeshStageRun.Status.AWAITING_APPROVAL,
        ],
        deleted_at__isnull=True,
    ).update(status=MeshStageRun.Status.CANCELED, completed_at=now)
    active_attempts = MeshRunAttempt.objects.filter(
        stage_run__loop_run=run,
        status__in=[MeshRunAttempt.Status.QUEUED, MeshRunAttempt.Status.RUNNING],
        deleted_at__isnull=True,
    )
    provider_attempt_ids = list(
        active_attempts.exclude(provider_run_id="").values_list("id", flat=True)
    )
    active_attempts.update(status=MeshRunAttempt.Status.CANCELED, completed_at=now, failure_code="run_canceled")
    IssueAssignee.objects.filter(issue=run.work_item, deleted_at__isnull=True).delete()
    run.status = MeshLoopRun.Status.CANCELED
    run.completed_at = now
    run.save(update_fields=["status", "completed_at", "updated_at"])
    MeshAuditEvent.objects.create(
        workspace_id=run.workspace_id,
        project_id=run.project_id,
        loop_run=run,
        work_item=run.work_item,
        actor_user=actor,
        actor_agent=AgentProfile.objects.filter(workspace_id=run.workspace_id, user=actor).first(),
        event_type="loop.canceled",
        payload={"reason": reason},
        occurred_at=now,
    )
    if provider_attempt_ids:
        from plane.bgtasks.mesh_runner import cancel_mesh_agent_attempt

        def enqueue_cancellations():
            for attempt_id in provider_attempt_ids:
                cancel_mesh_agent_attempt.delay(str(attempt_id))

        transaction.on_commit(enqueue_cancellations)
    return run


def _first_stage_node(graph: dict) -> dict | None:
    nodes = {str(node.get("id")): node for node in graph.get("nodes") or []}
    trigger = next((node for node in nodes.values() if node.get("type") == "trigger"), None)
    if not trigger:
        return None
    adjacency: dict[str, list[str]] = {}
    for edge in graph.get("edges") or []:
        adjacency.setdefault(str(edge.get("from")), []).append(str(edge.get("to")))
    pending = list(adjacency.get(str(trigger["id"]), []))
    visited: set[str] = set()
    while pending:
        node_id = pending.pop(0)
        if node_id in visited:
            continue
        visited.add(node_id)
        node = nodes.get(node_id)
        if node and node.get("type") == "stage":
            return node
        pending.extend(adjacency.get(node_id, []))
    return None


@transaction.atomic
def complete_stage(
    *,
    stage_run_id: str,
    actor_agent: AgentProfile,
    outcome: str,
    evidence: list,
    selected_next_node_id: str | None = None,
    handoff_target_agent_id: str | None = None,
) -> MeshLoopRun:
    stage = (
        MeshStageRun.objects.select_for_update(of=("self",))
        .select_related("loop_run__definition", "loop_run__work_item", "assigned_agent")
        .get(id=stage_run_id, deleted_at__isnull=True)
    )
    if stage.assigned_agent_id != actor_agent.id:
        raise ValueError("Only the Agent assigned to this Stage can complete it")
    if stage.status not in {MeshStageRun.Status.QUEUED, MeshStageRun.Status.RUNNING}:
        raise ValueError(f"Stage cannot be completed from status {stage.status}")
    if outcome not in {"succeeded", "failed"}:
        raise ValueError("outcome must be succeeded or failed")
    normalized_evidence = validate_stage_evidence(stage, evidence) if outcome == "succeeded" else []
    attempt = stage.attempts.filter(deleted_at__isnull=True).order_by("-created_at").first()
    now = timezone.now()
    if attempt:
        attempt.status = MeshRunAttempt.Status.SUCCEEDED if outcome == "succeeded" else MeshRunAttempt.Status.FAILED
        attempt.evidence = normalized_evidence
        attempt.completed_at = now
        attempt.save(update_fields=["status", "evidence", "completed_at", "updated_at"])
    if outcome == "failed":
        if attempt:
            from plane.bgtasks.mesh_runner import _record_terminal_failure

            # The failure service owns retry budgets for both MCP and A2A.
            attempt.status = MeshRunAttempt.Status.RUNNING
            attempt.save(update_fields=["status"])
            _record_terminal_failure(attempt.id, "agent_failed", "Agent reported a failed stage outcome")
            stage.loop_run.refresh_from_db()
            return stage.loop_run
        stage.status = MeshStageRun.Status.FAILED
        stage.completed_at = now
        stage.save(update_fields=["status", "completed_at", "updated_at"])
        MeshLoopRun.objects.filter(id=stage.loop_run_id).update(status=MeshLoopRun.Status.FAILED)
        _audit(stage, actor_agent=actor_agent, event_type="stage.failed", payload={"evidence": normalized_evidence})
        _timeline_stage_result(stage, actor_agent, attempt, outcome, normalized_evidence, handoff_target_agent_id)
        stage.loop_run.refresh_from_db()
        return stage.loop_run

    stage.status = MeshStageRun.Status.SUCCEEDED
    stage.completed_at = now
    stage.save(update_fields=["status", "completed_at", "updated_at"])
    _audit(stage, actor_agent=actor_agent, event_type="stage.succeeded", payload={"evidence": normalized_evidence})
    _timeline_stage_result(stage, actor_agent, attempt, outcome, normalized_evidence, handoff_target_agent_id)
    run = _advance_from_node(stage=stage, node_id=stage.node_id, selected_next_node_id=selected_next_node_id)
    if handoff_target_agent_id and run.status == MeshLoopRun.Status.WAITING_FOR_ASSIGNEE:
        _assign_handoff_target(run=run, actor_agent=actor_agent, target_agent_id=handoff_target_agent_id)
        run.refresh_from_db()
    return run


def _assign_handoff_target(*, run: MeshLoopRun, actor_agent: AgentProfile, target_agent_id: str) -> None:
    from plane.mesh.discovery import assign_stage, list_eligible_agents

    stage = (
        MeshStageRun.objects.filter(
            loop_run=run,
            status=MeshStageRun.Status.WAITING_FOR_ASSIGNEE,
            deleted_at__isnull=True,
        )
        .select_related("functional_role")
        .order_by("-created_at")
        .first()
    )
    if not stage:
        return
    node = next(
        (item for item in run.definition.graph.get("nodes", []) if str(item.get("id")) == stage.node_id),
        {},
    )
    roles = [stage.functional_role.key] if stage.functional_role else list(node.get("roles") or [])
    required = list(node.get("required_capabilities") or [])
    candidates = {
        item.agent_id: item
        for item in list_eligible_agents(
            project_id=str(run.project_id),
            role_keys=roles,
            required_capabilities=required,
        )
    }
    candidate = candidates.get(target_agent_id)
    if not candidate or not candidate.available:
        MeshAuditEvent.objects.create(
            workspace_id=run.workspace_id,
            project_id=run.project_id,
            loop_run=run,
            work_item_id=run.work_item_id,
            actor_user_id=actor_agent.user_id,
            actor_agent=actor_agent,
            event_type="handoff.target_unavailable",
            payload={"stage_run_id": str(stage.id), "target_agent_id": target_agent_id},
            occurred_at=timezone.now(),
        )
        return
    target_agent = AgentProfile.objects.get(
        workspace_id=run.workspace_id,
        agent_id=target_agent_id,
        status=AgentProfile.Status.ACTIVE,
        deleted_at__isnull=True,
    )
    role = (
        MeshFunctionalRole.objects.filter(
            project_id=run.project_id,
            key__in=set(candidate.functional_roles) & set(roles),
            deleted_at__isnull=True,
        )
        .order_by("sort_order")
        .first()
    )
    if not role:
        return
    try:
        assign_stage(
            stage_run=stage,
            target_agent=target_agent,
            target_role=role,
            selected_by_user=actor_agent.user,
            reason="Selected by the previous Agent at stage completion",
        )
    except ValueError as exc:
        MeshAuditEvent.objects.create(
            workspace_id=run.workspace_id,
            project_id=run.project_id,
            loop_run=run,
            work_item_id=run.work_item_id,
            actor_user_id=actor_agent.user_id,
            actor_agent=actor_agent,
            event_type="handoff.policy_rejected",
            payload={"stage_run_id": str(stage.id), "target_agent_id": target_agent_id, "message": str(exc)},
            occurred_at=timezone.now(),
        )
        return
    queue_stage_start_on_commit(str(stage.id))


@transaction.atomic
def resolve_approval(*, approval: MeshApproval, reviewer: User, approved: bool, decision_note: str) -> MeshLoopRun:
    approval = (
        MeshApproval.objects.select_for_update(of=("self",))
        .select_related("loop_run__definition", "loop_run__work_item", "stage_run")
        .get(id=approval.id)
    )
    if approval.status != MeshApproval.Status.PENDING:
        raise ValueError("Approval has already been resolved")
    approval.status = MeshApproval.Status.APPROVED if approved else MeshApproval.Status.REJECTED
    approval.reviewer = reviewer
    approval.decision_note = decision_note
    approval.resolved_at = timezone.now()
    approval.save(update_fields=["status", "reviewer", "decision_note", "resolved_at", "updated_at"])
    if not approved:
        MeshStageRun.objects.filter(id=approval.stage_run_id).update(status=MeshStageRun.Status.FAILED)
        MeshLoopRun.objects.filter(id=approval.loop_run_id).update(status=MeshLoopRun.Status.FAILED)
        approval.loop_run.refresh_from_db()
        return approval.loop_run
    approval.stage_run.status = MeshStageRun.Status.SUCCEEDED
    approval.stage_run.save(update_fields=["status", "updated_at"])
    return _advance_from_node(stage=approval.stage_run, node_id=_approval_node_id(approval))


def _advance_from_node(*, stage: MeshStageRun, node_id: str, selected_next_node_id: str | None = None) -> MeshLoopRun:
    run = MeshLoopRun.objects.select_for_update().select_related("definition", "work_item").get(id=stage.loop_run_id)
    graph = run.definition.graph
    nodes = {str(node.get("id")): node for node in graph.get("nodes") or []}
    adjacency: dict[str, list[str]] = {}
    for edge in graph.get("edges") or []:
        adjacency.setdefault(str(edge.get("from")), []).append(str(edge.get("to")))
    current = node_id
    visited = set()
    while True:
        targets = adjacency.get(current, [])
        if len(targets) > 1:
            if not selected_next_node_id or selected_next_node_id not in targets:
                raise ValueError(f"Node {current} requires selected_next_node_id from: {', '.join(targets)}")
            targets = [selected_next_node_id]
            selected_next_node_id = None
        if not targets:
            raise ValueError(f"Loop node {current} has no outgoing transition")
        current = targets[0]
        if not _record_transition(run):
            IssueAssignee.objects.filter(issue_id=run.work_item_id, deleted_at__isnull=True).delete()
            MeshAuditEvent.objects.create(
                workspace_id=run.workspace_id,
                project_id=run.project_id,
                loop_run=run,
                work_item_id=run.work_item_id,
                event_type="loop.transition_limit_exceeded",
                payload={"max_transitions": (graph.get("limits") or {}).get("max_transitions")},
                occurred_at=timezone.now(),
            )
            return run
        if current in visited:
            raise ValueError("Loop transition revisited a node before reaching a runnable boundary")
        visited.add(current)
        node = nodes[current]
        node_type = node.get("type")
        run.current_node_id = current
        if node_type == "complete":
            run.status = MeshLoopRun.Status.COMPLETED
            run.completed_at = timezone.now()
            run.save(update_fields=["current_node_id", "status", "budget", "completed_at", "updated_at"])
            IssueAssignee.objects.filter(issue_id=run.work_item_id, deleted_at__isnull=True).delete()
            return run
        if node_type == "approval":
            stage.status = MeshStageRun.Status.AWAITING_APPROVAL
            stage.save(update_fields=["status", "updated_at"])
            MeshApproval.objects.get_or_create(
                workspace_id=run.workspace_id,
                project_id=run.project_id,
                loop_run=run,
                stage_run=stage,
                status=MeshApproval.Status.PENDING,
            )
            run.status = MeshLoopRun.Status.AWAITING_APPROVAL
            run.save(update_fields=["current_node_id", "status", "budget", "updated_at"])
            return run
        if node_type == "wait":
            run.status = MeshLoopRun.Status.QUEUED
            run.save(update_fields=["current_node_id", "status", "budget", "updated_at"])
            queue_wait_resume_on_commit(
                stage_run_id=str(stage.id),
                wait_node_id=current,
                countdown=max(int(node.get("duration_seconds") or 0), 0),
            )
            return run
        if node_type == "stage":
            role_keys = list(node.get("roles") or [])
            role = MeshFunctionalRole.objects.filter(
                project_id=run.project_id,
                key=role_keys[0] if len(role_keys) == 1 else None,
                deleted_at__isnull=True,
            ).first()
            MeshStageRun.objects.create(
                workspace_id=run.workspace_id,
                project_id=run.project_id,
                loop_run=run,
                node_id=current,
                objective=str(node.get("objective") or ""),
                functional_role=role,
                required_evidence=list(node.get("evidence") or []),
            )
            IssueAssignee.objects.filter(issue_id=run.work_item_id, deleted_at__isnull=True).delete()
            run.status = MeshLoopRun.Status.WAITING_FOR_ASSIGNEE
            run.save(update_fields=["current_node_id", "status", "budget", "updated_at"])
            return run


@transaction.atomic
def resume_wait(*, stage_run_id: str, wait_node_id: str) -> MeshLoopRun:
    stage = (
        MeshStageRun.objects.select_for_update(of=("self",))
        .select_related("loop_run__definition", "loop_run__work_item")
        .get(id=stage_run_id, deleted_at__isnull=True)
    )
    run = MeshLoopRun.objects.select_for_update().get(id=stage.loop_run_id)
    if run.status != MeshLoopRun.Status.QUEUED or run.current_node_id != wait_node_id:
        return run
    return _advance_from_node(stage=stage, node_id=wait_node_id)


def _record_transition(run: MeshLoopRun) -> bool:
    budget = dict(run.budget or {})
    count = int(budget.get("_mesh_transition_count") or 0) + 1
    budget["_mesh_transition_count"] = count
    run.budget = budget
    maximum = (run.definition.graph.get("limits") or {}).get("max_transitions")
    if isinstance(maximum, int) and count > maximum:
        run.status = MeshLoopRun.Status.FAILED
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "budget", "completed_at", "updated_at"])
        return False
    return True


def _approval_node_id(approval: MeshApproval) -> str:
    run = approval.loop_run
    graph = run.definition.graph
    outgoing = [
        str(edge.get("to")) for edge in graph.get("edges") or [] if str(edge.get("from")) == approval.stage_run.node_id
    ]
    nodes = {str(node.get("id")): node for node in graph.get("nodes") or []}
    return next(
        (node_id for node_id in outgoing if nodes.get(node_id, {}).get("type") == "approval"), run.current_node_id
    )


def _audit(stage, *, actor_agent, event_type, payload):
    MeshAuditEvent.objects.create(
        workspace_id=stage.workspace_id,
        project_id=stage.project_id,
        loop_run_id=stage.loop_run_id,
        work_item_id=stage.loop_run.work_item_id,
        actor_user_id=actor_agent.user_id,
        actor_agent=actor_agent,
        event_type=event_type,
        payload={"stage_run_id": str(stage.id), **payload},
        occurred_at=timezone.now(),
    )


def _timeline_stage_result(stage, actor_agent, attempt, outcome, evidence, handoff_target_agent_id):
    provider = escape(attempt.provider if attempt else actor_agent.runtime_provider or "unknown")
    model = escape(attempt.model if attempt else "unspecified")
    evidence_keys = ", ".join(escape(str(item["key"])) for item in evidence) or "none"
    handoff = escape(handoff_target_agent_id or "Unassigned")
    IssueComment.objects.get_or_create(
        workspace_id=stage.workspace_id,
        project_id=stage.project_id,
        issue_id=stage.loop_run.work_item_id,
        external_source="mesh-runtime",
        external_id=f"stage:{stage.id}:{outcome}",
        defaults={
            "actor": actor_agent.user,
            "created_by": actor_agent.user,
            "comment_html": (
                f"<p><strong>Mesh Stage {escape(stage.node_id)}: {escape(outcome)}</strong></p>"
                f"<p>Agent: {escape(actor_agent.agent_id)}; provider/model: {provider}/{model}; "
                f"Evidence: {evidence_keys}; handoff: {handoff}.</p>"
            ),
        },
    )


def queue_stage_start_on_commit(stage_run_id: str) -> None:
    if not getattr(settings, "MESH_RUNNER_AUTOSTART", True):
        return
    from plane.bgtasks.mesh_runner import start_mesh_stage

    transaction.on_commit(lambda: start_mesh_stage.delay(stage_run_id))


def queue_wait_resume_on_commit(*, stage_run_id: str, wait_node_id: str, countdown: int) -> None:
    if not getattr(settings, "MESH_RUNNER_AUTOSTART", True):
        return
    from plane.bgtasks.mesh_runner import resume_mesh_wait

    transaction.on_commit(lambda: resume_mesh_wait.apply_async(args=[stage_run_id, wait_node_id], countdown=countdown))
