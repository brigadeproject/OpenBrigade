# v0.9 Final MVP Testing and Hardening

Scope: harden OpenBrigade from working prototype to PR candidate. This phase is about making the
system reproducible for a new user, trustworthy under containerized operation, and clear enough for
outside review. External/non-default connections are now tracked in v0.9.1. v0.9.2 is the web UI/UX
overhaul, not a blocker for the v0.9.0 core hardening pass. The next Pixel Agents-inspired Live Ops
Room buildout is tracked separately in v0.9.4 so break testing and RC hardening can continue in
parallel.

Status on 2026-05-27: v0.9.0 core hardening is implemented against the current live stack.
Automated tests, lint, frontend build, Docker rebuild, Redis runtime normalization, backend web auth
tests, Qdrant inspection, Neo4j relationship writes, TUI hardening, bad-heartbeat cleanup, web asset
packaging, non-dropping recovery, and local Ollama default selection pass locally. Operator workflows
now require Postgres-backed state. Remaining
release validation is clean empty-volume/auth-enabled smoke, partial migration failure recovery,
controlled backup/wipe/reseed/restore, clean-stack datastore sentinels, local Ollama live smoke, and
public repo cleanup.

## Goals and PR Candidate Definition

- Define the PR candidate as a clean `brigade_` stack that builds from source, starts from empty
  volumes, runs migrations, creates a first user, onboards agents, and survives recovery checks.
  Acceptance: a fresh operator can follow documented steps without relying on existing prototype data.
- Treat Postgres, Redis, Qdrant, and Neo4j as the live architecture.
  Acceptance: docs and code clearly identify live paths and fail when required stores are missing.
  Status: implemented; operator docs refreshed into separate architecture files.
- Keep runtime data disposable until the PR candidate pass is complete.
  Acceptance: backup, wipe/reseed, and recovery steps are documented and tested before publishing.
- Preserve live OpenClaw agents outside this system until a manual migration test succeeds.
  Acceptance: migration instructions begin with a blank OpenBrigade agent and manual paste-in.

## v0.9.0 Core Store and Runtime Hardening

### Canonical Stores

- Decide the final v0.9 stance for non-Postgres runtime state.
  Acceptance: live/operator flows require Postgres-backed state.
  Status: implemented through a Postgres-required `open_state_store`; unit tests opt into an
  isolated in-process store explicitly.
- Fail when stateful commands run without Postgres.
  Acceptance: operators get a clear error to use the containerized Postgres-backed stack.
  Status: implemented.
- Audit every `StateStore` call site for assumptions inherited from prototype local storage.
  Acceptance: task, chat, memory, knowledge, auth, team, and orchestrator flows work with Postgres.
  Status: implemented across local tests and live smoke; clean-stack sentinel pass remains.
- Remove live-operator documentation for non-Postgres runtime state.
  Acceptance: README and architecture docs describe Postgres plus Redis/Qdrant/Neo4j as required live
  systems.
  Status: implemented.

### Postgres and Migrations

- Expand `brigade db status` to distinguish applied, pending, failed, and unknown migrations.
  Acceptance: status output is actionable after success, partial failure, and drift.
  Status: implemented.
- Add migration failure reports.
  Acceptance: failed migration runs produce machine-readable JSON and human-readable summary.
  Status: implemented with `brigade_schema_migration_failures` and CLI JSON.
- Test partial migration recovery.
  Acceptance: a deliberately failing test migration does not leave the operator without next steps.
- Verify migrations in the clean container flow.
  Acceptance: a new stack runs `brigade db migrate`, then `brigade db status` reports no pending work.
- Document when migrations run automatically versus manually.
  Acceptance: startup behavior is predictable for orchestrator, CLI, and web services.

### Redis Queue Normalization

- Promote pending work to an explicit Redis-backed queue path.
  Acceptance: queued work can be listed, claimed, acknowledged, failed, and recovered.
  Status: implemented with named Redis runtime keys and `brigade datastore inspect --backend redis`.
- Keep active execution claims in Redis with owner, lease token, TTL, and assigned agent.
  Acceptance: duplicate `agent run`, `run-all`, and `orchestrator cycle` paths cannot double-execute work.
  Status: implemented with Redis claims plus Postgres durable records.
- Move alert queue behavior into a named Redis path.
  Acceptance: alerts can be enqueued, drained, replayed after restart, and archived durably.
  Status: implemented for enqueue/inspection; durable archive remains Postgres alerts.
