#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${MESH_PLANE_AGENT_ENV_FILE:-$ROOT_DIR/.agentpm/plane-agent-env.production.sh}"
AGENT_ID="${MESH_MCP_AGENT_ID:-hekate}"
MCP_URL="${MESH_PRODUCTION_MCP_URL:-http://100.79.187.62:8080/api/v1/workspaces/agentpm/mcp/}"

exec python3 "$ROOT_DIR/scripts/plane_native_mcp_proxy.py" \
  "$@" \
  --agent-id "$AGENT_ID" \
  --url "$MCP_URL" \
  --env-file "$ENV_FILE"
