# v0.4 Interfaces and Multi-User

Source section: `TODO.md` v0.4, "Interfaces and Multi-User". Scope: no code; research notes and implementation TODO detail for OpenBrigade.

## Build the planned TUI dashboard

- Build `brigade dashboard` as the first real operator surface, matching the concept doc's "Full Screen TUI" and dashboard goal: show the agent hierarchy, each agent's current task, and let the operator drill in for details.
- Useful snippets:
  - `reference/hermes-agent/ui-tui/src/entry.tsx:15` checks for a real TTY and exits cleanly when absent. Use the same guard so `brigade dashboard` behaves predictably in CI, pipes, and systemd.
  - `reference/hermes-agent/ui-tui/src/entry.tsx:20` through `reference/hermes-agent/ui-tui/src/entry.tsx:28` resets terminal modes and clears stale screen/scrollback before rendering. This is useful because long-running dashboards crash or get killed; OpenBrigade should not leave the user's terminal in a broken state.
  - `reference/hermes-agent/ui-tui/src/entry.tsx:37` through `reference/hermes-agent/ui-tui/src/entry.tsx:54` wires graceful cleanup on errors/signals. Reuse the pattern for stopping Redis/Postgres polling loops and restoring terminal modes.
  - `reference/hermes-agent/ui-tui/src/entry.tsx:72` through `reference/hermes-agent/ui-tui/src/entry.tsx:99` lazy-loads the Ink app, renders it with a gateway client, and handles hyperlink clicks. This is a good model if OpenBrigade chooses Textual/Rich instead of Ink: keep transport startup separate from render startup, and keep terminal lifecycle code at the entrypoint.
  - `reference/openclaw/docs/cli/dashboard.md:20` through `reference/openclaw/docs/cli/dashboard.md:28` is useful for auth hygiene: dashboard launch should avoid printing/copying bearer tokens once OpenBrigade adds web/API auth.
- Recommendation: implement the TUI as a read-heavy dashboard first, polling OpenBrigade's existing state APIs/storage with explicit refresh intervals. Keep write actions behind clear operator commands until RBAC is enforced.

## Add mission, goal, task, agent, and alert views

- Mission view: show the brigade-wide mission, current orchestrator cycle state, latest orchestrator reasoning summary, and recent mission-impacting changes. The design summary says mission is global and only the orchestrator sees it every cycle, so the dashboard should expose it only to owner/operator roles.
- Goal view: show per-agent goals, alignment status, last update, and whether current tasks advance the goal.
- Task view: show queue state, assignee, dependencies, progress summary, failures, blocks, provenance, and archival links.
- Agent view: show workspace, identity files, current task, heartbeat status, model/tool permissions, and recent status reports.
- Alert view: show actionable blocks, repeated task failures, stale agents, ingestion failures, auth/config issues, and operator-required decisions.
- Useful snippets:
  - `reference/openclaw/src/gateway/protocol/schema/tasks.ts:15` through `reference/openclaw/src/gateway/protocol/schema/tasks.ts:40` defines a compact `TaskSummary` with status, agent, session, owner, timing, summaries, and errors. OpenBrigade task cards should have the same compact summary shape before deeper drill-down.
  - `reference/openclaw/src/gateway/server-methods/tasks.ts:62` through `reference/openclaw/src/gateway/server-methods/tasks.ts:88` maps internal task records to a dashboard-safe summary. This is useful because OpenBrigade should not expose raw Redis/Postgres rows directly.
  - `reference/openclaw/docs/automation/tasks.md:23` through `reference/openclaw/docs/automation/tasks.md:38` frames tasks as an activity ledger, not a scheduler. That distinction maps well to OpenBrigade: orchestrator/heartbeat decide when work runs; the dashboard explains what happened.
  - `reference/openclaw/docs/automation/tasks.md:119` through `reference/openclaw/docs/automation/tasks.md:145` gives a lifecycle and terminal-state semantics that are close to OpenBrigade's `queued/running/completed/failed/blocked/abandoned` needs.
  - `reference/openclaw/ui/src/ui/views/agents.ts:123` through `reference/openclaw/ui/src/ui/views/agents.ts:147` derives selected agent, default agent, and tab counts before rendering. Use the same presenter pattern for TUI panels: compute view models once, then render simple panels.
  - `reference/openclaw/ui/src/ui/views/agents.ts:220` through `reference/openclaw/ui/src/ui/views/agents.ts:344` is a concrete multi-panel agent view: overview, files, tools, skills, channels, cron. OpenBrigade can adapt this to overview, identity/workspace, current task, memory, tools, and heartbeat.
  - `reference/hermes-agent/hermes_cli/kanban_diagnostics.py:1` through `reference/hermes-agent/hermes_cli/kanban_diagnostics.py:28` defines alerts as structured diagnostics with severity, detail, suggested actions, and auto-clearing behavior. This is the best alert model in the corpus.
  - `reference/hermes-agent/hermes_cli/kanban_diagnostics.py:38` through `reference/hermes-agent/hermes_cli/kanban_diagnostics.py:108` shows the actual diagnostic/action data shape. Use this for OpenBrigade alerts so UI buttons and CLI hints can share one source.