- Harden the local inference lock.
  Acceptance: stale locks expire or can be recovered safely without overlapping Ollama jobs.
  Status: implemented with Redis TTL-backed lock and isolated in-process store coverage.
- Add Redis restart tests.
  Acceptance: restart does not lose durable work or create duplicate execution side effects.
  Status: non-dropping recovery script inspects Redis and validates pending queue reconciliation; clean
  empty-volume rerun remains.

### Qdrant Memory

- Verify Qdrant episode writes with real container state.
  Acceptance: user chat, group chat, memory archive, and knowledge ingestion create retrievable Qdrant records.
  Status: write path and operator inspection are implemented; clean-stack sentinel rerun remains.
- Add Qdrant recovery checks to `check-recovery`.
  Acceptance: episode counts or sentinel IDs survive container recreation without volume loss.
  Status: live Qdrant inspection command added; non-dropping recovery passed after smoke writes.
- Stop storing raw transcripts as vector memory.
  Acceptance: Qdrant receives curated episode/summary records with source references back to Postgres.
  Status: implemented; Qdrant inspection reports source-kind counts.
- Add a simple query/read path for operator inspection.
  Acceptance: an operator can confirm what Qdrant stores without using raw container tools.
  Status: `brigade datastore inspect --backend qdrant`.

### Neo4j Provenance

- Verify Neo4j provenance writes with real container state.
  Acceptance: document, chunk, task, decision, team, and organization graph records appear in Neo4j.
  Status: write paths are implemented; clean-stack sentinel rerun remains.
- Add relationship edges for key provenance flows.
  Acceptance: documents link to chunks; tasks link to agents/goals; decisions link to assignments.
  Status: implemented for document/chunk, task/agent/goal, decision/assignment, and team/agent.
- Add Neo4j recovery checks to `check-recovery`.
  Acceptance: provenance sentinel nodes survive container recreation without volume loss.
  Status: live Neo4j inspection command added; non-dropping recovery passed after smoke writes.
- Add a simple graph inspection command or report.
  Acceptance: operators can validate provenance without direct Cypher knowledge.
  Status: `brigade datastore inspect --backend neo4j` and `brigade org graph --persist`.

### Wipe, Reseed, Backup, and Restore

- Add a full-wipe blank-userland script.
  Acceptance: `./ops/full-wipe.sh --confirm-full-wipe` or equivalent creates a backup, drops
  runtime volumes, rebuilds the stack, runs migrations, verifies health, and does not reseed MVP
  users, agents, goals, or mission state.
- Run `./ops/v07-wipe-reseed.sh --confirm-wipe` in a controlled pass.
  Acceptance: backup is created, volumes are recreated, migrations run, and MVP defaults are reseeded.
- Verify restore after wipe.
  Acceptance: a saved backup can restore source/config/app data/Postgres/volumes as documented.
- Add explicit safety text before destructive wipe operations.
  Acceptance: scripts require confirmation flags and explain what is preserved versus deleted.
- Update backup exclusions.
  Acceptance: source tarballs exclude `reference/`, `web/node_modules/`, `web/dist/`, caches, and runtime data.

### Security and Configuration

- Alert on default or weak JWT secret.
  Acceptance: `alert audit --include-health` reports default dev secret use.
  Status: implemented.
- Fail closed when `BRIGADE_REQUIRE_AUTH=true`.
  Acceptance: CLI/web write paths reject unauthenticated or invalid actors.
  Status: implemented with CLI auth checks and ASGI web API tests.
- Warn when web binds to a reachable host with auth disabled.
  Acceptance: operators get a clear warning for `0.0.0.0` or non-local bind without auth.
  Status: implemented through alert audit and web startup warning.
- Add config edit conflict protection.
  Acceptance: settings edits include base hash/version and reject stale writes.
  Status: implemented with `config_hash` and `config set --base-hash`.
- Keep secret editing out of web/TUI until redaction and restore behavior are explicit.
  Acceptance: UI never displays raw secrets and cannot overwrite them accidentally.

### TUI Hardening

- Test narrow terminal rendering for dashboard, chat, and settings.
  Acceptance: no crashes or unreadable overlap at small terminal widths.
  Status: implemented with safe curses writes and plain-render tests.
