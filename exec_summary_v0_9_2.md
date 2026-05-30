# OpenBrigade v0.9.2 — Pre-RC Sanity Check

  **1. Executive summary**

  OpenBrigade is a well-engineered orchestrator + assignment tracker + chat relay + workspace bookkeeping service with solid Postgres/Redis/Qdrant plumbing, RBAC, OAuth secret handling, two webhook connectors,
   transcripts, and ~4.5k LOC of tests. As an agent harness, the load-bearing parts are not yet implemented: there is no tool layer, no reasoning loop, the prompt sent to the model contains none of the context
   the architecture docs promise, the daemon never executes agents, and the response protocol silently archives any non-JSON prose as "complete." Verdict: not RC-ready as "agent harness"; possibly RC-ready as
  "orchestrator MVP" if rescoped.

  **2. RC red lights**

  *Blockers*
  - B-1 No tool execution surface. No registry, no dispatch, no schema, no MCP, no shell/fs/web/delegate tool. workspace.py:11 references a static TOOLS.md; nothing reads it. Refs: Hermes tools/registry.py +
  model_tools.handle_function_call; OpenClaw src/tools/ + src/mcp/.
  - B-2 Single-shot agent loop. run_agent_once (runner.py:178) calls the provider once and returns. No iteration, no observations. Refs: Hermes run_agent.py IterationBudget loop.
  - B-3 Prompt has no context. Runner sends assignment.assignment as the entire prompt; chat paths send 5 fixed lines. IDENTITY.md, MEMORY.md, SOUL.md, goals, episodes, knowledge — none injected.
  PROMPT_ARCHITECTURE.md promises all of it.
  - B-4 Daemon doesn't run agents. cli.py:1881 daemon loop runs only _run_cycle (deterministic assignment). Agents only fire on manual brigade agent run / run-all. Concept doc says cron launches heartbeats.
  - B-5 JSON-status contract unworkable with real models. parse_agent_response (runner.py:237) treats any non-{ response as complete with the prose as summary; the runner never instructs the model to emit the
  schema. Real Ollama/OpenAI/Gemini prose will be silently archived complete.

  *Highs that may block RC*
  - H-1 No Anthropic provider despite Concept requirement (may work via litellm fall-through — unverified).
  - H-2 Cost always recorded as $0 — financial routing is decorative.
  - H-3 Qdrant vectors hard-coded [0.0] with size: 1 — no embedding provider exists.
  - H-4 Structured logging configured but zero logger.* calls in 12.7k LOC of brigade/.
  - H-5 No test exercises the runner against real-model-shaped output (only the FakeProvider that emits exact JSON).
  - H-6 No assignment cancel/interrupt; no wall-clock per-cycle timeout.
  - H-7 __version__ = "0.1.0" in code and pyproject vs "v0.9.2" in docs.

  **3. Critical omissions by area**

  - Tool execution — entirely absent (B-1).
  - External connections — adequate for Telegram + Google Chat inbound + LiteLLM cloud; outbound action surface is one Telegram send; Google Chat "outbound" returns the body but never POSTs
  (connectors.py:438-450); no Anthropic adapter.
  - Agent handling — create/onboard/RBAC/state machine/execution-claim are solid; autonomous firing missing (B-4); multi-agent inter-agent chat unimplemented (Concept lines 79-88); auto stalled-task detection
  only runs on operator command.
  - Memory/context — written to many backends but never retrieved into a prompt (B-3); Qdrant has no real vectors (H-3); archive_stale_daily_memories never scheduled; no Neo4j retrieval path.
  - Orchestration/task management — assignment model, transitions, idempotency, abandonment/blocked caps, two-tier execution claim all solid; daemon does not execute (B-4); Assignment.dependency_ids declared
  but unread by the scheduler; no cancellation primitive.
  - Observability/debugging — JSON logging wired, never used (H-4); transcripts and audit records exist; no event stream; debugging a stuck agent = reading Postgres.
  - Configuration/setup — config.py, health.py, example JSON config are clean; psycopg/redis are core deps; no "minimal" config that runs without Postgres unless allow_json_store is set.
  - Tests — strong on RBAC, ingestion, state store concurrency, connector approval, schemas, migrations. Missing: any tool-execution test (because layer is absent — Blocker per the rules), any real-model parse
   test, any prompt-context-injection assertion, any daemon-runs-agents assertion, any cost-computation test, any Qdrant retrieval test.

  **4. What appears complete enough for RC cleanup**

  Assignment state machine + ALLOWED_TRANSITIONS, heartbeat-block parsing with strict validation codes, two-tier execution claim (Redis lease + Postgres advisory), connector inbound pipeline (rate-limit →
  identity-approval → audit), webhook secret gating, OAuth secret persistence + redaction, Postgres migration runner with advisory lock, RBAC roles + permission checks, available_model_options preference
  scoring. Test coverage of these is genuinely good.

  **5. Unclear areas**

  - Dependency scheduling: dependency_ids declared, no reader found — confirm via grep -rn dependency_ids brigade/.
  - Anthropic via LiteLLM: provider_from_settings has a generic LiteLLM fall-through — provider="anthropic" may work, but no test/doc confirms it.
  - What "v0.9.2" means: ob_v0_9-testing-results.md suggests UI/UX scope. If so, many Blockers above might be deliberately deferred; need an authoritative RC scope statement.
  - Knowledge retrieval: knowledge.py ingests + writes Qdrant/Neo4j; no retrieval call found from runner/services/connectors.
  - brigade_orchestrator Compose entrypoint: confirm it isn't somehow invoking run-all outside the daemon path.

  Files to resolve: /opt/openbrigade/docker-compose.yml, /opt/openbrigade/ops/check-recovery.sh, /opt/openbrigade/ops/brigade-live.sh, brigade/cli.py:870-905.

  **6. Recommended next step (prioritized)**

  1. To be an "agent harness": add a runner system prompt that (a) teaches the JSON-status protocol, (b) injects mission + identity + goals + curated MEMORY.md, (c) defaults non-JSON responses to working, not complete. (Closes B-3 / B-5 cheaply.)
  2. Wire the daemon to also run agents — even a --also-run-agents flag calling run_managed_agents after each _run_cycle (B-4).
  3. Instrument structured logging across runner/orchestrator/connectors/providers (H-4).
  4. Replace Qdrant [0.0] vectors with real embeddings, or remove Qdrant from the stack until v1.x (H-3).
  5. Align version strings (__init__.py, pyproject.toml, docs) (H-7).
  6. Add a regression test that runs the runner against non-JSON model output (H-5 / B-5).
  7. Build minimum: tool registry + dispatcher (Hermes pattern: ~5 starter tools — shell, read_file, write_file, web_fetch, delegate), in-cycle tool-call/observation loop in runner.py, prompt builder that actually injects identity + mission + active goals + curated memory, daemon path that fires heartbeats autonomously, fix the response protocol (B-5).
