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

- Root Python regression: 83 tests passed.
- Gateway: 6 tests passed, covering A2A auth/idempotency/persistence, worktrees,
  metadata conversion, isolated config/credentials, and file Artifacts.
- Plane MCP: 26 contract tests passed; Runner: 9; source formats: 10.
- Django check and migration drift check passed.
- Console production build passed locally; production sign-in page rendered with
  Mesh branding, AGPL link, and Plane CE attribution.
- Backup `20260919T134359Z`: PostgreSQL restored into a temporary database;
  nine projects verified and the temporary database dropped.
- Post-deployment backup `20260919T140319Z`: checksums and archive checks passed,
  including dereferenced shared Agent registry and service secret files.
- Gateway SQLite snapshot `~/.mesh/backups/20260919T141646Z`: integrity checks
  passed. Local backup directories are restricted and exclude environment tokens.

## Release Gates Still Open

Do not create mesh-v0.2.0 or freeze the contracts yet.

- Authenticated Console smoke for runtime actions, profiles, and Evidence detail;
  only the sign-in screen has been browser-verified in this continuation.
- Complete startup retry, Gateway outage, timeout, and restart-in-flight drills.
- Preserve actual model/usage for invalid completion attempts (current rollback
  leaves runtime-reported); distinguish unavailable costs from measured zero.
- Runner currently speaks tested A2A 1.0 JSON-RPC over HTTP; adopting the official
  client SDK remains open. Gateway already uses the official SDK.
- Full application-level backup recovery verification, beyond database/archive
  integrity, and final release provenance/source links.
- Re-review AGPM-1/8/12/13 against these remaining gates; do not close them based
  solely on unit tests or the existence of UI code.

Legacy verify_mvp.sh was not rerun because it starts/seeds local services and
performs legacy write-back smoke. Its lower-level regression tests were run;
this must not be described as a full legacy smoke pass.
