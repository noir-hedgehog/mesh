# Copyright (c) 2026-present Mesh contributors
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from plane.db.models import (
    AgentProfile,
    IssueAssignee,
    MeshAuditEvent,
    MeshFunctionalRole,
    MeshHandoff,
    MeshLoopRun,
    MeshProjectMemberRole,
    MeshProjectPolicy,
    MeshStageRun,
    ProjectMember,
    User,
)


@dataclass(frozen=True)
class EligibleAgent:
    agent_id: str
    display_name: str
    plane_user_id: str
    runtime_provider: str
    functional_roles: tuple[str, ...]
    capabilities: tuple[str, ...]
    available: bool

    @property
    def public_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "display_name": self.display_name,
            "plane_user_id": self.plane_user_id,
            "runtime_provider": self.runtime_provider,
            "functional_roles": list(self.functional_roles),
            "capabilities": list(self.capabilities),
            "available": self.available,
        }


def list_eligible_agents(
    *, project_id: str, role_keys: list[str], required_capabilities: list[str]
) -> list[EligibleAgent]:
    normalized_roles = {value.strip().lower() for value in role_keys if value.strip()}
    required = {value.strip() for value in required_capabilities if value.strip()}
    assignments = (
        MeshProjectMemberRole.objects.filter(
            project_id=project_id,
            project_member__is_active=True,
            project_member__deleted_at__isnull=True,
            project_member__member__is_active=True,
            project_member__role__gte=15,
            functional_role__key__in=normalized_roles,
            deleted_at__isnull=True,
            functional_role__deleted_at__isnull=True,
        )
        .select_related("project_member__member", "functional_role")
        .order_by("project_member_id", "functional_role__sort_order")
    )
    roles_by_user: dict[str, list[MeshFunctionalRole]] = {}
    members_by_user: dict[str, ProjectMember] = {}
    for assignment in assignments:
        user_id = str(assignment.project_member.member_id)
        roles_by_user.setdefault(user_id, []).append(assignment.functional_role)
        members_by_user[user_id] = assignment.project_member

    profiles = AgentProfile.objects.filter(
        workspace_id__in={member.workspace_id for member in members_by_user.values()},
        user_id__in=roles_by_user,
        status=AgentProfile.Status.ACTIVE,
        deleted_at__isnull=True,
    ).select_related("user")

    result: list[EligibleAgent] = []
    for profile in profiles:
        if not bool((profile.agent_card or {}).get("available", False)):
            continue
        role_capabilities = {
            capability for role in roles_by_user[str(profile.user_id)] for capability in (role.capabilities or [])
        }
        claims = set(profile.capability_claims or [])
        denied = set((profile.boundaries or {}).get("denied_capabilities") or [])
        effective = (role_capabilities & claims if claims else role_capabilities) - denied
        if not required.issubset(effective):
            continue
        result.append(
            EligibleAgent(
                agent_id=profile.agent_id,
                display_name=profile.user.display_name or profile.user.email,
                plane_user_id=str(profile.user_id),
                runtime_provider=profile.runtime_provider,
                functional_roles=tuple(role.key for role in roles_by_user[str(profile.user_id)]),
                capabilities=tuple(sorted(effective)),
                available=True,
            )
        )
    return sorted(result, key=lambda item: item.agent_id)


@transaction.atomic
def leave_stage_unassigned(stage_run: MeshStageRun) -> MeshStageRun:
    stage_run.refresh_from_db()
    if stage_run.status in {MeshStageRun.Status.SUCCEEDED, MeshStageRun.Status.FAILED, MeshStageRun.Status.CANCELED}:
        return stage_run
    previous_agent = stage_run.assigned_agent
    if previous_agent:
        MeshHandoff.objects.filter(
            loop_run_id=stage_run.loop_run_id,
            to_node_id=stage_run.node_id,
            target_agent=previous_agent,
            status=MeshHandoff.Status.ASSIGNED,
            deleted_at__isnull=True,
        ).update(status=MeshHandoff.Status.CANCELED)
    IssueAssignee.objects.filter(issue_id=stage_run.loop_run.work_item_id, deleted_at__isnull=True).delete()
    stage_run.assigned_agent = None
    stage_run.status = MeshStageRun.Status.WAITING_FOR_ASSIGNEE
    stage_run.save(update_fields=["assigned_agent", "status", "updated_at"])
    loop_run = MeshLoopRun.objects.select_for_update().get(id=stage_run.loop_run_id)
    loop_run.status = MeshLoopRun.Status.WAITING_FOR_ASSIGNEE
    loop_run.save(update_fields=["status", "updated_at"])
    return stage_run


