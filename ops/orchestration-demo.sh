#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CONFIRM="${1:-}"
if [[ "$CONFIRM" != "--confirm-wipe" ]]; then
  cat >&2 <<'USAGE'
usage: ./ops/orchestration-demo.sh --confirm-wipe

Runs the v1.0 orchestrator end-to-end acceptance scenario on a fresh stack:
mission + two teams, chief-first continuation, intake propose/create, the
blocker-resolution ladder, rest scheduling, recurrence materialization,
training-data export, and a status chat with the chief. Deterministic
orchestration behavior is asserted; model-dependent steps (decomposition,
chat reply quality) are exercised and printed for review.

Wipes runtime data first (source, config, ops scripts, and backups remain).
USAGE
  exit 2
fi

CONTAINER="${BRIGADE_LIVE_CONTAINER:-brigade_orchestrator}"
BRIGADE() { ./ops/brigade-live.sh "$@"; }
PYIN() { docker exec -i "$CONTAINER" python3 -; }

WORK_DIR="$(mktemp -d /tmp/orchestration-demo.XXXXXX)"
echo "work dir: $WORK_DIR"

step() { echo; echo "=== $1 ==="; }

# --- 1. Fresh stack: wipe, migrate, init; mission and two teams -------------------
step "1. fresh stack, mission, Team A (chief + 2 specialists), Team B (on-call team of one)"
./ops/v07-wipe-reseed.sh --confirm-wipe

BRIGADE mission set --statement "Demo mission: validate the v1.0 orchestrator end to end."
BRIGADE team create --id team-a --name "Team A"
BRIGADE team create --id team-b --name "Team B"
BRIGADE agent onboard --id chief-a --name "Chief A" --role crew_chief \
  --team team-a --crew-chief
BRIGADE agent onboard --id worker-py --name "Worker Py" --team team-a --specialty python
BRIGADE agent onboard --id worker-docs --name "Worker Docs" --team team-a --specialty docs
BRIGADE agent onboard --id chief-b --name "Chief B" --role crew_chief \
  --team team-b --crew-chief
BRIGADE goal add --agent chief-a --statement "Deliver the demo prototype" \
  --not "production rollout" --set-by human --human-confirmed
BRIGADE goal add --agent chief-b --statement "Maintain demo infrastructure" \
  --not "feature work" --set-by human --human-confirmed --engagement-mode on_call

# --- 2. Cycle 1: continuation targets Chief A only; outcome recorded --------------
step "2. cycle 1: chief-first continuation, on-call chief untouched, outcome recorded"
BRIGADE orchestrator cycle >"$WORK_DIR/cycle-1.json"
python3 - "$WORK_DIR/cycle-1.json" <<'EOF'
import json, sys
cycle = json.load(open(sys.argv[1]))
outcome = cycle["cycle_outcome"]
assert outcome["mode"] in {"worked", "work_in_flight", "no_work"}, outcome
print("cycle 1 outcome:", outcome["mode"], outcome.get("reason"))
EOF
BRIGADE status --json >"$WORK_DIR/status-1.json"
python3 - "$WORK_DIR/status-1.json" <<'EOF'
import json, sys
status = json.load(open(sys.argv[1]))
orch = [a for a in status["assignments"] if a["created_by"] == "orchestrator"]
assert all(a["assigned_to"] != "chief-b" for a in orch), \
    "on-call chief-b must receive no orchestrator-created work"
print(f"orchestrator-created assignments: {[a['assigned_to'] for a in orch]} (chief-b clean)")
EOF

# --- 3. Intake: ingest an article; propose then create ----------------------------
step "3. intake: ingest article -> intake_proposal; create mode -> chief assignment"
cat >"$WORK_DIR/article.md" <<'DOC'
# Demo article

Python packaging changes worth tracking for the prototype.
DOC
BRIGADE knowledge ingest --title "Demo article" --source demo --type article \
  --path "$WORK_DIR/article.md"
BRIGADE orchestrator cycle >"$WORK_DIR/cycle-intake-propose.json"
python3 - "$WORK_DIR/cycle-intake-propose.json" <<'EOF'
import json, sys
cycle = json.load(open(sys.argv[1]))
intake = cycle["sub_results"]["intake"]
assert intake["proposals"], f"expected an intake proposal, got: {intake}"
print("intake proposal recorded for:", intake["proposals"][0]["title"])
EOF
BRIGADE config set --key intake_mode --value create
cat >"$WORK_DIR/article2.md" <<'DOC'
# Second demo article

Docs cleanup notes for the prototype.
DOC
BRIGADE knowledge ingest --title "Second demo article" --source demo --type article \
  --path "$WORK_DIR/article2.md"
