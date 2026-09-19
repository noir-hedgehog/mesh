# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from __future__ import annotations

import json
from datetime import date, datetime
from html import escape
from typing import Any, Callable

from django.db import transaction
from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db.models import F, Q
from django.core.validators import URLValidator
from django.core.exceptions import ValidationError
from rest_framework import status
from rest_framework.response import Response

from plane.app.serializers import APITokenReadSerializer
from plane.bgtasks.webhook_task import model_activity
from plane.db.models import (
    DEFAULT_STATES,
    Issue,
    IssueActivity,
    IssueAssignee,
    IssueComment,
    IssueLabel,
    IssueLink,
    IssueRelation,
    Label,
    Cycle,
    CycleIssue,
    Module,
    ModuleIssue,
    Project,
    ProjectMember,
    State,
    User,
    Workspace,
    WorkspaceMember,
    AgentProfile,
    MeshFunctionalRole,
    MeshKnowledgeChunk,
    MeshLoopDefinition,
    MeshLoopRun,
    MeshProjectMemberRole,
    MeshProjectPolicy,
    MeshRunAttempt,
    MeshSkill,
    MeshSkillVersion,
    MeshStageRun,
)
from plane.mesh.discovery import assign_stage, leave_stage_unassigned, list_eligible_agents
from plane.mesh.runtime import cancel_loop, complete_stage, queue_stage_start_on_commit, start_loop
from plane.mesh.skills import submit_skill_version
from plane.utils.issue_relation_mapper import get_actual_relation

from .base import BaseAPIView


PROTOCOL_VERSION = "2024-11-05"
ROLE_GUEST = 5
ROLE_MEMBER = 15
ROLE_ADMIN = 20
SKILL_NAME = "mesh_plane_workflow"
SKILL_RESOURCE_URI = "mesh://skills/mesh-plane-workflow/SKILL.md"
LEGACY_SKILL_NAME = "agentpm_plane_workflow"
LEGACY_SKILL_RESOURCE_URI = "agentpm://skills/agentpm-plane-workflow/SKILL.md"
WORK_ITEM_KINDS = {
    "requirement": "#4F46E5",
    "bug": "#DC2626",
    "task": "#0F766E",
    "analysis": "#B45309",
}
SKILL_TEXT = """# Mesh Plane Workflow

Use Plane-native MCP as a strict workflow facade. Identity comes from the MCP server token (`X-Api-Key`), never from an `agent_id` argument.

Before writing:
1. Call `plane_get_me` to confirm the authenticated user and role.
2. Call `plane_list_projects` and choose the target `project_id`.
3. Call `plane_list_project_members(project_id)` to find project members and canonical `agent_id` values.
4. Call `plane_list_states(project_id)` before passing `state` or `status`.
5. Call `mesh_get_policy`, `mesh_get_loop`, and `mesh_list_eligible_agents` before starting or assigning a Loop.

Rules:
- `target_agent_id` and `member_agent_id` must be short canonical agent ids such as `iris`, not Plane user UUIDs or emails.
- If the target agent is not a project member, an Admin should call `plane_add_project_member(project_id, member_agent_id, role)`.
- Guest can read and comment. Member can write assigned work. Admin can assign, create projects, and manage project members.
- Use comments for progress notes and links/relations for durable context.
- A PM Agent or Project Admin starts with `mesh_start_loop`, then explicitly assigns each Stage.
- Complete an assigned Stage with strict Evidence items containing `key`, `kind`, and `title`; cover every required Evidence key.
- Pass `handoff_target_agent_id` only when it came from the next Stage's eligible candidates. Omit it to leave the Stage Unassigned.
- Use `mesh_get_run` to inspect provider/model, A2A state, Evidence, Handoff, and failures. Use `mesh_cancel_run` for explicit cancellation.
- Mesh never silently selects the next Agent.

On structured tool errors, read `hint` and call any listed `suggested_next_tools` before retrying.
"""


class McpToolError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: int = -32000,
        error_type: str = "tool_error",
        hint: str = "",
        retryable: bool = False,
        suggested_next_tools: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.error_type = error_type
        self.hint = hint
        self.retryable = retryable
        self.suggested_next_tools = suggested_next_tools or []

    @property
    def payload(self) -> dict[str, Any]:
        return {
            "error": {
                "type": self.error_type,
                "message": str(self),
                "hint": self.hint,
                "retryable": self.retryable,
                "suggested_next_tools": self.suggested_next_tools,
            }
        }


class PlaneNativeMcpEndpoint(BaseAPIView):
    """Plane-native Streamable HTTP MCP endpoint."""

    def post(self, request, slug):
        rpc_request = request.data
        if not isinstance(rpc_request, dict):
            return Response(_rpc_error(None, -32600, "invalid JSON-RPC request"), status=status.HTTP_400_BAD_REQUEST)

        request_id = rpc_request.get("id")
        method = rpc_request.get("method")
        params = rpc_request.get("params") or {}

        service = PlaneNativeMcpService(request=request, slug=slug)

        try:
            if method == "initialize":
                return Response(
                    _rpc_result(
                        request_id,
                        {
                            "protocolVersion": PROTOCOL_VERSION,
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "plane-native", "version": "0.2.0"},
                        },
                    ),
                    status=status.HTTP_200_OK,
                )

            if method == "notifications/initialized":
                return Response(status=status.HTTP_202_ACCEPTED)

            if method == "tools/list":
                return Response(
                    _rpc_result(request_id, {"tools": service.tool_descriptors()}), status=status.HTTP_200_OK
                )

            if method == "tools/call":
                name = params.get("name")
                arguments = params.get("arguments") or {}
                if not isinstance(arguments, dict):
                    raise McpToolError("tool arguments must be an object", code=-32602)
                try:
                    result = service.call_tool(name=name, args=arguments)
                    is_error = False
                except McpToolError as exc:
                    result = _json_text(exc.payload)
                    is_error = True
                except Exception as exc:
                    result = _json_text(
                        McpToolError(
                            str(exc),
                            error_type="internal_tool_error",
                            hint="Inspect the arguments, then retry after calling the relevant list/get tool for fresh context.",
                            suggested_next_tools=["plane_get_me", "plane_list_projects"],
                        ).payload
                    )
                    is_error = True
                return Response(
                    _rpc_result(
                        request_id,
                        {
                            "content": [{"type": "text", "text": result}],
                            "isError": is_error,
                        },
                    ),
                    status=status.HTTP_200_OK,
                )

            if method == "prompts/list":
                return Response(
                    _rpc_result(request_id, {"prompts": service.prompt_descriptors()}), status=status.HTTP_200_OK
                )

            if method == "prompts/get":
                return Response(
                    _rpc_result(request_id, service.get_prompt(params.get("name"))), status=status.HTTP_200_OK
                )

            if method == "resources/list":
                return Response(
                    _rpc_result(request_id, {"resources": service.resource_descriptors()}), status=status.HTTP_200_OK
                )

            if method == "resources/read":
                return Response(
                    _rpc_result(request_id, service.read_resource(params.get("uri"))), status=status.HTTP_200_OK
                )

            return Response(_rpc_error(request_id, -32601, f"method not found: {method}"), status=status.HTTP_200_OK)
        except McpToolError as exc:
            return Response(_rpc_error(request_id, exc.code, str(exc)), status=status.HTTP_200_OK)
        except Exception as exc:
            return Response(_rpc_error(request_id, -32000, str(exc)), status=status.HTTP_200_OK)

    def get(self, request, slug):
        service = PlaneNativeMcpService(request=request, slug=slug)
        return Response(
            {
                "name": "mesh-native",
                "protocolVersion": PROTOCOL_VERSION,
                "workspace": slug,
                "tools": [tool["name"] for tool in service.tool_descriptors()],
                "recommended_skill": {
                    "name": SKILL_NAME,
                    "resource_uri": SKILL_RESOURCE_URI,
                    "prompt_name": SKILL_NAME,
                },
            },
            status=status.HTTP_200_OK,
        )


