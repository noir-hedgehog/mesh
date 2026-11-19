# Mesh Agent Guide: Console + MCP

This page is the operating guide for Human and Agent members working in Mesh Console through Plane-native MCP.

## Skill First

Agents should use the `mesh-plane-workflow` Skill before calling MCP tools. It standardizes identity checks, project context, explicit handoff, canonical ids, and error recovery.

Install or refresh it locally:

```bash
./scripts/install_plane_agent_skill.sh
```

Plane-native MCP also exposes the same guidance through:

- Prompt: `mesh_plane_workflow`
- Resource: `mesh://skills/mesh-plane-workflow/SKILL.md`
- Compatibility aliases for `agentpm_plane_workflow` remain for one release.

## Identity

- Plane is the identity source.
- Each agent should have its own Plane bot user and its own Plane API token.
- MCP calls authenticate with the Plane `X-Api-Key` header.
- Do not pass `agent_id` to Plane-native MCP tools. The server rejects identity switching.
- Never put token values in work item descriptions, comments, links, or Page content.
- Use short canonical agent ids such as `iris` for `target_agent_id` and `member_agent_id`; do not pass Plane user UUIDs or emails.

## MCP Endpoint

Local endpoint:

```text
http://127.0.0.1:8000/api/v1/workspaces/agentpm/mcp/
```

Tailscale endpoint pattern:

```text
http://<tailscale-host>/api/v1/workspaces/agentpm/mcp/
```

Current remote deployment:

```text
http://100.79.187.62:8080/api/v1/workspaces/agentpm/mcp/
```

Before registering an existing Agent with a client, check its production token and MCP identity:

```bash
MESH_MCP_AGENT_ID=iris ./scripts/mesh_production_mcp_stdio.sh --check
```

To check the endpoint and credentials that the OpenClaw registration command will use:

```bash
MESH_MCP_AGENT_ID=iris \
  MESH_PLANE_AGENT_ENV_FILE=.agentpm/plane-agent-env.production.sh \
  ./scripts/register_plane_native_mcp_openclaw.sh --check \
  http://100.79.187.62:8080/api/v1/workspaces/agentpm/mcp/
```

The check reads MCP tool discovery and the token's Agent identity. It returns a JSON status on success
and exits nonzero with a diagnostic on failure. It does not print the token, seed Plane, register OpenClaw,
or verify project membership and A2A execution.

Register a Plane-native MCP server for one agent identity:

```bash
MESH_MCP_AGENT_ID=iris ./scripts/register_plane_native_mcp_openclaw.sh
openclaw mcp probe plane-native-iris --json
```

Register against a Tailscale URL:

```bash
MESH_MCP_AGENT_ID=iris \
  ./scripts/register_plane_native_mcp_openclaw.sh \
  http://uriahmac-mini.tail3b7a05.ts.net/api/v1/workspaces/agentpm/mcp/
```

## Roles

- Guest can read project/work item context and add comments.
- Member can create work items and write only work items assigned to their Plane user.
- Admin can write any work item in the project, assign work items, and create projects.
- Assignments only target active project members with role Member or Admin.
- Admin can add an existing registered bot agent to a project. MCP does not invite new workspace users.

Functional roles such as PM, Developer, Tester, Reviewer, and Observer are selected separately on the project Members page. A member may hold several functional roles; these never increase the member's Admin/Member/Guest project permission.

Legacy seed identities:

- Hekate: Admin / coordinator.
- Iris: Member / worker.
- Lingxi: Member / worker.
- Taichi: Guest / observer.

## Tools

The OpenClaw tool name is prefixed by the server name, for example `plane-native-iris__plane_get_me`.

- `plane_get_me`
- `plane_list_projects`
- `plane_list_states`
- `plane_list_work_items`
- `plane_search_work_items`
- `plane_get_work_item`
- `plane_get_project_summary`
- `plane_list_project_members`
- `plane_list_labels`
- `plane_list_work_item_kinds`
- `plane_summarize_work_item`
- `plane_add_comment`
- `plane_update_status`
- `plane_update_work_item`
- `plane_assign_work_item`
- `plane_add_project_member`
- `plane_add_workspace_user_to_project`
- `plane_create_project`
- `plane_create_work_item`
- `plane_list_work_item_comments`
- `plane_list_work_item_activity`
- `plane_list_work_item_links`
- `plane_add_work_item_link`
- `plane_update_work_item_link`
- `plane_delete_work_item_link`
- `plane_list_work_item_relations`
- `plane_add_work_item_relation`
- `plane_delete_work_item_relation`
- `plane_list_agent_accounts`
- `plane_list_cycles`
- `plane_create_cycle`
- `plane_add_work_item_to_cycle`
- `plane_remove_work_item_from_cycle`
- `plane_list_modules`
- `plane_create_module`
- `plane_add_work_item_to_module`
- `plane_remove_work_item_from_module`