- Test long messages and oversized settings payloads.
  Acceptance: views truncate or wrap predictably without corrupting terminal state.
  Status: implemented with bounded plain rendering.
- Test empty state and no-agent state.
  Acceptance: TUI gives actionable output instead of tracebacks.
  Status: implemented for existing plain views.
- Preserve non-TTY plain output.
  Acceptance: `--plain` remains stable for scripts and CI.
  Status: implemented and covered by tests.

### Operator Architecture Documentation

- Write a practical operator architecture document.
  Acceptance: document covers components, interfaces, memory structure, dreaming/rumination cycle,
  self-improvement systems, data flow, failure domains, and source of authority.
  Status: implemented in `OPERATING_ARCHITECTURE.md`.
- Document the intended network topology.
  Acceptance: one diagram or request-path section explains CLI, web, orchestrator, datastores, and Ollama.
  Status: implemented in `NETWORK_TOPOLOGY.md`.
- Document memory creation and retrieval.
  Acceptance: raw transcripts, curated episodes, Qdrant vectors, and Neo4j provenance boundaries are clear.
  Status: implemented in `MEMORY_ARCHITECTURE.md`.
- Document library ingestion.
  Acceptance: Markdown/text ingestion, source refs, chunking, provenance, and future PDF/web/repo paths are clear.
  Status: implemented in `LIBRARY_SYSTEMS.md`.
- Document system prompt build and rumination.
  Acceptance: operators understand what context agents receive and where future dreaming cycles fit.
  Status: implemented in `PROMPT_ARCHITECTURE.md`.

## v0.9.1 External Connections

Scope: external/non-default integrations. Local Ollama is treated as the internal/default live
runtime provider when `BRIGADE_OLLAMA_BASE_URL` is configured and is not part of this external
connection milestone.

### Telegram Wrapper

- Add BotFather setup documentation.
  Acceptance: operator can create a bot token and configure it without committing secrets.
- Add a Telegram inbound message wrapper.
  Acceptance: a Telegram message can create a user chat event routed through OpenBrigade.
  Status: implemented as CLI/testable payload wrapper.
- Add outbound reply support.
  Acceptance: agent response can be sent back to the Telegram conversation.
  Status: remaining.
- Add auth and allowlist controls.
  Acceptance: unknown Telegram users are blocked or routed to owner approval by default.
  Status: initial allowlist blocking implemented; owner approval flow remains.
- Add tests with fake Telegram payloads.
  Acceptance: no live Telegram network is required for unit tests.
  Status: implemented.
  Remaining: live disabled-by-default smoke, outbound send fake, size/rate limits, and operator setup doc.

### Google Chat Wrapper

- Add Google Chat app setup documentation.
  Acceptance: operator knows required project/app/webhook settings and secret storage.
- Add inbound Google Chat message handling.
  Acceptance: a Google Chat message can create a user chat event routed through OpenBrigade.
  Status: implemented as CLI/testable payload wrapper.
- Add outbound reply support.
  Acceptance: agent response can be returned to the originating Google Chat space/thread.
  Status: remaining.
- Add user identity mapping.
  Acceptance: Google identities map to OpenBrigade users or pending approval records.
  Status: initial allowlist metadata implemented; durable identity approval flow remains.
- Add tests with fake Google Chat events.
  Acceptance: no live Google network is required for unit tests.
  Status: implemented.
  Remaining: live disabled-by-default smoke, outbound send fake, size/rate limits, and operator setup doc.

### OpenAI/Codex Model Connection

- Add model connection configuration for OpenAI/Codex routes.
  Acceptance: operator can configure model credentials without exposing them in config output.
  Status: implemented through LiteLLM `openai` alias and redacted settings.
- Add OAuth/API-key documentation for the supported first pass.
  Acceptance: setup doc states exactly which auth mode is implemented and which is future work.
  Status: remaining.
- Add model route smoke test.
  Acceptance: a configured model can complete a bounded prompt and record usage metadata.
  Status: remaining for live external route; unit alias tests are implemented.
- Add failure behavior for missing/invalid credentials.
  Acceptance: missing credentials produce actionable errors and alerts, not tracebacks.
  Status: remaining.

### Google/Gemini Model Connection

- Add Gemini API/OAuth configuration path.
  Acceptance: operator can configure Gemini credentials without exposing them in config output.
  Status: implemented through LiteLLM `gemini` alias and redacted settings.
