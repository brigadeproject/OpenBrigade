# External Connector Runbook

External routes are disabled by default. Keep them disabled unless an operator is actively testing
or operating that connector with Postgres and Redis available for durable audit records, approvals,
rate limits, and queue state.

## Shared Safety Defaults

- Disable switches:
  - Telegram: `BRIGADE_TELEGRAM_WEBHOOK_ENABLED=false`
  - Google Chat: `BRIGADE_GOOGLE_CHAT_WEBHOOK_ENABLED=false`
  - OpenAI/Codex: leave API keys unset and remove OAuth credentials with `brigade model auth logout`
  - Gemini: leave API keys unset and remove OAuth credentials with `brigade model auth logout`
- Limits:
  - `BRIGADE_CONNECTOR_RATE_LIMIT_COUNT` and `BRIGADE_CONNECTOR_RATE_LIMIT_WINDOW_SECONDS`
  - `BRIGADE_CONNECTOR_MAX_BODY_BYTES`
  - `BRIGADE_CONNECTOR_MAX_INBOUND_CHARS`
  - `BRIGADE_CONNECTOR_MAX_OUTBOUND_CHARS`
- Audit checks:
  - `brigade alert audit --include-health`
  - `brigade connector approvals list`
  - `brigade datastore inspect --backend redis`
  - `brigade status --json` for chat messages, usage, episodes, and alerts

## Telegram

Setup:

1. Create a bot with BotFather.
2. Put `BRIGADE_TELEGRAM_BOT_TOKEN`, `BRIGADE_TELEGRAM_WEBHOOK_SECRET`, and
   `BRIGADE_TELEGRAM_DEFAULT_AGENT` in `.env`.
3. Set `BRIGADE_TELEGRAM_WEBHOOK_ENABLED=true`.
4. Register the HTTPS webhook with Telegram using the shared secret.

Bounded live smoke:

1. Use one allowlisted Telegram user.
2. Send one short message under the configured inbound size limit.
3. Verify a pending approval is created for unknown users, or a chat/episode/audit record is
   created for approved users.
4. Disable the route again if this is only a release validation.

Outbound behavior: replies are sent only through the configured Telegram bot token. If the token is
missing, the route rejects live outbound behavior and records the failure path instead of silently
sending.

Rollback: set `BRIGADE_TELEGRAM_WEBHOOK_ENABLED=false`, remove the public webhook at Telegram, and
remove the bot token from `.env`.

## Google Chat

Setup:

1. Configure the Google Chat webhook URL with `?token=<BRIGADE_GOOGLE_CHAT_SECRET>`.
2. Put `BRIGADE_GOOGLE_CHAT_SECRET` and `BRIGADE_GOOGLE_CHAT_DEFAULT_AGENT` in `.env`.
3. Set `BRIGADE_GOOGLE_CHAT_WEBHOOK_ENABLED=true`.

Bounded live smoke:

1. Use one allowlisted Chat sender.
2. Send one short event under the configured inbound size limit.
3. Verify pending approval or persisted chat/episode/audit records.
4. Disable the route after validation unless it is intended to stay live.

Outbound behavior: Google Chat is inbound-only for the RC. The webhook route parses and records
events, approvals, and audit state, but OpenBrigade does not POST replies back to the Google Chat
API yet. Operators should describe this connector as inbound-only until outbound API posting lands.

Rollback: set `BRIGADE_GOOGLE_CHAT_WEBHOOK_ENABLED=false`, remove the Chat app/webhook route, and
remove the shared secret from `.env`.

## OpenAI / Codex

Supported auth modes:

- API key through `OPENAI_API_KEY`.
- Local OAuth credential records under `BRIGADE_SECRET_STORE_PATH` via `brigade model auth login`.
  This is manual import/code exchange, not hosted browser/device-code login, and expired tokens
  require re-login.

Invalid credentials should return a blocked provider result or explicit auth error and must not
write secrets into messages, transcripts, or agent workspaces.

Bounded live smoke:

```bash
brigade model auth status
brigade model complete --provider openai --model <small-model> --prompt "Return one sentence."
```

Rollback: unset `OPENAI_API_KEY`, run `brigade model auth logout --provider openai`, and use
`--provider fake` or local Ollama for deterministic validation.

## Anthropic / Claude

Supported auth mode:

- API key through `ANTHROPIC_API_KEY`.

Claude OAuth is deferred for RC. Do not advertise Claude Code credential reuse or Claude OAuth as a
current feature.

## Gemini

Supported auth modes:

- API key through `GEMINI_API_KEY`.
- Local OAuth credential records under `BRIGADE_SECRET_STORE_PATH` via `brigade model auth login`.
  This is manual import/code exchange, not hosted browser/device-code login, and expired tokens
  require re-login.

Invalid credentials should return a blocked provider result or explicit auth error and must not
write secrets into messages, transcripts, or agent workspaces.

Bounded live smoke:

```bash
brigade model auth status
brigade model complete --provider gemini --model <small-model> --prompt "Return one sentence."
```

Rollback: unset `GEMINI_API_KEY`, run `brigade model auth logout --provider gemini`, and use
`--provider fake` or local Ollama for deterministic validation.
