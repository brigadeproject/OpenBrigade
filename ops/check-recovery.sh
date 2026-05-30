#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

WORK_DIR="$(mktemp -d /tmp/openbrigade-recovery.XXXXXX)"
REPORT_PATH="$WORK_DIR/report.json"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-120}"
KEEP_WORK_DIR="${KEEP_WORK_DIR:-0}"
REQUIRED_STORE_BACKEND="${REQUIRED_STORE_BACKEND:-PostgresStateStore}"

cleanup() {
  if [[ "$KEEP_WORK_DIR" != "1" ]]; then
    rm -rf "$WORK_DIR"
  fi
}
trap cleanup EXIT

usage() {
  cat <<EOF
usage: $0

environment:
  TIMEOUT_SECONDS   wait budget for container recovery, default 120
  REQUIRED_STORE_BACKEND   expected active store backend after recreate, default PostgresStateStore
  KEEP_WORK_DIR     set to 1 to preserve raw outputs in $WORK_DIR
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

./ops/brigade-live.sh config show >"$WORK_DIR/config-before.json"
./ops/brigade-live.sh status --json >"$WORK_DIR/status-before.json"
./ops/brigade-live.sh datastore inspect --backend redis --limit 10 \
  >"$WORK_DIR/redis-before.json" || true
./ops/brigade-live.sh datastore inspect --backend qdrant --limit 3 \
  >"$WORK_DIR/qdrant-before.json" || true
./ops/brigade-live.sh datastore inspect --backend neo4j --limit 3 \
  >"$WORK_DIR/neo4j-before.json" || true

./ops/recreate-stack.sh >"$WORK_DIR/recreate.out" 2>"$WORK_DIR/recreate.err"

deadline=$(( $(date +%s) + TIMEOUT_SECONDS ))
while true; do
  if ./ops/brigade-live.sh status --json >"$WORK_DIR/status-after.json" 2>"$WORK_DIR/status-after.err"; then
    break
  fi
  if (( $(date +%s) >= deadline )); then
    echo "timed out waiting for live prototype after recreate" >&2
    cat "$WORK_DIR/status-after.err" >&2 || true
    exit 1
  fi
  sleep 2
done

wait_for_datastore_inspect() {
  local backend="$1"
  local output_path="$2"
  local error_path="$3"
  while true; do
    if ./ops/brigade-live.sh datastore inspect --backend "$backend" --limit 3 \
      >"$output_path" 2>"$error_path"; then
      return 0
    fi
    if (( $(date +%s) >= deadline )); then
      return 1
    fi
    sleep 2
  done
}

./ops/brigade-live.sh config show >"$WORK_DIR/config-after.json"
./ops/brigade-live.sh dashboard --plain --view mission >"$WORK_DIR/dashboard-mission.txt"
./ops/brigade-live.sh dashboard --plain --view alerts >"$WORK_DIR/dashboard-alerts.txt"
./ops/brigade-live.sh dashboard --plain --view teams >"$WORK_DIR/dashboard-teams.txt"
wait_for_datastore_inspect redis "$WORK_DIR/redis-after.json" "$WORK_DIR/redis-after.err" \
  || true
wait_for_datastore_inspect qdrant "$WORK_DIR/qdrant-after.json" "$WORK_DIR/qdrant-after.err" \
  || true
wait_for_datastore_inspect neo4j "$WORK_DIR/neo4j-after.json" "$WORK_DIR/neo4j-after.err" \
  || true

python3 - "$WORK_DIR" "$REPORT_PATH" "$REQUIRED_STORE_BACKEND" <<'PY'
import json
import sys
from pathlib import Path

work_dir = Path(sys.argv[1])
report_path = Path(sys.argv[2])
required_store_backend = sys.argv[3]
config_before = json.loads((work_dir / "config-before.json").read_text())
config_after = json.loads((work_dir / "config-after.json").read_text())
before = json.loads((work_dir / "status-before.json").read_text())
after = json.loads((work_dir / "status-after.json").read_text())
def load_inspection(name):
    path = work_dir / name
    if not path.exists() or not path.read_text().strip():
        return {"ok": False, "sample_count": 0, "reason": "inspection did not produce output"}
    return json.loads(path.read_text())

qdrant_before = load_inspection("qdrant-before.json")
qdrant_after = load_inspection("qdrant-after.json")
neo4j_before = load_inspection("neo4j-before.json")
neo4j_after = load_inspection("neo4j-after.json")
redis_before = load_inspection("redis-before.json")
redis_after = load_inspection("redis-after.json")

goals_before = {key: len(value) for key, value in before.get("goals", {}).items()}
goals_after = {key: len(value) for key, value in after.get("goals", {}).items()}

summary = {
    "store_backend_before": config_before.get("store_backend"),
    "store_backend_after": config_after.get("store_backend"),
    "users_before": len(before.get("users", [])),
    "users_after": len(after.get("users", [])),
    "agents_before": len(before.get("agents", [])),
    "agents_after": len(after.get("agents", [])),
    "teams_before": len(before.get("teams", [])),
    "teams_after": len(after.get("teams", [])),
    "goals_before": goals_before,
    "goals_after": goals_after,
    "assignment_history_before": len(before.get("assignment_history", [])),
    "assignment_history_after": len(after.get("assignment_history", [])),
    "transcripts_before": len(before.get("transcripts", [])),
    "transcripts_after": len(after.get("transcripts", [])),
    "active_assignments_before": sorted(item["assignment_id"] for item in before.get("assignments", [])),
    "active_assignments_after": sorted(item["assignment_id"] for item in after.get("assignments", [])),
    "queued_assignments_after": sorted(
        item["assignment_id"] for item in after.get("assignments", [])
        if item.get("status") == "queued"
    ),
    "dashboard_mission_rendered": bool((work_dir / "dashboard-mission.txt").read_text().strip()),
    "dashboard_alerts_rendered": bool((work_dir / "dashboard-alerts.txt").read_text().strip()),
    "dashboard_teams_rendered": bool((work_dir / "dashboard-teams.txt").read_text().strip()),
    "redis_before_ok": bool(redis_before.get("ok")),
    "redis_after_ok": bool(redis_after.get("ok")),
    "redis_pending_count_before": int(redis_before.get("pending_count") or 0),
    "redis_pending_count_after": int(redis_after.get("pending_count") or 0),
    "redis_active_claim_count_after": int(redis_after.get("active_claim_count") or 0),
    "qdrant_before_ok": bool(qdrant_before.get("ok")),
    "qdrant_after_ok": bool(qdrant_after.get("ok")),
    "qdrant_sample_count_before": int(qdrant_before.get("sample_count") or 0),
    "qdrant_sample_count_after": int(qdrant_after.get("sample_count") or 0),
    "neo4j_before_ok": bool(neo4j_before.get("ok")),
    "neo4j_after_ok": bool(neo4j_after.get("ok")),
    "neo4j_sample_count_before": int(neo4j_before.get("sample_count") or 0),
    "neo4j_sample_count_after": int(neo4j_after.get("sample_count") or 0),
}

failures = []
if summary["store_backend_after"] != required_store_backend:
    failures.append(
        f"expected store backend {required_store_backend}, saw {summary['store_backend_after']}"
    )
if summary["users_before"] != summary["users_after"]:
    failures.append(
        f"user count changed across recreate: {summary['users_before']} -> {summary['users_after']}"
    )
if summary["agents_before"] != summary["agents_after"]:
    failures.append(
        f"agent count changed across recreate: {summary['agents_before']} -> {summary['agents_after']}"
    )
if summary["teams_before"] != summary["teams_after"]:
    failures.append(
        f"team count changed across recreate: {summary['teams_before']} -> {summary['teams_after']}"
    )
if summary["goals_before"] != summary["goals_after"]:
    failures.append("goal counts changed across recreate")
if summary["assignment_history_after"] < summary["assignment_history_before"]:
    failures.append("assignment history count regressed across recreate")
if summary["transcripts_after"] < summary["transcripts_before"]:
    failures.append("transcript count regressed across recreate")
if summary["active_assignments_before"] != summary["active_assignments_after"]:
    failures.append("active assignments changed across recreate")
if not summary["dashboard_mission_rendered"]:
    failures.append("dashboard mission view did not render after recreate")
if not summary["dashboard_alerts_rendered"]:
    failures.append("dashboard alerts view did not render after recreate")
if not summary["dashboard_teams_rendered"]:
    failures.append("dashboard teams view did not render after recreate")
if not summary["redis_after_ok"]:
    failures.append("redis inspection failed after recreate")
if summary["redis_pending_count_after"] != len(summary["queued_assignments_after"]):
    failures.append("redis pending assignment queue is not reconciled after recreate")
if not summary["qdrant_after_ok"]:
    failures.append("qdrant inspection failed after recreate")
if summary["qdrant_sample_count_before"] and not summary["qdrant_sample_count_after"]:
    failures.append("qdrant sample records disappeared after recreate")
if not summary["neo4j_after_ok"]:
    failures.append("neo4j inspection failed after recreate")
if summary["neo4j_sample_count_before"] and not summary["neo4j_sample_count_after"]:
    failures.append("neo4j sample records disappeared after recreate")

summary["invariant_failures"] = failures
summary["invariants_ok"] = not failures

report_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
print(json.dumps(summary, indent=2, sort_keys=True))
if failures:
    raise SystemExit(1)
PY

echo
if [[ "$KEEP_WORK_DIR" == "1" ]]; then
  echo "report_path=$REPORT_PATH"
fi
