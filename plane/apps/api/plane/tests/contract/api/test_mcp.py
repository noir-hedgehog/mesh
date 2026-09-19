# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import json
from unittest.mock import patch

import pytest

from plane.db.models import (
    APIToken,
    AgentRegistrationApplication,
    AgentExecutionProfile,
    AgentProfile,
    Cycle,
    CycleIssue,
    Issue,
    IssueActivity,
    IssueAssignee,
    IssueComment,
    IssueLink,
    IssueRelation,
    Label,
    Module,
    ModuleIssue,
    MeshFunctionalRole,
    MeshHandoff,
    MeshLoopDefinition,
    MeshLoopRun,
    MeshProjectMemberRole,
    MeshStageRun,
    Project,
    ProjectMember,
    State,
    User,
    Workspace,
    WorkspaceMember,
)


pytestmark = pytest.mark.django_db


def mcp_call(client, slug, name, arguments=None):
    data = mcp_raw_call(client, slug, name, arguments)
    if "error" in data:
        return data
    return json.loads(data["result"]["content"][0]["text"])


def mcp_raw_call(client, slug, name, arguments=None):
    response = client.post(
        f"/api/v1/workspaces/{slug}/mcp/",
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
        format="json",
    )
    assert response.status_code == 200
    return response.json()


@pytest.fixture
def native_mcp_data(api_client):
    human = User.objects.create(email="admin@example.com", username="admin", display_name="Admin")
    hekate = User.objects.create(
        email="agent-hekate@agentpm.local", username="agent-hekate", display_name="Hekate", is_bot=True
    )
    iris = User.objects.create(
        email="agent-iris@agentpm.local", username="agent-iris", display_name="Iris", is_bot=True
    )
    taichi = User.objects.create(
        email="agent-taichi@agentpm.local", username="agent-taichi", display_name="Taichi", is_bot=True
    )
    workspace = Workspace.objects.create(name="AgentPM", slug="agentpm", owner=human)
    WorkspaceMember.objects.create(workspace=workspace, member=human, role=20)
    WorkspaceMember.objects.create(workspace=workspace, member=hekate, role=20)
    WorkspaceMember.objects.create(workspace=workspace, member=iris, role=15)
    WorkspaceMember.objects.create(workspace=workspace, member=taichi, role=5)
    project = Project.objects.create(name="AgentPM MVP", identifier="AGPM", workspace=workspace, project_lead=hekate)
    ProjectMember.objects.create(workspace=workspace, project=project, member=hekate, role=20)
    ProjectMember.objects.create(workspace=workspace, project=project, member=iris, role=15)
    ProjectMember.objects.create(workspace=workspace, project=project, member=taichi, role=5)
    todo = State.objects.create(
        workspace=workspace, project=project, name="Todo", color="#000", group="unstarted", default=True
    )
    done = State.objects.create(workspace=workspace, project=project, name="Done", color="#0f0", group="completed")
    label = Label.objects.create(workspace=workspace, project=project, name="MCP", color="#f00")
    issue = Issue.objects.create(workspace=workspace, project=project, name="Native MCP task", state=todo)
    related_issue = Issue.objects.create(workspace=workspace, project=project, name="Related task", state=todo)
    IssueAssignee.objects.create(workspace=workspace, project=project, issue=issue, assignee=iris)
    IssueComment.objects.create(
        workspace=workspace,
        project=project,
        issue=issue,
        actor=iris,
        comment_html="<p>Existing comment</p>",
    )
    IssueActivity.objects.create(
        workspace=workspace,
        project=project,
        issue=issue,
        actor=iris,
        verb="created",
        field="name",
        new_value="Native MCP task",
    )
    IssueLink.objects.create(
        workspace=workspace,
        project=project,
        issue=issue,
        title="Spec",
        url="https://example.com/spec",
    )
    IssueRelation.objects.create(
        workspace=workspace,
        project=project,
        issue=issue,
        related_issue=related_issue,
        relation_type="relates_to",
    )
    tokens = {
        "hekate": APIToken.objects.create(user=hekate, workspace=workspace, token="mcp-hekate-token", user_type=1),
        "iris": APIToken.objects.create(user=iris, workspace=workspace, token="mcp-iris-token", user_type=1),
        "taichi": APIToken.objects.create(user=taichi, workspace=workspace, token="mcp-taichi-token", user_type=1),
    }
    return {
        "client": api_client,
        "workspace": workspace,
        "project": project,
        "issue": issue,
        "related_issue": related_issue,
        "label": label,
        "states": {"todo": todo, "done": done},
        "users": {"hekate": hekate, "iris": iris, "taichi": taichi},
        "tokens": tokens,
    }


def authenticate(client, token):
    client.credentials(HTTP_X_API_KEY=token.token)
    return client


def test_native_mcp_requires_api_key(api_client, native_mcp_data):
    response = api_client.post(
        "/api/v1/workspaces/agentpm/mcp/",
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        format="json",
    )

    assert response.status_code in {401, 403}


