# Prototype Operations

OpenBrigade is now a working prototype. Treat it as a separate lab environment from the live
OpenClaw deployment.

## Current Rule

- Do not migrate live agents into this stack yet.
- Use only prototype-local testing agents here.
- Preserve `.env`, the `brigade_` Docker volumes, and the live app data in `brigade_app_data`.
- Treat runtime data as disposable until the PR-candidate break-test pass is complete.
- Durable work at this stage is source code, docs, Compose config, ops scripts, and explicit backups.
- Before any future wipe/reseed/migration, export only the prototype records worth preserving.

## Data Policy

The prototype is allowed to accumulate test tasks, transcripts, memory, alerts, and run history. That
data is useful for testing but is not canonical production state.

For PR-candidate testing, it is acceptable to wipe and reseed the prototype datastores instead of
preserving every test record. If a specific prototype artifact becomes valuable, export it
deliberately before the wipe.

The v0.7 migration pass uses explicit SQL migration tracking, and runtime data may be wiped/reseeded
after a backup:

```bash
./ops/v07-wipe-reseed.sh --confirm-wipe
```

For brand-new-user testing without seeded MVP defaults, run:

```bash
./ops/full-wipe.sh --confirm-full-wipe
```

The full wipe backs up the current prototype, drops runtime volumes, rebuilds the stack, runs
migrations, verifies health, and asserts that mission, users, agents, teams, goals, assignments,
messages, transcripts, alerts, memory, and provenance records are empty.

Current migration hardening includes:

- `brigade db status` and `brigade db migrate`
- applied, pending, failed, and unknown migration reporting
- Qdrant episode upserts for episodic/vector memory records
- Neo4j provenance upserts for document, task, decision, and organization records
- `brigade_schema_migrations` tracking in Postgres
- `brigade datastore inspect --backend qdrant|neo4j`

Remaining datastore work is hardening and deeper queue/provenance behavior, not the initial wiring.

## v0.4 Baseline

The working prototype is considered complete through v0.4 when these checks pass:

```bash
python3 -m pytest
python3 -m ruff check .
python3 -m compileall brigade tests ops/ollama_bridge_proxy.py
KEEP_WORK_DIR=1 PROVIDER=ollama MODEL='qwen2.5-coder:7b' ./ops/stress-concurrency.sh
./ops/test-bad-heartbeats.sh
./ops/check-recovery.sh
```

## Live Prototype Commands

Use the live wrapper so commands target the running container state instead of the host
`.brigade/` directory:

```bash
./ops/brigade-live.sh status --json
./ops/brigade-live.sh dashboard
./ops/brigade-live.sh agent list
./ops/brigade-live.sh auth issue --username tm --role operator
./ops/brigade-live.sh db status
./ops/brigade-live.sh chat tui --agent sage --plain
./ops/brigade-live.sh settings tui --plain
```

Raw `brigade ...` commands from the repository checkout now trip a host-state guard for
stateful operations. Use `./ops/brigade-live.sh ...` for the running prototype. Only use
`brigade --allow-host-state ...` when you intentionally want host-local `.brigade/` state.

## v0.5 Onboarding and Teams

Create a prototype agent with required workspace files and team membership:

```bash
./ops/brigade-live.sh agent onboard \
  --id scout \
  --name SCOUT \
  --role prototype_research \
  --team discovery \
  --create-team \
  --crew-chief \
  --provider ollama \
  --model qwen2.5-coder:7b
```

Validate the workspace manifest:

```bash
./ops/brigade-live.sh agent validate --id scout
```

Inspect teams:

```bash
./ops/brigade-live.sh team list
./ops/brigade-live.sh team show --id discovery
./ops/brigade-live.sh dashboard --plain --view teams
```

## v0.8 Local Gateway

Run the local authenticated web gateway through Compose:

```bash
docker compose --env-file .env --profile app up -d --build brigade_web
```

The gateway listens on `${BRIGADE_BIND_ADDRESS:-127.0.0.1}:${BRIGADE_WEB_PORT:-58080}` and exposes
chat, hierarchy, settings, dashboard, auth, and health routes under `/api/*`.

Run a synchronous 1:1 inter-agent chat:

```bash
./ops/brigade-live.sh chat ask-agent \
  --from-agent test-scout \
  --to-agent test-builder \
  --message "What should we test next?" \
  --provider ollama \
  --model gpt-oss:20b
```

For local inference, use an installed Ollama model:

```bash
./ops/brigade-live.sh chat ask-agent \
  --from-agent test-scout \
  --to-agent v05-scout \
  --message "Give one concise recommendation for the next v0.5 test." \
  --provider ollama \
  --model qwen2.5-coder:7b
```

Run a bounded pass-the-mic group chat:

```bash
./ops/brigade-live.sh chat group \
  --participant test-scout \
  --participant test-builder \
  --participant v05-scout \
  --agenda "Pick the next v0.5 break test." \
  --max-turns 3 \
  --provider ollama \
  --model gpt-oss:20b
```

Delegate work as a Crew Chief:

```bash
./ops/brigade-live.sh team delegate \
  --team discovery \
  --chief v05-scout \
  --agent test-builder \
  --assignment "Write the next break-test note" \
  --goal-statement "Harden v0.5"
```

Ask the orchestrator to create work for goals with no active assignment:

```bash
./ops/brigade-live.sh orchestrator propose-stalled-goals
```

Check model routing against cost and cloud in-flight state:

```bash
./ops/brigade-live.sh model route \
  --task-type research \
  --risk normal \
  --local-model qwen2.5-coder:7b
```

