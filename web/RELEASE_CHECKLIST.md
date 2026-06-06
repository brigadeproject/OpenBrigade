# Release Checklist

Use this checklist before publishing a prototype tag or sharing a public snapshot.

## Validation

- Run unit tests: `python3 -m pytest`
- Run lint: `python3 -m ruff check .`
- Compile Python entry points: `python3 -m compileall brigade tests ops/ollama_bridge_proxy.py`
- Validate Compose config: `docker compose --env-file .env.example config`
- Check migration status: `brigade db status` or `./ops/brigade-live.sh db status`
- Smoke v0.8 plain interfaces: `brigade chat tui --agent sage --plain` and `brigade settings tui --plain`
- Smoke the live prototype when available: `./ops/check-recovery.sh`

## Runtime Safety

- Confirm `.env` is local-only and `.env.example` contains no real secrets.
- Confirm runtime data is disposable until v0.7 migration hardening.
- Back up the prototype before publishing or large refactors: `./ops/backup-prototype.sh`
- Do not migrate live OpenClaw agents into OpenBrigade until after v0.7 hardening and break testing.

## Public Cleanup

- Exclude generated caches, local workspaces, transcripts, dumps, and volume snapshots.
- Review `reference/` usage for license and attribution before copying code.
- Check for secrets or host-specific paths in docs, examples, and committed config.
- Keep README and PROTOTYPE commands aligned with the actual CLI.

## v0.5 MVP Smoke

```bash
./ops/brigade-live.sh agent onboard --id scout --name SCOUT --role prototype
./ops/brigade-live.sh team create --id discovery --name Discovery
./ops/brigade-live.sh team assign --team discovery --agent scout --crew-chief
./ops/brigade-live.sh chat ask-agent --from-agent scout --to-agent scout --message "status?" --provider ollama --model gpt-oss:20b
./ops/brigade-live.sh orchestrator propose-stalled-goals
./ops/brigade-live.sh model route --task-type research --risk normal
./ops/brigade-live.sh alert audit
./ops/brigade-live.sh team status --team discovery
./ops/brigade-live.sh org graph --persist
./ops/brigade-live.sh db status
./ops/brigade-live.sh chat tui --agent scout --plain
./ops/brigade-live.sh settings tui --plain
```

Resolve any queued smoke cloud jobs after testing:

```bash
./ops/brigade-live.sh cloud resolve --job-id <job-id> --status complete --summary "smoke done"
```