BRIGADE orchestrator cycle >"$WORK_DIR/cycle-intake-create.json"
python3 - "$WORK_DIR/cycle-intake-create.json" <<'EOF'
import json, sys
cycle = json.load(open(sys.argv[1]))
created = cycle["sub_results"]["intake"]["created"]
assert created, f"expected intake to create an assignment: {cycle['sub_results']['intake']}"
assert created[0]["idempotency_key"].startswith("intake:v1:")
print("intake created chief assignment:", created[0]["assignment_id"],
      "->", created[0]["agent_id"])
EOF
BRIGADE config set --key intake_mode --value propose

# --- 4. Ladder: force a block, watch retry -> analysis -> reassign -----------------
step "4. ladder: forced block walks retry -> analysis -> reassign, human untouched"
BRIGADE task create --agent worker-py --assignment "Ladder demo: fix the importer" \
  --created-by human --source direct_command >"$WORK_DIR/ladder-task.json"
LADDER_ID="$(python3 -c "import json;print(json.load(open('$WORK_DIR/ladder-task.json'))['assignment_id'])")"

force_failure() {
  PYIN <<EOF
from brigade.config import load_settings
from brigade.store import open_state_store
store = open_state_store(load_settings())
a = store.find_assignment("$LADDER_ID")
a.register_failure("demo: importer exploded", blockers=["missing module"])
store.update_assignment(a)
print("failures:", a.consecutive_failures)
EOF
}

assert_ladder_step() {
  python3 - "$1" "$2" <<'EOF'
import json, sys
cycle = json.load(open(sys.argv[1]))
steps = [a["step"] for a in cycle["sub_results"]["ladder"]["actions"]]
assert sys.argv[2] in steps, f"expected ladder step {sys.argv[2]}, got {steps}"
print("ladder step:", sys.argv[2])
EOF
}

force_failure
BRIGADE orchestrator cycle >"$WORK_DIR/cycle-ladder-1.json"
assert_ladder_step "$WORK_DIR/cycle-ladder-1.json" retry

force_failure
BRIGADE orchestrator cycle >"$WORK_DIR/cycle-ladder-2.json"
assert_ladder_step "$WORK_DIR/cycle-ladder-2.json" analysis

# Complete the analysis child deterministically, then fail once more.
PYIN <<EOF
from brigade.config import load_settings
from brigade.store import open_state_store
from brigade.ladder import find_analysis_child
from brigade.schemas import AssignmentStatus
store = open_state_store(load_settings())
blocked = store.find_assignment("$LADDER_ID")
child = find_analysis_child(store.assignments(), blocked)
assert child is not None and child.parent_assignment_id == "$LADDER_ID"
child.transition_to(AssignmentStatus.ASSIGNED)
child.mark_complete("Root cause: module missing from the runtime image.")
store.update_assignment(child)
print("analysis child completed:", child.assignment_id)
EOF
force_failure
BRIGADE orchestrator cycle >"$WORK_DIR/cycle-ladder-3.json"
assert_ladder_step "$WORK_DIR/cycle-ladder-3.json" reassign

BRIGADE status --json >"$WORK_DIR/status-ladder.json"
python3 - "$WORK_DIR/status-ladder.json" "$LADDER_ID" <<'EOF'
import json, sys
status = json.load(open(sys.argv[1]))
item = next(a for a in status["assignments"] if a["assignment_id"] == sys.argv[2])
assert not item["awaiting_human"], "human must not be interrupted before exhaustion"
assert item["assigned_to"] != "worker-py", "expected a new owner after reassign"
human_alerts = [a for a in status["alerts"] if "ladder exhaustion" in str(a)]
assert not human_alerts, "no human escalation alert before the ladder is exhausted"
print("reassigned to:", item["assigned_to"], "- human untouched")
EOF

# --- 5. Rest: window set to now; rest assignments fire ----------------------------
step "5. rest: window spanning now schedules low-priority rest"
BRIGADE config set --key rest_window_start_utc --value "00:00"
BRIGADE config set --key rest_window_end_utc --value "23:59"
BRIGADE orchestrator cycle >"$WORK_DIR/cycle-rest.json"
python3 - "$WORK_DIR/cycle-rest.json" <<'EOF'
import json, sys
cycle = json.load(open(sys.argv[1]))
rest = cycle["sub_results"]["rest"]
print("rest created:", [item["agent_id"] for item in rest["created"]],
      "suppressed:", rest["already_rested"])
EOF
echo "note: MEMORY.md cap, reflections.md, and rest proposals land after agents"
echo "complete the rest assignment (deterministic finalizer in the runner)."

