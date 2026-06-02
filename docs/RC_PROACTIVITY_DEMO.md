# RC Proactivity Demo

Use this recipe to capture the RC proactivity proof without relying on ad hoc operator memory.

## Setup

Run against the live stack through `./ops/brigade-live.sh` so tasks, reasoning, usage, and alerts
land in the configured stores.

```bash
./ops/brigade-live.sh init mvp --force --mission "Prove OpenBrigade can advance a mission without a human assigning each task"
./ops/brigade-live.sh goal add --agent sage --statement "Create a short validation plan" --success "tracked delegated work exists" --not "spawn dynamic agents" --human-confirmed
./ops/brigade-live.sh orchestrator daemon --max-cycles 2 --sleep-seconds 1
./ops/brigade-live.sh task list --status queued
./ops/brigade-live.sh dashboard --plain --view tasks
```

## Evidence To Capture

- The orchestrator creates idle-agent or Crew Chief planning work from a confirmed goal/mission.
- The assigned agent creates tracked follow-up work with `delegate` or `create_subtasks`.
- Child assignments retain `parent_assignment_id`; ordered subtasks include `dependency_ids`.
- Malformed escalation output, if tested with a weak model, records an alert and returns `no_action`
  instead of crashing the daemon.
- Ops Room or dashboard output shows the resulting mission, task, and alert state.

## Pass Criteria

The demo is RC-defensible when a fresh checkout can reproduce proactive task creation, tracked
delegation/decomposition, safe stale-work escalation behavior, and visible GUI or dashboard state
using documented commands.
