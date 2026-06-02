# OpenBrigade — RC TO-DO Punchlist

Final deliverable. Derived from reports [01](01-agent-tool-use.md)–[07](07-reference-inventories.md).
Ordered for shipping a credible Release Candidate on GitHub. Each item: what, why, where,
effort (S/M/L), and RC classification.

**Legend:** 🚫 Blocker · ⚠️ Should-fix before RC · 💡 Post-RC / roadmap
Effort: **S** ≈ hours · **M** ≈ 1–3 days · **L** ≈ 1–2 weeks

---

## Where OpenBrigade stands (one paragraph)

The core is real and runs: a sound tool-use loop, a genuinely proactive orchestrator with
idle/stale detection and bounded LLM escalation, working Telegram, multi-provider model
access (Claude/OpenAI/Gemini API + Ollama), and **both** a web GUI and a TUI wired to the
same backend. You are *ahead* of the proactive reference set on orchestration and at parity
with Hermes on GUI/TUI. **Per the scope decisions below, there are no hard RC blockers
left** — the remaining work is (1) make the README match reality so nothing is
over-promised, (2) land a handful of cheap fixes for *latent* features the code advertises
but the runtime ignores (per-agent model selection; structured task synthesis; daemon
robustness), and (3) treat **MCP client** as the flagship first post-RC milestone. None of
the gaps are architectural dead-ends.

---

## A. Scope decisions for RC (resolved) — reword the README, defer the builds

These were the four candidate blockers. With the owner's decisions, **none block the RC**;
each becomes a documentation change now + a roadmap item later.

1. **MCP client (consume external MCP servers as tools).** — **L** — *flagship post-RC, do first.*
   - *Decision:* Not RC-blocking, but it is the **single highest-value next build** and the
     chosen delivery path for Google tools. Schedule it as the first milestone after RC.
   - *Where:* new module alongside [`brigade/tools.py`](../brigade/tools.py); register MCP
     tools into `ToolRegistry`; config in [`brigade/config.py`](../brigade/config.py).
   - *RC action:* in the README, list MCP as "on the roadmap," not a current feature.
   - *Report:* [02](02-external-connectors.md).

2. **Claude OAuth.** — **S (reword)** — *nice-to-have, deferred.*
   - *Decision:* De-escalated. Third-party harness OAuth use is limited anyway; **Claude
     stays API-key only for RC.**
   - *RC action:* document "Claude = API key"; remove any "Claude OAuth" claim. (Code already
     supports Anthropic API key — no build needed.)

3. **Google tools (Drive/Gmail).** — **S (reword)** — *delivered via MCP, deferred.*
   - *Decision:* No bespoke Google integrations. Gmail/Drive arrive through community MCP
     servers once the MCP client (item 1) lands.
   - *RC action:* reword the goal to "Google tools via MCP (roadmap)"; don't ship a native
     build.

4. **Agents spin up sub-agents.** — **M** — *the one "real" gap; → v1.1.*
   - *Decision:* Acknowledged as the only genuine capability gap, **scoped to v1.1.** RC ships
     with delegation to a fixed roster.
   - *RC action:* document the system as "delegation across a fixed agent roster"; add the
     cheap **delegation depth/fan-out guard** (see item 15) so the fixed-roster path can't
     recurse. Defer true dynamic spawn to v1.1.
   - *Report:* [05](05-subagents-delegation-synthesis.md).

---

## B. Should-fix before RC ⚠️ — latent features & visible stubs

5. **⚠️ Honor per-agent models in the daemon.** — **S**
   - *Why:* `Agent.model_provider`/`model_name` are stored and shown in the UI but the daemon
     runs everyone through one global provider
     ([`brigade/runner.py:73`](../brigade/runner.py), [`cli.py:1898`](../brigade/cli.py)).
     This mismatch will be noticed.
   - *Do:* build each agent's provider via `provider_from_settings` inside
     `run_managed_agents`. *Report:* [06](06-model-self-selection.md).

6. **⚠️ Structured task synthesis for crew chiefs.** — **M**
   - *Why:* Chiefs are told to "build a task plan," but nothing converts the plan into
     tracked, dependency-linked child assignments
     ([report 05](05-subagents-delegation-synthesis.md)). `dependency_ids` exists but is
     never populated by synthesis.
   - *Do:* add a `decompose` / `create_subtasks` orchestrator action (or chief tool) that
     emits N child assignments with `dependency_ids` wired.

7. **⚠️ Harden the orchestrator escalation against bad model output.** — **S**
   - *Why:* `run_orchestrator_escalation` lets `parse_orchestrator_response` raise on
     malformed JSON ([`brigade/orchestrator.py:393`](../brigade/orchestrator.py)); a weak
     model could crash the daemon loop.
   - *Do:* catch/parse-fail → record alert + `no_action`, never propagate.

