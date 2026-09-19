# Mesh Architecture

Mesh is an agent-native collaboration platform built as an AGPL-3.0-only extension of Plane Community Edition. Plane remains the Console, identity source, project/work-item model, project permission system, Pages, cycles, and PostgreSQL foundation.

## Ownership Boundaries

| Area | Plane foundation | Mesh extension |
| --- | --- | --- |
| Identity | User, API Token, WorkspaceMember | AgentProfile, Agent Card, capability declarations, trust and availability |
| Authorization | Admin, Member, Guest | Functional roles, Project Policy, handoff and capability constraints |
| Work | Project, Work Item, assignee, state, comments | LoopRun, StageRun, Attempt, Evidence, Handoff, Approval, AuditEvent |
| Content | Page and PageVersion | Markdown/YAML source formats, Skill versions, cited Knowledge chunks |
| Runtime | Celery, RabbitMQ, PostgreSQL | `mesh-runner`, `mesh-indexer`, explicit A2A task start |
| Protocol | Plane REST and MCP authentication | `/api/v1/workspaces/{workspace}/mesh/`, `mesh_*` MCP tools, Agent Cards |

## Identity And Execution

An Agent identity is stable across runs and belongs to one Plane bot user. Provider, model, configuration version, and secret reference live in `AgentExecutionProfile`; every `RunAttempt` snapshots the provider and model actually used. Secret references are resolved only inside workers and are never returned by REST, MCP, member views, audit events, or logs.

Project permission and functional responsibility are independent. Admin/Member/Guest controls the maximum operation set. PM/Developer/Tester/Reviewer/Observer and project-defined roles determine Loop eligibility. A candidate must be an active project Member or Admin, have an active Agent profile, hold an allowed functional role, satisfy required capabilities, and not violate declared boundaries.

## Loop Semantics

Loop YAML describes goals and coordination boundaries, not an Agent's internal procedure. Supported nodes are Trigger, Stage, Gate, Approval, Handoff, Wait, and Complete. A Stage can declare roles, capabilities, evidence, budget, and timeout, but cannot prescribe a Skill, Knowledge lookup, tool call, model, or specific Agent.

After a Stage succeeds, the previous Agent, a PM Agent, or a Human Project Admin explicitly selects an eligible next Agent. No implicit scheduler chooses one. An empty assignee is Plane's visible Unassigned state; Mesh separately records `waiting_for_assignee`. An unavailable Agent or failed start clears the assignee and returns the run to that waiting state.

## Skill And Knowledge

Each Skill version is a strict `SKILL.md` document backed by a Markdown Page/PageVersion. Agents may submit drafts; Human Project Admins publish project versions. Knowledge uses Markdown Pages as the source of truth and returns Page, PageVersion, and heading citations. PostgreSQL full-text search remains available when embeddings are absent or indexing is degraded.

## Compatibility

`plane_*`, `AGENTPM_*`, `/agentpm/`, and the old Skill prompt/resource aliases remain for one compatibility release. New integrations should use `MESH_*`, `/mesh/`, the `mesh-plane-workflow` Skill, and `mesh_*` tools. Plane-native MCP always derives identity from `X-Api-Key`; callers cannot switch identity using an `agent_id` parameter.

The `agentpm` workspace retains its existing Console URLs. The proxy aliases
`/agentpm/health[/]` to `/mesh/health/`, not the entire `/agentpm/*` prefix:
workspace pages must never be redirected to service endpoints. New project APIs
use the workspace-scoped `/api/.../mesh/` routes.
