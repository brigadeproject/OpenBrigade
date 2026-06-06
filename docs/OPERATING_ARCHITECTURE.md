# OpenBrigade Operating Architecture

This document describes the v0.9 runtime architecture for the local PR-candidate stack. It covers
which components own which responsibilities and where operators should look when behavior is
surprising.

## Runtime Shape

OpenBrigade is an orchestrator-driven agent system. The operator sets a mission, creates users,
onboards agents, assigns goals, and then lets the orchestrator advance work through explicit
assignments.

The canonical runtime is the `brigade_` Docker Compose stack:

- `brigade_web`: FastAPI gateway and built React interface.
- `brigade_orchestrator`: daemon that reviews goals, queues work, and records reasoning.
- `brigade_postgres`: authoritative durable state.
- `brigade_redis`: runtime queues, execution claims, alerts, and local inference lock state.
- `brigade_qdrant`: curated episodic memory vectors.
- `brigade_neo4j`: provenance graph records and relationships.
- `brigade_ollama_proxy`: host-network bridge to the local Ollama service.

Normal operator workflows require Postgres-backed runtime state.

## Control Plane

Operator actions enter through:

- `./ops/brigade-live.sh ...` for commands executed inside the live container.
- `brigade ...` when already inside a configured container or environment.
- The web `/api/*` routes.
- TUI views for dashboard, chat, and settings.

Write paths pass through RBAC and then update Postgres first. Secondary stores are updated from that
durable event where appropriate:

- Assignments, users, agents, teams, goals, messages, transcripts, usage, alerts, cloud jobs,
  financial reports, and UI layouts are Postgres records.
- Pending work, assignment execution claims, alerts, and local model lock state are mirrored into
  Redis for runtime coordination.
- Curated episodes are written to Qdrant with source references.
- Documents, chunks, tasks, goals, decisions, teams, and agents are linked in Neo4j provenance.

## Orchestrator Loop

The orchestrator reads mission, goals, active assignments, agent states, and recent reasoning. It
then:

- Assigns queued work whose dependencies are satisfied.
- Proposes stalled-goal tasks when no active work advances a goal.
- Writes active assignment blocks to agent `HEARTBEAT.md` files.
- Records reasoning so operators can inspect why work moved.

The orchestrator must not silently mutate agent identity, memory, or tools. Any future
self-improvement cycle should produce explicit decisions, proposed tasks, or human-review items.

## Agent Runner

The runner executes one active assignment for one agent. It validates the `HEARTBEAT.md` assignment
block before calling a provider. Malformed, stale, duplicated, or mis-targeted heartbeat blocks mark
the assignment blocked and emit alerts without creating transcript, usage, or completion side
effects.

Provider routes include:

- `ollama` for the local/default runtime model path.
- LiteLLM-backed `openai`, `gemini`, and other external providers for v0.9.1 connection work.

All provider responses are parsed into assignment state, transcript records, usage records, and
financial reporting.

## Authority Model

Users have `owner`, `operator`, or `observer` roles. CLI and web writes check permissions before
mutating state. Teams define Crew Chiefs, members, delegation policy, parent/child relationships,
and escalation targets. Agent delegation is valid only inside that explicit authority scope.

## Failure Domains

Postgres failure is a hard stop for live workflows because durable state cannot be trusted without
it. Redis failure should block duplicate-sensitive runtime coordination and produce health failures.
Qdrant and Neo4j failures should alert while preserving canonical Postgres records. Model failures
should produce blocked/failed assignments with actionable summaries, not tracebacks.

## Source of Authority

Configuration comes from `.env`, `brigade.config.json`, and `BRIGADE_*` environment overrides.
Secrets stay out of agent workspaces and UI output. Runtime records in Postgres are the source of
truth; Redis, Qdrant, and Neo4j are specialized operational views derived from those workflows.
