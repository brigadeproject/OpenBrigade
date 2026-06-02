from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from brigade.auth import AuthResult, build_user_identity_context
from brigade.config import Settings
from brigade.health import HealthCheck
from brigade.providers import ModelProvider
from brigade.runner import _acquire_local_inference_lock
from brigade.schemas import Assignment, AssignmentStatus, ChatMessage, User
from brigade.store import StateStore
from brigade.time import utc_now_iso

OPS_ROOM_ROOMS: list[dict[str, Any]] = [
    {
        "id": "orchestrator",
        "label": "Orchestrator",
        "domains": [],
        "fixed_agent_id": "orchestrator",
        "kind": "orchestrator",
    },
    {
        "id": "studio",
        "label": "Studio",
        "domains": ["content", "writing", "marketing"],
        "kind": "work",
    },
    {
        "id": "craft",
        "label": "Craft Room",
        "domains": ["build", "design", "implementation", "prototype"],
        "kind": "work",
    },
    {
        "id": "cubicles",
        "label": "Cubicles",
        "domains": ["research", "ops", "coordination", "support"],
        "kind": "work",
    },
    {
        "id": "server",
        "label": "Server Room",
        "domains": ["infra", "security", "code", "test"],
        "kind": "work",
    },
    {
        "id": "finance",
        "label": "Finance",
        "domains": ["finance", "budget", "usage", "reporting"],
        "kind": "work",
    },
    {
        "id": "breakroom",
        "label": "Break Room",
        "domains": [],
        "statuses": ["idle", "queued"],
        "kind": "rest",
    },
    {
        "id": "barracks",
        "label": "Barracks",
        "domains": [],
        "statuses": ["blocked", "awaiting_human", "reflecting", "ruminating", "dreaming"],
        "kind": "rest",
    },
]

_ROOM_IDS = {room["id"] for room in OPS_ROOM_ROOMS}

_ROOM_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "server",
        (
            "api",
            "auth",
            "bug",
            "build",
            "ci",
            "code",
            "docker",
            "endpoint",
            "error",
            "infra",
            "migration",
            "network",
            "postgres",
            "redis",
            "security",
            "server",
            "stack",
            "test",
            "webhook",
        ),
    ),
    (
        "finance",
        (
            "abacus",
            "budget",
            "burn",
            "cost",
            "expense",
            "finance",
            "forecast",
            "invoice",
            "pricing",
            "report",
            "spend",
            "token",
            "usage",
        ),
    ),
    (
        "studio",
        (
            "blog",
            "brief",
            "content",
            "copy",
            "deck",
            "doc",
            "editorial",
            "launch plan",
            "marketing",
            "post",
            "release notes",
            "summary",
            "write",
        ),
    ),
    (
        "craft",
        (
            "design",
            "frontend",
            "implement",
            "mvp",
            "pixel",
            "polish",
            "prototype",
            "ship",
            "ui",
            "ux",
        ),
    ),
    (
        "cubicles",
        (
            "audit",
            "coordinate",
            "customer",
            "handoff",
            "ops",
            "organize",
            "plan",
            "research",
            "review",
            "support",
            "triage",
        ),
    ),
)

SAFE_CONFIG_KEYS = {
    "log_level": str,
    "orchestrator_cadence_seconds": int,
    "stale_work_seconds": int,
    "require_auth": bool,
    "default_provider": str,
    "default_model": str,
    "ollama_base_url": str,
    "web_host": str,
    "web_port": int,
}


