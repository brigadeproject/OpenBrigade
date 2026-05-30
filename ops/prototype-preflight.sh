#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "== OpenBrigade Prototype Preflight =="
date -u +"utc=%Y-%m-%dT%H:%M:%SZ"
echo

docker compose --env-file "${ENV_FILE:-.env}" --profile app ps
echo

./ops/brigade-live.sh health --json
echo

./ops/brigade-live.sh status --json
echo

./ops/brigade-live.sh agent list
echo

./ops/brigade-live.sh dashboard --plain --view alerts
echo

./ops/brigade-live.sh dashboard --plain --view agents
