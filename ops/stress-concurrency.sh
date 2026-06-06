#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

AGENT="${AGENT:-test-stress-builder}"
SECOND_AGENT="${SECOND_AGENT:-test-stress-scout}"
PROVIDER="${PROVIDER:-ollama}"
MODEL="${MODEL:-gpt-oss:20b}"
INCLUDE_RUN_ALL="${INCLUDE_RUN_ALL:-0}"
KEEP_WORK_DIR="${KEEP_WORK_DIR:-0}"
WORK_DIR="$(mktemp -d /tmp/openbrigade-stress.XXXXXX)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
REPORT_PATH="$WORK_DIR/report.json"
if [[ -z "${CLEANUP_PROVIDER:-}" ]]; then
  CLEANUP_PROVIDER="$PROVIDER"
else
  CLEANUP_PROVIDER="${CLEANUP_PROVIDER}"
fi
if [[ -z "${PRIMARY_CLEANUP_PASSES:-}" ]]; then
  if [[ "$PROVIDER" == "ollama" ]]; then
    PRIMARY_CLEANUP_PASSES=6
  else
    PRIMARY_CLEANUP_PASSES=3
  fi
else
  PRIMARY_CLEANUP_PASSES="${PRIMARY_CLEANUP_PASSES}"
fi
CLEANUP_SLEEP_SECONDS="${CLEANUP_SLEEP_SECONDS:-2}"

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
  AGENT             primary test agent, default test-stress-builder
  SECOND_AGENT      secondary test agent, default test-stress-scout
  PROVIDER          brigade provider, default ollama
  MODEL             model name, default gpt-oss:20b
  INCLUDE_RUN_ALL   set to 1 to include agent run-all in the concurrency burst
  CLEANUP_PROVIDER  provider used to drain leftovers, default PROVIDER
  PRIMARY_CLEANUP_PASSES   cleanup attempts per test agent, default 6 for ollama else 3
  CLEANUP_SLEEP_SECONDS    sleep between cleanup attempts, default 2
  KEEP_WORK_DIR     set to 1 to preserve raw outputs in $WORK_DIR
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

printf '%s\n' "$PRIMARY_CLEANUP_PASSES" >"$WORK_DIR/cleanup-passes.txt"
printf '%s\n' "$CLEANUP_SLEEP_SECONDS" >"$WORK_DIR/cleanup-sleep-seconds.txt"
printf '%s\n' "$PROVIDER" >"$WORK_DIR/provider.txt"
printf '%s\n' "$CLEANUP_PROVIDER" >"$WORK_DIR/cleanup-provider.txt"

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

ensure_test_agent "$AGENT"
ensure_test_agent "$SECOND_AGENT"
./ops/brigade-live.sh status --json >"$WORK_DIR/status-before.json"

python3 - "$WORK_DIR/status-before.json" "$AGENT" "$SECOND_AGENT" <<'PY'
import json
import sys
from pathlib import Path

status = json.loads(Path(sys.argv[1]).read_text())
agent_ids = {item.get("agent_id") for item in status.get("agents", [])}
for agent in sys.argv[2:]:
    if agent not in agent_ids:
        raise SystemExit(f"missing test agent: {agent}")
    state = status.get("agent_states", {}).get(agent, {"status": "idle"})
    if state.get("status") != "idle":
        raise SystemExit(f"test agent not idle: {agent} status={state.get('status')}")
PY

./ops/brigade-live.sh task create \
  --agent "$AGENT" \
  --assignment "stress-$STAMP primary assignment" \
  --created-by stress-script \
  --source stress_concurrency \
  --priority normal \
  --work-mode heartbeat \
  --estimated-cycles 1 \
  >"$WORK_DIR/primary-create.json"

./ops/brigade-live.sh task create \
  --agent "$SECOND_AGENT" \
  --assignment "stress-$STAMP secondary assignment" \
  --created-by stress-script \
  --source stress_concurrency \
  --priority normal \
  --work-mode heartbeat \
  --estimated-cycles 1 \
  >"$WORK_DIR/secondary-create.json"

./ops/brigade-live.sh orchestrator cycle >"$WORK_DIR/initial-cycle.json"

run_async() {
  local name="$1"
  shift
  (
    set +e
    "$@" >"$WORK_DIR/$name.out" 2>"$WORK_DIR/$name.err"
    echo "$?" >"$WORK_DIR/$name.code"
  ) &
  LAST_PID=$!
}

LAST_PID=""
pids=()
run_async run-primary ./ops/brigade-live.sh agent run --id "$AGENT" --provider "$PROVIDER" --model "$MODEL"
pids+=("$LAST_PID")
run_async run-primary-dup ./ops/brigade-live.sh agent run --id "$AGENT" --provider "$PROVIDER" --model "$MODEL"
pids+=("$LAST_PID")
run_async cycle-during-run ./ops/brigade-live.sh orchestrator cycle
pids+=("$LAST_PID")