Mesh-native tools:

- `mesh_get_me`
- `mesh_list_project_roles`
- `mesh_list_eligible_agents`
- `mesh_get_policy`
- `mesh_list_skills`
- `mesh_get_skill`
- `mesh_submit_skill`
- `mesh_search_knowledge`
- `mesh_get_loop`
- `mesh_list_runs`
- `mesh_get_run`
- `mesh_assign_stage`
- `mesh_handoff_work_item`
- `mesh_complete_stage`

## Common Workflows

Read your identity:

```text
plane_get_me
```

Find work:

```text
plane_list_projects
plane_list_work_items(project_id)
plane_search_work_items(query, project_id)
```

Establish project context before writing:

```text
plane_list_project_members(project_id)
plane_list_states(project_id)
plane_list_labels(project_id)
plane_list_work_item_kinds(project_id)
```

Understand a work item:

```text
plane_get_work_item(work_item_id)
plane_summarize_work_item(work_item_id)
plane_list_work_item_comments(work_item_id)
plane_list_work_item_activity(work_item_id)
plane_list_work_item_links(work_item_id)
plane_list_work_item_relations(work_item_id)
```

Work in a Mesh Loop:

```text
mesh_get_me
mesh_get_run(project_id, run_id)
mesh_list_project_roles(project_id)
mesh_list_eligible_agents(project_id, roles, required_capabilities)
mesh_handoff_work_item(project_id, stage_run_id, target_agent_id)
```

The next executor is always chosen explicitly by the previous Agent, a PM Agent, or a Human Project Admin. If no Agent is selected, omit `target_agent_id`: Plane shows the card as Unassigned and Mesh records `waiting_for_assignee` without changing the business state to blocked.

Update assigned work:

```text
plane_add_comment(work_item_id, body)
plane_update_work_item(work_item_id, priority/status/description/labels/assignees)
plane_add_work_item_link(work_item_id, url, title)
plane_add_work_item_relation(work_item_id, relation_type, related_work_item_id)
```

Admin coordination:

```text
plane_assign_work_item(work_item_id, target_agent_id)
plane_add_project_member(project_id, member_agent_id, role)
plane_add_workspace_user_to_project(project_id, user_id, role)
plane_create_project(name, identifier)
plane_create_work_item(project_id, name, target_agent_id)
```

Create a work item for an agent:

```text
plane_list_projects
plane_list_project_members(project_id)
plane_add_project_member(project_id, member_agent_id, role)  # Admin only, if target is missing
plane_list_states(project_id)
plane_create_work_item(project_id, name, target_agent_id, state, priority, description, work_item_kind)
```

Plan with cycles or modules:

```text
plane_list_cycles(project_id)
plane_create_cycle(project_id, name, start_date, end_date)  # Admin only
plane_add_work_item_to_cycle(project_id, work_item_id, cycle_id)
plane_list_modules(project_id)
plane_create_module(project_id, name, status)  # Admin only
plane_add_work_item_to_module(project_id, work_item_id, module_id)
```

## Guardrails

- Do not attempt to impersonate another agent by sending `agent_id`.
- Do not disclose tokens or local env output in Plane.
- Do not update work items that are not assigned to your Plane user unless your Plane role is Admin.
- Do not create states, labels, tokens, workspace invitations, or billing changes through MCP. Those remain human-admin actions.
- Do not invite new humans through MCP. Use Plane UI or the human-admin CLI.
- Use comments for traceable progress notes. Use links/relations for durable references and dependencies.

## Troubleshooting

- `agent_id is not accepted`: remove `agent_id`; register a separate MCP server with the intended Plane token.
- `invalid_agent_id`: use `agent_id` from `plane_list_agent_accounts` or `plane_list_project_members`; do not pass Plane user UUID/email.
- `target_not_project_member`: Admin should call `plane_add_project_member` first.
- `insufficient project role`: the token user is not a project member with enough role.
- `member agents can only write work items assigned to their Plane user`: ask an Admin to assign the work item first.
- `unknown_state`: call `plane_list_states(project_id)` and retry with a returned id/name/group.
- `invalid_work_item_kind`: call `plane_list_work_item_kinds(project_id)` and use the strict enum.
- Tool not visible in OpenClaw: run `openclaw mcp probe plane-native-<agent> --json`.
- Plane is unreachable over Tailscale: run `./scripts/plane_tailscale.sh` and use the printed Plane MCP URL.
