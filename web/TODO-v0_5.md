# v0.5 Proactivity and Expansion

Worker v0.5 research notes for the TODO.md section "Proactivity and Expansion". Scope: no code, only reference-backed implementation notes for OpenBrigade.

## Implementation Status

The first full v0.5 prototype pass is implemented:

- `agent onboard` and `agent validate` create and check explicit workspaces.
- `team create/list/show/assign/chief/delegate` records teams, Crew Chiefs, and bounded delegation.
- `dashboard --plain --view teams` shows team hierarchy.
- `chat ask-agent` and `chat group` persist inter-agent exchanges, usage, and episode summaries.
- `orchestrator propose-stalled-goals` creates idempotent queued work for goals with no active assignment.
- `model route` recommends local/cloud routing from financial and cloud in-flight state.
- `cloud dispatch/list/resolve` creates, inspects, and terminally resolves extended-work cloud-job records.
- `alert audit` raises prototype alerts for goal drift, repeated task failures, failed cloud jobs, and optional datastore health failures.
- `RELEASE_CHECKLIST.md`, README, and PROTOTYPE docs now cover the v0.5 MVP commands.

## Add agent onboarding and bootstrap flow

What this means for OpenBrigade:
- Add a first-class `brigade agent onboard` or `brigade agent init` flow that creates the workspace, writes the required manifest files, registers the agent, and validates role/provider/team defaults before the agent is allowed into the runtime.
- Treat onboarding as a harness operation, not a manual filesystem convention. The operator should not have to hand-build `workspace-{agent}/` and its required files.
- Capture enough metadata during onboarding to support later auth, routing, and team views: `agent_id`, display name, role, workspace path, model/provider defaults, team, and whether the agent is a Crew Chief.

Useful snippets:
- `OpenBrigade_V0.1_Design_Summary.md:46-50` defines Crew Chief as a role on a normal agent rather than a separate entity. The onboarding flow should make that an explicit toggle instead of a hidden convention.
- `OpenBrigade-Concept.md:11-15` sets the brigade metaphor and hierarchy expectations. The onboarding flow should collect the minimum structure needed to reflect that model from day one.
- `OpenBrigade-Concept.md:125-132` describes the structured `HEARTBEAT.md` contract. Onboarding should create a valid starter heartbeat file and any other required workspace files in a consistent shape.

Recommendation:
- Make onboarding non-interactive first, with optional prompts later. A good MVP shape is `brigade agent onboard --id scout --role prototype_research --team discovery`.
- Fail onboarding if required files are missing or the target workspace already contains incompatible state unless the operator explicitly requests repair.

## Add workspace manifest validation and repair guidance

What this means for OpenBrigade:
- Add a validator that checks each managed workspace for the required files and a consistent manifest shape before the runtime loop tries to use it.
- Report missing files, malformed structured blocks, unknown agent IDs, mismatched team assignments, and invalid Crew Chief flags as actionable diagnostics.
- Support a read-only validation mode first, then a guided repair mode for generating missing defaults.

Useful snippets:
- `OpenBrigade_V0.1_Design_Summary.md:131-145` defines the heartbeat contract and where the harness should write task state. Validation should check that contract before scheduling work.
- `reference/hermes-agent/hermes_cli/kanban_diagnostics.py:1-28` shows a useful structured diagnostic pattern with severity, detail, and suggested actions. That fits manifest validation well.
- `reference/proactive-claw-1.2.41/scripts/health_check.py:84-104` shows a practical health-check shape that fails clearly and returns structured status.

Recommendation:
- Expose this as `brigade agent validate` and surface the same results in the dashboard alert view.
- Keep repair explicit; validation should never silently rewrite agent files.

## Add team definitions, membership, and Crew Chief assignment

What this means for OpenBrigade:
- Create first-class team records instead of inferring teams from role names or directory structure.
- Allow an agent to belong to a named team and optionally be marked as the Crew Chief for that team.
- Make team membership the source of truth for later group chat, delegation, and team-scoped status views.

