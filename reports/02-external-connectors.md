# 02 — External Connectors

This is the report you asked for in the most detail. Each requested connector is rated
against the "working end-to-end" bar.

> **Reconciled to owner decisions (2026-05-31):** The 🚫 markers below were the pre-decision
> analysis. Final RC stance: **Claude OAuth is deferred** (Claude stays API-key only — a
> README wording fix, not a build); **Google tools are delivered via MCP**, not bespoke;
> **MCP client is the flagship first post-RC milestone**, not an RC blocker. So nothing in
> this report blocks the RC — the action items are documentation + roadmap scheduling. See
> the [punchlist](TODO-PUNCHLIST.md), Section A.

## Summary matrix

| Connector | OpenBrigade | OpenClaw | Hermes | RC verdict |
|---|---|---|---|---|
| **Claude / Anthropic API** | ✅ (LiteLLM) | ✅ | ✅ | ✅ Complete |
| **Claude / Anthropic OAuth** | ❌ | ✅ | ✅ (Claude Code creds) | 🚫 Blocker if "Claude OAuth" is an RC promise |
| **OpenAI API** | ✅ (LiteLLM) | ✅ | ✅ | ✅ Complete |
| **OpenAI OAuth** | ⚠️ manual import only | ✅ | ✅ | ⚠️ Partial |
| **Telegram** | ✅ inbound + outbound | ✅ | ✅ | ✅ Complete |
| **Google Gemini API** | ✅ (LiteLLM) | ✅ | ✅ | ✅ Complete |
| **Google Gemini OAuth** | ⚠️ manual import only | ✅ | ✅ | ⚠️ Partial |
| **Google tools (Drive, Gmail, Calendar)** | ❌ | ⚠️ partial | ⚠️ partial (Feishu instead) | 🚫 Blocker if promised; else defer |
| **MCP servers** | ❌ | ✅ client + server | ✅ client + server | 🚫 Blocker (table-stakes in 2026) |

> Reality check: **both references also lack first-class Gmail/Drive tools.** OpenClaw
> leans on a `himalaya` email skill + Google OAuth pass-through; Hermes ships Microsoft
> Graph and Feishu/Lark instead. So "Google tools" is the *least* standard of your
> requested items — you are not behind the field by omitting it, only behind your own
> stated goal.

## Model-provider auth (Claude / OpenAI / Gemini)

**Implementation:** [`brigade/providers.py`](../brigade/providers.py) `LiteLLMProvider` +
`provider_from_settings`. API-key paths for `openai`, `openai-codex`, `anthropic`,
`gemini`, plus generic LiteLLM and local `ollama`.

**OAuth:** [`brigade/secrets.py`](../brigade/secrets.py) stores/reads/expires OAuth
credentials; CLI flow at [`brigade/cli.py:751`](../brigade/cli.py) (`model auth login
--method oauth`) and `_exchange_oauth_code` at [`cli.py:2047`](../brigade/cli.py).

Two concrete limitations:

1. **OAuth providers are hardcoded to `{openai, openai-codex, gemini}`**
   ([`secrets.py:11`](../brigade/secrets.py) `MODEL_AUTH_PROVIDERS`). **Anthropic/Claude
   is excluded** — Claude is API-key only. Both references support Claude OAuth via the
   Claude Code credential file. If "Claude OAuth" is an RC bullet, this is a **blocker**.
2. **The OAuth flow is a manual code-exchange / token-import**, not an automated
   browser/PKCE/device-code flow. The operator must supply `--client-id`,
   `--redirect-uri`, `--token-url`, or import a token JSON. Functional for a power user,
   rough for an RC. Refresh-token *rotation* is also not implemented (tokens are read and
   checked for expiry but not auto-refreshed — see `LiteLLMProvider._resolved_api_key`,
   which errors on expiry instead of refreshing).

## Telegram

**Complete and the best-finished connector.** [`brigade/connectors.py`](../brigade/connectors.py):
inbound webhook parse (`parse_telegram_update`), live processing with identity approval +
rate limiting + audit (`process_live_connector_message`), and real outbound via the Bot
API (`send_telegram_message`). Wired into the gateway at
[`brigade/web.py:148`](../brigade/web.py) (`/api/connectors/telegram/webhook`).

## Google Chat (bonus, not requested but present)

⚠️ **Partial.** Inbound parse exists (`parse_google_chat_event`) and a webhook route
([`web.py:203`](../brigade/web.py)), but `google_chat_reply_sender`
([`connectors.py:449`](../brigade/connectors.py)) only *builds* a response body — it never
POSTs to the Chat API. Inbound works; outbound is a stub.

## Google Workspace tools (Drive, Gmail, Calendar)

❌ **Absent.** No Gmail, Drive, or Calendar tool, client, or OAuth scope anywhere in
`brigade/`. The model-auth Gemini OAuth shares Google's token endpoint but requests no
Workspace scopes and there is no API client to use them.

## MCP servers

❌ **Absent.** No MCP client and no MCP server in `brigade/` (grep for `mcp` returns only
unrelated matches). This is the most consequential connector gap:

- It is how 2026-era agents consume third-party tools without bespoke code.
- Both references treat MCP as a first-class protocol — **client** (consume external MCP
  servers as tools) *and* **server** (expose themselves to Claude Desktop / IDEs /
  other agents). Hermes' `mcp_serve.py` and OpenClaw's `src/mcp/*` are the models.
- Shipping an RC "agent platform" in 2026 without MCP client support will read as a gap
  to your target audience.

## RC assessment & priority order

🚫 **Blockers (resolve or explicitly scope out before RC):**

1. **MCP client** — consume external MCP servers as tools. Highest leverage; it also
   substitutes for many missing built-in tools (incl. a path to Gmail/Drive via existing
   community MCP servers, which sidesteps building Google tools yourself).
2. **Claude OAuth** — *if* you advertise it. Add `anthropic` to `MODEL_AUTH_PROVIDERS`
   and support the Claude Code credential format. If you instead document "Claude = API
   key," this de-escalates to non-blocker.
3. **Google tools** — *if* you advertise them. Recommended: **drop the bespoke goal and
   reach Gmail/Drive through MCP** once the MCP client lands. Otherwise this is a large
   build for little differentiation (neither reference ships it natively either).

⚠️ **Polish (not blockers, but visible):**

4. Finish Google Chat outbound (real POST) or mark the connector "inbound-only."
5. Automate the OAuth login (browser/device-code) and add refresh-token rotation.
6. Expose OpenBrigade itself as an MCP **server** (post-RC; nice-to-have).
