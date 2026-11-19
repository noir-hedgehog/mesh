/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect, useState } from "react";
// plane imports
import { EUserPermissions, EUserPermissionsLevel } from "@plane/constants";
import type { IUserLite, TProjectMembership } from "@plane/types";
import { renderFormattedDate } from "@plane/utils";
// components
import { MemberEmail } from "@/components/common/member-email";
import { MemberHeaderColumn } from "@/components/project/member-header-column";
import {
  AccountTypeColumn,
  FunctionalRolesColumn,
  MemberTypeColumn,
  NameColumn,
} from "@/components/project/settings/member-columns";
// hooks
import { useMember } from "@/hooks/store/use-member";
import { useUser, useUserPermissions } from "@/hooks/store/user";
import type { IMemberFilters } from "@/store/member/utils";
import projectMemberService, {
  type TMeshFunctionalRole,
} from "@/services/project/project-member.service";

export interface RowData extends Pick<
  TProjectMembership,
  "original_role" | "is_agent" | "functional_roles" | "agent_profile"
> {
  id: string | null;
  member: IUserLite;
}

type TUseProjectColumnsProps = {
  projectId: string;
  workspaceSlug: string;
};

export const useProjectColumns = (props: TUseProjectColumnsProps) => {
  const { projectId, workspaceSlug } = props;
  // states
  const [removeMemberModal, setRemoveMemberModal] = useState<RowData | null>(null);
  const [functionalRoles, setFunctionalRoles] = useState<TMeshFunctionalRole[]>([]);

  // store hooks
  const { data: currentUser } = useUser();
  const { allowPermissions, getProjectRoleByWorkspaceSlugAndProjectId } = useUserPermissions();
  const {
    project: {
      filters: { getFilters, updateFilters },
    },
  } = useMember();
  // derived values
  const isAdmin = allowPermissions(
    [EUserPermissions.ADMIN],
    EUserPermissionsLevel.PROJECT,
    workspaceSlug.toString(),
    projectId.toString(),
  );
  const currentProjectRole =
    getProjectRoleByWorkspaceSlugAndProjectId(workspaceSlug.toString(), projectId.toString()) ??
    EUserPermissions.GUEST;

  const displayFilters = getFilters(projectId);

  useEffect(() => {
    void projectMemberService
      .fetchMeshFunctionalRoles(workspaceSlug, projectId)
      .then(setFunctionalRoles)
      .catch(() => setFunctionalRoles([]));
  }, [projectId, workspaceSlug]);

  // handlers
  const handleDisplayFilterUpdate = (filters: Partial<IMemberFilters>) => {
    updateFilters(projectId, filters);
  };

  const columns = [
    {
      key: "Full Name",
      content: "Full name",
      thClassName: "text-left",
      thRender: () => (
        <MemberHeaderColumn
          property="full_name"
          displayFilters={displayFilters}
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
      key: "Display Name",
      content: "Display name",
      thRender: () => (
        <MemberHeaderColumn
          property="display_name"
          displayFilters={displayFilters}
          handleDisplayFilterUpdate={handleDisplayFilterUpdate}
        />
      ),
      tdRender: (rowData: RowData) => <div className="w-32">{rowData.member.display_name}</div>,
    },
    {
      key: "Email",
      content: "Email",
      thRender: () => (
        <MemberHeaderColumn
          property="email"
          displayFilters={displayFilters}
          handleDisplayFilterUpdate={handleDisplayFilterUpdate}
        />
      ),
      tdRender: (rowData: RowData) => (
        <MemberEmail
          email={rowData.member.email}
          isAgent={rowData.is_agent ?? rowData.member.is_bot ?? false}
          className="text-secondary"
        />
      ),
    },
    {
      key: "Member Type",
      content: "Member type",
      tdRender: (rowData: RowData) => <MemberTypeColumn rowData={rowData} />,
    },
    {
      key: "Project Role",
      content: "Project role",
      thRender: () => (
        <MemberHeaderColumn
          property="role"
          displayFilters={displayFilters}
          handleDisplayFilterUpdate={handleDisplayFilterUpdate}
        />
      ),
      tdRender: (rowData: RowData) => (
        <AccountTypeColumn
          rowData={rowData}
          currentProjectRole={currentProjectRole}
          projectId={projectId}
          workspaceSlug={workspaceSlug}
        />
      ),
    },
    {
      key: "Functional Roles",
      content: "Functional roles",
      tdRender: (rowData: RowData) => (
        <FunctionalRolesColumn
          availableRoles={functionalRoles}
          isAdmin={isAdmin}
          projectId={projectId}
          rowData={rowData}
          workspaceSlug={workspaceSlug}
        />
      ),
    },
    {
      key: "Joining Date",
      content: "Joining date",
      thRender: () => (
        <MemberHeaderColumn
          property="joining_date"
          displayFilters={displayFilters}
          handleDisplayFilterUpdate={handleDisplayFilterUpdate}
        />
      ),
      tdRender: (rowData: RowData) => (
        <div>{renderFormattedDate(rowData?.member?.joining_date)}</div>
      ),
    },
  ];
  return {
    columns,
    removeMemberModal,
    setRemoveMemberModal,
    displayFilters,
    handleDisplayFilterUpdate,
  };
};
