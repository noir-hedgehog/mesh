#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_ROOT="${MESH_BACKUP_DIR:-${AGENTPM_BACKUP_DIR:-$ROOT_DIR/backups}}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="$BACKUP_ROOT/$STAMP"
KEEP_DAYS="${MESH_BACKUP_KEEP_DAYS:-${AGENTPM_BACKUP_KEEP_DAYS:-14}}"

mkdir -p "$TARGET"
chmod 700 "$BACKUP_ROOT" "$TARGET"

sudo docker exec plane-db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > "$TARGET/plane-postgres.sql.gz"
sudo docker run --rm -v plane_uploads:/source:ro postgres:15.7-alpine tar -czf - -C /source . > "$TARGET/plane-uploads.tar.gz"
if [ "$(sudo docker inspect -f '{{.State.Running}}' agentpm 2>/dev/null || true)" = "true" ]; then
  sudo docker exec agentpm tar -czf - -C /data . > "$TARGET/agentpm-data.tar.gz"
elif sudo docker volume inspect agent-native-pm_agentpm_data >/dev/null 2>&1; then
  sudo docker run --rm -v agent-native-pm_agentpm_data:/source:ro postgres:15.7-alpine \
    tar -czf - -C /source . > "$TARGET/agentpm-data.tar.gz"
fi

if [ -d "$ROOT_DIR/.agentpm" ]; then
  tar -hczf "$TARGET/agent-registry.tar.gz" -C "$ROOT_DIR" .agentpm
fi

secret_files=()
for file in .env.agentpm plane/.env plane/apps/api/.env; do
  [ -f "$ROOT_DIR/$file" ] && secret_files+=("$file")
done
if [ "${#secret_files[@]}" -gt 0 ]; then
  tar -hczf "$TARGET/service-secrets.tar.gz" -C "$ROOT_DIR" "${secret_files[@]}"
fi

(cd "$TARGET" && sha256sum ./* > SHA256SUMS)
find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime "+$KEEP_DAYS" -exec rm -rf {} +
echo "$TARGET"