if [[ "$INCLUDE_RUN_ALL" == "1" ]]; then
  run_async run-all ./ops/brigade-live.sh agent run-all --provider "$PROVIDER" --model "$MODEL"
  pids+=("$LAST_PID")
fi

for pid in "${pids[@]}"; do
  wait "$pid"
done

./ops/brigade-live.sh status --json >"$WORK_DIR/status-after.json"

python3 - "$WORK_DIR/status-after.json" "$WORK_DIR/primary-create.json" "$WORK_DIR/secondary-create.json" <<'PY' >"$WORK_DIR/cleanup-targets.txt"
import json
import sys
from pathlib import Path

after = json.loads(Path(sys.argv[1]).read_text())
created = {
    json.loads(Path(sys.argv[2]).read_text())["assignment_id"],
    json.loads(Path(sys.argv[3]).read_text())["assignment_id"],
}

targets = {
    record["assigned_to"]
    for record in after.get("assignments", [])
    if record.get("assignment_id") in created
}
for agent_id in sorted(targets):
    print(agent_id)
PY

while IFS= read -r cleanup_agent; do
  [[ -n "$cleanup_agent" ]] || continue
  for pass in $(seq 1 "$PRIMARY_CLEANUP_PASSES"); do
    ./ops/brigade-live.sh agent run --id "$cleanup_agent" --provider "$CLEANUP_PROVIDER" --model "$MODEL" \
      >"$WORK_DIR/cleanup-$cleanup_agent-$pass.out" 2>"$WORK_DIR/cleanup-$cleanup_agent-$pass.err" || true
    ./ops/brigade-live.sh status --json >"$WORK_DIR/status-after-cleanup-check.json"
    if python3 - "$WORK_DIR" "$cleanup_agent" <<'PY'
import json
import sys
from pathlib import Path

work_dir = Path(sys.argv[1])
cleanup_agent = sys.argv[2]
status = json.loads((work_dir / "status-after-cleanup-check.json").read_text())
for record in status.get("assignments", []):
    if record.get("assigned_to") == cleanup_agent:
        raise SystemExit(1)
PY
    then
      break
    fi
    sleep "$CLEANUP_SLEEP_SECONDS"
  done
done <"$WORK_DIR/cleanup-targets.txt"

./ops/brigade-live.sh status --json >"$WORK_DIR/status-after-cleanup.json"

python3 - "$WORK_DIR" "$REPORT_PATH" "$INCLUDE_RUN_ALL" <<'PY'
import json
import sys
from collections import Counter
from pathlib import Path

work_dir = Path(sys.argv[1])
report_path = Path(sys.argv[2])
include_run_all = sys.argv[3] == "1"

before = json.loads((work_dir / "status-before.json").read_text())
after = json.loads((work_dir / "status-after.json").read_text())
after_cleanup = json.loads((work_dir / "status-after-cleanup.json").read_text())
primary = json.loads((work_dir / "primary-create.json").read_text())
secondary = json.loads((work_dir / "secondary-create.json").read_text())

job_names = ["run-primary", "run-primary-dup", "cycle-during-run"]
if include_run_all:
    job_names.append("run-all")

jobs = []
for name in job_names:
    code = int((work_dir / f"{name}.code").read_text().strip())
    stdout = (work_dir / f"{name}.out").read_text()
    stderr = (work_dir / f"{name}.err").read_text()
    jobs.append(
        {
            "name": name,
            "exit_code": code,
            "stdout_excerpt": stdout[:500],
            "stderr_excerpt": stderr[:500],
        }
    )

job_behavior = {}
for job in jobs:
    output = (job["stdout_excerpt"] + "\n" + job["stderr_excerpt"]).lower()
    if "assignment already being executed by" in output:
        job_behavior[job["name"]] = "execution_claim_backoff"
    elif "local inference unavailable until" in output:
        job_behavior[job["name"]] = "local_inference_cooldown"
    elif "\"status\": \"blocked\"" in output and "cloud dispatch blocked" in output:
        job_behavior[job["name"]] = "blocked"
    elif job["exit_code"] != 0:
        job_behavior[job["name"]] = "error"
    else:
        job_behavior[job["name"]] = "completed_or_noop"

transcript_paths = [
    item.get("path")
    for item in after.get("transcripts", [])
    if item.get("assignment_id") in {primary["assignment_id"], secondary["assignment_id"]}
]
duplicates = {
    path: count
    for path, count in Counter(transcript_paths).items()
    if path and count > 1
}

