# 06 — Agent Model Self-Selection

Your stated concern: *"Agent model self-selection when necessary."*

## What "self-selection" means here

An agent (or the orchestrator on its behalf) choosing or switching the model based on task
difficulty, cost, or availability — e.g., route a routine heartbeat to a local Ollama model
but escalate a hard reasoning task to a cloud model, or fall back when a provider is down.

## What OpenBrigade has

**The ingredients, but not the behavior.**

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

## The gap

- **The daemon runs every agent through one global provider.**
  `run_managed_agents(store, provider)` ([`runner.py:73`](../brigade/runner.py)) is called
  with a single `_provider_from_args(args, settings)`
  ([`cli.py:1898`](../brigade/cli.py)). The per-agent `model_provider`/`model_name` fields
  are **stored but never consulted at run time** in the daemon path.
- **No runtime switch / fallback.** `LiteLLMProvider` does not implement a fallback chain;
  on auth/availability failure it raises (`_map_litellm_error`) rather than degrading to
  another model. There is no "this task is hard → escalate model" decision anywhere.
- **No cost-driven routing.** The design doc's **ABACUS** financial agent (the intended
  cost-router that would set `block_cloud_dispatch`) is only partially realized:
  `persist_financial_report` ([`brigade/finance.py`](../brigade/finance.py)) records spend,
  and the runner has a *static* cloud single-flight guard, but nothing reads the financial
  report to *choose* a cheaper route.

## Comparison

| Capability | OpenBrigade | OpenClaw | Hermes | proactive set |
|---|---|---|---|---|
| Per-agent model config | ✅ (stored only) | ✅ | ✅ | fixed |
| Honor per-agent model at runtime | ❌ | ✅ | ✅ | n/a |
| Live model switch mid-session | ❌ | ✅ (`live-model-switch.ts`) | ❌ | ❌ |
| Automatic fallback chain | ❌ | ✅ (`model-fallback.ts`) | ❌ | ❌ |
| Cost/difficulty-based routing | ❌ | partial | partial (metadata only) | design-doc only (ABACUS) |
| Model metadata (cost/ctx/caps) | ❌ | ✅ | ✅ (models.dev) | ❌ |

Note: even the references mostly stop at *fallback* and *manual switch*. True
*difficulty-aware self-selection* is rare — Hermes ships the metadata but explicitly leaves
selection to the user. So your bar ("when necessary") is achievable without matching a
mature competitor, because no competitor fully does it either.

## RC assessment

⚠️ **Partial → likely a soft blocker**, because today the feature is *latent*: the UI shows
model options and agents carry a model field, but the daemon ignores both. That mismatch
will be noticed. Minimum viable closes in two steps, in order of value:

1. **Honor per-agent models** — make `run_managed_agents` build the provider from each
   agent's `model_provider`/`model_name` (via `provider_from_settings`) instead of one
   global provider. This makes the stored config *real* and is a small change. **Do this for
   RC.**
2. **Add a fallback chain** in `LiteLLMProvider` (or a wrapping provider): on
   auth/availability error, try the next configured route. Low effort, high reliability
   payoff.

Defer to post-RC (document as roadmap):

3. **Difficulty/cost-aware routing** — wire ABACUS so the orchestrator can downgrade routine
   work to local and reserve cloud for hard/extended tasks, honoring `block_cloud_dispatch`.
   This is the full realization of "self-selection" and matches your design doc, but it is
   not needed to ship a credible RC.