class PlaneNativeMcpService:
    def __init__(self, *, request, slug: str) -> None:
        self.request = request
        self.user = request.user
        self.workspace = Workspace.objects.get(slug=slug)
        self.workspace_member = WorkspaceMember.objects.get(
            workspace=self.workspace,
            member=self.user,
            is_active=True,
        )
        self.tools: dict[str, Callable[[dict[str, Any]], Any]] = {
            "plane_get_me": self.get_me,
            "plane_list_projects": self.list_projects,
            "plane_list_states": self.list_states,
            "plane_list_work_items": self.list_work_items,
            "plane_search_work_items": self.search_work_items,
            "plane_get_work_item": self.get_work_item,
            "plane_summarize_work_item": self.summarize_work_item,
            "plane_get_project_summary": self.get_project_summary,
            "plane_list_project_members": self.list_project_members,
            "plane_list_labels": self.list_labels,
            "plane_list_work_item_kinds": self.list_work_item_kinds,
            "plane_add_comment": self.add_comment,
            "plane_update_status": self.update_status,
            "plane_update_work_item": self.update_work_item,
            "plane_assign_work_item": self.assign_work_item,
            "plane_add_project_member": self.add_project_member,
            "plane_add_workspace_user_to_project": self.add_workspace_user_to_project,
            "plane_create_project": self.create_project,
            "plane_create_work_item": self.create_work_item,
            "plane_list_work_item_comments": self.list_work_item_comments,
            "plane_list_work_item_activity": self.list_work_item_activity,
            "plane_list_work_item_links": self.list_work_item_links,
            "plane_add_work_item_link": self.add_work_item_link,
            "plane_update_work_item_link": self.update_work_item_link,
            "plane_delete_work_item_link": self.delete_work_item_link,
            "plane_list_work_item_relations": self.list_work_item_relations,
            "plane_add_work_item_relation": self.add_work_item_relation,
            "plane_delete_work_item_relation": self.delete_work_item_relation,
            "plane_list_agent_accounts": self.list_agent_accounts,
            "plane_list_cycles": self.list_cycles,
            "plane_create_cycle": self.create_cycle,
            "plane_add_work_item_to_cycle": self.add_work_item_to_cycle,
            "plane_remove_work_item_from_cycle": self.remove_work_item_from_cycle,
            "plane_list_modules": self.list_modules,
            "plane_create_module": self.create_module,
            "plane_add_work_item_to_module": self.add_work_item_to_module,
            "plane_remove_work_item_from_module": self.remove_work_item_from_module,
            "mesh_get_me": self.mesh_get_me,
            "mesh_list_project_roles": self.mesh_list_project_roles,
            "mesh_list_eligible_agents": self.mesh_list_eligible_agents,
            "mesh_get_policy": self.mesh_get_policy,
            "mesh_list_skills": self.mesh_list_skills,
            "mesh_get_skill": self.mesh_get_skill,
            "mesh_submit_skill": self.mesh_submit_skill,
            "mesh_search_knowledge": self.mesh_search_knowledge,
            "mesh_get_loop": self.mesh_get_loop,
            "mesh_start_loop": self.mesh_start_loop,
            "mesh_list_runs": self.mesh_list_runs,
            "mesh_get_run": self.mesh_get_run,
            "mesh_cancel_run": self.mesh_cancel_run,
            "mesh_assign_stage": self.mesh_assign_stage,
            "mesh_handoff_work_item": self.mesh_assign_stage,
            "mesh_complete_stage": self.mesh_complete_stage,
        }

    def call_tool(self, *, name: str | None, args: dict[str, Any]) -> str:
        if not name or name not in self.tools:
            raise McpToolError(f"unknown tool: {name}", code=-32602)
        if "agent_id" in args:
            raise McpToolError(
                "agent_id is not accepted by Plane-native MCP; identity comes from X-Api-Key",
                code=-32602,
                error_type="identity_switch_rejected",
                hint="Register or select a separate MCP server with the intended Plane API token instead of passing agent_id.",
                suggested_next_tools=["plane_get_me"],
            )
        result = self.tools[name](args)
        return _json_text(result)

    def prompt_descriptors(self) -> list[dict[str, Any]]:
        return [
            {
                "name": LEGACY_SKILL_NAME,
                "description": "Compatibility alias for the Mesh Plane workflow.",
            },
            {
                "name": SKILL_NAME,
                "description": "Recommended workflow for using Plane-native MCP safely as an agent.",
            },
        ]

    def get_prompt(self, name: str | None) -> dict[str, Any]:
        if name not in {SKILL_NAME, LEGACY_SKILL_NAME}:
            raise McpToolError(f"unknown prompt: {name}", code=-32602)
        return {
            "description": "Mesh Plane-native MCP workflow",
            "messages": [
                {
                    "role": "user",
                    "content": {"type": "text", "text": SKILL_TEXT},
                }
            ],
        }

    def resource_descriptors(self) -> list[dict[str, Any]]:
        return [
            {
                "uri": LEGACY_SKILL_RESOURCE_URI,
                "name": "agentpm-plane-workflow",
                "description": "Compatibility alias for the Mesh Plane workflow skill.",
                "mimeType": "text/markdown",
            },
            {
                "uri": SKILL_RESOURCE_URI,
                "name": "mesh-plane-workflow",
                "description": "Mesh Plane-native MCP workflow skill.",
                "mimeType": "text/markdown",
            },
        ]

    def read_resource(self, uri: str | None) -> dict[str, Any]:
        if uri not in {SKILL_RESOURCE_URI, LEGACY_SKILL_RESOURCE_URI}:
            raise McpToolError(f"unknown resource: {uri}", code=-32602)
        return {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": SKILL_TEXT}]}

    def get_me(self, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "user": _compact_user(self.user),
            "agent_id": _agent_id_for_user(self.user),
            "workspace": {"id": str(self.workspace.id), "slug": self.workspace.slug, "name": self.workspace.name},
            "workspace_role": self.workspace_member.role,
        }

    def list_projects(self, args: dict[str, Any]) -> dict[str, Any]:
        projects = (
            Project.objects.filter(workspace=self.workspace)
            .filter(Q(project_projectmember__member=self.user, project_projectmember__is_active=True) | Q(network=2))
            .distinct()
            .order_by("name")
        )
        return {"projects": [_compact_project(project, self._project_role(project.id)) for project in projects]}

    def list_states(self, args: dict[str, Any]) -> dict[str, Any]:
        project = self._project(_required_arg(args, "project_id"))
        self._require_project_role(project, ROLE_GUEST)
        states = State.objects.filter(project=project, is_triage=False).order_by("sequence", "name")
        return {"states": [_compact_state(state) for state in states]}

    def list_work_items(self, args: dict[str, Any]) -> dict[str, Any]:
        project = self._project(_required_arg(args, "project_id"))
        self._require_project_role(project, ROLE_GUEST)
        limit = max(1, min(int(args.get("limit") or 50), 100))
        queryset = (
            Issue.issue_objects.filter(project=project, workspace=self.workspace)
            .select_related("project", "state")
            .prefetch_related("assignees", "labels")
            .order_by("-created_at")
        )
        if args.get("state"):
            queryset = queryset.filter(state=self._state(project, str(args["state"])))
        if args.get("search"):
            queryset = queryset.filter(name__icontains=str(args["search"]))
        if args.get("work_item_kind"):
            kind = _work_item_kind_value(args["work_item_kind"])
            queryset = queryset.filter(labels__name__iexact=f"kind:{kind}").distinct()
        return {"work_items": [_compact_issue(issue) for issue in queryset[:limit]]}

    def search_work_items(self, args: dict[str, Any]) -> dict[str, Any]:
        query = _required_arg(args, "query")
        limit = max(1, min(int(args.get("limit") or 10), 50))
        queryset = (
            Issue.issue_objects.filter(workspace=self.workspace)
            .filter(
                Q(project__project_projectmember__member=self.user, project__project_projectmember__is_active=True)
                | Q(project__network=2)
            )
            .select_related("project", "state")
            .prefetch_related("assignees", "labels")
            .distinct()
            .order_by("-created_at")
        )
        if args.get("project_id"):
            queryset = queryset.filter(project=self._project(str(args["project_id"])))
        if args.get("work_item_kind"):
            kind = _work_item_kind_value(args["work_item_kind"])
            queryset = queryset.filter(labels__name__iexact=f"kind:{kind}").distinct()
        search = Q(name__icontains=query) | Q(project__identifier__icontains=query)
        for sequence_id in _integers_in_text(query):
            search |= Q(sequence_id=sequence_id)
        return {"work_items": [_compact_issue(issue) for issue in queryset.filter(search)[:limit]]}

    def get_work_item(self, args: dict[str, Any]) -> dict[str, Any]:
        issue = self._issue(_required_arg(args, "work_item_id"), project_id=args.get("project_id"))
        self._require_project_role(issue.project, ROLE_GUEST)
        return {"work_item": _compact_issue(issue, detailed=True)}

    def summarize_work_item(self, args: dict[str, Any]) -> dict[str, Any]:
        issue = self._issue(_required_arg(args, "work_item_id"), project_id=args.get("project_id"))
        self._require_project_role(issue.project, ROLE_GUEST)
        comments = IssueComment.objects.filter(issue=issue).select_related("actor").order_by("-created_at")[:5]
        return {
            "work_item": _compact_issue(issue, detailed=True),
            "recent_comments": [_compact_comment(comment) for comment in comments],
        }

    def get_project_summary(self, args: dict[str, Any]) -> dict[str, Any]:
        project = self._project(_required_arg(args, "project_id"))
        self._require_project_role(project, ROLE_GUEST)
        return {
            "project": _compact_project(project, self._project_role(project.id)),
            "counts": {
                "members": ProjectMember.objects.filter(project=project, is_active=True).count(),
                "states": State.objects.filter(project=project).count(),
                "labels": Label.objects.filter(project=project).count(),
                "work_items": Issue.issue_objects.filter(project=project, workspace=self.workspace).count(),
                "work_items_by_kind": {
                    kind: Issue.issue_objects.filter(project=project, labels__name__iexact=f"kind:{kind}").count()
                    for kind in WORK_ITEM_KINDS
                },
            },
        }

    def list_project_members(self, args: dict[str, Any]) -> dict[str, Any]:
        project = self._project(_required_arg(args, "project_id"))
        self._require_project_role(project, ROLE_GUEST)
        members = (
            ProjectMember.objects.filter(project=project, is_active=True)
            .select_related("member")
            .order_by("member__display_name", "member__email")
        )
        return {"members": [_compact_project_member(member) for member in members]}

    def list_labels(self, args: dict[str, Any]) -> dict[str, Any]:
        project = self._project(_required_arg(args, "project_id"))
        self._require_project_role(project, ROLE_GUEST)
        labels = Label.objects.filter(project=project).order_by("sort_order", "name")
        return {"labels": [_compact_label(label) for label in labels]}

    def list_work_item_kinds(self, args: dict[str, Any]) -> dict[str, Any]:
        project = self._project(_required_arg(args, "project_id"))
        self._require_project_role(project, ROLE_GUEST)
        labels = self._ensure_kind_labels(project, create=(self._project_role(project.id) or 0) >= ROLE_ADMIN)
        return {
            "work_item_kinds": [
                {
                    "value": kind,
                    "label": kind.title(),
                    "label_id": str(labels[kind].id) if kind in labels else None,
                }
                for kind in WORK_ITEM_KINDS
            ]
        }

    def add_comment(self, args: dict[str, Any]) -> dict[str, Any]:
        issue = self._issue(_required_arg(args, "work_item_id"), project_id=args.get("project_id"))
        self._require_project_role(issue.project, ROLE_GUEST)
        body = _required_arg(args, "body")
        comment = IssueComment.objects.create(
            project=issue.project,
            workspace=self.workspace,
            issue=issue,
            actor=self.user,
            comment_html=f"<p>{escape(body)}</p>",
            comment_json={},
            created_by_id=self.user.id,
        )
        return {"comment": _compact_comment(comment)}

    def update_status(self, args: dict[str, Any]) -> dict[str, Any]:
        issue = self._issue(_required_arg(args, "work_item_id"), project_id=args.get("project_id"))
        role = self._require_project_role(issue.project, ROLE_MEMBER)
        if role < ROLE_ADMIN and not issue.issue_assignee.filter(assignee=self.user).exists():
            raise McpToolError("member agents can only update work items assigned to their Plane user")
        previous = {"state": str(issue.state_id) if issue.state_id else None}
        issue.state = self._state(issue.project, _required_arg(args, "status"))
        issue.save(created_by_id=self.user.id)
        self._emit_issue_activity(issue, {"state": str(issue.state_id)}, previous)
        return {"work_item": _compact_issue(issue, detailed=True)}

    def update_work_item(self, args: dict[str, Any]) -> dict[str, Any]:
        issue = self._issue(_required_arg(args, "work_item_id"), project_id=args.get("project_id"))
        self._require_issue_write(issue)
        changed = []
        previous: dict[str, Any] = {}
        requested: dict[str, Any] = {}
        if "name" in args:
            previous["name"] = issue.name
            issue.name = _required_arg(args, "name")
            requested["name"] = issue.name
            changed.append("name")
        if "description" in args:
            previous["description_html"] = issue.description_html
            issue.description_html = f"<p>{escape(str(args.get('description') or ''))}</p>"
            requested["description_html"] = issue.description_html
            changed.append("description")
        if "description_html" in args:
            previous["description_html"] = issue.description_html
            issue.description_html = str(args.get("description_html") or "")
            requested["description_html"] = issue.description_html
            changed.append("description_html")
        if "priority" in args:
            priority = str(args["priority"])
            if priority not in {choice[0] for choice in Issue.PRIORITY_CHOICES}:
                raise McpToolError(f"invalid priority: {priority}", code=-32602)
            previous["priority"] = issue.priority
            issue.priority = priority
            requested["priority"] = issue.priority
            changed.append("priority")
        if "status" in args or "state" in args:
            previous["state"] = str(issue.state_id) if issue.state_id else None
            issue.state = self._state(issue.project, str(args.get("status") or args.get("state")))
            requested["state"] = str(issue.state_id)
            changed.append("state")
        if "start_date" in args:
            previous["start_date"] = issue.start_date.isoformat() if issue.start_date else None
            issue.start_date = _optional_date(args.get("start_date"), "start_date")
            requested["start_date"] = issue.start_date.isoformat() if issue.start_date else None
            changed.append("start_date")
        if "target_date" in args:
            previous["target_date"] = issue.target_date.isoformat() if issue.target_date else None
            issue.target_date = _optional_date(args.get("target_date"), "target_date")
            requested["target_date"] = issue.target_date.isoformat() if issue.target_date else None
            changed.append("target_date")
        if issue.start_date and issue.target_date and issue.start_date > issue.target_date:
            raise McpToolError("start_date cannot be after target_date", code=-32602)
        issue.save(created_by_id=self.user.id)
        if "labels" in args:
            self._replace_issue_labels(issue, args["labels"])
            changed.append("labels")
        if "assignees" in args:
            previous["assignees"] = [str(value) for value in issue.assignees.values_list("id", flat=True)]
            self._replace_issue_assignees(issue, args["assignees"])
            requested["assignees"] = [str(value) for value in issue.assignees.values_list("id", flat=True)]
            changed.append("assignees")
        if "work_item_kind" in args:
            self._replace_issue_kind(issue, args["work_item_kind"])
            changed.append("work_item_kind")
        if requested:
            self._emit_issue_activity(issue, requested, previous)
        return {
            "work_item": _compact_issue(self._issue(str(issue.id), project_id=str(issue.project_id)), detailed=True),
            "changed": changed,
        }

    def assign_work_item(self, args: dict[str, Any]) -> dict[str, Any]:
        issue = self._issue(_required_arg(args, "work_item_id"), project_id=args.get("project_id"))
        self._require_project_role(issue.project, ROLE_ADMIN)
        target = self._assignable_agent_user(issue.project, _required_arg(args, "target_agent_id"))
        target_member = self._project_member(issue.project, target)
        if target_member.role < ROLE_MEMBER:
            raise McpToolError(
                "guest agents cannot be assigned Plane work items",
                error_type="guest_not_assignable",
                hint="Use plane_add_project_member with role=member or choose a Member/Admin agent.",
                suggested_next_tools=["plane_list_project_members"],
            )
        previous = [str(value) for value in issue.assignees.values_list("id", flat=True)]
        _, created = IssueAssignee.objects.get_or_create(
            project=issue.project,
            workspace=self.workspace,
            issue=issue,
            assignee=target,
            defaults={"created_by_id": self.user.id},
        )
        if created:
            current = [str(value) for value in issue.assignees.values_list("id", flat=True)]
            self._emit_issue_activity(issue, {"assignees": current}, {"assignees": previous})
        return {
            "work_item": _compact_issue(self._issue(str(issue.id), project_id=str(issue.project_id)), detailed=True)
        }

    def add_project_member(self, args: dict[str, Any]) -> dict[str, Any]:
        project = self._project(_required_arg(args, "project_id"))
        self._require_project_role(project, ROLE_ADMIN)
        target = self._agent_user(_required_arg(args, "member_agent_id"))
        member = self._upsert_project_member(project, target, _role_value(args.get("role") or "member"))
        return {"member": _compact_project_member(member)}

    def add_workspace_user_to_project(self, args: dict[str, Any]) -> dict[str, Any]:
        project = self._project(_required_arg(args, "project_id"))
        self._require_project_role(project, ROLE_ADMIN)
        target = self._workspace_user(_required_arg(args, "user_id"))
        member = self._upsert_project_member(project, target, _role_value(args.get("role") or "member"))
        return {"member": _compact_project_member(member)}

    def create_project(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.workspace_member.role < ROLE_ADMIN:
            raise McpToolError(
                "workspace admin role is required to create projects",
                error_type="insufficient_workspace_role",
                hint="Use a Plane-native MCP server registered with a workspace Admin token.",
                suggested_next_tools=["plane_get_me"],
            )
        name = _required_arg(args, "name")
        identifier = _required_arg(args, "identifier")
        description = str(args.get("description") or "")
        with transaction.atomic():
            project = Project.objects.create(
                workspace=self.workspace,
                name=name,
                identifier=identifier,
                description=description,
                project_lead=self.user,
                created_by_id=self.user.id,
            )
            ProjectMember.objects.create(project=project, workspace=self.workspace, member=self.user, role=ROLE_ADMIN)
            for agent_id in args.get("member_agent_ids") or []:
                target = self._agent_user(str(agent_id))
                self._upsert_project_member(project, target, ROLE_MEMBER)
            State.objects.bulk_create(
                [
                    State(
                        name=state["name"],
                        color=state["color"],
                        project=project,
                        sequence=state["sequence"],
                        workspace=self.workspace,
                        group=state["group"],
                        default=state.get("default", False),
                        created_by=self.user,
                    )
                    for state in DEFAULT_STATES
                ]
            )
            self._ensure_kind_labels(project, create=True)
        return {"project": _compact_project(project, ROLE_ADMIN)}

    def create_work_item(self, args: dict[str, Any]) -> dict[str, Any]:
        project = self._project(_required_arg(args, "project_id"))
        self._require_project_role(project, ROLE_MEMBER)
        target = (
            self._assignable_agent_user(project, _required_arg(args, "target_agent_id"))
            if args.get("target_agent_id")
            else None
        )
        if target:
            target_member = self._project_member(project, target)
            if target_member.role < ROLE_MEMBER:
                raise McpToolError(
                    "guest agents cannot be assigned Plane work items",
                    error_type="guest_not_assignable",
                    hint="Use plane_add_project_member with role=member or choose a Member/Admin agent.",
                    suggested_next_tools=["plane_list_project_members"],
                )
        issue = Issue.objects.create(
            project=project,
            workspace=self.workspace,
            name=_required_arg(args, "name"),
            description_html=f"<p>{escape(str(args.get('description') or ''))}</p>",
            priority=str(args.get("priority") or "none"),
            state=self._state(project, str(args["state"])) if args.get("state") else None,
            external_source=args.get("external_source"),
            external_id=args.get("external_id"),
            created_by_id=self.user.id,
        )
        if target:
            IssueAssignee.objects.create(
                project=project,
                workspace=self.workspace,
                issue=issue,
                assignee=target,
                created_by_id=self.user.id,
            )
        if args.get("work_item_kind"):
            self._replace_issue_kind(issue, args["work_item_kind"])
        self._emit_issue_activity(issue, {"assignees": [str(target.id)] if target else []}, None)
        return {"work_item": _compact_issue(issue, detailed=True)}

    def list_work_item_comments(self, args: dict[str, Any]) -> dict[str, Any]:
        issue = self._issue(_required_arg(args, "work_item_id"), project_id=args.get("project_id"))
        self._require_project_role(issue.project, ROLE_GUEST)
        limit = max(1, min(int(args.get("limit") or 50), 100))
        comments = IssueComment.objects.filter(issue=issue).select_related("actor").order_by("-created_at")[:limit]
        return {"comments": [_compact_comment(comment) for comment in comments]}

    def list_work_item_activity(self, args: dict[str, Any]) -> dict[str, Any]:
        issue = self._issue(_required_arg(args, "work_item_id"), project_id=args.get("project_id"))
        self._require_project_role(issue.project, ROLE_GUEST)
        limit = max(1, min(int(args.get("limit") or 50), 100))
        activities = IssueActivity.objects.filter(issue=issue).select_related("actor").order_by("-created_at")[:limit]
        return {"activities": [_compact_activity(activity) for activity in activities]}

    def list_work_item_links(self, args: dict[str, Any]) -> dict[str, Any]:
        issue = self._issue(_required_arg(args, "work_item_id"), project_id=args.get("project_id"))
        self._require_project_role(issue.project, ROLE_GUEST)
        links = IssueLink.objects.filter(issue=issue, workspace=self.workspace).order_by("-created_at")
        return {"links": [_compact_link(link) for link in links]}

    def add_work_item_link(self, args: dict[str, Any]) -> dict[str, Any]:
        issue = self._issue(_required_arg(args, "work_item_id"), project_id=args.get("project_id"))
        self._require_issue_write(issue)
        url = _valid_url(_required_arg(args, "url"))
        if IssueLink.objects.filter(issue=issue, url=url).exists():
            raise McpToolError("URL already exists for this work item", code=-32602)
        link = IssueLink.objects.create(
            project=issue.project,
            workspace=self.workspace,
            issue=issue,
            title=str(args.get("title") or ""),
            url=url,
            metadata=args.get("metadata") if isinstance(args.get("metadata"), dict) else {},
            created_by_id=self.user.id,
        )
        return {"link": _compact_link(link)}

    def update_work_item_link(self, args: dict[str, Any]) -> dict[str, Any]:
        issue = self._issue(_required_arg(args, "work_item_id"), project_id=args.get("project_id"))
        self._require_issue_write(issue)
        link = self._link(issue, _required_arg(args, "link_id"))
        if "url" in args:
            url = _valid_url(_required_arg(args, "url"))
            if IssueLink.objects.filter(issue=issue, url=url).exclude(pk=link.id).exists():
                raise McpToolError("URL already exists for this work item", code=-32602)
            link.url = url
        if "title" in args:
            link.title = str(args.get("title") or "")
        if "metadata" in args:
            if not isinstance(args["metadata"], dict):
                raise McpToolError("metadata must be an object", code=-32602)
            link.metadata = args["metadata"]
        link.save(update_fields=["url", "title", "metadata", "updated_at"])
        return {"link": _compact_link(link)}

    def delete_work_item_link(self, args: dict[str, Any]) -> dict[str, Any]:
        issue = self._issue(_required_arg(args, "work_item_id"), project_id=args.get("project_id"))
        self._require_issue_write(issue)
        link = self._link(issue, _required_arg(args, "link_id"))
        link_id = str(link.id)
        link.delete()
        return {"deleted": True, "link_id": link_id}

    def list_work_item_relations(self, args: dict[str, Any]) -> dict[str, Any]:
        issue = self._issue(_required_arg(args, "work_item_id"), project_id=args.get("project_id"))
        self._require_project_role(issue.project, ROLE_GUEST)
        relations = IssueRelation.objects.filter(
            Q(issue=issue) | Q(related_issue=issue),
            workspace=self.workspace,
        ).select_related("issue__project", "issue__state", "related_issue__project", "related_issue__state")
        grouped = {
            "blocking": [],
            "blocked_by": [],
            "duplicate": [],
            "relates_to": [],
            "start_after": [],
            "start_before": [],
            "finish_after": [],
            "finish_before": [],
            "implemented_by": [],
            "implements": [],
        }
        rows = []
        for relation in relations:
            relation_name, other_issue = _display_relation(issue, relation)
            grouped.setdefault(relation_name, []).append(str(other_issue.id))
            rows.append(_compact_relation(issue, relation))
        return {"relations": grouped, "items": rows}

    def add_work_item_relation(self, args: dict[str, Any]) -> dict[str, Any]:
        issue = self._issue(_required_arg(args, "work_item_id"), project_id=args.get("project_id"))
        self._require_issue_write(issue)
        relation_type = _relation_type(_required_arg(args, "relation_type"))
        related_ids = args.get("related_work_item_ids") or args.get("related_work_item_id") or args.get("issues")
        if isinstance(related_ids, str):
            related_ids = [related_ids]
        if not isinstance(related_ids, list) or not related_ids:
            raise McpToolError("related_work_item_ids is required", code=-32602)
        actual_relation = get_actual_relation(relation_type)
        is_reverse = relation_type in {"blocking", "start_after", "finish_after", "implements"}
        created = []
        for related_id in related_ids:
            related_issue = self._issue(str(related_id), project_id=str(issue.project_id))
            self._require_project_role(related_issue.project, ROLE_GUEST)
            left = related_issue if is_reverse else issue
            right = issue if is_reverse else related_issue
            if left.id == right.id:
                raise McpToolError("cannot relate a work item to itself", code=-32602)
            relation, _ = IssueRelation.objects.get_or_create(
                issue=left,
                related_issue=right,
                workspace=self.workspace,
                project=issue.project,
                defaults={
                    "relation_type": actual_relation,
                    "created_by": self.user,
                    "updated_by": self.user,
                },
            )
            if relation.relation_type != actual_relation:
                relation.relation_type = actual_relation
                relation.updated_by = self.user
                relation.save(update_fields=["relation_type", "updated_by", "updated_at"])
            created.append(_compact_relation(issue, relation))
        return {"relations": created}

    def delete_work_item_relation(self, args: dict[str, Any]) -> dict[str, Any]:
        issue = self._issue(_required_arg(args, "work_item_id"), project_id=args.get("project_id"))
        self._require_issue_write(issue)
        if args.get("relation_id"):
            relation = IssueRelation.objects.get(workspace=self.workspace, pk=args["relation_id"])
            if relation.issue_id != issue.id and relation.related_issue_id != issue.id:
                raise McpToolError("relation does not belong to this work item")
        else:
            relation_type = _relation_type(_required_arg(args, "relation_type"))
            related_issue = self._issue(_required_arg(args, "related_work_item_id"), project_id=str(issue.project_id))
            actual_relation = get_actual_relation(relation_type)
            is_reverse = relation_type in {"blocking", "start_after", "finish_after", "implements"}
            left = related_issue if is_reverse else issue
            right = issue if is_reverse else related_issue
            relation = IssueRelation.objects.get(
                workspace=self.workspace,
                issue=left,
                related_issue=right,
                relation_type=actual_relation,
            )
        relation_id = str(relation.id)
        relation.delete()
        return {"deleted": True, "relation_id": relation_id}

    def list_agent_accounts(self, args: dict[str, Any]) -> dict[str, Any]:
        members = (
            WorkspaceMember.objects.filter(workspace=self.workspace, is_active=True, member__is_bot=True)
            .select_related("member")
            .order_by("member__display_name", "member__email")
        )
        return {
            "agents": [
                {
                    "agent_id": _agent_id_for_user(member.member),
                    "plane_user_id": str(member.member_id),
                    "display_name": member.member.display_name,
                    "email": member.member.email,
                    "username": member.member.username,
                    "workspace_role": member.role,
                    "is_bot": member.member.is_bot,
                    "tokens": APITokenReadSerializer(
                        member.member.bot_tokens.filter(workspace=self.workspace), many=True
                    ).data,
                }
                for member in members
            ]
        }

    def list_cycles(self, args: dict[str, Any]) -> dict[str, Any]:
        project = self._project(_required_arg(args, "project_id"))
        self._require_project_role(project, ROLE_GUEST)
        cycles = Cycle.objects.filter(project=project, archived_at__isnull=True).order_by("-created_at")
        return {"cycles": [_compact_cycle(cycle) for cycle in cycles]}

    def create_cycle(self, args: dict[str, Any]) -> dict[str, Any]:
        project = self._project(_required_arg(args, "project_id"))
        self._require_project_role(project, ROLE_ADMIN)
        cycle = Cycle.objects.create(
            project=project,
            workspace=self.workspace,
            name=_required_arg(args, "name"),
            description=str(args.get("description") or ""),
            start_date=_optional_datetime(args.get("start_date"), "start_date"),
            end_date=_optional_datetime(args.get("end_date"), "end_date"),
            owned_by=self.user,
            created_by_id=self.user.id,
        )
        return {"cycle": _compact_cycle(cycle)}

    def add_work_item_to_cycle(self, args: dict[str, Any]) -> dict[str, Any]:
        issue = self._issue(_required_arg(args, "work_item_id"), project_id=args.get("project_id"))
        self._require_issue_write(issue)
        cycle = Cycle.objects.get(project=issue.project, pk=_required_arg(args, "cycle_id"))
        CycleIssue.objects.filter(issue=issue).delete()
        membership = CycleIssue.objects.create(
            workspace=self.workspace,
            project=issue.project,
            issue=issue,
            cycle=cycle,
            created_by_id=self.user.id,
        )
        return {"cycle": _compact_cycle(cycle), "work_item": _compact_issue(issue), "membership_id": str(membership.id)}

    def remove_work_item_from_cycle(self, args: dict[str, Any]) -> dict[str, Any]:
        issue = self._issue(_required_arg(args, "work_item_id"), project_id=args.get("project_id"))
        self._require_issue_write(issue)
        membership = CycleIssue.objects.filter(issue=issue, cycle_id=_required_arg(args, "cycle_id")).first()
        if membership:
            membership.delete()
        return {"removed": membership is not None, "work_item_id": str(issue.id)}

    def list_modules(self, args: dict[str, Any]) -> dict[str, Any]:
        project = self._project(_required_arg(args, "project_id"))
        self._require_project_role(project, ROLE_GUEST)
        modules = Module.objects.filter(project=project, archived_at__isnull=True).order_by("-created_at")
        return {"modules": [_compact_module(module) for module in modules]}

    def create_module(self, args: dict[str, Any]) -> dict[str, Any]:
        project = self._project(_required_arg(args, "project_id"))
        self._require_project_role(project, ROLE_ADMIN)
        module = Module.objects.create(
            project=project,
            workspace=self.workspace,
            name=_required_arg(args, "name"),
            description=str(args.get("description") or ""),
            status=_module_status(args.get("status") or "planned"),
            start_date=_optional_date(args.get("start_date"), "start_date"),
            target_date=_optional_date(args.get("target_date"), "target_date"),
            lead=self.user,
            created_by_id=self.user.id,
        )
        return {"module": _compact_module(module)}

    def add_work_item_to_module(self, args: dict[str, Any]) -> dict[str, Any]:
        issue = self._issue(_required_arg(args, "work_item_id"), project_id=args.get("project_id"))
        self._require_issue_write(issue)
        module = Module.objects.get(project=issue.project, pk=_required_arg(args, "module_id"))
        ModuleIssue.objects.filter(issue=issue).delete()
        membership = ModuleIssue.objects.create(
            workspace=self.workspace,
            project=issue.project,
            issue=issue,
            module=module,
            created_by_id=self.user.id,
        )
        return {
            "module": _compact_module(module),
            "work_item": _compact_issue(issue),
            "membership_id": str(membership.id),
        }

    def remove_work_item_from_module(self, args: dict[str, Any]) -> dict[str, Any]:
        issue = self._issue(_required_arg(args, "work_item_id"), project_id=args.get("project_id"))
        self._require_issue_write(issue)
        membership = ModuleIssue.objects.filter(issue=issue, module_id=_required_arg(args, "module_id")).first()
        if membership:
            membership.delete()
        return {"removed": membership is not None, "work_item_id": str(issue.id)}

    def mesh_get_me(self, args: dict[str, Any]) -> dict[str, Any]:
        profile = AgentProfile.objects.filter(workspace=self.workspace, user=self.user, deleted_at__isnull=True).first()
        execution = None
        if profile:
            execution = profile.execution_profiles.filter(
                is_default=True, is_active=True, deleted_at__isnull=True
            ).first()
        return {
            "user": _compact_user(self.user),
            "account_type": "agent" if profile else "human",
            "agent": {
                "agent_id": profile.agent_id,
                "agent_type": profile.agent_type,
                "runtime_provider": profile.runtime_provider,
                "status": profile.status,
                "trust_level": profile.trust_level,
                "capability_claims": profile.capability_claims,
                "boundaries": profile.boundaries,
                "default_execution": (
                    {
                        "provider": execution.provider,
                        "model": execution.model,
                        "configuration_version": execution.configuration_version,
                    }
                    if execution
                    else None
                ),
            }
            if profile
            else None,
            "workspace": {"id": str(self.workspace.id), "slug": self.workspace.slug, "name": self.workspace.name},
            "workspace_role": self.workspace_member.role,
        }

    def mesh_list_project_roles(self, args: dict[str, Any]) -> dict[str, Any]:
        project = self._project(_required_arg(args, "project_id"))
        self._require_project_role(project, ROLE_GUEST)
        roles = MeshFunctionalRole.objects.filter(project=project, deleted_at__isnull=True).order_by(
            "sort_order", "name"
        )
        return {
            "roles": [
                {
                    "id": str(role.id),
                    "key": role.key,
                    "name": role.name,
                    "description": role.description,
                    "capabilities": role.capabilities,
                    "allowed_handoff_role_keys": role.allowed_handoff_role_keys,
                }
                for role in roles
            ]
        }

    def mesh_list_eligible_agents(self, args: dict[str, Any]) -> dict[str, Any]:
        project = self._project(_required_arg(args, "project_id"))
        self._require_project_role(project, ROLE_GUEST)
        role_keys = args.get("roles") or ([args["role"]] if args.get("role") else [])
        if not role_keys:
            raise McpToolError(
                "role or roles is required",
                error_type="missing_functional_role",
                hint="Call mesh_list_project_roles and pass one or more returned role keys.",
                suggested_next_tools=["mesh_list_project_roles"],
            )
        agents = list_eligible_agents(
            project_id=str(project.id),
            role_keys=[str(value) for value in role_keys],
            required_capabilities=[str(value) for value in args.get("required_capabilities") or []],
        )
        return {"agents": [agent.public_dict for agent in agents]}

    def mesh_get_policy(self, args: dict[str, Any]) -> dict[str, Any]:
        project = self._project(_required_arg(args, "project_id"))
        self._require_project_role(project, ROLE_GUEST)
        policy = (
            MeshProjectPolicy.objects.filter(
                project=project,
                status=MeshProjectPolicy.Status.PUBLISHED,
                deleted_at__isnull=True,
            )
            .order_by("-version")
            .first()
        )
        if not policy:
            return {"policy": None, "fallback": "Mesh default role and project permission policy applies."}
        return {
            "policy": {
                "id": str(policy.id),
                "version": policy.version,
                "source_yaml": policy.source_yaml,
                "policy": policy.policy,
                "published_at": policy.published_at,
                "change_note": policy.change_note,
            }
        }

    def mesh_list_skills(self, args: dict[str, Any]) -> dict[str, Any]:
        project = self._project(_required_arg(args, "project_id"))
        self._require_project_role(project, ROLE_GUEST)
        skills = MeshSkill.objects.filter(project=project, deleted_at__isnull=True).order_by("name")
        rows = []
        for skill in skills:
            versions = skill.versions.filter(
                status=MeshSkillVersion.Status.PUBLISHED, deleted_at__isnull=True
            ).order_by("-created_at")
            latest = versions.first()
            rows.append(
                {
                    "id": str(skill.id),
                    "slug": skill.slug,
                    "name": skill.name,
                    "description": skill.description,
                    "visibility": skill.visibility,
                    "published_version": latest.version if latest else None,
                }
            )
        return {"skills": rows}

    def mesh_get_skill(self, args: dict[str, Any]) -> dict[str, Any]:
        project = self._project(_required_arg(args, "project_id"))
        self._require_project_role(project, ROLE_GUEST)
        identifier = _required_arg(args, "skill_id")
        skill = (
            MeshSkill.objects.filter(project=project, deleted_at__isnull=True)
            .filter(Q(id=identifier) if _looks_like_uuid(identifier) else Q(slug=identifier))
            .first()
        )
        if not skill:
            raise McpToolError(
                "Skill not found", error_type="skill_not_found", suggested_next_tools=["mesh_list_skills"]
            )
        version = (
            skill.versions.filter(status=MeshSkillVersion.Status.PUBLISHED, deleted_at__isnull=True)
            .order_by("-created_at")
            .first()
        )
        if not version:
            raise McpToolError("Skill has no published version", error_type="skill_not_published")
        return {
            "skill": {"id": str(skill.id), "slug": skill.slug, "name": skill.name},
            "version": version.version,
            "manifest": version.manifest,
            "source_text": version.source_text,
            "checksum": version.checksum,
        }

    def mesh_submit_skill(self, args: dict[str, Any]) -> dict[str, Any]:
        project = self._project(_required_arg(args, "project_id"))
        self._require_project_role(project, ROLE_MEMBER)
        source_text = _required_arg(args, "source_text")
        try:
            submitted = submit_skill_version(project=project, user=self.user, source_text=source_text)
        except ValueError as exc:
            raise McpToolError(str(exc), error_type="invalid_skill_source") from exc
        return {
            "skill_id": str(submitted.skill.id),
            "version_id": str(submitted.version.id),
            "page_id": str(submitted.skill.page_id),
            "status": submitted.version.status,
        }

    def mesh_search_knowledge(self, args: dict[str, Any]) -> dict[str, Any]:
        project = self._project(_required_arg(args, "project_id"))
        self._require_project_role(project, ROLE_GUEST)
        query_text = _required_arg(args, "query")
        query = SearchQuery(query_text, search_type="websearch")
        chunks = (
            MeshKnowledgeChunk.objects.filter(project=project, deleted_at__isnull=True)
            .select_related("document__page", "document__page_version")
            .annotate(text_rank=SearchRank(F("content_search"), query))
            .filter(text_rank__gt=0)
            .order_by("-text_rank", "document_id", "sort_order")
        )
        limit = max(1, min(int(args.get("limit") or 10), 50))
        return {
            "results": [
                {
                    "heading": chunk.heading,
                    "content": chunk.content,
                    "score": float(chunk.text_rank or 0),
                    "citation": {
                        "page_id": str(chunk.document.page_id),
                        "page_version_id": (
                            str(chunk.document.page_version_id) if chunk.document.page_version_id else None
                        ),
                        "page_name": chunk.document.page.name,
                        "heading": chunk.heading,
                    },
                }
                for chunk in chunks[:limit]
            ]
        }

    def mesh_get_loop(self, args: dict[str, Any]) -> dict[str, Any]:
        project = self._project(_required_arg(args, "project_id"))
        self._require_project_role(project, ROLE_GUEST)
        loop = (
            MeshLoopDefinition.objects.filter(
                project=project,
                slug=_required_arg(args, "slug"),
                status=MeshLoopDefinition.Status.PUBLISHED,
                deleted_at__isnull=True,
            )
            .order_by("-version")
            .first()
        )
        if not loop:
            raise McpToolError("Published Loop not found", error_type="loop_not_found")
        return {
            "loop": {
                "id": str(loop.id),
                "slug": loop.slug,
                "name": loop.name,
                "version": loop.version,
                "source_yaml": loop.source_yaml,
                "graph": loop.graph,
                "checksum": loop.checksum,
            }
        }

    def mesh_start_loop(self, args: dict[str, Any]) -> dict[str, Any]:
        project = self._project(_required_arg(args, "project_id"))
        self._require_project_role(project, ROLE_MEMBER)
        if not self._can_manage_mesh_loop(project.id):
            raise McpToolError(
                "PM functional role or Project Admin is required to start a Loop",
                error_type="loop_start_not_allowed",
                suggested_next_tools=["mesh_list_project_roles", "plane_list_project_members"],
            )
        loop = (
            MeshLoopDefinition.objects.filter(
                project=project,
                slug=_required_arg(args, "loop_slug"),
                status=MeshLoopDefinition.Status.PUBLISHED,
                deleted_at__isnull=True,
            )
            .order_by("-version")
            .first()
        )
        if not loop:
            raise McpToolError("Published Loop not found", error_type="loop_not_found")
        work_item = self._issue(_required_arg(args, "work_item_id"), project_id=str(project.id))
        run, created = start_loop(definition=loop, work_item=work_item, actor=self.user)
        payload = _compact_mesh_run(run)
        payload["stages"] = [_compact_mesh_stage(stage) for stage in run.stages.select_related("assigned_agent")]
        return {"run": payload, "created": created}

    def mesh_list_runs(self, args: dict[str, Any]) -> dict[str, Any]:
        project = self._project(_required_arg(args, "project_id"))
        self._require_project_role(project, ROLE_GUEST)
        runs = MeshLoopRun.objects.filter(project=project, deleted_at__isnull=True).select_related(
            "work_item", "definition"
        )[:100]
        return {"runs": [_compact_mesh_run(run) for run in runs]}

    def mesh_get_run(self, args: dict[str, Any]) -> dict[str, Any]:
        project = self._project(_required_arg(args, "project_id"))
        self._require_project_role(project, ROLE_GUEST)
        run = (
            MeshLoopRun.objects.filter(id=_required_arg(args, "run_id"), project=project, deleted_at__isnull=True)
            .select_related("work_item", "definition")
            .first()
        )
        if not run:
            raise McpToolError(
                "Loop run not found", error_type="run_not_found", suggested_next_tools=["mesh_list_runs"]
            )
        payload = _compact_mesh_run(run)
        payload["stages"] = [_compact_mesh_stage(stage) for stage in run.stages.select_related("assigned_agent")]
        payload["handoffs"] = [
            {
                "id": str(handoff.id),
                "from_stage_id": str(handoff.from_stage_id),
                "to_node_id": handoff.to_node_id,
                "from_agent_id": handoff.from_agent.agent_id if handoff.from_agent else None,
                "target_agent_id": handoff.target_agent.agent_id if handoff.target_agent else None,
                "target_role": handoff.target_role.key,
                "status": handoff.status,
                "reason": handoff.reason,
            }
            for handoff in run.handoffs.select_related("from_agent", "target_agent", "target_role")
        ]
        return {"run": payload}

    def mesh_cancel_run(self, args: dict[str, Any]) -> dict[str, Any]:
        project = self._project(_required_arg(args, "project_id"))
        self._require_project_role(project, ROLE_MEMBER)
        if not self._can_manage_mesh_loop(project.id):
            raise McpToolError(
                "PM functional role or Project Admin is required to cancel a Loop",
                error_type="loop_cancel_not_allowed",
            )
        run = MeshLoopRun.objects.filter(
            id=_required_arg(args, "run_id"), project=project, deleted_at__isnull=True
        ).first()
        if not run:
            raise McpToolError("Loop run not found", error_type="run_not_found")
        run = cancel_loop(run=run, actor=self.user, reason=str(args.get("reason") or ""))
        return {"run": _compact_mesh_run(run)}

    def mesh_assign_stage(self, args: dict[str, Any]) -> dict[str, Any]:
        project = self._project(_required_arg(args, "project_id"))
        self._require_project_role(project, ROLE_MEMBER)
        stage = (
            MeshStageRun.objects.filter(
                id=_required_arg(args, "stage_run_id"), project=project, deleted_at__isnull=True
            )
            .select_related("loop_run__work_item", "loop_run__definition", "functional_role")
            .first()
        )
        if not stage:
            raise McpToolError(
                "Stage run not found", error_type="stage_not_found", suggested_next_tools=["mesh_list_runs"]
            )
        if not self._can_assign_mesh_stage(stage):
            raise McpToolError(
                "Only the previous Agent, a PM Agent, or Project Admin can assign this stage",
                error_type="handoff_not_allowed",
            )
        target_agent_id = str(args.get("target_agent_id") or "").strip()
        if not target_agent_id:
            leave_stage_unassigned(stage)
            return {"stage": _compact_mesh_stage(stage), "work_item_assigned": False}
        node = next(
            (item for item in stage.loop_run.definition.graph.get("nodes", []) if item.get("id") == stage.node_id),
            {},
        )
        roles = [stage.functional_role.key] if stage.functional_role else list(node.get("roles") or [])
        required = list(node.get("required_capabilities") or [])
        eligible = {
            item.agent_id: item
            for item in list_eligible_agents(
                project_id=str(project.id), role_keys=roles, required_capabilities=required
            )
        }
        if target_agent_id not in eligible or not eligible[target_agent_id].available:
            leave_stage_unassigned(stage)
            raise McpToolError(
                "Target Agent is not an available eligible project member",
                error_type="target_not_eligible",
                hint="Call mesh_list_eligible_agents with the Stage role and capabilities, then retry with a returned agent_id.",
                suggested_next_tools=["mesh_list_eligible_agents"],
            )
        candidate = eligible[target_agent_id]
        profile = AgentProfile.objects.get(workspace=self.workspace, agent_id=target_agent_id, deleted_at__isnull=True)
        target_role = (
            MeshFunctionalRole.objects.filter(
                project=project,
                key__in=set(candidate.functional_roles) & set(roles),
                deleted_at__isnull=True,
            )
            .order_by("sort_order")
            .first()
        )
        if not target_role:
            leave_stage_unassigned(stage)
            raise McpToolError(
                "Target Agent has no eligible role for this Stage",
                error_type="target_role_not_eligible",
                suggested_next_tools=["mesh_list_eligible_agents"],
            )
        try:
            stage = assign_stage(
                stage_run=stage,
                target_agent=profile,
                target_role=target_role,
                selected_by_user=self.user,
                reason=str(args.get("reason") or ""),
            )
        except ValueError as exc:
            leave_stage_unassigned(stage)
            raise McpToolError(
                str(exc),
                error_type="handoff_forbidden_by_policy",
                hint="Choose an Agent whose functional role is allowed by the published Project Policy.",
                suggested_next_tools=["mesh_get_policy", "mesh_list_eligible_agents"],
            ) from exc
        queue_stage_start_on_commit(str(stage.id))
        return {"stage": _compact_mesh_stage(stage), "work_item_assigned": True}

    def mesh_complete_stage(self, args: dict[str, Any]) -> dict[str, Any]:
        profile = AgentProfile.objects.filter(
            workspace=self.workspace,
            user=self.user,
            status=AgentProfile.Status.ACTIVE,
            deleted_at__isnull=True,
        ).first()
        if not profile:
            raise McpToolError("An active Agent identity is required", error_type="agent_identity_required")
        try:
            run = complete_stage(
                stage_run_id=_required_arg(args, "stage_run_id"),
                actor_agent=profile,
                outcome=str(args.get("outcome") or "succeeded"),
                evidence=list(args.get("evidence") or []),
                selected_next_node_id=args.get("selected_next_node_id"),
                handoff_target_agent_id=args.get("handoff_target_agent_id"),
            )
        except ValueError as exc:
            raise McpToolError(str(exc), error_type="invalid_stage_completion") from exc
        return {"run": _compact_mesh_run(run), "status": run.status}

    def _can_assign_mesh_stage(self, stage: MeshStageRun) -> bool:
        if self._project_role(stage.project_id) == ROLE_ADMIN:
            return True
        profile = AgentProfile.objects.filter(workspace=self.workspace, user=self.user, status="active").first()
        if not profile:
            return False
        if MeshProjectMemberRole.objects.filter(
            project_id=stage.project_id,
            project_member__member=self.user,
            functional_role__key="pm",
            deleted_at__isnull=True,
        ).exists():
            return True
        return MeshStageRun.objects.filter(
            loop_run_id=stage.loop_run_id,
            assigned_agent=profile,
            status=MeshStageRun.Status.SUCCEEDED,
            created_at__lt=stage.created_at,
            deleted_at__isnull=True,
        ).exists()

    def _can_manage_mesh_loop(self, project_id: Any) -> bool:
        if self._project_role(project_id) == ROLE_ADMIN:
            return True
        return MeshProjectMemberRole.objects.filter(
            project_id=project_id,
            project_member__member=self.user,
            functional_role__key="pm",
            deleted_at__isnull=True,
        ).exists()

    def tool_descriptors(self) -> list[dict[str, Any]]:
        tools = [
            _tool("plane_get_me", "Return the authenticated Plane user and workspace role.", {}),
            _tool("plane_list_projects", "List Plane projects in this workspace.", {}),
            _tool(
                "plane_list_states",
                "List project states. Use returned state ids/names/groups when passing state/status to write tools.",
                {"project_id": {"type": "string", "description": "Project id from plane_list_projects."}},
                ["project_id"],
            ),
            _tool(
                "plane_list_work_items",
                "List Plane work items.",
                {
                    "project_id": {"type": "string", "description": "Project id from plane_list_projects."},
                    "state": {"type": "string", "description": "State id, name, or group from plane_list_states."},
                    "work_item_kind": {"type": "string", "enum": list(WORK_ITEM_KINDS)},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                ["project_id"],
            ),
            _tool(
                "plane_search_work_items",
                "Search Plane work items across visible projects or within one project.",
                {
                    "query": {"type": "string"},
                    "project_id": {"type": "string", "description": "Optional project id from plane_list_projects."},
                    "work_item_kind": {"type": "string", "enum": list(WORK_ITEM_KINDS)},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                ["query"],
            ),
            _tool(
                "plane_get_work_item",
                "Get one Plane work item.",
                {
                    "work_item_id": {
                        "type": "string",
                        "description": "Work item id/key from plane_list_work_items or plane_search_work_items.",
                    },
                    "project_id": {"type": "string", "description": "Optional project id from plane_list_projects."},
                },
                ["work_item_id"],
            ),
            _tool(
                "plane_get_project_summary",
                "Get project summary counts.",
                {"project_id": {"type": "string", "description": "Project id from plane_list_projects."}},
                ["project_id"],
            ),
            _tool(
                "plane_list_project_members",
                "List project members and roles without tokens. Bot rows include canonical agent_id for target_agent_id/member_agent_id.",
                {"project_id": {"type": "string", "description": "Project id from plane_list_projects."}},
                ["project_id"],
            ),
            _tool(
                "plane_list_labels",
                "List project labels.",
                {"project_id": {"type": "string", "description": "Project id from plane_list_projects."}},
                ["project_id"],
            ),
            _tool(
                "plane_list_work_item_kinds",
                "List AgentPM work item kinds and their backing CE labels.",
                {"project_id": {"type": "string", "description": "Project id from plane_list_projects."}},
                ["project_id"],
            ),
            _tool(
                "plane_summarize_work_item",
                "Summarize one Plane work item and recent comments.",
                {
                    "work_item_id": {
                        "type": "string",
                        "description": "Work item id/key from plane_list_work_items or plane_search_work_items.",
                    },
                    "project_id": {"type": "string", "description": "Optional project id from plane_list_projects."},
                },
                ["work_item_id"],
            ),
            _tool(
                "plane_add_comment",
                "Add a comment to a Plane work item as the authenticated Plane user.",
                {
                    "work_item_id": {
                        "type": "string",
                        "description": "Work item id/key from plane_list_work_items or plane_search_work_items.",
                    },
                    "body": {"type": "string"},
                    "project_id": {"type": "string", "description": "Optional project id from plane_list_projects."},
                },
                ["work_item_id", "body"],
            ),
            _tool(
                "plane_update_status",
                "Update a Plane work item state.",
                {
                    "work_item_id": {
                        "type": "string",
                        "description": "Work item id/key from plane_list_work_items or plane_search_work_items.",
                    },
                    "status": {"type": "string", "description": "State id, name, or group from plane_list_states."},
                    "project_id": {"type": "string", "description": "Optional project id from plane_list_projects."},
                },
                ["work_item_id", "status"],
            ),
            _tool(
                "plane_update_work_item",
                "Update allowed Plane work item fields. Members may update only assigned work items.",
                {
                    "work_item_id": {
                        "type": "string",
                        "description": "Work item id/key from plane_list_work_items or plane_search_work_items.",
                    },
                    "project_id": {"type": "string", "description": "Optional project id from plane_list_projects."},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "description_html": {"type": "string"},
                    "priority": {"type": "string"},
                    "status": {"type": "string", "description": "State id, name, or group from plane_list_states."},
                    "state": {"type": "string", "description": "State id, name, or group from plane_list_states."},
                    "start_date": {"type": ["string", "null"]},
                    "target_date": {"type": ["string", "null"]},
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Label ids/names from plane_list_labels.",
                    },
                    "assignees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Existing project member ids/emails/usernames from plane_list_project_members.",
                    },
                    "work_item_kind": {"type": "string", "enum": list(WORK_ITEM_KINDS)},
                },
                ["work_item_id"],
            ),
            _tool(
                "plane_assign_work_item",
                "Assign a Plane work item to an agent project member. Admin project role required. target_agent_id must be a short agent id from plane_list_agent_accounts or plane_list_project_members, not a Plane user UUID or email.",
                {
                    "work_item_id": {
                        "type": "string",
                        "description": "Work item id/key from plane_list_work_items, plane_search_work_items, or plane_get_work_item.",
                    },
                    "target_agent_id": {
                        "type": "string",
                        "description": "Canonical short agent id, for example iris. Get it from plane_list_agent_accounts or plane_list_project_members. Do not pass Plane user UUID/email.",
                    },
                    "project_id": {"type": "string", "description": "Project id from plane_list_projects."},
                },
                ["work_item_id", "target_agent_id"],
            ),
            _tool(
                "plane_add_project_member",
                "Add an existing workspace bot agent to a project. Admin project role required. Does not invite new users.",
                {
                    "project_id": {"type": "string", "description": "Project id from plane_list_projects."},
                    "member_agent_id": {
                        "type": "string",
                        "description": "Canonical short agent id, for example iris. Get it from plane_list_agent_accounts. Do not pass Plane user UUID/email.",
                    },
                    "role": {
                        "type": "string",
                        "enum": ["admin", "member", "guest"],
                        "description": "Project role to grant. Defaults to member.",
                    },
                },
                ["project_id", "member_agent_id"],
            ),
            _tool(
                "plane_add_workspace_user_to_project",
                "Add an existing workspace user to a project. Admin project role required. Does not send workspace invitations.",
                {
                    "project_id": {"type": "string", "description": "Project id from plane_list_projects."},
                    "user_id": {
                        "type": "string",
                        "description": "Existing workspace user id/email/username from Plane UI or workspace member APIs.",
                    },
                    "role": {
                        "type": "string",
                        "enum": ["admin", "member", "guest"],
                        "description": "Project role to grant. Defaults to member.",
                    },
                },
                ["project_id", "user_id"],
            ),
            _tool(
                "plane_create_project",
                "Create a Plane project. Workspace admin role required.",
                {
                    "name": {"type": "string"},
                    "identifier": {"type": "string"},
                    "description": {"type": "string"},
                    "member_agent_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional canonical short agent ids to add as Member project members.",
                    },
                },
                ["name", "identifier"],
            ),
            _tool(
                "plane_create_work_item",
                "Create a Plane work item and optionally assign it to a bot Plane user. target_agent_id must be a short agent id from plane_list_agent_accounts or plane_list_project_members, not a Plane user UUID/email.",
                {
                    "project_id": {"type": "string", "description": "Project id from plane_list_projects."},
                    "name": {"type": "string"},
                    "target_agent_id": {
                        "type": "string",
                        "description": "Canonical short agent id, for example iris. Get it from plane_list_agent_accounts or plane_list_project_members. Do not pass Plane user UUID/email. If missing from the project, Admin should call plane_add_project_member first.",
                    },
                    "description": {"type": "string"},
                    "priority": {"type": "string"},
                    "state": {
                        "type": "string",
                        "description": "State id, name, or group from plane_list_states for this project.",
                    },
                    "external_source": {"type": "string"},
                    "external_id": {"type": "string"},
                    "work_item_kind": {
                        "type": "string",
                        "enum": list(WORK_ITEM_KINDS),
                        "description": "AgentPM kind facade backed by a kind:* CE label.",
                    },
                },
                ["project_id", "name"],
            ),
            _tool(
                "plane_list_work_item_comments",
                "List work item comments.",
                {
                    "work_item_id": {"type": "string"},
                    "project_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                ["work_item_id"],
            ),
            _tool(
                "plane_list_work_item_activity",
                "List work item activity timeline.",
                {
                    "work_item_id": {"type": "string"},
                    "project_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                ["work_item_id"],
            ),
            _tool(
                "plane_list_work_item_links",
                "List external links on a work item.",
                {"work_item_id": {"type": "string"}, "project_id": {"type": "string"}},
                ["work_item_id"],
            ),
            _tool(
                "plane_add_work_item_link",
                "Add an external link to an assigned/admin work item.",
                {
                    "work_item_id": {"type": "string"},
                    "project_id": {"type": "string"},
                    "url": {"type": "string"},
                    "title": {"type": "string"},
                    "metadata": {"type": "object"},
                },
                ["work_item_id", "url"],
            ),
            _tool(
                "plane_update_work_item_link",
                "Update an external work item link.",
                {
                    "work_item_id": {"type": "string"},
                    "project_id": {"type": "string"},
                    "link_id": {"type": "string"},
                    "url": {"type": "string"},
                    "title": {"type": "string"},
                    "metadata": {"type": "object"},
                },
                ["work_item_id", "link_id"],
            ),
            _tool(
                "plane_delete_work_item_link",
                "Delete an external work item link.",
                {"work_item_id": {"type": "string"}, "project_id": {"type": "string"}, "link_id": {"type": "string"}},
                ["work_item_id", "link_id"],
            ),
            _tool(
                "plane_list_work_item_relations",
                "List grouped relations for a work item.",
                {"work_item_id": {"type": "string"}, "project_id": {"type": "string"}},
                ["work_item_id"],
            ),
            _tool(
                "plane_add_work_item_relation",
                "Add a relation from one work item to related work items.",
                {
                    "work_item_id": {"type": "string"},
                    "project_id": {"type": "string"},
                    "relation_type": {"type": "string"},
                    "related_work_item_id": {"type": "string"},
                    "related_work_item_ids": {"type": "array", "items": {"type": "string"}},
                },
                ["work_item_id", "relation_type"],
            ),
            _tool(
                "plane_delete_work_item_relation",
                "Delete a relation by id or relation tuple.",
                {
                    "work_item_id": {"type": "string"},
                    "project_id": {"type": "string"},
                    "relation_id": {"type": "string"},
                    "relation_type": {"type": "string"},
                    "related_work_item_id": {"type": "string"},
                },
                ["work_item_id"],
            ),
            _tool("plane_list_agent_accounts", "List bot Plane users without token secrets.", {}),
            _tool(
                "plane_list_cycles", "List active project cycles.", {"project_id": {"type": "string"}}, ["project_id"]
            ),
            _tool(
                "plane_create_cycle",
                "Create a project cycle. Admin required.",
                {
                    "project_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "start_date": {"type": "string", "format": "date-time"},
                    "end_date": {"type": "string", "format": "date-time"},
                },
                ["project_id", "name"],
            ),
            _tool(
                "plane_add_work_item_to_cycle",
                "Add an assigned/admin work item to a cycle.",
                {"project_id": {"type": "string"}, "work_item_id": {"type": "string"}, "cycle_id": {"type": "string"}},
                ["work_item_id", "cycle_id"],
            ),
            _tool(
                "plane_remove_work_item_from_cycle",
                "Remove an assigned/admin work item from a cycle.",
                {"project_id": {"type": "string"}, "work_item_id": {"type": "string"}, "cycle_id": {"type": "string"}},
                ["work_item_id", "cycle_id"],
            ),
            _tool(
                "plane_list_modules", "List active project modules.", {"project_id": {"type": "string"}}, ["project_id"]
            ),
            _tool(
                "plane_create_module",
                "Create a project module. Admin required.",
                {
                    "project_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["backlog", "planned", "in-progress", "paused", "completed", "cancelled"],
                    },
                    "start_date": {"type": "string", "format": "date"},
                    "target_date": {"type": "string", "format": "date"},
                },
                ["project_id", "name"],
            ),
            _tool(
                "plane_add_work_item_to_module",
                "Add an assigned/admin work item to a module.",
                {"project_id": {"type": "string"}, "work_item_id": {"type": "string"}, "module_id": {"type": "string"}},
                ["work_item_id", "module_id"],
            ),
            _tool(
                "plane_remove_work_item_from_module",
                "Remove an assigned/admin work item from a module.",
                {"project_id": {"type": "string"}, "work_item_id": {"type": "string"}, "module_id": {"type": "string"}},
                ["work_item_id", "module_id"],
            ),
            _tool(
                "mesh_get_me",
                "Return the authenticated Human/Agent Mesh identity and default execution profile. Never returns secrets.",
                {},
            ),
            _tool(
                "mesh_list_project_roles",
                "List project functional roles and capabilities used for Agent handoff.",
                {"project_id": {"type": "string", "description": "Project id from plane_list_projects."}},
                ["project_id"],
            ),
            _tool(
                "mesh_get_policy",
                "Read the latest published Mesh Project Policy. Agents cannot publish Policy through MCP.",
                {"project_id": {"type": "string", "description": "Project id from plane_list_projects."}},
                ["project_id"],
            ),
            _tool(
                "mesh_list_eligible_agents",
                "List active project Agent members eligible for one or more functional roles and required capabilities.",
                {
                    "project_id": {"type": "string", "description": "Project id from plane_list_projects."},
                    "role": {"type": "string", "description": "One role key from mesh_list_project_roles."},
                    "roles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Role keys from mesh_list_project_roles.",
                    },
                    "required_capabilities": {"type": "array", "items": {"type": "string"}},
                },
                ["project_id"],
            ),
            _tool(
                "mesh_list_skills",
                "List project Skills and published versions.",
                {"project_id": {"type": "string"}},
                ["project_id"],
            ),
            _tool(
                "mesh_get_skill",
                "Read one published SKILL.md by id or slug.",
                {"project_id": {"type": "string"}, "skill_id": {"type": "string"}},
                ["project_id", "skill_id"],
            ),
            _tool(
                "mesh_submit_skill",
                "Submit a strict SKILL.md version for Human Project Admin review.",
                {"project_id": {"type": "string"}, "source_text": {"type": "string"}},
                ["project_id", "source_text"],
            ),
            _tool(
                "mesh_search_knowledge",
                "Search project Markdown knowledge and return versioned Page citations.",
                {
                    "project_id": {"type": "string"},
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                ["project_id", "query"],
            ),
            _tool(
                "mesh_get_loop",
                "Get the latest published LoopDefinition YAML and graph by slug.",
                {"project_id": {"type": "string"}, "slug": {"type": "string"}},
                ["project_id", "slug"],
            ),
            _tool(
                "mesh_start_loop",
                "Start the latest published Loop for a Work Item. PM functional role or Project Admin required.",
                {
                    "project_id": {"type": "string", "description": "Project id from plane_list_projects."},
                    "work_item_id": {"type": "string", "description": "Work item id/key from plane_list_work_items."},
                    "loop_slug": {"type": "string", "description": "Published Loop slug from mesh_get_loop or the Loops page."},
                },
                ["project_id", "work_item_id", "loop_slug"],
            ),
            _tool("mesh_list_runs", "List project Loop runs.", {"project_id": {"type": "string"}}, ["project_id"]),
            _tool(
                "mesh_get_run",
                "Get one Loop run with stages, actual Agent, provider, model, usage, cost, and evidence.",
                {"project_id": {"type": "string"}, "run_id": {"type": "string"}},
                ["project_id", "run_id"],
            ),
            _tool(
                "mesh_cancel_run",
                "Cancel an active Loop and clear the Work Item assignee. PM functional role or Project Admin required.",
                {
                    "project_id": {"type": "string"},
                    "run_id": {"type": "string", "description": "Run id from mesh_list_runs."},
                    "reason": {"type": "string"},
                },
                ["project_id", "run_id"],
            ),
            _tool(
                "mesh_assign_stage",
                "Assign or leave a Loop stage unassigned. Caller must be the previous Agent, a PM Agent, or Project Admin. target_agent_id must come from mesh_list_eligible_agents.",
                {
                    "project_id": {"type": "string"},
                    "stage_run_id": {"type": "string"},
                    "target_agent_id": {
                        "type": "string",
                        "description": "Optional canonical Agent id. Omit to leave the Work Item unassigned.",
                    },
                },
                ["project_id", "stage_run_id"],
            ),
            _tool(
                "mesh_handoff_work_item",
                "Handoff the current Work Item stage to an eligible Agent. Alias of mesh_assign_stage for workflow-oriented clients.",
                {
                    "project_id": {"type": "string"},
                    "stage_run_id": {"type": "string"},
                    "target_agent_id": {
                        "type": "string",
                        "description": "Optional canonical Agent id from mesh_list_eligible_agents.",
                    },
                },
                ["project_id", "stage_run_id"],
            ),
            _tool(
                "mesh_complete_stage",
                "Complete the Stage assigned to the authenticated Agent and submit structured Evidence.",
                {
                    "stage_run_id": {"type": "string", "description": "Stage id from mesh_get_run."},
                    "outcome": {"type": "string", "enum": ["succeeded", "failed"]},
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["key", "kind", "title"],
                            "properties": {
                                "key": {"type": "string"},
                                "kind": {"type": "string"},
                                "title": {"type": "string"},
                                "uri": {"type": "string"},
                                "summary": {"type": "string"},
                                "metadata": {"type": "object"},
                            },
                            "additionalProperties": False,
                        },
                    },
                    "handoff_target_agent_id": {
                        "type": "string",
                        "description": "Optional canonical Agent id for the next Stage. Omit to leave it Unassigned.",
                    },
                    "selected_next_node_id": {
                        "type": "string",
                        "description": "Required only when a Gate has multiple outgoing transitions.",
                    },
                },
                ["stage_run_id", "evidence"],
            ),
        ]
        if self.workspace_member.role < ROLE_ADMIN:
            admin_only = {
                "plane_add_project_member",
                "plane_add_workspace_user_to_project",
                "plane_create_cycle",
                "plane_create_module",
            }
            tools = [tool for tool in tools if tool["name"] not in admin_only]
        return tools

    def _project(self, project_id: str) -> Project:
        return Project.objects.get(workspace=self.workspace, pk=project_id)

    def _project_role(self, project_id: Any) -> int | None:
        member = ProjectMember.objects.filter(project_id=project_id, member=self.user, is_active=True).first()
        return member.role if member else None

    def _project_member(self, project: Project, user: User) -> ProjectMember:
        try:
            return ProjectMember.objects.get(project=project, member=user, is_active=True)
        except ProjectMember.DoesNotExist:
            raise McpToolError(
                "target user is not an active project member",
                error_type="target_not_project_member",
                hint="Admin should call plane_add_project_member for bot agents or plane_add_workspace_user_to_project for existing workspace users before assigning work.",
                suggested_next_tools=["plane_list_project_members", "plane_add_project_member"],
            )

    def _require_project_role(self, project: Project, min_role: int) -> int:
        role = self._project_role(project.id)
        if role is None or role < min_role:
            raise McpToolError(
                "insufficient project role",
                error_type="insufficient_project_role",
                hint="Use a Plane-native MCP server whose token user is a project member with the required role.",
                suggested_next_tools=["plane_get_me", "plane_list_projects"],
            )
        return role

    def _require_issue_write(self, issue: Issue) -> int:
        role = self._require_project_role(issue.project, ROLE_MEMBER)
        if role < ROLE_ADMIN and not issue.issue_assignee.filter(assignee=self.user).exists():
            raise McpToolError(
                "member agents can only write work items assigned to their Plane user",
                error_type="not_assigned",
                hint="Ask an Admin to assign this work item to the authenticated agent, or use an Admin MCP server.",
                suggested_next_tools=["plane_get_me", "plane_get_work_item"],
            )
        return role

    def _issue(self, work_item_id: str, *, project_id: str | None = None) -> Issue:
        queryset = (
            Issue.issue_objects.filter(workspace=self.workspace)
            .select_related("project", "state")
            .prefetch_related("assignees", "labels")
        )
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        if "-" in work_item_id and not _looks_like_uuid(work_item_id):
            project_identifier, sequence_id = work_item_id.rsplit("-", 1)
            return queryset.get(project__identifier__iexact=project_identifier, sequence_id=sequence_id)
        return queryset.get(pk=work_item_id)

    def _state(self, project: Project, value: str) -> State:
        state = State.objects.filter(project=project, pk=value).first() if _looks_like_uuid(value) else None
        if state is None:
            state = (
                State.objects.filter(project=project, name__iexact=value, is_triage=False).order_by("sequence").first()
            )
        if state is None:
            state = (
                State.objects.filter(project=project, group__iexact=value, is_triage=False).order_by("sequence").first()
            )
        if state is None:
            raise McpToolError(
                f"unknown state/status: {value}",
                code=-32602,
                error_type="unknown_state",
                hint="Call plane_list_states(project_id) and pass a returned state id, name, or group.",
                suggested_next_tools=["plane_list_states"],
            )
        return state

    def _agent_user(self, value: str) -> User:
        if _looks_like_uuid(value) or "@" in value:
            raise McpToolError(
                f"invalid agent id: {value}",
                code=-32602,
                error_type="invalid_agent_id",
                hint="Do not pass Plane user UUIDs or emails. Use the canonical short agent_id such as iris from plane_list_agent_accounts or plane_list_project_members.",
                suggested_next_tools=["plane_list_agent_accounts", "plane_list_project_members"],
            )
        agent_id = _normalize_agent_id(value)
        user = self._workspace_user(agent_id, bots_only=True)
        if not user.is_bot:
            raise McpToolError(
                f"unknown agent Plane user: {value}",
                error_type="unknown_agent",
                hint="Call plane_list_agent_accounts and use one of the returned agent_id values.",
                suggested_next_tools=["plane_list_agent_accounts"],
            )
        return user

    def _workspace_user(self, value: str, *, bots_only: bool = False) -> User:
        query = Q(id=value) if _looks_like_uuid(value) else Q()
        query |= Q(email__iexact=value) | Q(username__iexact=value)
        if "@" not in value:
            query |= Q(email__iexact=f"agent-{value}@agentpm.local") | Q(username__iexact=f"agent-{value}")
        queryset = User.objects.filter(query)
        if bots_only:
            queryset = queryset.filter(is_bot=True)
        user = queryset.first()
        if user is None:
            if bots_only:
                raise McpToolError(
                    f"unknown agent id: {value}",
                    error_type="unknown_agent",
                    hint="Call plane_list_agent_accounts and use one of the returned agent_id values. Registration/approval for new agents remains a human-admin flow.",
                    suggested_next_tools=["plane_list_agent_accounts"],
                )
            raise McpToolError(
                f"unknown Plane user: {value}",
                error_type="unknown_plane_user",
                hint="Use an existing workspace user id/email/username. MCP does not invite new workspace users.",
                suggested_next_tools=["plane_get_me"],
            )
        if not WorkspaceMember.objects.filter(workspace=self.workspace, member=user, is_active=True).exists():
            raise McpToolError(
                f"Plane user is not a workspace member: {value}",
                error_type="not_workspace_member",
                hint="Invite or approve the user through Plane UI or the human-admin CLI before adding them to a project.",
            )
        return user

    def _assignable_user(self, project: Project, value: str) -> User:
        user = self._workspace_user(value)
        member = self._project_member(project, user)
        if member.role < ROLE_MEMBER:
            raise McpToolError(
                "guest users cannot be assigned Plane work items",
                error_type="guest_not_assignable",
                hint="Grant Member/Admin project role or choose another assignee.",
                suggested_next_tools=["plane_list_project_members"],
            )
        return user

    def _assignable_agent_user(self, project: Project, value: str) -> User:
        user = self._agent_user(value)
        member = self._project_member(project, user)
        if member.role < ROLE_MEMBER:
            raise McpToolError(
                "guest agents cannot be assigned Plane work items",
                error_type="guest_not_assignable",
                hint="Use plane_add_project_member with role=member or choose a Member/Admin agent.",
                suggested_next_tools=["plane_list_project_members", "plane_add_project_member"],
            )
        return user

    def _upsert_project_member(self, project: Project, user: User, role: int) -> ProjectMember:
        member, _ = ProjectMember.objects.get_or_create(
            project=project,
            workspace=self.workspace,
            member=user,
            defaults={"role": role, "is_active": True},
        )
        member.role = role
        member.is_active = True
        member.save(update_fields=["role", "is_active", "updated_at"])
        return member

    def _label(self, project: Project, value: str) -> Label:
        label = Label.objects.filter(project=project, pk=value).first() if _looks_like_uuid(value) else None
        if label is None:
            label = Label.objects.filter(project=project, name__iexact=value).first()
        if label is None:
            raise McpToolError(f"unknown label: {value}")
        return label

    def _replace_issue_labels(self, issue: Issue, labels: Any) -> None:
        if not isinstance(labels, list):
            raise McpToolError("labels must be an array", code=-32602)
        resolved_labels = [self._label(issue.project, str(label)) for label in labels]
        IssueLabel.objects.filter(issue=issue).delete()
        IssueLabel.objects.bulk_create(
            [
                IssueLabel(
                    issue=issue,
                    label=label,
                    project=issue.project,
                    workspace=self.workspace,
                    created_by_id=self.user.id,
                )
                for label in resolved_labels
            ],
            ignore_conflicts=True,
        )

    def _replace_issue_assignees(self, issue: Issue, assignees: Any) -> None:
        if not isinstance(assignees, list):
            raise McpToolError("assignees must be an array", code=-32602)
        resolved_assignees = [self._assignable_user(issue.project, str(assignee)) for assignee in assignees]
        IssueAssignee.objects.filter(issue=issue).delete()
        IssueAssignee.objects.bulk_create(
            [
                IssueAssignee(
                    issue=issue,
                    assignee=assignee,
                    project=issue.project,
                    workspace=self.workspace,
                    created_by_id=self.user.id,
                )
                for assignee in resolved_assignees
            ],
            ignore_conflicts=True,
        )

    def _emit_issue_activity(
        self,
        issue: Issue,
        requested_data: dict[str, Any],
        current_instance: dict[str, Any] | None,
    ) -> None:
        model_activity.delay(
            model_name="issue",
            model_id=str(issue.id),
            requested_data=requested_data,
            current_instance=json.dumps(current_instance) if current_instance is not None else None,
            actor_id=str(self.user.id),
            slug=self.workspace.slug,
            origin=self.request.build_absolute_uri("/").rstrip("/"),
        )

    def _ensure_kind_labels(self, project: Project, *, create: bool) -> dict[str, Label]:
        labels = {
            label.name.split(":", 1)[1].lower(): label
            for label in Label.objects.filter(project=project, name__istartswith="kind:")
            if ":" in label.name
        }
        if create:
            for kind, color in WORK_ITEM_KINDS.items():
                if kind not in labels:
                    labels[kind] = Label.objects.create(
                        project=project,
                        workspace=self.workspace,
                        name=f"kind:{kind}",
                        color=color,
                        description=f"AgentPM work item kind: {kind}",
                        created_by_id=self.user.id,
                    )
        return labels

    def _replace_issue_kind(self, issue: Issue, value: Any) -> None:
        kind = _work_item_kind_value(value)
        label = self._ensure_kind_labels(issue.project, create=True)[kind]
        existing = list(IssueLabel.objects.filter(issue=issue, label__name__istartswith="kind:"))
        if existing:
            primary, *duplicates = existing
            primary.label = label
            primary.save(created_by_id=self.user.id, update_fields=["label", "updated_at", "updated_by"])
            for duplicate in duplicates:
                duplicate.delete()
        else:
            IssueLabel.objects.create(
                issue=issue,
                label=label,
                project=issue.project,
                workspace=self.workspace,
                created_by_id=self.user.id,
            )

    def _link(self, issue: Issue, link_id: str) -> IssueLink:
        try:
            return IssueLink.objects.get(workspace=self.workspace, issue=issue, pk=link_id)
        except IssueLink.DoesNotExist:
            raise McpToolError("work item link not found")


