# OpenBrigade TODO

## v0.1 MVP Hardening
- Replace prototype file state with a real repository layer for Postgres and Redis. `Complete: Postgres is required for operator runtime state; broader datastore normalization landed through v0.9.`
- Add an Alembic-backed migration runner. `Deferred to v0.7 migration/hardening.`
- Start and smoke-test the full `brigade_` Docker stack.
- Add Redis queues for active assignments, pending work, alerts, and local inference lock. `Deferred to v0.7 migration/hardening; current prototype uses Redis-backed runtime state where needed.`
- Persist completed assignments, chats, reasoning logs, users, and knowledge records in Postgres.
- Add integration tests marked separately from unit tests.
- Wire `brigade health` to real Postgres, Redis, Qdrant, and Neo4j checks.
- Add Ollama integration tests gated by local availability.
- Add LiteLLM adapter for OpenAI, Anthropic, and Gemini API-key use.
- Add real agent runner mode using provider responses, not only fake completion.
- Add structured orchestrator reasoning records per cycle.
- Add retry handling for malformed model output, blocked tasks, failed tasks, and 5-failure alerts.
- Add backup and restore notes for volumes and state.

## v0.2 Agent Runtime
- Implement the heartbeat execution loop per managed agent.
- Add parse/update support for `HEARTBEAT.md` result blocks.
- Add agent state snapshots: `idle`, `working`, `blocked`, and `awaiting_human`.
- Add task continuation across cycles with `cycle_count`.
- Implement abandoned-at-10-cycles behavior end to end.
- Add local inference lock for Ollama jobs.
- Add cloud job tracking and in-flight guardrails.
- Add financial report generation for ABACUS.
- Add cost tracking hooks for local and cloud usage.

## v0.3 Memory and Knowledge
- Store raw transcripts in Postgres.
- Store structured episodic summaries in Qdrant. `Deferred to v0.7 migration/hardening.`
- Stop raw transcript dumping into vector memory. `Prototype uses Postgres/app-data records; final vector split deferred to v0.7.`
- Add daily memory curation job.
- Enforce the `MEMORY.md` 2KB soft cap in live workspaces.
- Archive daily memories after 7 days into Qdrant. `Deferred to v0.7 migration/hardening.`
- Add knowledge ingestion for Markdown and text files first.
- Add PDF, web, and repository ingestion later in this phase.
- Create Neo4j document, task, and decision provenance nodes. `Deferred to v0.7 migration/hardening.`

## v0.4 Interfaces and Multi-User
- Build the planned TUI dashboard.
- Add mission, goal, task, agent, and alert views.
- Add interactive task creation and assignment inspection.
- Add JWT auth scaffolding for future web/API use.
- Enforce owner/operator/observer permissions beyond data modeling.
- Add a web upload endpoint or minimal local upload command for knowledge.
- Add user identity injection into chat/session context.

## Post-v0.4 Break-Test Follow-Up
- Add explicit host-vs-live guardrails so local `brigade` use is harder to confuse with `./ops/brigade-live.sh`.
- Audit re-seed and re-init safety for users, agents, goals, mission state, and any future bootstrap records.
- Stress-test concurrent `agent run`, `agent run-all`, and `orchestrator cycle` paths for duplicate execution and lock gaps.
- Add malformed `HEARTBEAT.md` and partial-assignment parsing tests.
- Add recovery and state-consistency checks after container recreate without volume loss.
- Tighten CLI/operator guidance where the live prototype and host workspace can diverge.
- Replace remaining prototype app-data runtime state with the intended datastore-backed persistence layer. `Deferred to v0.7; runtime data is disposable until then.`

## v0.4 Prototype Acceptance Baseline
- Treat v0.1 through v0.4 as complete for the working prototype once the baseline commands pass.
- Runtime data may be wiped, reseeded, or rebuilt before v0.7 migration. Source, docs, Compose config, ops scripts, and explicit backups are the durable deliverables.
- Before wiping prototype data, export any selected records worth preserving.
- Required baseline:
  - `python3 -m pytest`
  - `python3 -m ruff check .`
  - `python3 -m compileall brigade tests ops/ollama_bridge_proxy.py`
  - `KEEP_WORK_DIR=1 PROVIDER=ollama MODEL='qwen2.5-coder:7b' ./ops/stress-concurrency.sh`
  - `./ops/test-bad-heartbeats.sh`
  - `./ops/check-recovery.sh`

