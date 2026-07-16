"""Natural-language chat with Crew Chiefs (release 1.1).

An operator converses with one persona per thread: a team's Crew Chief
(scoped to that chief's managed agents) or the fleet-wide "front desk"
(the Orchestrator's view). Threads are durable ``Conversation`` records;
their messages live in the ordinary chat log under ``thread:<id>``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import uuid4

from brigade.connectors import ConnectorChatReply, IncomingConnectorMessage
from brigade.prompt_floors import (
    CREW_CHIEF_CHAT_PROMPT,
    CREW_CHIEF_SYSTEM_PROMPT,
    MAX_CHAT_MEMORY_CHARS,
    _managed_agent_ids,
    build_chat_status_context,
    build_crew_chief_load,
    compact_json,
    read_agent_chat_notes,
    write_agent_chat_notes,
)
from brigade.providers import ModelProvider, ModelResponse
from brigade.runner import MAX_OBSERVATION_CHARS, _truncate
from brigade.schemas import (
    Assignment,
    ChatMessage,
    Conversation,
    Team,
    extract_json_object,
)
from brigade.services import (
    _acquire_chat_local_inference_lock,
    _chat_activity_snapshot,
    _classify_chat_confirmation,
    _find_chat_by_idempotency,
    _pending_chat_proposal,
    _release_chat_local_inference_lock,
    _resolve_chat_proposal,
    _stage_chat_proposal,
    _summarize,
    apply_chief_chat_actions,
    lookup_assignment,
)
from brigade.store import StateStore
from brigade.time import parse_utc_iso, utc_now, utc_now_iso
from brigade.tools import (
    ToolRegistry,
    ToolResult,
    ToolSpec,
    _web_fetch,
    native_tool_specs,
)

LOGGER = logging.getLogger("brigade.chief_chat")

FRONT_DESK_PERSONA = "front_desk"
CHIEF_CHAT_KIND_PREFIX = "chief_chat"


class UnknownPersonaError(ValueError):
    """The requested persona does not match front desk or any crew chief."""


@dataclass(frozen=True)
class Persona:
    """A resolved chat persona: front desk, or one team's crew chief."""

    persona_id: str  # "front_desk" or "chief:<agent_id>"
    kind: str  # "front_desk" | "chief"
    display_name: str
    chief_agent_id: str | None = None
    team_id: str | None = None
    managed_agent_ids: frozenset[str] = frozenset()

    @property
    def is_front_desk(self) -> bool:
        return self.kind == FRONT_DESK_PERSONA

    def to_dict(self) -> dict[str, object]:
        return {
            "persona_id": self.persona_id,
            "kind": self.kind,
            "display_name": self.display_name,
            "chief_agent_id": self.chief_agent_id,
            "team_id": self.team_id,
            "managed_agent_ids": sorted(self.managed_agent_ids),
        }


def _front_desk_persona() -> Persona:
    return Persona(
        persona_id=FRONT_DESK_PERSONA,
        kind=FRONT_DESK_PERSONA,
        display_name="Front desk",
    )


def _chief_persona(store: StateStore, teams: list[Team], team: Team) -> Persona:
    chief_id = str(team.crew_chief_id)
    agent = next((item for item in store.agents() if item.agent_id == chief_id), None)
    display = agent.display_name if agent else chief_id
    return Persona(
        persona_id=f"chief:{chief_id}",
        kind="chief",
        display_name=f"{display} ({team.display_name})",
        chief_agent_id=chief_id,
        team_id=team.team_id,
        managed_agent_ids=frozenset(_managed_agent_ids(teams, chief_id)),
    )


def available_personas(store: StateStore) -> list[Persona]:
    """Front desk plus one persona per team that has a crew chief."""
    teams = store.teams()
    personas = [_front_desk_persona()]
    seen_chiefs: set[str] = set()
    for team in teams:
        if not team.crew_chief_id or team.crew_chief_id in seen_chiefs:
            continue
        seen_chiefs.add(team.crew_chief_id)
        personas.append(_chief_persona(store, teams, team))
    return personas


def resolve_persona(
    store: StateStore,
    requested: str | None,
    *,
    default: str = "auto",
) -> Persona:
    """Resolve a persona request to a concrete Persona.

    Accepts ``front_desk``/``frontdesk``, ``chief:<agent_id>``, a bare chief
    agent id, a team id, or a display-name fragment (case-insensitive; used
    by the connector ``/chief`` command). ``None``/``auto`` fall back to the
    configured default: a single-chief fleet talks to that chief, anything
    else to the front desk.
    """
    personas = available_personas(store)
    chiefs = [item for item in personas if item.kind == "chief"]

    normalized = (requested or "").strip().lower()
    if not normalized or normalized == "auto":
        if default != "auto":
            return resolve_persona(store, default, default="auto")
        if len(chiefs) == 1:
            return chiefs[0]
        return personas[0]
    if normalized in {FRONT_DESK_PERSONA, "frontdesk", "front-desk", "orchestrator"}:
        return personas[0]

    target = normalized.removeprefix("chief:")
    for persona in chiefs:
        if target in {
            str(persona.chief_agent_id).lower(),
            str(persona.team_id).lower(),
        }:
            return persona
    # Display-name fragment as a last resort, only when unambiguous.
    fragment_matches = [
        persona for persona in chiefs if target and target in persona.display_name.lower()
    ]
    if len(fragment_matches) == 1:
        return fragment_matches[0]
    raise UnknownPersonaError(
        f"unknown persona: {requested!r}; expected front_desk, a chief agent id, or a team id"
    )


