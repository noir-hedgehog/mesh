# Copyright (c) 2026-present Mesh contributors
# SPDX-License-Identifier: AGPL-3.0-only

from unittest.mock import patch

import pytest

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