def test_native_mcp_initialize_and_tools_list(native_mcp_data):
    client = authenticate(native_mcp_data["client"], native_mcp_data["tokens"]["iris"])

    initialize = client.post(
        "/api/v1/workspaces/agentpm/mcp/",
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        format="json",
    )
    tools = client.post(
        "/api/v1/workspaces/agentpm/mcp/",
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        format="json",
    )

    assert initialize.status_code == 200
    assert initialize.json()["result"]["serverInfo"]["name"] == "plane-native"
    assert tools.status_code == 200
    tool_names = [tool["name"] for tool in tools.json()["result"]["tools"]]
    assert "plane_list_work_items" in tool_names
    assert "plane_get_me" in tool_names
    assert "plane_update_work_item" in tool_names
    assert "plane_add_work_item_relation" in tool_names
    assert "mesh_start_loop" in tool_names
    assert "mesh_cancel_run" in tool_names
    assert "mesh_complete_stage" in tool_names
    assert "plane_add_project_member" not in tool_names
    assert "plane_add_workspace_user_to_project" not in tool_names

    create_item = next(tool for tool in tools.json()["result"]["tools"] if tool["name"] == "plane_create_work_item")
    target_schema = create_item["inputSchema"]["properties"]["target_agent_id"]
    assert "plane_list_agent_accounts" in target_schema["description"]
    assert "Do not pass Plane user UUID/email" in target_schema["description"]
    completion = next(tool for tool in tools.json()["result"]["tools"] if tool["name"] == "mesh_complete_stage")
    evidence_schema = completion["inputSchema"]["properties"]["evidence"]["items"]
    assert evidence_schema["required"] == ["key", "kind", "title"]
    assert "handoff_target_agent_id" in completion["inputSchema"]["properties"]


def test_native_mcp_admin_tools_list_includes_project_member_management(native_mcp_data):
    client = authenticate(native_mcp_data["client"], native_mcp_data["tokens"]["hekate"])

    tools = client.post(
        "/api/v1/workspaces/agentpm/mcp/",
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        format="json",
    )
    tool_names = [tool["name"] for tool in tools.json()["result"]["tools"]]
    add_member = next(tool for tool in tools.json()["result"]["tools"] if tool["name"] == "plane_add_project_member")

    assert "plane_add_project_member" in tool_names
    assert "plane_add_workspace_user_to_project" in tool_names
    assert "member_agent_id" in add_member["inputSchema"]["properties"]


def test_native_mcp_rejects_agent_id_identity_switch(native_mcp_data):
    client = authenticate(native_mcp_data["client"], native_mcp_data["tokens"]["iris"])

    result = mcp_call(
        client,
        "agentpm",
        "plane_list_work_items",
        {"project_id": str(native_mcp_data["project"].id), "agent_id": "hekate"},
    )

    assert result["error"]["message"].startswith("agent_id is not accepted")
    assert result["error"]["type"] == "identity_switch_rejected"


def test_native_mcp_identity_and_members_do_not_expose_token_values(native_mcp_data):
    client = authenticate(native_mcp_data["client"], native_mcp_data["tokens"]["iris"])

    me = mcp_call(client, "agentpm", "plane_get_me", {})
    members = mcp_call(
        client,
        "agentpm",
        "plane_list_project_members",
        {"project_id": str(native_mcp_data["project"].id)},
    )
    payload = json.dumps({"me": me, "members": members})

    assert me["user"]["email"] == "agent-iris@agentpm.local"
    assert me["agent_id"] == "iris"
    assert any(member["agent_id"] == "taichi" for member in members["members"])
    assert "mcp-iris-token" not in payload
    assert "agent-taichi@agentpm.local" in payload


def test_native_mcp_guest_can_read_context_but_not_write(native_mcp_data):
    client = authenticate(native_mcp_data["client"], native_mcp_data["tokens"]["taichi"])
    project_id = str(native_mcp_data["project"].id)
    issue_id = str(native_mcp_data["issue"].id)

    labels = mcp_call(client, "agentpm", "plane_list_labels", {"project_id": project_id})
    comments = mcp_call(client, "agentpm", "plane_list_work_item_comments", {"work_item_id": issue_id})
    activity = mcp_call(client, "agentpm", "plane_list_work_item_activity", {"work_item_id": issue_id})
    links = mcp_call(client, "agentpm", "plane_list_work_item_links", {"work_item_id": issue_id})
    relations = mcp_call(client, "agentpm", "plane_list_work_item_relations", {"work_item_id": issue_id})
    update_denied = mcp_call(
        client, "agentpm", "plane_update_work_item", {"work_item_id": issue_id, "priority": "high"}
    )
    link_denied = mcp_call(
        client,
        "agentpm",
        "plane_add_work_item_link",
        {"work_item_id": issue_id, "url": "https://example.com/guest"},
    )
    relation_denied = mcp_call(
        client,
        "agentpm",
        "plane_add_work_item_relation",
        {
            "work_item_id": issue_id,
            "relation_type": "blocked_by",
            "related_work_item_id": str(native_mcp_data["related_issue"].id),
        },
    )

    assert labels["labels"][0]["name"] == "MCP"
    assert comments["comments"][0]["body"] == "Existing comment"
    assert activity["activities"][0]["verb"] == "created"
    assert links["links"][0]["url"] == "https://example.com/spec"
    assert str(native_mcp_data["related_issue"].id) in relations["relations"]["relates_to"]
    assert "insufficient project role" in update_denied["error"]["message"]
    assert "insufficient project role" in link_denied["error"]["message"]
    assert "insufficient project role" in relation_denied["error"]["message"]


