# v0.2 Agent Runtime Research Notes

Scope: flesh out the `TODO.md` v0.2 Agent Runtime bullets for OpenBrigade. This is documentation only; implementation remains future work.

## Implement the heartbeat execution loop per managed agent

OpenBrigade should run a deterministic per-agent cadence from the explicit workspace manifest, not by globbing `workspace-*`. Each cycle should load mission-level orchestrator context, read compact agent snapshots, write or preserve the current assignment, invoke the agent through the existing OpenClaw-compatible `HEARTBEAT.md` surface, then archive or continue the assignment based on the result.

Useful snippets:
- `reference/agent-system-design-v1.3.md:81` shows the orchestrator run sequence: load mission, pull agent state, pull financial report, check human input first, decide assignments, write state, dispatch work, log reasoning, sleep.
- `reference/agent-system-design-v1.3.md:120` defines the intended cadence: orchestrator at `:00`, SAGE at `:15`, GARDE at `:30`, ABACUS at `:45`, then the next orchestrator pass.
- `reference/codex-openclaw-starting-prompt.md:28` says each agent already reads its own `HEARTBEAT.md`, and future orchestrator work must keep that interface because OpenClaw expects it.
- `reference/codex-openclaw-starting-prompt.md:35` warns not to hardcode agent names or glob all `workspace-*`; OpenBrigade should use an explicit managed-agent manifest.

Why useful: these lines define the runtime contract, cadence, and discovery boundary. The loop should be boring infrastructure: predictable order, no persona logic in the orchestrator, and no accidental takeover of unrelated workspaces.

## Add parse/update support for `HEARTBEAT.md` result blocks

OpenBrigade should treat the final structured block in an agent's `HEARTBEAT.md` as the machine-readable assignment/result contract. The parser should preserve all human-readable prose, locate the last valid JSON assignment block, update only that block or append a replacement, and reject ambiguous/multiple-active states instead of guessing.

Useful snippets:
- `reference/agent-system-design-v1.3.md:190` defines the assignment schema expected at the end of `HEARTBEAT.md`, including `assignment_id`, `assigned_to`, `work_mode`, `status`, `estimated_cycles`, transcript fields, and the path where the row was written.
- `reference/agent-system-design-v1.3.md:214` explains assignment status transitions: orchestrator writes `assigned`; the agent writes completion output or marks `working` if it needs another cycle.
- `reference/agent-system-design-v1.3.md:220` explicitly requires compaction to preserve the latest parseable assignment block.
- `reference/self-improving-1.2.16/heartbeat-rules.md:7` separates a minimal workspace heartbeat snippet from mutable heartbeat state.

Why useful: the OpenBrigade parser should be defensive because `HEARTBEAT.md` is both human-editable and machine-consumed. The final parseable block is the compatibility layer with OpenClaw.

## Add agent state snapshots: `idle`, `working`, `blocked`, and `awaiting_human`

OpenBrigade should maintain compact per-agent snapshots for the orchestrator. Snapshots should contain only executive state: status, current assignment summary, blockers, last completion, and next availability. They should not pull full memories, raw transcripts, or internal meta-reasoning.

Useful snippets:
- `reference/agent-system-design-v1.3.md:71` says the orchestrator needs agent state snapshots but not full memory or raw histories.
- `reference/agent-system-design-v1.3.md:222` defines an agent state snapshot schema with status values `working`, `idle`, `blocked`, and `awaiting_human`.
- `reference/openclaw/src/agents/pi-embedded-runner/types.ts:116` has an OpenClaw liveness union of `working`, `paused`, `blocked`, and `abandoned`; this is not the exact OpenBrigade enum, but it is a useful precedent for keeping runtime state small and explicit.

Why useful: snapshots are the orchestrator's decision surface. `awaiting_human` should be distinct from `blocked` so the orchestrator can avoid churn and surface the right alert path.

## Add task continuation across cycles with `cycle_count`

OpenBrigade should increment `cycle_count` only when an assignment survives a heartbeat incomplete and remains eligible for continuation. The next heartbeat should receive the same `assignment_id`, updated `cycle_count`, last progress summary, and any blocker/sticking-point notes.

Useful snippets:
- `reference/agent-system-design-v1.3.md:203` includes assignment `status`, `estimated_cycles`, and `checkpoint_at`, which are the nearest reference fields for continuation.
- `reference/agent-system-design-v1.3.md:218` says a heartbeat should mark an assignment `working` only if it remains incomplete and should continue in a future cycle.
- `OpenBrigade-Concept.md:129` gives the project-specific rule: incomplete cycles become `in progress - turn ##`.

No good reusable code example exists in the reference corpus for `cycle_count` itself. The nearest reference is the assignment schema and status-transition guidance above.

Why useful: continuation must preserve identity. A continued task is not a new task; it is the same assignment with another cycle of evidence.

## Implement abandoned-at-10-cycles behavior end to end

OpenBrigade should mark an assignment `abandoned` when it reaches 10 incomplete cycles, archive the assignment with the latest sticking point, stop re-dispatching it automatically, and give the orchestrator a chance to assign failure analysis or ask the user.

