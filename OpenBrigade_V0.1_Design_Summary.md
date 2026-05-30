# OpenBrigade — V0.1 Design Summary

A consolidated reference of decisions made during the V2 requirements pass. This is the source of truth for MVP scope; the original `BrigadeV2.md` stream-of-consciousness is the inspiration, this is the spec.

---

## 1. Project Identity

- **Name:** OpenBrigade
- **CLI binary:** `brigade`
- **Python package:** `brigade`
- **License:** GNU GPLv3
- **Repo strategy:** Private during build, flip to public at MVP
- **Repo layout:** Monorepo (orchestrator, agent runner, TUI, web, ingestion, shared libs as packages)

---

## 2. Stack & Tooling

- **Language:** Python
- **Datastores (all in own containers via Docker Compose):**
  - PostgreSQL — completed assignment history, audit, dispatch transcripts, full-text chat storage, user accounts
  - Redis — runtime state, active assignments, pending queues, local inference lock
  - Qdrant — vector store for memory summaries and ingested knowledge
  - Neo4j — knowledge graph (schema designed in a later pass)
- **Migrations:** TBD
- **LLM clients:** LiteLLM for cloud providers (Anthropic, OpenAI, Gemini), native Ollama client for local inference
- **Config:** JSON throughout (`./brigade.config.json` for MVP, env vars prefixed `BRIGADE_*`)
- **Secrets:** `.env` + python-dotenv (real secret manager deferred)
- **Logging:** Structured JSON to stdout
- **CI:** GitHub Actions on PR from day one
- **Auth:** JWT (sessions and API)
- **RBAC roles:** owner / operator / observer
- **Time:** UTC in datastores, local time in UI/TUI/logs
- **Deployment target:** Docker Compose (single host)

---

## 3. The Brigade Model

Inspired by Escoffier's kitchen brigade.

| Role | Who | Authority |
|---|---|---|
| Owner / Executive Chef | Human user | Sets mission, ratifies governance, owns the system |
| Sous Chef / Crew Chief | Agent (user-designated role) | Coordinates a sub-team, can direct via sync chat or queue tasks |
| Line Worker | Agent | Owns a domain, executes assigned work |
| Maître D' | Orchestrator (service) | Tracks everything, does no work itself |

**Crew Chief is a role, not a separate entity.** A chief is just an agent with extra authority: directs other agents during their cycle (via sync inter-agent chat) or adds tasks to the orchestrator's queue. May read team agents' MEMORY.md directly (rare, but allowed).
Optional — not all brigades have one; some may have more than one.  An agent can only work for one Crew Chief.

---

## 4. The Orchestrator

A service (not an agent) that runs LLM-backed reasoning to assign work.

### Properties
- LLM-backed using a full-reasoning model
- Cron-driven for MVP (configurable cadence later)
- **Single point of failure for MVP** — no leader election, no HA. Accepted risk.
- The conductor: orchestrator wakes agents; agents do not run their own cron loops.

### Per-cycle context
1. Mission (global, brigade-wide)
2. Open task list
3. Last cycle's reasoning
4. Per-agent state snapshot (current task, last status)
5. Goals (per-agent)
- **Preference given to user requests when prioritizing**

### Run sequence (per cycle)
1. Load mission
2. Pull agent state snapshots / task updates
3. Resolve blocks/failures
4. Check task queue — user inputs evaluated first
5. Load own previous reasoning
6. Make assignment decisions
7. Wake agents (skip any agent currently mid-task; move to next free agent)
8. If long-form work queued → assign through agent using elevated model options
9. Log own reasoning for next run
10. Sleep until next cron tick