# --- read-only query tools ---------------------------------------------------


@dataclass(frozen=True)
class ChatToolContext:
    """Execution context for chief chat query tools.

    Deliberately not ``tools.ToolContext``: chat turns have no Assignment.
    Scoping lives here — a chief only sees work assigned to managed agents;
    the front desk (``scope_ids() is None``) sees the whole fleet."""

    store: StateStore
    persona: Persona
    operator: str

    def scope_ids(self) -> set[str] | None:
        if self.persona.is_front_desk:
            return None
        return set(self.persona.managed_agent_ids)


def _task_brief(item: Assignment) -> dict[str, Any]:
    return {
        "assignment_id": item.assignment_id,
        "assigned_to": item.assigned_to,
        "status": item.status.value,
        "priority": item.priority.value,
        "kind": item.kind.value,
        "assignment": item.assignment[:200],
        "blockers": item.blockers,
        "awaiting_human": item.awaiting_human,
        "updated_at": item.updated_at,
    }


def _int_arg(arguments: dict[str, Any], name: str, default: int) -> int:
    try:
        return max(1, int(arguments.get(name) or default))
    except (TypeError, ValueError):
        return default


def _tool_list_tasks(context: ChatToolContext, arguments: dict[str, Any]) -> ToolResult:
    scope = context.scope_ids()
    status = str(arguments.get("status") or "").strip().lower()
    agent_id = str(arguments.get("agent_id") or "").strip().lower()
    limit = min(_int_arg(arguments, "limit", 20), 50)
    if agent_id and scope is not None and agent_id not in scope:
        return ToolResult(False, f"agent {agent_id} is not on your team")
    tasks = []
    for item in context.store.assignments():
        if scope is not None and item.assigned_to not in scope:
            continue
        if agent_id and item.assigned_to != agent_id:
            continue
        if status and item.status.value != status:
            continue
        tasks.append(_task_brief(item))
    tasks.sort(key=lambda entry: str(entry["updated_at"]), reverse=True)
    return ToolResult(True, compact_json({"count": len(tasks), "tasks": tasks[:limit]}))


def _tool_search_tasks(context: ChatToolContext, arguments: dict[str, Any]) -> ToolResult:
    query = str(arguments.get("query") or "").strip().lower()
    if not query:
        return ToolResult(False, "search_tasks needs a query")
    include_history = str(arguments.get("include_history") or "").strip().lower() in {
        "true",
        "yes",
        "1",
    }
    scope = context.scope_ids()
    matches = []
    for item in context.store.assignments():
        if scope is not None and item.assigned_to not in scope:
            continue
        haystack = f"{item.assignment} {item.progress_summary or ''}".lower()
        if query in haystack:
            matches.append(_task_brief(item))
    if include_history:
        for entry in context.store.assignment_history():
            record = entry.get("record") or {}
            if scope is not None and record.get("assigned_to") not in scope:
                continue
            haystack = (
                f"{record.get('assignment') or ''} {entry.get('executive_summary') or ''}"
            ).lower()
            if query in haystack:
                matches.append(
                    {
                        "assignment_id": record.get("assignment_id"),
                        "assigned_to": record.get("assigned_to"),
                        "archived": True,
                        "final_status": entry.get("final_status"),
                        "assignment": str(record.get("assignment") or "")[:200],
                        "executive_summary": str(entry.get("executive_summary") or "")[:300],
                    }
                )
    return ToolResult(
        True, compact_json({"count": len(matches), "matches": matches[:20]})
    )


def _tool_get_task(context: ChatToolContext, arguments: dict[str, Any]) -> ToolResult:
    assignment_id = str(arguments.get("assignment_id") or "").strip()
    found = lookup_assignment(context.store, assignment_id)
    if found is None:
        return ToolResult(False, f"unknown assignment: {assignment_id}")
    scope = context.scope_ids()
    if scope is not None and found.get("assigned_to") not in scope:
        return ToolResult(
            False,
            f"assignment {assignment_id} is outside your team "
            f"(assigned to {found.get('assigned_to')})",
        )
    return ToolResult(True, compact_json(found))


def _tool_team_status(context: ChatToolContext, arguments: dict[str, Any]) -> ToolResult:
    if context.persona.is_front_desk:
        payload: dict[str, Any] = {
            "crew_chief_load": build_crew_chief_load(context.store),
            "activity": _chat_activity_snapshot(context.store),
        }
    else:
        payload = build_chat_status_context(
            context.store, str(context.persona.chief_agent_id)
        )
    return ToolResult(True, compact_json(payload))