Useful snippets:
- `OpenBrigade-Concept.md:129` states the project rule directly: at turn 10, assume the task failed and mark it `abandoned`, preserving the sticking point if possible.
- `OpenBrigade_V0.1_Design_Summary.md:226` restates the heartbeat lifecycle and says `in progress - turn ##` is abandoned at turn 10.
- `reference/agent-system-design-v1.3.md:110` says completed, failed, abandoned, or superseded Redis records must be archived to PostgreSQL before removal or compaction.
- `reference/openclaw/src/agents/pi-embedded-runner/run/incomplete-turn.ts:380` maps incomplete/replay-invalid terminal states to `abandoned`; useful as a liveness-state precedent, not as the exact cycle-count rule.

Why useful: abandonment is an audit transition, not silent cleanup. The implementation should leave enough evidence for the orchestrator to decide whether to split, retry, escalate, or stop.

## Add local inference lock for Ollama jobs

OpenBrigade should maintain a Redis-backed local generative inference lock for Ollama on the primary GPU endpoint. It should block overlapping local generative jobs, enforce minimum spacing, expose `next_available`, and leave embeddings/cloud routes untouched.

Useful snippets:
- `reference/agent-system-design-v1.3.md:36` says heartbeat/meta/orchestration jobs route through OpenClaw to the local model with one local generative job at a time and 10-minute minimum spacing.
- `reference/agent-system-design-v1.3.md:98` says never dispatch a local job if `local_inference.next_available` is in the future.
- `reference/agent-system-design-v1.3.md:284` defines the local inference lock shape: `status`, `last_completed`, and `next_available`.
- `reference/codex-openclaw-starting-prompt.md:39` specifies the two Ollama instances: `localhost:11434` for generative inference with the lock, and `localhost:11435` for embeddings without the lock.
- `reference/openclaw/src/cli/gateway-cli/run-loop.ts:108` is a useful lock lifecycle precedent: acquire a lock at startup and release it explicitly through a cleanup helper.

Why useful: the lock must protect scarce GPU inference, not all model work. Cloud jobs and embedding calls should not be serialized behind the generative lock.

## Add cloud job tracking and in-flight guardrails

OpenBrigade should track cloud jobs separately from local inference. Each cloud dispatch should have an assignment/job id, provider route, transcript path, owning agent, status, start/update timestamps, and completion artifact/result summary. The orchestrator should refuse another cloud dispatch while one is in flight unless it records explicit reasoning.

Useful snippets:
- `reference/agent-system-design-v1.3.md:41` says cloud jobs do not compete with local inference, should usually be assigned through the relevant agent, and direct Codex calls must write a transcript plus state row.
- `reference/agent-system-design-v1.3.md:103` requires explicit orchestrator reasoning before dispatching another cloud job while one is already in flight.
- `reference/agent-system-design-v1.3.md:143` says extended jobs run async, write output back to assigned agent state on completion, and are picked up next cycle.
- `reference/openclaw/src/cli/gateway-cli/run-loop.ts:488` shows an operational pattern for formatting active task blockers during restart drain; useful for status/debug output around in-flight work.

Why useful: in-flight guardrails prevent runaway spend and duplicated long-form work while preserving the agent continuity requirement.

## Add financial report generation for ABACUS

OpenBrigade should make ABACUS the last runtime step in the cycle and have it produce a structured financial report consumed by the next orchestrator cycle. The report should include local/cloud token usage, burn rates, budget remaining, cloud jobs in flight, `block_cloud_dispatch`, effectiveness scores, routing recommendations, source confidence, and local-vs-cloud savings.

Useful snippets:
- `reference/agent-system-design-v1.3.md:58` defines ABACUS as the financial agent for token burn, spend rate, effectiveness scoring, routing recommendations, and back-pressure.
- `reference/agent-system-design-v1.3.md:120` places ABACUS at `:45`, after SAGE and GARDE, so it can evaluate work just completed.
- `reference/agent-system-design-v1.3.md:244` defines the financial report schema, including `cloud_jobs_in_flight` and `block_cloud_dispatch`.
- `reference/agent-system-design-v1.3.md:271` recommends OpenGauge as the primary metrics source, with OpenClaw logs as a lower-confidence fallback.

No good code example exists in the reference corpus for an ABACUS-style financial report generator. The nearest reference is the schema and token-source guidance above.

Why useful: this report is not just accounting. It is the orchestrator's budget and routing feedback loop.

## Add cost tracking hooks for local and cloud usage

OpenBrigade should capture usage at the runner boundary for every model call: provider, model, route type, assignment id, token counts, cache counts where available, estimated cost, and whether the usage came from local, cloud, rumination, meta-reasoning, or direct orchestrator calls.

Useful snippets:
- `reference/openclaw/src/agents/pi-embedded-runner/types.ts:31` defines agent metadata usage fields for input, output, cache read/write, and totals.
- `reference/openclaw/src/cron/isolated-agent/run.ts:868` extracts run usage, provider, model, derives total tokens, estimates cost, and writes telemetry.
- `reference/openclaw/src/cron/types.ts:85` defines a concise cron usage summary shape with input/output/total/cache token fields.
- `reference/openclaw/src/cron/run-log.ts:310` normalizes persisted usage fields defensively so malformed usage does not poison logs.
- `reference/agent-system-design-v1.3.md:246` says ABACUS must track local, cloud, rumination, meta-reasoning, and direct orchestrator Codex usage.

Why useful: usage capture belongs at the execution edge, before summaries lose detail. ABACUS can aggregate later, but the hooks need raw-enough telemetry to support cost, savings, and routing decisions.