- Add Gemini route smoke test.
  Acceptance: a configured Gemini model can complete a bounded prompt and record usage metadata.
  Status: remaining for live external route; unit alias tests are implemented.
- Add failure behavior for missing/invalid credentials.
  Acceptance: missing credentials produce actionable errors and alerts.
  Status: remaining.

### External Connection Security

- Never store external provider secrets in agent workspaces.
  Acceptance: secrets live only in `.env`, secret store, or explicitly documented local config.
- Add connector rate limits and message size limits.
  Acceptance: oversized or repeated inbound messages are rejected or throttled.
- Add connector audit records.
  Acceptance: inbound and outbound external events are traceable by provider, user, and conversation.
  Status: initial inbound audit metadata exists for Telegram and Google Chat; outbound audit remains.
- Add connector disable switches.
  Acceptance: each external connection can be disabled without editing code.
  Status: remaining.

## v0.9.2 Web UI/UX Overhaul

Scope: make the web interface useful for daily operation. Backend auth/API safety moved into the
v0.9.0 core hardening pass and should remain covered by automated tests. v0.9.2 is primarily
browser workflow, information architecture, responsive layout, and role-aware interaction design.

### Auth Threat Model

- Threat-model zero users.
  Acceptance: bootstrap behavior is documented and safe.
  Status: backend behavior covered by ASGI tests; operator-facing UI state remains.
- Threat-model one user and one owner.
  Acceptance: implicit-user behavior cannot accidentally expose write access when auth is required.
  Status: backend behavior covered by ASGI tests; browser messaging remains.
- Threat-model `require_auth=false` on a reachable host.
  Acceptance: web gateway warns loudly or refuses unsafe configuration.
  Status: backend warning/audit implemented; browser warning banner remains.
- Threat-model stale, expired, malformed, and CR/LF-bearing bearer tokens.
  Acceptance: all fail cleanly with no route execution.
  Status: backend malformed/expired token coverage implemented; React expiry handling remains.

### Reliable API Integration Tests

- Replace route-registration-only web tests with running-service tests.
  Acceptance: tests hit a live `brigade_web` process or equivalent reliable ASGI harness.
  Status: implemented with direct ASGI harness; live auth-enabled smoke remains.
- Cover `/api/auth/me`.
  Acceptance: valid, missing, malformed, and expired token cases are tested.
- Cover `/api/auth/token`.
  Acceptance: owner can issue; unauthorized users cannot.
- Cover `/api/chat/ask-agent`.
  Acceptance: send/response persists messages, usage, and episodes.
- Cover `/api/settings/effective`.
  Acceptance: secrets are redacted and API version is present.
- Cover `/api/teams` and denied writes.
  Acceptance: observers can read but cannot mutate; operators/owners behave as intended.
  Status: implemented for denied writes; broader role-aware browser behavior remains.

### Browser and Gateway Security

- Add CSP and core security headers.
  Acceptance: web responses include CSP, no-sniff, frame, and referrer policy headers.
  Status: implemented with ASGI middleware and smoke-tested.
- Keep CORS same-origin by default.
  Acceptance: cross-origin access is disabled unless explicitly configured.
  Status: implemented by default absence of permissive CORS.
- Add token expiry handling in the React UI.
  Acceptance: expired token state is visible and does not loop or silently fail.
  Status: remaining.
- Add role-aware UI behavior.
  Acceptance: observer/operator/owner see appropriate controls and denied actions are clear.
  Status: remaining.
- Add web smoke with auth enabled.
  Acceptance: full web workflow passes with `BRIGADE_REQUIRE_AUTH=true`.
  Status: remaining live validation.

### Operator UX

- Supplement the novelty-first ops room with a work-first operator cockpit.
  Acceptance: first screen shows mission, agent health, queued/active work, blockers, alerts, and datastore health.
  Note: Ops Room will be accessible via a toggle on the interface, to be built out more in v0.9.4 and beyond.
- Make task workflows useful.
  Acceptance: operator can create, inspect, filter, and follow task history without dropping to the CLI.
- Make chat workflows useful.
  Acceptance: operator can pick agent/channel, see pending/error states, and understand persisted side effects.
- Make team/hierarchy workflows useful.
  Acceptance: teams, Crew Chiefs, members, delegation policy, escalation paths, and current workload are visible and editable where permitted.
- Make settings/status safe and clear.
  Acceptance: secrets stay redacted, config hash/stale-write failures are visible, and unsafe auth/bind state is obvious.
