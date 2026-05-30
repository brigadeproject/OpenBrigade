# RC Readiness

Validation window: 2026-05-30T02:37Z through 2026-05-30T03:03Z.

## Done

- Baseline tests, lint, compile, compose config, and migration status passed.
- Backup and restore were verified with `/opt/openbrigade/backups/20260530T024920Z`.
- Recovery, malformed-heartbeat, Redis, Postgres, Neo4j, Qdrant, dashboard, auth web, markdown chat, and role-denied gates passed.
- Release-path fixes landed for JWT sharing, authenticated browser smoke, stress validation, connector runbook docs, and generated-artifact exclusions.

## Deferred

- Real external live smokes for Telegram, Google Chat, OpenAI/Codex, and Gemini remain deferred until an operator supplies credentials intentionally.
- Public PR packaging cannot be verified with git commands in this checkout because `git status` fails with “not a git repository.”

## Known Risks

- Qdrant writes depend on a reachable embedding endpoint. This RC passed with
  `BRIGADE_OLLAMA_EMBEDDING_BASE_URL=http://host.docker.internal:11434`; the documented default
  `11435` requires a separate embedding runtime.
- The stack contains pre-existing active test/operator assignments that recovery preserved.
- The live Ollama stress path can produce multi-iteration assignment records; the stress gate now
  treats duplicate transcript paths, duplicate archive history, and lingering active assignments as
  failures rather than assuming one transcript per live-model assignment.

## Rollback Notes

- Disable external connectors with `BRIGADE_TELEGRAM_WEBHOOK_ENABLED=false` and
  `BRIGADE_GOOGLE_CHAT_WEBHOOK_ENABLED=false`.
- Remove model credentials with `brigade model auth logout` and unset provider API keys.
- Restore the validated snapshot with:

```bash
./ops/restore-prototype.sh /opt/openbrigade/backups/20260530T024920Z
```

## Decision

Do not publish from this exact directory until the git metadata issue is fixed. Runtime validation is otherwise green under the documented embedding override and disabled-by-default external connector posture.