### Authority
- **Can create new tasks** when it detects goals not progressing — this is its biggest function.
- **Can split, parallelize, sequence, and re-split tasks**. Task graph is a DAG and dynamic.
- When orchestrator splits a task → 2 (or more) new tasks; original is **superseded** (closed pointing at successors for audit).
- When an agent splits its own task → original becomes a **container** (no work, tracks subtasks). Agent may keep one of the subtasks for itself.
- Re-evaluates conditional logic each cycle (no conditional execution baked into heartbeat files). E.g., "If A is done then B else C-or-sleep" is decided fresh each cycle.

### Conflict resolution
- Agent's reported status is ground truth for **task state**.
- Orchestrator's reasoning is ground truth for **assignment decisions**.

### Failure handling
- Task fails → orchestrator re-assigns or asks agent why on next cycle, then retries.
- **5 consecutive failures on the same task → alert user.** Treated as a block requiring human intervention.
- Parent task with mixed subtask outcomes → blocked, status reflects the failed subtask.
- Cross-agent dependencies → orchestrator surfaces task A's completion as the wake signal for task B.

---

## 5. Agents

### Per-agent on-disk workspace
Lives at `./workspace-{agent}/`:

| File | Purpose |
|---|---|
| `AGENTS.md` | Core rules about being an agent, using the workspace |
| `USER.md` | Knowledge and understanding of the user(s) |
| `IDENTITY.md` | Name, role, personality, emoji avatar |
| `MEMORY.md` | Curated long-term memory, **soft cap 2KB** |
| `TOOLS.md` | Local notes on using skills |
| `SOUL.md` | Core truths, boundaries, standing permissions, vibe |

### Memory model
- Each agent has full access only to its own context memory.
- Shared library, knowledge graph, and Qdrant are accessible to all agents.
- Daily memory at `memory/YYYYMMDD-MEMORY.md`, **append-only** (3 retries on write conflict, then give up to avoid race conditions).
- Curation may **modify** existing daily entries (compact, clean) or remove once moved to Qdrant.
- After 7 days, daily memory → vectorized to Qdrant → deleted from disk.

### Agent triggers (the complete list)
An agent only acts when:
1. The orchestrator wakes it
2. A user starts a chat session with it
3. Another agent pings it (sync inter-agent chat)
4. The curation cron fires
5. Other purpose-built crons fire (morning briefing, mailbox check, etc.)

No agent-owned cron loops in V0.1.

### Per-agent model permissions
- Config-driven whitelist or blacklist of models the agent may use
- Fallback when blocked: ask the operator

---

## 6. Sessions & Concurrency

### Session types
- **Heartbeat** — orchestrator-assigned task work; bulk of agent activity; archived but not streamed to user
- **User chat** — primary user-facing surface; siloed from heartbeats
- **Cron job** — fire-and-forget; logged to full-text history only; **does not touch MEMORY.md**
- **Inter-agent chat** — sync with busy-signal fallback (see §7)

All session types write to the same agent history (full-text + summarized). History is queryable across all session types for that agent.

### Concurrency model
- **Default per-agent cap: 3 simultaneous sessions** (configurable)
- Slot allocation:
  - **1 user-chat slot** — covers ALL simultaneous user conversations on that agent (humans are slow, so they share)
  - **Up to 2 machine slots** — heartbeats and inter-agent chats
- A heartbeat and an inter-agent chat may run simultaneously
- Within the user slot, multiple users talk to the same agent in parallel with **separate contexts**
- Each session has its own LLM context — **no context bleed** (this is the silo principle)

### Memory write rules under concurrency
- MEMORY.md is rarely touched directly during chat — primarily updated during curation
- Daily memory file is append-only with retry-3-then-fail
- If a chat-driven memory write fails after 3 retries, drop it (curation will catch it later from session history)

### Cross-session state visibility
- An update written during one session does not surface in another session in real time
- A user can ask in their next turn (e.g., "did your heartbeat finish that task?") and the agent will check and respond
- Exception: if the agent has standing direction to proactively report something, it surfaces in chat unprompted

### Token budgets
- Per-session-type token budgets: **deferred to V1.1**

---

