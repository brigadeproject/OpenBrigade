from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from brigade.meta import evaluate_assignment_alignment
from brigade.prompt_floors import (
    DEFAULT_STALE_WORK_SECONDS,
    IMBALANCED_QUEUE_DEPTH,
    ORCHESTRATOR_SYSTEM_PROMPT,
    build_orchestrator_floor,
    compact_json,
)
from brigade.providers import ModelProvider
from brigade.schemas import Agent, AgentState, Assignment, AssignmentStatus, Goal, Priority
from brigade.store import StateStore
from brigade.time import parse_utc_iso, utc_now, utc_now_iso
from brigade.workspace import write_heartbeat_assignment

PRIORITY_RANK = {
    Priority.URGENT: 0,
    Priority.HIGH: 1,
    Priority.NORMAL: 2,
    Priority.LOW: 3,
}
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CycleResult:
    assigned: list[Assignment]
    skipped: list[Assignment]
    alerts: list[str]


@dataclass(frozen=True)
class ParsedOrchestratorResponse:
    status: str
    summary: str
    actions: list[dict[str, Any]]


def deterministic_cycle(
    assignments: list[Assignment],
    agents: list[Agent] | None = None,
    goals_by_agent: dict[str, list[Goal]] | None = None,
    workspace_root: Path | None = None,
    assignment_history: list[dict[str, object]] | None = None,
) -> CycleResult:
    queued = [item for item in assignments if item.status == AssignmentStatus.QUEUED]
    ordered = sorted(
        queued,
        key=lambda item: (
            0 if item.created_by == "human" else 1,
            PRIORITY_RANK[item.priority],
            item.created_at,
        ),
    )

    assigned_agents: set[str] = {
        item.assigned_to
        for item in assignments
        if item.status in {AssignmentStatus.ASSIGNED, AssignmentStatus.WORKING}
    }
    agent_map = {agent.agent_id: agent for agent in agents or []}
    completed_assignment_ids = _completed_assignment_ids(assignments, assignment_history or [])
    assigned: list[Assignment] = []
    skipped: list[Assignment] = []
    alerts: list[str] = []
    for item in ordered:
        if item.assigned_to in assigned_agents:
            skipped.append(item)
            continue
        waiting_on = [
            dependency_id
            for dependency_id in item.dependency_ids
            if dependency_id not in completed_assignment_ids
        ]
        if waiting_on:
            item.progress_summary = "waiting on dependencies: " + ", ".join(waiting_on)
            LOGGER.info(
                "assignment_waiting_on_dependencies",
                extra={
                    "assignment_id": item.assignment_id,
                    "dependencies": waiting_on,
                },
            )
            skipped.append(item)
            continue
        if agents is not None and item.assigned_to not in agent_map:
            alerts.append(
                f"assignment {item.assignment_id} targets unknown agent {item.assigned_to}"
            )
            skipped.append(item)
            continue
        if goals_by_agent is not None and item.assigned_to in goals_by_agent:
            decision = evaluate_assignment_alignment(item, goals_by_agent.get(item.assigned_to, []))
            if decision.action == "interrupt":
                item.transition_to(AssignmentStatus.BLOCKED)
                item.awaiting_human = True
                item.progress_summary = decision.rationale
                alerts.append(f"assignment {item.assignment_id} interrupted: {decision.rationale}")
                skipped.append(item)
                continue
        item.transition_to(AssignmentStatus.ASSIGNED)
        if workspace_root is not None and item.assigned_to in agent_map:
            heartbeat = write_heartbeat_assignment(
                agent_map[item.assigned_to], item, workspace_root
            )
            item.state_row_written_to = str(heartbeat)
        assigned_agents.add(item.assigned_to)
        assigned.append(item)

    LOGGER.info(
        "orchestrator_cycle_completed",
        extra={
            "assigned": len(assigned),
            "skipped": len(skipped),
            "alerts": len(alerts),
        },
    )
    return CycleResult(assigned=assigned, skipped=skipped, alerts=alerts)