def build_chat_payload(
    store: StateStore,
    *,
    channel: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    messages = store.messages(channel)
    channels: dict[str, int] = {}
    for message in store.messages():
        channels[message.channel] = channels.get(message.channel, 0) + 1
    return {
        "selected_channel": channel,
        "channels": [
            {"channel": name, "message_count": count}
            for name, count in sorted(channels.items())
        ],
        "messages": [message.to_dict() for message in messages[-limit:]],
        "agents": [agent.to_dict() for agent in store.agents()],
    }


def send_user_chat(
    store: StateStore,
    actor: AuthResult,
    *,
    user: User | None,
    agent_id: str,
    content: str,
    provider: ModelProvider,
    channel: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    agent = next((item for item in store.agents() if item.agent_id == agent_id), None)
    if agent is None:
        raise ValueError(f"unknown agent: {agent_id}")
    sender = user.username if user else "operator"
    conversation_id = channel or f"user:{sender}:{agent_id}"
    if idempotency_key:
        duplicate = _find_chat_by_idempotency(store, idempotency_key)
        if duplicate is not None:
            return {
                "status": "duplicate",
                "conversation_id": duplicate.channel,
                "request_message_id": duplicate.message_id,
                "response_message_id": None,
                "agent_id": agent_id,
            }

    metadata = _user_chat_metadata(actor, user)
    request = ChatMessage(
        channel=conversation_id,
        sender=sender,
        recipient=agent_id,
        content=content,
        metadata={
            **metadata,
            "kind": "user_chat_request",
            "conversation_id": conversation_id,
            "agent_id": agent_id,
            "idempotency_key": idempotency_key,
        },
    )
    store.add_message(request)

    route_type = getattr(provider, "route_type", "unknown")
    lock_acquired = False
    try:
        if route_type == "local":
            _acquire_chat_local_inference_lock(store, agent_id)
            lock_acquired = True
        response = provider.complete(
            _user_chat_prompt(agent.display_name, agent.agent_id, content, user)
        )
    except RuntimeError as exc:
        summary = str(exc)
        store.add_alert(f"user chat {conversation_id}: {summary}")
        return {
            "status": "blocked",
            "conversation_id": conversation_id,
            "summary": summary,
            "request_message_id": request.message_id,
            "response_message_id": None,
            "agent_id": agent_id,
            "route_type": route_type,
        }
    finally:
        if lock_acquired:
            _release_chat_local_inference_lock(store, agent_id)

    response_text = response.text.strip()
    response_message = ChatMessage(
        channel=conversation_id,
        sender=agent_id,
        recipient=sender,
        content=response_text,
        metadata={
            "kind": "user_chat_response",
            "conversation_id": conversation_id,
            "provider": response.provider,
            "model": response.model,
            "route_type": response.route_type,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "estimated_cost_usd": response.estimated_cost_usd,
        },
    )
    store.add_message(response_message)
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
            "conversation_id": conversation_id,
            "source": "user_chat",
        }
    )
    store.add_episode(
        {
            "episode_id": str(uuid4()),
            "agent_id": agent_id,
            "created_at": utc_now_iso(),
            "source": "user_chat",
            "conversation_id": conversation_id,
            "summary": _summarize(response_text),
            "request": content,
            "response": response_text,
            "user": sender,
        }
    )
    return {
        "status": "complete",
        "conversation_id": conversation_id,
        "summary": _summarize(response_text),
        "request_message_id": request.message_id,
        "response_message_id": response_message.message_id,
        "agent_id": agent_id,
        "provider": response.provider,
        "model": response.model,
        "route_type": response.route_type,
    }