## 7. Inter-Agent Communication

*This is where OpenClaw hurt most. V0.1 fixes it.*

### Why V0.1 is different from OpenClaw
- Agents are always triggerable via the harness — no "asleep agent" failure mode
- Receiving a message triggers the agent the same way a user message would
- Sync calls return a busy-signal when the receiver is over capacity, instead of timing out into a hang

### 1:1 sync chat
- Initiator sends → receiver wakes (or busy-signal returned)
- If receiver is busy and at concurrent cap:
  - Queue the message to fire at end of receiver's current task
  - Return busy-signal to initiator immediately
- If initiator's work can continue without the response → continue, handle reply on later turn
- If blocked waiting → start wait timer (default 30s, configurable per-call via tool param)
- If wait timer exceeds 2 minutes → mark initiator's task as `blocked, awaiting {receiver} response`
- Late reply may complete the task and clean itself up before next orchestrator cycle

### Group chats with "pass the mic"
- Initiator picks who goes first (usually self, since they called the meeting)
  - Fallback: Crew Chief picks
  - Fallback: Orchestrator picks best participant for topic
- Same agent may hold mic twice in a row only if waiting on a tool call result; otherwise must pass
- **End condition:** unanimous "I have nothing to add, we're done"
- **Hard turn cap:** 30 by default, configurable, `-1` for unlimited
- Conversation archived to each participant's history; summary to Qdrant tagged with all participants
- Chat session cleared after; initiator gets summary + session ID back

### User visibility
- Visible note in dashboard that agents are talking + the task they're discussing
- Live streams of inter-agent conversation: deferred

---

## 8. Tasks

### Data model
- DAG (no cycles)
- Dynamic: split, parallel, sequence, sub-agent assignment all supported
- Status set: `completed`, `failed`, `ongoing` (in-progress, will resume next cycle), `blocked`, `abandoned`
- Container tasks: track subtasks, do no work themselves
- Superseded tasks: closed records pointing at their replacements (audit trail)

### Authority to create tasks
- User
- Crew Chief
- Orchestrator (when it detects goals not progressing)
- Agents (when splitting their own assigned task, or via Chief)
- **User-assigned tasks take priority**

### Heartbeat task lifecycle
At end of a heartbeat the agent updates the Redis record with one of:
- `completed` + executive-summary
- `failed` + specific failure info
- `blocked` + what needs clearing (and may message user if only the user can clear it)
- `in progress - turn ##` if cycle ended mid-task; **abandoned at turn 10**

### Hierarchy
- **Mission** — one, brigade-wide, operator-defined; only the orchestrator sees it every cycle
- **Goals** — per-agent, operator-defined; agents use as decision guide, orchestrator uses for alignment checks
- **Tasks** — LLM-defined (mostly orchestrator), executed by agents

---

## 9. Multi-User

- **Agents shared by default** for MVP. Per-agent shared/private flag is V1.x.
- 1:1 user chat → user identity injected into system prompt at session start
- Multi-user-on-same-agent → consider per-turn injection (deferred detail)
- Mission is global to the brigade; goals are per-agent; nothing per-user except "who to report a completed task to, if anyone"
- RBAC:
  - **Owner** — full control, edits mission/goals/agents
  - **Operator** — chats, creates tasks, ingests knowledge
  - **Observer** — read-only dashboard + chat

---

## 10. Knowledge Library & Ingestion

### Sources
- Web articles (rewritten to markdown, stored full)
- PDFs (downloaded, stored)
- GitHub repos (downloaded, zipped)
- Books / long texts (plaintext or markdown)

### Submission paths
- Manual upload via dashboard
- User asks an agent to ingest something
- Agent finds something it wants to add (via tool call during a session)

### Pipeline
- Identify document type and source
- Chunk and store in Qdrant with metadata:
  - Single web pages and short emails → single chunk
  - Long articles / PDFs → small overlapping chunks
  - Books → chunked by chapter when possible (no overlap needed at chapter level)