def _tool_get_goals(context: ChatToolContext, arguments: dict[str, Any]) -> ToolResult:
    scope = context.scope_ids()
    goals: dict[str, list[dict[str, Any]]] = {}
    for agent_id, items in context.store.goals().items():
        if scope is not None and agent_id not in scope:
            continue
        if items:
            goals[agent_id] = [goal.to_dict() for goal in items]
    return ToolResult(True, compact_json(goals or {"goals": "none set"}))


def _tool_get_mission(context: ChatToolContext, arguments: dict[str, Any]) -> ToolResult:
    mission = context.store.mission()
    if mission is None:
        return ToolResult(True, compact_json({"mission": "not set"}))
    return ToolResult(True, compact_json(mission.to_dict()))


def _tool_search_episodes(
    context: ChatToolContext, arguments: dict[str, Any]
) -> ToolResult:
    query = str(arguments.get("query") or "").strip()
    if not query:
        return ToolResult(False, "search_episodes needs a query")
    matches = search_episode_summaries(
        context.store, query, limit=min(_int_arg(arguments, "limit", 3), 10)
    )
    return ToolResult(True, compact_json({"count": len(matches), "episodes": matches}))


def _tool_usage_summary(context: ChatToolContext, arguments: dict[str, Any]) -> ToolResult:
    days = min(_int_arg(arguments, "days", 7), 90)
    cutoff = utc_now() - timedelta(days=days)
    totals: dict[str, dict[str, float]] = {}
    for record in context.store.usage_records():
        recorded_at = record.get("recorded_at")
        try:
            if recorded_at and parse_utc_iso(str(recorded_at)) < cutoff:
                continue
        except ValueError:
            continue
        key = f"{record.get('provider')}:{record.get('model')}"
        bucket = totals.setdefault(
            key, {"calls": 0, "total_tokens": 0, "estimated_cost_usd": 0.0}
        )
        bucket["calls"] += 1
        bucket["total_tokens"] += int(record.get("total_tokens") or 0)
        bucket["estimated_cost_usd"] += float(record.get("estimated_cost_usd") or 0.0)
    for bucket in totals.values():
        bucket["estimated_cost_usd"] = round(bucket["estimated_cost_usd"], 4)
    return ToolResult(True, compact_json({"days": days, "by_model": totals}))


def _tool_list_recurrences(
    context: ChatToolContext, arguments: dict[str, Any]
) -> ToolResult:
    scope = context.scope_ids()
    jobs = []
    for recurrence in context.store.recurrences():
        template = recurrence.get("template") or {}
        target = str(template.get("assigned_to") or "")
        if scope is not None and target not in scope:
            continue
        jobs.append(
            {
                "recurrence_id": recurrence.get("recurrence_id"),
                "enabled": recurrence.get("enabled", True),
                "assigned_to": target,
                "assignment": str(template.get("assignment") or "")[:160],
                "interval_seconds": recurrence.get("interval_seconds"),
                "next_due_at": recurrence.get("next_due_at"),
                "delivers_briefing": isinstance(template.get("deliver_to"), dict),
                "last_materialized_at": recurrence.get("last_materialized_at"),
            }
        )
    return ToolResult(True, compact_json({"count": len(jobs), "scheduled_jobs": jobs}))


def _tool_remember(context: ChatToolContext, arguments: dict[str, Any]) -> ToolResult:
    note = str(arguments.get("note") or "").strip()
    if not note:
        return ToolResult(False, "remember needs a note")
    write_agent_chat_notes(context.store, context.persona.chief_agent_id, note)
    return ToolResult(True, "noted — this will be in your memory on every future turn")


def search_episode_summaries(
    store: StateStore, query: str, limit: int = 3
) -> list[dict[str, Any]]:
    """Uniform episode-search results: Qdrant returns {score, payload} rows,
    the JSON store substring fallback likewise; both stay a compact brief."""
    try:
        raw = store.search_episodes(query, limit=limit)
    except RuntimeError:
        return []
    matches = []
    for item in raw:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else item
        matches.append(
            {
                "summary": str(payload.get("summary") or payload.get("response") or "")[:300],
                "created_at": payload.get("created_at"),
                "conversation_id": payload.get("conversation_id"),
                "agent_id": payload.get("agent_id"),
            }
        )
    return matches


