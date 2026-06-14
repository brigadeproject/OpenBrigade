#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

CONTAINER="${BRIGADE_LIVE_CONTAINER:-brigade_orchestrator}"
AGENT="${AGENT:-test-builder}"
PROVIDER="${PROVIDER:-ollama}"
MODEL="${MODEL:-gpt-oss:20b}"
KEEP_WORK_DIR="${KEEP_WORK_DIR:-0}"
WORK_DIR="$(mktemp -d /tmp/openbrigade-heartbeat.XXXXXX)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
REPORT_PATH="$WORK_DIR/report.json"
EXPECTED_CASE_COUNT="${EXPECTED_CASE_COUNT:-6}"

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
  AGENT      test agent to exercise, default test-builder
  PROVIDER   brigade provider, default ollama
  MODEL      model name, default gpt-oss:20b
  EXPECTED_CASE_COUNT   number of malformed heartbeat cases, default 6
  KEEP_WORK_DIR   set to 1 to preserve raw outputs in $WORK_DIR
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

ensure_test_agent() {
  local agent_id="$1"
  if ./ops/brigade-live.sh agent validate --id "$agent_id" >/dev/null 2>&1; then
    return 0
  fi
  ./ops/brigade-live.sh agent onboard \
    --id "$agent_id" \
    --name "${agent_id^^}" \
    --role test_worker \
    --provider "$PROVIDER" \
    --model "$MODEL" \
    >/dev/null
}

reset_test_agent() {
  # Scoped strictly to the dedicated test agent: archive any active assignments
  # it still owns (only this gate ever creates them) and force it back to idle,
  # so a prior aborted run cannot strand the agent and block every future run.
  local agent_id="$1"
  docker exec -i "$CONTAINER" python - "$agent_id" <<'PY'
import json
import sys

from brigade.config import load_settings
from brigade.schemas import AgentState
from brigade.store import open_state_store

agent_id = sys.argv[1]
store = open_state_store(load_settings())
archived = []
for item in store.assignments():
    if item.assigned_to == agent_id:
        store.archive_assignment(item, executive_summary="bad-heartbeat gate pre-run reset")
        archived.append(item.assignment_id)
previous = store.agent_states().get(agent_id)
store.upsert_agent_state(
    AgentState(
        agent=agent_id,
        status="idle",
        last_completed=previous.last_completed if previous else None,
    )
)
print(json.dumps({"reset_archived": archived}))
PY
}

ensure_test_agent "$AGENT"
reset_test_agent "$AGENT" >"$WORK_DIR/reset-before.json"
./ops/brigade-live.sh status --json >"$WORK_DIR/status-before.json"

python3 - "$WORK_DIR/status-before.json" "$AGENT" <<'PY'
import json
import sys
from pathlib import Path

status = json.loads(Path(sys.argv[1]).read_text())
agent = sys.argv[2]
agent_ids = {item.get("agent_id") for item in status.get("agents", [])}
if agent not in agent_ids:
    raise SystemExit(f"missing test agent: {agent}")
state = status.get("agent_states", {}).get(agent, {"status": "idle"})
if state.get("status") != "idle":
    raise SystemExit(f"test agent not idle: {agent} status={state.get('status')}")
PY

cases=(
  invalid_json
  duplicate_conflicting_block
  wrong_assigned_to
  missing_required_fields
  stale_assignment_id
  truncated_fence
)

get_heartbeat_path() {
  docker exec "$CONTAINER" python -c "from brigade.config import load_settings; from brigade.store import open_state_store; store=open_state_store(load_settings()); agent_id='$AGENT'; agents={a.agent_id: a.workspace_path for a in store.agents()}; workspace=agents.get(agent_id); import sys; print(f'{store.data_dir}/{workspace}/HEARTBEAT.md' if workspace else sys.exit(f'unknown agent: {agent_id}'))"
}

cleanup_assignment() {
  local assignment_id="$1"
  docker exec -i "$CONTAINER" python - "$assignment_id" "$AGENT" <<'PY'
import json
import sys

from brigade.config import load_settings
from brigade.schemas import AgentState
from brigade.store import open_state_store

assignment_id = sys.argv[1]
agent_id = sys.argv[2]
store = open_state_store(load_settings())
assignment = store.find_assignment(assignment_id)
archived = False
if assignment is not None:
    store.archive_assignment(
        assignment,
        executive_summary="malformed heartbeat test cleanup",
    )
    archived = True
remaining = [
    item
    for item in store.assignments()
    if item.assigned_to == agent_id and item.assignment_id != assignment_id
]
if not remaining:
    previous = store.agent_states().get(agent_id)
    store.upsert_agent_state(
        AgentState(
            agent=agent_id,
            status="idle",
            last_completed=previous.last_completed if previous else None,
        )
    )
print(json.dumps({"archived": archived, "remaining_for_agent": len(remaining)}))
PY
}

HEARTBEAT_PATH="$(get_heartbeat_path)"

