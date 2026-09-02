"""Durable, owner-driven direct Crew Chief turns."""

from __future__ import annotations

import hashlib
import json
import logging
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from brigade.chief_chat import (
    CHIEF_CHAT_KIND_PREFIX,
    ChatToolContext,
    Persona,
    _complete_model_call,
    _immediate_task_creation_allowed,
    _maybe_refresh_summary,
    _normalize_chief_actions,
    _record_chief_usage,
    build_chief_chat_prompt,
    chief_query_registry,
    parse_chief_chat_reply,
    resolve_persona,
)
from brigade.citations import (
    available_citations,
    citation_context,
    citation_instructions,
    citation_validation_status,
    classify_rendered_claims,
    enforce_citation_answer,
)
from brigade.config import Settings
from brigade.direct_chief import ACTIVE_TURN_STATUSES, CHIEF_DIRECT_TOOL_GROUPS
from brigade.governance import ensure_policy_projections_current
from brigade.providers import ModelProvider, provider_from_settings
from brigade.research_control import record_citation_validation
from brigade.runner import MAX_OBSERVATION_CHARS, _truncate
from brigade.schemas import Agent, Assignment, ChatMessage, Conversation
from brigade.secrets import read_telegram_bot_token
from brigade.services import (
    _apply_chat_actions_now,
    _stage_chat_proposal,
    _summarize,
    apply_chief_chat_actions,
)
from brigade.store import StateStore
from brigade.time import parse_utc_iso, utc_now, utc_now_iso
from brigade.tools import (
    ToolRegistry,
    ToolResult,
    ToolSpec,
    default_tool_registry,
    native_tool_specs,
)

DIRECT_SOURCE = "chief_direct_chat"
LOGGER = logging.getLogger(__name__)
DIRECT_CONTEXT_FILES = (
    "AGENTS.md",
    "USER.md",
    "IDENTITY.md",
    "SOUL.md",
    "TOOLS.md",
    "MEMORY.md",
)
DIRECT_FILE_MAX_CHARS = 4000
DIRECT_PROGRESS_MIN_SECONDS = 300
DIRECT_OBSERVATION_LIMIT = 80
DIRECT_TOOL_GROUP_MAP = {
    "workspace_read": {"list_files", "read_file"},
    "workspace_write": {"write_file", "propose_policy_change"},
    "shell": {"shell"},
    "workspace_tools": {"run_workspace_tool", "request_tool"},
}
DIRECT_CONTROL_TOOLS = frozenset(
    {"report_progress", "request_permission", "request_turn_extension"}
)
_CONTEXT_CACHE: dict[str, tuple[str, tuple[tuple[str, int, int], ...], dict[str, Any]]] = {}


@dataclass(frozen=True)
class DirectChiefToolContext(ChatToolContext):
    agent: Agent | None = None
    assignment: Assignment | None = None
    turn_id: str = ""
    settings: Settings | None = None
    direct_chief_turn: bool = True

    @property
    def workspace(self) -> Path:
        if self.agent is None:
            raise ValueError("direct Chief context has no agent")
        return self.store.data_dir / self.agent.workspace_path


def _bounded_file(path: Path, max_chars: int = DIRECT_FILE_MAX_CHARS) -> str:
    try:
        value = path.read_text(encoding="utf-8")
    except OSError:
        return "[missing]"
    if len(value) <= max_chars:
        return value
    half = max_chars // 2
    return value[:half] + "\n[... truncated; use read_file for the full file ...]\n" + value[-half:]


def chief_context_snapshot(
    store: StateStore,
    agent: Agent,
    *,
    max_chars: int,
) -> dict[str, Any]:
    workspace = store.data_dir / agent.workspace_path
    now = utc_now()
    utc_date = now.date().isoformat()
    paths = [workspace / name for name in DIRECT_CONTEXT_FILES]
    memory_dir = workspace / "memory"
    cutoff = now.timestamp() - 86_400
    daily_paths = []
    if memory_dir.exists():
        daily_paths = [
            path
            for path in sorted(memory_dir.glob("*-MEMORY.md"), reverse=True)
            if path.stat().st_mtime >= cutoff
        ]
    all_paths = [*paths, *daily_paths]
    signature = tuple(
        (str(path), path.stat().st_mtime_ns, path.stat().st_size)
        if path.exists()
        else (str(path), 0, 0)
        for path in all_paths
    )
    cache_key = f"{agent.agent_id}:{max_chars}"
    cached = _CONTEXT_CACHE.get(cache_key)
    if cached and cached[0] == utc_date and cached[1] == signature:
        return cached[2]
    snapshot: dict[str, Any] = {
        "refreshed_at": utc_now_iso(),
        "utc_day": utc_date,
        "governed_workspace": {
            path.name: _bounded_file(path) for path in paths
        },
        "rolling_24h_daily_memory": {
            path.name: _bounded_file(path) for path in daily_paths
        },
    }
    encoded = json.dumps(snapshot, sort_keys=True, default=str)
    if len(encoded) > max_chars:
        snapshot["context_truncated"] = True
        remaining = max(0, max_chars - len(json.dumps(snapshot["governed_workspace"])))
        daily = snapshot["rolling_24h_daily_memory"]
        snapshot["rolling_24h_daily_memory"] = {
            key: _truncate(str(value), remaining)
            for key, value in daily.items()
            if remaining > 0
        }
    _CONTEXT_CACHE[cache_key] = (utc_date, signature, snapshot)
    return snapshot