def chief_query_registry(*, include_web_fetch: bool = True) -> ToolRegistry:
    """Read-only query tools for chat turns. Argument descriptions matter:
    ``native_tool_specs`` marks an argument required unless its description
    contains the word "optional"."""
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="list_tasks",
            description=(
                "List live tasks you can see, newest first. Filter by status "
                "(queued/assigned/working/blocked/awaiting_human) or agent."
            ),
            argument_schema={
                "status": "optional status filter",
                "agent_id": "optional agent filter",
                "limit": "optional max results (default 20)",
            },
        ),
        _tool_list_tasks,
    )
    registry.register(
        ToolSpec(
            name="search_tasks",
            description=(
                "Substring-search task text and progress summaries; set "
                "include_history to also search archived/completed work."
            ),
            argument_schema={
                "query": "text to search for",
                "include_history": "optional; 'true' to include archived tasks",
            },
        ),
        _tool_search_tasks,
    )
    registry.register(
        ToolSpec(
            name="get_task",
            description=(
                "Fetch one task by id (short prefixes work), including "
                "archived tasks with their final status and summary."
            ),
            argument_schema={"assignment_id": "the task id or unique prefix"},
        ),
        _tool_get_task,
    )
    registry.register(
        ToolSpec(
            name="team_status",
            description=(
                "Current load, goals, queue depth, blockers, and member "
                "states for your team (fleet-wide at the front desk)."
            ),
            argument_schema={},
        ),
        _tool_team_status,
    )
    registry.register(
        ToolSpec(
            name="get_goals",
            description="Active goals per agent you can see.",
            argument_schema={},
        ),
        _tool_get_goals,
    )
    registry.register(
        ToolSpec(
            name="get_mission",
            description="The fleet mission statement and success criteria.",
            argument_schema={},
        ),
        _tool_get_mission,
    )
    registry.register(
        ToolSpec(
            name="search_episodes",
            description=(
                "Semantic search over past episodes (completed work, prior "
                "conversations) for background on earlier decisions."
            ),
            argument_schema={
                "query": "what to look for",
                "limit": "optional max results (default 3)",
            },
        ),
        _tool_search_episodes,
    )
    registry.register(
        ToolSpec(
            name="usage_summary",
            description="Model usage and estimated cost per provider:model.",
            argument_schema={"days": "optional window in days (default 7)"},
        ),
        _tool_usage_summary,
    )
    registry.register(
        ToolSpec(
            name="list_recurrences",
            description=(
                "List scheduled jobs (recurrences) you can see: what runs, "
                "for whom, how often, and when it fires next."
            ),
            argument_schema={},
        ),
        _tool_list_recurrences,
    )
    if include_web_fetch:
        registry.register(
            ToolSpec(
                name="web_fetch",
                description="Fetch a small HTTP(S) text response for reference.",
                argument_schema={
                    "url": "http or https URL",
                    "max_chars": "optional integer",
                },
            ),
            _web_fetch,
        )
    registry.register(
        ToolSpec(
            name="remember",
            description=(
                "Save a durable note to your curated memory (operator "
                "preferences, standing decisions); it is injected into every "
                "future chat turn. Memory-only — needs no confirmation."
            ),
            argument_schema={"note": "the note to remember"},
        ),
        _tool_remember,
    )
    return registry


# --- reply parsing ------------------------------------------------------------


@dataclass(frozen=True)
class ChiefChatReply:
    kind: str  # "text" | "tool_call" | "actions" | "invalid"
    text: str = ""
    tool_name: str = ""
    tool_arguments: dict[str, Any] = field(default_factory=dict)
    actions: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    reason: str = ""


def parse_chief_chat_reply(text: str) -> ChiefChatReply:
    """One parser for both paths: native tool calls arrive pre-translated to
    the same ``{"status":"tool_call",...}`` JSON by the providers, and
    anything that is not a well-formed protocol object is ordinary prose
    (same fallthrough philosophy as the orchestrator chat parser)."""
    stripped = text.strip()
    candidate = extract_json_object(stripped)
    if not candidate.startswith("{"):
        return ChiefChatReply(kind="text", text=stripped)
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return ChiefChatReply(kind="text", text=stripped)
    if not isinstance(payload, dict):
        return ChiefChatReply(kind="text", text=stripped)
    status = str(payload.get("status") or "").strip().lower()
    if status == "tool_call":
        tool_name = str(payload.get("tool") or payload.get("tool_name") or "").strip()
        arguments = payload.get("arguments") or payload.get("tool_arguments") or {}
        if not tool_name:
            return ChiefChatReply(kind="invalid", reason="tool_call is missing a tool name")
        if not isinstance(arguments, dict):
            return ChiefChatReply(
                kind="invalid", reason="tool_call arguments must be a JSON object"
            )
        return ChiefChatReply(kind="tool_call", tool_name=tool_name, tool_arguments=arguments)
    if status == "propose_actions":
        actions = [item for item in payload.get("actions") or [] if isinstance(item, dict)]
        summary = str(payload.get("summary") or "").strip() or "Proposed action(s)."
        if actions:
            return ChiefChatReply(kind="actions", actions=actions, summary=summary)
        return ChiefChatReply(kind="text", text=summary)
    prose = str(payload.get("summary") or payload.get("response") or "").strip()
    return ChiefChatReply(kind="text", text=prose or stripped)


# --- prompt assembly ----------------------------------------------------------

