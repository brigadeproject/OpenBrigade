# 03 — GUI & TUI Applications

This is OpenBrigade's strongest area relative to the references. Both surfaces exist and
are wired to the same backend.

## GUI (web)

- **Backend:** [`brigade/web.py`](../brigade/web.py) — FastAPI app (`create_app`), titled
  "OpenBrigade Gateway." ~40 routes covering auth, dashboard, cockpit, ops-room (+ SSE
  event stream), models, mission, goals, agents, teams, tasks, chat, users, connector
  webhooks, and health.
- **Frontend:** [`web/src/main.tsx`](../web/src/main.tsx) — a substantial React 19 + Vite
  single-page app (~2,980 lines). Served as the SPA at `GET /` from `web.py`.
- **Notable surfaces:** `/api/cockpit`, `/api/ops-room` + `/api/ops-room/events` (SSE) and
  a persisted `/api/ops-room/layout` — i.e., a live operations view, not just a static
  dashboard. `/api/chat/ask-orchestrator` and `ask-orchestrator-markdown` expose the
  orchestrator conversationally.

**Verdict:** ✅ Complete and runnable. Auth (`brigade/auth.py`) and RBAC
(`brigade/rbac.py`) are wired into the routes.

## TUI

- **Implementation:** [`brigade/tui.py`](../brigade/tui.py) — `curses`-based dashboard with
  views `("mission", "goals", "tasks", "agents", "teams", "alerts")` plus a chat mode with
  slash-commands (`parse_chat_tui_command`).
- Renders the same store-derived payloads as the GUI (`build_dashboard_payload`).

**Verdict:** ✅ Complete for a dashboard/chat TUI.

## Comparison to references

| Aspect | OpenBrigade | OpenClaw | Hermes |
|---|---|---|---|
| Web UI | ✅ React 19 SPA + FastAPI | ✅ Lit web components + Node gateway | ✅ React 19 + Vite + xterm.js |
| TUI | ✅ curses | ✅ pi-tui (rich) | ✅ Ink (React-for-terminal) |
| Desktop app | ❌ | ❌ | ❌ |
| Mobile apps | ❌ | ✅ iOS/Android/macOS | ❌ |
| i18n | ❌ | ✅ | partial |
| Live streaming to UI | ✅ SSE (ops-room) | ✅ WS | ✅ WS |

## Gap analysis

- **Parity reached** on the two surfaces you asked about (web GUI + TUI). You are not
  behind Hermes here; you trail OpenClaw only on native mobile apps and i18n, neither of
  which you listed as RC goals.
- The references' TUIs are richer (streaming tool output inline, autocomplete, model
  override from the TUI). Your curses TUI is a dashboard+chat, not a full interactive agent
  console. That is a *polish* delta, not a *completeness* delta.

## RC assessment

**Not a blocker.** GUI and TUI both ship and are wired end-to-end. Recommended pre-RC
polish only:

1. Confirm `web/dist` build is produced and served (the SPA must be built for a clean
   install; verify `vite build` output is packaged or built on first run).
2. Smoke-test the ops-room SSE stream against a live orchestrator daemon so the "proactive"
   story is visible in the GUI — this is your headline differentiator and the GUI should
   show it moving.
3. (Optional, post-RC) richer TUI: inline tool-call streaming and a `/model` override.
