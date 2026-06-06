# v0.1 MVP Hardening Research Notes

Source TODO section: `TODO.md` "v0.1 MVP Hardening". Scope here is research and implementation clarification only.

## Replace Local File State with a real repository layer for Postgres and Redis

For OpenBrigade, this means splitting state by durability:

- Redis owns active runtime state: active assignments, pending queues, alert queues, local inference lock, and in-flight orchestrator cycle state.
- Postgres owns durable history: completed assignment records, chats/transcripts, reasoning logs, users, knowledge metadata, audit records, and archived Redis assignment outcomes.
- Repository interfaces should be explicit and narrow, for example `AssignmentRepository`, `RuntimeQueueRepository`, `ReasoningRepository`, `ChatRepository`, `UserRepository`, and `KnowledgeRepository`. Avoid direct datastore calls from orchestrator logic.

Useful references:

- `OpenBrigade_V0.1_Design_Summary.md:21-28` states the intended four-store split and names Postgres, Redis, Qdrant, Neo4j, LiteLLM, and Ollama as v0.1 infrastructure.
- `OpenBrigade-Concept.md:157-163` is the clearest source for the Redis/Postgres boundary: Redis is canonical for active runtime records and queues; completed, failed, abandoned, or superseded records are archived into Postgres before removal from Redis.
- `reference/hermes-agent/hermes_cli/kanban_db.py:753-882` is not Postgres, but it is the best repository/schema example for task persistence. It models tasks, task links, task events, task runs, failure counters, idempotency, indexes, and notification subscriptions.
- `reference/hermes-agent/hermes_cli/kanban_db.py:1172-1188` shows the importance of explicit write transactions around multi-statement task claims and event writes. The OpenBrigade Postgres layer should use transactions and row locks/advisory locks for assignment claims.
- `reference/hermes-agent/tests/test_mcp_serve.py:125-155` shows a minimal chat/session schema with `sessions` and `messages`, including reasoning fields. This is useful when designing durable chat storage.

No good real Postgres repository example exists in the corpus. The nearest reference is the Hermes SQLite task/session persistence above, adapted to Postgres with SQLAlchemy/asyncpg and Alembic.

## Add an Alembic-backed migration runner

For OpenBrigade, add a first-class `brigade migrate` command plus startup preflight that verifies the database is at the required revision before orchestrator cycles run. Migrations should create the v0.1 durable schema and be idempotent at the command boundary, not via ad hoc `CREATE TABLE` calls in runtime code.

Useful references:

- `OpenBrigade_V0.1_Design_Summary.md:323-323` explicitly includes Alembic migrations in v0.1 scope.
- `OpenBrigade_V0.1_Design_Summary.md:353-353` says the next step is to write the first Alembic migration.
- `reference/openclaw/src/plugin-sdk/migration-runtime.ts:204-233` shows a useful migration-report pattern: write both machine-readable `report.json` and human-readable `summary.md`.
- `reference/openclaw/src/infra/state-migrations.ts:121-143` shows a migration runner returning `changes` and `warnings`, which maps well to CLI output and logs.
- `reference/hermes-agent/hermes_cli/kanban_db.py:970-1035` shows additive migration logic for legacy schemas and why rename-based migration can be brittle.

No Alembic example exists in the corpus. Use Alembic's standard `env.py`, version table, and revision files, but borrow the report/preflight behavior from OpenClaw.

## Start and smoke-test the full `brigade_` Docker stack

For OpenBrigade, the smoke test should run `docker compose --env-file .env config`, start the stack, wait for Postgres/Redis/Qdrant/Neo4j health, run migrations, and run `brigade health --json`. The stack should use `brigade_` service/container/volume names to avoid collisions.

Useful references:

- `reference/openclaw/docker-compose.yml:12-21` shows why container-side runtime paths should be pinned instead of allowing host `.env` paths to leak into containers.
- `reference/openclaw/docker-compose.yml:78-89` shows a Compose healthcheck with interval, timeout, retries, and start period.
- `reference/openclaw/docker-compose.yml:56-67` shows useful hardening defaults: drop network capabilities, no-new-privileges, explicit ports, `init: true`, and restart policy.
- `reference/openclaw/src/docker-setup.e2e.test.ts:18-49` shows Docker stubbing for deterministic tests of setup scripts without touching a real Docker daemon.
- `reference/openclaw/src/docker-setup.e2e.test.ts:93-127` shows test env construction with isolated config/workspace directories.
- `reference/hermes-agent/docker-compose.yml:24-71` is a simpler Compose example with persistent volume mounts, UID/GID notes, localhost-only dashboard, and security warnings.