- Add responsive layout and browser tests.
  Acceptance: desktop and mobile layouts do not overlap text or controls; Playwright or equivalent screenshots cover the main flows.

### Web Runtime and Docker

- Start `brigade_web` and `brigade_orchestrator` together under Compose.
  Acceptance: both services run against the same datastore state without collisions.
  Status: implemented and rebuilt successfully on the live stack.
- Hit `/healthz` and `/`.
  Acceptance: backend health and built React UI both respond.
  Status: implemented and smoke-tested after Docker rebuild.
- Send a web chat and verify persisted side effects.
  Acceptance: messages, usage, and episode records appear through CLI/status queries.
  Status: implemented in ASGI tests; live auth-enabled smoke remains.
- Recreate containers without volume loss.
  Acceptance: web, auth, messages, and hierarchy state remain intact.
  Status: non-dropping recovery passed; auth-enabled browser flow remains.

## v0.9.3 PR Candidate

### Clean Container New-User Pass

- Build from clean source in containers.
  Acceptance: `docker compose --env-file .env --profile app build` succeeds.
  Status: `brigade_web` and `brigade_orchestrator` builds pass; Dockerfiles now package
  `web/public` pixel assets.
- Start from empty volumes.
  Acceptance: clean stack starts without requiring old prototype state.
- Run migrations and first-user setup.
  Acceptance: first owner/operator can be created and issued a token.
- Onboard a fresh agent/team structure.
  Acceptance: blank agents, teams, Crew Chiefs, goals, and route/delegate/escalate flows work.
- Run baseline commands.
  Acceptance: tests, lint, compile, health, migration status, dashboard, TUI, web, stress, heartbeat, and recovery checks pass.
  Status: tests/lint/frontend build/Compose config/Docker build/live health/db status/web smoke/bad
  heartbeat with cleanup/fake stress/non-dropping recovery pass.

### Manual Existing-Agent Migration Pass

- Create a blank OpenBrigade agent.
  Acceptance: workspace validates before migration material is added.
- Manually paste selected identity/memory/tools/user/soul material from one existing agent.
  Acceptance: copied material does not break workspace validation.
- Run a safe first assignment.
  Acceptance: migrated agent responds coherently without affecting the original agent.
- Compare behavior against the original agent.
  Acceptance: operator notes what migrated cleanly, what was confusing, and what is missing.
- Decide whether automated migration is needed later.
  Acceptance: PR candidate can document manual migration limits honestly.

### Public Repo Cleanup

- Confirm generated artifacts are excluded.
  Acceptance: `web/node_modules/`, `web/dist/`, caches, backups, local state, and volume snapshots are not included.
- Confirm reproducible frontend builds.
  Acceptance: `web/package-lock.json` exists and Docker uses `npm ci`.
- Review docs for secrets and host-specific paths.
  Acceptance: `.env.example` is safe and docs do not expose local secrets.
- Review reference-derived code and attribution.
  Acceptance: copied/adapted material has license and attribution notes where needed.

### PR Readiness Gates

- Update `README.md`, `PROTOTYPE.md`, `RELEASE_CHECKLIST.md`, and `TODO.md`.
  Acceptance: docs match the actual CLI, Docker services, web flow, and migration policy.
- Run final local validation.
  Acceptance: `python3 -m pytest`, `python3 -m ruff check .`, and compileall pass.
- Run final live validation.
  Acceptance: clean stack, migrations, auth-enabled web smoke, stress tests, bad heartbeat tests, and recovery pass.
  Note: external connector smoke is now a v0.9.1 gate, not part of v0.9.0 core validation.
- Prepare GitHub publication.
  Acceptance: repository is clean, public-facing docs are accurate, and known limitations are listed.

## v0.9.4 Pixel Agents Integration and Live Ops Room Buildout

Scope: continue the Pixel Agents-inspired interface after the v0.9.3 PR-candidate hardening track.
This milestone should improve the Live Ops Room without blocking clean-stack break testing, auth
hardening, migration validation, or v1.0 publication readiness. The custom OpenBrigade web UI remains
the product surface; VS Code extension behavior, Claude hooks, terminal/session scanners, and Pixel
Agents provider runtime code remain out of scope.

### Integration Boundaries and Attribution

- Keep `/opt/openbrigade/reference/pixel-agents` current before each integration pass.
  Acceptance: work starts from a known upstream commit and notes the reviewed Pixel Agents revision.
