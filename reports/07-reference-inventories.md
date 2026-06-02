# 07 — Reference Inventories (Appendix)

Raw feature inventories of the reference systems, captured during the survey. Use these as
the evidence base behind the per-feature verdicts. File paths are within each repo under
`reference/`.

---

## OpenClaw (`reference/openclaw`) — TypeScript / pnpm monorepo

- **Tool use:** `src/tools/` descriptor framework with availability guards; tools ship
  embedded *and* as standalone MCP servers (`src/mcp/openclaw-tools-serve.ts`); permission/
  sandbox model around execution.
- **Connectors (15+ channels, each a first-class extension under `extensions/`):** Telegram,
  WhatsApp, Discord, Slack, Signal, Google Chat, iMessage, Matrix, Nostr, Line, Feishu,
  MS Teams, Nextcloud Talk, QQ, Twitch, Zalo, voice-call (Telnyx), + email via `skills/himalaya`.
- **Model providers:** 50+ provider extensions incl. Anthropic (API/OAuth/Vertex), OpenAI,
  Google Gemini (API + OAuth), Bedrock, Ollama. Auth profiles per agent; OAuth credential
  store under `~/.openclaw/credentials/`.
- **Google Workspace:** partial — Google OAuth (`extensions/google/oauth.ts`) and a
  `resolveHooksGmailModel` reference, but no dedicated Gmail/Drive channel; relies on
  SDK + OAuth pass-through.
- **MCP:** first-class, both client and server (`src/mcp/*`, `src/gateway/mcp-http.*`).
- **GUI:** Lit web-components Control UI (`ui/src/`) + Node gateway (`src/gateway/`), i18n;
  **native mobile/desktop apps** (`apps/ios`, `apps/android`, `apps/macos`).
- **TUI:** `src/tui/` on `@earendil-works/pi-tui` — chat log, autocomplete, `/model` override.
- **Orchestration:** ACP "Agent Control Plane" (`src/acp/`) — session manager, turn-based
  execution, spawn lifecycle, event ledger, permission relay. Proactivity via
  `src/tasks/task-registry.ts`, `src/infra/heartbeat-wake.js`, `src/auto-reply/`, cron tool.
- **Sub-agents:** dynamic spawn (`src/agents/acp-spawn.ts`, `subagent-spawn-plan.ts`) with
  depth/breadth limits (`src/config/agent-limits.ts`).
- **Model self-selection:** live switch (`live-model-switch.ts`) + automatic fallback
  (`model-fallback.ts`) + visibility policy.
- **Skills:** 100+ bundled in `skills/`, each a `SKILL.md` with install/eligibility metadata.

**Standouts:** ACP orchestration, channel breadth, MCP-as-protocol, model fallback chains,
mobile apps.

---

## HermesAgent (`reference/hermes-agent`) — Python (+ React web, Ink TUI)

- **Tool use:** self-registering tool modules (`tools/registry.py`), composable `toolsets.py`,
  per-platform `toolset_distributions.py`; ~80 tools; check-function gating.
- **Connectors (20+ under `gateway/platforms/`):** Telegram, Discord, Slack, Signal, Matrix,
  Email, SMS, WeChat/WeCom, Feishu/Lark, DingTalk, Home Assistant, QQ, Mattermost, webhook,
  BlueBubbles, etc. Bidirectional + approval workflows.
- **Model providers:** Anthropic (API + OAuth via Claude Code creds), Gemini (native +
  CloudCode), Bedrock, OpenAI-compatible transport (OpenAI, Vercel, Deepseek, Kimi, Qwen,
  Ollama, LM Studio, xAI…), Codex. models.dev registry for metadata.
- **Google Workspace:** stub — Google OAuth (`agent/google_oauth.py`) + Code Assist, but **no
  Gmail/Drive/Calendar tools**. Ships Microsoft Graph and Feishu/Lark instead.
- **MCP:** client (`tools/mcp_tool.py`) and server (`mcp_serve.py`,
  `agent/transports/hermes_tools_mcp_server.py`); OAuth-aware, circuit breaker.
- **GUI:** React 19 + Vite + Tailwind + xterm.js (`web/`).
- **TUI:** Ink (React-for-terminal) at `ui-tui/`, driven by `tui_gateway/`.
- **Orchestration:** Kanban dispatcher (`cron/scheduler.py`, `tools/kanban_tools.py`,
  `~/.hermes/kanban.db`) + cron jobs with injection scanning; workers via env var handoff.
- **Sub-agents:** `tools/delegate_tool.py` spawns isolated child `AIAgent` via thread pool;
  restricted child toolset (recursion guard).
- **Model self-selection:** metadata only (models.dev); **no runtime auto-routing** — user
  selects.
- **Skills:** ~87 bundled, agentskills.io-compatible `SKILL.md`, progressive disclosure,
  platform gating.
- **ACP adapter:** `acp_adapter/` — JSON-RPC ACP server exposing Hermes to ACP clients
  (Copilot, OpenClaw).

**Standouts:** channel breadth, modular toolsets, production MCP (client+server), kanban
orchestration, rich model metadata.

---

## Proactive / self-improving reference set

Mined for proactivity, self-improvement, sub-agent, model-selection, and goal-synthesis
patterns. (Note: `agent-system-design-v1.3.md` is **OpenBrigade's own** design doc.)

- **proactive-claw-1.2.41** — strongest proactivity. Two-phase daemon (`scripts/daemon.py`):
  PLAN (scan + score events via `proactivity_engine.py`) → EXECUTE (fire due actions
  idempotently). `orchestrator.py` fans out parallel prep flows for high-stakes events;
  `intelligence_loop.py` does post-event follow-up synthesis.
- **LeoProactiveAgent** — async wake-loop (`proactiveagent/scheduler.py`) + multi-factor
  decision engine (`decision_engines/ai_based.py`) + adaptive sleep calculators.
- **ThuProactiveAgent** — event-driven via filesystem/input watchers + Activity Watch;
  `AgentCore.reflect()` logs learnings.
- **self-improving-with-reflection-1.2.11** — most complete learning lifecycle: tiered
  HOT/WARM/COLD markdown memory, correction capture, promote-after-3x, decay/archive,
  structured `reflections.md`, heartbeat maintenance.
- **self-improving-1.2.16 / self-improving-agent** — same tiered-memory family; the latter
  promotes learnings into `SOUL.md`/`AGENTS.md`/`TOOLS.md` and references OpenClaw
  `sessions_spawn()` for sub-agents.
- **self-improving-proactive-agent-1.0.0** — merges self-improvement + a
  `proactivity/session-state.md` carrying objective / decision / blocker / next-move, with
  goal objects (`success_criteria`, `explicitly_not`).

**Reusable patterns OpenBrigade does *not* yet have:**
- **Self-improvement / reflection memory** (tiered, correction-promotion, decay). OpenBrigade
  has knowledge chunks + episodes but no reflection-driven self-correction loop.
- **Adaptive cadence** (AI/pattern-based sleep timing) vs. OpenBrigade's fixed
  `orchestrator_cadence_seconds`.