## v0.5 Proactivity and Expansion
- Add agent onboarding/bootstrap flow. `Initial CLI flow complete: agent onboard.`
- Add workspace manifest validation and repair guidance. `Initial validation complete: agent validate.`
- Add team definitions, membership, and Crew Chief assignment. `Initial prototype records and CLI complete.`
- Add team hierarchy display in the dashboard and CLI. `Initial dashboard teams view and team show complete.`
- Add CLI for team creation, assignment, inspection, and membership changes. `Initial create/list/show/assign/chief commands complete.`
- Add sync 1:1 inter-agent chat. `Initial chat ask-agent flow complete with stored request/response, usage, and episode records.`
- Add group chat with a “pass the mic” workflow. `Initial serialized chat group flow complete.`
- Add Crew Chief authority flows. `Initial team delegate flow complete with authority checks and audit records.`
- Add proactive task creation when goals stall. `Initial orchestrator propose-stalled-goals flow complete with idempotent queued work.`
- Add model routing decisions using ABACUS cost and effectiveness data. `Initial model route command complete using financial report, usage, risk, and cloud in-flight state.`
- Add cloud dispatch for extended work. `Initial cloud dispatch/list/resolve commands complete with queued jobs, extended assignments, terminal resolution, and in-flight guardrails.`
- Add user alerting for drift, datastore failure, and repeated task failure. `Initial alert audit command complete for goal drift, repeated failures, failed cloud jobs, and optional datastore health.`
- Add release checklist, public repo cleanup, and MVP docs. `Initial RELEASE_CHECKLIST.md, README, and PROTOTYPE updates complete.`

## v0.6 Organization and Delegation
- Add hierarchical delegation rules for teams and sub-teams. `Initial parent/child Crew Chief scope and team delegation policy complete.`
- Add team-aware orchestrator policy for routing work to Crew Chiefs or individual agents. `Initial team route-work command complete with explainable routing records.`
- Add authority validation so agents can only direct allowed team members. `Initial agent_delegate guardrails complete for task creation and team delegation.`
- Add cross-team coordination rules and escalation paths. `Initial team escalate command complete with assignment, chat, and reasoning records.`
- Add team-scoped goal and status views. `Initial team status CLI and dashboard team counts complete.`
- Add organization graph storage for teams, reporting lines, and command relationships. `Initial org graph command and provenance snapshot persistence complete.`

## v0.7 Migration and Hardening
- Address any issues found in testing through v0.6. `Initial v0.7 pass complete with explicit migration reporting and service/UI smoke tests.`
- Add an Alembic-backed migration runner or equivalent explicit migration command. `SQL-first db status/migrate commands complete with brigade_schema_migrations tracking.`
- Decide whether to wipe and reseed prototype datastore state or export selected records before migration. `Decision: wipe/reseed runtime data after backup; ops/v07-wipe-reseed.sh added.`
- Normalize runtime persistence across Postgres, Redis queues, Qdrant, and Neo4j. `Initial adapters wired: Postgres remains durable record, Redis lock path remains active, Qdrant/Neo4j receive episode/provenance writes.`
- Move episodic/vector memory into Qdrant as a primary path. `Initial Qdrant episode upsert path complete using brigade_episodes collection.`
- Move document, task, and decision provenance into Neo4j as a primary path. `Initial Neo4j provenance upsert path complete via HTTP transaction endpoint.`
- Replace prototype/local app-data stand-ins that remain after v0.6. `Operator workflows now require Postgres-backed runtime state.`

## v0.8 - Live User Interfaces
- TUI Chat interface (see OpenClaw source in reference folder.) `Initial chat tui/plain view complete on shared user-chat service.`
- TUI Settings interface `Initial settings tui/plain view complete with redacted settings payload.`
- Web Chat interface `Initial FastAPI route and React/Vite chat view complete.`
- Web Heirarchy interface `Initial FastAPI hierarchy payload and React/Vite hierarchy view complete.`
- Web Settings interface `Initial FastAPI settings route and React/Vite settings view complete.`