for case_name in "${cases[@]}"; do
  case_dir="$WORK_DIR/$case_name"
  mkdir -p "$case_dir"

  ./ops/brigade-live.sh task create \
    --agent "$AGENT" \
    --assignment "heartbeat-$STAMP-$case_name" \
    --created-by heartbeat-script \
    --source bad_heartbeat_test \
    --priority normal \
    --work-mode heartbeat \
    --estimated-cycles 1 \
    >"$case_dir/task.json"
  assignment_id="$(
    python3 - "$case_dir/task.json" <<'PY'
import json
import sys
from pathlib import Path

print(json.loads(Path(sys.argv[1]).read_text())["assignment_id"])
PY
  )"

  ./ops/brigade-live.sh orchestrator cycle >"$case_dir/cycle.json"
  ./ops/brigade-live.sh status --json >"$case_dir/status-case-before.json"
  ./ops/brigade-live.sh alert list >"$case_dir/alerts-before.json"

  docker exec "$CONTAINER" cat "$HEARTBEAT_PATH" >"$case_dir/heartbeat-valid.md"

  python3 - "$case_name" "$case_dir/heartbeat-valid.md" "$case_dir/heartbeat-bad.md" <<'PY'
import json
import re
import sys
import uuid
from pathlib import Path

case_name = sys.argv[1]
source = Path(sys.argv[2]).read_text(encoding="utf-8")
target = Path(sys.argv[3])
marker = "```json brigade-assignment"
match = re.search(r"```json brigade-assignment\s*\n(.*?)\n```", source, re.DOTALL)
if not match:
    raise SystemExit("no assignment block found in valid heartbeat")
payload = json.loads(match.group(1))
sentinel = f"\n\nNOTE-SENTINEL-{case_name}\n"

if case_name == "invalid_json":
    replacement = f"{marker}\n{{ invalid json\n```"
elif case_name == "duplicate_conflicting_block":
    conflict = dict(payload)
    conflict["assigned_to"] = "test-scout"
    replacement = (
        source
        + sentinel
        + "\n"
        + marker
        + "\n"
        + json.dumps(conflict, indent=2, sort_keys=True)
        + "\n```"
    )
    target.write_text(replacement, encoding="utf-8")
    raise SystemExit(0)
elif case_name == "wrong_assigned_to":
    payload["assigned_to"] = "test-scout"
    replacement = f"{marker}\n{json.dumps(payload, indent=2, sort_keys=True)}\n```"
elif case_name == "missing_required_fields":
    payload.pop("assignment", None)
    replacement = f"{marker}\n{json.dumps(payload, indent=2, sort_keys=True)}\n```"
elif case_name == "stale_assignment_id":
    payload["assignment_id"] = str(uuid.uuid4())
    replacement = f"{marker}\n{json.dumps(payload, indent=2, sort_keys=True)}\n```"
elif case_name == "truncated_fence":
    replacement = f"{marker}\n{json.dumps(payload, indent=2, sort_keys=True)}\n"
else:
    raise SystemExit(f"unknown case: {case_name}")

prefix = source[: match.start()].rstrip() + sentinel + "\n\n"
target.write_text(prefix + replacement + "\n", encoding="utf-8")
PY

  docker cp "$case_dir/heartbeat-bad.md" "$CONTAINER:$HEARTBEAT_PATH"

  set +e
  ./ops/brigade-live.sh agent run --id "$AGENT" --provider "$PROVIDER" --model "$MODEL" \
    >"$case_dir/run.out" 2>"$case_dir/run.err"
  echo "$?" >"$case_dir/run.code"
  set -e

  docker exec "$CONTAINER" cat "$HEARTBEAT_PATH" >"$case_dir/heartbeat-after.md"
  ./ops/brigade-live.sh alert list >"$case_dir/alerts.json"
  ./ops/brigade-live.sh status --json >"$case_dir/status-after.json"

  docker cp "$case_dir/heartbeat-valid.md" "$CONTAINER:$HEARTBEAT_PATH"
  ./ops/brigade-live.sh agent run --id "$AGENT" --provider "$PROVIDER" --model "$MODEL" \
    >"$case_dir/cleanup.out" 2>"$case_dir/cleanup.err" || true
  cleanup_assignment "$assignment_id" >"$case_dir/archive-cleanup.json"
done

./ops/brigade-live.sh status --json >"$WORK_DIR/status-final.json"

python3 - "$WORK_DIR" "$REPORT_PATH" "$EXPECTED_CASE_COUNT" "$AGENT" <<'PY'
import json
import sys
from collections import Counter
from pathlib import Path