- No strong reference exists for mission/goal-specific views. Nearest references are OpenClaw's agent/task views and Hermes' Kanban diagnostics. OpenBrigade should define its own mission/goal view models from the design summary.

## Add interactive task creation and assignment inspection

- Add TUI/CLI flows for creating a user-priority task, selecting/confirming assignee, setting priority, attaching a goal, declaring dependencies, and inspecting why an assignment was made.
- Assignment inspection should answer: who created this task, why this agent, what goal/mission it advances, what dependencies/blockers exist, and what evidence/status updates produced the current state.
- Useful snippets:
  - `reference/hermes-agent/hermes_cli/kanban.py:260` through `reference/hermes-agent/hermes_cli/kanban.py:297` is a rich task creation surface: title, body, assignee, parents, workspace mode, tenant, priority, idempotency key, runtime cap, creator, skills, retries, JSON output. This is the closest match to OpenBrigade's future `brigade task create`.
  - `reference/hermes-agent/hermes_cli/kanban.py:299` through `reference/hermes-agent/hermes_cli/kanban.py:348` covers list/show/assign/reclaim/reassign. These are the minimum inspection and intervention verbs for OpenBrigade v0.4.
  - `reference/hermes-agent/hermes_cli/curses_ui.py:35` through `reference/hermes-agent/hermes_cli/curses_ui.py:53` provides a reusable curses checklist contract with selected defaults and live status text. Good fit for choosing dependencies, candidate agents, or knowledge sources.
  - `reference/hermes-agent/hermes_cli/curses_ui.py:57` through `reference/hermes-agent/hermes_cli/curses_ui.py:162` includes non-TTY safety and numbered fallback. OpenBrigade should keep every interactive flow scriptable with `--json`/flags.
  - `reference/openclaw/src/gateway/server-methods/tasks.ts:99` through `reference/openclaw/src/gateway/server-methods/tasks.ts:120` filters tasks by session and agent. Assignment inspection should support the same filters plus goal/mission/provenance filters.
- Recommendation: every interactive task mutation should also have a non-interactive CLI equivalent and should stamp `created_by_user_id`, `created_by_role`, idempotency key, and UTC timestamps.

## Add JWT auth scaffolding for future web/API use

