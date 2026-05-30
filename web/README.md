# OpenBrigade

OpenBrigade is a proactive agent harness built around an orchestrator, explicit missions,
per-agent goals, heartbeat assignments, durable task history, and curated memory/knowledge state.

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

Run the deterministic MVP flow:

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
brigade model complete --provider fake --prompt "Summarize the mission"
brigade model complete --provider openai --model gpt-4.1-mini --prompt "Summarize"
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
brigade chat tui --agent sage --plain
brigade settings tui --plain
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

Prototype v0.8 adds local live interfaces:

```bash
brigade chat tui --agent sage
brigade settings tui
brigade web --host 0.0.0.0 --port 8080
```

The web gateway exposes authenticated `/api/*` routes and serves the React/Vite control UI from the
`brigade_web` container.

Prototype v0.9 adds PR-candidate hardening surfaces:

```bash
brigade config inspect
brigade config set --key log_level --value DEBUG --base-hash <config_hash>
brigade connector telegram --agent sage --allow-user 123 --payload-json '<telegram-update-json>'
brigade connector google-chat --agent sage --allow-user users/alice --payload-json '<chat-event-json>'
brigade alert audit --include-health
```

Migration status now reports applied, pending, failed, and unknown migrations. External connector
commands are smoke-test wrappers; production deployments should keep provider secrets in `.env` and
use allowlists before enabling live webhooks.

Prototype v0.9.1 adds the Live Ops Room as the final RC-facing web surface before v1.0:

```bash
brigade web --host 127.0.0.1 --port 8080
```

The Live Ops Room renders a Pixel Agents-inspired room backed by OpenBrigade state. It streams
agent/task snapshots over `/api/ops-room/events`, uses REST for actions, supports per-user seat
placement, and exposes common mission, goal, task, and user-to-agent chat workflows. Pixel Agents
assets are included under `web/public/assets/pixel-agents/` with MIT attribution.

The orchestrator writes active assignments to `HEARTBEAT.md` in explicit agent workspaces under
`.brigade/`. The runner reads the last parseable assignment block, preserves surrounding notes,
updates continuation state across cycles, writes transcripts, and emits usage plus financial data.

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
Prototype-specific live-operation notes live in [PROTOTYPE.md](PROTOTYPE.md).
Operator architecture notes live in [OPERATING_ARCHITECTURE.md](OPERATING_ARCHITECTURE.md).
Network topology notes live in [NETWORK_TOPOLOGY.md](NETWORK_TOPOLOGY.md).
Memory architecture notes live in [MEMORY_ARCHITECTURE.md](MEMORY_ARCHITECTURE.md).
Library ingestion notes live in [LIBRARY_SYSTEMS.md](LIBRARY_SYSTEMS.md).
Prompt architecture notes live in [PROMPT_ARCHITECTURE.md](PROMPT_ARCHITECTURE.md).
Release and public-cleanup checks live in [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md).