## v0.9 - Final MVP Testing and Hardening
- Canonical live architecture is Postgres plus Redis/Qdrant/Neo4j. `Implemented in code and docs; operator workflows fail clearly when Postgres is not configured.`
- Redis runtime normalization. `Implemented: pending assignment queue, execution claims with leases, alert queue, local inference lock, redis inspection, and recovery queue reconciliation checks.`
- Local Ollama as internal/default runtime. `Implemented: configured BRIGADE_OLLAMA_BASE_URL makes Ollama the resolved live default; fake remains explicit for deterministic tests.`
- Qdrant memory validation. `Implemented: curated episode writes, source refs, inspection source-kind counts, and alerting on failed writes. Clean-stack sentinel rerun remains.`
- Neo4j provenance relationships. `Implemented: document->chunk, task->agent/goal, decision->assignment, and team->agent relationships plus inspection relationship samples. Clean-stack sentinel rerun remains.`
- Backend web auth/API hardening. `Implemented: fail-closed auth, malformed/expired token coverage, denied writes, settings redaction, security headers, Docker asset packaging, and ASGI API tests. Auth-enabled live web smoke remains.`
- TUI hardening. `Implemented: narrow terminal safe writes, long-line truncation, empty/no-agent plain states, and stable plain output coverage.`
- Migration failure and recovery behavior. `Partially implemented: db status/migrate/reporting exists; deliberate partial-failure recovery test remains.`
- Wipe/reseed/restore. `Scripts and prior validation exist; add a full-wipe blank-userland script before GitHub publication, then rerun controlled wipe/reseed/restore during final live validation.`
- Final v0.9.0 validation. `Run clean stack, migrations, auth-enabled web smoke, Ollama internal smoke when configured, stress, bad heartbeat, recovery, and backup/restore checks.`

### v0.9.1 - External connections
- Scope: external/non-default integrations only. Local Ollama is internal/default and belongs to v0.9.0 core runtime.
- Telegram wrapper:
  - Inbound fake-payload wrapper, allowlist, and tests are implemented.
  - Remaining: BotFather setup doc, outbound reply fake/live path, owner approval flow, rate/size limits, connector disable switch, and live disabled-by-default smoke.
- Google Chat wrapper:
  - Inbound fake-event wrapper, allowlist metadata, and tests are implemented.
  - Remaining: setup doc, outbound reply fake/live path, durable identity mapping/approval, rate/size limits, connector disable switch, and live disabled-by-default smoke.
- OpenAI/Codex model connection:
  - LiteLLM `openai` alias and redacted settings are implemented.
  - Remaining: supported auth-mode doc, missing/invalid credential behavior, bounded live smoke, usage metadata review, and disable switch.
- Google/Gemini model connection:
  - LiteLLM `gemini` alias and redacted settings are implemented.
  - Remaining: supported auth-mode doc, missing/invalid credential behavior, bounded live smoke, usage metadata review, and disable switch.
- External connection security:
  - Keep secrets out of agent workspaces.
  - Add per-connector rate limits, message size limits, disable switches, and inbound/outbound audit records.

### v0.9.2 - Web UI/UX Overhaul
- Scope: make the web interface useful for daily operation. Backend auth/API safety is now a v0.9.0 core gate and covered by automated ASGI tests.
- Replace the novelty-first ops room with a work-first operator cockpit:
  - First screen shows mission, agents, queued/active work, blockers, alerts, datastore health, and recent outcomes.
  - Operator can scan the system without opening multiple CLI views.
- Task workflows:
  - Create, inspect, filter, and follow assignment history.
  - Surface blockers, awaiting-human state, run provider/model, transcript links, and side effects.
- Chat workflows:
  - Pick agent/channel, see pending/error states, and understand persisted messages/usage/episodes.
  - Handle expired tokens without loops or silent failure.
- Team/hierarchy workflows:
  - Show teams, Crew Chiefs, members, delegation policy, escalation paths, and workload.
  - Make permitted edits clear and denied actions readable.
- Settings/status workflows:
  - Keep secrets redacted.
  - Show config hash/stale-write failures, unsafe auth/bind warnings, and datastore status.
- Browser quality:
  - Role-aware controls for observer/operator/owner.
  - Responsive desktop/mobile layout with no overlapping text or controls.
  - Playwright or equivalent screenshots for the main flows.
  - Auth-enabled live web smoke remains part of validation.

### v0.9.3 - PR Candidate
Let's clean it up and post it to Github.  From there we're live and will move a little slower with updates, but may get a lot better feedback.
- Add a Docker release-path check before the PR.
  - Build `brigade_web`.
  - Start the full app profile.
  - Run `brigade health --json`, `brigade db status`, dashboard smoke, auth-enabled web smoke, and recovery smoke.
- Ensure public cleanup includes generated web artifacts and dependency folders.
  - Keep `web/node_modules/`, `web/dist/`, caches, backups, and volume snapshots out of source artifacts.
  - Confirm `package-lock.json` is present so frontend builds are reproducible.