- OpenBrigade's design summary explicitly calls for JWT sessions/API and owner/operator/observer RBAC. v0.4 should add scaffolding even if the TUI remains local-first: config schema, signing key loading, token issue/verify helpers, user identity claims, role claim, expiry, key id, and audit logging.
- No good first-party JWT implementation example exists in the reference corpus. There are many bearer-token and OAuth examples, but not a clean app-local JWT auth layer to copy.
- Nearest useful snippets:
  - `reference/openclaw/src/gateway/http-auth-utils.ts:29` through `reference/openclaw/src/gateway/http-auth-utils.ts:35` extracts bearer tokens from `Authorization`. Reuse this request parsing shape for JWT bearer auth.
  - `reference/openclaw/src/gateway/http-auth-utils.ts:103` through `reference/openclaw/src/gateway/http-auth-utils.ts:139` separates auth checking from request handling and returns a structured request auth object. OpenBrigade should do the same so web, API, and TUI gateway paths share one verifier.
  - `reference/openclaw/src/gateway/auth.ts:36` through `reference/openclaw/src/gateway/auth.ts:52` shows a compact `GatewayAuthResult` with `ok`, method, user, reason, and rate-limit metadata. Model OpenBrigade's JWT verifier result similarly.
  - `reference/openclaw/docs/gateway/trusted-proxy-auth.md:32` through `reference/openclaw/docs/gateway/trusted-proxy-auth.md:49` documents the trust chain explicitly. Useful as a reminder to document JWT trust boundaries, key rotation, and proxy identity assumptions.
- Recommendation: use PyJWT or `python-jose` only behind a small internal interface. Claims should include `sub`, `username`, `role`, `iat`, `exp`, `jti`, and optional `tenant`/`scope`. Do not bake provider OAuth tokens into OpenBrigade JWTs.

## Enforce owner/operator/observer permissions beyond data modeling

- Implement authorization checks at every mutation/read boundary, not only in DB tables. Design roles:
  - Owner: full control, including mission/goals/agents/users/auth config.
  - Operator: chat, create tasks, inspect assignments, ingest knowledge, resolve alerts.
  - Observer: read-only dashboard and chat visibility allowed by policy; no mutations.
- Useful snippets:
  - `reference/openclaw/src/gateway/role-policy.ts:3` through `reference/openclaw/src/gateway/role-policy.ts:22` is a tiny central role policy. OpenBrigade should keep role-to-method decisions similarly centralized, but with `owner/operator/observer`.
  - `reference/openclaw/src/gateway/http-auth-utils.ts:141` through `reference/openclaw/src/gateway/http-auth-utils.ts:179` checks scopes before dispatch and returns a forbidden response when missing. This is the right enforcement location for web/API routes.
  - `reference/openclaw/src/gateway/http-auth-utils.ts:188` through `reference/openclaw/src/gateway/http-auth-utils.ts:214` refuses to trust self-declared scopes for shared-secret bearer requests. For OpenBrigade, do not let clients self-assert roles; roles must come from verified JWT claims or server-side user records.
  - `reference/openclaw/docs/concepts/session.md:23` through `reference/openclaw/docs/concepts/session.md:54` warns that shared DM sessions leak user context in multi-user setups. This is directly relevant to observer/operator chat visibility and user identity injection.
  - `reference/openclaw/docs/concepts/multi-agent.md:21` through `reference/openclaw/docs/concepts/multi-agent.md:38` explains per-agent auth/session isolation and credential-copying risks. OpenBrigade should apply the same isolation to user-owned sessions and agent workspaces.
- Recommendation: create a permission matrix before implementing endpoints. Include negative tests: observer cannot create tasks/upload knowledge/edit mission; operator cannot manage users/auth; owner can do all; unauthenticated requests fail closed.

## Add a web upload endpoint or minimal local upload command for knowledge

