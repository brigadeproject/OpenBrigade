from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from brigade.auth import AuthResult, build_user_identity_context
from brigade.config import Settings
from brigade.health import HealthCheck
from brigade.orchestrator import (
    build_orchestration_telemetry,
    orchestration_event,
    record_orchestration_events,
)
from brigade.providers import ModelProvider
from brigade.runner import _acquire_local_inference_lock
from brigade.schemas import (
    Assignment,
    AssignmentKind,
    AssignmentStatus,
    ChatMessage,
    Priority,
    Team,
    TERMINAL_STATUSES,
    User,
    WorkMode,
)
from brigade.store import StateStore
from brigade.time import utc_now_iso
from brigade.workspace import write_heartbeat_assignment

LOGGER = logging.getLogger("brigade.services")

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
    "proactive_mode": str,
    "proactive_creation_enabled": bool,
    "max_proactive_proposals_per_cycle": int,
    "max_proactive_creations_per_cycle": int,
    "require_auth": bool,
    "default_provider": str,
    "default_model": str,
    "ollama_base_url": str,
    "web_host": str,
    "web_port": int,
    "intake_mode": str,
    "max_intake_assignments_per_cycle": int,
    "intake_route_chief": str,
    "intake_default_priority": str,
    "rest_enabled": bool,
    "rest_window_start_utc": str,
    "rest_window_end_utc": str,
    "rest_idle_cycles_threshold": int,
    "rest_min_interval_seconds": int,
    "blocker_resolution_enabled": bool,
    "recurrence_detection_threshold": int,
    "recurrence_lookback_days": int,
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
            _user_chat_prompt(agent.display_name, agent.agent_id, content, user, store)
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


def decide_proposal(
    store: StateStore,
    *,
    proposal_id: str,
    decision: str,
    decided_by: str,
    reason: str | None = None,
) -> dict[str, Any]:
    if decision not in {"approved", "rejected"}:
        raise ValueError(f"unsupported proposal decision: {decision}")
    proposal = store.find_proposal(proposal_id)
    if proposal is None:
        raise ValueError(f"unknown proposal: {proposal_id}")
    if proposal.get("status") != "proposed":
        raise ValueError(f"proposal {proposal_id} is already {proposal.get('status')}")
    proposal["status"] = decision
    proposal["decided_by"] = decided_by
    proposal["decided_at"] = utc_now_iso()
    proposal["updated_at"] = proposal["decided_at"]
    if reason:
        proposal.setdefault("details", {})["decision_reason"] = reason
    if decision == "approved":
        effects = _apply_proposal_approval(store, proposal)
        if effects:
            proposal.setdefault("details", {})["approval_effects"] = effects
    store.update_proposal(proposal)
    mission = store.mission()
    record_orchestration_events(
        store,
        source="proposal_decision",
        decision_summary=f"proposal {proposal_id} {decision} by {decided_by}",
        mission_statement=mission.statement if mission else None,
        events=[
            orchestration_event(
                "proposal_decided",
                (
                    f"Proposal '{proposal.get('title')}' ({proposal.get('kind')}) "
                    f"was {decision} by {decided_by}."
                ),
                source="proposal_decision",
                decision=decision,
                status=decision,
                mission_statement=mission.statement if mission else None,
                agent_id=proposal.get("agent_id"),
                idempotency_key=proposal.get("idempotency_key"),
                payload=proposal,
            )
        ],
    )
    return proposal


def _apply_proposal_approval(
    store: StateStore,
    proposal: dict[str, Any],
) -> dict[str, Any]:
    """Materialize what an approved proposal promises.

    A ``tool_request`` becomes a ``kind=tool_build`` assignment for the
    requesting team's chief; an ``efficiency`` proposal becomes a recurrence
    record. ``rest_insight`` carries no side effect.
    """
    kind = proposal.get("kind")
    if kind == "tool_request":
        return _create_tool_build_assignment(store, proposal)
    if kind == "efficiency":
        return _create_recurrence_from_proposal(store, proposal)
    return {}