- Preserve MIT attribution for copied Pixel Agents assets and any adapted code.
  Acceptance: bundled assets include license and attribution files; docs mention asset provenance.
- Keep the OpenBrigade API as the source of truth.
  Acceptance: visual state is derived from OpenBrigade agents, teams, assignments, goals, alerts,
  usage, and runtime status, not Pixel Agents hook/session files.
- Maintain a strict boundary around copied code.
  Acceptance: imported logic is limited to rendering/layout concepts and asset handling; no VS Code
  adapter, Claude provider, or hook installer enters OpenBrigade runtime code.

### Live Ops Room UX

- Add a clear cockpit/ops-room switch in the web interface.
  Acceptance: default operator cockpit remains work-first; Ops Room is one click away and preserves
  current selection/state when toggled.
- Improve room readability for real OpenBrigade state.
  Acceptance: working, queued, blocked, awaiting-human, idle, cloud-running, and local-inference-lock
  states are visually distinct and explained in the side panel.
- Add team-aware visualization.
  Acceptance: Crew Chiefs, line workers, team membership, escalation paths, and active team workload
  are visible without opening raw payload views.
- Improve agent overlays.
  Acceptance: labels show agent display name, status, active task summary, blocker indicator, and
  token/cost signal without overlapping at normal desktop sizes.
- Add useful empty-state behavior.
  Acceptance: no-agent, no-mission, no-task, and auth-required states give clear next actions.

### Layout and Asset Handling

- Harden per-user seat persistence.
  Acceptance: seats persist per authenticated user across refresh, browser restart, and container
  restart with Postgres-backed state.
- Add layout reset and auto-arrange.
  Acceptance: operator can reset seats to default and auto-place all current agents without editing
  raw payloads or clearing browser storage.
- Decide whether v0.9.4 includes furniture editing or remains seat-only.
  Acceptance: decision is documented before implementation; if deferred, furniture editor remains
  explicitly listed as post-v1.0.
- Validate bundled asset loading in Docker.
  Acceptance: `brigade_web` serves sprites/layout/attribution files from built assets in a clean
  container.
- Add an asset failure fallback.
  Acceptance: missing sprites or failed image loads show a nonblank room or actionable error instead
  of a broken canvas.

### Actions and API Wiring

- Expand Ops Room actions only where they are already safe in backend RBAC.
  Acceptance: observer, operator, and owner permissions are reflected in visible controls and denied
  actions produce clear UI errors.
- Add task drilldown from room labels.
  Acceptance: clicking an active/blocked agent opens assignment details, blockers, progress, and
  relevant history.
- Add team and agent quick filters.
  Acceptance: operator can focus one team, one agent, blocked work, or queued work.
- Keep write paths on REST and read paths on snapshot/SSE for v0.9.4.
  Acceptance: no WebSocket protocol is introduced unless explicitly re-scoped.
- Add graceful SSE reconnect/backoff.
  Acceptance: temporary disconnects show degraded status, resume automatically, and do not create
  duplicate saves or duplicate actions.

### Testing and Hardening

- Replace route-registration-only Ops Room tests with reliable running-service coverage.
  Acceptance: `/api/ops-room`, `/api/ops-room/events`, layout save/load, mission save, goal add,
  task create, and chat actions are tested through an ASGI or live-service harness that does not hang.
- Add frontend smoke coverage.
  Acceptance: production build plus browser-level smoke confirms the canvas is nonblank, assets load,
  seat drag/save works, and main panels render on desktop and mobile widths.
- Add auth-enabled web smoke.
  Acceptance: owner can use all v0.9.4 controls; observer sees read-only state and cannot write.
- Add Docker clean-stack validation for the room.
  Acceptance: clean `brigade_web` container serves the built room and connects to the same Postgres
  state as CLI/orchestrator.
- Add visual regression screenshots if Playwright is available.
  Acceptance: baseline screenshots cover cockpit, Ops Room, selected agent, blocked state, and mobile
  layout.

### Deferred Beyond v0.9.4 Unless Re-scoped

- Full Pixel Agents furniture/floor/wall editor.
- WebSocket command protocol.
- VS Code extension behavior.
- Claude hook/session transcript ingestion.
- Original kitchen/brigade-themed custom asset pack.
- Goal edit/delete until OpenBrigade has stable goal IDs.
