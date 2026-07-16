# OpenBrigade

OpenBrigade is a proactive agent harness built around an orchestrator, explicit missions,
per-agent goals, heartbeat assignments, durable task history, and curated memory/knowledge state.

## RC capability boundary

The RC ships the proactive orchestrator, fixed-roster delegation, Telegram inbound/outbound,
Google Chat inbound webhooks, web GUI, TUI, API-key model access for Claude/OpenAI/Gemini, manual
OAuth credential import for OpenAI/Codex and Gemini, Ollama/local routing, and per-agent model
configuration during managed runs.

The RC does not claim Claude OAuth, native Google Workspace tools, dynamic sub-agent spawning, or
MCP client/server support as shipped features. Google Workspace tools are expected to arrive through
the post-RC MCP client milestone described in `docs/MCP_CLIENT_POST_RC.md`. Dynamic sub-agent
spawning is scoped to v1.1; the RC delegates tracked tasks across an existing, fixed agent roster.

The PR-candidate path uses the `brigade_` Docker stack with Postgres, Redis, Qdrant, and Neo4j.
Operator workflows require the containerized stores. Live commands should run through
`./ops/brigade-live.sh ...` so state lands in Postgres and the runtime services.

## Quickstart

Install development dependencies:

```bash
python3 -m pip install --user -e ".[dev]"
```

Validate the project:

```bash
python3 -m ruff check .
python3 -m pytest
docker compose --env-file .env.example config
```

Run the live MVP flow:

```bash
./ops/brigade-live.sh init mvp --mission "Make enough money to offset operating cost"
./ops/brigade-live.sh task create --agent abacus --assignment "Estimate cost and propose one revenue experiment"
./ops/brigade-live.sh orchestrator cycle
./ops/brigade-live.sh agent run --id abacus
./ops/brigade-live.sh dashboard
```

`brigade init mvp` is intentionally one-shot. Re-run it only with `--force` when you mean to
re-seed default agents and goals into an existing prototype state.

When working inside this repository against the live stack, prefer `./ops/brigade-live.sh ...`.
Raw `brigade ...` commands are intended for an already configured container or environment with a
Postgres DSN.

Run all explicitly managed agents from the current heartbeat manifest:

```bash
brigade agent run-all
```

Issue and verify a local JWT for future API/web surfaces:

```bash
brigade auth issue --username owner
brigade auth issue --username tm --role operator
brigade auth verify --token-value <token>
```

Ingest a Markdown or text document into chunked knowledge plus provenance records:

```bash
brigade knowledge ingest \
  --title "Reference notes" \
  --source local \
  --type note \
  --path ./notes/reference.md
```

Curate and archive agent memory:

```bash
brigade memory append --agent sage --date 20260518 --note "Validated operator preference"
brigade memory curate --agent sage
brigade memory archive --agent sage
```

Other MVP commands:

```bash
brigade user add --username alice --role operator
brigade chat send --channel user:alice --sender alice --recipient sage --message "What are you working on?"
brigade task inspect --id <assignment-id>
brigade knowledge upload --path ./notes/reference.md
brigade model complete --provider ollama --model gpt-oss:20b --prompt "Summarize the mission"
brigade model complete --provider openai --model gpt-4.1-mini --prompt "Summarize"
brigade model complete --provider openai-codex --model gpt-5.3-codex-spark --prompt "Summarize"
brigade model complete --provider gemini --model gemini-1.5-flash --prompt "Summarize"
brigade model route --task-type research --risk normal
brigade orchestrator propose-stalled-goals
brigade alert audit
brigade db migrations
brigade db status
brigade db migrate
brigade db schema
brigade datastore inspect --backend qdrant
brigade datastore inspect --backend neo4j
./ops/brigade-live.sh chat tui --agent sage --plain
brigade settings tui --plain
```

When using OAuth credentials with the live Docker harness, import an access token through stdin:

```bash
printf '%s' "$OPENCLAW_ACCESS_TOKEN" | ./ops/brigade-live.sh model auth login \
  --provider openai-codex \
  --method oauth \
  --access-token-stdin
```

Prototype v0.5 adds onboarding, team structure, Crew Chief delegation, inter-agent chat, model
routing decisions, explicit cloud-dispatch records, and alert auditing:

```bash
brigade agent onboard --id scout --name SCOUT --role prototype --team discovery --create-team
brigade team chief --team discovery --agent scout
brigade team delegate --team discovery --chief scout --agent scout --assignment "Plan next test"
brigade chat ask-agent --from-agent scout --to-agent scout --message "What should we test?"
brigade chat group --participant scout --participant abacus --agenda "Pick next break test"
brigade cloud dispatch --agent scout --assignment "Run extended synthesis"
brigade cloud resolve --job-id <job-id> --status complete --summary "done"
```

Prototype v0.6 adds operational organization policy:

```bash
brigade team policy --team discovery --delegation-policy chief_only
brigade team route-work --team discovery --scope team --assignment "Coordinate a test"
brigade team escalate --from-team discovery --to-team build --chief scout --assignment "Build it" --reason "Needs build support"
brigade team status --team discovery
brigade org graph --persist
```

