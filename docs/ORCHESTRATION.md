# OpenBrigade Orchestration

This document is the canonical reference for how the full Orchestrator works: the cycle contract,
the work-or-reason invariant, dispatch policy, blocker resolution, intake triggers, rest cycles,
tooling and efficiency flows, and traceability. It describes the v1.0 orchestrator milestone and is
the specification that `checklists/TODO-v1_0-orchestrator.md` implements. For overall runtime shape
see `OPERATING_ARCHITECTURE.md`; for prompt composition see `PROMPT_ARCHITECTURE.md`.

Scope locks for this milestone: no cost-aware model routing, no adaptive cadence, no delegation
recursion guards beyond existing depth handling, no MCP client, and no dynamic sub-agent spawning
(v1.1). Autonomy-adjacent behavior defaults to propose-only or is config-gated.

## Hierarchy of Intent

Three levels of intent, owned by different parts of the system. They are not interchangeable.

| Level | Owner | Description | Mutability |
|---|---|---|---|
| **Mission** | Orchestrator | The brigade's north star. Drives every assignment decision. | Rarely changes; human-edited. |
| **Goal** | Crew Chief / agent | What a team works toward, or what role the chief plays. | Changes occasionally; human or orchestrator set. |
| **Assignment** | Orchestrator → agent | The current unit of work. | Changes every cycle. |

The orchestrator is a service, not an agent. It holds the mission and translates it into
chief-level assignments. It never performs agent work itself and never silently mutates agent
identity, memory, or tools.

### Goal engagement modes

Every goal carries an `engagement_mode`:

- `directive` (default) — the team continuously works toward this goal. Idle agents with a
  directive goal receive synthesized "advance goal" work; idle missions generate continuation
  planning tasks for the chief; stale directive goals fire floor triggers.
- `on_call` — the goal defines the chief's role rather than a work stream (infrastructure,
  financial, and similar standby teams). On-call goals never generate idle-fill or continuation
  work and never fire stale-goal triggers. On-call chiefs are activated by intake routing (an
  ingested item or inbound message that matches their goal or team specialties), by the
  blocker-resolution ladder, or by direct human assignment.

A chief with no work and an on-call goal is healthy, not stalled. The cycle outcome records why
they were not tasked.

### Assignment kinds

Every assignment carries a `kind` so mission work, upkeep, and diagnostics are distinguishable in
queues, telemetry, and training data:

- `mission` (default) — work that advances the mission or a goal.
- `rest` — a dream-cycle assignment (memory curation, reflection, pondering).
- `maintenance` — workspace or system upkeep not tied to mission output.
- `failure_analysis` — a ladder-created diagnosis task, linked to the blocked parent.
- `tool_build` — an approved tool-request build task.

### Specialists and generalists

Agents carry a `specialties` list (free-form tags such as `python`, `finance`, `networking`). An
empty list means generalist. Specialties are routing hints, not permissions: crew chief context
shows each member's specialties so the chief routes matching work to the subject-matter expert and
gives generalists the remainder. Intake routing and ladder reassignment also use specialties to
pick targets.

## The Cycle Contract

The orchestrator runs on `orchestrator_cadence_seconds` (default 900). For testing, run 5-minute
cycles with `BRIGADE_ORCHESTRATOR_CADENCE_SECONDS=300`. Every cycle must end in exactly one of two
states: at least one actionable task was worked or created, or the reasoning record carries a
specific machine-readable reason why no work happened. There is no third state.

### Run sequence