def send_orchestrator_chat(
    store: StateStore,
    actor: AuthResult,
    *,
    user: User | None,
    content: str,
    provider: ModelProvider,
    channel: str = "orchestrator",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    sender = user.username if user else "operator"
    if idempotency_key:
        duplicate = _find_chat_by_idempotency(store, idempotency_key)
        if duplicate is not None:
            return {
                "status": "duplicate",
                "conversation_id": duplicate.channel,
                "request_message_id": duplicate.message_id,
                "response_message_id": None,
                "agent_id": "orchestrator",
            }

    metadata = _user_chat_metadata(actor, user)
    request = ChatMessage(
        channel=channel,
        sender=sender,
        recipient="orchestrator",
        content=content,
        metadata={
            **metadata,
            "kind": "orchestrator_chat_request",
            "conversation_id": channel,
            "agent_id": "orchestrator",
            "idempotency_key": idempotency_key,
        },
    )
    store.add_message(request)

    route_type = getattr(provider, "route_type", "unknown")
    lock_acquired = False
    try:
        if route_type == "local":
            _acquire_chat_local_inference_lock(store, "orchestrator")
            lock_acquired = True
        response = provider.complete(_orchestrator_chat_prompt(store, content, user))
    except RuntimeError as exc:
        summary = str(exc)
        store.add_alert(f"orchestrator chat {channel}: {summary}")
        return {
            "status": "blocked",
            "conversation_id": channel,
            "summary": summary,
            "request_message_id": request.message_id,
            "response_message_id": None,
            "agent_id": "orchestrator",
            "route_type": route_type,
        }
    finally:
        if lock_acquired:
            _release_chat_local_inference_lock(store, "orchestrator")

    response_text = response.text.strip()
    response_message = ChatMessage(
        channel=channel,
        sender="orchestrator",
        recipient=sender,
        content=response_text,
        metadata={
            "kind": "orchestrator_chat_response",
            "conversation_id": channel,
            "provider": response.provider,
            "model": response.model,
            "route_type": response.route_type,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "estimated_cost_usd": response.estimated_cost_usd,
        },
    )
    store.add_message(response_message)
    store.add_usage_record(
        {
            "usage_id": str(uuid4()),
            "assignment_id": None,
            "agent_id": "orchestrator",
            "provider": response.provider,
            "model": response.model,
            "route_type": response.route_type,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "total_tokens": response.input_tokens + response.output_tokens,
            "estimated_cost_usd": response.estimated_cost_usd,
            "recorded_at": utc_now_iso(),
            "conversation_id": channel,
            "source": "orchestrator_chat",
        }
    )
    store.add_episode(
        {
            "episode_id": str(uuid4()),
            "agent_id": "orchestrator",
            "created_at": utc_now_iso(),
            "source": "orchestrator_chat",
            "conversation_id": channel,
            "summary": _summarize(response_text),
            "request": content,
            "response": response_text,
            "user": sender,
        }
    )
    return {
        "status": "complete",
        "conversation_id": channel,
        "summary": _summarize(response_text),
        "request_message_id": request.message_id,
        "response_message_id": response_message.message_id,
        "agent_id": "orchestrator",
        "provider": response.provider,
        "model": response.model,
        "route_type": response.route_type,
    }


def build_hierarchy_payload(store: StateStore) -> dict[str, Any]:
    teams = [team.to_dict() for team in store.teams()]
    agents = [agent.to_dict() for agent in store.agents()]
    assignments = [assignment.to_dict() for assignment in store.assignments()]
    goals = {key: [goal.to_dict() for goal in values] for key, values in store.goals().items()}
    return {
        "teams": teams,
        "agents": agents,
        "assignments": assignments,
        "goals": goals,
        "roots": [team for team in teams if not team.get("parent_team_id")],
    }


def build_ops_room_payload(
    store: StateStore,
) -> dict[str, Any]:
    mission = store.mission()
    agents = store.agents()
    teams = store.teams()
    assignments = store.assignments()
    states = store.agent_states()
    goals = store.goals()
    reasoning = store.orchestrator_reasoning()
    usage = _usage_by_agent(store.usage_records())
    active_by_agent = _active_assignment_by_agent(assignments)
    chief_team_ids = {
        team.crew_chief_id: team.team_id for team in teams if team.crew_chief_id is not None
    }

    visual_agents: list[dict[str, Any]] = []
    for agent in agents:
        state = states.get(agent.agent_id)
        assignment = active_by_agent.get(agent.agent_id)
        status = _agent_status(state.status if state else None, assignment)
        visual_agents.append(
            {
                **agent.to_dict(),
                "team_role": "crew_chief"
                if agent.agent_id in chief_team_ids
                else agent.role,
                "crew_chief_for_team_id": chief_team_ids.get(agent.agent_id),
                "status": status,
                "activity": _agent_activity(status, assignment),
                "room": _agent_room(agent.to_dict(), status, assignment),
                "current_assignment": assignment.to_dict() if assignment else None,
                "state": state.to_dict() if state else None,
                "goals": [goal.to_dict() for goal in goals.get(agent.agent_id, [])],
                "usage": usage.get(agent.agent_id, _empty_usage()),
            }
        )

    return {
        "version": 1,
        "generated_at": utc_now_iso(),
        "mission": mission.to_dict() if mission else None,
        "latest_reasoning": reasoning[-1] if reasoning else None,
        "rooms": OPS_ROOM_ROOMS,
        "agents": visual_agents,
        "teams": [team.to_dict() for team in teams],
        "assignments": [assignment.to_dict() for assignment in assignments],
        "goals": {
            agent_id: [goal.to_dict() for goal in agent_goals]
            for agent_id, agent_goals in goals.items()
        },
        "alerts": store.alerts(),
        "financial_report": store.latest_financial_report(),
        "local_inference": store.local_inference(),
        "cloud_jobs": store.cloud_jobs(),
        "messages": [message.to_dict() for message in store.messages()[-30:]],
    }


def build_cockpit_payload(
    store: StateStore,
    settings: Settings,
    *,
    datastore_checks: list[HealthCheck],
    started_at: str,
    uptime_seconds: int,
) -> dict[str, Any]:
    dashboard = build_dashboard_payload_data(store)
    ops_room = build_ops_room_payload(store)
    assignments = ops_room["assignments"]
    agents = ops_room["agents"]
    blocked = [
        item
        for item in assignments
        if item.get("status") == AssignmentStatus.BLOCKED.value
        or item.get("awaiting_human")
        or item.get("blockers")
    ]
    active = [
        item
        for item in assignments
        if item.get("status") in {AssignmentStatus.ASSIGNED.value, AssignmentStatus.WORKING.value}
    ]
    queued = [
        item for item in assignments if item.get("status") == AssignmentStatus.QUEUED.value
    ]
    status_counts: dict[str, int] = {}
    for agent in agents:
        status = str(agent.get("status") or "idle")
        status_counts[status] = status_counts.get(status, 0) + 1

    return {
        "version": 1,
        "generated_at": utc_now_iso(),
        "started_at": started_at,
        "uptime_seconds": uptime_seconds,
        "auth": {
            "require_auth": settings.require_auth,
            "web_host": settings.web_host,
            "unsafe_bind_without_auth": (
                not settings.require_auth
                and settings.web_host not in {"127.0.0.1", "localhost", "::1"}
            ),
        },
        "mission": ops_room["mission"],
        "latest_reasoning": ops_room["latest_reasoning"],
        "agents": agents,
        "teams": ops_room["teams"],
        "tasks": {
            "active": active,
            "queued": queued,
            "blocked": blocked,
            "all": assignments,
            "history": dashboard["tasks"]["history"],
        },
        "counts": {
            "agents": len(agents),
            "active_tasks": len(active),
            "queued_tasks": len(queued),
            "blocked_tasks": len(blocked),
            "alerts": len(ops_room["alerts"]),
            "status_by_agent": status_counts,
        },
        "alerts": ops_room["alerts"],
        "datastores": [
            {"name": check.name, "ok": check.ok, "detail": check.detail}
            for check in datastore_checks
        ],
        "models": {
            "default_provider": settings.default_provider,
            "default_model": settings.default_model,
            "ollama_base_url": settings.ollama_base_url,
            "openai_configured": bool(settings.openai_api_key),
            "anthropic_configured": bool(settings.anthropic_api_key),
            "gemini_configured": bool(settings.gemini_api_key),
        },
        "usage": _usage_total(store.usage_records()),
        "financial_report": ops_room["financial_report"],
        "local_inference": ops_room["local_inference"],
        "cloud_jobs": ops_room["cloud_jobs"],
        "orchestrator": {
            "agent_id": "orchestrator",
            "display_name": "Orchestrator",
            "channel": "orchestrator",
        },
    }


def build_dashboard_payload_data(store: StateStore) -> dict[str, Any]:
    from brigade.tui import build_dashboard_payload

    return build_dashboard_payload(store)


def build_settings_payload(settings: Settings) -> dict[str, Any]:
    return {
        "config_path": str(settings.config_path),
        "config_hash": config_file_hash(settings.config_path),
        "data_dir": str(settings.data_dir),
        "log_level": settings.log_level,
        "orchestrator_cadence_seconds": settings.orchestrator_cadence_seconds,
        "stale_work_seconds": settings.stale_work_seconds,
        "require_auth": settings.require_auth,
        "jwt_issuer": settings.jwt_issuer,
        "jwt_audience": settings.jwt_audience,
        "jwt_secret": _redacted(settings.jwt_secret),
        "postgres_configured": bool(settings.postgres_dsn),
        "postgres_required": True,
        "store_backend": "PostgresStateStore" if settings.postgres_dsn else "unconfigured",
        "redis_configured": bool(settings.redis_url),
        "qdrant_configured": bool(settings.qdrant_url),
        "qdrant_collection": settings.qdrant_collection,
        "ollama_embedding_base_url": settings.ollama_embedding_base_url,
        "ollama_embedding_model": settings.ollama_embedding_model,
        "ollama_embedding_vector_size": settings.ollama_embedding_vector_size,
        "neo4j_configured": bool(settings.neo4j_http_url or settings.neo4j_uri),
        "web_host": settings.web_host,
        "web_port": settings.web_port,
        "default_provider": settings.default_provider,
        "default_model": settings.default_model,
        "ollama_base_url": settings.ollama_base_url,
        "secret_store_path": (
            str(settings.secret_store_path) if settings.secret_store_path else None
        ),
        "openai_auth_mode": settings.openai_auth_mode,
        "openai_configured": bool(settings.openai_api_key),
        "openai_api_key": _redacted(settings.openai_api_key),
        "openai_codex_auth_mode": settings.openai_codex_auth_mode,
        "anthropic_configured": bool(settings.anthropic_api_key),
        "anthropic_api_key": _redacted(settings.anthropic_api_key),
        "gemini_auth_mode": settings.gemini_auth_mode,
        "gemini_configured": bool(settings.gemini_api_key),
        "gemini_api_key": _redacted(settings.gemini_api_key),
        "telegram_webhook_enabled": settings.telegram_webhook_enabled,
        "telegram_configured": bool(settings.telegram_bot_token),
        "telegram_bot_token": _redacted(settings.telegram_bot_token),
        "telegram_webhook_secret": _redacted(settings.telegram_webhook_secret),
        "telegram_default_agent": settings.telegram_default_agent,
        "google_chat_webhook_enabled": settings.google_chat_webhook_enabled,
        "google_chat_configured": bool(settings.google_chat_secret),
        "google_chat_secret": _redacted(settings.google_chat_secret),
        "google_chat_default_agent": settings.google_chat_default_agent,
        "connector_rate_limit_count": settings.connector_rate_limit_count,
        "connector_rate_limit_window_seconds": settings.connector_rate_limit_window_seconds,
        "connector_max_inbound_chars": settings.connector_max_inbound_chars,
        "connector_max_outbound_chars": settings.connector_max_outbound_chars,
        "connector_max_body_bytes": settings.connector_max_body_bytes,
        "editable_keys": sorted(SAFE_CONFIG_KEYS),
    }


def set_config_value(
    config_path: Path,
    key: str,
    raw_value: str,
    *,
    base_hash: str | None = None,
) -> dict[str, Any]:
    if key not in SAFE_CONFIG_KEYS:
        raise ValueError(f"config key is not editable: {key}")
    current_hash = config_file_hash(config_path)
    if base_hash is not None and base_hash != current_hash:
        raise ValueError(
            "config changed since it was loaded; refresh settings and retry with the new base hash"
        )
    current = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    current[key] = _coerce_config_value(key, raw_value)
    config_path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "status": "updated",
        "path": str(config_path),
        "key": key,
        "value": current[key],
        "previous_hash": current_hash,
        "config_hash": config_file_hash(config_path),
    }


