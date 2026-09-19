# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.conf import settings
from django.db import models

from .workspace import WorkspaceBaseModel


class MeshLoopDefinition(WorkspaceBaseModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        SUPERSEDED = "superseded", "Superseded"
        DEPRECATED = "deprecated", "Deprecated"

    slug = models.SlugField(max_length=128)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    version = models.PositiveIntegerField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    source_yaml = models.TextField()
    graph = models.JSONField(default=dict)
    checksum = models.CharField(max_length=64)
    change_note = models.TextField(blank=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="published_mesh_loops",
    )
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "mesh_loop_definitions"
        ordering = ("slug", "-version")
        constraints = [
            models.UniqueConstraint(
                fields=["project", "slug", "version"],
                condition=models.Q(deleted_at__isnull=True),
                name="mesh_loop_definition_unique_version",
            )
        ]


class MeshLoopRun(WorkspaceBaseModel):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        WAITING_FOR_ASSIGNEE = "waiting_for_assignee", "Waiting for assignee"
        AWAITING_APPROVAL = "awaiting_approval", "Awaiting approval"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELED = "canceled", "Canceled"

    work_item = models.ForeignKey("db.Issue", on_delete=models.CASCADE, related_name="mesh_loop_runs")
    definition = models.ForeignKey(MeshLoopDefinition, on_delete=models.PROTECT, related_name="runs")
    definition_version = models.PositiveIntegerField()
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.QUEUED)
    current_node_id = models.CharField(max_length=128, blank=True)
    budget = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "mesh_loop_runs"
        ordering = ("-created_at",)


class MeshStageRun(WorkspaceBaseModel):
    class Status(models.TextChoices):
        WAITING_FOR_ASSIGNEE = "waiting_for_assignee", "Waiting for assignee"
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        AWAITING_APPROVAL = "awaiting_approval", "Awaiting approval"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELED = "canceled", "Canceled"

    loop_run = models.ForeignKey(MeshLoopRun, on_delete=models.CASCADE, related_name="stages")
    node_id = models.CharField(max_length=128)
    objective = models.TextField(blank=True)
    functional_role = models.ForeignKey(
        "db.MeshFunctionalRole", on_delete=models.SET_NULL, null=True, blank=True, related_name="stage_runs"
    )
    assigned_agent = models.ForeignKey(
        "db.AgentProfile", on_delete=models.SET_NULL, null=True, blank=True, related_name="stage_runs"
    )
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.WAITING_FOR_ASSIGNEE)
    required_evidence = models.JSONField(default=list, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "mesh_stage_runs"
        ordering = ("created_at",)


class MeshRunAttempt(WorkspaceBaseModel):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELED = "canceled", "Canceled"

    stage_run = models.ForeignKey(MeshStageRun, on_delete=models.CASCADE, related_name="attempts")
    agent = models.ForeignKey("db.AgentProfile", on_delete=models.PROTECT, related_name="run_attempts")
    execution_profile = models.ForeignKey(
        "db.AgentExecutionProfile", on_delete=models.SET_NULL, null=True, blank=True, related_name="run_attempts"
    )
    provider = models.CharField(max_length=64)
    model = models.CharField(max_length=255)
    configuration_version = models.PositiveIntegerField(default=1)
    provider_run_id = models.CharField(max_length=255, blank=True)
    provider_session_id = models.CharField(max_length=255, blank=True)
    provider_state = models.CharField(max_length=64, blank=True)
    failure_code = models.CharField(max_length=64, blank=True)
    failure_message = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    input_tokens = models.PositiveBigIntegerField(default=0)
    output_tokens = models.PositiveBigIntegerField(default=0)
    cost = models.DecimalField(max_digits=14, decimal_places=6, default=0)
    latency_ms = models.PositiveBigIntegerField(default=0)
    evidence = models.JSONField(default=list, blank=True)
    usage = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    last_polled_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    @property
    def reported_cost(self):
        return str(self.cost) if self.cost or (self.usage or {}).get("_mesh_cost_reported") is True else None

    @property
    def model_provider(self):
        return (self.usage or {}).get("_mesh_model_provider")

    class Meta:
        db_table = "mesh_run_attempts"
        ordering = ("created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_run_id"],
                condition=models.Q(deleted_at__isnull=True) & ~models.Q(provider_run_id=""),
                name="mesh_attempt_unique_provider_run",
            )
        ]


class MeshHandoff(WorkspaceBaseModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ASSIGNED = "assigned", "Assigned"
        DECLINED = "declined", "Declined"
        CANCELED = "canceled", "Canceled"

    loop_run = models.ForeignKey(MeshLoopRun, on_delete=models.CASCADE, related_name="handoffs")
    from_stage = models.ForeignKey(MeshStageRun, on_delete=models.CASCADE, related_name="outgoing_handoffs")
    to_node_id = models.CharField(max_length=128)
    target_role = models.ForeignKey(
        "db.MeshFunctionalRole", on_delete=models.PROTECT, related_name="targeted_handoffs"
    )
    from_agent = models.ForeignKey(
        "db.AgentProfile", on_delete=models.SET_NULL, null=True, related_name="outgoing_handoffs"
    )
    target_agent = models.ForeignKey(
        "db.AgentProfile", on_delete=models.SET_NULL, null=True, blank=True, related_name="incoming_handoffs"
    )
    selected_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="selected_mesh_handoffs",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    reason = models.TextField(blank=True)

    class Meta:
        db_table = "mesh_handoffs"
        ordering = ("-created_at",)


class MeshApproval(WorkspaceBaseModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        CANCELED = "canceled", "Canceled"

    loop_run = models.ForeignKey(MeshLoopRun, on_delete=models.CASCADE, related_name="approvals")
    stage_run = models.ForeignKey(MeshStageRun, on_delete=models.CASCADE, related_name="approvals")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="mesh_approvals"
    )
    decision_note = models.TextField(blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "mesh_approvals"
        ordering = ("-created_at",)


class MeshAuditEvent(WorkspaceBaseModel):
    loop_run = models.ForeignKey(
        MeshLoopRun, on_delete=models.CASCADE, null=True, blank=True, related_name="audit_events"
    )
    work_item = models.ForeignKey(
        "db.Issue", on_delete=models.CASCADE, null=True, blank=True, related_name="mesh_audit_events"
    )
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="mesh_audit_events"
    )
    actor_agent = models.ForeignKey(
        "db.AgentProfile", on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_events"
    )
    event_type = models.CharField(max_length=128)
    payload = models.JSONField(default=dict)
    occurred_at = models.DateTimeField()

    class Meta:
        db_table = "mesh_audit_events"
        ordering = ("occurred_at",)
        indexes = [models.Index(fields=["project", "event_type", "occurred_at"], name="mesh_audit_event_lookup")]
