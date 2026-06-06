# RC Proactivity Demo

Use this recipe to capture the RC proactivity proof without relying on ad hoc operator memory.

## Setup

Run against the live stack through `./ops/brigade-live.sh` so tasks, reasoning, usage, and alerts
land in the configured stores.

```bash
./ops/brigade-live.sh init mvp --force --mission "Prove OpenBrigade can advance a mission without a human assigning each task"
./ops/brigade-live.sh goal add --agent sage --statement "Create a short validation plan" --success "tracked delegated work exists" --not "spawn dynamic agents" --human-confirmed
./ops/brigade-live.sh orchestrator cycle
./ops/brigade-live.sh status --json
```

## Evidence To Capture

- The orchestrator records one Crew Chief-level `proactive_proposal` for the idle mission.
- The proposal includes mission, trigger, Crew Chief, and `orchestrator-proactive:v1:<sha256>`
  idempotency provenance in `orchestrator_reasoning`.
- Cockpit Orchestration Activity and the Ops Room Orchestrator room show the latest proposal.
- No task is created in default propose-only mode.
- The assigned agent creates tracked follow-up work with `delegate` or `create_subtasks`.
- Child assignments retain `parent_assignment_id`; ordered subtasks include `dependency_ids`.
- Malformed escalation output, if tested with a weak model, records an alert and returns `no_action`
  instead of crashing the daemon.
- Ops Room or dashboard output shows the resulting mission, task, and alert state.

## Pass Criteria

The demo is RC-defensible when a fresh checkout can reproduce visible propose-only mission
continuation, tracked delegation/decomposition after an operator accepts or creates follow-up work,
safe stale-work escalation behavior, and visible GUI or dashboard state using documented commands.
Actual automatic task creation is a separately gated test that requires `proactive_mode=create` and
`proactive_creation_enabled=true`.