## Add Redis queues for active assignments, pending work, alerts, and local inference lock

For OpenBrigade, Redis queues should be named and typed by purpose, for example `brigade:assignments:active`, `brigade:work:pending`, `brigade:alerts`, and `brigade:locks:local-inference`. Queue entries should have IDs, idempotency keys where appropriate, retry metadata, timestamps, and clear ack/fail semantics.

Useful references:

- `reference/openclaw/src/infra/session-delivery-queue-storage.ts:61-67` defines a queued entry shape with `id`, `enqueuedAt`, `retryCount`, `lastAttemptAt`, and `lastError`.
- `reference/openclaw/src/infra/session-delivery-queue-storage.ts:116-154` shows enqueue, idempotency-key handling, ack, and fail/update behavior.
- `reference/openclaw/src/infra/session-delivery-queue-recovery.ts:31-35` defines max retries, backoff schedule, and in-progress guards.
- `reference/openclaw/src/infra/session-delivery-queue-recovery.ts:126-199` shows a drain loop that prevents duplicate drains, reloads entries before retry, applies max retries, and respects backoff.
- `reference/openclaw/src/process/command-queue.ts:54-88` shows lane-based queue metadata: queued count, active count, max concurrency, draining flag, and generation.
- `reference/openclaw/src/infra/gateway-lock.ts:18-30` shows lock payload shape with PID, created time, config path, and process start time. For Redis, keep owner, lease token, acquired time, TTL, and purpose.

## Persist completed assignments, chats, reasoning logs, users, and knowledge records in Postgres

For OpenBrigade, this should become the initial schema:

- `assignments`: current durable task identity, status, assignee, priority, parent/superseded links, failure counters, timestamps.
- `assignment_runs`: each agent attempt/cycle with status, outcome, summary, model/provider, error, token/cost metadata.
- `assignment_events`: append-only audit for transitions, retries, blocks, alerts, and human interventions.
- `chat_sessions` and `chat_messages`: raw full-text user chat, heartbeat transcript references, inter-agent chats, reasoning metadata.
- `orchestrator_reasoning`: one record per orchestrator cycle with input snapshot references, decision summary, raw model response or parsed decision, assignments created, failures handled, and previous reasoning pointer.
- `users`: local MVP users and role assignments.
- `knowledge_records`: ingested object metadata and links to Qdrant/Neo4j IDs.

Useful references:

- `reference/hermes-agent/hermes_cli/kanban_db.py:754-881` is the best schema reference for task/run/event persistence and indexes.
- `reference/hermes-agent/tests/test_mcp_serve.py:125-155` shows the session/message split and reasoning-related message fields.
- `reference/hermes-agent/tests/test_hermes_state.py:1438-1450` shows tests that assert schema existence and versioning. OpenBrigade should have similar migration tests against Postgres.
- `OpenBrigade_V0.1_Design_Summary.md:212-231` defines task status and heartbeat status outcomes that the schema must represent.

## Add integration tests marked separately from unit tests

For OpenBrigade, default test runs should exclude integration tests. Integration tests should be opt-in, marked, and able to start against the local Compose stack or skip cleanly when services are unavailable.

Useful references:

- `reference/hermes-agent/pyproject.toml:221-224` registers an `integration` marker and defaults `pytest` to `-m 'not integration'`.
- `reference/hermes-agent/tests/integration/test_batch_runner.py:9-10` applies `pytestmark = pytest.mark.integration` at module level.
- `reference/hermes-agent/tests/conftest.py:652-659` registers a custom marker dynamically.
- `reference/hermes-agent/tests/conftest.py:662-683` shows an autouse safety guard preventing tests from touching live system processes unless explicitly bypassed.
- `reference/pixelagent/tests/pytest.ini:11-18` shows provider/functionality marker organization.

## Wire `brigade health` to real Postgres, Redis, Qdrant, and Neo4j checks