def _load_maintenance_actions(settings: Settings) -> list[dict[str, Any]]:
    path = settings.maintenance_actions_path
    if path is None or not path.exists():
        return []
    if not settings.allow_json_store:
        if path.is_symlink():
            raise ValueError("maintenance actions file must not be a symbolic link")
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("maintenance actions path must be a regular file")
        if metadata.st_uid != 0:
            raise ValueError("maintenance actions file must be owned by root")
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError("maintenance actions file must not be group/world writable")
    payload = json.loads(path.read_text(encoding="utf-8"))
    actions = payload.get("actions") if isinstance(payload, dict) else payload
    if not isinstance(actions, list):
        raise ValueError("maintenance actions file must contain an actions array")
    return [dict(item) for item in actions if isinstance(item, dict)]


def _maintenance_action(context: DirectChiefToolContext, arguments: dict[str, Any]) -> ToolResult:
    action_id = str(arguments.get("action_id") or "").strip()
    if not action_id:
        return ToolResult(False, "action_id is required")
    settings = context.settings
    if settings is None or context.agent is None:
        return ToolResult(False, "maintenance action context is unavailable")
    action = next(
        (item for item in _load_maintenance_actions(settings) if item.get("id") == action_id),
        None,
    )
    if action is None:
        return ToolResult(False, f"unknown maintenance action: {action_id}")
    allowed = {str(item) for item in action.get("allowed_chief_ids") or []}
    if allowed and context.agent.agent_id not in allowed:
        return ToolResult(
            False,
            f"maintenance action is not authorized for {context.agent.agent_id}",
        )
    command = action.get("argv")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(x, str) for x in command)
    ):
        return ToolResult(False, f"maintenance action {action_id} has invalid argv")
    timeout = min(max(1, int(action.get("timeout_seconds") or 300)), 3600)
    process = subprocess.Popen(
        command,
        cwd=context.workspace,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + timeout
    cancelled = False
    timed_out = False
    while process.poll() is None:
        current = context.store.find_chief_interactive_turn(context.turn_id)
        if current and current.get("cancel_requested"):
            cancelled = True
            process.terminate()
            break
        if time.monotonic() >= deadline:
            timed_out = True
            process.terminate()
            break
        time.sleep(0.25)
    try:
        stdout, stderr = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
    output = "\n".join(
        part for part in ((stdout or "").strip(), (stderr or "").strip()) if part
    )
    if cancelled:
        return ToolResult(
            False,
            _truncate(output or "cancelled by owner", 12_000),
            {"action_id": action_id, "cancelled": True},
        )
    if timed_out:
        return ToolResult(
            False,
            _truncate(output or f"timed out after {timeout} seconds", 12_000),
            {"action_id": action_id, "timed_out": True},
        )
    return ToolResult(
        process.returncode == 0,
        _truncate(output or f"exit code {process.returncode}", 12_000),
        {"action_id": action_id, "exit_code": process.returncode},
    )


def _search_memory(context: DirectChiefToolContext, arguments: dict[str, Any]) -> ToolResult:
    query = str(arguments.get("query") or "").strip()
    if not query:
        return ToolResult(False, "query is required")
    rows = context.store.search_episodes(query, limit=min(int(arguments.get("limit") or 8), 20))
    workspace = context.workspace
    terms = [item.lower() for item in query.split() if len(item) >= 3]
    daily = []
    for path in sorted((workspace / "memory").glob("*-MEMORY.md"), reverse=True):
        text = _bounded_file(path, 8000)
        if any(term in text.lower() for term in terms):
            daily.append({"path": f"memory/{path.name}", "excerpt": _truncate(text, 1200)})
        if len(daily) >= 5:
            break
    return ToolResult(True, json.dumps({"daily": daily, "episodes": rows}, default=str))


def _control_tool(name: str, arguments: dict[str, Any]) -> ToolResult:
    return ToolResult(True, name, {name: dict(arguments)})


def _direct_registry(
    policy: dict[str, Any],
    settings: Settings,
    *,
    all_tools: bool = False,
) -> ToolRegistry:
    registry = chief_query_registry(
        include_web_fetch=settings.chief_chat_web_fetch_enabled
    )
    groups = CHIEF_DIRECT_TOOL_GROUPS if all_tools else set(policy.get("tool_groups") or [])
    allowed = set()
    for group in groups:
        allowed.update(DIRECT_TOOL_GROUP_MAP.get(group, set()))
    registry.extend(default_tool_registry().restricted(allowed))
    registry.register(
        ToolSpec(
            name="search_memory",
            description="Search older daily and archived Chief memory relevant to this request.",
            argument_schema={"query": "memory query", "limit": "optional maximum results"},
        ),
        _search_memory,
    )
    if "maintenance" in groups:
        registry.register(
            ToolSpec(
                name="run_maintenance_action",
                description="Run one root-configured exact maintenance action.",
                argument_schema={"action_id": "configured maintenance action id"},
            ),
            _maintenance_action,
        )
    registry.register(
        ToolSpec(
            name="report_progress",
            description="Publish one meaningful milestone to the owner during a long turn.",
            argument_schema={"summary": "short concrete progress milestone"},
        ),
        lambda context, arguments: _control_tool("progress", arguments),
    )
    registry.register(
        ToolSpec(
            name="request_permission",
            description=(
                "Request one-shot owner permission for an exact currently unavailable "
                "direct tool call."
            ),
            argument_schema={
                "tool": "tool name",
                "arguments": "exact argument object",
                "reason": "why this one action is needed",
            },
        ),
        lambda context, arguments: _control_tool("permission_request", arguments),
    )
    registry.register(
        ToolSpec(
            name="request_turn_extension",
            description="Ask the owner to remove the tool-count ceiling for this request.",
            argument_schema={"reason": "why more than the current tool budget is needed"},
        ),
        lambda context, arguments: _control_tool("turn_extension", arguments),
    )
    return registry


def _chief_agent(store: StateStore, chief_agent_id: str) -> Agent:
    agent = next((item for item in store.agents() if item.agent_id == chief_agent_id), None)
    if agent is None:
        raise ValueError(f"unknown Chief agent: {chief_agent_id}")
    resolve_persona(store, chief_agent_id)
    return agent


def queue_direct_chief_turn(
    store: StateStore,
    *,
    thread: Conversation,
    chief_agent_id: str,
    operator_username: str,
    content: str,
    idempotency_key: str,
    source_provider: str = "web",
    source_account_id: str | None = None,
    source_chat_id: str | None = None,
) -> dict[str, Any]:
    active = [
        item
        for item in store.chief_interactive_turns(thread_id=thread.thread_id)
        if item.get("status") in ACTIVE_TURN_STATUSES
    ]
    if active:
        raise RuntimeError(f"Chief is already working on turn {active[0]['turn_id']}")
    duplicate = next(
        (
            item
            for item in store.chief_interactive_turns(thread_id=thread.thread_id)
            if item.get("idempotency_key") == idempotency_key
        ),
        None,
    )
    if duplicate:
        return duplicate
    _chief_agent(store, chief_agent_id)
    request = ChatMessage(
        channel=thread.channel,
        sender=operator_username,
        recipient=chief_agent_id,
        content=content,
        metadata={
            "kind": "chief_direct_chat_request",
            "agent_id": chief_agent_id,
            "idempotency_key": idempotency_key,
        },
    )
    store.add_message(request)
    now = utc_now_iso()
    turn = {
        "turn_id": str(uuid4()),
        "thread_id": thread.thread_id,
        "chief_agent_id": chief_agent_id,
        "operator_username": operator_username,
        "content": content,
        "request_message_id": request.message_id,
        "idempotency_key": idempotency_key,
        "source_provider": source_provider,
        "source_account_id": source_account_id,
        "source_chat_id": source_chat_id,
        "status": "queued",
        "observations": [],
        "tools_used": [],
        "tool_count": 0,
        "active_elapsed_seconds": 0.0,
        "unbounded_tool_calls": False,
        "cancel_requested": False,
        "created_at": now,
        "updated_at": now,
    }
    store.upsert_chief_interactive_turn(turn)
    return turn


def direct_turn_control(
    store: StateStore,
    *,
    thread: Conversation,
    operator_username: str,
    command: str,
) -> dict[str, Any] | None:
    normalized = command.strip().lower()
    active = next(
        (
            item
            for item in reversed(store.chief_interactive_turns(thread_id=thread.thread_id))
            if item.get("status") in ACTIVE_TURN_STATUSES
        ),
        None,
    )
    if active is None:
        if normalized in {"/status", "/cancel", "confirm", "/confirm"}:
            return {
                "status": "idle",
                "summary": "There is no active direct Chief turn.",
            }
        return None
    if normalized == "/status":
        return {
            "status": active["status"],
            "turn_id": active["turn_id"],
            "summary": (
                active.get("summary")
                or active.get("progress_summary")
                or "Work is in progress."
            ),
        }
    if normalized == "/cancel":
        if active["status"] in {"queued", "awaiting_permission"}:
            active.update(
                {
                    "status": "cancelled",
                    "cancel_requested": True,
                    "pending_permission": None,
                    "summary": "Cancelled by the owner.",
                    "updated_at": utc_now_iso(),
                }
            )
            store.upsert_chief_interactive_turn(active)
            store.add_message(
                ChatMessage(
                    channel=thread.channel,
                    sender=str(active["chief_agent_id"]),
                    recipient=operator_username,
                    content="Cancelled by the owner.",
                    metadata={
                        "kind": "chief_direct_chat_status",
                        "turn_id": active["turn_id"],
                        "status": "cancelled",
                    },
                )
            )
            active["status_message_key"] = "cancelled:Cancelled by the owner."
            store.upsert_chief_interactive_turn(active)
            return {"status": "cancelled", "turn_id": active["turn_id"]}
        active["cancel_requested"] = True
        active["updated_at"] = utc_now_iso()
        store.upsert_chief_interactive_turn(active)
        return {"status": "cancelling", "turn_id": active["turn_id"]}
    if normalized in {"confirm", "/confirm"} and active["status"] == "awaiting_permission":
        pending = dict(active.get("pending_permission") or {})
        if pending.get("kind") == "remove_tool_ceiling":
            active["unbounded_tool_calls"] = True
        elif pending.get("kind") == "tool_call":
            active["granted_tool_call"] = pending.get("tool_call")
        else:
            return {"status": "blocked", "summary": "No valid permission request is pending."}
        active["pending_permission"] = None
        active["status"] = "queued"
        active["permission_granted_by"] = operator_username
        active["updated_at"] = utc_now_iso()
        store.upsert_chief_interactive_turn(active)
        return {"status": "resumed", "turn_id": active["turn_id"]}
    return {
        "status": active["status"],
        "turn_id": active["turn_id"],
        "summary": "Chief is already working. Use /status or /cancel.",
    }


def _arguments_hash(tool_name: str, arguments: dict[str, Any]) -> str:
    payload = json.dumps({"tool": tool_name, "arguments": arguments}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _persist_progress(
    store: StateStore,
    settings: Settings,
    turn: dict[str, Any],
    thread: Conversation,
    summary: str,
) -> ChatMessage | None:
    last_at = turn.get("last_progress_at")
    if last_at:
        elapsed = (utc_now() - parse_utc_iso(str(last_at))).total_seconds()
        if elapsed < DIRECT_PROGRESS_MIN_SECONDS:
            return None
    message = ChatMessage(
        channel=thread.channel,
        sender=str(turn["chief_agent_id"]),
        recipient=str(turn["operator_username"]),
        content=summary,
        metadata={"kind": "chief_direct_chat_progress", "turn_id": turn["turn_id"]},
    )
    store.add_message(message)
    turn["last_progress_at"] = utc_now_iso()
    turn["progress_summary"] = summary
    _send_turn_telegram(store, settings, turn, summary)
    return message


def _send_turn_telegram(
    store: StateStore,
    settings: Settings,
    turn: dict[str, Any],
    text: str,
) -> bool:
    account_id = str(turn.get("source_account_id") or "").strip()
    chat_id = str(turn.get("source_chat_id") or "").strip()
    if not account_id or not chat_id:
        return False
    token = read_telegram_bot_token(settings, account_id)
    if not token:
        return False
    try:
        from brigade.connectors import send_telegram_message

        bounded = text[: settings.connector_max_outbound_chars]
        send_telegram_message(token, chat_id=chat_id, text=bounded)
        return True
    except Exception as exc:
        store.add_alert(
            f"direct Chief Telegram delivery failed for {turn['turn_id']}: {exc}"
        )
        return False


def _begin_tool_call(
    store: StateStore,
    turn: dict[str, Any],
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    fingerprint = _arguments_hash(tool_name, arguments)
    turn["in_flight_tool"] = {
        "tool": tool_name,
        "arguments": arguments,
        "arguments_hash": fingerprint,
        "started_at": utc_now_iso(),
    }
    turn["updated_at"] = utc_now_iso()
    store.upsert_chief_interactive_turn(turn)
    return fingerprint


def _complete_tool_call(turn: dict[str, Any]) -> None:
    turn["in_flight_tool"] = None


def _finalize_turn(
    store: StateStore,
    turn: dict[str, Any],
    thread: Conversation,
    persona: Persona,
    response,
    final_text: str,
    provider: ModelProvider,
    history_window: int,
) -> dict[str, Any]:
    observations = list(turn.get("observations") or [])
    draft = final_text
    final_text, citation_state, citations, errors, repaired = enforce_citation_answer(
        store, str(turn["content"]), observations, final_text
    )
    status = citation_validation_status(citation_state, errors)
    claims = (
        [{"kind": "unverified_draft", "citation_ids": []}]
        if status == "unvalidated_draft"
        else classify_rendered_claims(final_text, citations)
    )
    if citation_state.required:
        record_citation_validation(
            store,
            errors=errors,
            principal=persona.chief_agent_id or "front_desk",
            correlation_id=f"citation:chief-direct:{turn['turn_id']}",
        )
    message = ChatMessage(
        channel=thread.channel,
        sender=str(turn["chief_agent_id"]),
        recipient=str(turn["operator_username"]),
        content=final_text,
        metadata={
            "kind": "chief_direct_chat_response",
            "turn_id": turn["turn_id"],
            "tools_used": list(turn.get("tools_used") or []),
            "provider": response.provider,
            "model": response.model,
            "route_type": response.route_type,
            "citation_required": citation_state.required,
            "citations": citations,
            "citation_validation_errors": errors,
            "citation_validation_status": status,
            "available_citations": available_citations(citation_state),
            "citation_draft": draft if status == "unvalidated_draft" else None,
            "citation_repaired": repaired,
            "claim_classes": claims,
        },
    )
    store.add_message(message)
    store.add_episode(
        {
            "episode_id": str(uuid4()),
            "agent_id": turn["chief_agent_id"],
            "created_at": utc_now_iso(),
            "source": DIRECT_SOURCE,
            "conversation_id": thread.channel,
            "summary": _summarize(final_text),
            "request": turn["content"],
            "response": final_text,
            "user": turn["operator_username"],
        }
    )
    turn.update(
        {
            "status": "complete",
            "response_message_id": message.message_id,
            "summary": _summarize(final_text),
            "updated_at": utc_now_iso(),
        }
    )
    store.upsert_chief_interactive_turn(turn)
    store.touch_conversation(thread.thread_id)
    _maybe_refresh_summary(
        store,
        thread=thread,
        provider=provider,
        history_window=history_window,
        agent_label=str(turn["chief_agent_id"]),
    )
    return turn


def run_direct_chief_turn(
    store: StateStore,
    settings: Settings,
    turn_id: str,
    *,
    provider: ModelProvider | None = None,
) -> dict[str, Any]:
    turn = store.find_chief_interactive_turn(turn_id)
    if turn is None:
        raise ValueError(f"unknown direct Chief turn: {turn_id}")
    if turn.get("status") not in {"queued", "running"}:
        return turn
    thread = store.find_conversation(str(turn["thread_id"]))
    if thread is None:
        raise ValueError(f"unknown Chief thread: {turn['thread_id']}")
    persona = resolve_persona(store, str(turn["chief_agent_id"]))
    agent = _chief_agent(store, str(turn["chief_agent_id"]))
    policy = store.chief_chat_policy(agent.agent_id)
    if not policy or not policy.get("direct_enabled"):
        raise RuntimeError(f"direct Chief chat is disabled for {agent.agent_id}")
    ensure_policy_projections_current(store, agent, actor="chief_direct_chat")
    registry = _direct_registry(policy, settings)
    all_registry = _direct_registry(policy, settings, all_tools=True)
    grantable_tools = {
        spec.name for spec in all_registry.specs()
    } - DIRECT_CONTROL_TOOLS
    synthetic = Assignment(
        assignment=str(turn["content"]),
        assigned_to=agent.agent_id,
        created_by=str(turn["operator_username"]),
        source=DIRECT_SOURCE,
        assignment_id=str(turn["turn_id"]),
        idempotency_key=str(turn.get("idempotency_key") or turn["turn_id"]),
    )
    context = DirectChiefToolContext(
        store=store,
        persona=persona,
        operator=str(turn["operator_username"]),
        user_message=str(turn["content"]),
        request_message_id=str(turn["request_message_id"]),
        conversation_id=thread.channel,
        agent=agent,
        assignment=synthetic,
        turn_id=str(turn["turn_id"]),
        settings=settings,
    )
    provider = provider or provider_from_settings(
        settings,
        provider=thread.model_provider or agent.model_provider,
        model=thread.model_name or agent.model_name,
        api_base=thread.model_base_url,
    )
    started = time.monotonic()
    turn["status"] = "running"
    turn["updated_at"] = utc_now_iso()
    store.upsert_chief_interactive_turn(turn)

    granted = turn.pop("granted_tool_call", None)
    if isinstance(granted, dict):
        name = str(granted.get("tool") or "")
        arguments = dict(granted.get("arguments") or {})
        if name not in grantable_tools:
            raise RuntimeError(f"tool cannot receive a one-shot grant: {name}")
        fingerprint = _begin_tool_call(store, turn, name, arguments)
        result = all_registry.execute(name, context, arguments)
        _complete_tool_call(turn)
        observation = result.to_observation(name)
        observation["output"] = _truncate(str(observation["output"]), MAX_OBSERVATION_CHARS)
        observation["one_shot_grant"] = True
        observation["arguments_hash"] = fingerprint
        turn.setdefault("observations", []).append(observation)
        turn.setdefault("tools_used", []).append(name)
        turn["tool_count"] = int(turn.get("tool_count") or 0) + 1
        turn["permission_consumed_at"] = utc_now_iso()
        turn["uncertain_tool_calls"] = [
            item
            for item in turn.get("uncertain_tool_calls") or []
            if item != fingerprint
        ]
        turn["updated_at"] = utc_now_iso()
        store.upsert_chief_interactive_turn(turn)

    response = None
    while True:
        current = store.find_chief_interactive_turn(str(turn["turn_id"]))
        if current and current.get("cancel_requested"):
            turn["cancel_requested"] = True
        elapsed = float(turn.get("active_elapsed_seconds") or 0) + (
            time.monotonic() - started
        )
        hard_limit = int(policy.get("hard_elapsed_seconds") or 7200)
        normal_limit = int(policy.get("max_elapsed_seconds") or 1800)
        limit = hard_limit if turn.get("unbounded_tool_calls") else normal_limit
        if elapsed >= limit:
            turn.update(
                {
                    "status": "blocked",
                    "summary": f"direct Chief turn reached its {limit}-second active-time limit",
                    "active_elapsed_seconds": elapsed,
                    "updated_at": utc_now_iso(),
                }
            )
            store.upsert_chief_interactive_turn(turn)
            return turn
        if turn.get("cancel_requested"):
            turn.update(
                {
                    "status": "cancelled",
                    "summary": "Cancelled by the owner.",
                    "active_elapsed_seconds": elapsed,
                    "updated_at": utc_now_iso(),
                }
            )
            store.upsert_chief_interactive_turn(turn)
            return turn
        max_calls = int(policy.get("max_tool_calls") or 60)
        if not turn.get("unbounded_tool_calls") and int(turn.get("tool_count") or 0) >= max_calls:
            turn.update(
                {
                    "status": "awaiting_permission",
                    "pending_permission": {
                        "kind": "remove_tool_ceiling",
                        "reason": "The configured tool-call ceiling was reached.",
                    },
                    "summary": (
                        f"I reached the {max_calls}-tool ceiling. Confirm to remove the "
                        "count ceiling for this request."
                    ),
                    "active_elapsed_seconds": elapsed,
                    "updated_at": utc_now_iso(),
                }
            )
            store.upsert_chief_interactive_turn(turn)
            return turn
        snapshot = chief_context_snapshot(
            store,
            agent,
            max_chars=int(policy.get("context_max_chars") or 32_000),
        )
        memory = {"chief_workspace_context": snapshot}
        observations = list(turn.get("observations") or [])[-DIRECT_OBSERVATION_LIMIT:]
        prompt = build_chief_chat_prompt(
            store,
            thread=thread,
            persona=persona,
            operator=str(turn["operator_username"]),
            content=str(turn["content"]),
            registry=registry,
            observations=observations,
            memory=memory,
            citation_instruction=citation_instructions(
                citation_context(store, str(turn["content"]), observations)
            ),
        )
        prompt += (
            "\n\nDIRECT OWNER TURN: exposed direct tools execute immediately. "
            "Use report_progress only for meaningful milestones. Use request_permission "
            "for one exact unavailable action and request_turn_extension if the work needs "
            "more than the configured tool budget."
        )
        response = _complete_model_call(
            store,
            provider,
            prompt,
            tools=native_tool_specs(registry),
            holder=agent.agent_id,
        )
        reply = parse_chief_chat_reply(response.text)
        if reply.kind == "actions":
            request_message = next(
                (
                    item
                    for item in store.messages(thread.channel)
                    if item.message_id == turn["request_message_id"]
                ),
                None,
            )
            if request_message is None:
                raise RuntimeError("direct Chief request message is missing")
            actions = _normalize_chief_actions(
                reply.actions, default_agent_id=persona.chief_agent_id
            )
            if _immediate_task_creation_allowed(
                store,
                str(turn["operator_username"]),
                actions,
                action_types={"create_assignment"},
                request_text=str(turn["content"]),
            ):
                applied = _apply_chat_actions_now(
                    store,
                    actions,
                    channel=thread.channel,
                    sender=str(turn["operator_username"]),
                    request=request_message,
                    response=response,
                    agent_id=agent.agent_id,
                    kind_prefix=CHIEF_CHAT_KIND_PREFIX,
                    apply=lambda requested: apply_chief_chat_actions(
                        store,
                        requested,
                        chief_id=persona.chief_agent_id,
                        managed_agent_ids=set(persona.managed_agent_ids),
                        by=str(turn["operator_username"]),
                        conversation_channel=thread.channel,
                    ),
                )
                turn.update(
                    {
                        "status": "complete",
                        "result_status": "applied",
                        "summary": applied["summary"],
                        "response_message_id": applied["response_message_id"],
                        "actions_applied": applied["actions_applied"],
                        "active_elapsed_seconds": elapsed,
                        "updated_at": utc_now_iso(),
                    }
                )
                store.upsert_chief_interactive_turn(turn)
                store.touch_conversation(thread.thread_id)
                return turn
            proposed = _stage_chat_proposal(
                store,
                actions,
                reply.summary,
                channel=thread.channel,
                sender=str(turn["operator_username"]),
                request=request_message,
                response=response,
                agent_id=agent.agent_id,
                kind_prefix=CHIEF_CHAT_KIND_PREFIX,
            )
            turn.update(
                {
                    "status": "complete",
                    "result_status": "proposed",
                    "summary": reply.summary,
                    "response_message_id": proposed["response_message_id"],
                    "active_elapsed_seconds": elapsed,
                    "updated_at": utc_now_iso(),
                }
            )
            store.upsert_chief_interactive_turn(turn)
            store.touch_conversation(thread.thread_id)
            return turn
        _record_chief_usage(store, response, channel=thread.channel, agent_id=agent.agent_id)
        if reply.kind == "tool_call":
            fingerprint = _arguments_hash(reply.tool_name, reply.tool_arguments)
            if (
                reply.tool_name in grantable_tools
                and fingerprint in set(turn.get("uncertain_tool_calls") or [])
            ):
                turn.update(
                    {
                        "status": "awaiting_permission",
                        "pending_permission": {
                            "kind": "tool_call",
                            "reason": (
                                "The previous process stopped while this exact action was "
                                "in flight, so its outcome is unknown. Confirm to retry it once."
                            ),
                            "arguments_hash": fingerprint,
                            "tool_call": {
                                "tool": reply.tool_name,
                                "arguments": reply.tool_arguments,
                            },
                        },
                        "summary": (
                            f"The outcome of a prior {reply.tool_name} call is unknown; "
                            "confirm before retrying it."
                        ),
                        "active_elapsed_seconds": elapsed,
                        "updated_at": utc_now_iso(),
                    }
                )
                store.upsert_chief_interactive_turn(turn)
                return turn
            _begin_tool_call(store, turn, reply.tool_name, reply.tool_arguments)
            result = registry.execute(reply.tool_name, context, reply.tool_arguments)
            _complete_tool_call(turn)
            metadata = dict(result.metadata or {})
            if metadata.get("progress") is not None:
                summary = str(metadata["progress"].get("summary") or "").strip()
                if summary:
                    _persist_progress(store, settings, turn, thread, summary)
            if metadata.get("turn_extension") is not None:
                turn.update(
                    {
                        "status": "awaiting_permission",
                        "pending_permission": {
                            "kind": "remove_tool_ceiling",
                            "reason": str(metadata["turn_extension"].get("reason") or ""),
                        },
                        "summary": (
                            "I need the owner to remove the tool-count ceiling. "
                            + str(metadata["turn_extension"].get("reason") or "")
                        ).strip(),
                        "active_elapsed_seconds": elapsed,
                        "updated_at": utc_now_iso(),
                    }
                )
                store.upsert_chief_interactive_turn(turn)
                return turn
            request = metadata.get("permission_request")
            if isinstance(request, dict):
                tool_name = str(request.get("tool") or "")
                arguments = dict(request.get("arguments") or {})
                if tool_name not in grantable_tools:
                    result = ToolResult(False, f"tool cannot receive a one-shot grant: {tool_name}")
                else:
                    turn.update(
                        {
                            "status": "awaiting_permission",
                            "pending_permission": {
                                "kind": "tool_call",
                                "reason": str(request.get("reason") or ""),
                                "arguments_hash": _arguments_hash(tool_name, arguments),
                                "tool_call": {"tool": tool_name, "arguments": arguments},
                            },
                            "summary": (
                                f"Permission requested for one exact {tool_name} action: "
                                f"{json.dumps(arguments, sort_keys=True)}"
                            ),
                            "active_elapsed_seconds": elapsed,
                            "updated_at": utc_now_iso(),
                        }
                    )
                    store.upsert_chief_interactive_turn(turn)
                    return turn
            observation = result.to_observation(reply.tool_name)
            observation["output"] = _truncate(
                str(observation["output"]), MAX_OBSERVATION_CHARS
            )
            turn.setdefault("observations", []).append(observation)
            turn.setdefault("tools_used", []).append(reply.tool_name)
            turn["tool_count"] = int(turn.get("tool_count") or 0) + 1
            turn["active_elapsed_seconds"] = elapsed
            turn["updated_at"] = utc_now_iso()
            store.upsert_chief_interactive_turn(turn)
            continue
        if reply.kind == "text":
            final_text = reply.text
        else:
            final_text = "I could not complete that direct turn cleanly."
        turn["active_elapsed_seconds"] = elapsed
        return _finalize_turn(
            store,
            turn,
            thread,
            persona,
            response,
            final_text,
            provider,
            settings.chief_chat_history_window,
        )


class ChiefDirectTurnWorker:
    def __init__(
        self,
        settings: Settings,
        store: StateStore,
        *,
        provider_factory: Callable[..., ModelProvider] = provider_from_settings,
    ) -> None:
        self.settings = settings
        self.store = store
        self.provider_factory = provider_factory
        self.stop_event = threading.Event()
        self.claim_owner = f"chief-direct-worker:{uuid4()}"
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self.run,
            name="brigade-chief-direct-turns",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 45) -> None:
        self.stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._recover_interrupted_turns()
                queued = self.store.chief_interactive_turns(status="queued")
            except Exception:
                LOGGER.warning("chief_direct_turn_reconcile_failed", exc_info=True)
                self.stop_event.wait(1)
                continue
            if not queued:
                self.stop_event.wait(1)
                continue
            turn = queued[0]
            if not self.store.try_claim_chief_interactive_turn(
                str(turn["turn_id"]), self.claim_owner, lease_seconds=90
            ):
                continue
            turn = self.store.find_chief_interactive_turn(str(turn["turn_id"])) or turn
            try:
                agent = next(
                    item
                    for item in self.store.agents()
                    if item.agent_id == turn["chief_agent_id"]
                )
                thread = self.store.find_conversation(str(turn["thread_id"]))
                provider_name = (
                    thread.model_provider
                    if thread and thread.model_provider
                    else agent.model_provider
                )
                model_name = (
                    thread.model_name if thread and thread.model_name else agent.model_name
                )
                indicator = self._typing_indicator(turn)
                with indicator:
                    result = run_direct_chief_turn(
                        self.store,
                        self.settings,
                        str(turn["turn_id"]),
                        provider=self.provider_factory(
                            self.settings,
                            provider=provider_name,
                            model=model_name,
                            api_base=thread.model_base_url if thread else None,
                        ),
                    )
                result["claim_owner"] = None
                result["claim_expires_at"] = None
                result["updated_at"] = utc_now_iso()
                self.store.upsert_chief_interactive_turn(result)
                self._persist_status_message(result)
                self._deliver_telegram(result)
            except Exception as exc:
                current = self.store.find_chief_interactive_turn(str(turn["turn_id"])) or turn
                current.update(
                    {
                        "status": "failed",
                        "summary": str(exc)[:1000],
                        "claim_owner": None,
                        "claim_expires_at": None,
                        "updated_at": utc_now_iso(),
                    }
                )
                self.store.upsert_chief_interactive_turn(current)
                self.store.add_alert(f"direct Chief turn {turn['turn_id']} failed: {exc}")
                self._persist_status_message(current)
                self._deliver_telegram(current)

    def _recover_interrupted_turns(self) -> None:
        for turn in self.store.chief_interactive_turns(status="running"):
            expires_at = turn.get("claim_expires_at")
            if expires_at and parse_utc_iso(str(expires_at)) > utc_now():
                continue
            in_flight = turn.get("in_flight_tool")
            if isinstance(in_flight, dict):
                fingerprint = str(in_flight.get("arguments_hash") or "")
                turn.setdefault("observations", []).append(
                    {
                        "tool_name": str(in_flight.get("tool") or "unknown"),
                        "ok": False,
                        "output": (
                            "The worker stopped during this tool call. Its outcome is "
                            "unknown, and OpenBrigade will not repeat it without owner approval."
                        ),
                        "arguments_hash": fingerprint,
                        "uncertain_execution": True,
                    }
                )
                if fingerprint:
                    uncertain = set(turn.get("uncertain_tool_calls") or [])
                    uncertain.add(fingerprint)
                    turn["uncertain_tool_calls"] = sorted(uncertain)
            turn.update(
                {
                    "status": "queued",
                    "summary": "Resuming from the last durably completed step.",
                    "in_flight_tool": None,
                    "claim_owner": None,
                    "claim_expires_at": None,
                    "updated_at": utc_now_iso(),
                }
            )
            self.store.upsert_chief_interactive_turn(turn)

    def _typing_indicator(self, turn: dict[str, Any]):
        account_id = str(turn.get("source_account_id") or "").strip()
        chat_id = str(turn.get("source_chat_id") or "").strip()
        if not account_id or not chat_id:
            return nullcontext()
        token = read_telegram_bot_token(self.settings, account_id)
        if not token:
            return nullcontext()
        from brigade.connectors import telegram_typing_indicator

        return telegram_typing_indicator(token, chat_id=chat_id)

    def _persist_status_message(self, turn: dict[str, Any]) -> None:
        if turn.get("status") not in {
            "awaiting_permission",
            "blocked",
            "cancelled",
            "failed",
        }:
            return
        summary = str(turn.get("summary") or turn.get("status"))
        key = f"{turn.get('status')}:{summary}"
        if turn.get("status_message_key") == key:
            return
        thread = self.store.find_conversation(str(turn["thread_id"]))
        if thread is None:
            return
        self.store.add_message(
            ChatMessage(
                channel=thread.channel,
                sender=str(turn["chief_agent_id"]),
                recipient=str(turn["operator_username"]),
                content=summary,
                metadata={
                    "kind": "chief_direct_chat_status",
                    "turn_id": turn["turn_id"],
                    "status": turn["status"],
                },
            )
        )
        turn["status_message_key"] = key
        turn["updated_at"] = utc_now_iso()
        self.store.upsert_chief_interactive_turn(turn)

    def _deliver_telegram(self, turn: dict[str, Any]) -> None:
        account_id = str(turn.get("source_account_id") or "").strip()
        chat_id = str(turn.get("source_chat_id") or "").strip()
        if not account_id or not chat_id:
            return
        response_id = turn.get("response_message_id")
        delivery_key = f"{turn.get('status')}:{response_id or turn.get('summary')}"
        if turn.get("telegram_delivery_key") == delivery_key:
            return
        text = str(turn.get("summary") or turn.get("status") or "Turn updated.")
        if response_id:
            thread = self.store.find_conversation(str(turn["thread_id"]))
            if thread is not None:
                message = next(
                    (
                        item
                        for item in reversed(self.store.messages(thread.channel))
                        if item.message_id == response_id
                    ),
                    None,
                )
                if message:
                    text = message.content
        if _send_turn_telegram(self.store, self.settings, turn, text):
            turn["telegram_delivery_key"] = delivery_key
            turn["updated_at"] = utc_now_iso()
            self.store.upsert_chief_interactive_turn(turn)