```
 1. Load mission and previous cycle reasoning.
    No mission -> record outcome no_mission and stop the cycle.
 2. Snapshot state: assignments, agents, teams, goals, agent states.
    Idle agents increment idle_cycles; any other status resets it to zero.
 3. Blocker-resolution ladder. Blocked work is addressed before any fresh
    dispatch: retry, spawn failure analysis, reassign, or escalate to human.
 4. Intake drain. New knowledge documents and inbound connector messages
    become routed chief proposals or assignments, per intake_mode.
 5. Recurrence materialization. Approved recurring work that is due becomes
    queued assignments, exactly once per due slot.
 6. Mission continuation and idle synthesis. Existing continuation and
    idle-goal paths run, skipping on_call goals; all orchestrator-created
    work targets crew chiefs.
 7. Rest scheduling. Idle agents inside the rest window, or past the idle
    threshold, receive low-priority rest assignments.
 8. Deterministic dispatch. Queued work is assigned to idle agents by
    priority. Blocked agents receive no fresh work. Rest sorts last and
    never preempts queued mission work. Every skip records a reason.
 9. Floor triggers and bounded LLM escalation. request_human is rejected
    unless the target assignment's ladder is exhausted.
10. Cycle outcome classification. The reasoning record cannot be persisted
    without a CycleOutcome.
11. Persist reasoning record (version 2), agent states, and alerts. Sleep.
```

Steps 3–5 and 7 are new modules (`ladder.py`, `intake.py`, `efficiency.py`, `rest.py`); steps 6
and 8–9 are the existing orchestrator paths with the modifications described below. The full cycle
lives in `brigade/orchestrator.py` as `run_full_cycle` so the CLI `cycle` and `daemon` commands are
thin wrappers.

### Cycle outcomes: the work-or-reason invariant

A cycle "worked" when it took at least one action (dispatched, created, retried, reassigned, or
materialized an assignment; recorded an intake or continuation creation; applied an escalation
action) or when at least one assignment is currently in `working` status (mode `work_in_flight`,
with the in-flight assignment ids recorded). Otherwise the outcome is `no_work` and carries the
first matching reason from this taxonomy:

| Reason | When it is emitted |
|---|---|
| `no_mission` | No mission is set. Nothing else runs. |
| `all_blocked_awaiting_human` | Every active assignment is blocked with `awaiting_human` true; the ladder is exhausted everywhere. |
| `dependencies_unmet` | Queued work exists but every item skipped because its dependencies are incomplete. |
| `all_agents_busy` | Queued work exists but every eligible agent is occupied or blocked, and nothing newly dispatched. |
| `provider_unavailable` | Creation or escalation was attempted but the model provider failed or local inference is locked. |
| `rest_window` | The only possible activity was rest, and rest for the window already exists or completed. |
| `intake_only_pending_approval` | Intake proposals were recorded but `intake_mode` is `propose`, so no assignment was created. |
| `queue_empty_proposal_recorded` | The queue is empty; a continuation proposal was recorded but creation is gated off. |
| `duplicate_suppressed` | Every candidate action was suppressed by an idempotency key. |
| `budget_gate` | Reserved for post-RC cost-aware routing. Never emitted in v1.0. |
| `unclassified` | Fallback. Emitting it also raises an alert — it is treated as a bug. |

The outcome, the per-assignment skip reasons from dispatch, and the active configuration gates are
all embedded in the version-2 reasoning record, so requirement one — "actionable tasks or a
specific logic as to why work isn't being done" — is auditable from `brigade_orchestrator_reasoning`
alone.

## Dispatch Policy: Chief-First Decomposition

Orchestrator-originated work always targets a crew chief, and the assignment text stays high-level
and goal-oriented. The chief decomposes it into specific tasks for their team using the existing
`create_subtasks` and `delegate` tools, producing dependency-linked child assignments with full
lineage (`parent_assignment_id`, `dependency_ids`).

- **Creation paths covered**: mission continuation, idle synthesis, intake, escalation
  `create_assignment`, and recurrence materialization all route through `route_to_chief`.
- **Chief routing inside the team**: the crew chief floor lists each member's specialties; the
  chief routes work to the matching specialist and gives generalists the remainder.
- **Team of one**: a chief whose managed roster is only themself is their own subject-matter
  expert. Their continuation and intake assignments instruct them to decompose the work into
  subtasks assigned to themself with `create_subtasks`; the one-assignment-per-agent dispatch rule
  serializes the children across cycles.
- **Humans may target anyone**: human-created tasks keep their existing priority (sorted first in
  dispatch) and may be assigned directly to any agent.
- **Ladder tasks**: failure-analysis children go to the blocked agent's chief, who may delegate.