Prototype v0.7 adds explicit migration reporting and datastore hardening:

```bash
brigade db status
brigade db migrate
./ops/v07-wipe-reseed.sh --confirm-wipe
```

The v0.7 wipe/reseed script creates a backup, drops prototype runtime volumes, rebuilds the
`brigade_` stack, runs migrations, and reseeds MVP defaults. Use it only when you intend to discard
test runtime data.

For first-user testing without MVP defaults, use the blank-userland full wipe:

```bash
./ops/full-wipe.sh --confirm-full-wipe
```

This also creates a backup first, drops runtime volumes, rebuilds the app stack, runs migrations,
and verifies health, but it does not create a mission, users, agents, goals, or assignments.

Prototype v0.8 adds local live interfaces:

```bash
./ops/brigade-live.sh chat tui --agent sage
brigade settings tui
brigade web --host 0.0.0.0 --port 8080
```

The web gateway exposes authenticated `/api/*` routes and serves the React/Vite control UI from the
`brigade_web` container.

Headless browser smoke can validate the Cockpit and Ops Room views. When auth is required, issue an
owner/operator token and pass it as `BRIGADE_TOKEN`; the smoke script seeds the browser session
without printing the token:

```bash
./ops/web-browser-smoke.sh http://127.0.0.1:58080
BRIGADE_TOKEN=<owner-or-operator-token> ./ops/web-browser-smoke.sh http://127.0.0.1:58080
```

Prototype v0.9 adds PR-candidate hardening surfaces:

```bash
brigade config inspect
brigade config set --key log_level --value DEBUG --base-hash <config_hash>
brigade connector telegram --agent sage --allow-user 123 --payload-json '<telegram-update-json>'
brigade connector google-chat --agent sage --allow-user users/alice --payload-json '<chat-event-json>'
brigade alert audit --include-health
```

Migration status now reports applied, pending, failed, and unknown migrations. External connector
commands are smoke-test wrappers; production deployments should keep provider secrets in `.env`.

Prototype v0.9.1 adds live external connections. Routes are disabled until explicitly enabled,
and enabled live connectors require Postgres for durable audit/approval records plus Redis for
rate limiting:

```bash
brigade web --host 127.0.0.1 --port 8080
```

Operational setup, bounded-smoke, rollback, disable-switch, limit, and audit procedures live in
`docs/CONNECTORS_RUNBOOK.md`.

Live webhook routes:

- `POST /api/connectors/telegram/webhook`
- `POST /api/connectors/google-chat/webhook`

Telegram setup follows the BotFather webhook-secret pattern. Keep the bot token and webhook secret
in `.env`, then register the public HTTPS route with Telegram:

```bash
BRIGADE_TELEGRAM_WEBHOOK_ENABLED=true
BRIGADE_TELEGRAM_BOT_TOKEN=<botfather-token>
BRIGADE_TELEGRAM_WEBHOOK_SECRET=<random-shared-secret>
BRIGADE_TELEGRAM_DEFAULT_AGENT=sage

curl "https://api.telegram.org/bot${BRIGADE_TELEGRAM_BOT_TOKEN}/setWebhook" \
  -d "url=https://example.com/api/connectors/telegram/webhook" \
  -d "secret_token=${BRIGADE_TELEGRAM_WEBHOOK_SECRET}"
```

Google Chat webhooks use a shared query token. For RC, this connector is inbound-only: it records
incoming Chat events and routes them through the approval/audit path, but it does not POST outbound
replies to the Google Chat API.

```bash
BRIGADE_GOOGLE_CHAT_WEBHOOK_ENABLED=true
BRIGADE_GOOGLE_CHAT_SECRET=<random-shared-secret>
BRIGADE_GOOGLE_CHAT_DEFAULT_AGENT=sage
```

Configure the Google Chat incoming URL as:

```text
https://example.com/api/connectors/google-chat/webhook?token=<random-shared-secret>
```

Unknown external users create pending approval records and owner alerts. They do not trigger agent
runs until an owner approves them:

```bash
brigade connector approvals list
brigade connector approvals approve --provider telegram --external-user 123 --username alice
brigade connector approvals reject --provider google_chat --external-user users/alice --reason "not recognized"
```

### Chief chat (release 1.1)

Release 1.1 lets operators converse with the brigade in natural language through
**Crew Chiefs**. A chief behaves like a modern agent: it runs a multi-turn tool
loop over read-only query tools (`list_tasks`, `team_status`, `search_episodes`,
`usage_summary`, …) to answer from live and historical state, keeps long-term
memory (conversation continuity, episodic recall, curated notes), and stages
state-changing actions (`create_assignment`, `cancel_assignment`, `set_priority`,
`attach_guidance`, `retry_blocked_assignment`) for the operator to confirm in
chat. Each conversation talks to one persona: a team's Crew Chief (scoped to that
chief's agents) or the fleet-wide **front desk** (the orchestrator's view).

Threads are durable and identity-keyed, so the mobile SPA and an approved
Telegram user with the same username share one conversation. Web/mobile use the
thread routes:

- `GET/POST /api/chat/threads` — list personas/threads, get-or-create by persona
- `GET/POST /api/chat/threads/{id}/messages` — read history, send a turn

Routing external connectors through chief chat is opt-in and off by default while
it soaks (`BRIGADE_CONNECTOR_CHIEF_CHAT_ENABLED=true`). Once on, an approved
connector user switches persona with control commands: `/frontdesk`,
`/chief <team-or-agent>`, `/who`, and `/new`. Telegram runs the turn out of band
(the webhook returns immediately and the reply is posted when the turn
finishes); Google Chat runs synchronously with a tighter iteration cap. See the
`BRIGADE_CHIEF_CHAT_*` settings in `.env.example`, including the
`BRIGADE_OLLAMA_NUM_CTX` sizing note — loop prompts grow ~1-2KB per iteration.

OpenAI, OpenAI/Codex, Anthropic/Claude, and Gemini routes continue to use LiteLLM. API keys remain
supported through `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, and `GEMINI_API_KEY`. Claude is API-key
only for RC. OpenAI/Codex and Gemini OAuth credentials can be imported or manually exchanged and
stored locally under `.brigade/secrets/model-auth/` or `BRIGADE_SECRET_STORE_PATH`; status output
redacts tokens. The RC flow is not a hosted browser/device-code login and does not refresh expired
tokens automatically:

```bash
brigade model auth login --provider openai --method oauth --access-token <token> --refresh-token <token>
brigade model auth login --provider openai-codex --method oauth --access-token <token>
brigade model auth login --provider gemini --method oauth --token-json ./token.json
brigade model auth login --provider gemini --method oauth --auth-code <code> --client-id <id> --client-secret <secret> --redirect-uri <uri>
brigade model auth status
brigade model auth logout --provider gemini
```

Reference setup material: [OpenAI API authentication](https://platform.openai.com/docs/api-reference/authentication?api-mode=responses),
[Gemini OAuth quickstart](https://ai.google.dev/gemini-api/docs/oauth), and
[LiteLLM provider routing](https://docs.litellm.ai/).

The Live Ops Room remains available through the local web gateway:

```bash
brigade web --host 127.0.0.1 --port 8080
```

The Live Ops Room renders a room-floor view backed by OpenBrigade state. It streams
agent/task snapshots over `/api/ops-room/events`, uses REST for actions, routes agents into rooms
from their active assignments, and exposes common mission, goal, task, and user-to-agent chat
workflows.

The orchestrator writes active assignments to `HEARTBEAT.md` in explicit agent workspaces under
`.brigade/`. The runner reads the last parseable assignment block, preserves surrounding notes,
updates continuation state across cycles, writes transcripts, and emits usage plus financial data.
Each orchestrator tick first builds a cheap floor snapshot of mission, goal freshness, and Crew
Chief load. Expensive model reasoning is only invoked when stale-work or load-imbalance predicates
fire; `BRIGADE_STALE_WORK_SECONDS` controls the default freshness window.

Qdrant episode writes use Ollama embeddings when configured. The local development stack expects
the main Ollama runtime at `BRIGADE_OLLAMA_BASE_URL` and a separate embedding runtime at
`BRIGADE_OLLAMA_EMBEDDING_BASE_URL`; the default embedding setup is
`http://host.docker.internal:11434` with `nomic-embed-text:latest` and 768-dimensional vectors.
Use a model-specific collection such as `brigade_episodes_nomic_embed_text` when changing vector
size or embedding model.

## Docker Stack

The root Compose file defines isolated datastore services for later integration:

- `brigade_postgres`
- `brigade_redis`
- `brigade_qdrant`
- `brigade_neo4j`
- `brigade_web` when the `app` profile is enabled

Copy `.env.example` to `.env`, replace placeholder secrets, then run:

```bash
docker compose --env-file .env --profile app up -d --build
```

To include the packaged daemon container:

```bash
docker compose --env-file .env --profile app up -d
```

## Current Boundaries

Live Ollama calls are available through `brigade model complete --provider ollama` when Ollama is
running. LiteLLM-backed cloud calls are available through `litellm`, `openai`, or `gemini` providers
once the `models` extra is installed and API keys are set.

Backup guidance for source, runtime state, and the `brigade_` volume set lives in [BACKUP.md](BACKUP.md).
Prototype-specific live-operation notes live in [docs/PROTOTYPE.md](docs/PROTOTYPE.md).
Operator architecture notes live in [docs/OPERATING_ARCHITECTURE.md](docs/OPERATING_ARCHITECTURE.md).
Network topology notes live in [docs/NETWORK_TOPOLOGY.md](docs/NETWORK_TOPOLOGY.md).
Memory architecture notes live in [docs/MEMORY_ARCHITECTURE.md](docs/MEMORY_ARCHITECTURE.md).
Library ingestion notes live in [docs/LIBRARY_SYSTEMS.md](docs/LIBRARY_SYSTEMS.md).
Prompt architecture notes live in [docs/PROMPT_ARCHITECTURE.md](docs/PROMPT_ARCHITECTURE.md).
Release and public-cleanup checks live in [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md).