def config_file_hash(config_path: Path) -> str:
    if not config_path.exists():
        return "sha256:missing"
    digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
    return f"sha256:{digest}"


def _find_chat_by_idempotency(store: StateStore, key: str) -> ChatMessage | None:
    return next(
        (
            message
            for message in store.messages()
            if message.metadata.get("idempotency_key") == key
        ),
        None,
    )


def _acquire_chat_local_inference_lock(store: StateStore, holder: str) -> None:
    try:
        _acquire_local_inference_lock(store, holder)
        return
    except RuntimeError as exc:
        if "local inference unavailable until" not in str(exc):
            raise
    state = store.local_inference()
    if state.get("status") == "busy":
        raise RuntimeError(f"local inference already held by {state.get('holder')}")
    now = utc_now_iso()
    store.set_local_inference(
        {
            **state,
            "status": "idle",
            "holder": None,
            "next_available": now,
        }
    )
    _acquire_local_inference_lock(store, holder)


def _release_chat_local_inference_lock(store: StateStore, holder: str) -> None:
    release = getattr(store, "release_local_inference_lock", None)
    if callable(release):
        release(holder, cooldown_seconds=0)
        return
    state = store.local_inference()
    if state.get("holder") != holder:
        return
    completed_at = utc_now_iso()
    store.set_local_inference(
        {
            "status": "idle",
            "holder": None,
            "last_completed": completed_at,
            "next_available": completed_at,
        }
    )


