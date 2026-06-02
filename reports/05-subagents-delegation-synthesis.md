# 05 — Sub-agents, Delegation & Goal/Task Synthesis

Covers three of your stated concerns:
- Orchestrator/Crew Chiefs synthesizing goals and tasks for delegation.
- Agents spinning up sub-agents.

## Delegation between existing agents — ✅ works

- **Agent-initiated:** the `delegate` tool ([`brigade/tools.py:248`](../brigade/tools.py))
  lets a working agent create a queued assignment for **another registered agent**, with
  `parent_assignment_id` set for lineage.
- **Orchestrator-initiated:** `create_assignment` / `rebalance_queued_assignment`
  escalation actions ([`brigade/orchestrator.py:413`](../brigade/orchestrator.py)).
- **Crew-chief authorization:** `_chief_authorized_for_agent`
  ([`brigade/cli.py:2514`](../brigade/cli.py)) enforces that a chief may direct agents on
  its own team; `Team.delegation_policy` defaults to `chief_only`
  ([`brigade/schemas.py:256`](../brigade/schemas.py)).

So a chief *can* fan work out to its team, and lineage is tracked via
`parent_assignment_id`. This satisfies "delegation."

## Dynamic sub-agent spawning — ❌ missing

Critical distinction: **delegation targets agents that already exist.** There is no path to
*create a new agent at runtime*. Agents are created via the CLI/admin
([`cli.py:2285`](../brigade/cli.py) `model_provider=agent.model_provider`) — a human
provisions them. An agent cannot say "spawn a researcher sub-agent for this subtask."

Compare:
- **OpenClaw:** `src/agents/acp-spawn.ts`, `subagent-spawn-plan.ts`, depth/breadth limits
  in `agent-limits.ts` — true dynamic child sessions with inherited permissions and their
  own model/auth.
- **Hermes:** `tools/delegate_tool.py` spawns an isolated child `AIAgent` via a thread pool
  with a fresh context and a restricted toolset (recursion guard: child can't re-delegate).

Your `delegate` is closest to a *task hand-off*, not a *child-agent spawn*. For RC this is
acceptable **if positioned as "delegate to a fixed roster."** If the product promise is
"agents spin up sub-agents," it is a **blocker**.

## Goal/task synthesis for delegation — ⚠️ partial

What exists:
- The orchestrator **synthesizes assignments** from goals/mission for idle agents
  (`build_idle_agent_assignments`) and from stalled goals (`propose-stalled-goals`).
- A crew chief is handed a *meta-assignment*: "Build the next concrete task plan for the
  current mission and identify which agent should execute each step."

What is missing:
- There is **no structured decomposition primitive**. The chief produces a plan as free
  text in its transcript; turning that plan into child assignments depends on the chief
  agent voluntarily emitting `delegate` tool calls. Nothing parses a plan into a task graph,
  and nothing guarantees the steps become tracked assignments with dependencies.
- `dependency_ids` exists on `Assignment` and the orchestrator honors it, but **no code
  path populates dependencies during synthesis** — they can only be set by a human/admin at
  creation. So synthesized multi-step plans don't get dependency wiring automatically.

This is the weakest link in the "Crew Chief synthesizes goals and tasks" story: the
*mechanism to ask* exists; the *mechanism to reliably produce a structured, dependency-
linked task breakdown* does not.

## Gap analysis

| Capability | OpenBrigade | OpenClaw | Hermes | Verdict |
|---|---|---|---|---|
| Delegate to existing agent | ✅ | ✅ | ✅ | ✅ Complete |
| Lineage (`parent_assignment_id`) | ✅ | ✅ | ✅ | ✅ Complete |
| Chief→team authorization | ✅ | ✅ | ✅ | ✅ Complete |
| Spawn **new** sub-agent at runtime | ❌ | ✅ | ✅ | ❌ Missing |
| Recursion guard on delegation | ⚠️ none | ✅ | ✅ | ⚠️ Partial |
| Structured plan → child tasks | ❌ | ✅ | ✅ (kanban) | ❌ Missing |
| Auto dependency wiring on synthesis | ❌ | ✅ | ✅ | ❌ Missing |

## RC assessment

> **Owner decision (2026-05-31):** dynamic sub-agent spawning is acknowledged as the only
> genuine capability gap and is **scoped to v1.1** — not an RC blocker. RC ships with
> delegation across a fixed agent roster.

- **Sub-agent spawning (→ v1.1):** For RC, keep the fixed roster and document it as such.
  Add the cheap **`delegate` recursion/fan-out guard** (cap depth and children per parent via
  the `parent_assignment_id` chain) so delegation can't explode. True dynamic spawn lands in
  v1.1.
- **Goal/task synthesis:** Recommended pre-RC — add a structured **`decompose`/
  `create_subtasks` orchestrator action (or chief tool)** that takes a plan and emits N
  child assignments with `dependency_ids` populated. This converts the current "ask the
  chief to think" into "the chief produces tracked, ordered work," which is what your design
  doc (`agent-system-design-v1.3.md`) actually promises. Without it, the delegation story
  works in a demo but is fragile in practice.
