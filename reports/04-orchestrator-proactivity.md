# 04 — Orchestrator & Proactivity

Your stated concern: *"The Orchestrator and Agents being proactive."* Good news — this is
implemented and is arguably OpenBrigade's signature capability. It is more structured than
anything in the proactive reference set.

## What OpenBrigade has

All in [`brigade/orchestrator.py`](../brigade/orchestrator.py) unless noted.

1. **Deterministic dispatch cycle** — `deterministic_cycle`: priority-ranked, human-first
   ordering; dependency gating (waits on incomplete `dependency_ids`); goal-alignment veto
   via [`brigade/meta.py`](../brigade/meta.py) `evaluate_assignment_alignment` (interrupts
   work that crosses a goal's `explicitly_not` boundary); writes a `HEARTBEAT.md` row per
   assignment.

2. **Proactive idle-agent task creation** — `build_idle_agent_assignments`: when an agent
   is idle but has a confirmed goal, the orchestrator *synthesizes* an "Advance goal: …"
   assignment. When a **Crew Chief** is idle and a mission exists, it creates a
   "Build the next concrete task plan for the current mission…" assignment. Idempotency
   keys prevent duplicates. **This is genuine self-initiated work.**

3. **Floor predicates** — `evaluate_orchestrator_floor`: detects **stale goals**, **stale
   tasks** (no movement past `stale_seconds`, respecting future checkpoints), and **crew-
   chief load imbalance** (one chief overloaded while another is idle).

4. **LLM escalation** — `run_orchestrator_escalation`: when predicates fire, it builds a
   context prompt (floor + triggers + targeted provenance + knowledge snippets) and lets
   the model return bounded actions: `create_assignment`, `rebalance_queued_assignment`,
   `request_human`. Actions are validated and applied (`apply_orchestrator_actions`), with
   rejects audited. It explicitly **refuses to move active work** — only queued items.
   Malformed model responses now degrade to `no_action` instead of crashing the daemon path.

5. **Stalled-goal proposal** — CLI `orchestrator propose-stalled-goals`
   ([`cli.py:1882`](../brigade/cli.py)).

6. **Autonomous daemon** — `orchestrator daemon` ([`cli.py:1888`](../brigade/cli.py)): loops
   on `orchestrator_cadence_seconds`, runs a cycle, then runs the managed agents. This is
   the background loop that makes the system proactive without a human in the seat.

7. **Auditability** — every cycle writes an orchestrator-reasoning record
   (`build_cycle_reasoning_record`) and usage record; the GUI ops-room surfaces it.

## How this compares

| Capability | OpenBrigade | proactive-claw / Leo / Thu | OpenClaw | Hermes |
|---|---|---|---|---|
| Background loop / cadence | ✅ daemon | ✅ daemon / wake-loop | ✅ task registry + cron | ✅ cron + kanban |
| Idle detection → self-initiated work | ✅ | ✅ (scoring) | ✅ | ✅ |
| Stale-work detection | ✅ | partial | ✅ | ✅ |
| Goal-alignment veto (`explicitly_not`) | ✅ | only v1.0 proactive | — | — |
| LLM escalation w/ bounded actions | ✅ | ❌ | ✅ | ✅ (kanban) |
| Load rebalancing across chiefs | ✅ | ❌ | partial | ✅ |
| Cost-gated dispatch (ABACUS) | ⚠️ partial (see report 06) | ❌ | partial | partial |

The proactive reference set (`proactive-claw`, `LeoProactiveAgent`, etc.) mostly does
*event/timer → score → act*. OpenBrigade does that **and** adds multi-agent coordination,
dependency graphs, goal-boundary safety, and human-escalation — which is a more complete
orchestration story than any single proactive reference.

## Gaps / risks for RC

1. **The escalation prompt still depends on a capable model.** With `ollama` small models the
   JSON-action contract may be unreliable, but malformed output is now handled by returning
   `no_action` with warning telemetry rather than propagating a parser exception.
2. **Full model self-selection remains partial.** The daemon honors per-agent model settings
   and default-provider fallback during managed runs, but task-difficulty and cost-aware
   routing remain roadmap work — see report [06 — Model Self-Selection](06-model-self-selection.md).
3. **Crew-chief decomposition is tool-mediated.** The `create_subtasks` tool now creates
   dependency-linked child assignments with guardrails, but the quality of decomposition
   still depends on the chief model choosing and filling that tool correctly — see report
   [05 — Sub-agents & Synthesis](05-subagents-delegation-synthesis.md).

## RC assessment

**Not a blocker — this is a strength.** Required before RC: an **end-to-end proactivity
demo** (mission set → daemon runs → idle chief gets a planning task → chief delegates →
worker executes → orchestrator escalates a stalled task), captured so it is reproducible.
If that loop runs against a real model, the proactivity claim is substantiated. Keep
`docs/RC_PROACTIVITY_DEMO.md` current and rerun it during final release validation.