Queue extended cloud-dispatch work without running it immediately:

```bash
./ops/brigade-live.sh cloud dispatch \
  --agent test-builder \
  --assignment "Run extended synthesis on the v0.5 test findings" \
  --model gpt-4.1-mini \
  --max-cost-usd 2.00
./ops/brigade-live.sh cloud list
./ops/brigade-live.sh cloud resolve --job-id <job-id> --status complete --summary "done"
```

Audit alerts for drift, repeated failures, failed cloud jobs, and optional datastore health:

```bash
./ops/brigade-live.sh alert audit
./ops/brigade-live.sh alert audit --include-health
```

## v0.9 PR-Candidate Surfaces

Use config hashes when changing settings through automation or UI flows:

```bash
./ops/brigade-live.sh config inspect
./ops/brigade-live.sh config set --key log_level --value DEBUG --base-hash <config_hash>
```

Smoke-test connector payload handling without live webhooks:

```bash
./ops/brigade-live.sh connector telegram \
  --agent sage \
  --allow-user 123 \
  --payload-json '{"message":{"chat":{"id":1},"from":{"id":123},"text":"status?"}}'

./ops/brigade-live.sh connector google-chat \
  --agent sage \
  --allow-user users/alice \
  --payload-json '{"user":{"name":"users/alice"},"message":{"space":{"name":"spaces/test"},"text":"status?"}}'
```

Secrets belong in `.env` or an operator-managed secret store, not in agent workspaces. Web routes now
emit baseline security headers, and `alert audit` reports default or weak JWT secrets.

## v0.9.1 Live Ops Room

Run the RC web surface through the same gateway:

```bash
docker compose --env-file .env --profile app up -d --build brigade_web
```

The browser app opens directly into the Live Ops Room. It uses OpenBrigade's `/api/ops-room` snapshot
and `/api/ops-room/events` stream for live agent state, and keeps writes on existing REST routes.
Supported v0.9.1 actions are:

- set mission
- add goals
- create assignments
- ask an agent
- route task cards into live rooms from assignment state

The room intentionally does not include the full Pixel Agents furniture editor, VS Code integration,
WebSocket protocol, or goal edit/delete. Pixel Agents assets are bundled in
`web/public/assets/pixel-agents/` with the upstream MIT notice and character attribution.

## v0.6 Organization and Delegation

Set team policy and a default escalation path:

```bash
./ops/brigade-live.sh team policy \
  --team discovery \
  --delegation-policy chief_only \
  --escalation-team build
```

Route work through team-aware policy:

```bash
./ops/brigade-live.sh team route-work \
  --team discovery \
  --scope team \
  --urgency high \
  --assignment "Coordinate the next break-test pass"
```

Escalate work across teams:

```bash
./ops/brigade-live.sh team escalate \
  --from-team discovery \
  --to-team build \
  --chief v05-scout \
  --assignment "Prototype the selected break-test fixture" \
  --reason "Needs implementation support"
```

Inspect team status and the organization graph:

```bash
./ops/brigade-live.sh team status --team discovery
./ops/brigade-live.sh org graph --persist
./ops/brigade-live.sh dashboard --plain --view teams
```

## Save the Prototype

Create a full prototype backup:

```bash
./ops/backup-prototype.sh
```

This captures:

- source tree snapshot, excluding `reference/`
- `.env` snapshot
- resolved Compose config
- live app data tarball
- PostgreSQL dump
- raw snapshots of all `brigade_` volumes

## Recreate Containers

Rebuild containers without dropping data:

```bash
./ops/recreate-stack.sh
```

Rebuild from scratch and drop volumes only when you intend a fresh environment:

```bash
./ops/recreate-stack.sh --drop-volumes
```

Restore a saved prototype backup:

```bash
./ops/restore-prototype.sh backups/<timestamp>
```

## Suggested Test Agents

Keep test-only agents clearly named and isolated, for example:

- `test-scout` in `workspace-test-scout/`
- `test-builder` in `workspace-test-builder/`

Create them against the live prototype with:

```bash
./ops/brigade-live.sh agent add --id test-scout --name TEST-SCOUT --workspace workspace-test-scout --role prototype_research
./ops/brigade-live.sh agent add --id test-builder --name TEST-BUILDER --workspace workspace-test-builder --role prototype_builder
```

## Shared Break-Test Session

For a shared `tmux` session during manual break testing, use:

```bash
./ops/tmux-shared-session.sh create
tmux attach -t brigade-test
```

This creates a four-pane layout:

- interactive command pane
- live `docker compose logs` pane
- auto-refresh watch pane
- one-shot preflight snapshot pane

The watch pane uses [ops/prototype-watch.sh](/opt/openbrigade/ops/prototype-watch.sh). The
preflight pane uses [ops/prototype-preflight.sh](/opt/openbrigade/ops/prototype-preflight.sh).

Useful settings:

```bash
BRIGADE_TEST_SESSION=brigade-break
LOG_SERVICES="brigade_orchestrator brigade_postgres brigade_redis"
POLL_SECONDS=5
./ops/tmux-shared-session.sh create "$BRIGADE_TEST_SESSION"
```

Other session commands:

```bash
./ops/tmux-shared-session.sh attach brigade-test
./ops/tmux-shared-session.sh print brigade-test
./ops/tmux-shared-session.sh kill brigade-test
```

## Morning Break-Test Baseline

Before manual testing, capture a baseline in the live prototype:

```bash
./ops/prototype-preflight.sh
./ops/backup-prototype.sh
```

That gives you a health snapshot plus a restorable checkpoint before trying to break behavior.