def _rpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _json_text(payload: Any) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _required_arg(args: dict[str, Any], name: str) -> str:
    value = args.get(name)
    if value in (None, ""):
        raise McpToolError(
            f"{name} is required",
            code=-32602,
            error_type="missing_required_argument",
            hint=f"Pass {name}; inspect the tool schema and call the relevant list/get tool to obtain canonical ids.",
        )
    return str(value)


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
    }


def _compact_project(project: Project, role: int | None = None) -> dict[str, Any]:
    return {
        "id": str(project.id),
        "name": project.name,
        "identifier": project.identifier,
        "description": project.description,
        "network": project.network,
        "member_role": role,
    }


def _compact_state(state: State) -> dict[str, Any]:
    return {
        "id": str(state.id),
        "name": state.name,
        "group": state.group,
        "color": state.color,
        "default": state.default,
        "sequence": state.sequence,
    }


def _compact_issue(issue: Issue, *, detailed: bool = False) -> dict[str, Any]:
    assignees = [
        {"id": str(user.id), "display_name": user.display_name, "email": user.email} for user in issue.assignees.all()
    ]
    labels = [
        _compact_label(label)
        for label in Label.objects.filter(label_issue__issue=issue, label_issue__deleted_at__isnull=True).distinct()
    ]
    payload = {
        "id": str(issue.id),
        "key": f"{issue.project.identifier}-{issue.sequence_id}",
        "name": issue.name,
        "project_id": str(issue.project_id),
        "project_identifier": issue.project.identifier,
        "state": _compact_state(issue.state) if issue.state else None,
        "priority": issue.priority,
        "start_date": issue.start_date,
        "target_date": issue.target_date,
        "assignees": assignees,
        "labels": labels,
        "work_item_kind": next(
            (label["name"].split(":", 1)[1].lower() for label in labels if label["name"].lower().startswith("kind:")),
            None,
        ),
        "created_at": issue.created_at,
        "updated_at": issue.updated_at,
    }
    if detailed:
        payload.update(
            {
                "description_html": issue.description_html,
                "description_stripped": issue.description_stripped,
                "external_source": issue.external_source,
                "external_id": issue.external_id,
            }
        )
    return payload


