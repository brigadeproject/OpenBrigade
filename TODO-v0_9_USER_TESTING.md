# v0.9 User and Break Testing Checklist

Use this checklist for the remaining v0.9.0 core validation. External connectors are v0.9.1 work.
The web UI/UX overhaul is v0.9.2 work.

## Rules For This Pass

- Run operator workflows through `./ops/brigade-live.sh ...` or a configured container.
- Treat Postgres, Redis, Qdrant, and Neo4j as required runtime systems.
- Record every confusing step, unclear error, stale record, or unsafe default.
- Keep existing non-OpenBrigade agent workspaces out of scope.
- Preserve backups before destructive wipe/reseed tests.

## Automated Baseline

- Run Python tests.
  Command: `python3 -m pytest -q`
  Expected: all tests pass; current result is `129 passed, 1 deselected`.
  Notes:
- Run lint.
  Command: `python3 -m ruff check .`
  Expected: no lint errors.
  Notes:
- Run frontend build.
  Command: `cd web && npm run build`
  Expected: Vite build succeeds and bundled assets are present.
  Notes:
- Inspect Compose configuration.
  Command: `docker compose --env-file .env config`
  Expected: config renders without missing required values.
  Notes:

## Clean Stack

- Back up current runtime state.
  Command: `./ops/backup-prototype.sh`
  Expected: source/config/app data/datastore backup directory is created.
  Notes:
- Wipe and reseed runtime volumes.
  Command: `./ops/v07-wipe-reseed.sh --confirm-wipe`
  Expected: backup runs, volumes are recreated, migrations run, MVP defaults are seeded.
  Notes:
- Rebuild and start the app profile.
  Command: `docker compose --env-file .env --profile app up -d --build`
  Expected: web, orchestrator, Postgres, Redis, Qdrant, Neo4j, and Ollama proxy start.
  Notes:
- Check migration status.
  Command: `./ops/brigade-live.sh db status`
  Expected: migrations `0001` through `0005` are applied and no failures are present.
  Notes:
- Check datastore health.
  Command: `./ops/brigade-live.sh health --json`
  Expected: Postgres, Redis, Qdrant, and Neo4j are green.
  Notes:

## Required Store Behavior

- Confirm stateful host commands fail clearly when no Postgres DSN is configured.
  Command: run a stateful `brigade ...` command from an intentionally unconfigured environment.
  Expected: command exits with a clear Postgres-required message.
  Notes:
- Confirm live commands use the containerized stores.
  Command: `./ops/brigade-live.sh status --json`
  Expected: status reads live state and does not create host-local runtime state.
  Notes:

## Web and Auth

- Verify web health.
  Command: open `http://127.0.0.1:${BRIGADE_WEB_PORT:-58080}/healthz`
  Expected: healthy JSON response.
  Notes:
- Verify web UI loads.
  Command: open `http://127.0.0.1:${BRIGADE_WEB_PORT:-58080}/`
  Expected: app shell loads, dashboard data refreshes, and asset requests succeed.
  Notes:
- Verify Ops Room assets.
  Command: open the Ops Room and inspect the browser network panel.
  Expected: bundled sprites/layout files load from the app container.
  Notes:
- Run auth-enabled smoke.
  Command: configure a non-default JWT secret, issue an operator token, and call protected APIs.
  Expected: unauthenticated writes are denied; authenticated operator writes succeed.
  Notes:

## Runtime Break Tests

- Run concurrency stress.
  Command: `./ops/stress-concurrency.sh`
  Expected: no duplicate execution claims or lost assignments.
  Notes:
- Run bad heartbeat checks.
  Command: `./ops/test-bad-heartbeats.sh`
  Expected: malformed/stale/mis-targeted heartbeat blocks are blocked safely and cleaned up.
  Notes:
- Run non-dropping recovery.
  Command: `./ops/check-recovery.sh`
  Expected: runtime state remains consistent after container recreation without volume loss.
  Notes:
- Run local Ollama smoke.
  Command: run one bounded `model complete` and one agent task through provider `ollama`.
  Expected: completion succeeds or fails with a clear local availability error.
  Notes:

## Memory and Provenance

- Write a known memory record.
  Command: append, curate, archive, then inspect Qdrant.
  Expected: curated episode records are visible with source references.
  Notes:
- Write a known provenance record.
  Command: ingest a small document and inspect Neo4j.
  Expected: document/chunk nodes and relationships are visible.
  Notes:
- Recreate containers without volume loss.
  Command: restart the stack and rerun Qdrant/Neo4j inspection.
  Expected: sentinel memory/provenance records are still visible.
  Notes:

## Backup and Restore

- Create a backup.
  Command: `./ops/backup-prototype.sh`
  Expected: backup directory includes source/config/app data and datastore snapshots/dumps.
  Notes:
- Restore into a stopped stack.
  Command: `./ops/restore-prototype.sh backups/<timestamp>`
  Expected: restored stack passes health and status checks.
  Notes:
- Verify restored data.
  Command: `./ops/brigade-live.sh status --json`
  Expected: users, agents, goals, assignments, and datastore-backed records match the backup.
  Notes:

## Release Blockers

- Any stateful operator workflow that succeeds without required datastore configuration.
- Any migration failure that leaves no clear recovery report.
- Any auth-enabled web write that bypasses RBAC.
- Any wipe/reseed/restore step that can destroy data without explicit confirmation.
- Any bundled web asset missing from a clean Docker image.
- Any recovery run that loses durable work or duplicates active execution.
