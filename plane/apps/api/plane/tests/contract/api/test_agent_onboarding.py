# Copyright (c) 2026-present Mesh contributors
# SPDX-License-Identifier: AGPL-3.0-only

import time
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from rest_framework.test import APIClient

from plane.db.models import AgentExecutionProfile, AgentProfile, Project, ProjectMember, User, Workspace, WorkspaceMember


pytestmark = pytest.mark.django_db


def test_project_agent_invitation_is_scoped_single_use_and_approved_as_member(session_client, workspace):
    project = Project.objects.create(workspace=workspace, name="Build", identifier="BLD")
    other_project = Project.objects.create(workspace=workspace, name="Review", identifier="REV")
    applications = f"/api/workspaces/{workspace.slug}/agent-applications/"
    invitation = session_client.post(applications, {"project_id": str(project.id)}, format="json")
    assert invitation.status_code == 201
    assert invitation.data["project_name"] == project.name
    submit_path = urlsplit(invitation.data["submit_url"]).path
    agent = APIClient()
    payload = {
        "invite_token": invitation.data["invite_token"],
        "agent_id": "new-builder",
        "display_name": "新建 Agent",
        "runtime_provider": "openclaw",
        "default_model": "builder",
        "endpoint_url": "https://worker.example.ts.net/agents/new-builder/a2a",
        "capability_claims": ["code.write"],
        "denied_capabilities": ["deploy.production"],
        "requested_role": "admin",
        "project_id": str(other_project.id),
        "agent_card": {"available": True},
    }
    submitted = agent.post(submit_path, payload, format="json")
    assert submitted.status_code == 201
    assert submitted.data["status"] == "pending"
    assert set(submitted.data) == {"id", "status"}
    assert not AgentProfile.objects.filter(workspace=workspace, agent_id="new-builder").exists()

    replay = agent.post(submit_path, payload, format="json")
    assert replay.status_code == 200
    assert replay.data == submitted.data
    reused = agent.post(submit_path, {**payload, "agent_id": "another-agent"}, format="json")
    assert reused.status_code == 409

    waiting = session_client.get(applications, {"status": "pending", "project_id": str(project.id)})
    assert [row["id"] for row in waiting.data] == [submitted.data["id"]]
    assert waiting.data[0]["display_name"] == "新建 Agent"
    assert waiting.data[0]["requested_role"] == "member"
    assert waiting.data[0]["boundaries"] == {"denied_capabilities": ["deploy.production"]}
    assert session_client.get(applications, {"project_id": str(other_project.id)}).data == []

    detail = f"{applications}{submitted.data['id']}/"
    approved = session_client.patch(
        detail, {"action": "approve", "role": "admin", "project_id": str(other_project.id)}, format="json"
    )
    assert approved.status_code == 200
    assert approved.data["application"]["project_id"] == str(project.id)
    assert approved.data["account"]["token"]["token"]
    user_id = approved.data["account"]["user"]["id"]
    assert WorkspaceMember.objects.get(workspace=workspace, member_id=user_id).role == 15
    assert ProjectMember.objects.get(project=project, member_id=user_id).role == 15
    assert not ProjectMember.objects.filter(project=other_project, member_id=user_id).exists()
    profile = AgentProfile.objects.get(workspace=workspace, agent_id="new-builder")
    assert profile.agent_card == {}
    assert AgentExecutionProfile.objects.get(agent=profile).model == "builder"
    assert session_client.patch(detail, {"action": "approve"}, format="json").status_code == 409

    member_id = approved.data["account"]["workspace_member_id"]
    changed = session_client.patch(
        f"/api/workspaces/{workspace.slug}/agents/{member_id}/", {"role": "admin"}, format="json"
    )
    assert changed.status_code == 200
    assert changed.data["role"] == 20


def test_agent_invitation_cannot_be_used_in_another_workspace(session_client, workspace):
    other = Workspace.objects.create(name="Other", slug="other", owner=workspace.owner)
    foreign_project = Project.objects.create(workspace=other, name="Foreign", identifier="FRN")
    applications = f"/api/workspaces/{workspace.slug}/agent-applications/"
    assert session_client.post(applications, {"project_id": str(foreign_project.id)}, format="json").status_code == 404
    invitation = session_client.post(applications, {}, format="json")
    assert invitation.status_code == 201
    path = urlsplit(invitation.data["submit_url"]).path
    payload = {
        "invite_token": invitation.data["invite_token"],
        "agent_id": "workspace-agent",
        "display_name": "Workspace Agent",
    }
    agent = APIClient()
    assert agent.post(path, {**payload, "invite_token": "invalid"}, format="json").status_code == 403
    foreign_path = f"/api/workspaces/{other.slug}/agent-applications/submit/"
    assert agent.post(foreign_path, payload, format="json").status_code == 403
    with patch("django.core.signing.time.time", return_value=time.time() + 25 * 60 * 60):
        assert agent.post(path, payload, format="json").status_code == 403
    submitted = agent.post(path, payload, format="json")
    assert submitted.status_code == 201
    approved = session_client.patch(f"{applications}{submitted.data['id']}/", {"action": "approve"}, format="json")
    assert approved.status_code == 200
    assert approved.data["account"]["workspace_role"] == 15
    assert not ProjectMember.objects.filter(member_id=approved.data["account"]["user"]["id"]).exists()


def test_workspace_member_cannot_issue_agent_invitation(workspace):
    member = User.objects.create(username="regular-member", email="member@example.com")
    WorkspaceMember.objects.create(workspace=workspace, member=member, role=15)
    client = APIClient()
    client.force_authenticate(user=member)
    response = client.post(f"/api/workspaces/{workspace.slug}/agent-applications/", {}, format="json")
    assert response.status_code == 403


def test_creating_an_agent_does_not_replace_an_existing_account(session_client, workspace):
    url = f"/api/workspaces/{workspace.slug}/agents/"
    payload = {"agent_id": "agent-to-preserve", "display_name": "Original", "workspace_role": "admin"}
    initial = session_client.post(url, payload, format="json")
    assert initial.status_code == 201
    assert initial.data["workspace_role"] == 15
    assert session_client.post(url, {**payload, "display_name": "Replacement"}, format="json").status_code == 409
    profile = AgentProfile.objects.get(workspace=workspace, agent_id="agent-to-preserve")
    assert profile.user.display_name == "Original"
