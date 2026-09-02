# External Connector Runbook

## Browser worker recovery

Public browser research is served by the isolated `brigade_browser` worker. Diagnose it with
`./ops/brigade-live.sh health --json` and `docker compose --env-file .env --profile app logs brigade_browser`.
A saturated worker returns a bounded 429 observation; restart only the browser service with
`docker compose --env-file .env --profile app up -d --build brigade_browser`. Public sessions do not
survive restart. For a named authenticated profile, first revoke it through the worker's
`clear-profile` operation, then rotate or revoke the external profile provider; never copy cookies or
browser profile files into OpenBrigade configuration, logs, or tickets.

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

Choose exactly one inbound mode. Polling is recommended for a local OpenBrigade
stack because it does not require public HTTPS ingress. Both modes preserve the
same identity approval, audit, rate-limit, Executive-routing, and outbound
reply behavior.

### Long polling (recommended)

1. Put `BRIGADE_TELEGRAM_BOT_TOKEN` and `BRIGADE_TELEGRAM_DEFAULT_AGENT` in
   `.env`.
2. Set `BRIGADE_TELEGRAM_POLLING_ENABLED=true` and
   `BRIGADE_TELEGRAM_WEBHOOK_ENABLED=false`.
3. To route approved identities to their owner Executive, set
   `BRIGADE_CONNECTOR_EXECUTIVE_CHAT_ENABLED=true`.
4. Rebuild the app services:

   ```bash
   docker compose --env-file .env --profile app up -d --build brigade_orchestrator brigade_web
   ```

The orchestrator polls Telegram's `getUpdates` endpoint independently of its
normal cycle cadence. It stores the highest processed update ID in Redis and
calls `deleteWebhook` on startup, retaining pending Telegram updates. Only one
process may poll a bot token; do not leave an OpenClaw or other Telegram
polling process running with the same token.

### Optional named Crew Chief bots

Named bots are an opt-in polling surface in addition to the default Telegram
bot. Each account is permanently bound to one active Crew Chief and routes to
the same owner/persona conversation shown in the Chat tab. A named bot cannot
switch to the Front Desk, Executive, or another Chief. Its token is stored as a
mode-0600 file under the configured secret store; the durable database record
contains only a fingerprint and status metadata.

This surface requires Postgres and Redis in live operation. The JSON store is
supported only for offline tests. Configure it from **Proposals -> Connector
Approvals -> Named Crew Chief Bots**, or with the CLI:

```bash
printf '%s' "$TELEGRAM_CHIEF_BOT_TOKEN" | brigade connector telegram-account add \
  --id infra-chief --chief infrastructure-chief --label "Infrastructure Chief" \
  --token-stdin
brigade connector telegram-account test --id infra-chief
brigade connector telegram-account enable --id infra-chief
# After the owner's first message creates a pending identity:
brigade connector approvals approve --provider telegram:infra-chief \
  --external-user 123456789 --username owner
```

Creating an account leaves it disabled. Test it, then enable it. The
orchestrator notices account changes without a restart. Disable an account
before removing it; pass `--delete-secret` only when the token file should also
be removed. During a migration from an older poller, leave that service stopped
but intact until the rollback window closes. Never run two pollers for the same
token.

Normal Chief conversation remains the default. Owner-only direct execution is
enabled separately per Chief with explicit tool groups:

```bash
brigade connector chief-policy set --chief infrastructure-chief --direct on \
  --tool-group workspace_read --tool-group shell --tool-group maintenance
```

Direct turns use a 60-call and 30-minute default budget. The Chief may request
a one-shot exact action or removal of the count ceiling; only the owner can
confirm it, and the two-hour active-time hard stop remains. `/status` and
`/cancel` work from the named bot. Long turns keep Telegram's typing indicator
active and send meaningful milestones to both Telegram and the canonical Chat
thread.

On startup, each polling bot publishes its applicable Telegram command menu.
Shared chat commands are `/help`, `/who`, `/model`, `/new`, `/clear`, and
`/status`. Named Chief bots also expose `/cancel` and `/confirm`; the default
Chief-routing bot exposes `/chief` and `/frontdesk`. `/model` reports the
thread's effective route and numbered configured options. Use `/model <number>`
or `/model <provider>/<model>` to persist a choice on the canonical thread, and
`/model default` to return to the persona's assigned route. The same selection
is reflected in the Chat tab and used by queued direct-Chief work.

For an owner-bound Chief chat, an explicit request to create or assign a task
creates the durable assignment immediately and returns a plain-language receipt
with the target, priority, and task ID. A second `confirm` is still required for
other state changes, including cancellation, reprioritization, recurrence
changes, and guidance mutations. This behavior is shared by Telegram and the
canonical Chat thread; model-emitted task JSON is protocol data and is never the
outbound Telegram response.