created_ids = {primary["assignment_id"], secondary["assignment_id"]}
active_ids = {item["assignment_id"] for item in after.get("assignments", [])}
history_ids = {item["assignment_id"] for item in after.get("assignment_history", [])}
active_ids_after_cleanup = {
    item["assignment_id"] for item in after_cleanup.get("assignments", [])
}
history_ids_after_cleanup = {
    item["assignment_id"] for item in after_cleanup.get("assignment_history", [])
}

usage_counts = Counter(
    item.get("assignment_id")
    for item in after_cleanup.get("usage_records", [])
    if item.get("assignment_id") in created_ids
)
history_counts = Counter(
    item.get("assignment_id")
    for item in after_cleanup.get("assignment_history", [])
    if item.get("assignment_id") in created_ids
)
transcript_counts = Counter(
    item.get("assignment_id")
    for item in after_cleanup.get("transcripts", [])
    if item.get("assignment_id") in created_ids
)
transcript_paths_after_cleanup = [
    item.get("path")
    for item in after_cleanup.get("transcripts", [])
    if item.get("assignment_id") in created_ids
]
duplicates_after_cleanup = {
    path: count
    for path, count in Counter(transcript_paths_after_cleanup).items()
    if path and count > 1
}
cleanup_agents = [
    line.strip()
    for line in (work_dir / "cleanup-targets.txt").read_text(encoding="utf-8").splitlines()
    if line.strip()
]

summary = {
    "created_assignments": {
        "primary": primary["assignment_id"],
        "secondary": secondary["assignment_id"],
    },
    "jobs": jobs,
    "history_delta": len(after.get("assignment_history", [])) - len(before.get("assignment_history", [])),
    "usage_delta": len(after.get("usage_records", [])) - len(before.get("usage_records", [])),
    "active_assignments_after": sorted(created_ids & active_ids),
    "archived_assignments_after": sorted(created_ids & history_ids),
    "active_assignments_after_cleanup": sorted(created_ids & active_ids_after_cleanup),
    "archived_assignments_after_cleanup": sorted(created_ids & history_ids_after_cleanup),
    "cleanup_agents": cleanup_agents,
    "usage_counts_after_cleanup": {
        assignment_id: usage_counts.get(assignment_id, 0) for assignment_id in sorted(created_ids)
    },
    "history_counts_after_cleanup": {
        assignment_id: history_counts.get(assignment_id, 0) for assignment_id in sorted(created_ids)
    },
    "transcript_counts_after_cleanup": {
        assignment_id: transcript_counts.get(assignment_id, 0) for assignment_id in sorted(created_ids)
    },
    "duplicate_transcript_paths": duplicates,
    "duplicate_transcript_paths_after_cleanup": duplicates_after_cleanup,
    "job_behavior": job_behavior,
    "provider": (work_dir / "provider.txt").read_text().strip(),
    "cleanup_provider": (work_dir / "cleanup-provider.txt").read_text().strip(),
    "cleanup_policy": {
        "passes": int((work_dir / "cleanup-passes.txt").read_text().strip()),
        "sleep_seconds": float((work_dir / "cleanup-sleep-seconds.txt").read_text().strip()),
    },
    "agent_states_after": {
        key: after["agent_states"].get(key)
        for key in sorted(after.get("agent_states", {}))
        if key in {"test-builder", "test-scout"}
    },
    "agent_states_after_cleanup": {
        key: after_cleanup["agent_states"].get(key)
        for key in sorted(after_cleanup.get("agent_states", {}))
        if key in {"test-builder", "test-scout"}
    },
}

failures = []
for assignment_id in sorted(created_ids):
    if summary["usage_counts_after_cleanup"][assignment_id] < 1:
        failures.append(
            f"expected at least one usage record for {assignment_id}, "
            f"saw {summary['usage_counts_after_cleanup'][assignment_id]}"
        )
    if summary["history_counts_after_cleanup"][assignment_id] != 1:
        failures.append(
            f"expected exactly one archived history record for {assignment_id}, "
            f"saw {summary['history_counts_after_cleanup'][assignment_id]}"
        )
    if summary["transcript_counts_after_cleanup"][assignment_id] < 1:
        failures.append(
            f"expected at least one transcript record for {assignment_id}, "
            f"saw {summary['transcript_counts_after_cleanup'][assignment_id]}"
        )

if summary["duplicate_transcript_paths"] or summary["duplicate_transcript_paths_after_cleanup"]:
    failures.append(
        "duplicate transcript paths detected: "
        + json.dumps(
            {
                "before_cleanup": summary["duplicate_transcript_paths"],
                "after_cleanup": summary["duplicate_transcript_paths_after_cleanup"],
            },
            sort_keys=True,
        )
    )
if summary["active_assignments_after_cleanup"]:
    failures.append(
        "lingering test-agent assignments after cleanup: "
        + ", ".join(summary["active_assignments_after_cleanup"])
    )

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