- Metadata extracted for knowledge graph nodes/edges:
  - Author, Title, Type, Category, Subject keywords, Publication date

### Ingestion failures
- Surface to user, log, retry queue (specifics deferred — design pass during ingestion build)

---

## 11. Memory Curation

- **Cadence:** daily per agent, off-peak (default 2:30 AM local time)
- **Trigger:** separate cron, fire-and-forget. NOT a heartbeat task.
- **Model:** least expensive cloud model the agent can access, OR best local model
- **Prompt style:** freehand ("here are your daily memories, decide what to elevate")
- **Authority:** may add, remove, modify, or merge entries in MEMORY.md
- **Compaction trigger:** MEMORY.md exceeds 2KB (soft cap) → LLM rewrite-to-fit on next cycle
- **Daily memory archival:** after 7 days → vectorize to Qdrant → delete from disk

---

## 12. Error Handling Philosophy

| Failure | Response |
|---|---|
| Malformed LLM response | 3 retries, exponential backoff (1s, 4s, 16s), then fail the turn |
| Rate limit with timeframe | Flag orchestrator to run only local-inference-capable work |
| Rate limit / overload, no timeframe | Wait 5 minutes, retry (assume server-side overload) |
| Tool call fails (unrecoverable) | Surface to agent → try different approach → if none works, task fails |
| Datastore down (any of the 4) | **Fail loud.** Refuse to start cycle, alert user |
| Inconsistent agent self-report (claims done, output missing) | Orchestrator overrides to `failed`, asks agent why next cycle, retries |
| Same task fails 5x in a row | Alert user. Treated as block requiring human intervention |

---

## 13. Scope

### In V0.1 (MVP)
- TUI chat interface (single surface)
- Ollama provider; cloud providers (Anthropic / OpenAI / Gemini) wired but optional
- Mission → Goals → Tasks (DAG, dynamic, splittable)
- Orchestrator (LLM-backed, cron-driven, wakes agents)
- 4-store stack (Postgres + Redis + Qdrant + Neo4j)
- Per-agent workspaces with the 6 `.md` files
- Heartbeats, user chats, cron jobs as session types
- Sync inter-agent 1:1 chat with busy-signal/queue
- Group chat with "pass the mic"
- Memory curation (daily, separate cron)
- Multi-user with JWT auth, owner/operator/observer RBAC
- Knowledge ingestion via web upload + agent tool call
- Crew Chief role (user-designated)
- Monorepo, Docker Compose, CI on PR, public at MVP
- Alembic migrations, LiteLLM + native Ollama, JSON config & logging, UTC storage

### Out of V0.1 (V0.5 or later)
- Web GUI dashboard (beyond what auth/ingestion needs)
- Telegram, Discord, Slack, Signal, Google Chat surfaces
- Per-session-type token budgets
- Cloud-provider OAuth flows (Codex, Gemini OAuth — keep API keys only)
- Live-stream view of inter-agent chats

### Future Improvements (V1.x or later)
- Per-user agents (the GARDE-shared / Avis-private split)
- Leader election / orchestrator HA
- OpenTelemetry tracing
- Real secret manager (Vault, etc.)

---

## 14. Open Decisions for Later Passes

These are not blockers for build start, but need decisions before the relevant subsystem ships:

- **Neo4j schema** — node and edge types for documents, agents, tasks, decisions
- **Knowledge ingestion failure handling** — retry/dedup specifics
- **Per-session-type token budgets** — V1.1 work
- **Multi-user identity injection cadence** — system-prompt-once vs every-turn when 2+ users on same agent
- **Web dashboard design** — once the TUI is settled
- **Per-agent shared/private flag** — V1.x work for the GARDE/Avis split

---

*End of summary. The next step is to scaffold the monorepo and write the first Alembic migration. Spin up the dev VM/container when you're ready.*