Maintenance actions are never free-form privileged shell commands. Copy
`docs/maintenance-actions.example.json` to an operator-controlled path, edit it
as root, set `root:<service-group>` ownership and mode 0640, and point
`BRIGADE_MAINTENANCE_ACTIONS_PATH` at it. Each
entry supplies exact argv, an allowed-Chief list, and a timeout. OpenBrigade
does not edit or bypass sudoers; an argv containing `sudo` can only succeed if
the host already authorizes that exact command non-interactively.

### Webhook (public HTTPS alternative)

Setup:

1. Create a bot with BotFather.
2. Put `BRIGADE_TELEGRAM_BOT_TOKEN`, `BRIGADE_TELEGRAM_WEBHOOK_SECRET`, and
   `BRIGADE_TELEGRAM_DEFAULT_AGENT` in `.env`.
3. Set `BRIGADE_TELEGRAM_WEBHOOK_ENABLED=true`.
4. Keep `BRIGADE_BIND_ADDRESS=127.0.0.1`. Configure an operator-managed,
   stable public HTTPS reverse proxy or tunnel that forwards only
   `POST /api/connectors/telegram/webhook` to
   `http://127.0.0.1:${BRIGADE_WEB_PORT:-58080}/api/connectors/telegram/webhook`.
   It must preserve `X-Telegram-Bot-Api-Secret-Token`; do not log request
   bodies or that header.
5. Rebuild the app services so Compose forwards the connector settings into
   `brigade_web`:

   ```bash
   docker compose --env-file .env --profile app up -d --build brigade_web brigade_orchestrator
   ```

6. Register the public HTTPS route with Telegram using the shared secret:

   ```bash
   curl --fail-with-body "https://api.telegram.org/bot${BRIGADE_TELEGRAM_BOT_TOKEN}/setWebhook" \
     --data-urlencode "url=https://<public-host>/api/connectors/telegram/webhook" \
     --data-urlencode "secret_token=${BRIGADE_TELEGRAM_WEBHOOK_SECRET}" \
     --data-urlencode 'allowed_updates=["message"]'
   curl --fail-with-body "https://api.telegram.org/bot${BRIGADE_TELEGRAM_BOT_TOKEN}/getWebhookInfo"
   ```

The app profile intentionally publishes the web service on loopback only.
Telegram cannot connect until the public HTTPS ingress in step 4 is active.

Bounded live smoke:

1. Use one allowlisted Telegram user.
2. Send one short message under the configured inbound size limit.
3. Verify a pending approval is created for unknown users, or a chat/episode/audit record is
   created for approved users.
4. Disable the route again if this is only a release validation.

Outbound behavior: replies are sent only through the configured Telegram bot token. If the token is
missing, the route rejects live outbound behavior and records the failure path instead of silently
sending.

Rollback: set `BRIGADE_TELEGRAM_POLLING_ENABLED=false` or
`BRIGADE_TELEGRAM_WEBHOOK_ENABLED=false`, as applicable. For webhook mode,
remove the public webhook at Telegram; then remove the bot token from `.env`
when the connector is no longer needed.

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
brigade model complete --provider openai-codex --model gpt-5.4 --prompt "Return one sentence."
```

To make OpenAI/Codex the runtime default for orchestrator and agent loops, set:

```bash
BRIGADE_DEFAULT_PROVIDER=openai-codex
BRIGADE_DEFAULT_MODEL=gpt-5.4
BRIGADE_OPENAI_CODEX_AUTH_MODE=oauth
```

For API-key fallback, use the same provider/model default with:

```bash
OPENAI_API_KEY=<openai-api-key>
BRIGADE_OPENAI_CODEX_AUTH_MODE=api_key
```

For containerized local use, import a provider-issued access token through stdin so the token is not
stored in shell history or visible as a process argument:

```bash
printf '%s' "$OPENCLAW_ACCESS_TOKEN" | ./ops/brigade-live.sh model auth login \
  --provider openai-codex \
  --method oauth \
  --access-token-stdin
```

Existing agents keep their persisted `model_provider` and `model_name` until changed. Move one
agent to Codex without recreating it:

```bash
./ops/brigade-live.sh agent model \
  --id <agent-id> \
  --provider openai-codex \
  --model gpt-5.4
```

Rollback: unset `OPENAI_API_KEY`, run `brigade model auth logout --provider openai` and
`brigade model auth logout --provider openai-codex`, set `BRIGADE_DEFAULT_PROVIDER=ollama`, and
move any persisted agents back to an installed Ollama model.

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
local Ollama for bounded validation.
