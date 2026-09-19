#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="$HOME/.mesh/agent-gateway"
ENV_FILE="$ROOT_DIR/.agentpm/mesh-agent-gateway.env"
RUNTIME_ENV="$INSTALL_DIR/mesh-agent-gateway.env"
VENV="$INSTALL_DIR/venv"
PLIST="$HOME/Library/LaunchAgents/dev.mesh.agent-gateway.plist"
RUNNER="$INSTALL_DIR/run-gateway"
TAILSCALE_IP="$(tailscale ip -4 | head -1)"
PYTHON_BIN=""

if [ -z "$TAILSCALE_IP" ]; then
  echo "Tailscale must be connected before installing the Mesh Agent Gateway." >&2
  exit 1
fi

mkdir -p "$ROOT_DIR/.agentpm" "$INSTALL_DIR" "$HOME/Library/LaunchAgents" "$HOME/.mesh/worktrees"

if [ ! -f "$ENV_FILE" ]; then
  token="$(openssl rand -hex 32)"
  {
    printf 'export MESH_GATEWAY_TOKEN=%q\n' "$token"
    printf 'export MESH_GATEWAY_HOST=%q\n' "$TAILSCALE_IP"
    printf 'export MESH_GATEWAY_PORT=18890\n'
    printf 'export MESH_GATEWAY_PUBLIC_URL=%q\n' "http://$TAILSCALE_IP:18890"
    printf 'export MESH_GATEWAY_STATE_DB=%q\n' "$HOME/.mesh/mesh-agent-gateway.sqlite3"
    printf 'export MESH_GATEWAY_WORKTREE_ROOT=%q\n' "$HOME/.mesh/worktrees"
    printf 'export MESH_GATEWAY_PROJECT_REPOS=%q\n' "${MESH_GATEWAY_PROJECT_REPOS_JSON:-{}}"
  } > "$ENV_FILE"
fi

upsert_export() {
  local key="$1"
  local value="$2"
  local temporary
  temporary="$(mktemp)"
  grep -v "^export ${key}=" "$ENV_FILE" > "$temporary" || true
  printf 'export %s=%q\n' "$key" "$value" >> "$temporary"
  mv "$temporary" "$ENV_FILE"
}

upsert_export MESH_GATEWAY_HOST "$TAILSCALE_IP"
upsert_export MESH_GATEWAY_PUBLIC_URL "http://$TAILSCALE_IP:18890"
if [ -n "${MESH_GATEWAY_PROJECT_REPOS_JSON:-}" ]; then
  upsert_export MESH_GATEWAY_PROJECT_REPOS "$MESH_GATEWAY_PROJECT_REPOS_JSON"
fi
if [ -n "${MESH_GATEWAY_GIT_BASE_REF:-}" ]; then
  upsert_export MESH_GATEWAY_GIT_BASE_REF "$MESH_GATEWAY_GIT_BASE_REF"
fi

NODE_BIN=""
for candidate in /opt/homebrew/opt/node@24/bin/node "$HOME/.local/share/mise/installs/node/24/bin/node"; do
  if [ -x "$candidate" ]; then NODE_BIN="$candidate"; break; fi
done
if [ -z "$NODE_BIN" ]; then
  echo "A compatible isolated Node 24 runtime is required. Install node@24 or configure it with mise." >&2
  exit 1
fi
NODE_VERSION="$($NODE_BIN -p 'process.versions.node')"
"$NODE_BIN" -e 'const [major,minor]=process.versions.node.split(".").map(Number); if (major !== 24 || minor < 15) process.exit(1)'

for candidate in python3.13 python3.12 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then PYTHON_BIN="$(command -v "$candidate")"; break; fi
done
if [ -z "$PYTHON_BIN" ]; then
  echo "Python 3.12 or newer is required for the Mesh Agent Gateway." >&2
  exit 1
fi