The deterministic dispatch function itself stays routing-agnostic — chief-first lives in the
creation paths — so existing dispatch behavior and tests compose unchanged.

Multi-cycle work is unchanged: assignments carry `estimated_cycles` and `cycle_count`, agents mark
incomplete work `working` at the end of a run, and the orchestrator continues it next cycle.

## Blocker-Resolution Ladder

Solving blockers without the human is the orchestrator's first responsibility each cycle. The
ladder runs before any fresh dispatch and processes every assignment that is `blocked` and not yet
`awaiting_human`. Steps key off the existing `consecutive_failures` counter:

| Step | Trigger | Action |
|---|---|---|
| 1. Retry | `consecutive_failures == 1` | Transition `blocked -> assigned`, rewrite the heartbeat block. The runner retries on its next pass. Fully deterministic. |
| 2. Failure analysis | `consecutive_failures == 2` | Create a child assignment (`kind=failure_analysis`, `priority=high`) for the blocked agent's chief, embedding the last error, blockers, and transcript path. The blocked task stays blocked until the analysis child completes. |
| 3. Reassign | `consecutive_failures >= 3` and analysis complete | Deterministically pick a new owner: idle teammate with a matching specialty, else any idle teammate, else the chief. The analysis summary is embedded in the new `assignment_rationale`. |
| 4. Human | `consecutive_failures >= 5` | Mark `awaiting_human` and raise an alert summarizing the full ladder history. This is the only step that interrupts the human. |

Every step action carries an idempotency key (`ladder:v1:<assignment_id>:<step>:<failures>`) so a
step fires at most once per failure increment, safely across daemon restarts.

The LLM escalation layer gains three bounded actions — `retry_blocked_assignment`,
`reassign_blocked_assignment`, and `create_failure_analysis` — validated against ladder state:
out-of-order actions are rejected, and `request_human` is rejected unless the target's ladder is
exhausted. Malformed model output degrades to `no_action` exactly as today; the deterministic
ladder has already acted, so the invariant holds without the LLM.

Ladder events: `ladder_retry`, `ladder_analysis_created`, `ladder_reassigned`,
`ladder_escalated_human`, each with full assignment lineage.

## Intake Triggers

Work can start from automated input, not only from cycle reasoning. Intake is pull-based: each
cycle scans already-persisted artifacts, so connectors and the knowledge pipeline are unchanged and
the pipeline is replay-safe.

- **Sources**: knowledge documents (CLI/web ingestion) and connector inbound messages
  (`metadata.kind == "external_inbound"`, e.g. a Telegram message or forwarded email). Live chat
  replies still happen immediately in the connector path; intake additionally turns actionable
  inbound content into tracked work.
- **Idempotency**: `intake:v1:<sha256(source_kind, source_id)>` — a document or message becomes a
  task at most once, with no watermark table needed.
- **Routing precedence**: (a) the `intake_route_chief` config override; (b) token overlap between
  the item's title/summary/text and each chief's goal statements plus team member specialties —
  this is how on-call chiefs get invoked; (c) the first crew chief.
- **Mode gate**: mirrors proactive continuation. `propose` (default) records an `intake_proposal`
  event only; `create` builds the assignment; `off` disables intake.
- **Assignment shape**: high-level and chief-oriented — "Review ingested item '<title>': decide
  whether it advances the mission, create the follow-up subtasks for your team, or close it with a
  rationale." Source provenance (document or message id) is carried in the rationale and events.
- **Bounded**: at most `max_intake_assignments_per_cycle` (default 2) per cycle, oldest first.

## Rest and Dream Cycles

Agents need downtime to curate memory, reflect, and ponder. Rest is hybrid and config-driven: a
scheduled window guarantees curation happens, and an opportunistic path uses idle capacity.

**Eligibility** — an agent is offered rest when all of these hold:

- `rest_enabled` is true and the agent has no active or queued non-rest work;
- the current time is inside the UTC rest window (`rest_window_start_utc`–`rest_window_end_utc`,
  default 03:00–05:00), or the agent has been idle for `rest_idle_cycles_threshold` consecutive
  cycles (default 6);
