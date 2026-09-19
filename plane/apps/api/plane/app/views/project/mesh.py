# Copyright (c) 2026-present Mesh contributors
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from pgvector.django import CosineDistance
from rest_framework import status
from rest_framework.response import Response

from plane.app.permissions import ProjectAdminPermission, ProjectMemberPermission
from plane.app.views.base import BaseAPIView
from plane.db.models import (
    AgentProfile,
    IssueAssignee,
    MeshAuditEvent,
    MeshApproval,
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
    ProjectMember,
)
from plane.mesh.discovery import assign_stage, leave_stage_unassigned, list_eligible_agents
from plane.mesh.runtime import cancel_loop, queue_stage_start_on_commit, resolve_approval, start_loop
from plane.mesh.skills import submit_skill_version
from plane.mesh.source_formats import parse_loop_yaml, parse_project_policy_yaml, sha256_text


DEFAULT_FUNCTIONAL_ROLES = (
    ("pm", "PM", ["work.read", "work.assign", "loop.draft", "handoff.select"]),
    ("developer", "Developer", ["work.read", "work.update", "code.write", "test.run"]),
    ("tester", "Tester", ["work.read", "work.comment", "test.run", "handoff.reject"]),
    ("reviewer", "Reviewer", ["work.read", "work.comment", "review.approve"]),
    ("observer", "Observer", ["work.read", "work.comment"]),
)


def _seed_default_roles(project_id: str, workspace_id: str) -> None:
    if MeshFunctionalRole.objects.filter(project_id=project_id, deleted_at__isnull=True).exists():
        return
    for index, (key, name, capabilities) in enumerate(DEFAULT_FUNCTIONAL_ROLES):
        MeshFunctionalRole.objects.create(
            project_id=project_id,
            workspace_id=workspace_id,
            key=key,
            name=name,
            capabilities=capabilities,
            is_default=True,
            sort_order=index * 1000,
        )


class MeshProjectRolesEndpoint(BaseAPIView):
    permission_classes = [ProjectMemberPermission]

    def get(self, request, slug, project_id):
        project_member = ProjectMember.objects.filter(
            project_id=project_id, member=request.user, is_active=True, deleted_at__isnull=True
        ).first()
        if not project_member:
            return Response({"error": "Project membership is required"}, status=status.HTTP_403_FORBIDDEN)
        _seed_default_roles(str(project_id), str(project_member.workspace_id))
        roles = MeshFunctionalRole.objects.filter(project_id=project_id, deleted_at__isnull=True)
        return Response({"roles": [_role_dict(role) for role in roles]})

    def post(self, request, slug, project_id):
        if not _is_project_admin(request.user.id, project_id):
            return Response({"error": "Project Admin permission is required"}, status=status.HTTP_403_FORBIDDEN)
        key = str(request.data.get("key") or "").strip().lower()
        name = str(request.data.get("name") or "").strip()
        if not key or not name:
            return Response({"error": "key and name are required"}, status=status.HTTP_400_BAD_REQUEST)
        member = ProjectMember.objects.filter(
            project_id=project_id, member=request.user, deleted_at__isnull=True
        ).first()
        role = MeshFunctionalRole.objects.create(
            project_id=project_id,
            workspace_id=member.workspace_id,
            key=key,
            name=name,
            description=str(request.data.get("description") or ""),
            capabilities=list(request.data.get("capabilities") or []),
            allowed_handoff_role_keys=list(request.data.get("allowed_handoff_role_keys") or []),
        )
        return Response({"role": _role_dict(role)}, status=status.HTTP_201_CREATED)


class MeshMemberRolesEndpoint(BaseAPIView):
    permission_classes = [ProjectAdminPermission]

    @transaction.atomic
    def put(self, request, slug, project_id, project_member_id):
        project_member = (
            ProjectMember.objects.select_for_update()
            .filter(id=project_member_id, project_id=project_id, is_active=True, deleted_at__isnull=True)
            .first()
        )
        if not project_member:
            return Response({"error": "Project member not found"}, status=status.HTTP_404_NOT_FOUND)
        role_ids = list(dict.fromkeys(str(value) for value in request.data.get("role_ids") or []))
        roles = list(MeshFunctionalRole.objects.filter(id__in=role_ids, project_id=project_id, deleted_at__isnull=True))
        if len(roles) != len(role_ids):
            return Response({"error": "One or more functional roles are invalid"}, status=status.HTTP_400_BAD_REQUEST)
        MeshProjectMemberRole.objects.filter(project_member=project_member, deleted_at__isnull=True).delete()
        for role in roles:
            MeshProjectMemberRole.objects.create(
                project_id=project_id,
                workspace_id=project_member.workspace_id,
                project_member=project_member,
                functional_role=role,
                assigned_by=request.user,
            )
        return Response({"project_member_id": str(project_member.id), "roles": [_role_dict(role) for role in roles]})


