#!/usr/bin/env bash
set -euo pipefail
umask 077

BACKUP_ROOT="${MESH_GATEWAY_BACKUP_ROOT:-$HOME/.mesh/backups}"
mkdir -p "$BACKUP_ROOT"
chmod 700 "$BACKUP_ROOT"
export MESH_GATEWAY_BACKUP_ROOT="$BACKUP_ROOT"
python3 - <<'PY'
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

target = Path(os.environ['MESH_GATEWAY_BACKUP_ROOT']) / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
target.mkdir(mode=0o700)
source = Path(os.environ.get('MESH_GATEWAY_STATE_DB', '~/.mesh/mesh-agent-gateway.sqlite3')).expanduser()
databases = [source, source.with_name(f'{source.stem}-idempotency.sqlite3')]
checksums = {}
for database in databases:
    if not database.is_file():
        raise SystemExit(f'Missing Gateway database: {database}')
    output = target / database.name
    with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as src, sqlite3.connect(output) as dst:
        src.backup(dst)
        if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise SystemExit(f'Backup failed integrity check: {output.name}')
    output.chmod(0o600)
    checksums[output.name] = hashlib.sha256(output.read_bytes()).hexdigest()
(target / 'checksums.json').write_text(json.dumps(checksums, indent=2) + '\n')
print(target)
PY