def _compact_user(user: User) -> dict[str, Any]:
    return {
        "id": str(user.id),
        "display_name": user.display_name,
        "email": user.email,
        "username": user.username,
        "is_bot": user.is_bot,
        "agent_id": _agent_id_for_user(user),
    }


def _compact_project_member(member: ProjectMember) -> dict[str, Any]:
    return {
        "id": str(member.id),
        "plane_user_id": str(member.member_id),
        "agent_id": _agent_id_for_user(member.member),
        "role": member.role,
        "is_active": member.is_active,
        "user": _compact_user(member.member),
    }


def _compact_label(label: Label) -> dict[str, Any]:
    return {
        "id": str(label.id),
        "name": label.name,
        "color": label.color,
        "description": label.description,
        "parent_id": str(label.parent_id) if label.parent_id else None,
        "sort_order": label.sort_order,
    }


def _compact_cycle(cycle: Cycle) -> dict[str, Any]:
    return {
        "id": str(cycle.id),
        "project_id": str(cycle.project_id),
        "name": cycle.name,
        "description": cycle.description,
        "start_date": cycle.start_date,
        "end_date": cycle.end_date,
        "owned_by": str(cycle.owned_by_id),
        "created_at": cycle.created_at,
        "updated_at": cycle.updated_at,
    }


