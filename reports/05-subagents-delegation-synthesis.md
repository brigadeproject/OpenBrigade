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
- **Structured subtasks:** the `create_subtasks` tool creates multiple queued child
  assignments with `parent_assignment_id`, optional goal text, dependency links, and the same
  delegation guard used by `delegate`.

So a chief *can* fan work out to its team, and lineage is tracked via
`parent_assignment_id`. This satisfies "delegation" and gives Crew Chiefs a structured way
to turn plans into tracked work.

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

## Goal/task synthesis for delegation — ✅ fixed-roster path works

What exists:
- The orchestrator **synthesizes assignments** from goals/mission for idle agents
  (`build_idle_agent_assignments`) and from stalled goals (`propose-stalled-goals`).
- A crew chief is handed a *meta-assignment*: "Build the next concrete task plan for the
  current mission and identify which agent should execute each step."
- The chief can call `create_subtasks` to emit a bounded batch of child assignments.
  `depends_on_previous` wires ordered dependency chains, and tests cover the dependency-linked
  child creation path.

What remains:
- The chief model must still choose the tool and supply a good task breakdown. OpenBrigade
  does not yet parse arbitrary prose plans into tasks automatically.

This is strong enough for the fixed-roster RC story: the mechanism to ask exists, and the
mechanism to produce bounded, dependency-linked child work exists. The remaining risk is
model/tool-use quality, not missing task-graph plumbing.

## Gap analysis

| Capability | OpenBrigade | OpenClaw | Hermes | Verdict |
|---|---|---|---|---|
| Delegate to existing agent | ✅ | ✅ | ✅ | ✅ Complete |
| Lineage (`parent_assignment_id`) | ✅ | ✅ | ✅ | ✅ Complete |
| Chief→team authorization | ✅ | ✅ | ✅ | ✅ Complete |
| Spawn **new** sub-agent at runtime | ❌ | ✅ | ✅ | ❌ Missing |
| Recursion guard on delegation | ✅ | ✅ | ✅ | ✅ Complete |
| Structured plan → child tasks | ✅ tool-mediated | ✅ | ✅ (kanban) | ✅ Complete for fixed roster |
| Auto dependency wiring on synthesis | ✅ via `create_subtasks` | ✅ | ✅ | ✅ Complete for fixed roster |

## RC assessment

> **Owner decision (2026-05-31):** dynamic sub-agent spawning is acknowledged as the only
> genuine capability gap and is **scoped to v1.1** — not an RC blocker. RC ships with
> delegation across a fixed agent roster.

- **Sub-agent spawning (→ v1.1):** For RC, keep the fixed roster and document it as such.
  The fixed-roster path now has delegation depth/fan-out guardrails. True dynamic spawn lands
  in v1.1.
- **Goal/task synthesis:** The pre-RC `create_subtasks` fix has landed for fixed-roster
  task graphs. Continue improving prompts and demos so chiefs reliably choose the tool and
  produce useful decomposition.