Useful snippets:
- `OpenBrigade-Concept.md:11-15` defines the owner, Crew Chiefs, and line-worker analogy. OpenBrigade should store this explicitly instead of leaving it descriptive only.
- `OpenBrigade_V0.1_Design_Summary.md:46-50` defines Crew Chief authority as a role flag on an agent. Team records should reference that role directly.
- `reference/pixel-agents/server/src/teamProvider.ts:50-57` is a good model for treating membership as structured source-of-truth state rather than inferred text.

Recommendation:
- Add `brigade team create`, `brigade team assign`, and `brigade team show` before attempting team-aware orchestration logic.
- Enforce one Crew Chief per team initially unless there is a strong reason to support co-leads.

## Add team hierarchy display in the dashboard and CLI

What this means for OpenBrigade:
- Show teams and agents as a hierarchy, not just a flat list, in both the TUI and plain CLI output.
- Let operators see who reports to whom, which team owns a task, and whether a team currently has a Crew Chief assigned.
- Keep display read-only before adding deeper hierarchy editing.

Useful snippets:
- `OpenBrigade-Concept.md:168` explicitly calls for a dashboard that shows the hierarchical listing of agents and their current tasks.
- `TODO-v0_4.md:7` already identifies hierarchy as part of the dashboard goal, but current implementation is still mostly flat.
- `reference/openclaw/ui/src/ui/views/agents.ts:220-344` is a useful presenter example for richer agent views fed from precomputed state.

Recommendation:
- Start with a tree view in `brigade dashboard` and a plain `brigade team show` output that is stable for scripts and tests.
- Avoid encoding authority solely in display order; hierarchy display should read from explicit team metadata.

## Add CLI for team creation, assignment, inspection, and membership changes

What this means for OpenBrigade:
- Add operator commands for creating teams, assigning agents to teams, promoting/demoting Crew Chiefs, and inspecting the resulting structure.
- Keep every hierarchy mutation auditable with actor, timestamp, and reason fields where practical.
- Reuse RBAC so owners can change org structure while operators can inspect it and possibly request limited membership changes depending on policy.

Useful snippets:
- `reference/hermes-agent/hermes_cli/kanban.py:299-348` is a good CLI reference for list/show/assign-style verbs with predictable output.
- `reference/openclaw/src/gateway/role-policy.ts:3-22` is a useful central-policy example for deciding which roles may mutate team structure.
- `reference/openclaw/src/gateway/http-auth-utils.ts:141-179` shows the right pattern for enforcing permissions before dispatch.

Recommendation:
- Add `brigade team create`, `brigade team assign`, `brigade team unassign`, `brigade team chief set`, and `brigade team show`.
- Keep `team show` available to observers, but reserve structure mutation for owners until there is a stronger policy reason to widen it.

## Add sync 1:1 inter-agent chat

What this means for OpenBrigade:
- Add a first-class `agent_chat` flow where one agent can ask another agent a bounded question and receive a synchronous response.
- The chat must wake the receiving agent through the OpenBrigade harness, not depend on an already-active terminal/session.
- Store the full transcript in Postgres, summarize it into vector memory with both agent IDs tagged, and return `conversation_id`, summary, status, and any task/goal links to the initiator.
- Enforce loop and timeout controls: max turns, max tokens, max wall time, optional Crew Chief escalation if either agent blocks.

Useful snippets:
- `OpenBrigade-Concept.md:79-85` identifies the failure mode: session-send can deliver and wait, but passive/asleep agents time out. The proposed fix is a new chat session archived to both agents' histories with summary and conversation ID returned.
- `OpenBrigade_V0.1_Design_Summary.md:175-181` says v0.1 must avoid the OpenClaw "asleep agent" issue by making message receipt trigger the agent like a user message.
- `OpenBrigade-Concept.md:159-163` and `migrations/0001_core_state.sql:83-88` are useful storage direction: Redis active state, Postgres durable history, and dispatch transcripts indexed by assignment/agent.
- `reference/pixel-agents/server/src/providers/hook/claude/claude.ts:58-60` is a small status-display example for direct `SendMessage` activity.

