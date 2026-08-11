# OpenBrigade v1.4.0 Release Gate

This is the shared handoff for the governed-research release. Update a row only
after its acceptance evidence has been recorded in
`reports/RESEARCH-CAPABILITY-TODO.md`.

## Release boundary

- Base checkpoint: `v1.3.0` / `release/1.3` (`2c8b287`).
- Release scope: governed **public general-web** research, bounded MCP tool use,
  browser isolation, source-backed citation-bearing answers, and operator health
  controls.
- Do not claim legal-research fitness, legal advice, authenticated browsing beyond
  approved profiles, MCP resources/prompts/streaming, or Executive automation.
- Keep the historical `release-1.1.patch` artifact out of commits and release
  notes.

## Milestone ledger

| Milestone | Required evidence | Commit / status |
| --- | --- | --- |
| MCP contract | Offline and opt-in tests; redacted status; supported-subset docs | pending release review |
| Browser worker | Isolation, hostile-page, saturation, and audit tests; Compose service docs | pending release review |
| Search quality | Deterministic general-web evaluation fixture and degraded-mode visibility | pending release review |
| Citations | API, Chief, Executive, assignment, CLI/TUI, and web rendering tests | pending release review |
| Operator controls | Authorized status/test UI and API; redacted health/audit behavior | pending release review |

## Final release checks

- [ ] `git diff --check`
- [ ] `python3 -m ruff check .`
- [ ] `python3 -m pytest`
- [ ] `cd web && npm run build`
- [ ] `docker compose --env-file .env.example --profile app config`
- [ ] App-profile rebuild and `./ops/brigade-live.sh health --json`
- [ ] Opt-in MCP/browser/search smokes are recorded separately with no secrets.
- [ ] Release notes and README match the shipped boundary.
- [ ] `release/1.4` and annotated `v1.4.0` resolve to the same validated commit
      locally and on `origin`.

## Publishing handoff

Publish only from a clean worktree after the checklist is complete. Verify with:

```bash
git ls-remote --refs origin refs/heads/release/1.4 refs/tags/v1.4.0 refs/tags/v1.4.0^{}
```
