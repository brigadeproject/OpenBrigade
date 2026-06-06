#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

CONFIRM="${1:-}"
ENV_FILE="${ENV_FILE:-.env}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-120}"
WORK_DIR="$(mktemp -d /tmp/openbrigade-full-wipe.XXXXXX)"
KEEP_WORK_DIR="${KEEP_WORK_DIR:-0}"

cleanup() {
  if [[ "$KEEP_WORK_DIR" != "1" ]]; then
    rm -rf "$WORK_DIR"
  fi
}
trap cleanup EXIT

usage() {
  cat <<'EOF'
usage: ./ops/full-wipe.sh --confirm-full-wipe

Creates a prototype backup, drops all brigade_ runtime volumes, rebuilds the app
stack, runs migrations, verifies health, and leaves userland empty.

This does not reseed MVP defaults. It deletes generated runtime data including
missions, users, agents, teams, goals, assignments, chats, transcripts, alerts,
episodes, provenance records, Redis state, Qdrant state, and Neo4j state.
The running daemon may recreate runtime telemetry such as empty orchestrator
reasoning ticks and zero-cost financial reports after the stack starts.

environment:
  ENV_FILE          compose env file, default .env
  TIMEOUT_SECONDS  wait budget for live container readiness, default 120
  BACKUP_ROOT      backup root passed through to backup-prototype.sh
  KEEP_WORK_DIR    set to 1 to preserve raw validation output under /tmp
EOF
}

if [[ "$CONFIRM" == "-h" || "$CONFIRM" == "--help" ]]; then
  usage
  exit 0
fi

if [[ "$CONFIRM" != "--confirm-full-wipe" ]]; then
  usage >&2
  exit 2
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing env file: $ENV_FILE" >&2
  exit 1
fi

echo "OpenBrigade full wipe requested." >&2
echo "A backup will be created before deleting brigade_ runtime volumes." >&2
echo "The rebuilt stack will be migrated but not reseeded." >&2

BACKUP_DIR="$(./ops/backup-prototype.sh)"
echo "backup=$BACKUP_DIR" | tee "$WORK_DIR/summary.txt"

./ops/recreate-stack.sh --drop-volumes >"$WORK_DIR/recreate.out" 2>"$WORK_DIR/recreate.err"

deadline=$(( $(date +%s) + TIMEOUT_SECONDS ))
while true; do
  if ./ops/brigade-live.sh health --json >"$WORK_DIR/health-before-migrate.json" 2>"$WORK_DIR/health-before-migrate.err"; then
    break
  fi
  if (( $(date +%s) >= deadline )); then
    echo "timed out waiting for live stack after full wipe" >&2
    cat "$WORK_DIR/health-before-migrate.err" >&2 || true
    exit 1
  fi
  sleep 2
done

./ops/brigade-live.sh db migrate >"$WORK_DIR/db-migrate.out" 2>"$WORK_DIR/db-migrate.err"
./ops/brigade-live.sh db status >"$WORK_DIR/db-status.json"
./ops/brigade-live.sh health --json >"$WORK_DIR/health-after-migrate.json"
./ops/brigade-live.sh status --json >"$WORK_DIR/status-after.json"

python3 - "$WORK_DIR/status-after.json" "$WORK_DIR/full-wipe-report.json" <<'PY'
import json
import sys
from pathlib import Path

status_path = Path(sys.argv[1])
report_path = Path(sys.argv[2])
status = json.loads(status_path.read_text(encoding="utf-8"))

empty_list_fields = [
    "users",
    "agents",
    "teams",
    "assignments",
    "assignment_history",
    "alerts",
    "knowledge_documents",
    "knowledge_chunks",
    "episodes",
    "provenance_records",
    "messages",
    "usage_records",
    "transcripts",
    "cloud_jobs",
]
empty_mapping_fields = ["agent_states", "goals"]
none_fields = ["mission"]
telemetry_fields = ["orchestrator_reasoning"]

failures = []
for field in empty_list_fields:
    value = status.get(field)
    if not isinstance(value, list) or len(value) != 0:
        failures.append(f"{field} is not empty")
for field in empty_mapping_fields:
    value = status.get(field)
    if not isinstance(value, dict) or len(value) != 0:
        failures.append(f"{field} is not empty")
for field in none_fields:
    if status.get(field) is not None:
        failures.append(f"{field} is not empty")

report = {
    "empty_userland": not failures,
    "failures": failures,
    "counts": {
        field: len(status.get(field, []))
        for field in empty_list_fields
        if isinstance(status.get(field), list)
    },
    "mapping_counts": {
        field: len(status.get(field, {}))
        for field in empty_mapping_fields
        if isinstance(status.get(field), dict)
    },
    "mission_is_empty": status.get("mission") is None,
    "runtime_telemetry": {
        field: len(status.get(field, []))
        for field in telemetry_fields
        if isinstance(status.get(field), list)
    },
    "financial_report_generated": status.get("financial_report") is not None,
}
report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
if failures:
    raise SystemExit(1)
PY

cat "$WORK_DIR/full-wipe-report.json"
echo "backup=$BACKUP_DIR"
if [[ "$KEEP_WORK_DIR" == "1" ]]; then
  echo "work_dir=$WORK_DIR"
fi