CHIEF_CHAT_ACTION_DOCS = [
    '{"type":"create_assignment","agent_id":"...","assignment":"...",'
    '"priority":"normal","rationale":"..."}',
    '{"type":"cancel_assignment","assignment_id":"...","reason":"..."}',
    '{"type":"set_priority","assignment_id":"...","priority":"high"}',
    '{"type":"attach_guidance","assignment_id":"...","message":"..."}',
    '{"type":"retry_blocked_assignment","assignment_id":"..."}',
    '{"type":"create_recurrence","agent_id":"...","assignment":"...",'
    '"interval_seconds":86400,"next_due_at":"optional UTC ISO start",'
    '"deliver_briefing":true}'
    " — a scheduled job; interval_seconds 86400 = daily; deliver_briefing "
    "posts each run's finished summary back into this conversation.",
    '{"type":"set_recurrence_enabled","recurrence_id":"...","enabled":false}',
]


def _tool_manifest(registry: ToolRegistry) -> list[str]:
    lines = ["Available tools:"]
    for spec in registry.specs():
        arguments = ", ".join(
            f"{name} ({description})" for name, description in spec.argument_schema.items()
        )
        suffix = f" Args: {arguments}" if arguments else ""
        lines.append(f"- {spec.name}: {spec.description}{suffix}")
    return lines


MAX_SUMMARY_CHARS = 2048


def build_chat_memory(
    store: StateStore,
    *,
    thread: Conversation,
    persona: Persona,
    content: str,
    history_window: int = 12,
    exclude_message_id: str | None = None,
) -> dict[str, Any]:
    """The three memory blocks, each degrading independently to absence:
    curated notes (every turn), the thread's rolling summary + recent
    history (continuity), and episodic recall for the inbound message."""
    memory: dict[str, Any] = {}
    notes = read_agent_chat_notes(store, persona.chief_agent_id)
    if notes.strip():
        memory["curated_notes"] = notes[-MAX_CHAT_MEMORY_CHARS:]
    if thread.rolling_summary.strip():
        memory["conversation_summary"] = thread.rolling_summary[-MAX_SUMMARY_CHARS:]
    history = [
        message
        for message in store.recent_messages(thread.channel, limit=history_window + 1)
        if message.message_id != exclude_message_id
    ][-history_window:]
    if history:
        memory["recent_thread_history"] = [
            {
                "sender": message.sender,
                "kind": message.metadata.get("kind"),
                "content": _truncate(message.content, 400),
            }
            for message in history
        ]
    episodes = search_episode_summaries(store, content, limit=3)
    if episodes:
        memory["possibly_relevant_past_episodes"] = episodes
    return memory


def build_chief_chat_prompt(
    store: StateStore,
    *,
    thread: Conversation,
    persona: Persona,
    operator: str,
    content: str,
    registry: ToolRegistry,
    observations: list[dict[str, Any]],
    pending: dict[str, Any] | None = None,
    memory: dict[str, Any] | None = None,
    demand_final: bool = False,
) -> str:
    if persona.is_front_desk:
        role = (
            "You are the OpenBrigade front desk: the operator's fleet-wide "
            "point of contact, with visibility across every team."
        )
    else:
        role = "\n".join(
            [
                CREW_CHIEF_SYSTEM_PROMPT,
                f"You are {persona.display_name}, and you manage these agents: "
                f"{', '.join(sorted(persona.managed_agent_ids))}. You can only "
                "see and act on your own team's work.",
            ]
        )
    context: dict[str, Any] = {
        "mission": store.mission().statement if store.mission() else "not set",
    }
    if memory:
        context.update(memory)
    if pending:
        context["pending_proposal_awaiting_confirmation"] = {
            "summary": pending.get("summary"),
            "actions": pending.get("actions"),
        }
    if observations:
        context["tool_observations"] = observations
    sections = [
        role,
        "",
        CREW_CHIEF_CHAT_PROMPT,
        "",
        *_tool_manifest(registry),
        "",
        "Allowed actions inside propose_actions:",
        *CHIEF_CHAT_ACTION_DOCS,
        "",
        "Context JSON:",
        compact_json(context),
    ]
    if demand_final:
        sections.extend(
            [
                "",
                "Your tool budget for this turn is exhausted. Reply with your "
                "final answer as plain prose now, using the observations above.",
            ]
        )
    sections.extend(
        [
            "",
            f"Operator {operator} says:",
            content,
        ]
    )
    return "\n".join(sections)


# --- the turn loop ------------------------------------------------------------


def _complete_model_call(
    store: StateStore,
    provider: ModelProvider,
    prompt: str,
    *,
    tools: list[dict[str, Any]],
    holder: str,
) -> ModelResponse:
    """One completion, holding the chat local-inference lock only for the
    duration of the call so agents and other chats interleave between
    iterations."""
    lock_local = getattr(provider, "route_type", "unknown") == "local"
    if lock_local:
        _acquire_chat_local_inference_lock(store, holder)
    try:
        if tools and getattr(provider, "supports_native_tools", False):
            return provider.complete(prompt, tools=tools)
        return provider.complete(prompt)
    finally:
        if lock_local:
            _release_chat_local_inference_lock(store, holder)