def _create_tool_build_assignment(
    store: StateStore,
    proposal: dict[str, Any],
) -> dict[str, Any]:
    from brigade.orchestrator import route_to_chief
    from brigade.schemas import AssignmentKind, Priority

    details = proposal.get("details") or {}
    tool_name = str(details.get("name") or proposal.get("title") or "tool")
    requesting_agent = proposal.get("agent_id")
    chief = route_to_chief(store, agent_id=requesting_agent)
    target = chief.agent_id if chief is not None else requesting_agent
    if not target:
        raise ValueError(
            f"tool request {proposal.get('proposal_id')} has no routable owner"
        )
    idempotency_key = f"tool-build:v1:{proposal.get('proposal_id')}"
    existing = store.find_assignment_by_idempotency_key(idempotency_key)
    if existing is not None:
        return {"assignment_id": existing.assignment_id}
    assignment = Assignment(
        assignment=(
            f"Build the workspace tool '{tool_name}'. "
            f"Purpose: {details.get('purpose') or 'not stated'}. "
            f"Spec: {details.get('spec') or 'not stated'}. "
            f"Produce an executable script at tools/{tool_name}, a descriptor "
            f"tools/{tool_name}.json with name, description, and argument "
            "schema, a usage note in TOOLS.md, and run the tool once as a "
            "smoke test."
        ),
        assigned_to=target,
        created_by="orchestrator",
        source="proposal_approval",
        kind=AssignmentKind.TOOL_BUILD,
        priority=Priority.NORMAL,
        assignment_rationale=(
            f"Approved tool request proposal {proposal.get('proposal_id')} "
            f"from {requesting_agent or 'unknown'}."
        ),
        created_by_role="orchestrator",
        idempotency_key=idempotency_key,
    )
    persisted = store.add_assignment(assignment)
    return {"assignment_id": persisted.assignment_id}


def _create_recurrence_from_proposal(
    store: StateStore,
    proposal: dict[str, Any],
) -> dict[str, Any]:
    from brigade.schemas import build_recurrence
    from brigade.time import add_seconds_iso

    details = proposal.get("details") or {}
    template = details.get("template")
    interval_seconds = int(details.get("interval_seconds") or 0)
    if not isinstance(template, dict) or interval_seconds <= 0:
        raise ValueError(
            f"efficiency proposal {proposal.get('proposal_id')} is missing "
            "a recurrence template or interval"
        )
    next_due_at = str(
        details.get("next_due_at") or add_seconds_iso(utc_now_iso(), interval_seconds)
    )
    recurrence = build_recurrence(
        template=template,
        interval_seconds=interval_seconds,
        next_due_at=next_due_at,
        proposal_id=str(proposal.get("proposal_id")),
    )
    persisted = store.add_recurrence(recurrence)
    return {"recurrence_id": persisted.get("recurrence_id")}


def _team_descendant_ids(teams: list[Team], team_id: str) -> list[str]:
    children = [team.team_id for team in teams if team.parent_team_id == team_id]
    descendants: list[str] = []
    for child in children:
        descendants.append(child)
        descendants.extend(_team_descendant_ids(teams, child))
    return descendants


def _chief_authorized_for_agent(
    teams: list[Team],
    chief_agent_id: str,
    target_agent_id: str,
) -> bool:
    chief_teams = [team for team in teams if team.crew_chief_id == chief_agent_id]
    if not chief_teams:
        return False
    for team in chief_teams:
        scoped_team_ids = {team.team_id, *_team_descendant_ids(teams, team.team_id)}
        for candidate in teams:
            if candidate.team_id in scoped_team_ids and target_agent_id in candidate.members:
                return True
    return False