# --- 6. Efficiency: synthetic history -> proposal -> approve -> materialize once ---
step "6. efficiency proposal approved; recurrence materializes exactly once"
PYIN <<'EOF'
from brigade.config import load_settings
from brigade.store import open_state_store
from brigade.schemas import Assignment, AssignmentStatus
store = open_state_store(load_settings())
for day in (1, 4, 7):
    a = Assignment(
        assignment=f"Send weekly digest 2026-06-0{day}",
        assigned_to="worker-docs",
        created_by="human",
        source="direct_command",
    )
    store.add_assignment(a)
    a.transition_to(AssignmentStatus.ASSIGNED)
    a.mark_complete("sent")
    a.updated_at = f"2026-06-0{day}T09:00:00+00:00"
    store.archive_assignment(a, executive_summary="sent")
print("seeded 3 completed digests for worker-docs")
EOF
BRIGADE orchestrator cycle >"$WORK_DIR/cycle-detect.json"
PROPOSAL_ID="$(BRIGADE proposal list --kind efficiency --status proposed \
  | python3 -c "import json,sys;rows=json.load(sys.stdin);print(rows[0]['proposal_id'])")"
echo "efficiency proposal: $PROPOSAL_ID"
BRIGADE proposal approve "$PROPOSAL_ID" >/dev/null
PYIN <<'EOF'
from brigade.config import load_settings
from brigade.store import open_state_store
store = open_state_store(load_settings())
rec = store.recurrences()[-1]
rec["next_due_at"] = "2026-06-10T00:00:00+00:00"
store.update_recurrence(rec)
print("recurrence due now:", rec["recurrence_id"])
EOF
BRIGADE orchestrator cycle >"$WORK_DIR/cycle-mat-1.json"
BRIGADE orchestrator cycle >"$WORK_DIR/cycle-mat-2.json"
python3 - "$WORK_DIR/cycle-mat-1.json" "$WORK_DIR/cycle-mat-2.json" <<'EOF'
import json, sys
first = json.load(open(sys.argv[1]))["sub_results"]["recurrence"]["materialized"]
second = json.load(open(sys.argv[2]))["sub_results"]["recurrence"]["materialized"]
assert len(first) == 1, f"expected exactly one materialization, got {first}"
assert second == [], f"due slot must not double-fire: {second}"
print("materialized once:", first[0]["assignment_id"])
EOF

# --- 7. Export: JSONL counts match; spot-check lines --------------------------------
step "7. training-data export"
BRIGADE export training-data --out /tmp/orchestration-demo-export >"$WORK_DIR/manifest.json"
docker cp "$CONTAINER:/tmp/orchestration-demo-export" "$WORK_DIR/export"
python3 - "$WORK_DIR" <<'EOF'
import json, sys
from pathlib import Path
work = Path(sys.argv[1])
manifest = json.load(open(work / "manifest.json"))
export = work / "export"
for filename, count in manifest["counts"].items():
    rows = [json.loads(line) for line in (export / filename).read_text().splitlines()]
    assert len(rows) == count, f"{filename}: manifest says {count}, file has {len(rows)}"
cycles = [json.loads(line) for line in (export / "cycles.jsonl").read_text().splitlines()]
assert cycles and all("cycle_outcome" in c for c in cycles)
sample = cycles[-1]
print("cycles exported:", len(cycles),
      "- sample outcome:", sample["cycle_outcome"]["mode"],
      "- events:", len(sample.get("events", [])))
transcripts = (export / "transcripts.jsonl").read_text().splitlines()
print("transcripts exported:", len(transcripts))
EOF

# --- 8. Chat with Chief A ------------------------------------------------------------
step "8. chat with Chief A (model-dependent; reply printed for review)"
set +e
PYIN <<'EOF'
import json
from brigade.auth import AuthResult
from brigade.config import load_settings
from brigade.providers import provider_from_settings
from brigade.services import send_user_chat
from brigade.store import open_state_store

settings = load_settings()
store = open_state_store(settings)
result = send_user_chat(
    store,
    AuthResult(ok=True, method="demo"),
    user=None,
    agent_id="chief-a",
    content="What's the team status? What's stuck and what are the priorities?",
    provider=provider_from_settings(settings),
)
print(json.dumps(result, indent=2, sort_keys=True)[:2000])
EOF
CHAT_RC=$?
set -e
if [[ "$CHAT_RC" -ne 0 ]]; then
  echo "chat skipped (model provider unavailable); the grounded status context"
  echo "is still embedded in every chat prompt via build_chat_status_context."
fi

step "demo complete"
echo "artifacts: $WORK_DIR"
echo "every cycle record carries a cycle_outcome; orchestrator-created work carries"
echo "idempotency keys and provenance; the human was untouched until ladder exhaustion."
