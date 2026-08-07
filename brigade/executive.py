"""User-owned Executive concierge chat for release 1.3.

Executives are not mission workers. They are per-user assistant agents that can
inspect Brigade state, stage user-confirmed Brigade mutations, and keep their
own memory without entering the normal heartbeat assignment runner.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from brigade.chief_chat import (
    ChiefChatReply,
    _complete_model_call,
    _tool_manifest,
    parse_chief_chat_reply,
    search_episode_summaries,
)
from brigade.connectors import ConnectorChatReply, IncomingConnectorMessage
from brigade.knowledge import ingest_text, store_ingest_result
from brigade.memory import (
    build_memory_entry,
    explicit_memory_request,
    record_memory_mutation,
)
from brigade.providers import ModelProvider, ModelResponse
from brigade.runner import MAX_OBSERVATION_CHARS, _truncate
from brigade.schemas import (
    AGENT_ROLE_EXECUTIVE,
    Agent,
    Assignment,
    ChatMessage,
    Conversation,
    Goal,
    Priority,
)
from brigade.services import (
    _classify_chat_confirmation,
    _find_chat_by_idempotency,
    _pending_chat_proposal,
    _record_operator_event,
    _resolve_chat_proposal,
    _stage_chat_proposal,
    _summarize,
    attach_operator_guidance,
    lookup_assignment,
)
from brigade.store import StateStore
from brigade.time import utc_now_iso
from brigade.tools import ToolRegistry, ToolResult, ToolSpec, _web_fetch, native_tool_specs

EXECUTIVE_CHAT_KIND_PREFIX = "executive_chat"


class UnknownExecutiveError(ValueError):
    """The requested Executive persona does not exist or is not owned by user."""


@dataclass(frozen=True)
class ExecutivePersona:
    persona_id: str
    agent_id: str
    display_name: str
    owner_username: str
    kind: str = "executive"

    def to_dict(self) -> dict[str, object]:
        return {
            "persona_id": self.persona_id,
            "kind": self.kind,
            "agent_id": self.agent_id,
            "display_name": self.display_name,
            "owner_username": self.owner_username,
        }


@dataclass(frozen=True)
class ExecutiveToolContext:
    store: StateStore
    persona: ExecutivePersona
    operator: str
    user_message: str = ""
    request_message_id: str | None = None
    conversation_id: str | None = None

    @property
    def agent(self) -> Agent:
        found = next(
            (
                item
                for item in self.store.agents()
                if item.agent_id == self.persona.agent_id
            ),
            None,
        )
        if found is None:
            raise ValueError(f"unknown executive agent: {self.persona.agent_id}")
        return found


@dataclass(frozen=True)
class ExecutiveActionResult:
    applied: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "applied": self.applied,
            "rejected": self.rejected,
            "skipped": self.skipped,
        }


def executive_agents_for_user(store: StateStore, username: str) -> list[Agent]:
    return [
        agent
        for agent in store.agents()
        if agent.role == AGENT_ROLE_EXECUTIVE and agent.owner_username == username
    ]


def available_executive_personas(store: StateStore, username: str) -> list[ExecutivePersona]:
    return [
        ExecutivePersona(
            persona_id=f"executive:{agent.agent_id}",
            agent_id=agent.agent_id,
            display_name=agent.display_name,
            owner_username=username,
        )
        for agent in executive_agents_for_user(store, username)
    ]


def resolve_executive_persona(
    store: StateStore, username: str, requested: str | None = None
) -> ExecutivePersona:
    personas = available_executive_personas(store, username)
    if not personas:
        raise UnknownExecutiveError(f"no Executive is configured for {username}")
    normalized = str(requested or "").strip().lower()
    if not normalized or normalized == "executive":
        if len(personas) == 1:
            return personas[0]
        raise UnknownExecutiveError("multiple Executives are configured; specify executive:<id>")
    target = normalized.removeprefix("executive:")
    for persona in personas:
        if target in {persona.agent_id.lower(), persona.persona_id.lower()}:
            return persona
    matches = [item for item in personas if target in item.display_name.lower()]
    if len(matches) == 1:
        return matches[0]
    raise UnknownExecutiveError(f"unknown Executive persona: {requested!r}")


def _compact_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _record_executive_usage(
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
            "source": EXECUTIVE_CHAT_KIND_PREFIX,
        }
    )


def _tool_brigade_status(context: ExecutiveToolContext, arguments: dict[str, Any]) -> ToolResult:
    del arguments
    payload = {
        "mission": context.store.mission().to_dict() if context.store.mission() else None,
        "agents": [agent.to_dict() for agent in context.store.agents()],
        "teams": [team.to_dict() for team in context.store.teams()],
        "assignments": [item.to_dict() for item in context.store.assignments()],
        "alerts": context.store.alerts()[-20:],
        "recurrences": context.store.recurrences(),
    }
    return ToolResult(True, _compact_json(payload))


def _tool_get_goals(context: ExecutiveToolContext, arguments: dict[str, Any]) -> ToolResult:
    agent_id = str(arguments.get("agent_id") or "").strip()
    goals = context.store.goals(agent_id or None)
    payload = {
        key: [goal.to_dict() for goal in values]
        for key, values in goals.items()
        if values
    }
    return ToolResult(True, _compact_json(payload or {"goals": "none set"}))


def _tool_get_task(context: ExecutiveToolContext, arguments: dict[str, Any]) -> ToolResult:
    assignment_id = str(arguments.get("assignment_id") or "").strip()
    found = lookup_assignment(context.store, assignment_id)
    if found is None:
        return ToolResult(False, f"unknown assignment: {assignment_id}")
    return ToolResult(True, _compact_json(found))


def _tool_search_knowledge(context: ExecutiveToolContext, arguments: dict[str, Any]) -> ToolResult:
    query = str(arguments.get("query") or "").strip().lower()
    if not query:
        return ToolResult(False, "search_knowledge needs a query")
    matches = []
    for document in context.store.knowledge_documents():
        haystack = f"{document.get('title') or ''} {document.get('source') or ''}".lower()
        if query in haystack:
            matches.append(document)
    for episode in search_episode_summaries(context.store, query, limit=5):
        matches.append({"kind": "episode", **episode})
    return ToolResult(True, _compact_json({"count": len(matches), "matches": matches[:20]}))


def _tool_web_fetch(context: ExecutiveToolContext, arguments: dict[str, Any]) -> ToolResult:
    del context
    return _web_fetch(None, arguments)


def _tool_remember(context: ExecutiveToolContext, arguments: dict[str, Any]) -> ToolResult:
    note = str(arguments.get("note") or "").strip()
    if not note:
        return ToolResult(False, "remember needs a note")
    if not explicit_memory_request(context.user_message):
        return ToolResult(
            False,
            (
                "durable memory requires explicit operator wording such as "
                "'remember that ...' or a governed memory update"
            ),
        )
    workspace = context.store.data_dir / context.agent.workspace_path
    workspace.mkdir(parents=True, exist_ok=True)
    memory = workspace / "MEMORY.md"
    entry_id, line, metadata = build_memory_entry(
        note,
        source="user_stated",
        author=context.operator,
        conversation_id=context.conversation_id,
        message_id=context.request_message_id,
    )
    with memory.open("a", encoding="utf-8") as handle:
        handle.write(f"\n{line}\n")
    record_memory_mutation(
        context.store,
        agent_id=context.agent.agent_id,
        memory_path=memory,
        note=note,
        metadata=metadata,
    )
    return ToolResult(
        True,
        "remembered explicit operator memory",
        {"entry_id": entry_id, "source": "user_stated", "status": "active"},
    )


def executive_query_registry(*, include_web_fetch: bool = True) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="brigade_status",
            description="Read the current Brigade mission, agents, tasks, alerts, and schedules.",
            argument_schema={},
        ),
        _tool_brigade_status,
    )
    registry.register(
        ToolSpec(
            name="get_goals",
            description="Read active goals, optionally for one agent.",
            argument_schema={"agent_id": "optional agent id"},
        ),
        _tool_get_goals,
    )
    registry.register(
        ToolSpec(
            name="get_task",
            description="Fetch one task by id or unique prefix.",
            argument_schema={"assignment_id": "task id or unique prefix"},
        ),
        _tool_get_task,
    )
    registry.register(
        ToolSpec(
            name="search_knowledge",
            description="Search knowledge documents and prior episodes by text.",
            argument_schema={"query": "search text"},
        ),
        _tool_search_knowledge,
    )
    if include_web_fetch:
        registry.register(
            ToolSpec(
                name="web_fetch",
                description="Fetch a small public HTTP(S) text response for reference.",
                argument_schema={
                    "url": "http or https URL",
                    "max_chars": "optional integer",
                },
            ),
            _tool_web_fetch,
        )
    registry.register(
        ToolSpec(
            name="remember",
            description=(
                "Save a durable Executive memory note only when the operator "
                "explicitly asks you to remember/save/note it."
            ),
            argument_schema={"note": "memory note"},
        ),
        _tool_remember,
    )
    return registry


EXECUTIVE_ACTION_DOCS = [
    '{"type":"create_task","agent_id":"...","assignment":"...",'
    '"priority":"normal","goal_statement":"optional"}',
    '{"type":"create_goal","agent_id":"...","statement":"...",'
    '"success_criteria":["..."],"explicitly_not":["..."]}',
    '{"type":"attach_guidance","assignment_id":"...","message":"..."}',
    '{"type":"ingest_text","title":"...","source":"...","document_type":"note","content":"..."}',
]


def build_executive_prompt(
    store: StateStore,
    *,
    persona: ExecutivePersona,
    operator: str,
    content: str,
    registry: ToolRegistry,
    observations: list[dict[str, Any]],
    pending: dict[str, Any] | None = None,
    demand_final: bool = False,
) -> str:
    context: dict[str, Any] = {
        "operator": operator,
        "executive_agent": persona.to_dict(),
        "mission": store.mission().statement if store.mission() else "not set",
    }
    if pending:
        context["pending_proposal_awaiting_confirmation"] = {
            "summary": pending.get("summary"),
            "actions": pending.get("actions"),
        }
    if observations:
        context["tool_observations"] = observations
    sections = [
        "You are the user's Executive: a personal assistant and concierge to OpenBrigade.",
        "You are user-driven, chatty, direct, and helpful. You do not perform mission work.",
        (
            "Use tools to inspect Brigade state and answer accurately. For state "
            "changes, propose actions."
        ),
        "",
        *_tool_manifest(registry),
        "",
        "Allowed actions inside propose_actions:",
        *EXECUTIVE_ACTION_DOCS,
        "",
        "Context JSON:",
        _compact_json(context),
    ]
    if demand_final:
        sections.extend(
            [
                "",
                "Your tool budget for this turn is exhausted. Reply in plain prose now.",
            ]
        )
    sections.extend(["", f"User {operator} says:", content])
    return "\n".join(sections)


def apply_executive_actions(
    store: StateStore,
    actions: list[dict[str, Any]],
    *,
    persona: ExecutivePersona,
    by: str,
) -> dict[str, list[dict[str, Any]]]:
    result = ExecutiveActionResult()
    for action in actions:
        action_type = str(action.get("type") or "").strip()
        try:
            if action_type == "create_task":
                applied = _apply_create_task(store, action, persona=persona, by=by)
            elif action_type == "create_goal":
                applied = _apply_create_goal(store, action, persona=persona, by=by)
            elif action_type == "attach_guidance":
                applied = _apply_attach_guidance(store, action, persona=persona, by=by)
            elif action_type == "ingest_text":
                applied = _apply_ingest_text(store, action, persona=persona, by=by)
            else:
                raise ValueError(f"unsupported executive action type: {action_type or '<missing>'}")
            result.applied.append(applied)
            _record_operator_event(
                store,
                action=f"executive_chat_{action_type}",
                summary=f"{action_type} applied by Executive {persona.agent_id} for {by}",
                assignment_id=str(
                    applied.get("assignment_id") or applied.get("document_id") or ""
                ),
                agent_id=persona.agent_id,
                by=by,
                payload={"executive": persona.to_dict(), "action": action},
            )
        except ValueError as exc:
            result.rejected.append({"action": action, "reason": str(exc)})
    return result.to_dict()


def _resolve_known_agent(store: StateStore, agent_id: str) -> Agent:
    found = next((item for item in store.agents() if item.agent_id == agent_id), None)
    if found is None:
        raise ValueError(f"unknown agent: {agent_id}")
    if found.role == AGENT_ROLE_EXECUTIVE:
        raise ValueError("Executive agents do not receive mission tasks")
    return found


def _apply_create_task(
    store: StateStore, action: dict[str, Any], *, persona: ExecutivePersona, by: str
) -> dict[str, Any]:
    agent_id = str(action.get("agent_id") or "").strip()
    assignment_text = str(action.get("assignment") or "").strip()
    if not agent_id:
        raise ValueError("create_task is missing agent_id")
    if not assignment_text:
        raise ValueError("create_task is missing assignment")
    target = _resolve_known_agent(store, agent_id)
    try:
        priority = Priority(str(action.get("priority") or Priority.NORMAL.value))
    except ValueError as exc:
        raise ValueError(f"unsupported priority: {action.get('priority')}") from exc
    assignment = Assignment(
        assignment=assignment_text,
        assigned_to=target.agent_id,
        created_by=persona.agent_id,
        source="executive_chat",
        priority=priority,
        goal_statement=(
            str(action.get("goal_statement")).strip()
            if action.get("goal_statement") is not None
            else None
        ),
        assignment_rationale="User-confirmed Executive action.",
        created_by_user_id=by,
        created_by_role="executive",
    )
    persisted = store.add_assignment(assignment)
    return {
        "type": "create_task",
        "assignment_id": persisted.assignment_id,
        "agent_id": target.agent_id,
        "status": persisted.status.value,
    }


def _apply_create_goal(
    store: StateStore, action: dict[str, Any], *, persona: ExecutivePersona, by: str
) -> dict[str, Any]:
    agent_id = str(action.get("agent_id") or "").strip()
    statement = str(action.get("statement") or "").strip()
    if not agent_id:
        raise ValueError("create_goal is missing agent_id")
    if not statement:
        raise ValueError("create_goal is missing statement")
    target = _resolve_known_agent(store, agent_id)
    goal = Goal(
        statement=statement,
        success_criteria=[str(item) for item in action.get("success_criteria") or []],
        explicitly_not=[str(item) for item in action.get("explicitly_not") or []],
        set_by=by,
        human_confirmed=True,
    )
    store.add_goal(target.agent_id, goal)
    return {
        "type": "create_goal",
        "agent_id": target.agent_id,
        "statement": goal.statement,
        "executive_agent_id": persona.agent_id,
    }


def _apply_attach_guidance(
    store: StateStore, action: dict[str, Any], *, persona: ExecutivePersona, by: str
) -> dict[str, Any]:
    del persona
    assignment_id = str(action.get("assignment_id") or "").strip()
    message = str(action.get("message") or "").strip()
    if not assignment_id:
        raise ValueError("attach_guidance is missing assignment_id")
    if not message:
        raise ValueError("attach_guidance is missing message")
    return {
        "type": "attach_guidance",
        **attach_operator_guidance(
            store, assignment_id, operator=by, message=message
        ),
    }


def _apply_ingest_text(
    store: StateStore, action: dict[str, Any], *, persona: ExecutivePersona, by: str
) -> dict[str, Any]:
    title = str(action.get("title") or "").strip()
    source = str(action.get("source") or f"executive:{persona.agent_id}").strip()
    document_type = str(action.get("document_type") or "note").strip()
    content = str(action.get("content") or "").strip()
    if not title:
        raise ValueError("ingest_text is missing title")
    if not content:
        raise ValueError("ingest_text is missing content")
    saved = store_ingest_result(
        store,
        ingest_text(
            title,
            source,
            document_type,
            content,
            content_path=f"executive:{persona.agent_id}:{uuid4()}",
            extra_metadata={"ingested_by": by, "executive_agent_id": persona.agent_id},
        ),
    )
    return {"type": "ingest_text", "document_id": saved["document_id"], "title": title}


def run_executive_chat_turn(
    store: StateStore,
    *,
    thread: Conversation,
    persona: ExecutivePersona,
    operator: str,
    content: str,
    provider: ModelProvider,
    max_iterations: int = 6,
    idempotency_key: str | None = None,
    enable_web_fetch: bool = True,
) -> dict[str, Any]:
    channel = thread.channel
    agent_label = persona.agent_id
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
            "kind": f"{EXECUTIVE_CHAT_KIND_PREFIX}_request",
            "conversation_id": channel,
            "agent_id": agent_label,
            "persona": persona.persona_id,
            "idempotency_key": idempotency_key,
        },
    )
    store.add_message(request)
    pending = _pending_chat_proposal(store, channel, kind_prefix=EXECUTIVE_CHAT_KIND_PREFIX)
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
            kind_prefix=EXECUTIVE_CHAT_KIND_PREFIX,
            apply=lambda actions: apply_executive_actions(
                store, actions, persona=persona, by=operator
            ),
        )
        store.touch_conversation(thread.thread_id)
        return result

    registry = executive_query_registry(include_web_fetch=enable_web_fetch)
    context = ExecutiveToolContext(
        store=store,
        persona=persona,
        operator=operator,
        user_message=content,
        request_message_id=request.message_id,
        conversation_id=channel,
    )
    tools = native_tool_specs(registry)
    observations: list[dict[str, Any]] = []
    tools_used: list[str] = []
    final_text: str | None = None
    response = None
    budget = max(1, max_iterations)
    iterations = 0
    for iteration in range(budget):
        iterations = iteration + 1
        demand_final = iteration == budget - 1 and budget > 1
        prompt = build_executive_prompt(
            store,
            persona=persona,
            operator=operator,
            content=content,
            registry=registry,
            observations=observations,
            pending=pending,
            demand_final=demand_final,
        )
        response = _complete_model_call(store, provider, prompt, tools=tools, holder=agent_label)
        reply: ChiefChatReply = parse_chief_chat_reply(response.text)
        if reply.kind == "actions":
            result = _stage_chat_proposal(
                store,
                reply.actions,
                reply.summary,
                channel=channel,
                sender=operator,
                request=request,
                response=response,
                agent_id=agent_label,
                kind_prefix=EXECUTIVE_CHAT_KIND_PREFIX,
            )
            store.touch_conversation(thread.thread_id)
            return {**result, "iterations": iterations, "tools_used": tools_used}
        _record_executive_usage(store, response, channel=channel, agent_id=agent_label)
        if reply.kind == "tool_call" and not demand_final:
            tool_result = registry.execute(reply.tool_name, context, reply.tool_arguments)
            observation = tool_result.to_observation(reply.tool_name)
            observation["output"] = _truncate(str(observation["output"]), MAX_OBSERVATION_CHARS)
            observations.append(observation)
            tools_used.append(reply.tool_name)
            continue
        final_text = (
            reply.text
            if reply.kind == "text"
            else "I could not complete that turn cleanly."
        )
        break
    if response is None:  # pragma: no cover
        raise RuntimeError("executive chat turn produced no model response")
    if not final_text:
        final_text = "I checked what I could, but I need a more specific instruction."
    response_message = ChatMessage(
        channel=channel,
        sender=agent_label,
        recipient=operator,
        content=final_text,
        metadata={
            "kind": f"{EXECUTIVE_CHAT_KIND_PREFIX}_response",
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
            "source": EXECUTIVE_CHAT_KIND_PREFIX,
            "conversation_id": channel,
            "summary": _summarize(final_text),
            "request": content,
            "response": final_text,
            "user": operator,
        }
    )
    store.touch_conversation(thread.thread_id)
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


def run_connector_executive_chat(
    store: StateStore,
    incoming: IncomingConnectorMessage,
    username: str,
    *,
    provider: ModelProvider,
    max_iterations: int = 3,
    enable_web_fetch: bool = True,
) -> ConnectorChatReply:
    try:
        persona = resolve_executive_persona(store, username)
    except UnknownExecutiveError as exc:
        raise RuntimeError(str(exc)) from exc
    conversation = store.resolve_active_conversation(
        username,
        persona.persona_id,
        title=persona.display_name,
    )
    result = run_executive_chat_turn(
        store,
        thread=conversation,
        persona=persona,
        operator=username,
        content=incoming.text,
        provider=provider,
        max_iterations=max_iterations,
        enable_web_fetch=enable_web_fetch,
        idempotency_key=(
            f"{incoming.provider}:executive:{incoming.external_user_id}:"
            f"{incoming.external_message_id}"
        ),
    )
    response_id = result.get("response_message_id")
    response = (
        next(
            (
                item
                for item in store.messages(conversation.channel)
                if item.message_id == response_id
            ),
            None,
        )
        if isinstance(response_id, str)
        else None
    )
    text = response.content if response else str(result.get("summary") or "")
    return ConnectorChatReply(text=text, agent_id=persona.agent_id)