def test_native_mcp_comment_author_is_authenticated_bot(native_mcp_data):
    client = authenticate(native_mcp_data["client"], native_mcp_data["tokens"]["iris"])

    result = mcp_call(
        client,
        "agentpm",
        "plane_add_comment",
        {
            "project_id": str(native_mcp_data["project"].id),
            "work_item_id": str(native_mcp_data["issue"].id),
            "body": "Looks good",
        },
    )

    comment = IssueComment.objects.get(pk=result["comment"]["id"])
    assert comment.actor_id == native_mcp_data["users"]["iris"].id


def test_native_mcp_member_updates_only_assigned_work_items(native_mcp_data):
    client = authenticate(native_mcp_data["client"], native_mcp_data["tokens"]["iris"])

    success = mcp_call(
        client,
        "agentpm",
        "plane_update_status",
        {
            "project_id": str(native_mcp_data["project"].id),
            "work_item_id": str(native_mcp_data["issue"].id),
            "status": "Done",
        },
    )
    assert success["work_item"]["state"]["name"] == "Done"

    other = Issue.objects.create(
        workspace=native_mcp_data["workspace"],
        project=native_mcp_data["project"],
        name="Unassigned",
        state=native_mcp_data["states"]["todo"],
    )
    denied = mcp_call(
        client,
        "agentpm",
        "plane_update_status",
        {"project_id": str(native_mcp_data["project"].id), "work_item_id": str(other.id), "status": "Done"},
    )
    assert "member agents can only update" in denied["error"]["message"]


def test_native_mcp_member_updates_assigned_work_item_fields(native_mcp_data):
    client = authenticate(native_mcp_data["client"], native_mcp_data["tokens"]["iris"])
    issue_id = str(native_mcp_data["issue"].id)

    updated = mcp_call(
        client,
        "agentpm",
        "plane_update_work_item",
        {
            "work_item_id": issue_id,
            "name": "Updated by Iris",
            "priority": "high",
            "status": "Done",
            "start_date": "2026-01-01",
            "target_date": "2026-01-03",
            "labels": [str(native_mcp_data["label"].id)],
            "assignees": ["iris"],
        },
    )

    assert updated["work_item"]["name"] == "Updated by Iris"
    assert updated["work_item"]["priority"] == "high"
    assert updated["work_item"]["state"]["name"] == "Done"
    assert updated["work_item"]["labels"][0]["name"] == "MCP"
    assert updated["work_item"]["assignees"][0]["email"] == "agent-iris@agentpm.local"


def test_native_mcp_member_link_and_relation_write_requires_assignment(native_mcp_data):
    client = authenticate(native_mcp_data["client"], native_mcp_data["tokens"]["iris"])
    issue_id = str(native_mcp_data["issue"].id)
    other = Issue.objects.create(
        workspace=native_mcp_data["workspace"],
        project=native_mcp_data["project"],
        name="Unassigned relation task",
        state=native_mcp_data["states"]["todo"],
    )

    link = mcp_call(
        client,
        "agentpm",
        "plane_add_work_item_link",
        {"work_item_id": issue_id, "url": "https://example.com/iris", "title": "Iris"},
    )
    relation = mcp_call(
        client,
        "agentpm",
        "plane_add_work_item_relation",
        {"work_item_id": issue_id, "relation_type": "blocked_by", "related_work_item_id": str(other.id)},
    )
    link_denied = mcp_call(
        client,
        "agentpm",
        "plane_add_work_item_link",
        {"work_item_id": str(other.id), "url": "https://example.com/denied"},
    )

    assert link["link"]["url"] == "https://example.com/iris"
    assert relation["relations"][0]["relation_type"] == "blocked_by"
    assert "member agents can only write" in link_denied["error"]["message"]


def test_native_mcp_guest_cannot_create_or_update(native_mcp_data):
    client = authenticate(native_mcp_data["client"], native_mcp_data["tokens"]["taichi"])

    create_result = mcp_call(
        client,
        "agentpm",
        "plane_create_work_item",
        {"project_id": str(native_mcp_data["project"].id), "name": "Guest attempt"},
    )
    update_result = mcp_call(
        client,
        "agentpm",
        "plane_update_status",
        {
            "project_id": str(native_mcp_data["project"].id),
            "work_item_id": str(native_mcp_data["issue"].id),
            "status": "Done",
        },
    )

    assert "insufficient project role" in create_result["error"]["message"]
    assert "insufficient project role" in update_result["error"]["message"]


