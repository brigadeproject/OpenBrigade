# 06 — Agent Model Self-Selection

Your stated concern: *"Agent model self-selection when necessary."*

## What "self-selection" means here

An agent (or the orchestrator on its behalf) choosing or switching the model based on task
difficulty, cost, or availability — e.g., route a routine heartbeat to a local Ollama model
but escalate a hard reasoning task to a cloud model, or fall back when a provider is down.

## What OpenBrigade has

**The first behavior is now real; the full self-selection loop is still roadmap.**

- **Per-agent model config exists in the data model:** `Agent.model_provider` /
  `Agent.model_name` ([`brigade/schemas.py:235`](../brigade/schemas.py)).
- **A recommendation engine exists:** `available_model_options` + `_recommended_option` +
  `_model_preference_score` ([`brigade/providers.py:300`](../brigade/providers.py)) ranks
  installed/available models and is surfaced at `GET /api/models`
  ([`brigade/web.py:315`](../brigade/web.py)). This *advises* a human/UI; it does not let an
  agent route itself.
- **Route typing exists:** providers carry `route_type` (`local` / `cloud`),
  and the runner already treats cloud vs. local differently (cloud single-flight guard,
  local inference lock/cooldown) — [`brigade/runner.py:177`](../brigade/runner.py).
- **Per-agent model config is honored during managed runs:** `agent run`, `agent run-all`,
  and `orchestrator daemon` build providers from each agent's configured provider/model when
  no explicit provider override is supplied. Managed runs also retry a per-agent provider
  failure with the default provider.

## The gap

- **No ordered multi-provider fallback chain.** Managed agent runs fall back from an agent's
  configured provider to the default provider, but `LiteLLMProvider` does not yet own a
  configured route list such as `openai -> gemini -> ollama`.
- **No task-difficulty runtime switch.** There is no "this task is hard -> escalate model"
  decision anywhere.
- **No cost-driven routing.** The design doc's **ABACUS** financial agent (the intended
  cost-router that would set `block_cloud_dispatch`) is only partially realized:
  `persist_financial_report` ([`brigade/finance.py`](../brigade/finance.py)) records spend,
  and the runner has a *static* cloud single-flight guard, but nothing reads the financial
  report to *choose* a cheaper route.

## Comparison

| Capability | OpenBrigade | OpenClaw | Hermes | proactive set |
|---|---|---|---|---|
| Per-agent model config | ✅ | ✅ | ✅ | fixed |
| Honor per-agent model at runtime | ✅ managed runs | ✅ | ✅ | n/a |
| Live model switch mid-session | ❌ | ✅ (`live-model-switch.ts`) | ❌ | ❌ |
| Automatic fallback chain | ⚠️ default-provider retry | ✅ (`model-fallback.ts`) | ❌ | ❌ |
| Cost/difficulty-based routing | ❌ | partial | partial (metadata only) | design-doc only (ABACUS) |
| Model metadata (cost/ctx/caps) | ❌ | ✅ | ✅ (models.dev) | ❌ |

Note: even the references mostly stop at *fallback* and *manual switch*. True
*difficulty-aware self-selection* is rare — Hermes ships the metadata but explicitly leaves
selection to the user. So your bar ("when necessary") is achievable without matching a
mature competitor, because no competitor fully does it either.

## RC assessment

⚠️ **Partial, no longer a soft blocker.** The UI-visible per-agent model setting is now
honored during managed runs, so the former latent-feature mismatch is closed. The remaining
work is richer routing rather than basic correctness:

1. **Add an ordered fallback chain** in `LiteLLMProvider` (or a wrapping provider): on
   auth/availability error, try the next configured route. Low effort, high reliability
   payoff.

Defer to post-RC (document as roadmap):

2. **Difficulty/cost-aware routing** — wire ABACUS so the orchestrator can downgrade routine
   work to local and reserve cloud for hard/extended tasks, honoring `block_cloud_dispatch`.
   This is the full realization of "self-selection" and matches your design doc, but it is
   not needed to ship a credible RC.
