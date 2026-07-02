# OpenBrigade Harness — Tool-Call Findings, Bugs & Notes

**Purpose:** The durable "why" behind the runbook. Findings, bug receipts, and running notes so this survives across sessions (with or without Claude Code).
**Companion doc:** `harness-tool-reliability-runbook.md` (the what-to-do).

---

## The one-paragraph summary

The harness has *two different* local tool-call problems with *two different* root causes, plus a downstream write-execution question. **gpt-oss:20b** (the primary offender) fails because it was trained on OpenAI's **Harmony** channel format and emits tool calls on a `commentary` channel that a native-JSON parser drops. **Qwen3.5** (not installed, noted for the record) fails because current Ollama routes it through the *wrong tool-call pipeline* entirely. **Qwen3:14b** (dense, not installed yet) avoids both and is the pull-and-test primary candidate. Because the harness is server-agnostic with per-backend parsers, most of these fixes live in code Tom controls — which is the good news.

---

## Environment snapshot (this session)

- **Ollama:** v0.30.6 (current stable). Past every version-pin workaround in the old bug threads (those topped out ~v0.17.x). If failures persist here, the cause is format/parser, not stale Ollama.
- **Box:** MACINTYRE. RTX 4070 Ti Super **16GB** = model inference. RTX 2060 Super = embeddings (dedicated).
- **Harness:** server-agnostic abstraction layer; parsers already written for Ollama, openai-codex, and Claude. Claude Code (Fable 5) does most coding + nearly all automated testing.

### Local model inventory (by pull date)

| Model | Pulled | Size | Notes |
|---|---|---|---|
| Ornith-1.0-9B | 2026-06-27 | 5.6 GB | current agent model; decent at lightweight tasks |
| nemotron-mini | 2026-06-09 | 2.7 GB | maintenance/heartbeat class |
| gpt-oss:20b-rumination | 2026-03-30 | 13.8 GB | rumination variant |
| nomic-embed-text | 2026-03-12 | 0.3 GB | embeddings |
| qwen2.5-coder:7b | 2026-03-05 | 4.7 GB | only Qwen currently on box |
| gpt-oss:20b | 2026-02-23 | 13.8 GB | **primary offender for tool-call failures** |
| devstral-small-2 | 2026-02-03 | 15.2 GB | coding |

> No Qwen3 (dense or 3.5) is installed. Qwen3:14b is a *planned pull*, not a current asset.

---

## Finding 1 — gpt-oss:20b: Harmony channel-format mismatch (PRIMARY)

**Symptom set (matches what Tom reported):** blank/empty responses, "planning text but no dispatched action," malformed tool calls, and write-file failing far more than read/search.

**Root cause:** Per OpenAI's own model card, gpt-oss "should only be used with the harmony format as it will not work correctly otherwise." Tool calls come out shaped like:
`<|start|>assistant<|channel|>commentary to=functions.write_file json<|message|>{...}`
— on a `commentary` channel, not as a native `tool_calls` array. If Ollama's harmony parser or the harness parser doesn't reverse-map that channel + `to=functions.<name>` recipient, the call is silently dropped and you get reasoning-only / empty content.