def _compact_module(module: Module) -> dict[str, Any]:
    return {
        "id": str(module.id),
        "project_id": str(module.project_id),
        "name": module.name,
        "description": module.description,
        "status": module.status,
        "start_date": module.start_date,
        "target_date": module.target_date,
        "lead": str(module.lead_id) if module.lead_id else None,
        "created_at": module.created_at,
        "updated_at": module.updated_at,
    }


def _compact_comment(comment: IssueComment) -> dict[str, Any]:
    return {
        "id": str(comment.id),
        "body": comment.comment_stripped,
        "comment_html": comment.comment_html,
        "actor": {
            "id": str(comment.actor_id) if comment.actor_id else None,
            "display_name": comment.actor.display_name if comment.actor else None,
            "email": comment.actor.email if comment.actor else None,
        },
        "created_at": comment.created_at,
    }


def _compact_activity(activity: IssueActivity) -> dict[str, Any]:
    return {
        "id": str(activity.id),
        "verb": activity.verb,
        "field": activity.field,
        "old_value": activity.old_value,
        "new_value": activity.new_value,
        "comment": activity.comment,
        "actor": _compact_user(activity.actor) if activity.actor else None,
        "created_at": activity.created_at,
        "epoch": activity.epoch,
    }


def _compact_mesh_run(run: MeshLoopRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "work_item_id": str(run.work_item_id),
        "work_item_name": run.work_item.name,
        "definition_id": str(run.definition_id),
        "definition_slug": run.definition.slug,
        "definition_version": run.definition_version,
        "status": run.status,
        "current_node_id": run.current_node_id,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
    }