work_dir = Path(sys.argv[1])
report_path = Path(sys.argv[2])
expected_case_count = int(sys.argv[3])
agent_id = sys.argv[4]
cases = []
failures = []
created_ids = set()
for case_dir in sorted(path for path in work_dir.iterdir() if path.is_dir()):
    if case_dir.name.startswith("tmp"):
        continue
    code = int((case_dir / "run.code").read_text().strip())
    err_text = (case_dir / "run.err").read_text()
    out_text = (case_dir / "run.out").read_text()
    run_payload = json.loads(out_text) if out_text.strip() else {}
    heartbeat_after = (case_dir / "heartbeat-after.md").read_text(encoding="utf-8")
    alerts_before = json.loads((case_dir / "alerts-before.json").read_text())
    alerts_after = json.loads((case_dir / "alerts.json").read_text())
    before = json.loads((case_dir / "status-case-before.json").read_text())
    status = json.loads((case_dir / "status-after.json").read_text())
    task = json.loads((case_dir / "task.json").read_text())
    assignment_id = task["assignment_id"]
    created_ids.add(assignment_id)
    note_token = f"NOTE-SENTINEL-{case_dir.name}"
    before_usage = Counter(
        item.get("assignment_id")
        for item in before.get("usage_records", [])
        if item.get("assignment_id") == assignment_id
    )
    after_usage = Counter(
        item.get("assignment_id")
        for item in status.get("usage_records", [])
        if item.get("assignment_id") == assignment_id
    )
    before_history = Counter(
        item.get("assignment_id")
        for item in before.get("assignment_history", [])
        if item.get("assignment_id") == assignment_id
    )
    after_history = Counter(
        item.get("assignment_id")
        for item in status.get("assignment_history", [])
        if item.get("assignment_id") == assignment_id
    )
    before_transcripts = Counter(
        item.get("assignment_id")
        for item in before.get("transcripts", [])
        if item.get("assignment_id") == assignment_id
    )
    after_transcripts = Counter(
        item.get("assignment_id")
        for item in status.get("transcripts", [])
        if item.get("assignment_id") == assignment_id
    )
    alert_delta = len(alerts_after) - len(alerts_before)
    error_text = (err_text + "\n" + out_text).lower()
    degraded_cleanly = run_payload.get("status") == "blocked" and "traceback" not in error_text
    side_effect_free = all(
        (
            after_usage.get(assignment_id, 0) == before_usage.get(assignment_id, 0),
            after_history.get(assignment_id, 0) == before_history.get(assignment_id, 0),
            after_transcripts.get(assignment_id, 0) == before_transcripts.get(assignment_id, 0),
        )
    )
    case_summary = {
        "case": case_dir.name,
        "assignment_id": assignment_id,
        "exit_code": code,
        "run_status": run_payload.get("status"),
        "note_preserved": note_token in heartbeat_after,
        "alerts_delta": alert_delta,
        "degraded_cleanly": degraded_cleanly,
        "side_effect_free": side_effect_free,
        "usage_records_after": after_usage.get(assignment_id, 0),
        "history_records_after": after_history.get(assignment_id, 0),
        "transcript_records_after": after_transcripts.get(assignment_id, 0),
        "agent_state_after": status.get("agent_states", {}).get(agent_id),
        "stdout_excerpt": out_text[:300],
        "stderr_excerpt": err_text[:300],
    }
    if code != 0:
        failures.append(f"{case_dir.name}: expected structured blocked result for malformed heartbeat")
    if not case_summary["note_preserved"]:
        failures.append(f"{case_dir.name}: surrounding notes were not preserved")
    if not case_summary["degraded_cleanly"]:
        failures.append(f"{case_dir.name}: malformed heartbeat degraded via traceback or unclear error")
    if case_summary["alerts_delta"] <= 0:
        failures.append(f"{case_dir.name}: expected alert emission for malformed heartbeat")
    if not case_summary["side_effect_free"]:
        failures.append(f"{case_dir.name}: malformed heartbeat created completion side effects")
    cases.append(
        case_summary
    )

if len(cases) != expected_case_count:
    failures.append(
        f"expected {expected_case_count} malformed heartbeat cases, saw {len(cases)}"
    )

final_status = json.loads((work_dir / "status-final.json").read_text())
lingering = sorted(
    item.get("assignment_id")
    for item in final_status.get("assignments", [])
    if item.get("assignment_id") in created_ids
)
final_agent_state = final_status.get("agent_states", {}).get(agent_id)
if lingering:
    failures.append(
        "cleanup left active malformed heartbeat assignments: "
        + ", ".join(lingering)
    )
if final_agent_state and final_agent_state.get("status") != "idle":
    failures.append(
        f"test agent cleanup left {agent_id} status={final_agent_state.get('status')}"
    )

report = {
    "cases": cases,
    "cleanup": {
        "active_assignment_ids": lingering,
        "agent_state": final_agent_state,
    },
    "invariant_failures": failures,
    "invariants_ok": not failures,
}
report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
print(json.dumps(report, indent=2, sort_keys=True))
if failures:
    raise SystemExit(1)
PY

echo
if [[ "$KEEP_WORK_DIR" == "1" ]]; then
  echo "report_path=$REPORT_PATH"
fi
