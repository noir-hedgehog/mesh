# Production Deployment

Mesh runs inside the Plane Django API with dedicated `mesh-runner` and
`mesh-indexer` Celery workers. Plane-native MCP remains under
`/api/v1/workspaces/<slug>/mcp/`. The old standalone AgentPM/SQLite containers
are retained only as rollback artifacts and remain stopped after migration.

## First deployment

1. Install the A2A 1.0 Gateway on the machine that owns the OpenClaw agents. It uses an isolated Node 24 runtime and does not change the system Node version:

```bash
./scripts/install_openclaw_bridge.sh
```

2. Deploy Mesh Console and its workers. Deployment prepares ignored environment
files, synchronizes bridge credentials, and installs the signed assignment
webhook without printing secrets:

```bash
./scripts/deploy_remote.sh --plane
```

3. Verify containers and health:

```bash
./scripts/check_remote.sh
curl http://100.79.187.62:8080/mesh/health/
```

Use `--seed` only when initializing an empty Plane installation. Tokens and registry files remain under `.agentpm/` and are excluded from rsync and git. Policy publishing and approval decisions travel through Plane's authenticated Admin API proxy; direct `/agentpm/` write methods are blocked by Caddy.

## Backup and rollback

Run `scripts/backup_services.sh` on the server from the deployment root. It captures PostgreSQL, MinIO uploads, AgentPM SQLite data, and the local Agent registry with checksums.

Every deployment creates a source snapshot in `.deploy/source-backups`. Restore code with:

```bash
./scripts/rollback_remote.sh <timestamp>.tar.gz
```

Data restoration is intentionally manual: verify `SHA256SUMS`, stop writers, restore PostgreSQL/volumes, then run `scripts/check_remote.sh`.

Deployment installs `agentpm-backup.timer`, which runs daily. Validate any backup before a recovery exercise:

```bash
./scripts/verify_backup.sh backups/<timestamp>
```

A recovery exercise should restore into an isolated Docker project and verify Plane login, MCP identity, policy history, and AgentPM timeline before declaring the backup usable.

Keep production Plane credentials separate from local seed credentials. Codex
loads `.agentpm/plane-agent-env.production.sh` through
`scripts/mesh_production_mcp_stdio.sh`; OpenClaw may keep the compatibility
filename `.agentpm/plane-agent-env.remote.sh`. Neither file is committed.

Run `scripts/bootstrap_mesh_production.sh` after a data migration or new project
seed. It idempotently publishes baseline Project Policies, installs the
`mesh-plane-workflow` Skill, assigns default Agent functional roles, and indexes
project Pages as cited Knowledge.

Prepare the v0.2 project, Cycle, production Loop, runtime profiles, and acceptance work item after the Gateway is healthy:

```bash
MESH_V02_GATEWAY_BASE_URL=http://<local-tailscale-ip>:18890 \
MESH_V02_SYNC_AGENT_CARDS=1 \
./scripts/bootstrap_mesh_v02.sh
```

The bootstrap marks every configured Agent Card unavailable before synchronizing it. A failed Card request leaves the Agent out of candidate discovery. The production `mesh-runner` receives `MESH_AGENT_GATEWAY_TOKEN` from its ignored environment; the secret is referenced by name in `AgentExecutionProfile` and is never returned by the API.

Gateway state is stored in `~/.mesh/mesh-agent-gateway.sqlite3`, with a separate persistent idempotency database and per-Loop worktrees under `~/.mesh/worktrees`. Back up these files locally with restricted permissions; do not copy the bearer token into Mesh data or backups intended for sharing.

## Exposure policy

### Console builds on small hosts

Build the Console with `pnpm turbo run build --filter=web` from `plane/` on a
development machine. Transfer `plane/apps/web/build/client/` with the exact source
commit to the release's `plane/apps/web/` directory, then build the serving image
using `docker build -f Dockerfile.prebuilt -t plane-web .` with BuildKit in that
directory. For hosts with the legacy Docker builder, use
`tar -cf - Dockerfile.prebuilt nginx/nginx.conf build/client | docker build -f Dockerfile.prebuilt -t plane-web -`
so the normal development `.dockerignore` does not exclude the built assets.
This keeps the frontend compiler off the production host. Record the source commit
and artifact checksum in the release directory. Keep the preceding API and Web
images tagged by release before replacing the running containers.

- Plane HTTP/HTTPS binds to `LISTEN_HOST`; production uses the Tailscale IP.
- Plane API, MinIO console, MinIO API, and AgentPM's direct port bind to loopback only.
- Do not expose ports 8000, 8081, 9000, or 9090 through a cloud security group.
