/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useState } from "react";
import { useParams } from "next/navigation";
import { EUserPermissions, EUserPermissionsLevel, LOGIN_MEDIUM_LABELS } from "@plane/constants";
import { useTranslation } from "@plane/i18n";
import { Button } from "@plane/propel/button";
import { TOAST_TYPE, setToast } from "@plane/propel/toast";
import { renderFormattedDate } from "@plane/utils";
import { MemberHeaderColumn } from "@/components/project/member-header-column";
import { MemberEmail } from "@/components/common/member-email";
import type { RowData } from "@/components/workspace/settings/member-columns";
import { AccountTypeColumn, NameColumn } from "@/components/workspace/settings/member-columns";
import { useMember } from "@/hooks/store/use-member";
import { useUser, useUserPermissions } from "@/hooks/store/user";
import type { IMemberFilters } from "@/store/member/utils";
import { WorkspaceService } from "@/services/workspace.service";

const workspaceService = new WorkspaceService();

export const useMemberColumns = () => {
  // states
  const [removeMemberModal, setRemoveMemberModal] = useState<RowData | null>(null);
  const [tokenMemberModal, setTokenMemberModal] = useState<RowData | null>(null);

  const { workspaceSlug } = useParams();

  const { data: currentUser } = useUser();
  const { allowPermissions } = useUserPermissions();
  const {
    workspace: {
      fetchWorkspaceMembers,
      filtersStore: { filters, updateFilters },
    },
  } = useMember();
  const { t } = useTranslation();

  // derived values
  const isAdmin = allowPermissions([EUserPermissions.ADMIN], EUserPermissionsLevel.WORKSPACE);

  const isSuspended = (rowData: RowData) => rowData.is_active === false;

  // handlers
  const handleDisplayFilterUpdate = (filterUpdates: Partial<IMemberFilters>) => {
    updateFilters(filterUpdates);
  };

  const columns = [
    {
      key: "Full name",
      content: t("workspace_settings.settings.members.details.full_name"),
      thClassName: "text-left",
      thRender: () => (
        <MemberHeaderColumn
          property="full_name"
          displayFilters={filters}
          handleDisplayFilterUpdate={handleDisplayFilterUpdate}
        />
      ),
      tdRender: (rowData: RowData) => (
        <NameColumn
          rowData={rowData}
          workspaceSlug={workspaceSlug}
          isAdmin={isAdmin}
          currentUser={currentUser}
          setRemoveMemberModal={setRemoveMemberModal}
        />
      ),
    },

    {
      key: "Display name",
      content: t("workspace_settings.settings.members.details.display_name"),
      tdRender: (rowData: RowData) => (
        <div className={`w-32 ${isSuspended(rowData) ? "text-placeholder" : ""}`}>{rowData.member.display_name}</div>
      ),
      thRender: () => (
        <MemberHeaderColumn
          property="display_name"
          displayFilters={filters}
          handleDisplayFilterUpdate={handleDisplayFilterUpdate}
        />
      ),
    },

    {
      key: "Email address",
      content: t("workspace_settings.settings.members.details.email_address"),
      tdRender: (rowData: RowData) => (
        <MemberEmail
          email={rowData.member.email}
          isAgent={rowData.member.is_bot ?? false}
          className={isSuspended(rowData) ? "text-placeholder" : undefined}
        />
      ),
      thRender: () => (
        <MemberHeaderColumn
          property="email"
          displayFilters={filters}
          handleDisplayFilterUpdate={handleDisplayFilterUpdate}
        />
      ),
    },

    {
      key: "Workspace role",
      content: "Workspace role",
      thRender: () => (
        <MemberHeaderColumn
          property="role"
          displayFilters={filters}
          handleDisplayFilterUpdate={handleDisplayFilterUpdate}
        />
      ),
      tdRender: (rowData: RowData) => <AccountTypeColumn rowData={rowData} workspaceSlug={workspaceSlug} />,
    },

    {
      key: "Account type",
      content: "Account type",
      tdRender: (rowData: RowData) => {
        if (isSuspended(rowData)) return null;
        return (
          <span className="inline-flex w-20 rounded-sm bg-surface-2 px-2 py-0.5 text-11 font-medium text-secondary">
            {rowData.member.is_bot ? "Agent" : "Human"}
          </span>
        );
      },
    },

    {
      key: "Tokens",
      content: "Tokens",
      tdRender: (rowData: RowData) => {
        if (!rowData.member.is_bot || !isAdmin) return null;
        const slug = workspaceSlug?.toString();
        const toggleAgent = async () => {
          if (!slug) return;
          try {
            await workspaceService.updateWorkspaceAgent(slug, rowData.id, { is_active: !rowData.is_active });
            await fetchWorkspaceMembers(slug);
            setToast({
              type: TOAST_TYPE.SUCCESS,
              title: rowData.is_active ? "Agent disabled" : "Agent enabled",
              message: `${rowData.member.display_name} access was updated.`,
            });
          } catch (error) {
            setToast({
              type: TOAST_TYPE.ERROR,
              title: "Agent update failed",
              message: (error as { error?: string })?.error || "Could not update the Agent.",
            });
          }
        };
        return (
          <div className="flex items-center gap-1">
            <Button variant="secondary" size="sm" onClick={() => setTokenMemberModal(rowData)}>Tokens</Button>
            <Button variant="secondary" size="sm" onClick={() => void toggleAgent()}>{rowData.is_active ? "Disable" : "Enable"}</Button>
          </div>
        );
      },
    },

    {
      key: "Authentication",
      content: t("workspace_settings.settings.members.details.authentication"),
      tdRender: (rowData: RowData) => {
        if (isSuspended(rowData)) return null;
        const loginMedium = (rowData.member as typeof rowData.member & { last_login_medium?: string }).last_login_medium;
        if (!loginMedium) return null;
        return <div>{LOGIN_MEDIUM_LABELS[loginMedium as keyof typeof LOGIN_MEDIUM_LABELS]}</div>;
      },
    },

    {
      key: "Joining date",
      content: t("workspace_settings.settings.members.details.joining_date"),
      tdRender: (rowData: RowData) =>
        isSuspended(rowData) ? null : <div>{renderFormattedDate(rowData?.member?.joining_date)}</div>,
      thRender: () => (
        <MemberHeaderColumn
          property="joining_date"
          displayFilters={filters}
          handleDisplayFilterUpdate={handleDisplayFilterUpdate}
        />
      ),
    },
  ];
  return { columns, workspaceSlug, removeMemberModal, setRemoveMemberModal, tokenMemberModal, setTokenMemberModal };
};