def _active_assignment_by_agent(assignments: list[Assignment]) -> dict[str, Assignment]:
    active: dict[str, Assignment] = {}
    for assignment in sorted(assignments, key=lambda item: item.updated_at):
        if assignment.status in {
            AssignmentStatus.COMPLETE,
            AssignmentStatus.FAILED,
            AssignmentStatus.ABANDONED,
            AssignmentStatus.SUPERSEDED,
        }:
            continue
        active[assignment.assigned_to] = assignment
    return active


def _agent_room(
    agent: dict[str, Any],
    status: str,
    assignment: Assignment | None,
) -> dict[str, Any]:
    agent_id = str(agent.get("agent_id") or "")
    if agent_id == "orchestrator":
        return _room_projection("orchestrator", source="fixed", reason="fixed orchestrator room")
    if assignment is not None:
        explicit_room = (assignment.room_id or "").strip().lower()
        if explicit_room in _ROOM_IDS:
            return _room_projection(
                explicit_room,
                source="assignment",
                reason="task room",
                domain=explicit_room,
            )
        room_id, domain = _task_room_id(assignment, agent)
        return _room_projection(
            room_id,
            source="assignment",
            reason=f"task domain: {domain}",
            domain=domain,
        )
    if status in {"blocked", "awaiting_human", "reflecting", "ruminating", "dreaming"}:
        return _room_projection("barracks", source="status", reason=status, domain=status)
    return _room_projection(
        "breakroom",
        source="availability",
        reason="no active task",
        domain="idle",
    )


