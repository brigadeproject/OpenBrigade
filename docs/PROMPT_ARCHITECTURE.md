# OpenBrigade Prompt Architecture

Prompt construction should be explicit, inspectable, and tied to OpenBrigade state. The model should
receive enough context to do the current job, not the entire system history.

## Prompt Inputs

Core inputs:

- Mission statement and success criteria.
- Current assignment text and metadata.
- Assigned agent identity, role, team, and current goals.
- Relevant blockers, progress summary, and dependency state.
- Selected memory summaries and knowledge snippets.
- User identity context when the request comes from chat or web.

Excluded from routine prompts:

- Raw full transcript history.
- Secrets and API keys.
- Unreviewed external webhook payloads.
- Whole agent workspaces unless the assignment specifically asks for workspace review.

## Assignment Prompts

The orchestrator writes an assignment block to `HEARTBEAT.md`. The runner validates that block before
execution. The prompt sent to the provider should be derived from that validated assignment and the
agent record in the store.

Malformed heartbeat blocks are safety failures, not model work. They should block cleanly and avoid
completion side effects.

## Chat Prompts

User-to-agent chat prompts include the target agent, sender identity, and the current message.
Responses create request/response messages, usage records, and curated episodes. Chat prompts should
remain bounded; if a user wants document-scale analysis, route that through library ingestion and a
task.

## Team Prompts

Team delegation and escalation prompts should include:

- Source team and target team or agent.
- Crew Chief authority.
- Delegation policy.
- Reason for routing or escalation.
- Linked goal or parent assignment when present.

This keeps team movement auditable and prevents implicit cross-team authority.

## Staff Meeting Prompts

Staff Meeting prompts are phase-specific contracts generated from a frozen packet and immutable
meeting records. They do not implement meeting policy. The harness owns authorization, transitions,
quorum, voting, veto clearance, limits, and persistence.

- Independent-review prompts contain equivalent packet material plus one role lens and do not
  expose other panel responses.
- Synthesis prompts go only to the chair and include the structured panel responses required for
  synthesis.
- Deliberation and targeted follow-up prompts contain the current synthesis and deterministic,
  attributed excerpts. Mandatory veto, dissent, contradiction, risk, and objection spans cannot be
  suppressed by chair wording.
- Ballot prompts produce one secret role-seat ballot. A discussion after reveal opens a new secret
  ballot round; it does not rewrite an earlier ballot.
- Final-report prompts supply current-round ballots and vetoes. The harness renders the required
  fifteen-section report around the chair's conclusion and independently computed arithmetic.

Every model response still uses the ordinary OpenBrigade outer completion envelope. Its `summary`
is a JSON-encoded phase object that the harness validates before accepting the seat as responsive.
Malformed output becomes an absence; the harness never fabricates a response or vote for that seat.

## Proactive Mission Continuation

Mission continuation is bounded and observable. The default mode is propose-only:
the Orchestrator may record one Crew Chief-level continuation proposal for an
idle mission cycle, but it does not create a task unless both controls are set:

- `proactive_mode` is `create`.
- `proactive_creation_enabled` is `true`.

The continuation evaluator only fires when a mission exists, at least one Crew
Chief exists, and there is no active or queued next work. It generates leadership
planning or coordination work for an existing Crew Chief. It must not create
low-level worker tasks or dynamic agents in this step.

Every proposal or creation carries provenance in `orchestrator_reasoning`:
mission, supported goal when available, trigger condition, target Crew Chief,
parent or child assignment IDs when relevant, source, and idempotency key. The
idempotency key is:

```text
orchestrator-proactive:v1:<sha256>
```

The hash is built from normalized mission text, supported goal text if any,
trigger condition, target Crew Chief, and proposed assignment text. Active
assignments, archived assignments, and prior proposals are checked before a new
proposal or task is recorded.

## Rumination and Self-Improvement

Future dreaming/rumination cycles should be separate from normal assignment execution. They should
read summaries of mission, goals, memory, outcomes, alerts, and team state, then emit one of:

- A proposed task.
- A proposed goal review.
- A proposed memory/library curation action.
- A human-review item.

They should not silently rewrite identity, tools, secrets, or operator policy.

## Provider Behavior

`ollama` is the local/default runtime model path. External model routes should fail with clear
errors when credentials are missing.

All provider responses should be parsed into structured status: `complete`, `working`, `blocked`,
`awaiting_human`, or `failed`. Unsupported or malformed provider output should retry when safe and
then block with an actionable alert.
