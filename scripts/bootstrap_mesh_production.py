#!/usr/bin/env python3
"""Idempotently publish the baseline Mesh policy, Skill, roles, and knowledge."""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml
from django.db import transaction
from django.utils import timezone

from plane.bgtasks.mesh_indexer import index_mesh_page
from plane.db.models import (
    AgentProfile,
    MeshFunctionalRole,
    MeshProjectMemberRole,
    MeshProjectPolicy,
    MeshSkill,
    MeshSkillVersion,
    Page,
    Project,
    ProjectMember,
    WorkspaceMember,
)
from plane.mesh.skills import submit_skill_version
from plane.mesh.source_formats import parse_project_policy_yaml, parse_skill_markdown


ROLE_ASSIGNMENTS = {
    "hekate": ("pm", "reviewer"),
    "iris": ("developer",),
    "lingxi": ("tester",),
    "taichi": ("observer",),
}

POLICY_HANDOFFS = {
    "pm": ["developer", "tester", "reviewer"],
    "developer": ["tester", "reviewer"],
    "tester": ["developer", "reviewer"],
    "reviewer": ["developer"],
    "observer": [],
}


def _human_admin(project: Project):
    member = (
        ProjectMember.objects.filter(
            project=project,
            is_active=True,
            role__gte=20,
            member__is_bot=False,
            deleted_at__isnull=True,
        )
        .select_related("member")
        .first()
    )
    if member:
        return member.member
    workspace_member = (
        WorkspaceMember.objects.filter(
            workspace=project.workspace,
            is_active=True,
            role__gte=20,
            member__is_bot=False,
            deleted_at__isnull=True,
        )
        .select_related("member")
        .first()
    )
    if workspace_member:
        return workspace_member.member
    return project.workspace.owner


def _policy_payload(roles: list[MeshFunctionalRole]) -> dict:
    role_keys = {role.key for role in roles}
    return {
        "schema_version": 1,
        "roles": {
            role.key: {
                "capabilities": role.capabilities,
                "require_human_approval": False,
            }
            for role in roles
        },
        "allowed_handoffs": {
            role.key: [target for target in POLICY_HANDOFFS.get(role.key, []) if target in role_keys]
            for role in roles
        },
        "delegation": {"max_depth": 3, "explicit_assignee_required": True},
        "budgets": {
            "default_max_attempts": 2,
            "default_timeout_seconds": 3600,
        },
        "approvals": {
            "loop_publish": "human_project_admin",
            "skill_publish": "human_project_admin",
            "policy_publish": "human_project_admin",
        },
    }