def evaluate_orchestrator_floor(
    store: StateStore,
    floor: dict[str, Any] | None = None,
    *,
    stale_seconds: int = DEFAULT_STALE_WORK_SECONDS,
) -> list[dict[str, Any]]:
    floor = floor or build_orchestrator_floor(store, stale_seconds=stale_seconds)
    triggers: list[dict[str, Any]] = []
    for goal in floor.get("goals", []):
        if goal.get("stale"):
            triggers.append(
                {
                    "kind": "stale_goal",
                    "severity": "warning",
                    "goal_id": goal.get("id"),
                    "agent_id": goal.get("agent_id"),
                    "summary": (
                        f"goal '{goal.get('title')}' has no fresh activity since "
                        f"{goal.get('last_activity')}"
                    ),
                    "goal": goal,
                }
            )

    now = utc_now()
    for assignment in store.assignments():
        if assignment.status not in {
            AssignmentStatus.ASSIGNED,
            AssignmentStatus.WORKING,
            AssignmentStatus.BLOCKED,
        }:
            continue
        if _has_future_checkpoint(assignment, now):
            continue
        age = (now - parse_utc_iso(assignment.updated_at)).total_seconds()
        if age <= stale_seconds:
            continue
        triggers.append(
            {
                "kind": "stale_task",
                "severity": "warning",
                "assignment_id": assignment.assignment_id,
                "agent_id": assignment.assigned_to,
                "summary": (
                    f"assignment {assignment.assignment_id} has not moved since "
                    f"{assignment.updated_at}"
                ),
                "assignment": assignment.to_dict(),
            }
        )

    loads = floor.get("crew_chief_load", [])
    idle_chiefs = [
        item
        for item in loads
        if item.get("state") == "idle" and int(item.get("queue_depth") or 0) == 0
    ]
    overloaded = [
        item
        for item in loads
        if int(item.get("queue_depth") or 0) >= IMBALANCED_QUEUE_DEPTH
    ]
    for item in overloaded:
        if not idle_chiefs:
            continue
        triggers.append(
            {
                "kind": "chief_load_imbalance",
                "severity": "info",
                "chief": item.get("chief"),
                "idle_chiefs": [chief.get("chief") for chief in idle_chiefs],
                "summary": (
                    f"chief {item.get('chief')} has queue depth "
                    f"{item.get('queue_depth')} while another chief is idle"
                ),
                "load": item,
            }
        )
    return triggers


def run_orchestrator_escalation(
    store: StateStore,
    provider: ModelProvider,
    *,
    floor: dict[str, Any] | None = None,
    triggers: list[dict[str, Any]] | None = None,
    stale_seconds: int = DEFAULT_STALE_WORK_SECONDS,
) -> dict[str, Any]:
    floor = floor or build_orchestrator_floor(store, stale_seconds=stale_seconds)
    triggers = triggers if triggers is not None else evaluate_orchestrator_floor(
        store,
        floor,
        stale_seconds=stale_seconds,
    )
    if not triggers:
        return {
            "status": "not_needed",
            "summary": "no stale-work or load-imbalance predicates fired",
            "triggers": [],
            "actions_applied": [],
            "actions_rejected": [],
        }

    prompt = build_orchestrator_escalation_prompt(store, floor, triggers)
    response = provider.complete(prompt)
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
            "source": "orchestrator_escalation",
        }
    )
    parsed = parse_orchestrator_response(response.text)
    action_result = apply_orchestrator_actions(store, parsed.actions)
    return {
        "status": parsed.status,
        "summary": parsed.summary,
        "triggers": triggers,
        "actions_applied": action_result["applied"],
        "actions_rejected": action_result["rejected"],
        "provider": response.provider,
        "model": response.model,
    }