- v0.4 should provide at least one ingestion path for operator-supplied knowledge: a local command such as `brigade knowledge upload path.md` or a small authenticated web/API upload. Since v0.3 already plans Markdown/text ingestion first, this should call the ingestion pipeline rather than invent a separate store.
- Useful snippets:
  - `reference/mempalace/mempalace/cli.py:5` through `reference/mempalace/mempalace/cli.py:28` shows a simple local ingestion UX: project mining and conversation mining share one palace/search surface. OpenBrigade can mirror this with `brigade knowledge ingest <path> --scope mission|agent`.
  - `reference/mempalace/mempalace/cli.py:68` through `reference/mempalace/mempalace/cli.py:99` routes CLI arguments to project or conversation mining while preserving dry-run, limit, agent, and override flags. Useful for keeping local upload scriptable and testable.
  - `reference/mempalace/mempalace/miner.py:22` through `reference/mempalace/mempalace/miner.py:43` whitelists readable file extensions. OpenBrigade v0.4 should start with `.md`/`.txt` and explicit size limits.
  - `reference/mempalace/mempalace/miner.py:54` through `reference/mempalace/mempalace/miner.py:58` defines chunk size, overlap, minimum chunk, and max file size. Useful defaults for a conservative first ingestion path.
  - `reference/openclaw/src/gateway/server-methods/skills-upload-store.ts:11` through `reference/openclaw/src/gateway/server-methods/skills-upload-store.ts:20` sets TTLs, max chunk size, active upload cap, idempotency length, SHA pattern, upload ID pattern, and base64 pattern. Use the same kind of limits for web knowledge uploads.
  - `reference/openclaw/src/gateway/server-methods/skills-upload-store.ts:98` through `reference/openclaw/src/gateway/server-methods/skills-upload-store.ts:158` validates SHA, upload ID, size, slug, offset, and idempotency key. Useful for upload hardening.
  - `reference/openclaw/src/gateway/server-methods/skills-upload.ts:73` through `reference/openclaw/src/gateway/server-methods/skills-upload.ts:115` separates begin/chunk/commit handlers and rejects uploads unless enabled by config. OpenBrigade can adapt this for `knowledge.upload.begin/chunk/commit`.
  - `reference/openclaw/packages/memory-host-sdk/src/host/batch-upload.ts:14` through `reference/openclaw/packages/memory-host-sdk/src/host/batch-upload.ts:31` shows multipart upload of generated JSONL. Useful if OpenBrigade later batches embeddings or document chunks.
- Recommendation: implement the local upload command first unless web auth is ready. For web upload, require JWT operator/owner role, file extension allowlist, byte limit, SHA-256, idempotency key, and audit record.

## Add user identity injection into chat/session context

- The design summary says 1:1 user chat injects user identity into the system prompt at session start, while mission remains global and only reporting ownership is per-user. v0.4 should make that explicit in session metadata and prompt assembly.
- Useful snippets:
  - `reference/openclaw/ui/src/ui/user-identity.ts:8` through `reference/openclaw/ui/src/ui/user-identity.ts:15` defines bounded local user identity fields. OpenBrigade should bound display name/avatar-like fields and sanitize them before prompt injection.
  - `reference/openclaw/ui/src/ui/user-identity.ts:31` through `reference/openclaw/ui/src/ui/user-identity.ts:45` normalizes identity and detects whether identity exists. Use the same approach before adding identity context.
  - `reference/mempalace/README.md:623` through `reference/mempalace/README.md:626` treats a plain identity file as Layer 0 loaded every session. This is the best conceptual match for "who is the user?" context.
  - `reference/openclaw/src/agents/pi-embedded-runner/run/attempt.spawn-workspace.context-injection.test.ts:116` through `reference/openclaw/src/agents/pi-embedded-runner/run/attempt.spawn-workspace.context-injection.test.ts:141` forwards `senderId` and `senderIsOwner` into action-discovery input. OpenBrigade should pass verified `user_id`, `username`, and role into chat/session context, not just UI display text.
  - `reference/openclaw/docs/concepts/session.md:23` through `reference/openclaw/docs/concepts/session.md:54` explains why multi-user DM/session isolation matters. Identity injection without isolation can leak Alice's context to Bob.
  - `reference/openclaw/src/agents/pi-embedded-runner/run/attempt.spawn-workspace.context-injection.test.ts:48` through `reference/openclaw/src/agents/pi-embedded-runner/run/attempt.spawn-workspace.context-injection.test.ts:78` tests when context injection is skipped or included. Use this pattern to decide whether user identity is injected once per session or refreshed every turn.
- Recommendation: store identity in session metadata as verified server-side data, not as user-supplied prompt text. Inject a short block such as `Current user: username=<...>, role=<...>, user_id=<...>` with escaping/sanitization, and include tests for session reset, role change, and multi-user isolation.