def _record_chief_usage(
    store: StateStore, response: ModelResponse, *, channel: str, agent_id: str
) -> None:
    store.add_usage_record(
        {
            "usage_id": str(uuid4()),
            "assignment_id": None,
            "agent_id": agent_id,
            "provider": response.provider,
            "model": response.model,
            "route_type": response.route_type,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "total_tokens": response.input_tokens + response.output_tokens,
            "estimated_cost_usd": response.estimated_cost_usd,
            "recorded_at": utc_now_iso(),
            "conversation_id": channel,
            "source": CHIEF_CHAT_KIND_PREFIX,
        }
    )


def run_chief_chat_turn(
    store: StateStore,
    *,
    thread: Conversation,
    persona: Persona,
    operator: str,
    content: str,
    provider: ModelProvider,
    max_iterations: int = 6,
    history_window: int = 12,
    idempotency_key: str | None = None,
    enable_web_fetch: bool = True,
) -> dict[str, Any]:
    """One operator message -> one chief answer, via a bounded tool loop.

    The chief may call read-only query tools (each iteration is a fresh
    completion with accumulated observations), stage a propose_actions
    envelope for operator confirmation, or answer in prose. Request,
    response, usage, and an episode are persisted like orchestrator chat."""
    channel = thread.channel
    agent_label = persona.chief_agent_id or FRONT_DESK_PERSONA
    if idempotency_key:
        duplicate = _find_chat_by_idempotency(store, idempotency_key)
        if duplicate is not None:
            return {
                "status": "duplicate",
                "conversation_id": duplicate.channel,
                "request_message_id": duplicate.message_id,
                "response_message_id": None,
                "agent_id": agent_label,
            }
    request = ChatMessage(
        channel=channel,
        sender=operator,
        recipient=agent_label,
        content=content,
        metadata={
            "kind": f"{CHIEF_CHAT_KIND_PREFIX}_request",
            "conversation_id": channel,
            "agent_id": agent_label,
            "persona": persona.persona_id,
            "idempotency_key": idempotency_key,
        },
    )
    store.add_message(request)

    pending = _pending_chat_proposal(store, channel, kind_prefix=CHIEF_CHAT_KIND_PREFIX)
    decision = _classify_chat_confirmation(content) if pending else None
    if pending is not None and decision is not None:
        result = _resolve_chat_proposal(
            store,
            pending,
            decision,
            channel=channel,
            sender=operator,
            request=request,
            agent_id=agent_label,
            kind_prefix=CHIEF_CHAT_KIND_PREFIX,
            apply=lambda actions: apply_chief_chat_actions(
                store,
                actions,
                chief_id=persona.chief_agent_id,
                managed_agent_ids=(
                    None if persona.is_front_desk else set(persona.managed_agent_ids)
                ),
                by=operator,
                conversation_channel=channel,
            ),
        )
        store.touch_conversation(thread.thread_id)
        return result

    registry = chief_query_registry(include_web_fetch=enable_web_fetch)
    context = ChatToolContext(store=store, persona=persona, operator=operator)
    tools = native_tool_specs(registry)
    memory = build_chat_memory(
        store,
        thread=thread,
        persona=persona,
        content=content,
        history_window=history_window,
        exclude_message_id=request.message_id,
    )
    observations: list[dict[str, Any]] = []
    tools_used: list[str] = []
    final_text: str | None = None
    budget = max(1, max_iterations)
    response: ModelResponse | None = None
    iterations = 0

    for iteration in range(budget):
        iterations = iteration + 1
        demand_final = iteration == budget - 1 and budget > 1
        prompt = build_chief_chat_prompt(
            store,
            thread=thread,
            persona=persona,
            operator=operator,
            content=content,
            registry=registry,
            observations=observations,
            pending=pending,
            memory=memory,
            demand_final=demand_final,
        )
        try:
            response = _complete_model_call(
                store, provider, prompt, tools=tools, holder=agent_label
            )
        except RuntimeError as exc:
            summary = str(exc)
            store.add_alert(f"chief chat {channel}: {summary}")
            return {
                "status": "blocked",
                "conversation_id": channel,
                "summary": summary,
                "request_message_id": request.message_id,
                "response_message_id": None,
                "agent_id": agent_label,
                "route_type": getattr(provider, "route_type", "unknown"),
            }
        reply = parse_chief_chat_reply(response.text)
        if reply.kind == "actions":
            # _stage_chat_proposal records this completion's usage itself.
            result = _stage_chat_proposal(
                store,
                reply.actions,
                reply.summary,
                channel=channel,
                sender=operator,
                request=request,
                response=response,
                agent_id=agent_label,
                kind_prefix=CHIEF_CHAT_KIND_PREFIX,
            )
            store.touch_conversation(thread.thread_id)
            return {**result, "iterations": iterations, "tools_used": tools_used}
        _record_chief_usage(store, response, channel=channel, agent_id=agent_label)
        if reply.kind == "tool_call" and not demand_final:
            tool_result = registry.execute(reply.tool_name, context, reply.tool_arguments)
            observation = tool_result.to_observation(reply.tool_name)
            observation["output"] = _truncate(
                str(observation["output"]), MAX_OBSERVATION_CHARS
            )
            observations.append(observation)
            tools_used.append(reply.tool_name)
            continue
        if reply.kind == "invalid" and not demand_final:
            observations.append(
                {
                    "tool": "protocol_validation",
                    "ok": False,
                    "output": (
                        f"your last reply was not usable: {reply.reason}. Either "
                        "send a valid tool_call object or answer in plain prose."
                    ),
                    "metadata": {},
                }
            )
            continue
        if reply.kind in {"tool_call", "invalid"}:
            final_text = _budget_exhausted_answer(observations)
        else:
            final_text = reply.text
        break

    if final_text is None:
        final_text = _budget_exhausted_answer(observations)
    if response is None:  # pragma: no cover - budget >= 1 always completes once
        raise RuntimeError("chief chat turn produced no model response")
    response_message = ChatMessage(
        channel=channel,
        sender=agent_label,
        recipient=operator,
        content=final_text,
        metadata={
            "kind": f"{CHIEF_CHAT_KIND_PREFIX}_response",
            "conversation_id": channel,
            "agent_id": agent_label,
            "persona": persona.persona_id,
            "tools_used": tools_used,
            "provider": response.provider,
            "model": response.model,
            "route_type": response.route_type,
        },
    )
    store.add_message(response_message)
    store.add_episode(
        {
            "episode_id": str(uuid4()),
            "agent_id": agent_label,
            "created_at": utc_now_iso(),
            "source": CHIEF_CHAT_KIND_PREFIX,
            "conversation_id": channel,
            "summary": _summarize(final_text),
            "request": content,
            "response": final_text,
            "user": operator,
        }
    )
    store.touch_conversation(thread.thread_id)
    _maybe_refresh_summary(
        store,
        thread=thread,
        provider=provider,
        history_window=history_window,
        agent_label=agent_label,
    )
    return {
        "status": "complete",
        "conversation_id": channel,
        "summary": _summarize(final_text),
        "request_message_id": request.message_id,
        "response_message_id": response_message.message_id,
        "agent_id": agent_label,
        "provider": response.provider,
        "model": response.model,
        "route_type": response.route_type,
        "iterations": iterations,
        "tools_used": tools_used,
    }