No good full code example exists for a clean synchronous 1:1 agent chat protocol. The nearest references are OpenBrigade's own concept notes plus Pixel Agents' event/status normalization.

## Add group chat with a "pass the mic" workflow

What this means for OpenBrigade:
- Add a `group_chat` session model with explicit participants, current speaker, agenda, turn budget, and moderator policy.
- "Pass the mic" should be a serialized turn token, not an open broadcast. Only the current speaker may respond; they must pass to another named agent or back to the moderator.
- Persist every turn with sender, recipient/next_speaker, turn index, and reason. On completion, archive full transcript to each participant history and summarize to vector memory.
- Add anti-chaos controls: max idle turns, duplicate-response suppression, stuck speaker timeout, and Crew Chief takeover.

Useful snippets:
- `OpenBrigade-Concept.md:82-88` documents why naive Telegram-style group chat failed: agents responded only to the user, often all at once, and round-robin handoffs became ambiguous without sender tagging.
- `reference/pixel-agents/server/src/teamProvider.ts:50-57` is useful as a membership source-of-truth pattern. OpenBrigade group chat needs the same idea: active participants should come from a session record, not inferred text.
- `reference/pixel-agents/server/src/teamUtils.ts:3-15` distinguishes inline teammates from independently routed teammates. For OpenBrigade, use the same explicit relationship modeling to avoid routing group chat turns to the wrong agent.
- `reference/pixel-agents/server/src/hookEventHandler.ts:657-724` shows name-based routing for teammate idle/task-completed events with a fallback to all teammates. Useful as a caution: group chat should prefer exact current-speaker routing and treat fallback broadcast as exceptional.

No good code example exists for a pass-the-mic conversation engine. The nearest reference is Pixel Agents' team membership and event routing machinery.

## Add Crew Chief authority flows

What this means for OpenBrigade:
- Model Crew Chief as a role with bounded authority to create tasks, split work, coordinate agents, approve/escalate group chat outcomes, and mediate blocked work.
- Authority decisions should be auditable: record who created/superseded/blocked tasks, why, and which goal/mission justified it.
- User-created tasks remain top priority; Crew Chief decisions should never silently override user assignments.
- Crew Chief can request user approval for high-impact actions, public actions, deletion, spending, scheduling, or external communication.

Useful snippets:
- `OpenBrigade_V0.1_Design_Summary.md:85-90` defines orchestrator authority to create tasks when goals are not progressing, split/parallelize/sequence work, supersede originals, and re-evaluate conditions each cycle.
- `OpenBrigade_V0.1_Design_Summary.md:219-224` explicitly lists task creation authority: User, Crew Chief, Orchestrator, and Agents via Chief, with user-assigned tasks prioritized.
- `OpenBrigade_V0.1_Design_Summary.md:92-95` separates task-state truth from assignment-decision truth. Use that split for Crew Chief: agents report task state; Crew Chief/orchestrator owns assignment decisions.
- `reference/self-improving-proactive-agent-1.0.0/SKILL.md:90-96` is a good authority boundary checklist: ask first for messages/contact, spending, deleting data, public actions, and commitments/scheduling for others.

## Add proactive task creation when goals stall

What this means for OpenBrigade:
- Add a stall detector over goal progress, active assignments, blocked tasks, repeated failures, stale checkpoints, and missing next actions.
- When a goal has no active or recent progressing task, create a proposed task with reason, evidence, owner suggestion, and success criteria.
- Support action levels: auto-create low-risk internal tasks; require Crew Chief or user approval for high-impact/external tasks.
- Store stall decisions in orchestrator reasoning so created tasks are explainable.