def build_orchestrator_escalation_prompt(
    store: StateStore,
    floor: dict[str, Any],
    triggers: list[dict[str, Any]],
) -> str:
    context = {
        "floor": floor,
        "triggers": triggers,
        "targeted_provenance": _targeted_provenance(store, triggers),
        "knowledge_snippets": _targeted_knowledge_snippets(store, triggers),
    }
    return "\n".join(
        [
            ORCHESTRATOR_SYSTEM_PROMPT,
            "",
            "OpenBrigade orchestrator escalation protocol:",
            "Return only one JSON object. Do not wrap it in Markdown.",
            "Use this shape:",
            (
                '{"status":"actions|no_action|request_human|failed",'
                '"summary":"...","actions":[]}'
            ),
            "Allowed actions:",
            (
                '{"type":"create_assignment","agent_id":"...","assignment":"...",'
                '"goal_statement":"...","priority":"normal","rationale":"..."}'
            ),
            (
                '{"type":"rebalance_queued_assignment","assignment_id":"...",'
                '"to_agent_id":"...","rationale":"..."}'
            ),
            '{"type":"request_human","message":"..."}',
            "Do not move active assigned or working tasks.",
            "",
            "Context JSON:",
            compact_json(context),
        ]
    )


def parse_orchestrator_response(text: str) -> ParsedOrchestratorResponse:
    try:
        payload = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid orchestrator JSON response: {exc.msg}") from exc
    status = str(payload.get("status") or "").strip().lower()
    if status not in {"actions", "no_action", "request_human", "failed"}:
        raise ValueError(f"unsupported orchestrator status: {status or '<missing>'}")
    summary = str(payload.get("summary") or "").strip()
    if not summary:
        raise ValueError("orchestrator response is missing a summary")
    actions = payload.get("actions") or []
    if not isinstance(actions, list):
        raise ValueError("orchestrator actions must be a list")
    normalized = [item for item in actions if isinstance(item, dict)]
    if status == "request_human" and not normalized:
        normalized = [{"type": "request_human", "message": summary}]
    return ParsedOrchestratorResponse(status=status, summary=summary, actions=normalized)


