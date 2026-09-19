# Mesh v0.2 Validation Status

Updated: 2026-09-19. Production remains the only project data source.

## Deployed and Verified

- Console: http://100.79.187.62:8080/ (Tailscale required).
- Project: Mesh Platform, identifier AGPM, id `529232be-8e6f-4c57-9b66-d56438050a92`.
- Cycle: Mesh v0.2 Real Loop, with AGPM-18 through AGPM-25 and acceptance AGPM-26.
- AGPM-14/15/16 retained as Superseded; no historical work items deleted.
- A2A 1.0 Gateway on the local Tailscale address, four synchronized Agent Cards.
- Production API, Runner, Indexer, Console, and additive migration 0125 deployed.
- Policy, published workflow Skill 0.2.1, and cited Knowledge available in AGPM.
- Workflow Skill installed in local Codex and OpenClaw.
- OpenClaw native MCP registration includes both plane_* and mesh_* tools.
- Per-task config exposes only the selected Agent's native Plane MCP identity;
  coding profiles explicitly allow that namespace. This is tool scoping, not an
  OS-level sandbox against a malicious local process with the same user access.
- Loop worktrees are outside the development checkout. Agents do not push/merge.
- Completion uses a strict JSON Artifact file, not inference from chat prose.

## Real Execution

Acceptance run: `a6afd0b3-015d-4df8-ac91-2d6f7ee28cc0` (AGPM-26).
Completed at 2026-09-19 14:20:55 UTC with three succeeded Stages, three
succeeded Attempts, two explicit Handoffs, and seven AuditEvents. Models were
Iris: kimi-for-coding; Lingxi: MiniMax-M3; Hekate: MiniMax-M2.7. All required
Evidence keys were present. Work Item assignees were cleared on completion.
The isolated change is commit `811d48a9e170cd109bd96e7d65af57a55a193a84`.
It adds a dependency-free label helper for literal A2A TASK_STATE_* wire values.
The commit remains on its isolated run branch and is not merged or pushed.

Earlier canceled/failed runs remain in production as evidence. They exposed:

- nested Protobuf metadata needing explicit conversion;
- coding-profile MCP tools omitted despite successful server registration;
- local HTTP proxies intermittently breaking Tailscale MCP;
- orphaned OpenClaw children after canceling only the parent process;
- invalid outcome enums and prose-wrapped completion JSON.

These were repaired without accepting loose outcome aliases or fabricating
Evidence. Missing Evidence exhausted the configured retry budget as expected.

## Verification

- Root Python regression: 85 tests passed.
- Gateway: 6 tests passed, covering A2A auth/idempotency/persistence, worktrees,
  metadata conversion, isolated config/credentials, and file Artifacts.
- Plane MCP: 26 contract tests passed; Runner: 20; source formats: 10.
- Django check and migration drift check passed.
- Console production build and typecheck passed locally. Authenticated production
  browser smoke passed with zero uncaught errors: all three actual execution
  models and expandable Evidence, Members, Policy, Skills, Knowledge, Loops,
  and read-only Agent execution profile inspection. The secret-reference edit
  field remained empty. Desktop and mobile screenshots are in the ignored
  `.agentpm/console-smoke/` directory.
- AGPM-27 exercised the actual Console start/cancel controls: run
  `6f8be682-28d0-461f-8418-0488dfd3157c` waited for an assignee and was canceled
  without dispatching an Agent. Temporary human Admin test sessions were revoked;
  this verifies authenticated UI behavior, not password-based sign-in.
- Four Agent identities read the completed acceptance run through production MCP.
  Handoffs are chronological (`lingxi`, then `hekate`); unreported costs are null,
  not fabricated zeros. Invalid/failed completion keeps actual model and usage.
- Startup retry reuse, exhausted polling retry, timeout, canceled-run safety and
  chronological Handoff serialization are covered by Runner regression tests.
- Backup `20260919T134359Z`: PostgreSQL restored into a temporary database;
  nine projects verified and the temporary database dropped.
- Post-deployment backup `20260919T140319Z`: checksums and archive checks passed,
  including dereferenced shared Agent registry and service secret files.
- Gateway SQLite snapshot `~/.mesh/backups/20260919T141646Z`: integrity checks
  passed. Local backup directories are restricted and exclude environment tokens.
- Pre-update production backup `20260919T145815Z`: checksum/archive checks passed.
- Running API, Console and proxy source baseline: `6310be34c`; production source
  directory `/opt/apps/mesh/releases/6310be34c`. Containers report healthy, and
  `/mesh/health/` has database ok and zero stale attempts.

## Service Repairs

- The broad `/agentpm/*` redirect was intercepting the existing `agentpm`
  workspace and causing project pages to return 404. Only the legacy health
  endpoint now redirects; project URLs remain unchanged.
- The initial loading placeholder rendered differently at build time and in
  the browser. It now waits for mounting before using the browser theme.
  Router runtime/build packages were also aligned to 7.13.1. A cold production
  page load now completes without React hydration errors.
- Proxy compression and long-lived caching for fingerprinted static assets
  reduce repeated transfers over Tailscale. Missing assets return 404, not HTML.

Repeat the privileged UI smoke from the repository with an installed Playwright
module (or set `PLAYWRIGHT_MODULE` to its module path):

```sh
node scripts/verify_mesh_console.mjs \
  --project 529232be-8e6f-4c57-9b66-d56438050a92 \
  --issue 38a2ecce-b6b0-44c8-9ec7-c60ab0243e11
```

Use `--controls-issue <dedicated-smoke-issue-id>` only when intentionally testing
start/cancel actions. The script requires administrator SSH access and creates
a ten-minute session for an existing human project Admin, revoked in `finally`.

## Release Gates Still Open

Do not create mesh-v0.2.0 or freeze the contracts yet.

- Live Console Stage assignment and profile write/rotation smoke; read-only
  profiles, start/cancel and Evidence inspection are verified.
- Live Gateway outage, timeout and restart-in-flight drills. Retry/timeout
  regression tests pass, but are not a substitute for production failure drills.
- Runner currently speaks tested A2A 1.0 JSON-RPC over HTTP; adopting the official
  client SDK remains open. Gateway already uses the official SDK.
- Full application-level backup recovery verification, beyond database/archive
  integrity, and final release provenance/source links.
- Re-review AGPM-1/8/12/13 against these remaining gates; do not close them based
  solely on unit tests or the existence of UI code.

Legacy verify_mvp.sh was not rerun because it starts/seeds local services and
performs legacy write-back smoke. Its lower-level regression tests were run;
this must not be described as a full legacy smoke pass.