8. **⚠️ Model fallback chain.** — **S/M**
   - *Why:* `LiteLLMProvider` raises on provider failure with no fallback
     ([`brigade/providers.py:174`](../brigade/providers.py)). One provider outage stalls work.
   - *Do:* wrap providers so an auth/availability error tries the next configured route.

9. **⚠️ Finish or label Google Chat outbound.** — **S**
   - *Why:* `google_chat_reply_sender` builds a body but never POSTs
     ([`brigade/connectors.py:449`](../brigade/connectors.py)). Inbound works, outbound is a
     stub.
   - *Do:* implement the real Chat API POST, or mark the connector "inbound-only" in docs.

10. **⚠️ Automate OAuth login + refresh-token rotation.** — **M**
    - *Why:* Current flow is manual code-exchange/import; expired tokens error instead of
      refreshing ([`brigade/providers.py:220`](../brigade/providers.py),
      [`secrets.py`](../brigade/secrets.py)).
    - *Do:* add a browser/device-code flow for the supported providers and auto-refresh on
      expiry.

11. **⚠️ Verify the web GUI build ships.** — **S**
    - *Why:* RC must install clean; confirm `vite build` output (`web/dist`) is packaged or
      built on first run and served by [`brigade/web.py`](../brigade/web.py).
    - *Do:* add a build/package step (and a smoke test that `GET /` returns the SPA).

12. **⚠️ Capture an end-to-end proactivity demo.** — **S**
    - *Why:* Proactivity is your headline differentiator; it must be reproducibly
      demonstrable (mission → daemon → idle chief planning task → delegate → execute →
      stale-task escalation), ideally visible in the ops-room SSE stream.
    - *Report:* [04](04-orchestrator-proactivity.md).

---

## C. Polish & parity (nice-to-have for RC)

13. **💡 Tool capability gating** — hide a tool when its prerequisite/env is absent (Hermes
    check-function pattern). **S**. [Report 01](01-agent-tool-use.md).
14. **💡 Per-role / per-team tool grants** — leverage existing `role`/`team_id`/`rbac.py`. **M**.
15. **💡 Delegation recursion guard tests** — even with the fixed roster, add depth caps + tests. **S**.
16. **💡 Richer TUI** — inline tool-call streaming, `/model` override (parity with refs). **M**.

---

## D. Post-RC roadmap 💡 (document, don't build now)

17. **💡 Expose OpenBrigade as an MCP *server*** (Claude Desktop / IDE / other agents). **M**.
18. **💡 Difficulty/cost-aware routing via ABACUS** — wire the financial report into dispatch
    so routine work goes local and hard/extended work goes cloud, honoring
    `block_cloud_dispatch`. Full realization of "self-selection." **L**.
    [Report 06](06-model-self-selection.md).
19. **💡 Self-improvement / reflection loop** — tiered correction memory with promotion/decay,
    borrowed from `self-improving-with-reflection-1.2.11`. You have episodes/knowledge but no
    reflection-driven self-correction. **L**. [Report 07](07-reference-inventories.md).
20. **💡 Adaptive orchestrator cadence** — replace fixed `orchestrator_cadence_seconds` with
    load/activity-based timing (LeoProactiveAgent pattern). **M**.
21. **💡 Connector breadth** — Discord/Slack/Signal/Matrix to approach reference parity. **L**.
22. **💡 Native mobile apps / i18n** — only if they become product goals (OpenClaw-only today). **L**.

---

## Minimum path to a defensible RC

With the scope decisions in Section A, **no hard blockers remain.** The RC ships once the
README matches reality and the cheap "latent feature" fixes land:

**1. README truth-pass (≈ half a day, all small):**
- Claude = API key (not OAuth). — item 2
- MCP + Google-tools = roadmap, not current features. — items 1, 3
- Delegation = fixed agent roster; dynamic sub-agent spawn = v1.1. — item 4

**2. Cheap fixes that make advertised features real / robust:**
- **Item 5** — honor per-agent models in the daemon (UI-visible feature, ~hours).
- **Item 7** — don't let a malformed model response crash the orchestrator daemon.
- **Item 15** — delegation depth/fan-out guard (prevents runaway delegation on the fixed roster).
- **Item 11** — verify the web GUI build ships on a clean install.
- **Item 12** — capture a reproducible end-to-end proactivity demo (the headline differentiator).

**3. MCP Client:**
- MCP client **(Item 1)** — unlocks Google tools and broad third-party tooling in one stroke.
- GUI/CLI method of adding MCP Servers

That set removes every over-promise and makes the three genuine strengths — proactive
orchestration, dual GUI/TUI, multi-provider models — provable on a fresh checkout.

**4. First post-RC milestone:** Dynamic sub-agent spawning (item 4/19-range).
The remaining ⚠️ items raise quality; the 💡 items are the rest of the roadmap.
