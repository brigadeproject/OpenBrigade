# 01 — Agent Tool Use

## What OpenBrigade has

A real, working tool-use loop. Agents emit a JSON `tool_call` action, the runner
executes it, feeds the observation back, and loops up to a budget.

- Registry + dispatch: [`brigade/tools.py`](../brigade/tools.py) — `ToolRegistry`,
  `ToolSpec`, `ToolContext`, `ToolResult`.
- Agent loop: [`brigade/runner.py:316`](../brigade/runner.py) `_complete_assignment_with_tools`,
  bounded by `MAX_AGENT_ITERATIONS = 6`.
- Protocol: [`brigade/runner.py:362`](../brigade/runner.py) `build_assignment_prompt` —
  `{"status":"tool_call","tool":"...","arguments":{}}`.

**Built-in tools** (`default_tool_registry`):

| Tool | Purpose | Notes |
|------|---------|-------|
| `list_files` | list workspace files | sandboxed to agent workspace |
| `read_file` | read text file | 12k char truncation |
| `write_file` | write/append | sandboxed |
| `shell` | run argv command | no shell interpreter, 30s cap |
| `web_fetch` | HTTP(S) GET | 12k char cap |
| `delegate` | queue an assignment for another agent | inter-agent, not sub-agent spawn |

Strengths worth keeping: workspace path-escape guard
([`tools.py:151`](../brigade/tools.py) `_safe_workspace_path`), argv-only shell (no
injection surface), per-tool error capture, transcript of every tool observation
([`runner.py:741`](../brigade/runner.py)).

## What the references have

- **OpenClaw**: `ToolDescriptor` framework with availability guards; tools shipped
  both embedded and as standalone MCP servers; tool execution under a permission/sandbox
  model; 100+ skills layered on top.
- **Hermes**: self-registering tool modules (`tools/registry.py`), composable
  **toolsets** (`toolsets.py`) and per-platform **toolset distributions**
  (`toolset_distributions.py`); ~80 built-in tools (browser, vision, kanban, TTS, etc.);
  check-function gating (a tool appears only when its credential/env is present).

## Gap analysis

| Capability | OpenBrigade | OpenClaw | Hermes | Verdict |
|---|---|---|---|---|
| Tool-call loop | ✅ | ✅ | ✅ | ✅ Complete |
| File/shell/web tools | ✅ | ✅ | ✅ | ✅ Complete |
| Path/shell sandboxing | ✅ | ✅ | ✅ | ✅ Complete |
| Tool gating by capability | ❌ (all tools always on) | ✅ | ✅ | ⚠️ Partial |
| Composable toolsets / per-role tool grants | ❌ | ✅ | ✅ | ⚠️ Partial |
| MCP tools (consume external) | ❌ | ✅ | ✅ | ❌ Missing |
| Rich tools (browser, vision, image gen) | ❌ | ✅ | ✅ | ❌ Missing (likely out of RC scope) |

## RC assessment

**Not a blocker on its own.** The core loop is sound and the six built-ins are enough
to demonstrate the product. Two things are worth doing before RC because they are cheap
and visibly raise quality:

1. **Capability gating** — hide/disable a tool when its prerequisite is absent (mirrors
   Hermes check-functions). Trivial to add to `ToolRegistry`.
2. **Per-role / per-team tool grants** — the data model already has `role`,
   `team_id`, and `delegation_policy`; wiring a tool allow-list per role closes the gap
   with the references and matches the RBAC story already in `brigade/rbac.py`.

MCP tool **consumption** is the one strategically important miss — see report
[02 — External Connectors](02-external-connectors.md), where MCP is tracked as a
connector-tier blocker.