def _task_room_id(assignment: Assignment, agent: dict[str, Any]) -> tuple[str, str]:
    text = " ".join(
        str(value or "")
        for value in (
            assignment.assignment,
            assignment.goal_statement,
            assignment.assignment_rationale,
            agent.get("role"),
            agent.get("team_id"),
        )
    ).lower()
    for room_id, keywords in _ROOM_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return room_id, _room_domain(room_id, text)
    if assignment.status == AssignmentStatus.QUEUED:
        return "breakroom", "queued"
    return "cubicles", "operations"


def _room_domain(room_id: str, text: str) -> str:
    room = next((item for item in OPS_ROOM_ROOMS if item["id"] == room_id), None)
    domains = list(room.get("domains", []) if room else [])
    for domain in domains:
        if domain in text:
            return str(domain)
    return str(domains[0] if domains else room_id)


def _room_projection(
    room_id: str,
    *,
    source: str,
    reason: str,
    domain: str | None = None,
) -> dict[str, Any]:
    room = next((item for item in OPS_ROOM_ROOMS if item["id"] == room_id), OPS_ROOM_ROOMS[3])
    return {
        "id": room["id"],
        "label": room["label"],
        "source": source,
        "reason": reason,
        "domain": domain,
    }


def _agent_status(status: str | None, assignment: Assignment | None) -> str:
    if assignment is not None:
        if assignment.awaiting_human:
            return "awaiting_human"
        if assignment.status == AssignmentStatus.BLOCKED:
            return "blocked"
        if assignment.status == AssignmentStatus.QUEUED:
            return "queued"
        if assignment.status in {AssignmentStatus.ASSIGNED, AssignmentStatus.WORKING}:
            return "working"
    return status or "idle"


def _agent_activity(status: str, assignment: Assignment | None) -> str:
    if status == "awaiting_human":
        return "attention"
    if status == "blocked":
        return "blocked"
    if assignment and assignment.status == AssignmentStatus.QUEUED:
        return "queued"
    if status == "working":
        return "typing"
    return "idle"