def test_native_mcp_admin_can_manage_links_and_relations(native_mcp_data):
    client = authenticate(native_mcp_data["client"], native_mcp_data["tokens"]["hekate"])
    issue_id = str(native_mcp_data["issue"].id)

    link = mcp_call(
        client,
        "agentpm",
        "plane_add_work_item_link",
        {"work_item_id": issue_id, "url": "https://example.com/admin", "title": "Admin"},
    )
    updated_link = mcp_call(
        client,
        "agentpm",
        "plane_update_work_item_link",
        {"work_item_id": issue_id, "link_id": link["link"]["id"], "title": "Updated Admin"},
    )
    relation = mcp_call(
        client,
        "agentpm",
        "plane_add_work_item_relation",
        {
            "work_item_id": issue_id,
            "relation_type": "blocking",
            "related_work_item_id": str(native_mcp_data["related_issue"].id),
        },
    )
    deleted_link = mcp_call(
        client,
        "agentpm",
        "plane_delete_work_item_link",
        {"work_item_id": issue_id, "link_id": link["link"]["id"]},
    )
    deleted_relation = mcp_call(
        client,
        "agentpm",
        "plane_delete_work_item_relation",
        {"work_item_id": issue_id, "relation_id": relation["relations"][0]["id"]},
    )

    assert updated_link["link"]["title"] == "Updated Admin"
    assert relation["relations"][0]["relation_type"] == "blocking"
    assert deleted_link["deleted"] is True
    assert deleted_relation["deleted"] is True


def test_native_mcp_admin_can_assign_and_list_accounts_without_secrets(native_mcp_data):
    client = authenticate(native_mcp_data["client"], native_mcp_data["tokens"]["hekate"])

    assigned = mcp_call(
        client,
        "agentpm",
        "plane_assign_work_item",
        {
            "project_id": str(native_mcp_data["project"].id),
            "work_item_id": str(native_mcp_data["issue"].id),
            "target_agent_id": "iris",
        },
    )
    accounts = mcp_call(client, "agentpm", "plane_list_agent_accounts", {})
    payload = json.dumps(accounts)

    assert assigned["work_item"]["id"] == str(native_mcp_data["issue"].id)
    assert any(agent["agent_id"] == "iris" for agent in accounts["agents"])
    assert "agent-iris@agentpm.local" in payload
    assert "mcp-iris-token" not in payload


def test_native_mcp_admin_can_add_existing_agent_to_project(native_mcp_data):
    client = authenticate(native_mcp_data["client"], native_mcp_data["tokens"]["hekate"])
    project = Project.objects.create(
        name="Agent Sandbox",
        identifier="AGSB",
        workspace=native_mcp_data["workspace"],
        project_lead=native_mcp_data["users"]["hekate"],
    )
    ProjectMember.objects.create(
        workspace=native_mcp_data["workspace"],
        project=project,
        member=native_mcp_data["users"]["hekate"],
        role=20,
    )

    result = mcp_call(
        client,
        "agentpm",
        "plane_add_project_member",
        {"project_id": str(project.id), "member_agent_id": "iris", "role": "member"},
    )

    assert result["member"]["agent_id"] == "iris"
    assert result["member"]["role"] == 15
    assert ProjectMember.objects.filter(project=project, member=native_mcp_data["users"]["iris"], role=15).exists()


def test_native_mcp_non_admin_cannot_add_project_member(native_mcp_data):
    client = authenticate(native_mcp_data["client"], native_mcp_data["tokens"]["iris"])

    result = mcp_call(
        client,
        "agentpm",
        "plane_add_project_member",
        {"project_id": str(native_mcp_data["project"].id), "member_agent_id": "taichi", "role": "member"},
    )

    assert result["error"]["type"] == "insufficient_project_role"


def test_native_mcp_admin_can_add_existing_workspace_user_to_project(native_mcp_data):
    client = authenticate(native_mcp_data["client"], native_mcp_data["tokens"]["hekate"])
    human = User.objects.create(email="pm@example.com", username="pm", display_name="PM")
    WorkspaceMember.objects.create(workspace=native_mcp_data["workspace"], member=human, role=15)
    project = Project.objects.create(
        name="Human Project",
        identifier="HUM",
        workspace=native_mcp_data["workspace"],
        project_lead=native_mcp_data["users"]["hekate"],
    )
    ProjectMember.objects.create(
        workspace=native_mcp_data["workspace"],
        project=project,
        member=native_mcp_data["users"]["hekate"],
        role=20,
    )

    result = mcp_call(
        client,
        "agentpm",
        "plane_add_workspace_user_to_project",
        {"project_id": str(project.id), "user_id": str(human.id), "role": "guest"},
    )

    assert result["member"]["plane_user_id"] == str(human.id)
    assert result["member"]["agent_id"] is None
    assert result["member"]["role"] == 5


