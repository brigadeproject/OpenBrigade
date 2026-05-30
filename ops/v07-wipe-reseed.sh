#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CONFIRM="${1:-}"
if [[ "$CONFIRM" != "--confirm-wipe" ]]; then
  cat >&2 <<'USAGE'
usage: ./ops/v07-wipe-reseed.sh --confirm-wipe

Creates a prototype backup, drops brigade_ Docker volumes, rebuilds the app stack,
runs explicit migrations, and reseeds the MVP defaults. Runtime data is disposable
for v0.7; source, config, ops scripts, and backups remain durable.
USAGE
  exit 2
fi

./ops/backup-prototype.sh
./ops/recreate-stack.sh --drop-volumes

docker compose --env-file .env --profile app up -d --build

timeout 90 bash -c '
  until ./ops/brigade-live.sh health --json >/tmp/brigade-v07-health.json 2>/tmp/brigade-v07-health.err; do
    sleep 3
  done
'

./ops/brigade-live.sh db migrate
./ops/brigade-live.sh init mvp --force
./ops/brigade-live.sh health --json
