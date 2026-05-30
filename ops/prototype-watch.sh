#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

POLL_SECONDS="${POLL_SECONDS:-10}"

while true; do
  clear
  echo "== OpenBrigade Prototype Watch =="
  date -u +"utc=%Y-%m-%dT%H:%M:%SZ"
  echo

  echo "== Health =="
  ./ops/brigade-live.sh health --json || true
  echo

  echo "== Alerts =="
  ./ops/brigade-live.sh alert list || true
  echo

  echo "== Agents =="
  ./ops/brigade-live.sh dashboard --plain --view agents || true
  echo

  echo "== Mission =="
  ./ops/brigade-live.sh dashboard --plain --view mission || true
  sleep "$POLL_SECONDS"
done