def _compact_mesh_stage(stage: MeshStageRun) -> dict[str, Any]:
    attempts = MeshRunAttempt.objects.filter(stage_run=stage, deleted_at__isnull=True).select_related("agent")
    return {
        "id": str(stage.id),
        "node_id": stage.node_id,
        "objective": stage.objective,
        "status": stage.status,
        "assigned_agent_id": stage.assigned_agent.agent_id if stage.assigned_agent else None,
        "attempts": [
            {
                "id": str(attempt.id),
                "agent_id": attempt.agent.agent_id,
                "provider": attempt.provider,
                "model": attempt.model,
                "configuration_version": attempt.configuration_version,
                "provider_run_id": attempt.provider_run_id,
                "provider_state": attempt.provider_state,
                "status": attempt.status,
                "failure_code": attempt.failure_code,
                "failure_message": attempt.failure_message,
                "input_tokens": attempt.input_tokens,
                "output_tokens": attempt.output_tokens,
                "cost": attempt.reported_cost,
                "model_provider": attempt.model_provider,
                "latency_ms": attempt.latency_ms,
                "evidence": attempt.evidence,
                "last_polled_at": attempt.last_polled_at,
                "heartbeat_at": attempt.heartbeat_at,
            }
            for attempt in attempts
        ],
    }


