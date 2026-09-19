---
name: mesh-plane-workflow
version: 0.2.1
description: Use when an agent works with the production Mesh Console through Plane-native MCP, especially to discover project Policy, Skills, Knowledge, members and states; create or update work items; hand off Loop stages; or recover from MCP errors.
---

# Mesh Plane Workflow

Plane-native MCP is a strict workflow facade. Your identity comes from the MCP server token (`X-Api-Key`). Do not pass `agent_id` to Plane-native MCP tools.

## Environment

- Production is the source of truth: `http://100.79.187.62:8080/`.
- The production MCP endpoint is `http://100.79.187.62:8080/api/v1/workspaces/agentpm/mcp/`.
- Use the configured `mesh_production` or compatibility `agentpm_plane` MCP server.
- Do not write to `127.0.0.1` unless the user explicitly asks for local development or testing.
- Never copy Plane API tokens into prompts, Skill content, logs, work items, or comments.

## Required Context Flow

1. Call `plane_get_me` before writing. Confirm the authenticated user, `agent_id`, and workspace role.
2. Call `plane_list_projects`. Choose the target `project_id`; keep passing it explicitly.
3. Call `plane_list_project_members(project_id)`. Use returned `agent_id` values for `target_agent_id` and `member_agent_id`.
4. Call `plane_list_states(project_id)` before passing `state` or `status`.
5. Call `plane_list_work_item_kinds(project_id)` before passing `work_item_kind`.
6. Call `mesh_get_policy(project_id)` before delegating or starting a Loop stage.
7. Call `mesh_list_skills(project_id)` and `mesh_search_knowledge(project_id, query)` when project SOP or context is relevant.
8. For assigned work, call `plane_get_work_item` or `plane_summarize_work_item` before updating.
9. Call `mesh_get_loop(project_id, slug)` before starting or completing a Loop so required roles and Evidence are current.

## Parameter Rules

- `project_id`: from `plane_list_projects`.
- `work_item_id`: from `plane_list_work_items`, `plane_search_work_items`, or `plane_get_work_item`.
- `state` / `status`: state id, name, or group from `plane_list_states`.
- `target_agent_id`: short canonical agent id such as `iris`; never Plane user UUID or email.
- `member_agent_id`: short canonical agent id such as `iris`; never Plane user UUID or email.
- `labels`: ids or names from `plane_list_labels`.
- `work_item_kind`: strict enum from `plane_list_work_item_kinds`; currently `requirement`, `bug`, `task`, or `analysis`.
- `assignees`: existing project member ids/emails/usernames from `plane_list_project_members`.

## Common Workflows

Discover Mesh context before delegating:

1. `mesh_get_me`
2. `mesh_get_policy(project_id)`
3. `mesh_list_project_roles(project_id)`
4. `mesh_list_eligible_agents(project_id, roles, required_capabilities)`
5. `mesh_list_skills(project_id)` and `mesh_get_skill(project_id, skill_slug)`
6. `mesh_search_knowledge(project_id, query)` and preserve returned Page/version/heading citations
7. `mesh_assign_stage(project_id, stage_run_id, target_agent_id)` with an eligible short Agent id

Loop stages define objectives, evidence, roles, and policy boundaries. They do not prescribe which Skill, knowledge query, model, or internal reasoning steps an Agent must use.

Run a Mesh Loop:

1. A PM Agent or Project Admin calls `mesh_get_loop(project_id, slug)` and `mesh_start_loop(project_id, work_item_id, loop_slug)`.
2. The first Stage remains Unassigned until PM/Admin calls `mesh_list_eligible_agents` and `mesh_assign_stage`.
3. The assigned Agent reads the Work Item, latest Policy, relevant published Skills, and Knowledge citations.
4. The assigned Agent performs only the current Stage objective in the Loop worktree.
5. For a Gateway/A2A-assigned Stage, return a JSON Artifact to the Gateway; do not also call `mesh_complete_stage`. For a directly operated Stage, call `mesh_complete_stage(project_id, stage_run_id, outcome, evidence, handoff_target_agent_id)`. `outcome` is exactly `succeeded` or `failed`, never `passed` or `completed`. Include every required Evidence key. Each item must contain string `key`, `kind`, and `title`.
6. Use `uri`, `summary`, and `metadata` for commit/test output and exact Skill version or Knowledge Page/version/heading references.
7. `handoff_target_agent_id` must be an available eligible Agent returned for the next Stage. Omit it to leave the next Stage and Plane card Unassigned.
8. Use `mesh_get_run(project_id, run_id)` to confirm actual Agent, provider/model, A2A state, Evidence, Handoff, or failure details.
9. A PM Agent or Project Admin may call `mesh_cancel_run(project_id, run_id, reason)`; cancellation clears the Work Item assignee and requests provider cancellation.

Never infer or silently select the next Agent. An unavailable or Policy-ineligible target remains Unassigned.

If configured MCP tools are missing or disconnected, stop and report the missing runtime capability. Do not search environment files for credentials or switch to another Agent identity. OpenClaw's `coding` profile needs an explicit `alsoAllow` entry for its own server namespace, such as `plane-native-iris__*`; the Mesh Gateway configures this per task. Tailscale MCP traffic must bypass unrelated system HTTP proxies.

Create work for an agent:

1. `plane_list_projects`
2. `plane_list_project_members(project_id)`
3. If the target agent is missing and you are Admin, call `plane_add_project_member(project_id, member_agent_id, role)`
4. `plane_list_states(project_id)`
5. `plane_list_work_item_kinds(project_id)`
6. `plane_create_work_item(project_id, name, target_agent_id, state, priority, description, work_item_kind)`

Plan with cycles or modules:

1. `plane_list_cycles(project_id)` or `plane_list_modules(project_id)`
2. Admin creates a missing cycle/module with `plane_create_cycle` or `plane_create_module`
3. Add assigned work with `plane_add_work_item_to_cycle` or `plane_add_work_item_to_module`

Add a registered agent to a project:

1. `plane_get_me`
2. `plane_list_agent_accounts`
3. `plane_list_project_members(project_id)`
4. `plane_add_project_member(project_id, member_agent_id, role)`

Update assigned work:

1. `plane_get_work_item(work_item_id, project_id)`
2. `plane_add_comment(work_item_id, body, project_id)` for progress notes
3. `plane_update_status(work_item_id, status, project_id)` when moving state
4. `plane_update_work_item(...)` only for fields you intend to change

## Roles

- Guest: read and comment.
- Member: create work items and write work assigned to its Plane user.
- Admin: assign work, create projects, and manage project membership.

## Error Recovery

If a tool returns `isError=true`, parse the JSON content:

- Read `error.message`.
- Follow `error.hint`.
- Call tools in `error.suggested_next_tools` before retrying.

Important errors:

- `invalid_agent_id`: use a short `agent_id` from `plane_list_agent_accounts` or `plane_list_project_members`; do not pass UUID/email.
- `target_not_project_member`: Admin should call `plane_add_project_member` before assigning/creating work for that agent.
- `insufficient_project_role`: switch to an Admin MCP server or ask a project Admin.
- `not_assigned`: ask an Admin to assign the work item to you before writing.
- `unknown_state`: call `plane_list_states(project_id)` and retry with a returned state.
- `invalid_work_item_kind`: call `plane_list_work_item_kinds(project_id)` and retry with a returned kind.
- `invalid_stage_completion`: compare submitted Evidence keys with `required_evidence` in `mesh_get_run`, then resubmit a complete strict Evidence array.
- `target_not_eligible`: refresh Policy, roles, and `mesh_list_eligible_agents`; omit the target when no valid Agent is available.
