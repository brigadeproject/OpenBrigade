# OpenBrigade v0.9 User and Break Testing

Updated: 2026-05-27

This document replaces the long-running v0.9 testing journal with a current, operator-facing break
test record. The scope here is the v0.9.0 core stack: local/default Ollama, Postgres, Redis, Qdrant,
Neo4j, CLI/TUI, and the current web interface. External connectors move to v0.9.1. The UI/UX
overhaul moves to v0.9.2.

## Current Storage Stance

Stateful operator workflows must run against the real stack. Postgres is required for durable runtime
state, Redis owns runtime coordination, Qdrant owns curated memory vectors, and Neo4j owns provenance
relationships. A command running without required datastore configuration should fail clearly instead
of proceeding without required services.

## Validation Results So Far

- `python3 -m pytest tests/test_store.py tests/test_v0_7_v0_8.py -q`
  Result: `16 passed in 0.90s`.
- `python3 -m pytest -q`
  Result: `129 passed, 1 deselected in 7.65s`.
- `python3 -m ruff check .`
  Result: passed.
- `cd web && npm run build`
  Result: Vite production build passed in `1.39s`.
- Live Docker rebuild from this v0.9 pass:
  `docker compose --env-file .env --profile app up -d --build`
  Result: web and orchestrator images rebuilt and started.
- Live datastore health from this v0.9 pass:
  `./ops/brigade-live.sh health --json`
  Result: Postgres, Redis, Qdrant, and Neo4j were green; migrations `0001` through `0005` were
  applied.
- Live web smoke from this v0.9 pass:
  `/healthz`, `/`, `/api/ops-room`, and bundled pixel-agent asset URLs returned `200`.
- Headless browser smoke from this v0.9 pass:
  the Ops Room loaded and rendered nonblank bundled assets.
- `./ops/stress-concurrency.sh`
  Result: passed.
- `./ops/test-bad-heartbeats.sh`
  Result: passed after cleanup; stale test assignments no longer remain active.
- `./ops/check-recovery.sh`
  Result: non-dropping recovery passed against the current stack.

## Bugs Closed In This Pass

- Runtime state now requires configured datastores for operator workflows.
- `brigade db status` reports an unconfigured store clearly when Postgres is missing.
- Test-only state behavior is explicit in the test harness.
- Docker images package `web/public`, so pixel-agent assets are available in clean containers.
- Bad heartbeat tests clean up their active assignment state.
- Web/API smoke now covers the reachable host-bound interface.
- README, backup notes, TODOs, and architecture docs now describe the real store split.

## Remaining v0.9.0 Break Tests

- Clean empty-volume stack:
  start from removed volumes, run migrations, seed MVP defaults, and confirm health.
- Auth-enabled web smoke:
  set a non-default JWT secret, enable auth, issue an operator token, and verify denied/allowed web
  API paths.
- Partial migration failure recovery:
  run a deliberately failing migration in an isolated database and confirm the operator gets a clear
  failure report plus a safe next step.
- Backup, wipe, reseed, and restore:
  create a backup, wipe runtime volumes, reseed defaults, restore the backup, and verify status.
- Clean-stack Qdrant and Neo4j sentinels:
  write known memory/provenance records after a fresh start and confirm inspection commands can read
  them back after container recreation.
- Local Ollama live smoke:
  with the local Ollama server available, run one bounded model completion and one agent task through
  the internal/default `ollama` route.
- Public repo cleanup:
  confirm generated artifacts, caches, backups, secrets, local state, and volume snapshots are not
  part of the publishable tree.

## Recommended Next Run

1. `docker compose --env-file .env --profile app down`
2. `./ops/backup-prototype.sh`
3. `./ops/v07-wipe-reseed.sh --confirm-wipe`
4. `docker compose --env-file .env --profile app up -d --build`
5. `./ops/brigade-live.sh db status`
6. `./ops/brigade-live.sh health --json`
7. `python3 -m pytest -q`
8. `python3 -m ruff check .`
9. `cd web && npm run build`
10. `./ops/stress-concurrency.sh`
11. `./ops/test-bad-heartbeats.sh`
12. `./ops/check-recovery.sh`

Record any unclear error message, unsafe default, missing cleanup, or datastore mismatch as a
v0.9.0 release blocker unless it is explicitly scoped to v0.9.1 or v0.9.2.