def _compact_link(link: IssueLink) -> dict[str, Any]:
    return {
        "id": str(link.id),
        "title": link.title,
        "url": link.url,
        "metadata": link.metadata,
        "work_item_id": str(link.issue_id),
        "created_at": link.created_at,
        "updated_at": link.updated_at,
    }


def _compact_relation(focus: Issue, relation: IssueRelation) -> dict[str, Any]:
    relation_type, related_issue = _display_relation(focus, relation)
    return {
        "id": str(relation.id),
        "relation_type": relation_type,
        "stored_relation_type": relation.relation_type,
        "work_item_id": str(focus.id),
        "related_work_item": _compact_issue(related_issue),
        "created_at": relation.created_at,
        "updated_at": relation.updated_at,
    }


def _display_relation(focus: Issue, relation: IssueRelation) -> tuple[str, Issue]:
    if relation.issue_id == focus.id:
        return relation.relation_type, relation.related_issue
    reverse = {
        "blocked_by": "blocking",
        "start_before": "start_after",
        "finish_before": "finish_after",
        "implemented_by": "implements",
    }
    return reverse.get(relation.relation_type, relation.relation_type), relation.issue


def _relation_type(value: str) -> str:
    allowed = {
        "blocking",
        "blocked_by",
        "duplicate",
        "relates_to",
        "start_after",
        "start_before",
        "finish_after",
        "finish_before",
        "implemented_by",
        "implements",
    }
    if value not in allowed:
        raise McpToolError(f"invalid relation_type: {value}", code=-32602)
    return value


