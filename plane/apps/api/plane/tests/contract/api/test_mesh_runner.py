# Copyright (c) 2026-present Mesh contributors
# SPDX-License-Identifier: AGPL-3.0-only

from unittest.mock import patch
from datetime import timedelta

import pytest
from celery.exceptions import Retry
from django.utils import timezone

from plane.db.models import (
    AgentProfile, IssueAssignee, MeshFunctionalRole, MeshLoopDefinition,
    MeshLoopRun, MeshRunAttempt, MeshStageRun,
)
from plane.mesh.runtime import cancel_loop, validate_stage_evidence
from plane.bgtasks.mesh_runner import _prepare_attempt, _record_completion, _record_terminal_failure
from .test_mcp import native_mcp_data  # noqa: F401

pytestmark = pytest.mark.django_db


@pytest.fixture
def running_stage(native_mcp_data):
    data = native_mcp_data
    scope = {"workspace": data["workspace"], "project": data["project"]}
    agent = AgentProfile.objects.create(
        workspace=data["workspace"], user=data["users"]["iris"], agent_id="iris",
        status="active", agent_card={"available": True},
    )
    role = MeshFunctionalRole.objects.create(**scope, key="developer", name="Developer")
    definition = MeshLoopDefinition.objects.create(
        **scope, slug="runner-test", name="Runner test", version=1, status="published",
        source_yaml="", checksum="f" * 64,
        graph={"nodes": [{"id": "dev", "type": "stage"}, {"id": "done", "type": "complete"}],
               "edges": [{"from": "dev", "to": "done"}]},
    )
    run = MeshLoopRun.objects.create(
        **scope, work_item=data["issue"], definition=definition, definition_version=1, status="running",
    )
    stage = MeshStageRun.objects.create(
        **scope, loop_run=run, node_id="dev", functional_role=role,
        assigned_agent=agent, status="running", required_evidence=["summary"],
    )
    attempt = MeshRunAttempt.objects.create(
        **scope, stage_run=stage, agent=agent, provider="openclaw", model="test-model", status="running",
    )
    return data, stage, attempt


def test_late_provider_events_cannot_revive_canceled_run(running_stage):
    data, stage, attempt = running_stage
    cancel_loop(run=stage.loop_run, actor=data["users"]["hekate"], reason="test")
    assert _record_completion(str(attempt.id), {"evidence": []}, "TASK_STATE_COMPLETED")["status"] == "canceled"
    assert _record_terminal_failure(str(attempt.id), "poll_error", "offline")["status"] == "canceled"
    _, new_attempt = _prepare_attempt(str(stage.id))
    assert new_attempt is None
    stage.refresh_from_db()
    assert stage.status == "canceled"
    assert not IssueAssignee.objects.filter(issue=data["issue"]).exists()


def test_duplicate_terminal_completion_is_idempotent(running_stage):
    _, stage, attempt = running_stage
    completion = {"outcome": "succeeded", "evidence": [{"key": "summary", "kind": "text", "title": "Done"}]}
    first = _record_completion(str(attempt.id), completion, "TASK_STATE_COMPLETED")
    second = _record_completion(str(attempt.id), completion, "TASK_STATE_COMPLETED")
    assert first["status"] == "completed"
    assert second["idempotent"] is True
    assert stage.loop_run.stages.count() == 1


def test_provider_failure_returns_unassigned_then_exhausts_budget(running_stage):
    data, stage, attempt = running_stage
    with patch("plane.bgtasks.mesh_runner._policy_limits", return_value=(2, 60)):
        assert _record_terminal_failure(str(attempt.id), "failed", "test")["status"] == "waiting_for_assignee"
        stage.refresh_from_db()
        assert stage.assigned_agent_id is None
        data["issue"].refresh_from_db()
        assert data["issue"].state_id == data["states"]["todo"].id
        retry = MeshRunAttempt.objects.create(
            workspace=stage.workspace, project=stage.project, stage_run=stage,
            agent=attempt.agent, provider="openclaw", model="test", status="running",
        )
        assert _record_terminal_failure(str(retry.id), "failed", "test")["status"] == "failed"


@pytest.mark.parametrize("target", [None, "unavailable-agent"])
def test_missing_or_unavailable_handoff_waits_without_changing_business_state(running_stage, target):
    data, stage, attempt = running_stage
    definition = stage.loop_run.definition
    definition.graph = {
        "nodes": [{"id": "dev", "type": "stage"}, {"id": "test", "type": "stage", "roles": ["tester"]}],
        "edges": [{"from": "dev", "to": "test"}],
    }
    definition.save()
    result = _record_completion(str(attempt.id), {
        "outcome": "succeeded", "handoff_target_agent_id": target,
        "evidence": [{"key": "summary", "kind": "text", "title": "Done"}],
    }, "TASK_STATE_COMPLETED")
    assert result["status"] == "waiting_for_assignee"
    following = MeshStageRun.objects.get(loop_run=stage.loop_run, node_id="test")
    assert following.assigned_agent_id is None
    assert following.status == "waiting_for_assignee"
    assert not IssueAssignee.objects.filter(issue=data["issue"]).exists()
    data["issue"].refresh_from_db()
    assert data["issue"].state_id == data["states"]["todo"].id