For OpenBrigade, `brigade health` should return both human text and `--json`. It should check connectivity, latency, schema/migration version, basic read/write capability where safe, and stale runtime conditions. Any datastore down should fail loud and prevent orchestrator cycles.

Useful references:

- `OpenBrigade_V0.1_Design_Summary.md:300-300` says datastore failure should fail loud and refuse to start a cycle.
- `reference/openclaw/docs/cli/health.md:14-19` documents `--json`, timeout, verbose, and debug options.
- `reference/openclaw/src/commands/health.ts:59-75` uses a default timeout and debug flag for health behavior.
- `reference/openclaw/src/commands/health-format.ts:24-52` separates failure formatting from probe execution.
- `reference/openclaw/src/commands/health-format.ts:54-99` shows probe result formatting with `ok`, elapsed time, status, and error.
- `reference/proactive-claw-1.2.41/scripts/health_check.py:48-69` shows DB integrity checks and structured status output.
- `reference/proactive-claw-1.2.41/scripts/health_check.py:190-237` shows stale-data checks using UTC timestamps.

## Add Ollama integration tests gated by local availability

For OpenBrigade, Ollama tests should only run when explicitly enabled and a local endpoint/model is available. They should cover native local chat, model discovery, timeout behavior, and the local inference lock.

Useful references:

- `reference/openclaw/extensions/ollama/ollama.live.test.ts:11-18` gates live tests with env vars and default local endpoint/model values.
- `reference/openclaw/extensions/ollama/ollama.live.test.ts:117-151` tests a real local CLI path and asserts provider/model/transport output.
- `reference/openclaw/extensions/ollama/ollama.live.test.ts:153-220` tests native chat payload handling, model params, tool schema normalization, and stream completion.
- `reference/openclaw/extensions/ollama/index.ts:149-210` shows provider registration, non-interactive setup, catalog discovery, and provider metadata.

## Add LiteLLM adapter for OpenAI, Anthropic, and Gemini API-key use

For OpenBrigade, LiteLLM should be a cloud-provider adapter behind a common model interface. It should support API-key configuration, custom base URL, provider/model refs, JSON config, and deterministic tests without real API calls.

Useful references:

- `reference/openclaw/extensions/litellm/index.ts:43-107` registers a LiteLLM provider, API-key auth, docs path, env var, catalog, and image-generation provider.
- `reference/openclaw/extensions/litellm/index.ts:18-40` normalizes custom base URLs and writes provider config.
- `reference/openclaw/extensions/litellm/provider-catalog.ts:4-9` maps LiteLLM to an OpenAI-compatible completions API.
- `reference/openclaw/extensions/litellm/index.test.ts:31-106` tests non-interactive API-key setup and custom base URL without a real provider call.
- `reference/pixelagent/blueprints/multi_provider/core/base.py:27-57` is a Python provider-agnostic agent constructor pattern, though it uses Pixeltable rather than LiteLLM.

## Add real agent runner mode using provider responses, not only stubbed test completion

For OpenBrigade, the runner should use test stubs in unit tests and real provider-backed sessions at runtime. Real mode should record provider, model, usage, raw/final text, tool calls, errors, and transcript references, then update the Redis assignment record.

Useful references:

- `reference/openclaw/src/agents/cli-runner.ts:167-220` shows a real runner entry point that prepares context, runs the agent, and cleans up live resources in `finally`.
- `reference/openclaw/src/agents/cli-runner.ts:223-260` shows run context construction with session ID, provider, model, system prompt, prompt, history messages, images count, workspace, trigger, and job ID.
- `reference/pixelagent/blueprints/multi_provider/core/base.py:136-202` shows the basic real agent flow: store user message, trigger completion, collect response, store assistant response.
- `reference/openclaw/extensions/ollama/ollama.live.test.ts:117-151` is the best live-provider acceptance test shape for a real runner path.

## Add structured orchestrator reasoning records per cycle

For OpenBrigade, every orchestrator cycle should persist structured reasoning separately from chat history. Store cycle ID, started/ended timestamps, mission/goals snapshot refs, prior reasoning ref, agent state snapshot refs, task queue snapshot refs, model/provider, decision summary, assignments emitted, blocks/failures handled, parse/retry status, and raw response location.

Useful references:

- `OpenBrigade_V0.1_Design_Summary.md:65-83` defines per-cycle context and run sequence, including loading previous reasoning and logging current reasoning.
- `OpenBrigade-Concept.md:140-153` says orchestrator reasoning belongs in its own table, not conversation history.
- `reference/hermes-agent/tests/run_agent/test_last_reasoning_per_turn.py:12-25` shows current-turn reasoning extraction and why stale prior reasoning must not leak into the next turn.
- `reference/hermes-agent/tests/run_agent/test_last_reasoning_per_turn.py:64-78` explicitly tests that prior-turn reasoning is not reused when the current turn has none.
- `reference/hermes-agent/tests/run_agent/test_deepseek_reasoning_content_echo.py:111-192` shows provider-specific reasoning replay hazards. OpenBrigade should store orchestrator reasoning for audit, but avoid blindly replaying hidden provider reasoning across providers.

## Add retry handling for malformed model output, blocked tasks, failed tasks, and 5-failure alerts

For OpenBrigade, implement retries at distinct levels:

- Malformed model output: 3 attempts with exponential backoff matching the spec.
- Transient provider errors: retry only for timeouts, 5xx, and network signals; do not retry invalid auth, validation, or missing model.
- Blocked/failed tasks: persist failure event, increment consecutive failure count, let orchestrator reassign or request failure analysis.
- Five consecutive failures: create an alert and mark the task blocked awaiting human intervention.

Useful references:

- `OpenBrigade_V0.1_Design_Summary.md:96-100` defines failed task behavior and the 5-failure alert rule.
- `OpenBrigade_V0.1_Design_Summary.md:294-302` defines malformed LLM retries, rate-limit behavior, datastore failure behavior, and 5-failure alerts.
- `reference/openclaw/src/provider-runtime/operation-retry.ts:145-171` classifies transient provider errors and explicitly excludes common permanent failures.
- `reference/openclaw/src/provider-runtime/operation-retry.ts:180-195` computes exponential retry delay.
- `reference/openclaw/src/infra/session-delivery-queue-recovery.ts:64-96` shows backoff eligibility based on retry count and last attempt time.
- `reference/hermes-agent/hermes_cli/kanban_db.py:772-779` documents consecutive failure counters and last failure text on a task row.
- `reference/hermes-agent/hermes_cli/kanban_db.py:825-852` records each attempt in `task_runs`, including status, outcome, summary, metadata, and error.

## Add backup and restore notes for volumes and state

For OpenBrigade, v0.1 docs should cover backing up and restoring Docker volumes for Postgres, Redis, Qdrant, and Neo4j, plus `brigade.config.json`, `.env` handling, workspace directories, and any local uploaded knowledge files. Notes should distinguish durable data from transient runtime queues/locks.

Useful references:

- `reference/openclaw/docs/cli/backup.md:23-32` lists backup guarantees: manifest, timestamped archive, no overwrite, source-tree output rejection, verify command, and immediate verify option.
- `reference/openclaw/docs/cli/backup.md:34-57` defines backup sources and explicitly skips live-mutation files such as sessions, logs, queues, sockets, pid files, and temp files.
- `reference/openclaw/src/commands/backup-shared.ts:87-145` shows backup planning for state/config/credentials/workspace and an `onlyConfig` mode.
- `reference/openclaw/src/commands/backup-shared.ts:146-225` canonicalizes and deduplicates paths so nested sources are not archived twice.
- `reference/openclaw/src/infra/backup-create.ts:100-173` retries archive writes when live files race with tar.
- `reference/openclaw/src/infra/backup-volatile-filter.ts:67-130` is the clearest volatile-file filter. OpenBrigade should document Redis lock/queue handling similarly.
- `reference/hermes-agent/hermes_cli/backup.py:92-113` shows safe DB copying for SQLite via backup API. For OpenBrigade, use datastore-native tools instead: `pg_dump`/`pg_restore` or volume snapshot for Postgres, Redis RDB/AOF snapshot, Qdrant snapshot, and Neo4j dump.
- `reference/hermes-agent/hermes_cli/backup.py:256-280` validates an archive before restore.
- `reference/hermes-agent/hermes_cli/backup.py:376-388` blocks path traversal and tightens permissions on restored secret files.