def _optional_date(value: Any, name: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise McpToolError(f"{name} must be YYYY-MM-DD", code=-32602)


def _optional_datetime(value: Any, name: str) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise McpToolError(f"{name} must be an ISO-8601 date-time", code=-32602)


def _work_item_kind_value(value: Any) -> str:
    kind = str(value or "").strip().lower()
    if kind not in WORK_ITEM_KINDS:
        raise McpToolError(
            f"invalid work_item_kind: {value}",
            code=-32602,
            error_type="invalid_work_item_kind",
            hint=f"Use one of: {', '.join(WORK_ITEM_KINDS)}.",
            suggested_next_tools=["plane_list_work_item_kinds"],
        )
    return kind


def _module_status(value: Any) -> str:
    module_status = str(value or "planned").strip().lower()
    allowed = {"backlog", "planned", "in-progress", "paused", "completed", "cancelled"}
    if module_status not in allowed:
        raise McpToolError(f"invalid module status: {value}", code=-32602)
    return module_status


def _agent_id_for_user(user: User) -> str | None:
    if not user.is_bot:
        return None
    username = (user.username or "").strip().lower()
    if username.startswith("agent-"):
        return _normalize_agent_id(username.removeprefix("agent-"))
    email = (user.email or "").strip().lower()
    if email.startswith("agent-") and email.endswith("@agentpm.local"):
        return _normalize_agent_id(email.removeprefix("agent-").split("@", 1)[0])
    return None


def _normalize_agent_id(value: str) -> str:
    import re

    return re.sub(r"[^a-z0-9_-]+", "-", value.strip().lower()).strip("-_")


def _role_value(value: Any) -> int:
    raw = str(value or "member").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "admin": ROLE_ADMIN,
        "administrator": ROLE_ADMIN,
        "coordinator": ROLE_ADMIN,
        "member": ROLE_MEMBER,
        "worker": ROLE_MEMBER,
        "guest": ROLE_GUEST,
        "observer": ROLE_GUEST,
    }
    if raw in aliases:
        return aliases[raw]
    raise McpToolError(
        f"invalid project role: {value}",
        code=-32602,
        error_type="invalid_project_role",
        hint="Use one of: admin, member, guest.",
    )


def _valid_url(value: str) -> str:
    try:
        URLValidator(schemes=["http", "https"])(value)
    except ValidationError:
        raise McpToolError("url must be a valid http(s) URL", code=-32602)
    return value


def _integers_in_text(value: str) -> list[int]:
    import re

    return [int(match) for match in re.findall(r"\b\d+\b", value)]


def _looks_like_uuid(value: str) -> bool:
    import uuid

    try:
        uuid.UUID(str(value))
        return True
    except (TypeError, ValueError):
        return False