Useful snippets:
- `OpenBrigade_V0.1_Design_Summary.md:85-87` says creating new tasks when goals are not progressing is the orchestrator's "biggest function."
- `OpenBrigade_V0.1_Design_Summary.md:73-83` gives the cycle insertion point: load state, resolve blocks/failures, check queues, make assignment decisions, then wake agents.
- `reference/self-improving-proactive-agent-1.0.0/heartbeat-rules.md:5-16` is the closest behavior spec: check stale blockers, active work with no clear next step, and message only when a decision or recommendation is ready.
- `reference/self-improving-proactive-agent-1.0.0/SKILL.md:152-157` reinforces the same heartbeat behavior: re-check follow-ups, review stale blockers, detect missing next moves, and surface prepared recommendations only when useful.

## Add model routing decisions using ABACUS cost and effectiveness data

What this means for OpenBrigade:
- Let ABACUS maintain model/provider cost and effectiveness records from actual OpenBrigade runs: prompt/output tokens, wall time, retry count, task type, quality outcome, failure modes, and dollar cost.
- Add a model router that chooses local/Ollama, cloud LiteLLM provider, or elevated model options based on task risk, expected context, deadline, budget, and historical effectiveness.
- Store routing decisions in orchestrator reasoning: candidate models, selected model, cost estimate, expected effectiveness, and fallback policy.
- Keep scoring/evaluation cheap. Do not use expensive frontier models for routine route scoring unless task risk justifies it.

Useful snippets:
- `OpenBrigade_V0.1_Design_Summary.md:27` names the intended provider layer: LiteLLM for cloud providers and native Ollama for local inference.
- `reference/pixelagent/blueprints/multi_provider/README.md:22-28` explains provider flexibility, failover, benchmarking, and cost optimization by price-performance ratio.
- `reference/pixelagent/blueprints/multi_provider/README.md:282-290` is useful architecture guidance: shared interfaces with provider-specific implementations, enabling hybrid agents and model selection by task complexity.
- `reference/openclaw/src/plugin-sdk/provider-selection-runtime.ts:28-50` is a compact provider-selection pattern: honor explicit config, report missing configured provider, otherwise auto-select by ordered candidates.
- `reference/openclaw/src/plugin-sdk/provider-catalog-shared.ts:98-105` and `reference/openclaw/src/plugin-sdk/provider-catalog-shared.ts:132-140` show model catalog cost metadata normalization. Good shape for ABACUS cost inputs.
- `reference/proactive-claw-1.2.41/SKILL.md:142-150` recommends a small/local scoring model while allowing bigger planning models. Apply this to ABACUS route scoring.

No ABACUS-specific routing implementation exists in the corpus. The nearest references are Pixelagent multi-provider architecture, OpenClaw provider selection/cost metadata, and Proactive Claw's small scoring model guidance.

## Add cloud dispatch for extended work

What this means for OpenBrigade:
- Add a dispatch path for long-running work that should outlive the local orchestrator tick or run on remote/cloud capacity.
- Treat cloud dispatch as an assignment backend: enqueue work, record remote job ID, stream or poll status, archive transcript, and reconcile result into the normal assignment lifecycle.
- Require explicit capability and security boundaries: allowed agents, allowed workspaces, allowed tools, secret handling, max spend/time, cancellation, and transcript capture.
- Make local execution the default. Cloud dispatch should be selected only when work is too long, too expensive locally, needs a remote environment, or the user explicitly asks.

Useful snippets:
- `OpenBrigade_V0.1_Design_Summary.md:80-82` already has a slot for "long-form work queued" and elevated model options.
- `OpenBrigade-Concept.md:159-163` and `migrations/0001_core_state.sql:83-88` establish durable dispatch transcript storage.
- `reference/openclaw/README.md:149-162` is useful for security posture: route channels/accounts to isolated agents and sandbox non-main sessions before exposing remote surfaces.
- `reference/openclaw/SECURITY.md:229-234` is a strong warning for remote execution: nodes extend the operator trust boundary, and approvals are guardrails rather than multi-tenant authorization.
- `reference/openclaw/SECURITY.md:306-308` recommends avoiding public exposure and using SSH/Tailscale-style access for remote operation.

No good code example exists for cloud dispatch in this corpus. The nearest references are OpenBrigade's dispatch transcript schema and OpenClaw's remote/gateway security notes.

