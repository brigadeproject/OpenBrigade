# Backup and Restore

OpenBrigade's live MVP state is the containerized `brigade_` stack:

- Postgres: durable records, assignments, users, agents, teams, goals, messages, transcripts,
  reasoning, usage, reports, UI layouts, and alerts.
- Redis: runtime queues, execution claims, alert queue, and local inference lock state.
- Qdrant: curated episode vectors.
- Neo4j: provenance graph records.
- App data volume: agent workspaces, heartbeat files, raw transcript files, and local reports.

## Source Backup

Create a source tarball from the repository root:

```bash
tar --exclude='reference' --exclude='.brigade' --exclude='backups' \
  --exclude='web/node_modules' --exclude='web/dist' \
  --exclude='__pycache__' --exclude='.pytest_cache' --exclude='.ruff_cache' \
  -czf /tmp/openbrigade-source-$(date -u +%Y%m%dT%H%M%SZ).tar.gz .
```

This captures the active code and docs without the large `reference/` corpus or transient runtime state.

For the containerized working prototype, prefer the scripted backup flow:

```bash
./ops/backup-prototype.sh
```

## Runtime Backup

For the Docker stack, back up named volumes with datastore-native tools when possible:

- Postgres: `pg_dump` or a stopped-volume snapshot.
- Redis: copy the persisted `appendonly.aof` or `dump.rdb`.
- Qdrant: snapshot collections before copying volume data.
- Neo4j: use `neo4j-admin database dump` or a stopped-volume snapshot.

The prototype backup script captures both a logical PostgreSQL dump and raw snapshots of all
`brigade_` volumes.

## Restore

1. Restore the source tree.
2. Restore datastore volumes or import dumps before starting the stack.
3. Restore the app data volume if you need workspaces, heartbeat files, transcript files, or reports.
4. Run `./ops/brigade-live.sh health --json` and `./ops/brigade-live.sh status --json` to verify state.

For the containerized prototype, use:

```bash
./ops/restore-prototype.sh backups/<timestamp>
```