def _usage_by_agent(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    usage: dict[str, dict[str, Any]] = {}
    for record in records:
        agent_id = record.get("agent_id")
        if not agent_id:
            continue
        bucket = usage.setdefault(str(agent_id), _empty_usage())
        bucket["input_tokens"] += int(record.get("input_tokens") or 0)
        bucket["output_tokens"] += int(record.get("output_tokens") or 0)
        bucket["total_tokens"] += int(record.get("total_tokens") or 0)
        bucket["estimated_cost_usd"] += float(record.get("estimated_cost_usd") or 0.0)
        recorded_at = record.get("recorded_at")
        last_recorded_at = bucket["last_recorded_at"]
        if recorded_at and (last_recorded_at is None or recorded_at > last_recorded_at):
            bucket["last_recorded_at"] = recorded_at
    return usage


def _empty_usage() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "estimated_cost_usd": 0.0,
        "last_recorded_at": None,
    }


def _usage_total(records: list[dict[str, Any]]) -> dict[str, Any]:
    total = _empty_usage()
    by_agent: dict[str, dict[str, Any]] = {}
    for agent_id, usage in _usage_by_agent(records).items():
        by_agent[agent_id] = usage
        total["input_tokens"] += usage["input_tokens"]
        total["output_tokens"] += usage["output_tokens"]
        total["total_tokens"] += usage["total_tokens"]
        total["estimated_cost_usd"] += usage["estimated_cost_usd"]
        last_recorded_at = total["last_recorded_at"]
        recorded_at = usage["last_recorded_at"]
        if recorded_at and (last_recorded_at is None or recorded_at > last_recorded_at):
            total["last_recorded_at"] = recorded_at
    return {**total, "by_agent": by_agent}


def _user_chat_metadata(actor: AuthResult, user: User | None) -> dict[str, Any]:
    effective_user = user or actor.user
    metadata: dict[str, Any] = {}
    if effective_user is not None:
        metadata["verified_user"] = effective_user.to_dict()
        metadata["identity_context"] = build_user_identity_context(effective_user)
    if actor.user is not None:
        metadata["actor"] = actor.user.to_dict()
        metadata["auth_method"] = actor.method
    return metadata


def _user_chat_prompt(
    display_name: str,
    agent_id: str,
    content: str,
    user: User | None,
) -> str:
    username = user.username if user else "operator"
    return "\n".join(
        [
            f"You are {display_name} ({agent_id}).",
            f"User {username} is chatting with you through OpenBrigade.",
            "Answer directly and concisely. If you need action, state the next concrete step.",
            "",
            "Message:",
            content,
        ]
    )


def _orchestrator_chat_prompt(store: StateStore, content: str, user: User | None) -> str:
    username = user.username if user else "operator"
    mission = store.mission()
    assignments = store.assignments()
    runnable_statuses = {
        AssignmentStatus.ASSIGNED,
        AssignmentStatus.WORKING,
        AssignmentStatus.QUEUED,
    }
    active = [
        item
        for item in assignments
        if item.status in runnable_statuses
    ]
    blocked = [
        item for item in assignments
        if item.status == AssignmentStatus.BLOCKED or item.awaiting_human or item.blockers
    ]
    latest_reasoning = store.orchestrator_reasoning()[-1:] or []
    context = [
        "You are the OpenBrigade orchestrator.",
        f"User {username} is asking for operational guidance.",
        "Answer directly and concisely. Prefer concrete next steps and mention uncertainty.",
        "",
        f"Mission: {mission.statement if mission else 'not set'}",
        f"Active tasks: {len(active)}",
        f"Blocked tasks: {len(blocked)}",
    ]
    if latest_reasoning:
        context.append(f"Latest reasoning: {latest_reasoning[0].get('decision_summary')}")
    context.extend(["", "Message:", content])
    return "\n".join(context)


def _coerce_config_value(key: str, raw_value: str) -> object:
    expected = SAFE_CONFIG_KEYS[key]
    if expected is bool:
        lowered = raw_value.lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"{key} expects a boolean")
    if expected is int:
        return int(raw_value)
    return raw_value


def _redacted(value: str | None) -> str | None:
    if not value:
        return None
    return "***redacted***"


def _summarize(value: str) -> str:
    stripped = " ".join(value.split())
    return stripped if len(stripped) <= 240 else stripped[:237].rstrip() + "..."