def test_native_mcp_create_work_item_reports_target_not_project_member(native_mcp_data):
    client = authenticate(native_mcp_data["client"], native_mcp_data["tokens"]["hekate"])
    project = Project.objects.create(
        name="Missing Member Project",
        identifier="MISS",
        workspace=native_mcp_data["workspace"],
        project_lead=native_mcp_data["users"]["hekate"],
    )
    ProjectMember.objects.create(
        workspace=native_mcp_data["workspace"],
        project=project,
        member=native_mcp_data["users"]["hekate"],
        role=20,
    )

    raw = mcp_raw_call(
        client,
        "agentpm",
        "plane_create_work_item",
        {"project_id": str(project.id), "name": "Needs Iris", "target_agent_id": "iris"},
    )
    result = json.loads(raw["result"]["content"][0]["text"])

    assert "error" not in raw
    assert raw["result"]["isError"] is True
    assert result["error"]["type"] == "target_not_project_member"
    assert "plane_add_project_member" in result["error"]["suggested_next_tools"]


def test_native_mcp_rejects_plane_user_uuid_as_target_agent_id(native_mcp_data):
    client = authenticate(native_mcp_data["client"], native_mcp_data["tokens"]["hekate"])

    raw = mcp_raw_call(
        client,
        "agentpm",
        "plane_create_work_item",
        {
            "project_id": str(native_mcp_data["project"].id),
            "name": "Bad target",
            "target_agent_id": str(native_mcp_data["users"]["iris"].id),
        },
    )
    result = json.loads(raw["result"]["content"][0]["text"])

    assert "error" not in raw
    assert raw["result"]["isError"] is True
    assert result["error"]["type"] == "invalid_agent_id"
    assert "Do not pass Plane user UUIDs" in result["error"]["hint"]


def test_native_mcp_prompts_and_resources_expose_recommended_skill(native_mcp_data):
    client = authenticate(native_mcp_data["client"], native_mcp_data["tokens"]["iris"])

    prompts = client.post(
        "/api/v1/workspaces/agentpm/mcp/",
        {"jsonrpc": "2.0", "id": 1, "method": "prompts/list", "params": {}},
        format="json",
    ).json()
    prompt = client.post(
        "/api/v1/workspaces/agentpm/mcp/",
        {"jsonrpc": "2.0", "id": 2, "method": "prompts/get", "params": {"name": "agentpm_plane_workflow"}},
        format="json",
    ).json()
    resources = client.post(
        "/api/v1/workspaces/agentpm/mcp/",
        {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}},
        format="json",
    ).json()
    resource = client.post(
        "/api/v1/workspaces/agentpm/mcp/",
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "resources/read",
            "params": {"uri": "agentpm://skills/agentpm-plane-workflow/SKILL.md"},
        },
        format="json",
    ).json()

    assert prompts["result"]["prompts"][0]["name"] == "agentpm_plane_workflow"
    assert "plane_list_project_members" in prompt["result"]["messages"][0]["content"]["text"]
    assert resources["result"]["resources"][0]["uri"] == "agentpm://skills/agentpm-plane-workflow/SKILL.md"
    assert "target_agent_id" in resource["result"]["contents"][0]["text"]


def test_mesh_identity_and_eligible_agent_discovery_do_not_leak_secrets(native_mcp_data):
    workspace = native_mcp_data["workspace"]
    project = native_mcp_data["project"]
    iris = native_mcp_data["users"]["iris"]
    project_member = ProjectMember.objects.get(project=project, member=iris)
    profile = AgentProfile.objects.create(
        workspace=workspace,
        user=iris,
        agent_id="iris",
        runtime_provider="openclaw",
        status=AgentProfile.Status.ACTIVE,
        capability_claims=["code.write"],
        boundaries={"denied_capabilities": ["deploy.production"]},
        agent_card={"available": True},
    )
    AgentExecutionProfile.objects.create(
        workspace=workspace,
        agent=profile,
        provider="openclaw",
        model="gpt-test",
        secret_reference="env:TOP_SECRET",
        is_default=True,
    )
    role = MeshFunctionalRole.objects.create(
        workspace=workspace,
        project=project,
        key="developer",
        name="Developer",
        capabilities=["work.read", "code.write"],
    )
    MeshProjectMemberRole.objects.create(
        workspace=workspace,
        project=project,
        project_member=project_member,
        functional_role=role,
    )

    client = authenticate(native_mcp_data["client"], native_mcp_data["tokens"]["iris"])
    me = mcp_call(client, "agentpm", "mesh_get_me", {})
    eligible = mcp_call(
        client,
        "agentpm",
        "mesh_list_eligible_agents",
        {"project_id": str(project.id), "role": "developer", "required_capabilities": ["code.write"]},
    )
    mesh_api_identity = client.get("/api/v1/workspaces/agentpm/mesh/").json()
    mesh_api_candidates = client.get(
        f"/api/v1/workspaces/agentpm/mesh/projects/{project.id}/eligible-agents/",
        {"role": "developer", "capabilities": "code.write"},
    ).json()
    payload = json.dumps(
        {
            "me": me,
            "eligible": eligible,
            "mesh_api_identity": mesh_api_identity,
            "mesh_api_candidates": mesh_api_candidates,
        }
    )

    assert me["account_type"] == "agent"
    assert me["agent"]["agent_id"] == "iris"
    assert me["agent"]["default_execution"]["model"] == "gpt-test"
    assert eligible["agents"][0]["agent_id"] == "iris"
    assert mesh_api_identity["identity"]["agent_id"] == "iris"
    assert mesh_api_candidates["agents"][0]["agent_id"] == "iris"
    assert "TOP_SECRET" not in payload
    assert "secret_reference" not in payload