- the agent's last completed rest is older than `rest_min_interval_seconds` (default 86400).

At most one scheduled and one opportunistic rest per agent per UTC day
(`rest:v1:<agent>:<date>:<window|idle>`). Rest assignments are `kind=rest`, `priority=low`, sort
last in dispatch, and never preempt queued mission work.

**The dream protocol** — the rest assignment instructs the agent to, using its normal file tools:

1. Read its daily notes (`memory/*-MEMORY.md`) and promote durable facts into `MEMORY.md`, keeping
   it at or under 2KB; prune outdated or irrelevant entries.
2. Append entries to `reflections.md` — what was done, the outcome, the lesson — with a status of
   `candidate`, `promoted`, or `archived` (candidates graduate when they prove useful, archive
   after long disuse).
3. Process up to three questions from `PONDER.md`, the open-questions queue any agent may append to
   during normal work; write conclusions or sharper questions.
4. Write a structured report to `rest/<YYYYMMDD>-REST.md` with sections `## Promoted`, `## Pruned`,
   `## Reflections`, `## Ponderings`, and `## Proposals` (each proposal a bullet tagged
   `[efficiency]` or `[tool_request]`).

**Deterministic finalizer** — when a rest assignment completes, the runner finalizes it regardless
of model quality: the existing workspace memory curation enforces the 2KB cap and archives stale
daily notes into episodes; the rest report is parsed into one episodic record
(`source_kind="rest_cycle"`) and one proposal row per `## Proposals` bullet; a `rest_completed`
event is emitted. Dream output therefore always lands in durable, reviewable form.

`reflections.md` and `PONDER.md` are seeded into workspaces on creation but are not required files,
so existing heartbeat validation is untouched.

## Tool Requests and Workspace Tools

Agents can find or build the tools they need, within the v1.0 security model (no dynamic code
loading; everything runs through the existing subprocess sandbox).

1. **Request**: any agent calls the `request_tool` tool with a name, purpose, and spec. This
   creates a `tool_request` proposal and an alert — it never builds anything directly.
2. **Approve**: a human approves via `brigade proposal approve <id>` (operator RBAC), or a crew
   chief approves proposals from their own team via the `approve_proposal` tool. Approval creates a
   `kind=tool_build` assignment for the requesting team's chief.
3. **Build**: the build task produces an executable script under `<workspace>/tools/`, a
   `tools/<name>.json` descriptor (name, description, argument schema), a usage note in `TOOLS.md`,
   and a smoke run.
4. **Use**: the generic `run_workspace_tool` tool executes `<workspace>/tools/<name>` through the
   same subprocess guard as `shell` (30-second cap, path-safety validation, no shell interpreter).
   The agent floor merges the workspace tool manifest into the available-tools list, so the new
   tool is visible on the very next heartbeat — no process reload, no dynamic imports.

## Efficiency Detection and Recurrences

The orchestrator watches for repetitive work and proposes converting it into scheduled recurrences.

- **Detection**: each cycle, completed assignment history is grouped by assignee and normalized
  assignment text (dates and ids stripped). A group reaching `recurrence_detection_threshold`
  (default 3) within `recurrence_lookback_days` (default 14) becomes an `efficiency` proposal
  containing the pattern, observed count, sample assignment ids, and a proposed recurrence template
  with an interval derived from the median completion gap. When Qdrant is available, similar
  episodes are attached as supporting evidence — evidence only, never the trigger, so detection
  stays deterministic and testable offline.
- **Approval**: a human (CLI/web) or the team's chief approves the proposal, creating a recurrence
  record (template, interval, next due time).
- **Materialization**: cycle step 5 turns due recurrences into queued assignments, exactly once per
  due slot (`recurrence:v1:<id>:<next_due_at>`), then advances the due time.

Rest-cycle `[efficiency]` proposals enter the same proposal queue, so agents' own observations
about repetitive work flow through the identical approval path.

## Traceability and Training Data

Traceability is mandatory. Every decision the orchestrator makes must be reconstructable from
durable records, and the accumulated history must be exportable as training data.

