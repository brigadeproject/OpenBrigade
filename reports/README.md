# OpenBrigade — RC Feature-Completeness Reports

Generated 2026-05-31. Comparison of the OpenBrigade codebase (`brigade/` + `web/`)
against reference systems **OpenClaw** (`reference/openclaw`) and **HermesAgent**
(`reference/hermes-agent`), plus the proactive/self-improving reference set.

**Bar used:** "working end-to-end." A feature counts as complete only if it is wired
and runnable end-to-end. Scaffolding or stubs are classified as gaps. A **blocker** is
anything that would stop a usable Release Candidate from shipping on GitHub.

> Note: `reference/agent-system-design-v1.3.md` is OpenBrigade's own design document.
> It is treated here as the spec of intent, not a competitor.

## Reports

| # | Report | Verdict (one-line) |
|---|--------|--------------------|
| 01 | [Agent Tool Use](01-agent-tool-use.md) | Functional, narrow toolset; MCP on roadmap. |
| 02 | [External Connectors](02-external-connectors.md) | Telegram done; Claude OAuth deferred, Google tools + MCP = roadmap. |
| 03 | [GUI & TUI](03-gui-tui.md) | Both ship and are wired. Strongest area. |
| 04 | [Orchestrator & Proactivity](04-orchestrator-proactivity.md) | Genuinely proactive; strong differentiator. |
| 05 | [Sub-agents, Delegation & Goal Synthesis](05-subagents-delegation-synthesis.md) | Delegation works; dynamic sub-agent spawn = v1.1. |
| 06 | [Model Self-Selection](06-model-self-selection.md) | Latent (stored, not run); cheap to make real. |
| 07 | [Reference Inventories (appendix)](07-reference-inventories.md) | Raw OpenClaw/Hermes/proactive findings. |
| — | [**TO-DO PUNCHLIST**](TODO-PUNCHLIST.md) | **Start here for action items.** |

> **Scope note (owner decisions, 2026-05-31):** No hard RC blockers remain. Claude OAuth is
> deferred (Claude stays API-key only); Google tools are delivered via MCP, not bespoke;
> MCP client is the flagship first post-RC milestone; dynamic sub-agent spawning is scoped
> to v1.1. The 🚫 markers in reports 02 and 05 reflect the original pre-decision analysis and
> are reconciled to these decisions in the [punchlist](TODO-PUNCHLIST.md).

## How to read the verdicts

- ✅ **Complete** — wired end-to-end, runnable.
- ⚠️ **Partial** — present but incomplete, stubbed, or not wired into the runtime.
- ❌ **Missing** — not present.
- 🚫 **Blocker** — must be resolved (or explicitly scoped out) before RC.