def test_mesh_stage_assignment_can_return_plane_work_item_to_unassigned(native_mcp_data):
    workspace = native_mcp_data["workspace"]
    project = native_mcp_data["project"]
    issue = native_mcp_data["issue"]
    iris = native_mcp_data["users"]["iris"]
    profile = AgentProfile.objects.create(
        workspace=workspace,
        user=iris,
        agent_id="iris",
        status=AgentProfile.Status.ACTIVE,
        agent_card={"available": True},
    )
    role = MeshFunctionalRole.objects.create(workspace=workspace, project=project, key="developer", name="Developer")
    MeshProjectMemberRole.objects.create(
        workspace=workspace,
        project=project,
        project_member=ProjectMember.objects.get(project=project, member=iris),
        functional_role=role,
    )
    definition = MeshLoopDefinition.objects.create(
        workspace=workspace,
        project=project,
        slug="bug-fix",
        name="Bug fix",
        version=1,
        status=MeshLoopDefinition.Status.PUBLISHED,
        source_yaml="schema_version: 1",
        graph={"nodes": [{"id": "repair", "type": "stage", "roles": ["developer"]}]},
        checksum="0" * 64,
    )
    run = MeshLoopRun.objects.create(
        workspace=workspace,
        project=project,
        work_item=issue,
        definition=definition,
        definition_version=1,
        status=MeshLoopRun.Status.WAITING_FOR_ASSIGNEE,
    )
    MeshStageRun.objects.create(
        workspace=workspace,
        project=project,
        loop_run=run,
        node_id="triage",
        functional_role=role,
        assigned_agent=profile,
        status=MeshStageRun.Status.SUCCEEDED,
    )
    stage = MeshStageRun.objects.create(
        workspace=workspace,
        project=project,
        loop_run=run,
        node_id="repair",
        functional_role=role,
    )
    client = authenticate(native_mcp_data["client"], native_mcp_data["tokens"]["hekate"])

    assigned = mcp_call(
        client,
        "agentpm",
        "mesh_assign_stage",
        {"project_id": str(project.id), "stage_run_id": str(stage.id), "target_agent_id": "iris"},
    )
    assert assigned["stage"]["assigned_agent_id"] == "iris"
    handoff = MeshHandoff.objects.get(loop_run=run, to_node_id="repair")
    assert handoff.target_agent == profile
    assert handoff.status == MeshHandoff.Status.ASSIGNED

    unassigned = mcp_call(
        client,
        "agentpm",
        "mesh_assign_stage",
        {"project_id": str(project.id), "stage_run_id": str(stage.id)},
    )
    assert unassigned["stage"]["assigned_agent_id"] is None
    assert unassigned["stage"]["status"] == "waiting_for_assignee"
    assert not IssueAssignee.objects.filter(issue=issue).exists()
    run.refresh_from_db()
    assert run.status == MeshLoopRun.Status.WAITING_FOR_ASSIGNEE
    handoff.refresh_from_db()
    assert handoff.status == MeshHandoff.Status.CANCELED


