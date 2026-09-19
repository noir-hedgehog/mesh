#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

usage() {
  cat <<'EOF'
Usage: scripts/register_plane_native_mcp_openclaw.sh [--dry-run] [mcp-url]

Registers Plane's built-in MCP endpoint through a local secret-loading stdio proxy.
The OpenClaw config stores the agent id and URL, but never the Plane API token.
For a remote deployment, set MESH_PLANE_AGENT_ENV_FILE to a dedicated ignored
file such as .agentpm/plane-agent-env.remote.sh and MESH_SKIP_PLANE_SEED=1.
Matching AGENTPM_* names remain compatibility fallbacks for one release.
EOF
}

DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]; then DRY_RUN=1; shift; fi
if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then usage; exit 0; fi

command -v python3 >/dev/null 2>&1 || { echo "python3 CLI not found" >&2; exit 1; }
OPENCLAW_BIN="${MESH_OPENCLAW_BIN:-openclaw}"
if [ "$DRY_RUN" -eq 0 ]; then
  command -v "$OPENCLAW_BIN" >/dev/null 2>&1 || { echo "openclaw CLI not found" >&2; exit 1; }
  if [ "${MESH_SKIP_PLANE_SEED:-${AGENTPM_SKIP_PLANE_SEED:-0}}" != "1" ]; then
    eval "$(./scripts/seed_plane_mvp.sh | /usr/bin/grep '^export ')"
    eval "$(./scripts/seed_plane_agents.sh | /usr/bin/grep '^export ')"
  fi
fi

ENV_FILE="${MESH_PLANE_AGENT_ENV_FILE:-${AGENTPM_PLANE_AGENT_ENV_FILE:-$ROOT_DIR/.agentpm/plane-agent-env.sh}}"
AGENT_ID="${MESH_MCP_AGENT_ID:-${AGENTPM_MCP_AGENT_ID:-hekate}}"
SERVER_NAME="plane-native-$AGENT_ID"
PLANE_API_BASE_URL="${PLANE_API_BASE_URL:-http://127.0.0.1:8000}"
PLANE_WORKSPACE_SLUG="${PLANE_WORKSPACE_SLUG:-agentpm}"
MCP_URL="${PLANE_NATIVE_MCP_URL:-${1:-${PLANE_API_BASE_URL%/}/api/v1/workspaces/$PLANE_WORKSPACE_SLUG/mcp/}}"

ARGS=(
  --command python3
  --arg scripts/plane_native_mcp_proxy.py
  --arg=--agent-id --arg "$AGENT_ID"
  --arg=--url --arg "$MCP_URL"
  --arg=--env-file --arg "$ENV_FILE"
  --cwd "$ROOT_DIR"
  --timeout 20
  --connect-timeout 20
  --include 'plane_*,mesh_*'
)

if [ "$DRY_RUN" -eq 1 ]; then
  printf 'openclaw mcp add %q' "$SERVER_NAME"
  printf ' %q' "${ARGS[@]}"
  printf '\n%s\n' 'scripts/install_plane_agent_skill.sh --openclaw-only'
  exit 0
fi

"$OPENCLAW_BIN" mcp add "$SERVER_NAME" "${ARGS[@]}"
"$OPENCLAW_BIN" mcp reload >/dev/null 2>&1 || true
"$OPENCLAW_BIN" mcp probe "$SERVER_NAME" --json

if [ "${MESH_SKIP_SKILL_INSTALL:-${AGENTPM_SKIP_SKILL_INSTALL:-0}}" != "1" ]; then
  ./scripts/install_plane_agent_skill.sh --openclaw-only
fi