**Corroboration:**
- OpenAI model card + `openai/harmony`: gpt-oss trained on Harmony; must use it.
- Ollama #11991: `harmony parser: no reverse mapping found for function name` → the model tried to call a tool, parser failed, client got an empty-assistant-message error.
- Ollama #11867: supplying a `format` to gpt-oss returns **empty response** — repeatable.
- Community (HF gpt-oss-20b discussion #80): reliable fixes = (a) **XML-style tool calling**, (b) **list tools explicitly in system prompt with schema**, (c) **do NOT require JSON response format** (causes empty content), (d) strip reasoning tokens before parsing.

**Why write_file specifically:** biggest, most-structured argument payload (file content), so it's the first thing a fragile channel-parse corrupts. A passing `web_search` doesn't clear the tool path.

**Fix path:** Runbook Phase 1.

**STATUS 2026-07-02 — RESOLVED at the harness layer, verified by Phase 0.** Two compounding root causes, both fixed in brigade:
1. Brigade sent `/api/chat` with **no `tools` parameter** while prompting for tool use. gpt-oss emitted Harmony commentary-channel calls anyway; with no declared functions Ollama's reverse-map fails → HTTP 500 "error parsing tool call" or empty content. Fixed by declaring registry tools natively + translating returned `tool_calls` back into the protocol (commit 9505e27). On v0.30.6 Ollama's builtin harmony parser handles the channel format correctly *when tools are declared* — no harness-side Harmony parsing needed.
2. Ollama's default **num_ctx=4096** silently overflowed on brigade's Floor prompts → reasoning-only/empty output. Fixed with `options.num_ctx=16384` (env `BRIGADE_OLLAMA_NUM_CTX`) + per-observation 4000-char cap (commit 90e84ce).

Post-fix production evidence: 4 consecutive agent runs, 24 local tool calls, 0 fallbacks; gpt-oss dispatched a real `write_file` that landed on disk (Articles_of_Incorporation.md).

---

## Finding 2 — Qwen3.5: wrong-pipeline mis-wiring (NOT installed; for the record)

**Root cause:** Current Ollama routes Qwen3.5 tool calls through the Qwen3 Hermes-style **JSON** pipeline, but Qwen3.5 was trained on the Qwen3-Coder **XML** format (`<function=name><parameter=key>value</parameter></function>`). Renderer, format instruction, history rendering, and output parser are all wrong for the model.

**Corroboration:**
- Ollama #14493: enumerates 6 concrete mismatches; correct pipeline (Qwen3CoderRenderer + Qwen3CoderParser) exists in the codebase but is wired to "qwen3-coder" instead of "qwen3.5." Parser side partially fixed in v0.17.3; renderer side noted still broken at time of report.
- Ollama #14745: qwen3.5:9b prints tool call as text instead of executing; workaround was pinning to v0.17.5.
- Ollama #14492: qwen3.5:35b `unexpected end of JSON input` 500s across context sizes; qwen3:30b did *not* reproduce.
- ZeroClaw #3079: third-party harness reproduced the exact symptom set — but **direct Ollama API calls with the same schema returned valid tool_calls**. Confirms the model is capable; the pipeline/harness path is where it breaks.

**Implication:** Don't reach for qwen3.5 as a quick win. Re-check status before ever revisiting (see Open Questions).

---

## Finding 3 — Qwen3 (dense) schema-serialization bug (relevant once Qwen3:14b is pulled)

**Root cause:** Ollama #14601 — when tools are passed via the `/api/chat` tools parameter, Qwen3 tool definitions get serialized as **Go structs rather than valid JSON**, and assistant tool calls get **stripped from conversation history**. Both bugs are *absent* when tools are embedded directly in the system prompt with the tools parameter omitted.

**Status vs. our Ollama:** Reported against earlier builds; we're on v0.30.6. Treat as "verify, don't assume." The Phase 2 checks (JSON-valid tools, history round-trip, system-prompt fallback) exist to catch this if it's still live for the dense model.

---

## Finding 4 — The write path itself (downstream of the model)

Independent of format: Tom already added a validation pass because the model would report success on writes that didn't land. That pass is doing real work and stays. Formalized in Runbook Phase 3 as validate → repair-retry → fallback, with phantom writes logged as labeled data.

---

## Why the harness being server-agnostic is the win

Every root cause above lives in the format/parser boundary — the exact layer the harness abstracts. Parsers already exist for Ollama, openai-codex, and Claude. So "fix tool calling" mostly means "make the Ollama-side parser match the format each model was actually trained on," which is Tom/Fable-5 code, not waiting on upstream. The Claude tiers remain the fallback when a local parse exhausts retries.

---

## Failure log (append as tests run)

> Format: date | model | task | raw output shape | parser result | classification

- 2026-07-02 | gpt-oss:20b | write_file "hello" (tools declared, num_ctx 16384) | Harmony `commentary` channel → `to=functions.write_file` | Ollama builtin harmony parser produced structured `tool_calls`; harness translated to protocol JSON; runner parsed tool=write_file args={path, content} | **PASS end-to-end** (debug receipt: `harmonyparser.go:298 header={Role:assistant Channel:commentary Recipient:functions.write_file}` → `routes.go:2814 parser=harmony toolCalls=[write_file]`)
- 2026-07-02 | gpt-oss:20b | write_file "hello" (tools **omitted** — old harness behavior) | protocol JSON in `content`, `tool_calls=null`, argument name **wrong** (`file_path` vs `path`) | parseable but schema-blind | **repro of old failure mode**: without the `tools` param the model freelances; on longer prompts it instead emits commentary-channel calls Ollama can't reverse-map → HTTP 500 "error parsing tool call" (observed live 2026-07-02 13:18) or empty content
- 2026-07-02 | Ornith-1.0-9B | write_file "hello" (tools declared) | protocol JSON in `content`, `tool_calls=null` | parsed directly by runner | **PASS** — resolves the open question: Ornith emits **plain-text protocol JSON (no Harmony, no XML)** and sometimes native `tool_calls` (seen in earlier bench runs); harness handles both shapes
- 2026-07-02 | gpt-oss:20b + Ornith-9B | production cycles pre-fix | n/a | empty assistant message → cloud fallback | **second root cause found**: Ollama default `num_ctx=4096`; brigade Floor prompts exceed it (llama-server 400: "request (4286 tokens) exceeds the available context size (4096)"). Fixed: `options.num_ctx=16384` (commit 90e84ce) + observation cap MAX_OBSERVATION_CHARS=4000

---

## Backend scoreboard (fill from Runbook Phase 4)

| Backend | Test set | Valid-dispatch % | Notes |
|---|---|---|---|
| gpt-oss:20b | v1 (20 tasks) | — | pre-fix baseline, then post-Phase-1 |
| Ornith-1.0-9B | v1 | — | current agent model |
| qwen3:14b | v1 | — | once pulled (Phase 5) |
| gpt-oss:20b | quick-6 (2026-07-02, post-fix) | 100% (6/6) | 3× native write_file (Harmony→tool_calls) + 3× protocol JSON; 13.8GB spills past 12GB-class VRAM budgets when co-resident |
| Ornith-1.0-9B | quick-6 (2026-07-02) | 100% (6/6) | plain-text protocol JSON shape; 5.9GB incl. 16k ctx fully in VRAM; capability ceiling on complex multi-file tasks (loops without write_file) |
| qwen3:14b | quick-6 (2026-07-02) | 100% (6/6) | native tool_calls with schema-perfect args (incl. typed `append: false`); thinking segregated; history round-trip clean; 12.0GB 100% VRAM on the 16GB card; warm latency 2.5–7.6s |

---

## Open questions / to re-verify

- [x] Is Finding 3 (Qwen3 Go-struct serialization) still live on v0.30.6, or patched? **Behaviorally absent 2026-07-02:** qwen3:14b produced 3/3 native tool calls with schema-conformant, correctly-typed arguments via the `/api/chat` tools parameter, and the multi-turn history round-trip (assistant tool call + tool result + follow-up) answered with correct context — neither bug reproduces. (v0.30.6's qwen3 path emits no rendered-prompt dump, so verification is behavioral rather than byte-level.)
- [ ] Has the Qwen3.5 wrong-pipeline routing (#14493) been fixed in a build ≥ our version? Re-check before ever considering qwen3.5.
- [x] Does Ornith use a native `tool_calls` shape, Harmony, or XML? **Resolved 2026-07-02:** plain-text protocol JSON in `content` (sometimes native `tool_calls`); no Harmony, no XML. Does not share gpt-oss's failure mode; both shapes handled by the harness.
- [ ] Learning-loop / effectiveness scoring for autonomous task selection remains an unresolved design problem (flagged separately from this reliability work).