def apply_orchestrator_actions(
    store: StateStore,
    actions: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    applied: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for action in actions:
        action_type = str(action.get("type") or "").strip()
        try:
            if action_type == "create_assignment":
                applied.append(_apply_create_assignment(store, action))
            elif action_type == "rebalance_queued_assignment":
                applied.append(_apply_rebalance_queued_assignment(store, action))
            elif action_type == "request_human":
                message = str(action.get("message") or "orchestrator requested human review")
                store.add_alert(message)
                applied.append({"type": action_type, "message": message})
            elif action_type in {"", "no_action"}:
                continue
            else:
                raise ValueError(f"unsupported action type: {action_type}")
        except ValueError as exc:
            record = {"action": action, "reason": str(exc)}
            rejected.append(record)
            store.add_alert(f"orchestrator action rejected: {exc}")
    return {"applied": applied, "rejected": rejected}


def _completed_assignment_ids(
    assignments: list[Assignment],
    assignment_history: list[dict[str, object]],
) -> set[str]:
    completed = {
        item.assignment_id
        for item in assignments
        if item.status == AssignmentStatus.COMPLETE
    }
    for item in assignment_history:
        if item.get("final_status") == AssignmentStatus.COMPLETE.value:
            assignment_id = item.get("assignment_id")
            if isinstance(assignment_id, str):
                completed.add(assignment_id)
    return completed


def _apply_create_assignment(store: StateStore, action: dict[str, Any]) -> dict[str, Any]:
    agent_id = str(action.get("agent_id") or "").strip()
    assignment_text = str(action.get("assignment") or "").strip()
    if not agent_id:
        raise ValueError("create_assignment is missing agent_id")
    if not assignment_text:
        raise ValueError("create_assignment is missing assignment")
    if agent_id not in {agent.agent_id for agent in store.agents()}:
        raise ValueError(f"create_assignment targets unknown agent {agent_id}")
    priority = _priority_from_action(action)
    idempotency_key = (
        "orchestrator-escalation:"
        f"{agent_id}:{assignment_text}:{action.get('goal_statement') or ''}"
    )
    if any(item.idempotency_key == idempotency_key for item in store.assignments()):
        return {
            "type": "create_assignment",
            "status": "skipped_existing",
            "agent_id": agent_id,
            "idempotency_key": idempotency_key,
        }
    assignment = Assignment(
        assignment=assignment_text,
        assigned_to=agent_id,
        created_by="orchestrator",
        source="orchestrator_escalation",
        priority=priority,
        goal_statement=(
            str(action.get("goal_statement")).strip()
            if action.get("goal_statement") is not None
            else None
        ),
        assignment_rationale=str(action.get("rationale") or "Orchestrator escalation."),
        created_by_role="orchestrator",
        idempotency_key=idempotency_key,
    )
    store.add_assignment(assignment)
    return {
        "type": "create_assignment",
        "status": "created",
        "assignment_id": assignment.assignment_id,
        "agent_id": agent_id,
    }


def _apply_rebalance_queued_assignment(
    store: StateStore,
    action: dict[str, Any],
) -> dict[str, Any]:
    assignment_id = str(action.get("assignment_id") or "").strip()
    to_agent_id = str(action.get("to_agent_id") or "").strip()
    if not assignment_id:
        raise ValueError("rebalance_queued_assignment is missing assignment_id")
    if not to_agent_id:
        raise ValueError("rebalance_queued_assignment is missing to_agent_id")
    if to_agent_id not in {agent.agent_id for agent in store.agents()}:
        raise ValueError(f"rebalance target is unknown agent {to_agent_id}")
    assignment = store.find_assignment(assignment_id)
    if assignment is None:
        raise ValueError(f"unknown assignment {assignment_id}")
    if assignment.status != AssignmentStatus.QUEUED:
        raise ValueError(f"assignment {assignment_id} is not queued")
    previous = assignment.assigned_to
    assignment.assigned_to = to_agent_id
    assignment.assignment_rationale = str(
        action.get("rationale") or "Rebalanced by orchestrator escalation."
    )
    assignment.updated_at = utc_now_iso()
    store.update_assignment(assignment)
    return {
        "type": "rebalance_queued_assignment",
        "status": "updated",
        "assignment_id": assignment_id,
        "from_agent_id": previous,
        "to_agent_id": to_agent_id,
    }


def _priority_from_action(action: dict[str, Any]) -> Priority:
    raw = str(action.get("priority") or Priority.NORMAL.value).strip().lower()
    try:
        return Priority(raw)
    except ValueError:
        return Priority.NORMAL


def _has_future_checkpoint(assignment: Assignment, now) -> bool:
    if not assignment.checkpoint_at:
        return False
    try:
        return parse_utc_iso(assignment.checkpoint_at) > now
    except ValueError:
        return False


def _targeted_provenance(
    store: StateStore,
    triggers: list[dict[str, Any]],
    *,
    limit: int = 12,
) -> list[dict[str, Any]]:
    targets = {
        str(value)
        for trigger in triggers
        for value in (
            trigger.get("assignment_id"),
            trigger.get("goal_id"),
            trigger.get("agent_id"),
            trigger.get("chief"),
        )
        if value
    }
    records = []
    for record in reversed(store.provenance_records()):
        encoded = json.dumps(record, sort_keys=True)
        if any(target in encoded for target in targets):
            records.append(record)
        if len(records) >= limit:
            break
    return records


def _targeted_knowledge_snippets(
    store: StateStore,
    triggers: list[dict[str, Any]],
    *,
    limit: int = 3,
) -> list[dict[str, str]]:
    terms = _trigger_terms(triggers)
    snippets: list[dict[str, str]] = []
    if terms:
        for point in store.search_episodes(" ".join(sorted(terms)), limit=limit):
            payload = point.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            text = "\n".join(
                str(payload.get(key) or "")
                for key in ("summary", "request", "response")
                if payload.get(key)
            )
            if not text.strip():
                continue
            snippets.append(
                {
                    "chunk_id": str(payload.get("episode_id") or ""),
                    "source": "qdrant_episode",
                    "text": text[:1200],
                }
            )
            if len(snippets) >= limit:
                return snippets
    for chunk in store.knowledge_chunks():
        text = str(chunk.get("text") or "")
        haystack = text.lower()
        if terms and not any(term in haystack for term in terms):
            continue
        snippets.append(
            {
                "chunk_id": str(chunk.get("chunk_id") or chunk.get("id") or ""),
                "source": str(chunk.get("source") or chunk.get("content_path") or ""),
                "text": text[:1200],
            }
        )
        if len(snippets) >= limit:
            break
    return snippets


def _trigger_terms(triggers: list[dict[str, Any]]) -> set[str]:
    terms: set[str] = set()
    for trigger in triggers:
        goal = trigger.get("goal")
        if isinstance(goal, dict):
            for word in str(goal.get("title") or "").lower().split():
                if len(word) >= 4:
                    terms.add(word.strip(".,:;!?()[]{}\"'"))
        assignment = trigger.get("assignment")
        if isinstance(assignment, dict):
            for word in str(assignment.get("assignment") or "").lower().split():
                if len(word) >= 4:
                    terms.add(word.strip(".,:;!?()[]{}\"'"))
    return {term for term in terms if term}


def derive_agent_states(
    agents: list[Agent],
    assignments: list[Assignment],
    existing: dict[str, AgentState] | None = None,
) -> dict[str, AgentState]:
    state_map: dict[str, AgentState] = {}
    existing = existing or {}
    active_by_agent: dict[str, list[Assignment]] = {}
    for assignment in assignments:
        if assignment.status in {
            AssignmentStatus.ASSIGNED,
            AssignmentStatus.WORKING,
            AssignmentStatus.BLOCKED,
        }:
            active_by_agent.setdefault(assignment.assigned_to, []).append(assignment)

    for agent in agents:
        previous = existing.get(agent.agent_id)
        active = sorted(
            active_by_agent.get(agent.agent_id, []),
            key=lambda item: item.updated_at,
            reverse=True,
        )
        current = active[0] if active else None
        if current is None:
            state_map[agent.agent_id] = AgentState(
                agent=agent.agent_id,
                status="idle",
                last_completed=previous.last_completed if previous else None,
            )
            continue
        if current.awaiting_human:
            status = "awaiting_human"
        elif current.status == AssignmentStatus.BLOCKED:
            status = "blocked"
        else:
            status = "working"
        state_map[agent.agent_id] = AgentState(
            agent=agent.agent_id,
            status=status,
            current_assignment_id=current.assignment_id,
            current_assignment_summary=current.assignment,
            assignment_progress=current.progress_summary,
            blockers=current.blockers,
            last_completed=previous.last_completed if previous else None,
            next_available=current.checkpoint_at or "after_current_assignment",
        )
    return state_map


def build_cycle_reasoning_record(
    mission_statement: str | None,
    assignments: list[Assignment],
    result: CycleResult,
    agent_states: dict[str, AgentState],
    previous_reasoning_id: str | None = None,
    floor: dict[str, Any] | None = None,
    floor_triggers: list[dict[str, Any]] | None = None,
    escalation: dict[str, Any] | None = None,
) -> dict[str, object]:
    return {
        "reasoning_id": str(uuid4()),
        "cycle_id": str(uuid4()),
        "started_at": utc_now_iso(),
        "ended_at": utc_now_iso(),
        "mission_statement": mission_statement,
        "previous_reasoning_id": previous_reasoning_id,
        "queued_assignments": [
            item.assignment_id for item in assignments if item.status == AssignmentStatus.QUEUED
        ],
        "assigned": [item.assignment_id for item in result.assigned],
        "skipped": [item.assignment_id for item in result.skipped],
        "alerts": result.alerts,
        "agent_states": {agent: state.to_dict() for agent, state in agent_states.items()},
        "floor": floor,
        "floor_triggers": floor_triggers or [],
        "escalation": escalation,
        "decision_summary": (
            "assigned="
            f"{len(result.assigned)} "
            "skipped="
            f"{len(result.skipped)} "
            "alerts="
            f"{len(result.alerts)}"
        ),
    }