def test_mesh_loop_start_strict_evidence_completion_and_cancel(native_mcp_data):
    workspace = native_mcp_data["workspace"]
    project = native_mcp_data["project"]
    issue = native_mcp_data["issue"]
    iris = native_mcp_data["users"]["iris"]
    profile = AgentProfile.objects.create(
        workspace=workspace,
        user=iris,
        agent_id="iris",
        status=AgentProfile.Status.ACTIVE,
        agent_card={"available": True},
    )
    role = MeshFunctionalRole.objects.create(
        workspace=workspace,
        project=project,
        key="developer",
        name="Developer",
        capabilities=["code.write"],
    )
    MeshProjectMemberRole.objects.create(
        workspace=workspace,
        project=project,
        project_member=ProjectMember.objects.get(project=project, member=iris),
        functional_role=role,
    )
    definition = MeshLoopDefinition.objects.create(
        workspace=workspace,
        project=project,
        slug="mesh-code-change-v1",
        name="Code change",
        version=1,
        status=MeshLoopDefinition.Status.PUBLISHED,
        source_yaml="schema_version: 1",
        graph={
            "nodes": [
                {"id": "assigned", "type": "trigger"},
                {
                    "id": "develop",
                    "type": "stage",
                    "roles": ["developer"],
                    "evidence": ["summary", "tests"],
                },
                {"id": "done", "type": "complete"},
            ],
            "edges": [
                {"from": "assigned", "to": "develop"},
                {"from": "develop", "to": "done"},
            ],
        },
        checksum="1" * 64,
    )
    admin_client = authenticate(native_mcp_data["client"], native_mcp_data["tokens"]["hekate"])
    started = mcp_call(
        admin_client,
        "agentpm",
        "mesh_start_loop",
        {"project_id": str(project.id), "work_item_id": str(issue.id), "loop_slug": definition.slug},
    )
    assert started["created"] is True
    stage_id = started["run"]["stages"][0]["id"]
    assigned = mcp_call(
        admin_client,
        "agentpm",
        "mesh_assign_stage",
        {"project_id": str(project.id), "stage_run_id": stage_id, "target_agent_id": "iris"},
    )
    assert assigned["stage"]["assigned_agent_id"] == "iris"

    iris_client = authenticate(native_mcp_data["client"], native_mcp_data["tokens"]["iris"])
    invalid = mcp_call(
        iris_client,
        "agentpm",
        "mesh_complete_stage",
        {
            "stage_run_id": stage_id,
            "evidence": [{"key": "summary", "kind": "text", "title": "Implemented"}],
        },
    )
    assert invalid["error"]["type"] == "invalid_stage_completion"
    assert "missing required evidence keys: tests" in invalid["error"]["message"]

    completed = mcp_call(
        iris_client,
        "agentpm",
        "mesh_complete_stage",
        {
            "stage_run_id": stage_id,
            "evidence": [
                {"key": "summary", "kind": "text", "title": "Implemented"},
                {"key": "tests", "kind": "test_result", "title": "Tests passed"},
            ],
        },
    )
    assert completed["status"] == MeshLoopRun.Status.COMPLETED
    timeline_comment = IssueComment.objects.get(issue=issue, external_source="mesh-runtime")
    assert "Agent: iris" in timeline_comment.comment_html
    assert "Evidence: summary, tests" in timeline_comment.comment_html

    second_issue = Issue.objects.create(
        workspace=workspace,
        project=project,
        name="Cancelable Mesh task",
        state=native_mcp_data["states"]["todo"],
    )
    second = mcp_call(
        authenticate(native_mcp_data["client"], native_mcp_data["tokens"]["hekate"]),
        "agentpm",
        "mesh_start_loop",
        {"project_id": str(project.id), "work_item_id": str(second_issue.id), "loop_slug": definition.slug},
    )
    canceled = mcp_call(
        admin_client,
        "agentpm",
        "mesh_cancel_run",
        {"project_id": str(project.id), "run_id": second["run"]["id"], "reason": "test"},
    )
    assert canceled["run"]["status"] == MeshLoopRun.Status.CANCELED
    assert not IssueAssignee.objects.filter(issue=second_issue).exists()


def test_native_mcp_work_item_kind_facade(native_mcp_data):
    client = authenticate(native_mcp_data["client"], native_mcp_data["tokens"]["hekate"])
    project_id = str(native_mcp_data["project"].id)

    kinds = mcp_call(client, "agentpm", "plane_list_work_item_kinds", {"project_id": project_id})
    created = mcp_call(
        client,
        "agentpm",
        "plane_create_work_item",
        {"project_id": project_id, "name": "Kind facade bug", "work_item_kind": "bug"},
    )
    filtered = mcp_call(
        client,
        "agentpm",
        "plane_list_work_items",
        {"project_id": project_id, "work_item_kind": "bug"},
    )
    updated = mcp_call(
        client,
        "agentpm",
        "plane_update_work_item",
        {"work_item_id": created["work_item"]["id"], "work_item_kind": "requirement"},
    )
    invalid = mcp_call(
        client,
        "agentpm",
        "plane_update_work_item",
        {"work_item_id": created["work_item"]["id"], "work_item_kind": "incident"},
    )

    assert {item["value"] for item in kinds["work_item_kinds"]} == {"requirement", "bug", "task", "analysis"}
    assert created["work_item"]["work_item_kind"] == "bug"
    assert any(item["id"] == created["work_item"]["id"] for item in filtered["work_items"])
    assert updated["work_item"]["work_item_kind"] == "requirement"
    assert invalid["error"]["type"] == "invalid_work_item_kind"