def _maybe_refresh_summary(
    store: StateStore,
    *,
    thread: Conversation,
    provider: ModelProvider,
    history_window: int,
    agent_label: str,
) -> None:
    """Roll the thread summary forward once it outgrows the history window.

    One cheap extra completion roughly every half-window of turns (each turn
    appends two messages); any failure is logged and never blocks the turn."""
    channel = thread.channel
    messages = store.messages(channel)
    window = max(1, history_window)
    if len(messages) <= 2 * window or len(messages) % window not in {0, 1}:
        return
    tail = messages[-(4 * window):]
    transcript = "\n".join(
        f"{message.sender}: {_truncate(message.content, 300)}" for message in tail
    )
    prompt = "\n".join(
        [
            "Summarize this operator<->chief conversation in at most 10 short "
            "bullets. Keep task ids, decisions made, and unresolved questions. "
            "Reply with plain-text bullets only.",
            "",
            "Existing summary (fold it in):",
            thread.rolling_summary or "<none>",
            "",
            "Transcript tail:",
            transcript,
        ]
    )
    try:
        response = _complete_model_call(store, provider, prompt, tools=[], holder=agent_label)
        summary = response.text.strip()[:MAX_SUMMARY_CHARS]
        if summary:
            store.set_conversation_summary(thread.thread_id, summary)
            _record_chief_usage(store, response, channel=channel, agent_id=agent_label)
    except Exception:
        LOGGER.warning("chief_chat_summary_refresh_failed", exc_info=True)


# --- connector routing --------------------------------------------------------


@dataclass(frozen=True)
class ControlCommand:
    verb: str  # "frontdesk" | "chief" | "who" | "new"
    argument: str = ""