@pytest.mark.parametrize("evidence", [
    [{"key": 42, "kind": "text", "title": "bad"}],
    [{"key": "summary", "kind": "text", "title": "bad", "extra": True}],
    [{"key": "other", "kind": "text", "title": "missing summary"}],
])
def test_evidence_rejects_invalid_shapes(running_stage, evidence):
    with pytest.raises(ValueError):
        validate_stage_evidence(running_stage[1], evidence)


def test_invalid_card_removes_previous_availability(running_stage):
    from plane.mesh.agent_cards import sync_agent_card

    agent = running_stage[2].agent
    agent.endpoint_url = "http://100.64.0.1:18890/agents/iris/a2a"
    agent.save()
    with patch("plane.mesh.agent_cards.urllib.request.urlopen") as request:
        request.return_value.__enter__.return_value.read.return_value = b'{"supportedInterfaces": []}'
        with pytest.raises(ValueError, match="1.0"):
            sync_agent_card(agent)
    agent.refresh_from_db()
    assert agent.agent_card["available"] is False


@pytest.mark.parametrize("outcome,evidence", [("succeeded", []), ("failed", [])])
def test_failed_completion_keeps_actual_runtime_metrics(running_stage, outcome, evidence):
    _, _, attempt = running_stage
    result = _record_completion(str(attempt.id), {
        "outcome": outcome, "evidence": evidence, "provider": "test-provider", "model": "actual-model",
        "usage": {"input": 42, "output": 11, "secret": "must-not-persist"}, "latency_ms": 900,
    }, "TASK_STATE_COMPLETED")
    attempt.refresh_from_db()
    assert result["status"] == "waiting_for_assignee"
    assert attempt.status == "failed"
    assert attempt.model == "actual-model"
    assert attempt.model_provider == "test-provider"
    assert (attempt.input_tokens, attempt.output_tokens, attempt.latency_ms) == (42, 11, 900)
    assert "secret" not in attempt.usage
    assert attempt.reported_cost is None


@pytest.mark.parametrize("cost,expected", [(None, None), (0, "0.000000"), ("0.125", "0.125000"), ("NaN", None), (-1, None)])
def test_reported_cost_distinguishes_unknown_from_zero(running_stage, cost, expected):
    _, _, attempt = running_stage
    _record_completion(str(attempt.id), {
        "outcome": "succeeded", "evidence": [{"key": "summary", "kind": "text", "title": "Done"}], "cost": cost,
    }, "TASK_STATE_COMPLETED")
    attempt.refresh_from_db()
    assert attempt.reported_cost == expected
    from plane.api.views.mcp import _compact_mesh_stage
    from plane.app.views.project.mesh import _stage_run_dict
    assert _compact_mesh_stage(attempt.stage_run)["attempts"][0]["cost"] == expected
    assert _stage_run_dict(attempt.stage_run)["attempts"][0]["cost"] == expected


def test_start_retries_reuse_attempt_then_return_unassigned(running_stage):
    from plane.bgtasks.mesh_runner import start_mesh_stage

    data, stage, attempt = running_stage
    with patch("plane.bgtasks.mesh_runner._send_agent_task", side_effect=OSError("Gateway offline")):
        for retries in range(3):
            start_mesh_stage.push_request(retries=retries, called_directly=False)
            try:
                with patch.object(start_mesh_stage, "retry", side_effect=Retry()) as retry:
                    if retries < 2:
                        with pytest.raises(Retry):
                            start_mesh_stage.run(str(stage.id))
                        retry.assert_called_once()
                    else:
                        assert start_mesh_stage.run(str(stage.id))["status"] == "waiting_for_assignee"
                        retry.assert_not_called()
            finally:
                start_mesh_stage.pop_request()
    stage.refresh_from_db()
    attempt.refresh_from_db()
    assert stage.attempts.count() == 1
    assert stage.assigned_agent_id is None
    assert attempt.failure_code == "start_failed"
    data["issue"].refresh_from_db()
    assert data["issue"].state_id == data["states"]["todo"].id


@pytest.mark.parametrize("timed_out", [False, True])
def test_poll_outage_or_timeout_returns_unassigned_without_reviving_attempt(running_stage, timed_out):
    from plane.bgtasks.mesh_runner import poll_mesh_agent_task

    data, stage, attempt = running_stage
    attempt.provider_run_id = "remote-task"
    attempt.started_at = timezone.now() - timedelta(hours=2 if timed_out else 0)
    attempt.save()
    poll_mesh_agent_task.push_request(retries=3, called_directly=False)
    try:
        with patch("plane.bgtasks.mesh_runner._get_agent_task", side_effect=OSError("Gateway offline")) as get_task, \
             patch("plane.bgtasks.mesh_runner._cancel_agent_task") as cancel_task:
            result = poll_mesh_agent_task.run(str(attempt.id))
            assert result["status"] == "waiting_for_assignee"
            assert result["failure_code"] == ("timeout" if timed_out else "poll_error")
            assert cancel_task.call_count == int(timed_out)
            assert get_task.call_count == int(not timed_out)
            assert poll_mesh_agent_task.run(str(attempt.id))["status"] == "failed"
            assert get_task.call_count == int(not timed_out)
    finally:
        poll_mesh_agent_task.pop_request()
    stage.refresh_from_db()
    assert stage.assigned_agent_id is None
    assert not IssueAssignee.objects.filter(issue=data["issue"]).exists()
    data["issue"].refresh_from_db()
    assert data["issue"].state_id == data["states"]["todo"].id