@transaction.atomic
def _bootstrap_project(project: Project, source_text: str) -> dict:
    admin = _human_admin(project)
    if admin is None:
        return {"project": project.name, "status": "skipped", "reason": "no human admin"}

    roles = list(
        MeshFunctionalRole.objects.filter(project=project, deleted_at__isnull=True).order_by("sort_order", "name")
    )
    role_by_key = {role.key: role for role in roles}
    role_assignments = 0
    profiles = AgentProfile.objects.filter(
        workspace=project.workspace,
        status=AgentProfile.Status.ACTIVE,
        deleted_at__isnull=True,
    ).select_related("user")
    managed_role_keys = {role_key for assignments in ROLE_ASSIGNMENTS.values() for role_key in assignments}
    for profile in profiles:
        project_member = ProjectMember.objects.filter(
            project=project,
            member=profile.user,
            is_active=True,
            deleted_at__isnull=True,
        ).first()
        if not project_member:
            continue
        desired_role_keys = ROLE_ASSIGNMENTS.get(profile.agent_id, ("observer",))
        MeshProjectMemberRole.objects.filter(
            project=project,
            project_member=project_member,
            functional_role__key__in=managed_role_keys,
            deleted_at__isnull=True,
        ).exclude(functional_role__key__in=desired_role_keys).delete()
        for role_key in desired_role_keys:
            role = role_by_key.get(role_key)
            if not role:
                continue
            _, created = MeshProjectMemberRole.objects.get_or_create(
                workspace=project.workspace,
                project=project,
                project_member=project_member,
                functional_role=role,
                deleted_at__isnull=True,
                defaults={"assigned_by": admin},
            )
            role_assignments += int(created)

    policy_payload = _policy_payload(roles)
    policy_source = yaml.safe_dump(policy_payload, sort_keys=False, allow_unicode=True)
    parsed_policy = parse_project_policy_yaml(policy_source, known_role_keys=set(role_by_key))
    current_policy = (
        MeshProjectPolicy.objects.filter(
            project=project,
            status=MeshProjectPolicy.Status.PUBLISHED,
            deleted_at__isnull=True,
        )
        .order_by("-version")
        .first()
    )
    policy_created = False
    if not current_policy or current_policy.policy != parsed_policy:
        latest = (
            MeshProjectPolicy.objects.filter(project=project, deleted_at__isnull=True)
            .order_by("-version")
            .first()
        )
        MeshProjectPolicy.objects.filter(
            project=project,
            status=MeshProjectPolicy.Status.PUBLISHED,
            deleted_at__isnull=True,
        ).update(status=MeshProjectPolicy.Status.SUPERSEDED)
        MeshProjectPolicy.objects.create(
            workspace=project.workspace,
            project=project,
            version=(latest.version + 1 if latest else 1),
            status=MeshProjectPolicy.Status.PUBLISHED,
            source_yaml=policy_source,
            policy=parsed_policy,
            published_by=admin,
            published_at=timezone.now(),
            change_note="Mesh production baseline",
            created_by=admin,
        )
        policy_created = True

    parsed_skill = parse_skill_markdown(source_text)
    skill_slug = str(parsed_skill.manifest["name"]).strip().lower().replace(" ", "-")
    skill_version = str(parsed_skill.manifest["version"])
    existing_version = MeshSkillVersion.objects.filter(
        project=project,
        skill__slug=skill_slug,
        version=skill_version,
        deleted_at__isnull=True,
    ).first()
    skill_created = False
    if not existing_version:
        submitted = submit_skill_version(project=project, user=admin, source_text=source_text)
        existing_version = submitted.version
        skill_created = True
    if existing_version.status != MeshSkillVersion.Status.PUBLISHED:
        existing_version.status = MeshSkillVersion.Status.PUBLISHED
        existing_version.approved_by = admin
        existing_version.approved_at = timezone.now()
        existing_version.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    MeshSkill.objects.filter(id=existing_version.skill_id).update(visibility=MeshSkill.Visibility.PROJECT)

    indexed_pages = 0
    pages = Page.objects.filter(
        projects=project,
        project_pages__deleted_at__isnull=True,
        deleted_at__isnull=True,
    ).distinct()
    for page in pages:
        result = index_mesh_page(str(page.id))
        if result.get("status") in {"ready", "degraded"}:
            indexed_pages += 1

    return {
        "project": project.name,
        "project_id": str(project.id),
        "status": "ready",
        "role_assignments_created": role_assignments,
        "policy_created": policy_created,
        "skill_created": skill_created,
        "indexed_pages": indexed_pages,
    }


def main() -> None:
    skill_path = Path(os.environ["MESH_BOOTSTRAP_SKILL_PATH"])
    source_text = skill_path.read_text()
    workspace_slug = os.environ.get("MESH_BOOTSTRAP_WORKSPACE_SLUG", "").strip()
    projects = Project.objects.filter(deleted_at__isnull=True).select_related("workspace", "workspace__owner")
    if workspace_slug:
        projects = projects.filter(workspace__slug=workspace_slug)
    project_id = os.environ.get("MESH_BOOTSTRAP_PROJECT_ID", "").strip()
    if project_id:
        projects = projects.filter(id=project_id)
    results = [_bootstrap_project(project, source_text) for project in projects.order_by("workspace__slug", "name")]
    print(json.dumps({"projects": results}, ensure_ascii=False, indent=2))


main()