## Add user alerting for drift, datastore failure, and repeated task failure

What this means for OpenBrigade:
- Add an alert queue and delivery policy for three classes: behavior drift, datastore failure, and repeated task failure.
- Datastore failure is critical: fail loud, refuse cycle start, alert user with the failed store and remediation hint.
- Repeated task failure should trigger after the design's 5 consecutive failure threshold and mark the task blocked pending human intervention.
- Drift alerts should be evidence-based: rising failure/dismissal rates, slower task completion, repeated blocked tasks, or model routing cost/effectiveness regression.
- Respect quiet hours for non-critical alerts, but never suppress critical datastore/repeated-failure alerts.

Useful snippets:
- `OpenBrigade_V0.1_Design_Summary.md:96-99` defines the repeated-task failure policy: retry, then 5 consecutive failures alerts the user and becomes a block.
- `OpenBrigade_V0.1_Design_Summary.md:293-302` defines datastore-down behavior as fail loud/refuse cycle/alert user, and repeats the 5x failure alert.
- `reference/proactive-claw-1.2.41/scripts/behaviour_report.py:159-193` computes drift alerts from metric changes such as higher dismiss rate, lower prep rate, and rising negative outcomes.
- `reference/proactive-claw-1.2.41/scripts/behaviour_report.py:239-246` wraps drift into a simple `warning`/`ok` alert payload with message.
- `reference/proactive-claw-1.2.41/scripts/daemon.py:100-116` shows alert delivery via configured channels with quiet-hours suppression for non-critical messages.
- `reference/proactive-claw-1.2.41/scripts/daemon.py:140-162` shows OS notification delivery. It is useful as a local-only notification adapter, not as final OpenBrigade design.
- `reference/proactive-claw-1.2.41/scripts/health_check.py:84-104` and `reference/proactive-claw-1.2.41/scripts/health_check.py:167-183` show health/config consistency checks that return structured status.

## Add release checklist, public repo cleanup, and MVP docs

What this means for OpenBrigade:
- Add a release checklist covering artifact preview, excluded-file verification, syntax/test validation, dependency/license review, public README sanity, `.env`/secret scan, and install/run docs.
- Add public repo cleanup before v0.5: remove private notes, generated caches, stray workspaces, secrets, local transcripts, and any reference-derived material lacking license/attribution.
- MVP docs should explain the actual current feature set, setup flow, config, datastores, health checks, first task run, limitations, and safety model.
- Keep docs literal. Do not advertise cloud dispatch, proactive chat, or Crew Chief authority until implemented and validated.

Useful snippets:
- `reference/proactive-claw-1.2.41/RELEASE_CHECKLIST.md:1-16` gives a clean artifact preview and excluded-file verification pattern.
- `reference/proactive-claw-1.2.41/RELEASE_CHECKLIST.md:18-26` validates JSON, Python syntax, and shell scripts before publishing.
- `reference/proactive-claw-1.2.41/CHANGELOG.md:1-7` is a concise release note pattern that mentions convenience, trust, docs, and ops changes.
- `reference/proactive-claw-1.2.41/CHANGELOG.md:19-30` is useful for public cleanup: split core from integrations, exclude heavy/network/cloud helpers, and update docs/config to match.
- `reference/proactive-claw-1.2.41/SECURITY.md:62-70` documents install behavior, network use, privilege level, and optional excluded scripts.
- `reference/proactive-claw-1.2.41/SECURITY.md:103-109` is a good security table pattern for token handling, unauthorized writes, third-party leakage, privilege, dependency supply chain, and retention.
- `reference/proactive-claw-1.2.41/scripts/setup.sh:1-8` gives a public-safe setup posture: no curl/wget, no eval remote code, no sudo/root, and no auto-installing dependencies.
- `reference/proactive-claw-1.2.41/scripts/setup.sh:217-263` shows a `--doctor` readiness check pattern that reports missing config/credentials/dependencies and fails clearly.
