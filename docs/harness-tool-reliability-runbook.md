# OpenBrigade Harness — Tool-Call Reliability Runbook

**Purpose:** Get local-model tool calling (especially `write_file`) reliable in the OpenBrigade harness.
**Scope:** Tool-call path first; related bugs surfaced in testing tracked alongside.
**Audience:** Works as a brief for Claude Code (Fable 5) *and* as a by-hand checklist for Tom.
**Companion doc:** `harness-tool-findings.md` (the why, the bug receipts, the notes).

**Current known state (as of this session):**
- Ollama `v0.30.6` — current stable, past all the old version-pin workarounds. If failures persist here, cause is format/parser, not a stale-Ollama bug.
- `gpt-oss:20b` — the primary offender. Root cause is Harmony channel-format, not model capability.
- No Qwen3 of any kind installed. Only Qwen present is `qwen2.5-coder:7b`.
- Current agent model: `Ornith-1.0-9B`.

---

## How to use this

Work top to bottom. Each step is: **do → observe → branch.** Don't skip Phase 0 — it's the one that tells you *where* the break is, and everything after depends on knowing that. Check boxes as you go so a resumed session (yours or Claude Code's) knows where it stands.

---

## Phase 0 — See the actual prompt (the single most useful step)

The whole investigation turns on one question: **is the tool schema reaching the model intact, and is the model's reply being parsed correctly on the way back?** You cannot answer that from the harness's point of view alone. You have to see the raw prompt Ollama builds and the raw bytes the model returns.

- [x] **0.1 — Turn on debug prompt dumping.** Restart the Ollama server with `OLLAMA_DEBUG=2`. This logs the fully-rendered prompt sent to the model runner. *(Done 2026-07-02 via a second `ollama serve` instance on port 11436 with `OLLAMA_DEBUG=2`, sharing the service's model store read-only — no sudo/service restart needed. Log: session scratchpad `ollama-debug.log`.)*
- [x] **0.2 — Run one clean `write_file` call** through the harness against `gpt-oss:20b`. Something trivial and known-good: write "hello" to a temp path. *(Done — real `OllamaProvider` + `_native_tool_specs` path.)*
- [x] **0.3 — Capture three things from the logs:**
  1. The rendered prompt — are the tools present, and in what format? (JSON block? XML? System-prompt text?)
  2. The raw model output — does it contain a `commentary` channel tool call (`<|channel|>commentary to=functions.write_file ...`), or plain text describing the write, or nothing?
  3. What the harness parser produced from that output — a dispatched call, or a phantom "I wrote the file" with no dispatch?
  **Captured 2026-07-02:** (1) tools reach the model via Ollama's builtin gpt-oss (Harmony) renderer — 840 prompt tokens, `truncated=0`; (2) raw output = Harmony `commentary` channel `to=functions.write_file` with correct `{path, content}` args, thinking on `analysis` channel; (3) Ollama's builtin harmony parser (`parser=harmony`) converted it to a structured `tool_calls` array, harness translated to protocol JSON, runner dispatched. **Full pass.**

- [x] **0.4 — Classify the failure** using this fork:

  | What you see in the logs | Root cause | Go to |
  |---|---|---|
  | Model emits `commentary`-channel call, harness doesn't dispatch it | Harness parser doesn't handle Harmony channel format | Phase 1 |
  | Model emits only reasoning / plain text, never a tool call | Harmony format not applied, or JSON-format-forced empty-response bug | Phase 1 + 2 |
  | Tools missing/mangled in the rendered prompt | Schema not reaching model (serialization) | Phase 2 |
  | Everything looks right but write silently no-ops | Downstream write execution, not the model | Phase 3 |

  **Classification (2026-07-02):** historical failures were rows 1+2 with two concrete causes, both now fixed harness-side: (a) brigade omitted the `tools` parameter, so Harmony commentary calls had no reverse-mapping → HTTP 500 / dropped dispatch (fixed: native tools declaration, commit 9505e27); (b) Ollama's default `num_ctx=4096` overflowed on Floor prompts → empty assistant message (fixed: `options.num_ctx=16384` + observation cap, commit 90e84ce). Post-fix Phase 0 run is clean end-to-end; production shows 0 fallbacks across 4 runs / 24 local tool calls.

> **Why this ordering:** `write_file` fails more than `web_search` because it carries the biggest argument payload (file content). A fragile channel-parse mangles the largest, most structured argument first. So a passing `web_search` does *not* mean the tool path is healthy — test with `write_file` specifically.

---

## Phase 1 — Fix the harness parser for Harmony (gpt-oss path)

gpt-oss was trained on OpenAI's **Harmony** response format. Its tool calls arrive on a `commentary` channel, not as a clean `tool_calls` array. If your Ollama parser expects native-style JSON tool calls, it will drop these.

- [x] **1.1 — Confirm the Ollama parser branch** in the harness handles the `commentary` channel + `to=functions.<name>` recipient shape. *(On v0.30.6, Ollama's builtin harmony parser owns this and works when tools are declared; the harness consumes the structured `tool_calls` it produces — `brigade/providers.py` translates them into the agent protocol. Verified in Phase 0.)*
- [x] **1.2 — Prefer XML-style tool calling for gpt-oss.** *(Not needed on v0.30.6 — native `tools` + builtin harmony parser is reliable. Keep XML as a fallback lever only if regressions appear.)*
- [x] **1.3 — List tools explicitly in the system prompt** for gpt-oss, including the schema. *(Brigade does both: tool registry with schemas in the Floor JSON prompt AND native `tools` parameter. The no-tools-param repro showed why the parameter matters: without it the model invents argument names, e.g. `file_path` vs `path`.)*
- [x] **1.4 — Do NOT force a JSON response format on gpt-oss.** *(Brigade never sets `format`; confirmed.)*
- [x] **1.5 — Strip/segregate reasoning tokens** before parsing. *(Ollama routes gpt-oss reasoning to `message.thinking`; the harness reads only `content`/`tool_calls`. Confirmed in Phase 0 raw capture.)*
- [x] **1.6 — Re-run Phase 0.2** and confirm the write now dispatches. *(Dispatches cleanly; also verified in production — real file landed on disk.)*

---

## Phase 2 — Fix the schema-delivery path (applies to Qwen-family too)

Even a capable model fails if the tool schema arrives malformed. This phase matters most once you pull Qwen3:14b (Phase 5), but check it now if Phase 0 showed mangled tools.

- [ ] **2.1 — Verify tool definitions serialize as valid JSON** in the rendered prompt, not as language-native struct dumps. (This was the Qwen3 failure: definitions rendered as Go structs instead of JSON.)
- [ ] **2.2 — Verify tool-call history round-trips.** After a tool call + tool result, send a follow-up turn and confirm the prior assistant tool call is *still present* in the rendered prompt. Some pipelines strip assistant tool calls from history, corrupting multi-step sequences.
- [ ] **2.3 — Verify thinking tags close.** Confirm no unclosed `<think>` blocks in multi-turn prompts (an unclosed think tag corrupts every subsequent turn the model sees).
- [ ] **2.4 — Fallback lever:** if the `tools` parameter path is unreliable for a given backend, embed tools directly in the system prompt and omit the `tools` parameter. This sidesteps a whole class of parameter-serialization bugs.

---

## Phase 3 — Harden the write path itself (defense in depth)

You already added a validation pass that checks whether files the model *claims* to have written actually exist. Keep it and formalize it — it's catching real phantom writes.

- [ ] **3.1 — Keep the post-write validation pass.** After any `write_file`, stat the path and confirm size > 0 (or matches expected content length).
- [ ] **3.2 — On validation failure, retry with a repair prompt**, not a blind re-send. Feed back "the file was not written; re-emit the tool call" so the model corrects the call rather than re-narrating.
- [ ] **3.3 — Cap retries** (2–3) and escalate to the Claude-tier fallback on exhaustion. This is exactly what the fallback tiers are for.
- [ ] **3.4 — Log every phantom write** with the model, the raw output, and the parse result, into the findings doc's failure log. Free labeled data for whichever model you settle on.

---

## Phase 4 — Regression harness (Claude Code / Fable 5 owns this)

Turn the above into automated tests so a fix stays fixed and a model swap is measurable.

- [ ] **4.1 — Build a fixed tool-call test set:** ~20 tasks exercising `web_search`, `list_files`, `read_file`, `write_file`, weighted toward write and toward multi-step (call → result → follow-up).
- [ ] **4.2 — Score = valid dispatched calls / total.** Adopt the 80% threshold: below 80% on a backend, fix the tool descriptions/format *before* blaming the model.
- [ ] **4.3 — Run the set per backend** (gpt-oss, Ornith, Qwen3:14b once pulled) and record scores in the findings doc. Same set, so results are comparable.
- [ ] **4.4 — Wire it as a pre-merge check** so parser changes can't silently regress tool calling.

---

## Phase 5 — Model selection: pull and A/B Qwen3:14b as primary

Qwen3:14b (dense, **not** 3.5) avoids *both* local root causes — it's not gpt-oss's Harmony channel format, and it's not the Qwen3.5 wrong-pipeline mis-wiring. At ~9GB Q4 it fits the 16GB card in MACINTYRE with context headroom (embeddings are on the other GPU).

- [ ] **5.1 — Pull it:** `ollama pull qwen3:14b`
- [ ] **5.2 — `ollama show qwen3:14b`** — confirm `tools` capability and note the context length.
- [ ] **5.3 — `ollama ps` after load** — confirm `size_vram` ≈ model size (no CPU spillover / swapping). Spillover means multi-second first-call latency.
- [ ] **5.4 — Run the Phase 4 test set** against it. Compare score to gpt-oss and Ornith.
- [ ] **5.5 — Decide primary vs. fallback** from the numbers, not vibes. Candidate everyday roles: briefing, rumination, writing, research.
- [ ] **5.6 — Toggle thinking mode off** (`think: false`) for latency-sensitive everyday tasks if the reasoning delay hurts; leave on for research/rumination where it helps.

> **Note on Qwen3.5:** do not reach for `qwen3.5:*` as a quick win. On current Ollama it has been routed through the wrong tool-call pipeline (trained on Qwen3-Coder XML, served as Hermes-style JSON). It may get fixed, but Qwen3:14b is the stable bet today. Track status in the findings doc before revisiting.

---

## Definition of done

- [ ] `write_file` dispatches reliably on the chosen primary model (Phase 4 score ≥ 80%, ideally higher for a 4-tool set).
- [ ] Phantom-write validation + repair-retry + fallback chain in place.
- [ ] Regression test set runs per backend and gates merges.
- [ ] Primary model chosen on measured scores; fallback tiers wired.
- [ ] Findings doc updated with final scores and any new bugs.
