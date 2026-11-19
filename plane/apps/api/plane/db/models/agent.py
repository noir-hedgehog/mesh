# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.conf import settings
from django.db import models

from .workspace import WorkspaceBaseModel


class AgentRegistrationApplication(WorkspaceBaseModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    agent_id = models.CharField(max_length=64)
    display_name = models.CharField(max_length=255)
    email = models.EmailField(max_length=255)
    requested_role = models.CharField(max_length=16, default="member")
    reason = models.TextField(blank=True)
    agent_type = models.CharField(max_length=64, default="autonomous")
    runtime_provider = models.CharField(max_length=64, default="custom")
    default_model = models.CharField(max_length=255, blank=True)
    endpoint_url = models.URLField(max_length=1024, blank=True)
    agent_card = models.JSONField(default=dict, blank=True)
    capability_claims = models.JSONField(default=list, blank=True)
    boundaries = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    source = models.CharField(max_length=32, default="bootstrap")
    invite_id = models.UUIDField(null=True, unique=True)
    reviewed_by = models.ForeignKey(
        "db.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="reviewed_agent_applications",
    )
    review_note = models.TextField(blank=True)
    reviewed_at = models.DateTimeField(null=True)

    class Meta:
        db_table = "agent_registration_applications"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "agent_id"],
                condition=models.Q(status="pending", deleted_at__isnull=True),
                name="unique_pending_agent_application",
            )
        ]


class AgentProfile(WorkspaceBaseModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"
        RETIRED = "retired", "Retired"

    class TrustLevel(models.TextChoices):
        UNVERIFIED = "unverified", "Unverified"
        VERIFIED = "verified", "Verified"
        TRUSTED = "trusted", "Trusted"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="mesh_agent_profiles")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_mesh_agents",
    )
    agent_id = models.CharField(max_length=64)
    agent_type = models.CharField(max_length=64, default="autonomous")
    runtime_provider = models.CharField(max_length=64, default="custom")
    endpoint_url = models.URLField(max_length=1024, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    trust_level = models.CharField(max_length=16, choices=TrustLevel.choices, default=TrustLevel.UNVERIFIED)
    agent_card = models.JSONField(default=dict, blank=True)
    capability_claims = models.JSONField(default=list, blank=True)
    boundaries = models.JSONField(default=dict, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "mesh_agent_profiles"
        ordering = ("agent_id",)
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "agent_id"],
                condition=models.Q(deleted_at__isnull=True),
                name="mesh_agent_profile_unique_agent_id",
            ),
            models.UniqueConstraint(
                fields=["workspace", "user"],
                condition=models.Q(deleted_at__isnull=True),
                name="mesh_agent_profile_unique_user",
            ),
        ]


class AgentExecutionProfile(WorkspaceBaseModel):
    agent = models.ForeignKey(AgentProfile, on_delete=models.CASCADE, related_name="execution_profiles")
    provider = models.CharField(max_length=64)
    model = models.CharField(max_length=255)
    configuration_version = models.PositiveIntegerField(default=1)
    secret_reference = models.CharField(max_length=512, blank=True)
    settings = models.JSONField(default=dict, blank=True)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "mesh_agent_execution_profiles"
        ordering = ("-is_default", "provider", "model")
        constraints = [
            models.UniqueConstraint(
                fields=["agent", "provider", "model", "configuration_version"],
                condition=models.Q(deleted_at__isnull=True),
                name="mesh_execution_profile_unique_version",
            )
        ]

    def save(self, *args, **kwargs):
        if self.is_default:
            AgentExecutionProfile.objects.filter(
                agent=self.agent, is_default=True, deleted_at__isnull=True
            ).exclude(id=self.id).update(is_default=False)
        super().save(*args, **kwargs)


class MeshFunctionalRole(WorkspaceBaseModel):
    key = models.SlugField(max_length=64)
    name = models.CharField(max_length=128)
    description = models.TextField(blank=True)
    capabilities = models.JSONField(default=list, blank=True)
    allowed_handoff_role_keys = models.JSONField(default=list, blank=True)
    is_default = models.BooleanField(default=False)
    sort_order = models.FloatField(default=65535)

    class Meta:
        db_table = "mesh_functional_roles"
        ordering = ("sort_order", "name")
        constraints = [
            models.UniqueConstraint(
                fields=["project", "key"],
                condition=models.Q(deleted_at__isnull=True),
                name="mesh_functional_role_unique_key",
            )
        ]


class MeshProjectMemberRole(WorkspaceBaseModel):
    project_member = models.ForeignKey(
        "db.ProjectMember", on_delete=models.CASCADE, related_name="mesh_functional_roles"
    )
    functional_role = models.ForeignKey(
        MeshFunctionalRole, on_delete=models.CASCADE, related_name="member_assignments"
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_mesh_functional_roles",
    )

    class Meta:
        db_table = "mesh_project_member_roles"
        constraints = [
            models.UniqueConstraint(
                fields=["project_member", "functional_role"],
                condition=models.Q(deleted_at__isnull=True),
                name="mesh_project_member_role_unique",
            )
        ]


class MeshProjectPolicy(WorkspaceBaseModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        SUPERSEDED = "superseded", "Superseded"

    version = models.PositiveIntegerField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    source_yaml = models.TextField(blank=True)
    policy = models.JSONField(default=dict)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="published_mesh_policies",
    )
    change_note = models.TextField(blank=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "mesh_project_policies"
        ordering = ("-version",)
        constraints = [
            models.UniqueConstraint(
                fields=["project", "version"],
                condition=models.Q(deleted_at__isnull=True),
                name="mesh_project_policy_unique_version",
            )
        ]