def test_native_mcp_cycle_and_module_workflow(native_mcp_data):
    client = authenticate(native_mcp_data["client"], native_mcp_data["tokens"]["hekate"])
    project_id = str(native_mcp_data["project"].id)
    issue_id = str(native_mcp_data["issue"].id)

    cycle = mcp_call(client, "agentpm", "plane_create_cycle", {"project_id": project_id, "name": "Sprint 1"})
    module = mcp_call(client, "agentpm", "plane_create_module", {"project_id": project_id, "name": "Core"})
    cycle_add = mcp_call(
        client, "agentpm", "plane_add_work_item_to_cycle", {"work_item_id": issue_id, "cycle_id": cycle["cycle"]["id"]}
    )
    module_add = mcp_call(
        client,
        "agentpm",
        "plane_add_work_item_to_module",
        {"work_item_id": issue_id, "module_id": module["module"]["id"]},
    )
    cycles = mcp_call(client, "agentpm", "plane_list_cycles", {"project_id": project_id})
    modules = mcp_call(client, "agentpm", "plane_list_modules", {"project_id": project_id})

    assert Cycle.objects.filter(id=cycle["cycle"]["id"]).exists()
    assert Module.objects.filter(id=module["module"]["id"]).exists()
    assert CycleIssue.objects.filter(id=cycle_add["membership_id"], issue_id=issue_id).exists()
    assert ModuleIssue.objects.filter(id=module_add["membership_id"], issue_id=issue_id).exists()
    assert cycles["cycles"][0]["name"] == "Sprint 1"
    assert modules["modules"][0]["name"] == "Core"


def test_agent_registration_request_and_human_admin_approval(native_mcp_data):
    client = native_mcp_data["client"]
    client.credentials()
    requested = client.post(
        "/api/agentpm/workspaces/agentpm/agent-applications/request/",
        {"agent_id": "atlas", "display_name": "Atlas", "requested_role": "member", "reason": "Project worker"},
        format="json",
    )
    assert requested.status_code == 201
    application = AgentRegistrationApplication.objects.get(agent_id="atlas")
    assert application.status == "pending"

    human = User.objects.get(email="admin@example.com")
    client.force_authenticate(user=human)
    approved = client.patch(
        f"/api/workspaces/agentpm/agent-applications/{application.id}/",
        {"action": "approve", "role": "member"},
        format="json",
    )

    assert approved.status_code == 200
    body = approved.json()
    assert body["application"]["status"] == "approved"
    assert body["account"]["user"]["agent_id"] == "atlas"
    assert body["account"]["token"]["token"].startswith("plane_api_")
    assert WorkspaceMember.objects.filter(
        workspace=native_mcp_data["workspace"], member__username="agent-atlas", role=15
    ).exists()


def test_human_admin_creates_agent_identity_and_separate_execution_profile(native_mcp_data):
    client = native_mcp_data["client"]
    client.force_authenticate(user=User.objects.get(email="admin@example.com"))
    response = client.post(
        "/api/workspaces/agentpm/agents/",
        {
            "agent_id": "nova",
            "display_name": "Nova",
            "agent_type": "remote",
            "runtime_provider": "openclaw",
            "default_model": "gpt-test",
            "secret_reference": "env:NOVA_TOKEN",
            "capability_claims": ["code.write"],
            "boundaries": {"denied_capabilities": ["deploy.production"]},
            "create_token": False,
        },
        format="json",
    )

    assert response.status_code == 201
    profile = AgentProfile.objects.get(workspace=native_mcp_data["workspace"], agent_id="nova")
    execution = AgentExecutionProfile.objects.get(agent=profile, is_default=True)
    assert profile.agent_type == "remote"
    assert profile.capability_claims == ["code.write"]
    assert execution.model == "gpt-test"
    assert execution.secret_reference == "env:NOVA_TOKEN"
    assert "NOVA_TOKEN" not in json.dumps(response.json())
    assert "secret_reference" not in json.dumps(response.json())

    details = client.get(f"/api/workspaces/agentpm/agents/{response.json()['workspace_member_id']}/")
    assert details.status_code == 200
    assert details.json()["default_execution"]["has_secret_reference"] is True
    assert "secret_reference" not in details.json()["default_execution"]
    assert "NOVA_TOKEN" not in json.dumps(details.json())

    updated = client.patch(
        f"/api/workspaces/agentpm/agents/{response.json()['workspace_member_id']}/",
        {
            "endpoint_url": "http://100.118.86.67:18890/agents/nova/a2a",
            "status": "active",
            "trust_level": "verified",
            "default_execution": {
                "provider": "openclaw",
                "model": "gpt-test",
                "configuration_version": 1,
                "settings": {"workspace_mode": "per_loop"},
            },
        },
        format="json",
    )
    assert updated.status_code == 200
    execution.refresh_from_db()
    assert execution.secret_reference == "env:NOVA_TOKEN"
    assert execution.settings["workspace_mode"] == "per_loop"

    with patch("plane.app.views.workspace.member.sync_agent_card") as sync:
        sync.side_effect = ValueError("Agent Card sync failed: offline")
        synced = client.post(
            f"/api/workspaces/agentpm/agents/{response.json()['workspace_member_id']}/",
            {},
            format="json",
        )
    assert synced.status_code == 400
    assert "offline" in synced.json()["error"]