def delegate_from_crew_chief(
    store: StateStore,
    *,
    team_id: str,
    chief_agent_id: str,
    target_agent_id: str,
    assignment_text: str,
    goal_statement: str | None,
    rationale: str | None,
    priority: Priority,
    current_user: User | None,
) -> dict[str, Any]:
    """Create a Crew Chief delegation assignment for a team member.

    Single source of truth for chief->member delegation, shared by the CLI
    (`team delegate`) and the web gateway (`POST /api/teams/{id}/delegate`).
    Raises ``ValueError`` for unknown team/agent and ``PermissionError`` when
    the actor is not the team's Crew Chief, the target is out of command scope,
    or a team only accepts orchestrator-issued work.
    """
    teams = store.teams()
    team = next((item for item in teams if item.team_id == team_id), None)
    if team is None:
        raise ValueError(f"unknown team: {team_id}")
    if team.crew_chief_id != chief_agent_id:
        raise PermissionError(f"{chief_agent_id} is not Crew Chief for team {team_id}")
    if team.delegation_policy == "orchestrator_only":
        raise PermissionError(f"team {team_id} only accepts orchestrator-issued work")
    agents = {agent.agent_id: agent for agent in store.agents()}
    if chief_agent_id not in agents:
        raise ValueError(f"unknown chief agent: {chief_agent_id}")
    target = agents.get(target_agent_id)
    if target is None:
        raise ValueError(f"unknown agent: {target_agent_id}")
    if not _chief_authorized_for_agent(teams, chief_agent_id, target_agent_id):
        raise PermissionError(f"{target_agent_id} is outside {chief_agent_id}'s command scope")
    target_team_id = target.team_id or next(
        (item.team_id for item in teams if target_agent_id in item.members), None
    )
    target_team = next((item for item in teams if item.team_id == target_team_id), None)
    if target_team and target_team.delegation_policy == "orchestrator_only":
        raise PermissionError(
            f"team {target_team.team_id} only accepts orchestrator-issued work"
        )

    assignment = Assignment(
        assignment=assignment_text,
        assigned_to=target_agent_id,
        created_by=chief_agent_id,
        source="crew_chief_delegate",
        priority=priority,
        work_mode=WorkMode.HEARTBEAT,
        goal_statement=goal_statement,
        assignment_rationale=rationale or "Crew Chief delegated team work.",
        created_by_user_id=current_user.username if current_user else None,
        created_by_role="crew_chief",
        idempotency_key=f"chief:{team_id}:{chief_agent_id}:{target_agent_id}:{assignment_text}",
    )
    persisted = store.add_assignment(assignment)
    created = persisted.assignment_id == assignment.assignment_id
    if created:
        now = utc_now_iso()
        store.add_orchestrator_reasoning(
            {
                "reasoning_id": str(uuid4()),
                "cycle_id": str(uuid4()),
                "started_at": now,
                "ended_at": now,
                "source": "crew_chief_delegate",
                "mission_statement": None,
                "queued_assignments": [],
                "assigned": [],
                "skipped": [],
                "alerts": [],
                "agent_states": {},
                "decision_summary": (
                    f"{chief_agent_id} delegated assignment {persisted.assignment_id} "
                    f"to {target_agent_id}"
                ),
                "events": [],
                "payload": {
                    "team_id": team_id,
                    "chief_agent_id": chief_agent_id,
                    "target_agent_id": target_agent_id,
                    "assignment_id": persisted.assignment_id,
                },
            }
        )
    return {
        "status": "queued" if created else "existing",
        "team_id": team_id,
        "chief_agent_id": chief_agent_id,
        "assignment": persisted.to_dict(),
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


def build_orchestration_payload(store: StateStore) -> dict[str, Any]:
    return build_orchestration_telemetry(store.orchestrator_reasoning())


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
    orchestration = build_orchestration_payload(store)
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
        "orchestration": orchestration,
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


class AssignmentActionError(RuntimeError):
    """A task cancel/reissue could not be applied as requested."""


def assignment_relations(store: StateStore, assignment_id: str) -> dict[str, Any]:
    """Active parent/children/dependents for an assignment (orphan safety).

    Children are found by reverse-scanning ``parent_assignment_id``; dependents
    are active assignments whose ``dependency_ids`` include this one.
    """
    assignments = store.assignments()
    target = next((a for a in assignments if a.assignment_id == assignment_id), None)
    parent = None
    if target is not None and target.parent_assignment_id:
        parent = next(
            (a for a in assignments if a.assignment_id == target.parent_assignment_id),
            None,
        )
    children = [a for a in assignments if a.parent_assignment_id == assignment_id]
    dependents = [a for a in assignments if assignment_id in (a.dependency_ids or [])]
    return {
        "target": target,
        "parent": parent,
        "children": children,
        "dependents": dependents,
    }


def active_blocking_relations(relations: dict[str, Any]) -> list[Assignment]:
    """Children + dependents still active — work that a kill would orphan/hang."""
    blocking = list(relations["children"]) + list(relations["dependents"])
    return [a for a in blocking if a.status not in TERMINAL_STATUSES]


def cancel_assignment(
    store: StateStore,
    assignment_id: str,
    *,
    reason: str = "cancelled by operator",
    by: str = "operator",
    force: bool = False,
) -> dict[str, Any]:
    """Cancel an assignment: move it to a terminal state and archive it.

    ``QUEUED`` work becomes ``SUPERSEDED``; in-flight/blocked work becomes
    ``ABANDONED``. Refuses (unless ``force``) when active children/dependents
    would be orphaned. Dependents are released from the cancelled dependency so
    they do not wait on it forever.
    """
    relations = assignment_relations(store, assignment_id)
    target = relations["target"]
    if target is None:
        raise AssignmentActionError(f"unknown assignment: {assignment_id}")
    if target.status in TERMINAL_STATUSES:
        raise AssignmentActionError(
            f"assignment {assignment_id} is already {target.status.value}"
        )
    blocking = active_blocking_relations(relations)
    if blocking and not force:
        ids = ", ".join(a.assignment_id for a in blocking)
        raise AssignmentActionError(
            f"assignment {assignment_id} has {len(blocking)} active child/dependent "
            f"task(s) ({ids}); cancelling would orphan them. Re-run with force to "
            "cancel anyway."
        )
    orphaned = [
        a.assignment_id
        for a in relations["children"]
        if a.status not in TERMINAL_STATUSES
    ]
    terminal = (
        AssignmentStatus.SUPERSEDED
        if target.status == AssignmentStatus.QUEUED
        else AssignmentStatus.ABANDONED
    )
    target.transition_to(terminal)
    summary = f"{reason} (by {by})"
    target.progress_summary = summary
    store.archive_assignment(target, summary)
    released: list[str] = []
    for dependent in relations["dependents"]:
        if assignment_id in (dependent.dependency_ids or []):
            dependent.dependency_ids = [
                dep for dep in dependent.dependency_ids if dep != assignment_id
            ]
            store.update_assignment(dependent)
            released.append(dependent.assignment_id)
    LOGGER.info(
        "assignment_cancelled",
        extra={
            "assignment_id": assignment_id,
            "status": terminal.value,
            "by": by,
            "orphaned_children": orphaned,
            "released_dependents": released,
        },
    )
    _record_operator_event(
        store,
        action="cancel",
        summary=summary,
        assignment_id=assignment_id,
        agent_id=target.assigned_to,
        by=by,
        payload={"status": terminal.value, "released_dependents": released},
    )
    return {
        "assignment_id": assignment_id,
        "status": terminal.value,
        "reason": summary,
        "orphaned_children": orphaned,
        "released_dependents": released,
    }


def reissue_assignment(
    store: StateStore,
    assignment_id: str,
    *,
    by: str = "operator",
) -> dict[str, Any]:
    """Reset a blocked assignment's failure state and re-dispatch it.

    Clears ``consecutive_failures``/blockers/``awaiting_human`` and transitions
    ``BLOCKED -> ASSIGNED`` (re-queued for its owner) with a fresh heartbeat.
    """
    target = store.find_assignment(assignment_id)
    if target is None:
        raise AssignmentActionError(f"unknown assignment: {assignment_id}")
    if target.status != AssignmentStatus.BLOCKED:
        raise AssignmentActionError(
            f"assignment {assignment_id} is {target.status.value}; only blocked "
            "assignments can be reissued"
        )
    target.consecutive_failures = 0
    target.blockers = []
    target.last_error = None
    target.awaiting_human = False
    target.checkpoint_at = None
    target.progress_summary = f"reissued by {by}"
    target.transition_to(AssignmentStatus.ASSIGNED)
    store.update_assignment(target)
    _rewrite_assignment_heartbeat(store, target)
    LOGGER.info(
        "assignment_reissued",
        extra={
            "assignment_id": assignment_id,
            "agent_id": target.assigned_to,
            "by": by,
        },
    )
    _record_operator_event(
        store,
        action="retry",
        summary=f"{assignment_id} retried (unblocked) by {by}",
        assignment_id=assignment_id,
        agent_id=target.assigned_to,
        by=by,
    )
    return {"assignment_id": assignment_id, "status": target.status.value}


def cancel_assignments_where(
    store: StateStore,
    *,
    status: str | None = None,
    blocker_contains: str | None = None,
    kind: str | None = None,
    reason: str = "bulk cancel by operator",
    by: str = "operator",
    force: bool = True,
) -> list[dict[str, Any]]:
    """Bulk-cancel active assignments matching a status/blocker/kind filter."""
    results: list[dict[str, Any]] = []
    for assignment in list(store.assignments()):
        if assignment.status in TERMINAL_STATUSES:
            continue
        if status is not None and assignment.status.value != status:
            continue
        if kind is not None and assignment.kind.value != kind:
            continue
        if blocker_contains is not None and not any(
            blocker_contains.lower() in (b or "").lower() for b in assignment.blockers
        ):
            continue
        try:
            results.append(
                cancel_assignment(
                    store,
                    assignment.assignment_id,
                    reason=reason,
                    by=by,
                    force=force,
                )
            )
        except AssignmentActionError as exc:
            results.append(
                {"assignment_id": assignment.assignment_id, "error": str(exc)}
            )
    return results


def _record_operator_event(
    store: StateStore,
    *,
    action: str,
    summary: str,
    assignment_id: str,
    agent_id: str | None = None,
    by: str = "operator",
    payload: dict[str, Any] | None = None,
) -> None:
    """Record a manual operator action in the orchestration audit stream."""
    mission = store.mission()
    record_orchestration_events(
        store,
        source="operator",
        decision_summary=summary,
        mission_statement=mission.statement if mission else None,
        events=[
            orchestration_event(
                f"operator_{action}",
                summary,
                source="operator",
                decision=action,
                assignment_id=assignment_id,
                agent_id=agent_id,
                payload={"by": by, "action": action, **(payload or {})},
            )
        ],
    )


def reissue_assignment_as_new(
    store: StateStore,
    assignment_id: str,
    *,
    by: str = "operator",
    note: str | None = None,
) -> dict[str, Any]:
    """Supersede a task and create a fresh QUEUED attempt with a NEW id.

    Task IDs are never reused: the original is archived (SUPERSEDED for queued
    work, ABANDONED otherwise) and a new assignment is created carrying
    ``reissued_from_assignment_id`` lineage. Dependents are re-pointed to the new
    attempt so the dependency chain stays intact.
    """
    original = store.find_assignment(assignment_id)
    if original is None:
        raise AssignmentActionError(f"unknown assignment: {assignment_id}")
    if original.status in TERMINAL_STATUSES:
        raise AssignmentActionError(
            f"assignment {assignment_id} is already {original.status.value}"
        )
    new_assignment = Assignment(
        assignment=original.assignment,
        assigned_to=original.assigned_to,
        created_by=by,
        source="manual_orchestration",
        priority=original.priority,
        work_mode=original.work_mode,
        kind=original.kind,
        goal_statement=original.goal_statement,
        assignment_rationale=(
            f"Reissued from {original.assignment_id} by {by}"
            + (f": {note}" if note else "")
        ),
        dependency_ids=list(original.dependency_ids or []),
        parent_assignment_id=original.parent_assignment_id,
        created_by_role="operator",
        reissued_from_assignment_id=original.assignment_id,
    )
    persisted = store.add_assignment(new_assignment)
    terminal = (
        AssignmentStatus.SUPERSEDED
        if original.status == AssignmentStatus.QUEUED
        else AssignmentStatus.ABANDONED
    )
    original.transition_to(terminal)
    original.progress_summary = f"superseded by {persisted.assignment_id} (by {by})"
    store.archive_assignment(original, original.progress_summary)
    repointed: list[str] = []
    for dependent in store.assignments():
        if assignment_id in (dependent.dependency_ids or []):
            dependent.dependency_ids = [
                persisted.assignment_id if dep == assignment_id else dep
                for dep in dependent.dependency_ids
            ]
            store.update_assignment(dependent)
            repointed.append(dependent.assignment_id)
    _record_operator_event(
        store,
        action="reissue",
        summary=(
            f"{assignment_id} reissued as {persisted.assignment_id}; original "
            f"{terminal.value} and its id retired (never reused)."
        ),
        assignment_id=persisted.assignment_id,
        agent_id=persisted.assigned_to,
        by=by,
        payload={"reissued_from": assignment_id, "original_status": terminal.value},
    )
    LOGGER.info(
        "assignment_reissued_as_new",
        extra={"from": assignment_id, "to": persisted.assignment_id, "by": by},
    )
    return {
        "assignment_id": persisted.assignment_id,
        "reissued_from_assignment_id": assignment_id,
        "original_status": terminal.value,
        "status": persisted.status.value,
        "repointed_dependents": repointed,
    }


def update_assignment_fields(
    store: StateStore,
    assignment_id: str,
    *,
    assignment_text: str | None = None,
    priority: str | None = None,
    assigned_to: str | None = None,
    by: str = "operator",
) -> dict[str, Any]:
    """Edit / reassign / reprioritize a non-running (queued or blocked) task."""
    target = store.find_assignment(assignment_id)
    if target is None:
        raise AssignmentActionError(f"unknown assignment: {assignment_id}")
    if target.status in TERMINAL_STATUSES:
        raise AssignmentActionError(
            f"assignment {assignment_id} is already {target.status.value}"
        )
    if target.status in {AssignmentStatus.ASSIGNED, AssignmentStatus.WORKING}:
        raise AssignmentActionError(
            f"assignment {assignment_id} is running; reissue or pause it before editing"
        )
    changes: list[str] = []
    previous_agent = target.assigned_to
    if assignment_text is not None and assignment_text.strip():
        target.assignment = assignment_text.strip()
        changes.append("text")
    if priority is not None:
        try:
            target.priority = Priority(priority)
        except ValueError as exc:
            raise AssignmentActionError(f"unsupported priority: {priority}") from exc
        changes.append(f"priority={priority}")
    reassigned = False
    if assigned_to is not None and assigned_to != target.assigned_to:
        if assigned_to not in {agent.agent_id for agent in store.agents()}:
            raise AssignmentActionError(f"unknown agent: {assigned_to}")
        target.assigned_to = assigned_to
        reassigned = True
        changes.append(f"agent={assigned_to}")
    if not changes:
        raise AssignmentActionError("no updatable fields provided")
    target.updated_at = utc_now_iso()
    store.update_assignment(target)
    if reassigned:
        _rewrite_assignment_heartbeat(store, target)
    _record_operator_event(
        store,
        action="reassign" if reassigned else "edit",
        summary=(
            f"{assignment_id} updated ({', '.join(changes)}) by {by}"
            + (f"; {previous_agent} -> {assigned_to}" if reassigned else "")
        ),
        assignment_id=assignment_id,
        agent_id=target.assigned_to,
        by=by,
        payload={"changes": changes, "previous_agent": previous_agent},
    )
    return {
        "assignment_id": assignment_id,
        "status": target.status.value,
        "changes": changes,
    }


def _rewrite_assignment_heartbeat(store: StateStore, assignment: Assignment) -> None:
    agent = next(
        (a for a in store.agents() if a.agent_id == assignment.assigned_to), None
    )
    if agent is None:
        return
    heartbeat = write_heartbeat_assignment(agent, assignment, store.data_dir)
    assignment.state_row_written_to = str(heartbeat)
    store.update_assignment(assignment)


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
        "orchestration": ops_room["orchestration"],
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
        "proactive_mode": settings.proactive_mode,
        "proactive_creation_enabled": settings.proactive_creation_enabled,
        "max_proactive_proposals_per_cycle": settings.max_proactive_proposals_per_cycle,
        "max_proactive_creations_per_cycle": settings.max_proactive_creations_per_cycle,
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
        if assignment.kind == AssignmentKind.REST:
            # Dreaming agents rest in the Barracks; the dream-protocol text
            # must never keyword-route into a work room.
            return _room_projection(
                "barracks",
                source="assignment",
                reason="rest cycle",
                domain="dreaming",
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
    store: StateStore | None = None,
) -> str:
    from brigade.prompt_floors import build_chat_status_context

    username = user.username if user else "operator"
    lines = [
        f"You are {display_name} ({agent_id}).",
        f"User {username} is chatting with you through OpenBrigade.",
        "Answer directly and concisely. If you need action, state the next concrete step.",
    ]
    if store is not None:
        lines.extend(
            [
                "",
                "Live status context (ground answers about current work, "
                "priorities, and blockers in this, not memory):",
                json.dumps(
                    build_chat_status_context(store, agent_id),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ]
        )
    lines.extend(["", "Message:", content])
    return "\n".join(lines)


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