- **Reasoning record v2**: every cycle persists `record_version: 2`, the `cycle_outcome`, the
  per-assignment `skip_reasons`, the ladder/intake/recurrence/rest sub-results, and a
  `config_snapshot` of the gates active that cycle (cadence, proactive mode, intake mode, rest
  window, ladder enabled). Version-1 records remain readable.
- **Events**: all new event types (`cycle_outcome`, `ladder_*`, `intake_*`,
  `recurrence_materialized`, `rest_scheduled`, `rest_completed`, `proposal_created`,
  `proposal_decided`) flow through the existing
  orchestration event path, so Cockpit and Ops Room telemetry render them with only filter-set
  additions.
- **Export**: `brigade export training-data --out DIR [--since ISO]` (operator RBAC) writes
  self-contained JSONL: `cycles.jsonl` (full reasoning records), `assignments.jsonl` (text, kind,
  priority, lineage, cycle counts, failure counts, final status, executive summary — the
  state/decision/outcome tuples that make training data), `transcripts.jsonl` (prompt, tool
  observations, and responses inlined), `usage.jsonl`, `episodes.jsonl`, `proposals.jsonl`, and a
  `manifest.json` with counts, time range, and schema versions.

## Chatting with a Crew Chief

Human chat is the main control surface, and a chief must be able to answer questions about current
status, tasks, priorities, and blockers truthfully — grounded in live state, not memory. Chat
prompts for a crew chief include a compacted status context: team goals with engagement modes,
member load and specialties, queue depth, active assignments with progress summaries, open
blockers, awaiting-human items, and recent team alerts. Line workers get their own state and active
assignment. The same context feeds web chat, TUI chat, and connector (Telegram) chat, so asking a
chief "what's the team working on and what's stuck?" over any surface gets a grounded answer.

## Configuration Reference

All keys follow the existing pattern: `Settings` field, `BRIGADE_*` environment override, and
`brigade.config.json` key.

| Key | Default | Purpose |
|---|---|---|
| `orchestrator_cadence_seconds` | `900` | Cycle interval. Use `300` for 5-minute test cycles. |
| `intake_mode` | `"propose"` | `propose` / `create` / `off` for intake triggers. |
| `max_intake_assignments_per_cycle` | `2` | Intake cap per cycle. |
| `intake_route_chief` | unset | Force all intake to one chief. |
| `intake_default_priority` | `"normal"` | Priority for intake-created assignments. |
| `rest_enabled` | `true` | Master switch for rest scheduling. |
| `rest_window_start_utc` / `rest_window_end_utc` | `"03:00"` / `"05:00"` | Scheduled rest window (UTC). |
| `rest_idle_cycles_threshold` | `6` | Idle cycles before opportunistic rest. |
| `rest_min_interval_seconds` | `86400` | Minimum spacing between rests per agent. |
| `blocker_resolution_enabled` | `true` | Master switch for the ladder. |
| `recurrence_detection_threshold` | `3` | Completions of similar work before an efficiency proposal. |
| `recurrence_lookback_days` | `14` | History window for recurrence detection. |
| `proactive_mode` / `proactive_creation_enabled` | `"propose"` / `false` | Existing continuation gates, unchanged. |

## Glossary

- **Mission** — the brigade-wide objective, held by the orchestrator.
- **Goal** — a team's directive work stream or on-call role definition, held by the crew chief.
- **Assignment** — one unit of work; "task" in conversation means an assignment.
- **Crew Chief** — the agent leading a team; receives high-level work and decomposes it.
- **Line worker** — a team member agent; specialist (tagged specialties) or generalist.
- **Team of one** — a chief with no other members; their own subject-matter expert.
- **Cycle** — one orchestrator pass; cadence is config-driven.
- **Ladder** — the blocker-resolution sequence: retry, analyze, reassign, human.
- **Intake** — the pipeline turning ingested documents and inbound messages into work.
- **Rest / dream cycle** — the curation, reflection, and pondering assignment.
- **Proposal** — a pending suggestion (efficiency, tool request, rest insight) awaiting approval.
- **Recurrence** — an approved repeating assignment template materialized on schedule.