def parse_control_command(text: str) -> ControlCommand | None:
    """Recognize the four connector control commands. Anything else — including
    an unknown /slash — returns None and is treated as ordinary chat.

    NL persona detection is deliberately out of scope for 1.1: switching
    personas over a connector is commands only."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped[1:].split(maxsplit=1)
    if not parts:
        return None
    verb = parts[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""
    if verb in {"frontdesk", "front_desk", "front-desk"}:
        return ControlCommand("frontdesk")
    if verb == "chief":
        return ControlCommand("chief", argument)
    if verb == "who":
        return ControlCommand("who")
    if verb == "new":
        return ControlCommand("new")
    return None


def _persona_agent_label(persona: Persona) -> str:
    return persona.chief_agent_id or FRONT_DESK_PERSONA


def _current_connector_conversation(
    store: StateStore, username: str, *, default_persona: str
) -> Conversation:
    """The operator's current connector thread is their most-recently-touched
    active conversation, so persona switches (which touch the target) and mobile
    SPA activity on the same thread both steer it. Falls back to a fresh thread
    for the default persona."""
    active = store.conversations(username, status="active")
    if active:
        return active[0]
    persona = resolve_persona(store, None, default=default_persona)
    return store.resolve_active_conversation(
        username,
        persona.persona_id,
        chief_agent_id=persona.chief_agent_id,
        team_id=persona.team_id,
        title=persona.display_name,
    )


def _switch_persona_conversation(
    store: StateStore, username: str, persona: Persona
) -> Conversation:
    conversation = store.resolve_active_conversation(
        username,
        persona.persona_id,
        chief_agent_id=persona.chief_agent_id,
        team_id=persona.team_id,
        title=persona.display_name,
    )
    store.touch_conversation(conversation.thread_id)
    return conversation


def _handle_control_command(
    store: StateStore,
    username: str,
    command: ControlCommand,
    *,
    default_persona: str,
) -> ConnectorChatReply:
    if command.verb == "who":
        conversation = _current_connector_conversation(
            store, username, default_persona=default_persona
        )
        try:
            persona = resolve_persona(store, conversation.persona)
        except UnknownPersonaError:
            persona = resolve_persona(store, None, default=default_persona)
        return ConnectorChatReply(
            f"You're talking to {persona.display_name}.", _persona_agent_label(persona)
        )

    if command.verb == "new":
        conversation = _current_connector_conversation(
            store, username, default_persona=default_persona
        )
        try:
            persona = resolve_persona(store, conversation.persona)
        except UnknownPersonaError:
            persona = resolve_persona(store, None, default=default_persona)
        conversation.status = "archived"
        store.upsert_conversation(conversation)
        _switch_persona_conversation(store, username, persona)
        return ConnectorChatReply(
            f"Started a fresh conversation with {persona.display_name}.",
            _persona_agent_label(persona),
        )

    # frontdesk / chief -> switch persona
    if command.verb == "chief" and not command.argument:
        chiefs = [item for item in available_personas(store) if item.kind == "chief"]
        listing = ", ".join(item.display_name for item in chiefs) or "none configured"
        return ConnectorChatReply(
            f"Available chiefs: {listing}. Use /chief <name> to switch.",
            FRONT_DESK_PERSONA,
        )
    requested = "front_desk" if command.verb == "frontdesk" else command.argument
    try:
        persona = resolve_persona(store, requested)
    except UnknownPersonaError as exc:
        return ConnectorChatReply(f"Sorry — {exc}", FRONT_DESK_PERSONA)
    _switch_persona_conversation(store, username, persona)
    return ConnectorChatReply(
        f"Switched to {persona.display_name}. What do you need?",
        _persona_agent_label(persona),
    )


def _reply_text_from_result(store: StateStore, result: dict[str, Any]) -> str:
    message_id = result.get("response_message_id")
    channel = result.get("conversation_id")
    if message_id and channel:
        for message in reversed(store.messages(str(channel))):
            if message.message_id == message_id:
                return message.content
    return str(result.get("summary") or "…")


def run_connector_chief_chat(
    store: StateStore,
    incoming: IncomingConnectorMessage,
    username: str,
    *,
    provider: ModelProvider,
    default_persona: str = "auto",
    max_iterations: int = 6,
    history_window: int = 12,
    enable_web_fetch: bool = True,
) -> ConnectorChatReply:
    """One connector message -> one chief-chat reply. Handles control commands
    (persona switching, /who, /new) before any model call; otherwise continues
    the operator's current thread."""
    command = parse_control_command(incoming.text)
    if command is not None:
        return _handle_control_command(
            store, username, command, default_persona=default_persona
        )

    conversation = _current_connector_conversation(
        store, username, default_persona=default_persona
    )
    try:
        persona = resolve_persona(store, conversation.persona)
    except UnknownPersonaError:
        persona = resolve_persona(store, None, default=default_persona)
        conversation = _switch_persona_conversation(store, username, persona)
    store.touch_conversation(conversation.thread_id)
    result = run_chief_chat_turn(
        store,
        thread=conversation,
        persona=persona,
        operator=username,
        content=incoming.text,
        provider=provider,
        max_iterations=max_iterations,
        history_window=history_window,
        enable_web_fetch=enable_web_fetch,
        idempotency_key=(
            f"{incoming.provider}:{incoming.external_message_id}"
            if incoming.external_message_id
            else None
        ),
    )
    return ConnectorChatReply(
        _reply_text_from_result(store, result), _persona_agent_label(persona)
    )


def _budget_exhausted_answer(observations: list[dict[str, Any]]) -> str:
    if not observations:
        return (
            "I could not produce an answer within this turn's tool budget. "
            "Please try a more specific question."
        )
    lines = [
        "I ran out of tool budget before finishing, but here is what I found:"
    ]
    for observation in observations:
        lines.append(
            f"- {observation.get('tool')}: "
            f"{_truncate(str(observation.get('output') or ''), 400)}"
        )
    return "\n".join(lines)