@transaction.atomic
def assign_stage(
    *,
    stage_run: MeshStageRun,
    target_agent: AgentProfile,
    target_role: MeshFunctionalRole,
    selected_by_user: User,
    reason: str = "",
) -> MeshStageRun:
    """Apply an explicit handoff to Mesh and Plane as one atomic action."""
    stage_run = (
        MeshStageRun.objects.select_for_update()
        .select_related("loop_run__work_item")
        .get(id=stage_run.id, deleted_at__isnull=True)
    )
    if stage_run.status not in {MeshStageRun.Status.WAITING_FOR_ASSIGNEE, MeshStageRun.Status.QUEUED}:
        raise ValueError(f"Stage cannot be assigned from status {stage_run.status}")
    if stage_run.loop_run.status in {MeshLoopRun.Status.COMPLETED, MeshLoopRun.Status.FAILED, MeshLoopRun.Status.CANCELED}:
        raise ValueError("A terminal Loop cannot be assigned")
    previous_stage = (
        MeshStageRun.objects.filter(
            loop_run_id=stage_run.loop_run_id,
            status=MeshStageRun.Status.SUCCEEDED,
            created_at__lt=stage_run.created_at,
            deleted_at__isnull=True,
        )
        .select_related("assigned_agent", "functional_role")
        .order_by("-created_at")
        .first()
    )
    if previous_stage and previous_stage.functional_role_id:
        source_role = previous_stage.functional_role
        if source_role.allowed_handoff_role_keys and target_role.key not in source_role.allowed_handoff_role_keys:
            raise ValueError(f"Role {source_role.key} cannot hand off to role {target_role.key}")
        policy = MeshProjectPolicy.objects.filter(
            project_id=stage_run.project_id,
            status=MeshProjectPolicy.Status.PUBLISHED,
            deleted_at__isnull=True,
        ).first()
        allowed_handoffs = (policy.policy or {}).get("allowed_handoffs", {}) if policy else {}
        if source_role.key in allowed_handoffs and target_role.key not in allowed_handoffs[source_role.key]:
            raise ValueError(f"Project Policy forbids handoff {source_role.key} -> {target_role.key}")
    IssueAssignee.objects.filter(issue_id=stage_run.loop_run.work_item_id, deleted_at__isnull=True).delete()
    IssueAssignee.objects.create(
        workspace_id=stage_run.workspace_id,
        project_id=stage_run.project_id,
        issue_id=stage_run.loop_run.work_item_id,
        assignee_id=target_agent.user_id,
        created_by_id=selected_by_user.id,
    )
    stage_run.functional_role = target_role
    stage_run.assigned_agent = target_agent
    stage_run.status = MeshStageRun.Status.QUEUED
    stage_run.save(update_fields=["functional_role", "assigned_agent", "status", "updated_at"])
    MeshLoopRun.objects.filter(id=stage_run.loop_run_id).update(status=MeshLoopRun.Status.RUNNING)

    if previous_stage:
        MeshHandoff.objects.create(
            workspace_id=stage_run.workspace_id,
            project_id=stage_run.project_id,
            loop_run_id=stage_run.loop_run_id,
            from_stage=previous_stage,
            to_node_id=stage_run.node_id,
            target_role=target_role,
            from_agent=previous_stage.assigned_agent,
            target_agent=target_agent,
            selected_by_user=selected_by_user,
            status=MeshHandoff.Status.ASSIGNED,
            reason=reason,
        )
    MeshAuditEvent.objects.create(
        workspace_id=stage_run.workspace_id,
        project_id=stage_run.project_id,
        loop_run_id=stage_run.loop_run_id,
        work_item_id=stage_run.loop_run.work_item_id,
        actor_user=selected_by_user,
        actor_agent=AgentProfile.objects.filter(
            user=selected_by_user, workspace_id=stage_run.workspace_id, deleted_at__isnull=True
        ).first(),
        event_type="stage.assigned",
        payload={
            "stage_run_id": str(stage_run.id),
            "target_agent_id": target_agent.agent_id,
            "target_role": target_role.key,
            "reason": reason,
        },
        occurred_at=timezone.now(),
    )
    return stage_run
