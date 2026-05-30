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

## Rumination and Self-Improvement

Future dreaming/rumination cycles should be separate from normal assignment execution. They should
read summaries of mission, goals, memory, outcomes, alerts, and team state, then emit one of:

- A proposed task.
- A proposed goal review.
- A proposed memory/library curation action.
- A human-review item.

They should not silently rewrite identity, tools, secrets, or operator policy.

## Provider Behavior

`fake` is for deterministic tests. `ollama` is the local/default runtime model path. External model
routes belong to v0.9.1 connection work and should fail with clear errors when credentials are
missing.

All provider responses should be parsed into structured status: `complete`, `working`, `blocked`,
`awaiting_human`, or `failed`. Unsupported or malformed provider output should retry when safe and
then block with an actionable alert.
