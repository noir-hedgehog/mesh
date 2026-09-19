#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_CONTAINER="${MESH_API_CONTAINER:-api}"
SKILL_SOURCE="${MESH_BOOTSTRAP_SKILL_SOURCE:-$ROOT_DIR/skills/mesh-plane-workflow/SKILL.md}"
CONTAINER_SCRIPT="/tmp/bootstrap_mesh_production.py"
CONTAINER_SKILL="/tmp/mesh-plane-workflow.SKILL.md"
DOCKER=(docker)

if ! docker info >/dev/null 2>&1; then
  DOCKER=(sudo docker)
fi

if [ ! -f "$SKILL_SOURCE" ]; then
  echo "Mesh Skill source not found: $SKILL_SOURCE" >&2
  exit 1
fi

"${DOCKER[@]}" cp "$ROOT_DIR/scripts/bootstrap_mesh_production.py" "$API_CONTAINER:$CONTAINER_SCRIPT"
"${DOCKER[@]}" cp "$SKILL_SOURCE" "$API_CONTAINER:$CONTAINER_SKILL"
"${DOCKER[@]}" exec \
  -e MESH_BOOTSTRAP_SKILL_PATH="$CONTAINER_SKILL" \
  -e MESH_BOOTSTRAP_WORKSPACE_SLUG="${MESH_BOOTSTRAP_WORKSPACE_SLUG:-}" \
  -e MESH_BOOTSTRAP_PROJECT_ID="${MESH_BOOTSTRAP_PROJECT_ID:-}" \
  "$API_CONTAINER" python manage.py shell -c "exec(open('$CONTAINER_SCRIPT').read())"