class MeshEligibleAgentsEndpoint(BaseAPIView):
    permission_classes = [ProjectMemberPermission]

    def get(self, request, slug, project_id):
        roles = [value for value in request.query_params.get("roles", "").split(",") if value]
        capabilities = [value for value in request.query_params.get("capabilities", "").split(",") if value]
        if not roles:
            return Response({"error": "roles is required"}, status=status.HTTP_400_BAD_REQUEST)
        agents = list_eligible_agents(project_id=str(project_id), role_keys=roles, required_capabilities=capabilities)
        return Response({"agents": [agent.public_dict for agent in agents]})


class MeshProjectPolicyEndpoint(BaseAPIView):
    permission_classes = [ProjectMemberPermission]

    def get(self, request, slug, project_id):
        policies = MeshProjectPolicy.objects.filter(project_id=project_id, deleted_at__isnull=True)
        if request.query_params.get("history") == "true":
            return Response({"policies": [_policy_dict(policy) for policy in policies[:100]]})
        policy = policies.filter(status=MeshProjectPolicy.Status.PUBLISHED).first()
        if not policy:
            return Response({"error": "Published Mesh Project Policy not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response({"policy": _policy_dict(policy)})

    @transaction.atomic
    def post(self, request, slug, project_id):
        if not _is_project_admin(request.user.id, project_id):
            return Response({"error": "Human Project Admin permission is required"}, status=status.HTTP_403_FORBIDDEN)
        if request.user.is_bot:
            return Response({"error": "Agents can propose Policy changes but cannot publish them"}, status=403)
        source_yaml = str(request.data.get("source_yaml") or "")
        role_keys = set(
            MeshFunctionalRole.objects.filter(project_id=project_id, deleted_at__isnull=True).values_list(
                "key", flat=True
            )
        )
        try:
            policy_data = parse_project_policy_yaml(source_yaml, known_role_keys=role_keys)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        member = ProjectMember.objects.get(project_id=project_id, member=request.user, deleted_at__isnull=True)
        latest = (
            MeshProjectPolicy.objects.filter(project_id=project_id, deleted_at__isnull=True)
            .order_by("-version")
            .first()
        )
        MeshProjectPolicy.objects.filter(
            project_id=project_id, status=MeshProjectPolicy.Status.PUBLISHED, deleted_at__isnull=True
        ).update(status=MeshProjectPolicy.Status.SUPERSEDED)
        policy = MeshProjectPolicy.objects.create(
            workspace_id=member.workspace_id,
            project_id=project_id,
            version=(latest.version + 1 if latest else 1),
            status=MeshProjectPolicy.Status.PUBLISHED,
            source_yaml=source_yaml,
            policy=policy_data,
            published_by=request.user,
            published_at=timezone.now(),
            change_note=str(request.data.get("change_note") or ""),
        )
        return Response({"policy": _policy_dict(policy)}, status=status.HTTP_201_CREATED)


class MeshSkillsEndpoint(BaseAPIView):
    permission_classes = [ProjectMemberPermission]

    def get(self, request, slug, project_id):
        skills = MeshSkill.objects.filter(project_id=project_id, deleted_at__isnull=True).prefetch_related("versions")
        return Response({"skills": [_skill_dict(skill) for skill in skills]})

    def post(self, request, slug, project_id):
        source_text = str(request.data.get("source_text") or "")
        try:
            member = ProjectMember.objects.select_related("project").get(
                project_id=project_id, member=request.user, deleted_at__isnull=True
            )
            submitted = submit_skill_version(project=member.project, user=request.user, source_text=source_text)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            {"skill": _skill_dict(submitted.skill), "version": _skill_version_dict(submitted.version)}, status=201
        )


class MeshSkillPublishEndpoint(BaseAPIView):
    permission_classes = [ProjectAdminPermission]

    def post(self, request, slug, project_id, version_id):
        version = MeshSkillVersion.objects.filter(id=version_id, project_id=project_id, deleted_at__isnull=True).first()
        if not version:
            return Response({"error": "Skill version not found"}, status=status.HTTP_404_NOT_FOUND)
        version.status = MeshSkillVersion.Status.PUBLISHED
        version.approved_by = request.user
        version.approved_at = timezone.now()
        version.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
        return Response({"version": _skill_version_dict(version)})


class MeshKnowledgeSearchEndpoint(BaseAPIView):
    permission_classes = [ProjectMemberPermission]

    def post(self, request, slug, project_id):
        query_text = str(request.data.get("query") or "").strip()
        embedding = request.data.get("embedding")
        if not query_text and not embedding:
            return Response({"error": "query or embedding is required"}, status=status.HTTP_400_BAD_REQUEST)
        chunks = MeshKnowledgeChunk.objects.filter(project_id=project_id, deleted_at__isnull=True).select_related(
            "document__page", "document__page_version"
        )
        if query_text:
            query = SearchQuery(query_text, search_type="websearch")
            chunks = chunks.annotate(text_rank=SearchRank(F("content_search"), query)).filter(text_rank__gt=0)
        else:
            chunks = chunks.annotate(text_rank=F("sort_order") * 0)
        if isinstance(embedding, list) and len(embedding) == 1536:
            chunks = chunks.annotate(distance=CosineDistance("embedding", embedding)).order_by("distance", "-text_rank")
        else:
            chunks = chunks.order_by("-text_rank", "document_id", "sort_order")
        limit = min(max(int(request.data.get("limit") or 10), 1), 50)
        return Response({"results": [_knowledge_chunk_dict(chunk) for chunk in chunks[:limit]]})


class MeshLoopsEndpoint(BaseAPIView):
    permission_classes = [ProjectMemberPermission]

    def get(self, request, slug, project_id):
        loops = MeshLoopDefinition.objects.filter(project_id=project_id, deleted_at__isnull=True)
        return Response({"loops": [_loop_definition_dict(loop) for loop in loops]})

    def post(self, request, slug, project_id):
        if not _can_draft_loop(request.user.id, project_id):
            return Response({"error": "PM functional role or Project Admin is required"}, status=403)
        source_yaml = str(request.data.get("source_yaml") or "")
        try:
            graph = parse_loop_yaml(source_yaml)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        member = ProjectMember.objects.filter(
            project_id=project_id, member=request.user, deleted_at__isnull=True
        ).first()
        slug_value = str(request.data.get("slug") or graph["name"]).strip().lower().replace(" ", "-")
        latest = (
            MeshLoopDefinition.objects.filter(project_id=project_id, slug=slug_value, deleted_at__isnull=True)
            .order_by("-version")
            .first()
        )
        loop = MeshLoopDefinition.objects.create(
            workspace_id=member.workspace_id,
            project_id=project_id,
            slug=slug_value,
            name=graph["name"],
            description=str(graph.get("description") or ""),
            version=(latest.version + 1 if latest else 1),
            source_yaml=source_yaml,
            graph=graph,
            checksum=sha256_text(source_yaml),
            change_note=str(request.data.get("change_note") or ""),
        )
        return Response({"loop": _loop_definition_dict(loop)}, status=status.HTTP_201_CREATED)


class MeshLoopPublishEndpoint(BaseAPIView):
    permission_classes = [ProjectAdminPermission]

    @transaction.atomic
    def post(self, request, slug, project_id, loop_id):
        loop = (
            MeshLoopDefinition.objects.select_for_update()
            .filter(id=loop_id, project_id=project_id, deleted_at__isnull=True)
            .first()
        )
        if not loop:
            return Response({"error": "Loop definition not found"}, status=status.HTTP_404_NOT_FOUND)
        MeshLoopDefinition.objects.filter(
            project_id=project_id,
            slug=loop.slug,
            status=MeshLoopDefinition.Status.PUBLISHED,
            deleted_at__isnull=True,
        ).exclude(id=loop.id).update(status=MeshLoopDefinition.Status.SUPERSEDED)
        loop.status = MeshLoopDefinition.Status.PUBLISHED
        loop.published_by = request.user
        loop.published_at = timezone.now()
        loop.save(update_fields=["status", "published_by", "published_at", "updated_at"])
        return Response({"loop": _loop_definition_dict(loop)})


class MeshLoopStartEndpoint(BaseAPIView):
    permission_classes = [ProjectMemberPermission]

    @transaction.atomic
    def post(self, request, slug, project_id, loop_id):
        if not _can_draft_loop(request.user.id, project_id):
            return Response({"error": "PM functional role or Project Admin is required"}, status=403)
        loop = MeshLoopDefinition.objects.filter(
            id=loop_id,
            project_id=project_id,
            status=MeshLoopDefinition.Status.PUBLISHED,
            deleted_at__isnull=True,
        ).first()
        if not loop:
            return Response({"error": "Published Loop definition not found"}, status=status.HTTP_404_NOT_FOUND)
        from plane.db.models import Issue

        work_item = Issue.issue_objects.filter(
            id=request.data.get("work_item_id"), project_id=project_id, deleted_at__isnull=True
        ).first()
        if not work_item:
            return Response({"error": "Work item not found"}, status=status.HTTP_404_NOT_FOUND)
        run, created = start_loop(definition=loop, work_item=work_item, actor=request.user)
        return Response(
            {"run": _loop_run_dict(run, include_stages=True)},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class MeshApprovalsEndpoint(BaseAPIView):
    permission_classes = [ProjectAdminPermission]

    def get(self, request, slug, project_id, approval_id=None):
        approvals = MeshApproval.objects.filter(project_id=project_id, deleted_at__isnull=True).select_related(
            "loop_run__work_item", "stage_run", "reviewer"
        )
        if approval_id:
            approval = approvals.filter(id=approval_id).first()
            if not approval:
                return Response({"error": "Approval not found"}, status=status.HTTP_404_NOT_FOUND)
            return Response({"approval": _approval_dict(approval)})
        return Response({"approvals": [_approval_dict(item) for item in approvals[:100]]})

    @transaction.atomic
    def post(self, request, slug, project_id, approval_id=None):
        if request.user.is_bot:
            return Response({"error": "Formal approval requires a Human Project Admin"}, status=403)
        approval = (
            MeshApproval.objects.select_for_update()
            .filter(id=approval_id, project_id=project_id, status=MeshApproval.Status.PENDING, deleted_at__isnull=True)
            .first()
        )
        if not approval:
            return Response({"error": "Pending approval not found"}, status=status.HTTP_404_NOT_FOUND)
        decision = str(request.data.get("decision") or "").lower()
        if decision not in {"approve", "reject"}:
            return Response({"error": "decision must be approve or reject"}, status=status.HTTP_400_BAD_REQUEST)
        resolve_approval(
            approval=approval,
            reviewer=request.user,
            approved=decision == "approve",
            decision_note=str(request.data.get("decision_note") or ""),
        )
        approval.refresh_from_db()
        MeshAuditEvent.objects.create(
            workspace_id=approval.workspace_id,
            project_id=project_id,
            loop_run_id=approval.loop_run_id,
            work_item_id=approval.loop_run.work_item_id,
            actor_user=request.user,
            event_type=f"approval.{approval.status}",
            payload={"approval_id": str(approval.id), "decision_note": approval.decision_note},
            occurred_at=timezone.now(),
        )
        return Response({"approval": _approval_dict(approval)})


class MeshStageAssignmentEndpoint(BaseAPIView):
    permission_classes = [ProjectMemberPermission]

    @transaction.atomic
    def post(self, request, slug, project_id, stage_run_id):
        stage = (
            MeshStageRun.objects.select_for_update(of=("self",))
            .select_related("loop_run__work_item", "functional_role", "loop_run__definition")
            .filter(id=stage_run_id, project_id=project_id, deleted_at__isnull=True)
            .first()
        )
        if not stage:
            return Response({"error": "Stage run not found"}, status=status.HTTP_404_NOT_FOUND)
        if not _can_assign_stage(request.user.id, stage):
            return Response(
                {"error": "Only the previous Agent, a PM Agent, or Project Admin can assign this stage"}, status=403
            )
        target_agent_id = str(request.data.get("target_agent_id") or "").strip()
        if not target_agent_id:
            leave_stage_unassigned(stage)
            return Response({"stage": _stage_run_dict(stage)})
        role_keys = (
            [stage.functional_role.key] if stage.functional_role else list(_stage_node(stage).get("roles") or [])
        )
        required = list(_stage_node(stage).get("required_capabilities") or [])
        eligible = {
            item.agent_id: item
            for item in list_eligible_agents(
                project_id=str(project_id), role_keys=role_keys, required_capabilities=required
            )
        }
        if target_agent_id not in eligible or not eligible[target_agent_id].available:
            leave_stage_unassigned(stage)
            return Response(
                {
                    "error": "Target Agent is not an available eligible project member",
                    "eligible_agents": list(eligible),
                },
                status=status.HTTP_409_CONFLICT,
            )
        candidate = eligible[target_agent_id]
        profile = AgentProfile.objects.get(
            workspace_id=stage.workspace_id, agent_id=target_agent_id, deleted_at__isnull=True
        )
        target_role = (
            MeshFunctionalRole.objects.filter(
                project_id=project_id,
                key__in=set(candidate.functional_roles) & set(role_keys),
                deleted_at__isnull=True,
            )
            .order_by("sort_order")
            .first()
        )
        if not target_role:
            leave_stage_unassigned(stage)
            return Response({"error": "Target Agent has no eligible role for this Stage"}, status=409)
        try:
            stage = assign_stage(
                stage_run=stage,
                target_agent=profile,
                target_role=target_role,
                selected_by_user=request.user,
                reason=str(request.data.get("reason") or ""),
            )
        except ValueError as exc:
            leave_stage_unassigned(stage)
            return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)
        queue_stage_start_on_commit(str(stage.id))
        return Response({"stage": _stage_run_dict(stage)})


class MeshRunsEndpoint(BaseAPIView):
    permission_classes = [ProjectMemberPermission]

    def get(self, request, slug, project_id, loop_run_id=None):
        runs = MeshLoopRun.objects.filter(project_id=project_id, deleted_at__isnull=True).select_related(
            "work_item", "definition"
        )
        if loop_run_id:
            run = runs.filter(id=loop_run_id).first()
            if not run:
                return Response({"error": "Loop run not found"}, status=status.HTTP_404_NOT_FOUND)
            return Response({"run": _loop_run_dict(run, include_stages=True)})
        return Response({"runs": [_loop_run_dict(run) for run in runs[:100]]})

    def post(self, request, slug, project_id, loop_run_id=None):
        if not loop_run_id:
            return Response({"error": "loop_run_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        if not _can_draft_loop(request.user.id, project_id):
            return Response({"error": "PM functional role or Project Admin is required"}, status=403)
        run = MeshLoopRun.objects.filter(id=loop_run_id, project_id=project_id, deleted_at__isnull=True).first()
        if not run:
            return Response({"error": "Loop run not found"}, status=status.HTTP_404_NOT_FOUND)
        run = cancel_loop(run=run, actor=request.user, reason=str(request.data.get("reason") or ""))
        return Response({"run": _loop_run_dict(run, include_stages=True)})


def _role_dict(role):
    return {
        "id": str(role.id),
        "key": role.key,
        "name": role.name,
        "description": role.description,
        "capabilities": role.capabilities,
        "allowed_handoff_role_keys": role.allowed_handoff_role_keys,
        "is_default": role.is_default,
    }


def _policy_dict(policy):
    return {
        "id": str(policy.id),
        "version": policy.version,
        "status": policy.status,
        "source_yaml": policy.source_yaml,
        "policy": policy.policy,
        "published_by": str(policy.published_by_id) if policy.published_by_id else None,
        "published_at": policy.published_at,
        "change_note": policy.change_note,
    }


def _skill_dict(skill):
    versions = list(skill.versions.all())
    published = next((version for version in versions if version.status == "published"), None)
    return {
        "id": str(skill.id),
        "slug": skill.slug,
        "name": skill.name,
        "description": skill.description,
        "visibility": skill.visibility,
        "published_version": published.version if published else None,
        "versions": [_skill_version_dict(version) for version in versions],
    }


def _skill_version_dict(version):
    return {
        "id": str(version.id),
        "version": version.version,
        "manifest": version.manifest,
        "checksum": version.checksum,
        "status": version.status,
        "approved_at": version.approved_at,
    }


def _knowledge_chunk_dict(chunk):
    return {
        "id": str(chunk.id),
        "heading": chunk.heading,
        "content": chunk.content,
        "citation": {
            "page_id": str(chunk.document.page_id),
            "page_version_id": str(chunk.document.page_version_id) if chunk.document.page_version_id else None,
            "page_name": chunk.document.page.name,
            "heading": chunk.heading,
        },
        "text_rank": float(getattr(chunk, "text_rank", 0) or 0),
        "distance": float(getattr(chunk, "distance", 0) or 0) if hasattr(chunk, "distance") else None,
    }


def _loop_definition_dict(loop):
    return {
        "id": str(loop.id),
        "slug": loop.slug,
        "name": loop.name,
        "description": loop.description,
        "version": loop.version,
        "status": loop.status,
        "source_yaml": loop.source_yaml,
        "graph": loop.graph,
        "checksum": loop.checksum,
        "published_at": loop.published_at,
    }


def _stage_run_dict(stage):
    attempts = MeshRunAttempt.objects.filter(stage_run=stage, deleted_at__isnull=True)
    return {
        "id": str(stage.id),
        "node_id": stage.node_id,
        "objective": stage.objective,
        "status": stage.status,
        "assigned_agent_id": stage.assigned_agent.agent_id if stage.assigned_agent else None,
        "functional_role": stage.functional_role.key if stage.functional_role else None,
        "attempts": [
            {
                "id": str(attempt.id),
                "agent_id": attempt.agent.agent_id,
                "provider": attempt.provider,
                "model": attempt.model,
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
            for attempt in attempts.select_related("agent")
        ],
    }


def _loop_run_dict(run, include_stages=False):
    payload = {
        "id": str(run.id),
        "work_item_id": str(run.work_item_id),
        "work_item_name": run.work_item.name,
        "definition_id": str(run.definition_id),
        "definition_version": run.definition_version,
        "status": run.status,
        "current_node_id": run.current_node_id,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
    }
    if include_stages:
        payload["stages"] = [
            _stage_run_dict(stage) for stage in run.stages.select_related("assigned_agent", "functional_role")
        ]
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
    return payload


def _approval_dict(approval):
    return {
        "id": str(approval.id),
        "status": approval.status,
        "loop_run_id": str(approval.loop_run_id),
        "stage_run_id": str(approval.stage_run_id),
        "work_item_id": str(approval.loop_run.work_item_id),
        "work_item_name": approval.loop_run.work_item.name,
        "reviewer_id": str(approval.reviewer_id) if approval.reviewer_id else None,
        "decision_note": approval.decision_note,
        "created_at": approval.created_at,
        "resolved_at": approval.resolved_at,
    }


def _first_stage_node(graph):
    nodes = {str(node.get("id")): node for node in graph.get("nodes") or []}
    trigger = next((node for node in nodes.values() if node.get("type") == "trigger"), None)
    if not trigger:
        return None
    adjacency = {}
    for edge in graph.get("edges") or []:
        adjacency.setdefault(str(edge.get("from")), []).append(str(edge.get("to")))
    pending = list(adjacency.get(str(trigger["id"]), []))
    visited = set()
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


def _is_project_admin(user_id, project_id):
    return ProjectMember.objects.filter(
        project_id=project_id, member_id=user_id, role__gte=20, is_active=True, deleted_at__isnull=True
    ).exists()


def _can_draft_loop(user_id, project_id):
    if _is_project_admin(user_id, project_id):
        return True
    return MeshProjectMemberRole.objects.filter(
        project_id=project_id,
        project_member__member_id=user_id,
        project_member__is_active=True,
        functional_role__key="pm",
        deleted_at__isnull=True,
    ).exists()


def _can_assign_stage(user_id, stage):
    if _is_project_admin(user_id, stage.project_id):
        return True
    profile = AgentProfile.objects.filter(user_id=user_id, workspace_id=stage.workspace_id, status="active").first()
    if not profile:
        return False
    if MeshProjectMemberRole.objects.filter(
        project_id=stage.project_id,
        project_member__member_id=user_id,
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


def _stage_node(stage):
    return next(
        (node for node in (stage.loop_run.definition.graph.get("nodes") or []) if node.get("id") == stage.node_id),
        {},
    )
