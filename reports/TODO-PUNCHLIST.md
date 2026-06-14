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
with Hermes on GUI/TUI. **Per the scope decisions below, no hard code blockers remain, and the
live hardening checks pass as of 2026-06-14: the bad-heartbeat gate
(`./ops/test-bad-heartbeats.sh`) passes after the heartbeat-repair fix and is now re-runnable,
and the destructive blank-userland new-user pass (`./ops/full-wipe.sh --confirm-full-wipe` +
fresh owner/agent/team/goal/delegation onboarding) passes with `empty_userland: true`. The
README truth-pass is the only remaining gate.** Several former
latent-feature gaps have since landed: per-agent model selection
during managed runs, structured subtask creation, delegation guards, malformed
orchestrator-output degradation, default-provider fallback for managed agent runs, connector
rate/size limits, and packaged web build serving all exist in code and tests. The remaining
work is final live/operator validation, keeping README wording honest, and treating **MCP
client** as the flagship first post-RC milestone.

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

5. **✅ Honor per-agent models in the daemon.** — **S**
   - *Why:* `Agent.model_provider`/`model_name` are stored and shown in the UI; managed runs
     need to honor those fields rather than flattening every agent through one operator-selected
     provider.
   - *Status:* implemented through the managed-agent provider factory used by `agent run-all`
     and `orchestrator daemon`; covered by `test_run_managed_agents_uses_per_agent_provider_factory`.
     *Report:* [06](06-model-self-selection.md).

6. **✅ Structured task synthesis for crew chiefs.** — **M**
   - *Why:* Chiefs are told to "build a task plan," but nothing converts the plan into
     tracked, dependency-linked child assignments
     ([report 05](05-subagents-delegation-synthesis.md)). `dependency_ids` exists but is
     never populated by synthesis.
   - *Status:* implemented as the `create_subtasks` tool with dependency linking and
     delegation guard coverage.

7. **✅ Harden the orchestrator escalation against bad model output.** — **S**
   - *Why:* `run_orchestrator_escalation` lets `parse_orchestrator_response` raise on
     malformed JSON ([`brigade/orchestrator.py:393`](../brigade/orchestrator.py)); a weak
     model could crash the daemon loop.
   - *Status:* implemented as parse-fail → `no_action` with warning telemetry; covered by
     `test_orchestrator_escalation_degrades_on_malformed_model_output`.

8. **✅ Default-provider fallback for managed agent runs.** — **S/M**
   - *Why:* `LiteLLMProvider` raises on provider failure with no fallback
     ([`brigade/providers.py:174`](../brigade/providers.py)). One provider outage stalls work.
   - *Status:* implemented for per-agent provider failures by retrying with the default
     provider during `agent run`, `agent run-all`, and daemon-managed runs. A richer ordered
     multi-provider route chain remains a post-RC model-routing enhancement.

9. **✅ Label Google Chat outbound accurately.** — **S**
   - *Why:* `google_chat_reply_sender` builds a body but never POSTs
     ([`brigade/connectors.py:449`](../brigade/connectors.py)). Inbound works, outbound is a
     stub.
   - *Status:* docs label Google Chat as inbound-only for RC. The webhook can return a
     threaded response body for approved senders, but it still does not POST to the Google
     Chat API.

10. **⚠️ Automate OAuth login + refresh-token rotation.** — **M**
    - *Why:* Current flow is manual code-exchange/import; expired tokens error instead of
      refreshing ([`brigade/providers.py:220`](../brigade/providers.py),
      [`secrets.py`](../brigade/secrets.py)).
    - *Do:* add a browser/device-code flow for the supported providers and auto-refresh on
      expiry.

11. **✅ Verify the web GUI build ships.** — **S**
    - *Why:* RC must install clean; confirm `vite build` output (`web/dist`) is packaged or
      built on first run and served by [`brigade/web.py`](../brigade/web.py).
    - *Status:* Dockerfiles copy `web/dist`, `brigade.web` serves the built SPA when present,
      and `test_web_index_serves_built_spa` covers `GET /`.

12. **⚠️ Keep the end-to-end proactivity demo current.** — **S**
    - *Why:* Proactivity is your headline differentiator; it must be reproducibly
      demonstrable (mission → daemon → idle chief planning task → delegate → execute →
      stale-task escalation), ideally visible in the ops-room SSE stream.
    - *Status:* propose-only mission-continuation behavior and idempotency are covered by
      tests and documented in `docs/RC_PROACTIVITY_DEMO.md`; rerun the documented live demo
      during the final RC gate.
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

With the scope decisions in Section A, **no hard code blockers remain, and the live hardening
checks pass as of 2026-06-14:** the bad-heartbeat gate passes after the heartbeat-repair fix,
and the destructive blank-userland new-user pass passes (clean wipe → rebuild → migrate →
`empty_userland: true`, then a fresh owner + token, agent/team/Crew Chief, goal, and
delegate/route-work all succeed on the empty stack). The README truth-pass is the only
remaining gate. The RC ships once the README matches reality and the cheap
"latent feature" fixes land:

**1. README truth-pass (≈ half a day, all small):**
- Claude = API key (not OAuth). — item 2
- MCP + Google-tools = roadmap, not current features. — items 1, 3
- Delegation = fixed agent roster; dynamic sub-agent spawn = v1.1. — item 4

**2. Fixes that already landed and need final gate verification:**
- **Item 5** — per-agent models are honored during managed runs.
- **Item 6** — Crew Chiefs can create structured, dependency-linked subtasks.
- **Item 7** — malformed orchestrator model output degrades to `no_action`.
- **Item 8** — managed runs retry per-agent provider failures with the default provider.
- **Item 11** — the built web GUI is packaged and served.
- **Item 12** — the documented proactivity demo was reproduced on 2026-06-14: propose-only
  mission continuation (`proposed=1 created=0`, `trigger=mission_idle_no_active_or_queued_work`)
  with `orchestrator-proactive:v1:<sha256>` provenance and idempotent `duplicate_idempotency_key`
  dedupe on the following cycle.

**3. MCP Client:**
- MCP client **(Item 1)** — unlocks Google tools and broad third-party tooling in one stroke.
- GUI/CLI method of adding MCP Servers

That set removes every over-promise and makes the three genuine strengths — proactive
orchestration, dual GUI/TUI, multi-provider models — provable on a fresh checkout.

**4. First post-RC milestone:** Dynamic sub-agent spawning (item 4/19-range).
The remaining ⚠️ items raise quality; the 💡 items are the rest of the roadmap.