OPENCLAW_JS="$(readlink /opt/homebrew/bin/openclaw 2>/dev/null || true)"
if [ -z "$OPENCLAW_JS" ]; then OPENCLAW_JS="/opt/homebrew/lib/node_modules/openclaw/openclaw.mjs"; fi
if [[ "$OPENCLAW_JS" != /* ]]; then OPENCLAW_JS="/opt/homebrew/bin/$OPENCLAW_JS"; fi
OPENCLAW_WRAPPER="$INSTALL_DIR/openclaw-node24"
{
  printf '#!/usr/bin/env bash\nexec %q %q "$@"\n' "$NODE_BIN" "$OPENCLAW_JS"
} > "$OPENCLAW_WRAPPER"
chmod 700 "$OPENCLAW_WRAPPER"

if [ ! -x "$VENV/bin/python" ]; then
  "$PYTHON_BIN" -m venv "$VENV"
fi
"$VENV/bin/python" -c 'import sys; assert sys.version_info >= (3, 12), "Python 3.12 or newer is required"'
"$VENV/bin/python" -m pip install --disable-pip-version-check -r "$ROOT_DIR/services/mesh_agent_gateway/requirements.txt"
mkdir -p "$INSTALL_DIR/services/mesh_agent_gateway"
cp "$ROOT_DIR/services/__init__.py" "$INSTALL_DIR/services/__init__.py"
cp "$ROOT_DIR/services/mesh_agent_gateway/__init__.py" "$INSTALL_DIR/services/mesh_agent_gateway/__init__.py"
cp "$ROOT_DIR/services/mesh_agent_gateway/app.py" "$INSTALL_DIR/services/mesh_agent_gateway/app.py"
cp "$ENV_FILE" "$RUNTIME_ENV"
{
  printf 'export OPENCLAW_BIN=%q\n' "$OPENCLAW_WRAPPER"
  printf 'export PYTHONPATH=%q\n' "$INSTALL_DIR"
} >> "$RUNTIME_ENV"
chmod 600 "$ENV_FILE" "$RUNTIME_ENV"

{
  printf '#!/usr/bin/env bash\n'
  printf 'set -euo pipefail\n'
  printf 'source %q\n' "$RUNTIME_ENV"
  printf 'cd %q\n' "$INSTALL_DIR"
  printf 'exec %q -m services.mesh_agent_gateway.app\n' "$VENV/bin/python"
} > "$RUNNER"
chmod 700 "$RUNNER"

"$PYTHON_BIN" - <<PY
from pathlib import Path
Path("$PLIST").write_text('''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>dev.mesh.agent-gateway</string>
<key>ProgramArguments</key><array><string>$RUNNER</string></array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>WorkingDirectory</key><string>$INSTALL_DIR</string>
<key>StandardOutPath</key><string>/tmp/mesh-agent-gateway.log</string>
<key>StandardErrorPath</key><string>/tmp/mesh-agent-gateway.err.log</string>
</dict></plist>''', encoding="utf-8")
PY

launchctl bootout "gui/$UID/dev.agentpm.openclaw-bridge" >/dev/null 2>&1 || true
launchctl bootout "gui/$UID/dev.mesh.agent-gateway" >/dev/null 2>&1 || true
for attempt in {1..10}; do
  if launchctl bootstrap "gui/$UID" "$PLIST"; then break; fi
  if [ "$attempt" -eq 10 ]; then exit 1; fi
  sleep 1
done
launchctl kickstart -k "gui/$UID/dev.mesh.agent-gateway"

for _ in {1..40}; do
  if curl --noproxy '*' -fsS "http://$TAILSCALE_IP:18890/health" >/dev/null; then break; fi
  sleep 0.5
done
curl --noproxy '*' -fsS "http://$TAILSCALE_IP:18890/health"
curl --noproxy '*' -fsS "http://$TAILSCALE_IP:18890/agents/iris/.well-known/agent-card.json" >/dev/null
"$OPENCLAW_WRAPPER" --version >/dev/null
"$OPENCLAW_WRAPPER" skills list --json | grep -q 'mesh-plane-workflow'
"$OPENCLAW_WRAPPER" mcp probe plane-native-hekate --json | grep -q 'plane_get_me'
printf '\nMesh Agent Gateway installed with Node %s at http://%s:18890\n' "$NODE_VERSION" "$TAILSCALE_IP"
