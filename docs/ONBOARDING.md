# OpenBrigade Onboarding

This runbook is for a fresh local/prototype OpenBrigade setup using the `brigade_`
Docker stack. Run stateful commands through `./ops/brigade-live.sh` so writes land in
the running container, Postgres, Redis, Qdrant, and Neo4j.

## 1. Prepare The Environment

```bash
cp .env.example .env
```

Edit `.env` before starting the stack. Replace placeholder secrets at minimum:

```bash
BRIGADE_POSTGRES_PASSWORD=...
BRIGADE_NEO4J_AUTH=neo4j/...
BRIGADE_JWT_SECRET=...
```

For local Ollama-backed embeddings, keep the default container-facing host URL unless
your machine needs a different route:

```bash
BRIGADE_OLLAMA_BASE_URL=http://host.docker.internal:11434
BRIGADE_OLLAMA_EMBEDDING_BASE_URL=http://host.docker.internal:11434
```

## 2. Start The Stack

For a normal first start:

```bash
docker compose --env-file .env --profile app up -d --build
./ops/brigade-live.sh db migrate
./ops/brigade-live.sh health --json
```

For a deliberately blank local userland, use the backup-first full wipe:

```bash
./ops/full-wipe.sh --confirm-full-wipe
```

Do not run `init mvp --force` when testing the first-user experience. That command
reseeds the default MVP roster and goals.

## 3. Create The First Owner Before Opening The GUI

The GUI permission gates depend on the authenticated user returned by `/api/auth/me`.
If there are no users yet and `BRIGADE_REQUIRE_AUTH=false`, the web app enters
bootstrap mode. Bootstrap mode can read status, but write controls stay disabled.

Create the first owner from the live container:

```bash
./ops/brigade-live.sh auth issue --username tm --role owner --ttl-seconds 86400
```

This command creates or updates the `tm` user and prints a JWT. Treat that token as a
secret.

With auth disabled and exactly one local user, the browser can use implicit single-user
access after refresh. If there are multiple users, or if the browser has a stale token,
paste the fresh JWT into the GUI token field and click Refresh.

Expected owner verification:

```bash
./ops/brigade-live.sh user list
curl http://127.0.0.1:58080/api/auth/me
```

The user should be `tm`, the role should be `owner`, and permissions should include
`admin`, `mission:write`, `task:write`, `chat:write`, `team:write`, and `goal:write`.

If the Cockpit or Ops Room shows warnings like these, the browser is not authenticated
as a user with write permissions:

```text
Missing admin; global model changes are disabled.
Missing mission:write; mission edits are disabled.
Missing task:write; task creation is disabled.
Missing chat:write; orchestrator chat is disabled.
Missing team:write; team edits are disabled.
Missing goal:write; goal edits are disabled.
```

Fix it by creating the owner user, clearing any stale JWT in the GUI token field, and
refreshing the page. If needed, issue a fresh owner token and paste it into the GUI.

## 4. Create A Minimal Team

> **GUI alternative:** As of 1.0.1 the Cockpit **Teams** widget can do everything in
> this section without the CLI. "Add agent" onboards an agent (seeding its workspace),
> optionally creating its team inline and marking it Crew Chief; "New team" sets up a
> team with a parent for hierarchy; the per-team controls manage the chief, members,
> parent, and escalation; and "Delegate work" covers section 8. Agent creation requires
> the `agent:write` permission (owner). The CLI steps below remain fully supported.

Set the mission:

```bash
./ops/brigade-live.sh mission set \
  --statement "Run a small autonomous product team" \
  --success "The team can plan, delegate, execute, and report work" \
  --not "Create new agents dynamically"
```

Create one Crew Chief and two agents on the same team:

```bash
./ops/brigade-live.sh agent onboard \
  --id chief \
  --name CHIEF \
  --role crew_chief \
  --team alpha \
  --create-team \
  --crew-chief \
  --provider ollama \
  --model gpt-oss:20b

./ops/brigade-live.sh agent onboard \
  --id scout \
  --name SCOUT \
  --role researcher \
  --team alpha \
  --provider ollama \
  --model gpt-oss:20b

./ops/brigade-live.sh agent onboard \
  --id builder \
  --name BUILDER \
  --role builder \
  --team alpha \
  --provider ollama \
  --model gpt-oss:20b
```

Verify the roster:

```bash
./ops/brigade-live.sh agent list
./ops/brigade-live.sh team show --id alpha
./ops/brigade-live.sh dashboard --plain --view teams
```

## 5. Open The GUI

Open:

```text
http://127.0.0.1:58080/
```

After the first owner exists, the top status strip should show owner-level auth rather
than bootstrap/read-only state. The Cockpit and Ops Room should allow mission edits,
task creation, chat, goal edits, team edits, and model changes according to the owner
permissions.

## 6. Create Executable Work

Agent Chat and Orchestrator Chat are conversational. They store messages and model
responses, but they do not create assignments or run delegation tools. If you ask
Chief for a breakout plan in chat, Chief can describe the plan without delegating it.

Use `Add Task` in the task panels, or `Create Task` from Agent Chat, when the
request should enter the runner. Assign the task to Chief when Chief should break
work apart and delegate through `delegate` or `create_subtasks`.

The packaged daemon runs on `BRIGADE_ORCHESTRATOR_CADENCE_SECONDS`, which defaults
to 900 seconds. For immediate testing, create the task and then run a short manual
cycle:

```bash
./ops/brigade-live.sh orchestrator daemon --max-cycles 2 --sleep-seconds 1
```

## 7. Observe Proactive Continuation

With the mission and Crew Chief created, but before creating executable work, run
one cycle:

```bash
./ops/brigade-live.sh orchestrator cycle
./ops/brigade-live.sh status --json
```

Default proactive mode is propose-only. The cycle should not create a task.
Instead, `orchestrator_reasoning` should contain exactly one
`proactive_proposal` event from `orchestrator_mission_continuation`. The event
shows the mission, supported goal when available, trigger condition
`mission_idle_no_active_or_queued_work`, target Crew Chief, and an idempotency key
starting with `orchestrator-proactive:v1:`.

In the GUI, Cockpit should show the event in Orchestration Activity, and Ops Room
should show the latest proposal in the Orchestrator room.

## 8. Smoke The Team

Delegate work through the Crew Chief:

```bash
./ops/brigade-live.sh team delegate \
  --team alpha \
  --chief chief \
  --agent scout \
  --assignment "Research the first useful demo scenario" \
  --goal-statement "Prepare the team for a working demo"

./ops/brigade-live.sh orchestrator cycle
./ops/brigade-live.sh agent run --id scout --provider ollama --model gpt-oss:20b
./ops/brigade-live.sh task list
```

The task list should be empty after the run completes. Replace `gpt-oss:20b` with
another installed Ollama model if that is your configured local runtime.
