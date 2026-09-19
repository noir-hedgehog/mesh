# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Django imports
from django.db import transaction
from django.db.models import Count, Q, OuterRef, Subquery, IntegerField
from django.utils import timezone
from django.db.models.functions import Coalesce

# Third party modules
from uuid import uuid4

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from plane.app.permissions import WorkspaceEntityPermission, allow_permission, ROLE

# Module imports
from plane.app.serializers import (
    APITokenReadSerializer,
    APITokenSerializer,
    ProjectMemberRoleSerializer,
    WorkspaceMemberAdminSerializer,
    WorkspaceMemberMeSerializer,
    WorkSpaceMemberSerializer,
)
from plane.app.views.base import BaseAPIView
from plane.db.models import (
    APIToken,
    AgentExecutionProfile,
    AgentProfile,
    AgentRegistrationApplication,
    DraftIssue,
    Project,
    ProjectMember,
    User,
    Workspace,
    WorkspaceMember,
)
from plane.utils.cache import invalidate_cache
from plane.mesh.agent_cards import sync_agent_card

from .. import BaseViewSet


class WorkSpaceMemberViewSet(BaseViewSet):
    serializer_class = WorkspaceMemberAdminSerializer
    model = WorkspaceMember

    search_fields = ["member__display_name", "member__first_name"]
    use_read_replica = True

    def get_queryset(self):
        return self.filter_queryset(
            super()
            .get_queryset()
            .filter(workspace__slug=self.kwargs.get("slug"))
            .select_related("member", "member__avatar_asset")
        )

    @allow_permission(allowed_roles=[ROLE.ADMIN, ROLE.MEMBER, ROLE.GUEST], level="WORKSPACE")
    def list(self, request, slug):
        workspace_member = WorkspaceMember.objects.get(member=request.user, workspace__slug=slug, is_active=True)

        # Get all active workspace members
        workspace_members = self.get_queryset()
        if workspace_member.role > 5:
            serializer = WorkspaceMemberAdminSerializer(workspace_members, fields=("id", "member", "role"), many=True)
        else:
            serializer = WorkSpaceMemberSerializer(workspace_members, fields=("id", "member", "role"), many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @allow_permission(allowed_roles=[ROLE.ADMIN, ROLE.MEMBER, ROLE.GUEST], level="WORKSPACE")
    def retrieve(self, request, slug, pk):
        workspace_member = WorkspaceMember.objects.get(member=request.user, workspace__slug=slug, is_active=True)

        try:
            # Get the specific workspace member by pk
            member = self.get_queryset().get(pk=pk)
        except WorkspaceMember.DoesNotExist:
            return Response(
                {"error": "Workspace member not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        if workspace_member.role > ROLE.GUEST.value:
            serializer = WorkspaceMemberAdminSerializer(member, fields=("id", "member", "role"))
        else:
            serializer = WorkSpaceMemberSerializer(member, fields=("id", "member", "role"))
        return Response(serializer.data, status=status.HTTP_200_OK)

    @allow_permission(allowed_roles=[ROLE.ADMIN], level="WORKSPACE")
    def partial_update(self, request, slug, pk):
        workspace_member = WorkspaceMember.objects.get(
            pk=pk, workspace__slug=slug, member__is_bot=False, is_active=True
        )
        if request.user.id == workspace_member.member_id:
            return Response(
                {"error": "You cannot update your own role"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # If a user is moved to a guest role he can't have any other role in projects
        if "role" in request.data and int(request.data.get("role")) == 5:
            ProjectMember.objects.filter(workspace__slug=slug, member_id=workspace_member.member_id).update(role=5)

        serializer = WorkSpaceMemberSerializer(workspace_member, data=request.data, partial=True)

        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @allow_permission(allowed_roles=[ROLE.ADMIN], level="WORKSPACE")
    def destroy(self, request, slug, pk):
        # Check the user role who is deleting the user
        workspace_member = WorkspaceMember.objects.get(
            workspace__slug=slug, pk=pk, member__is_bot=False, is_active=True
        )

        # check requesting user role
        requesting_workspace_member = WorkspaceMember.objects.get(
            workspace__slug=slug, member=request.user, is_active=True
        )

        if str(workspace_member.id) == str(requesting_workspace_member.id):
            return Response(
                {"error": "You cannot remove yourself from the workspace. Please use leave workspace"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if requesting_workspace_member.role < workspace_member.role:
            return Response(
                {"error": "You cannot remove a user having role higher than you"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if (
            Project.objects.annotate(
                total_members=Count("project_projectmember"),
                member_with_role=Count(
                    "project_projectmember",
                    filter=Q(
                        project_projectmember__member_id=workspace_member.id,
                        project_projectmember__role=20,
                    ),
                ),
            )
            .filter(total_members=1, member_with_role=1, workspace__slug=slug)
            .exists()
        ):
            return Response(
                {
                    "error": "User is a part of some projects where they are the only admin, they should either leave that project or promote another user to admin."  # noqa: E501
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Deactivate the users from the projects where the user is part of
        _ = ProjectMember.objects.filter(
            workspace__slug=slug, member_id=workspace_member.member_id, is_active=True
        ).update(is_active=False, updated_at=timezone.now())

        workspace_member.is_active = False
        workspace_member.save()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @invalidate_cache(
        path="/api/workspaces/:slug/members/",
        url_params=True,
        user=False,
        multiple=True,
    )
    @invalidate_cache(path="/api/users/me/settings/")
    @invalidate_cache(path="api/users/me/workspaces/", user=False, multiple=True)
    @allow_permission(allowed_roles=[ROLE.ADMIN, ROLE.MEMBER, ROLE.GUEST], level="WORKSPACE")
    def leave(self, request, slug):
        workspace_member = WorkspaceMember.objects.get(workspace__slug=slug, member=request.user, is_active=True)

        # Check if the leaving user is the only admin of the workspace
        if (
            workspace_member.role == 20
            and not WorkspaceMember.objects.filter(workspace__slug=slug, role=20, is_active=True).count() > 1
        ):
            return Response(
                {
                    "error": "You cannot leave the workspace as you are the only admin of the workspace you will have to either delete the workspace or promote another user to admin."  # noqa: E501
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if (
            Project.objects.annotate(
                total_members=Count("project_projectmember"),
                member_with_role=Count(
                    "project_projectmember",
                    filter=Q(
                        project_projectmember__member_id=request.user.id,
                        project_projectmember__role=20,
                    ),
                ),
            )
            .filter(total_members=1, member_with_role=1, workspace__slug=slug)
            .exists()
        ):
            return Response(
                {
                    "error": "You are a part of some projects where you are the only admin, you should either leave the project or promote another user to admin."  # noqa: E501
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # # Deactivate the users from the projects where the user is part of
        _ = ProjectMember.objects.filter(
            workspace__slug=slug, member_id=workspace_member.member_id, is_active=True
        ).update(is_active=False, updated_at=timezone.now())

        # # Deactivate the user
        workspace_member.is_active = False
        workspace_member.save()
        return Response(status=status.HTTP_204_NO_CONTENT)


class WorkspaceMemberUserViewsEndpoint(BaseAPIView):
    def post(self, request, slug):
        workspace_member = WorkspaceMember.objects.get(workspace__slug=slug, member=request.user, is_active=True)
        workspace_member.view_props = request.data.get("view_props", {})
        workspace_member.save()

        return Response(status=status.HTTP_204_NO_CONTENT)


class WorkspaceMemberAPITokenEndpoint(BaseAPIView):
    @allow_permission(allowed_roles=[ROLE.ADMIN], level="WORKSPACE")
    def get(self, request, slug, member_id, pk=None):
        workspace_member = WorkspaceMember.objects.get(
            workspace__slug=slug,
            member_id=member_id,
            member__is_bot=True,
            is_active=True,
        )
        queryset = APIToken.objects.filter(
            workspace=workspace_member.workspace,
            user=workspace_member.member,
            is_service=True,
        )
        if pk is None:
            serializer = APITokenReadSerializer(queryset, many=True)
            return Response(serializer.data, status=status.HTTP_200_OK)

        serializer = APITokenReadSerializer(queryset.get(pk=pk))
        return Response(serializer.data, status=status.HTTP_200_OK)

    @allow_permission(allowed_roles=[ROLE.ADMIN], level="WORKSPACE")
    def post(self, request, slug, member_id):
        workspace_member = WorkspaceMember.objects.get(
            workspace__slug=slug,
            member_id=member_id,
            member__is_bot=True,
            is_active=True,
        )
        api_token = APIToken.objects.create(
            label=request.data.get("label", str(uuid4().hex)),
            description=request.data.get("description", ""),
            expired_at=request.data.get("expired_at", None),
            user=workspace_member.member,
            user_type=1,
            workspace=workspace_member.workspace,
            is_service=True,
            allowed_rate_limit=request.data.get("allowed_rate_limit", "1000/min"),
        )
        serializer = APITokenSerializer(api_token)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @allow_permission(allowed_roles=[ROLE.ADMIN], level="WORKSPACE")
    def delete(self, request, slug, member_id, pk):
        workspace_member = WorkspaceMember.objects.get(
            workspace__slug=slug,
            member_id=member_id,
            member__is_bot=True,
            is_active=True,
        )
        APIToken.objects.get(
            pk=pk,
            workspace=workspace_member.workspace,
            user=workspace_member.member,
            is_service=True,
        ).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


def _agent_id(value):
    import re

    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,62}", normalized):
        raise ValueError("agent_id must contain 2-63 lowercase letters, numbers, hyphens, or underscores")
    return normalized


def _agent_role(value, default=15):
    roles = {"admin": 20, "member": 15, "guest": 5, 20: 20, 15: 15, 5: 5, "20": 20, "15": 15, "5": 5}
    if value in (None, ""):
        return default
    if value not in roles:
        raise ValueError("role must be admin, member, or guest")
    return roles[value]


def _serialize_agent_application(application):
    return {
        "id": str(application.id),
        "agent_id": application.agent_id,
        "display_name": application.display_name,
        "email": application.email,
        "requested_role": application.requested_role,
        "reason": application.reason,
        "agent_type": application.agent_type,
        "runtime_provider": application.runtime_provider,
        "endpoint_url": application.endpoint_url,
        "agent_card": application.agent_card,
        "capability_claims": application.capability_claims,
        "boundaries": application.boundaries,
        "status": application.status,
        "source": application.source,
        "project_id": str(application.project_id) if application.project_id else None,
        "review_note": application.review_note,
        "reviewed_by": str(application.reviewed_by_id) if application.reviewed_by_id else None,
        "created_at": application.created_at,
        "reviewed_at": application.reviewed_at,
    }


def _create_agent_account(*, workspace, actor, payload):
    agent_id = _agent_id(payload.get("agent_id"))
    display_name = str(payload.get("display_name") or agent_id).strip()
    email = str(payload.get("email") or f"agent-{agent_id}@agentpm.local").strip().lower()
    workspace_role = _agent_role(payload.get("workspace_role") or payload.get("requested_role"), 15)
    project_role = _agent_role(payload.get("project_role") or payload.get("requested_role"), 15)
    project_id = payload.get("project_id")

    with transaction.atomic():
        user, _ = User.objects.get_or_create(
            email=email,
            defaults={"username": f"agent-{agent_id}", "display_name": display_name},
        )
        if not user.is_bot and WorkspaceMember.objects.filter(member=user).exists():
            raise ValueError("email already belongs to a human Plane user")
        user.username = f"agent-{agent_id}"
        user.display_name = display_name
        user.first_name = display_name
        user.last_name = "Agent"
        user.is_active = True
        user.is_email_verified = True
        user.is_email_valid = True
        user.is_bot = True
        user.bot_type = "WORKSPACE_SEED"
        user.set_unusable_password()
        user.save()

        workspace_member, _ = WorkspaceMember.objects.get_or_create(
            workspace=workspace,
            member=user,
            defaults={"role": workspace_role, "is_active": True},
        )
        workspace_member.role = workspace_role
        workspace_member.is_active = True
        workspace_member.save()

        agent_profile, _ = AgentProfile.objects.update_or_create(
            workspace=workspace,
            agent_id=agent_id,
            defaults={
                "user": user,
                "owner": actor,
                "agent_type": str(payload.get("agent_type") or "autonomous"),
                "runtime_provider": str(payload.get("runtime_provider") or "custom"),
                "endpoint_url": str(payload.get("endpoint_url") or ""),
                "status": AgentProfile.Status.ACTIVE,
                "trust_level": str(payload.get("trust_level") or AgentProfile.TrustLevel.VERIFIED),
                "agent_card": dict(payload.get("agent_card") or {}),
                "capability_claims": list(payload.get("capability_claims") or []),
                "boundaries": dict(payload.get("boundaries") or {}),
            },
        )
        default_model = str(payload.get("default_model") or "").strip()
        default_execution = None
        if default_model:
            provider = str(payload.get("execution_provider") or agent_profile.runtime_provider).strip()
            configuration_version = int(payload.get("configuration_version") or 1)
            default_execution, _ = AgentExecutionProfile.objects.update_or_create(
                workspace=workspace,
                agent=agent_profile,
                provider=provider,
                model=default_model,
                configuration_version=configuration_version,
                defaults={
                    "secret_reference": str(payload.get("secret_reference") or "").strip(),
                    "settings": dict(payload.get("execution_settings") or {}),
                    "is_default": True,
                    "is_active": True,
                },
            )

        if project_id:
            project = Project.objects.get(workspace=workspace, id=project_id)
            project_member, _ = ProjectMember.objects.get_or_create(
                workspace=workspace,
                project=project,
                member=user,
                defaults={"role": project_role, "is_active": True},
            )
            project_member.role = project_role
            project_member.is_active = True
            project_member.save()

        token = None
        if payload.get("create_token", True):
            token = APIToken.objects.create(
                label=f"Mesh {agent_id} MCP",
                description=f"Mesh MCP token for {display_name}",
                user=user,
                user_type=1,
                workspace=workspace,
                is_service=True,
                allowed_rate_limit="1000/min",
            )

    return {
        "workspace_member_id": str(workspace_member.id),
        "user": {
            "id": str(user.id),
            "agent_id": agent_id,
            "display_name": display_name,
            "email": email,
            "is_bot": True,
        },
        "workspace_role": workspace_member.role,
        "agent_profile": {
            "id": str(agent_profile.id),
            "agent_type": agent_profile.agent_type,
            "runtime_provider": agent_profile.runtime_provider,
            "status": agent_profile.status,
            "trust_level": agent_profile.trust_level,
            "capability_claims": agent_profile.capability_claims,
            "boundaries": agent_profile.boundaries,
        },
        "default_execution": (
            {
                "id": str(default_execution.id),
                "provider": default_execution.provider,
                "model": default_execution.model,
                "configuration_version": default_execution.configuration_version,
                "is_active": default_execution.is_active,
            }
            if default_execution
            else None
        ),
        "token": APITokenSerializer(token).data if token else None,
    }


def _serialize_workspace_agent(member):
    profile = AgentProfile.objects.filter(
        workspace=member.workspace, user=member.member, deleted_at__isnull=True
    ).first()
    execution = (
        profile.execution_profiles.filter(is_default=True, is_active=True, deleted_at__isnull=True).first()
        if profile
        else None
    )
    return {
        "workspace_member_id": str(member.id),
        "user_id": str(member.member_id),
        "agent_id": profile.agent_id if profile else _agent_id((member.member.username or "agent-unknown").removeprefix("agent-")),
        "display_name": member.member.display_name,
        "email": member.member.email,
        "role": member.role,
        "is_active": member.is_active and member.member.is_active,
        "agent_profile": (
            {
                "id": str(profile.id),
                "agent_type": profile.agent_type,
                "runtime_provider": profile.runtime_provider,
                "endpoint_url": profile.endpoint_url,
                "status": profile.status,
                "trust_level": profile.trust_level,
                "agent_card": profile.agent_card,
                "capability_claims": profile.capability_claims,
                "boundaries": profile.boundaries,
                "last_seen_at": profile.last_seen_at,
            }
            if profile
            else None
        ),
        "default_execution": (
            {
                "id": str(execution.id),
                "provider": execution.provider,
                "model": execution.model,
                "configuration_version": execution.configuration_version,
                "has_secret_reference": bool(execution.secret_reference),
                "settings": execution.settings,
                "is_active": execution.is_active,
            }
            if execution
            else None
        ),
    }


class AgentRegistrationRequestEndpoint(BaseAPIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, slug):
        try:
            workspace = Workspace.objects.get(slug=slug)
            agent_id = _agent_id(request.data.get("agent_id"))
            requested_role = str(request.data.get("requested_role") or "member").lower()
            _agent_role(requested_role)
            existing = AgentRegistrationApplication.objects.filter(
                workspace=workspace,
                agent_id=agent_id,
                status=AgentRegistrationApplication.Status.PENDING,
            ).first()
            if existing:
                return Response(_serialize_agent_application(existing), status=status.HTTP_200_OK)
            application = AgentRegistrationApplication.objects.create(
                workspace=workspace,
                project_id=request.data.get("project_id") or None,
                agent_id=agent_id,
                display_name=str(request.data.get("display_name") or agent_id).strip(),
                email=str(request.data.get("email") or f"agent-{agent_id}@agentpm.local").strip().lower(),
                requested_role=requested_role,
                reason=str(request.data.get("reason") or "").strip(),
                agent_type=str(request.data.get("agent_type") or "autonomous").strip(),
                runtime_provider=str(request.data.get("runtime_provider") or "custom").strip(),
                endpoint_url=str(request.data.get("endpoint_url") or "").strip(),
                agent_card=dict(request.data.get("agent_card") or {}),
                capability_claims=list(request.data.get("capability_claims") or []),
                boundaries=dict(request.data.get("boundaries") or {}),
            )
            return Response(_serialize_agent_application(application), status=status.HTTP_201_CREATED)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class WorkspaceAgentEndpoint(BaseAPIView):
    @allow_permission(allowed_roles=[ROLE.ADMIN], level="WORKSPACE")
    def get(self, request, slug):
        members = (
            WorkspaceMember.objects.filter(
                workspace__slug=slug,
                member__is_bot=True,
            )
            .select_related("member")
            .order_by("member__display_name")
        )
        return Response([_serialize_workspace_agent(member) for member in members], status=status.HTTP_200_OK)

    @allow_permission(allowed_roles=[ROLE.ADMIN], level="WORKSPACE")
    def post(self, request, slug):
        try:
            workspace = Workspace.objects.get(slug=slug)
            return Response(
                _create_agent_account(workspace=workspace, actor=request.user, payload=request.data),
                status=status.HTTP_201_CREATED,
            )
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class WorkspaceAgentDetailEndpoint(BaseAPIView):
    @allow_permission(allowed_roles=[ROLE.ADMIN], level="WORKSPACE")
    def get(self, request, slug, member_id):
        member = WorkspaceMember.objects.select_related("member", "workspace").get(
            id=member_id, workspace__slug=slug, member__is_bot=True
        )
        return Response(_serialize_workspace_agent(member))

    @allow_permission(allowed_roles=[ROLE.ADMIN], level="WORKSPACE")
    def patch(self, request, slug, member_id):
        try:
            member = WorkspaceMember.objects.select_related("member").get(
                id=member_id, workspace__slug=slug, member__is_bot=True
            )
            if "role" in request.data:
                member.role = _agent_role(request.data.get("role"))
            if "is_active" in request.data:
                active = bool(request.data.get("is_active"))
                member.is_active = active
                member.member.is_active = active
                member.member.save(update_fields=["is_active", "updated_at"])
                ProjectMember.objects.filter(workspace=member.workspace, member=member.member).update(is_active=active)
            member.save()
            profile = AgentProfile.objects.filter(workspace=member.workspace, user=member.member).first()
            if profile:
                for field in ("agent_type", "runtime_provider", "endpoint_url", "status", "trust_level"):
                    if field in request.data:
                        if field == "endpoint_url" and profile.endpoint_url != request.data.get(field):
                            profile.agent_card = {"available": False, "sync_pending": True}
                        setattr(profile, field, str(request.data.get(field) or ""))
                if "capability_claims" in request.data:
                    profile.capability_claims = list(request.data.get("capability_claims") or [])
                if "boundaries" in request.data:
                    profile.boundaries = dict(request.data.get("boundaries") or {})
                profile.save()
                execution_data = request.data.get("default_execution")
                if isinstance(execution_data, dict) and execution_data.get("model"):
                    provider = str(execution_data.get("provider") or profile.runtime_provider)
                    version = int(execution_data.get("configuration_version") or 1)
                    execution_defaults = {
                        "settings": dict(execution_data.get("settings") or {}),
                        "is_default": True,
                        "is_active": bool(execution_data.get("is_active", True)),
                    }
                    if "secret_reference" in execution_data:
                        execution_defaults["secret_reference"] = str(execution_data.get("secret_reference") or "")
                    AgentExecutionProfile.objects.update_or_create(
                        workspace=member.workspace,
                        agent=profile,
                        provider=provider,
                        model=str(execution_data["model"]),
                        configuration_version=version,
                        defaults=execution_defaults,
                    )
            return Response(_serialize_workspace_agent(member))
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    @allow_permission(allowed_roles=[ROLE.ADMIN], level="WORKSPACE")
    def post(self, request, slug, member_id):
        try:
            member = WorkspaceMember.objects.select_related("member", "workspace").get(
                id=member_id, workspace__slug=slug, member__is_bot=True
            )
            profile = AgentProfile.objects.get(workspace=member.workspace, user=member.member, deleted_at__isnull=True)
            sync_agent_card(profile)
            return Response(_serialize_workspace_agent(member))
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class WorkspaceAgentApplicationEndpoint(BaseAPIView):
    @allow_permission(allowed_roles=[ROLE.ADMIN], level="WORKSPACE")
    def get(self, request, slug, pk=None):
        queryset = AgentRegistrationApplication.objects.filter(workspace__slug=slug)
        if request.query_params.get("status"):
            queryset = queryset.filter(status=request.query_params["status"])
        if pk:
            return Response(_serialize_agent_application(queryset.get(pk=pk)))
        return Response([_serialize_agent_application(item) for item in queryset[:100]])

    @allow_permission(allowed_roles=[ROLE.ADMIN], level="WORKSPACE")
    def patch(self, request, slug, pk):
        application = AgentRegistrationApplication.objects.get(workspace__slug=slug, pk=pk)
        if application.status != AgentRegistrationApplication.Status.PENDING:
            return Response({"error": "application has already been reviewed"}, status=status.HTTP_409_CONFLICT)
        action = str(request.data.get("action") or "").lower()
        if action not in {"approve", "reject"}:
            return Response({"error": "action must be approve or reject"}, status=status.HTTP_400_BAD_REQUEST)
        application.reviewed_by = request.user
        application.review_note = str(request.data.get("review_note") or "")
        application.reviewed_at = timezone.now()
        if action == "reject":
            application.status = AgentRegistrationApplication.Status.REJECTED
            application.save()
            return Response(_serialize_agent_application(application))
        try:
            account = _create_agent_account(
                workspace=application.workspace,
                actor=request.user,
                payload={
                    "agent_id": application.agent_id,
                    "display_name": application.display_name,
                    "email": application.email,
                    "requested_role": request.data.get("role") or application.requested_role,
                    "project_id": request.data.get("project_id") or application.project_id,
                    "project_role": request.data.get("role") or application.requested_role,
                    "agent_type": application.agent_type,
                    "runtime_provider": application.runtime_provider,
                    "endpoint_url": application.endpoint_url,
                    "agent_card": application.agent_card,
                    "capability_claims": application.capability_claims,
                    "boundaries": application.boundaries,
                },
            )
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        application.status = AgentRegistrationApplication.Status.APPROVED
        application.save()
        return Response({"application": _serialize_agent_application(application), "account": account})


class WorkspaceMemberUserEndpoint(BaseAPIView):
    use_read_replica = True

    def get(self, request, slug):
        draft_issue_count = (
            DraftIssue.objects.filter(created_by=request.user, workspace_id=OuterRef("workspace_id"))
            .values("workspace_id")
            .annotate(count=Count("id"))
            .values("count")
        )

        workspace_member = (
            WorkspaceMember.objects.filter(member=request.user, workspace__slug=slug, is_active=True)
            .annotate(draft_issue_count=Coalesce(Subquery(draft_issue_count, output_field=IntegerField()), 0))
            .first()
        )
        serializer = WorkspaceMemberMeSerializer(workspace_member)
        return Response(serializer.data, status=status.HTTP_200_OK)


class WorkspaceProjectMemberEndpoint(BaseAPIView):
    serializer_class = ProjectMemberRoleSerializer
    model = ProjectMember

    permission_classes = [WorkspaceEntityPermission]

    def get(self, request, slug):
        # Fetch all project IDs where the user is involved
        project_ids = (
            ProjectMember.objects.filter(member=request.user, is_active=True)
            .values_list("project_id", flat=True)
            .distinct()
        )

        # Get all the project members in which the user is involved
        project_members = ProjectMember.objects.filter(
            workspace__slug=slug, project_id__in=project_ids, is_active=True
        ).select_related("project", "member", "workspace")
        project_members = ProjectMemberRoleSerializer(project_members, many=True).data

        project_members_dict = dict()

        # Construct a dictionary with project_id as key and project_members as value
        for project_member in project_members:
            project_id = project_member.pop("project")
            if str(project_id) not in project_members_dict:
                project_members_dict[str(project_id)] = []
            project_members_dict[str(project_id)].append(project_member)

        return Response(project_members_dict, status=status.HTTP_200_OK)
