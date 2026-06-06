from __future__ import annotations

import hashlib
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
from brigade.schemas import (
    Agent,
    AgentState,
    Assignment,
    AssignmentStatus,
    Goal,
    Priority,
    WorkMode,
)
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
ORCHESTRATION_TELEMETRY_VERSION = 1
ORCHESTRATION_EVENT_VERSION = 1
MISSION_CONTINUATION_SOURCE = "orchestrator_mission_continuation"
MISSION_CONTINUATION_TRIGGER = "mission_idle_no_active_or_queued_work"


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


@dataclass(frozen=True)
class ProactiveContinuationConfig:
    mode: str = "propose"
    creation_enabled: bool = False
    max_proposals_per_cycle: int = 1
    max_creations_per_cycle: int = 1


def orchestration_event(
    event_type: str,
    summary: str,
    *,
    source: str,
    decision: str | None = None,
    status: str | None = None,
    mission_statement: str | None = None,
    goal_statement: str | None = None,
    trigger: str | None = None,
    assignment_id: str | None = None,
    assignment_ids: list[str] | None = None,
    agent_id: str | None = None,
    parent_assignment_id: str | None = None,
    child_assignment_ids: list[str] | None = None,
    idempotency_key: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    provenance = {
        "mission_statement": mission_statement,
        "goal_statement": goal_statement,
        "trigger": trigger,
        "assignment_id": assignment_id,
        "assignment_ids": list(assignment_ids or []),
        "agent_id": agent_id,
        "parent_assignment_id": parent_assignment_id,
        "child_assignment_ids": list(child_assignment_ids or []),
        "idempotency_key": idempotency_key,
        "source": source,
    }
    return {
        "event_id": str(uuid4()),
        "schema_version": ORCHESTRATION_EVENT_VERSION,
        "recorded_at": utc_now_iso(),
        "type": event_type,
        "decision": decision,
        "status": status,
        "summary": summary,
        "source": source,
        "provenance": {key: value for key, value in provenance.items() if value not in (None, [])},
        "payload": payload or {},
    }


def build_orchestration_reasoning_record(
    *,
    source: str,
    decision_summary: str,
    events: list[dict[str, Any]] | None = None,
    mission_statement: str | None = None,
    queued_assignments: list[str] | None = None,
    assigned: list[str] | None = None,
    skipped: list[str] | None = None,
    alerts: list[str] | None = None,
    payload: dict[str, Any] | None = None,
    previous_reasoning_id: str | None = None,
) -> dict[str, Any]:
    now = utc_now_iso()
    return {
        "reasoning_id": str(uuid4()),
        "cycle_id": str(uuid4()),
        "started_at": now,
        "ended_at": now,
        "source": source,
        "mission_statement": mission_statement,
        "previous_reasoning_id": previous_reasoning_id,
        "queued_assignments": list(queued_assignments or []),
        "assigned": list(assigned or []),
        "skipped": list(skipped or []),
        "alerts": list(alerts or []),
        "agent_states": {},
        "decision_summary": decision_summary,
        "events": list(events or []),
        "payload": payload or {},
    }


def record_orchestration_events(
    store: StateStore,
    *,
    source: str,
    decision_summary: str,
    events: list[dict[str, Any]],
    mission_statement: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    previous = store.orchestrator_reasoning()
    record = build_orchestration_reasoning_record(
        source=source,
        decision_summary=decision_summary,
        events=events,
        mission_statement=mission_statement,
        payload=payload,
        previous_reasoning_id=previous[-1].get("reasoning_id") if previous else None,
    )
    store.add_orchestrator_reasoning(record)
    return record


def build_orchestration_telemetry(
    reasoning_records: list[dict[str, Any]],
    *,
    limit: int = 40,
) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    for record in reasoning_records:
        raw_events = record.get("events")
        if isinstance(raw_events, list) and raw_events:
            events.extend(_normalize_orchestration_event(item, record) for item in raw_events)
        else:
            events.extend(_legacy_orchestration_events(record))

    events = [event for event in events if event.get("summary")]
    events.sort(key=lambda item: str(item.get("recorded_at") or ""))
    latest = list(reversed(events))[:limit]
    decisions = [
        event
        for event in latest
        if event.get("type") == "cycle_decision" or event.get("decision") is not None
    ]
    proposals = [
        event
        for event in latest
        if event.get("type") in {"proactive_proposal", "created_work"}
        or event.get("status") in {"proposed", "created"}
    ]
    return {
        "version": ORCHESTRATION_TELEMETRY_VERSION,
        "generated_at": utc_now_iso(),
        "latest_event": latest[0] if latest else None,
        "events": latest,
        "decisions": decisions,
        "proposals": proposals,
        "counts": _orchestration_event_counts(events),
    }


def evaluate_mission_continuation(
    store: StateStore,
    config: ProactiveContinuationConfig | None = None,
) -> dict[str, Any]:
    config = config or ProactiveContinuationConfig()
    mode = config.mode.strip().lower()
    if mode not in {"propose", "create", "off"}:
        mode = "propose"

    mission = store.mission()
    if mission is None:
        return _record_mission_continuation_skip(
            store,
            reason="no_mission",
            summary="No mission is set, so the Orchestrator cannot propose continuation work.",
            config=config,
        )

    crew_chiefs = _crew_chief_agents(store)
    if not crew_chiefs:
        return _record_mission_continuation_skip(
            store,
            reason="no_crew_chief",
            summary="No Crew Chief exists for the current mission.",
            config=config,
            mission_statement=mission.statement,
        )

    active_next_work = _active_or_queued_assignments(store.assignments())
    if active_next_work:
        return _record_mission_continuation_skip(
            store,
            reason="active_or_queued_work_exists",
            summary=(
                f"{len(active_next_work)} active or queued assignment(s) already exist, "
                "so no proactive continuation was proposed."
            ),
            config=config,
            mission_statement=mission.statement,
            payload={"assignment_ids": [item.assignment_id for item in active_next_work]},
        )

    if mode == "off":
        return _record_mission_continuation_skip(
            store,
            reason="proactive_mode_off",
            summary="Proactive mission continuation is disabled.",
            config=config,
            mission_statement=mission.statement,
        )

    if config.max_proposals_per_cycle <= 0:
        return _record_mission_continuation_skip(
            store,
            reason="proposal_cap_zero",
            summary="The proactive proposal cap is zero for this cycle.",
            config=config,
            mission_statement=mission.statement,
        )

    target = crew_chiefs[0]
    supported_goal = _supported_goal_for_chief(store, target.agent_id)
    proposed_assignment = _mission_continuation_assignment_text()
    idempotency_key = _mission_continuation_idempotency_key(
        mission_statement=mission.statement,
        goal_statement=supported_goal.statement if supported_goal else None,
        trigger=MISSION_CONTINUATION_TRIGGER,
        crew_chief_id=target.agent_id,
        assignment_text=proposed_assignment,
    )

    if _idempotency_seen(store, idempotency_key):
        return _record_mission_continuation_skip(
            store,
            reason="duplicate_idempotency_key",
            summary="A matching proactive continuation proposal or assignment already exists.",
            config=config,
            mission_statement=mission.statement,
            goal_statement=supported_goal.statement if supported_goal else None,
            agent_id=target.agent_id,
            idempotency_key=idempotency_key,
        )

    proposal = {
        "assignment": proposed_assignment,
        "assigned_to": target.agent_id,
        "goal_statement": supported_goal.statement if supported_goal else mission.statement,
        "trigger": MISSION_CONTINUATION_TRIGGER,
        "idempotency_key": idempotency_key,
        "mode": mode,
        "creation_enabled": config.creation_enabled,
    }
    events = [
        orchestration_event(
            "proactive_proposal",
            f"Proposed Crew Chief continuation work for {target.agent_id}.",
            source=MISSION_CONTINUATION_SOURCE,
            decision="proposed",
            status="proposed",
            mission_statement=mission.statement,
            goal_statement=supported_goal.statement if supported_goal else None,
            trigger=MISSION_CONTINUATION_TRIGGER,
            agent_id=target.agent_id,
            idempotency_key=idempotency_key,
            payload=proposal,
        )
    ]
    created: list[dict[str, Any]] = []
    creation_reasons: list[str] = []
    if mode == "create" and config.creation_enabled:
        if config.max_creations_per_cycle <= 0:
            creation_reasons.append("creation_cap_zero")
        else:
            assignment = Assignment(
                assignment=proposed_assignment,
                assigned_to=target.agent_id,
                created_by="orchestrator",
                source=MISSION_CONTINUATION_SOURCE,
                priority=Priority.NORMAL,
                work_mode=WorkMode.HEARTBEAT,
                goal_statement=supported_goal.statement if supported_goal else mission.statement,
                assignment_rationale=(
                    "The mission has no active or queued next work; Orchestrator generated "
                    "one Crew Chief-level continuation assignment."
                ),
                created_by_role="orchestrator",
                idempotency_key=idempotency_key,
            )
            persisted = store.add_assignment(assignment)
            if persisted.assignment_id == assignment.assignment_id:
                created.append(persisted.to_dict())
                events.append(
                    orchestration_event(
                        "created_work",
                        f"Created Crew Chief continuation assignment for {target.agent_id}.",
                        source=MISSION_CONTINUATION_SOURCE,
                        decision="created",
                        status="created",
                        mission_statement=mission.statement,
                        goal_statement=supported_goal.statement if supported_goal else None,
                        trigger=MISSION_CONTINUATION_TRIGGER,
                        assignment_id=persisted.assignment_id,
                        assignment_ids=[persisted.assignment_id],
                        agent_id=target.agent_id,
                        idempotency_key=idempotency_key,
                        payload=persisted.to_dict(),
                    )
                )
            else:
                creation_reasons.append("duplicate_assignment")
    else:
        creation_reasons.append("creation_disabled")

    decision_summary = (
        f"proposed=1 created={len(created)} trigger={MISSION_CONTINUATION_TRIGGER}"
    )
    record_orchestration_events(
        store,
        source=MISSION_CONTINUATION_SOURCE,
        decision_summary=decision_summary,
        events=events,
        mission_statement=mission.statement,
        payload={
            "proposal": proposal,
            "created": created,
            "creation_reasons": creation_reasons,
            "config": _proactive_config_payload(config),
        },
    )
    return {
        "status": "created" if created else "proposed",
        "proposal": proposal,
        "created": created,
        "skipped": [],
        "creation_reasons": creation_reasons,
    }


def _normalize_orchestration_event(
    raw_event: Any,
    record: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(raw_event, dict):
        raw_event = {"summary": str(raw_event)}
    provenance = (
        raw_event.get("provenance")
        if isinstance(raw_event.get("provenance"), dict)
        else {}
    )
    payload = raw_event.get("payload") if isinstance(raw_event.get("payload"), dict) else {}
    event_id = raw_event.get("event_id") or raw_event.get("id") or str(uuid4())
    return {
        "id": str(event_id),
        "schema_version": int(raw_event.get("schema_version") or ORCHESTRATION_EVENT_VERSION),
        "recorded_at": str(
            raw_event.get("recorded_at")
            or record.get("ended_at")
            or record.get("started_at")
            or ""
        ),
        "type": str(raw_event.get("type") or "reasoning_summary"),
        "decision": _optional_text(raw_event.get("decision")),
        "status": _optional_text(raw_event.get("status")),
        "summary": str(raw_event.get("summary") or record.get("decision_summary") or ""),
        "source": str(raw_event.get("source") or record.get("source") or "orchestrator"),
        "mission_statement": _optional_text(
            provenance.get("mission_statement") or raw_event.get("mission_statement")
            or record.get("mission_statement")
        ),
        "goal_statement": _optional_text(
            provenance.get("goal_statement") or raw_event.get("goal_statement")
        ),
        "trigger": _optional_text(provenance.get("trigger") or raw_event.get("trigger")),
        "assignment_id": _optional_text(
            provenance.get("assignment_id") or raw_event.get("assignment_id")
        ),
        "assignment_ids": _text_list(
            provenance.get("assignment_ids") or raw_event.get("assignment_ids")
        ),
        "agent_id": _optional_text(provenance.get("agent_id") or raw_event.get("agent_id")),
        "parent_assignment_id": _optional_text(
            provenance.get("parent_assignment_id") or raw_event.get("parent_assignment_id")
        ),
        "child_assignment_ids": _text_list(
            provenance.get("child_assignment_ids") or raw_event.get("child_assignment_ids")
        ),
        "idempotency_key": _optional_text(
            provenance.get("idempotency_key") or raw_event.get("idempotency_key")
        ),
        "payload": payload,
        "record_id": _optional_text(record.get("reasoning_id")),
        "cycle_id": _optional_text(record.get("cycle_id")),
    }


def _legacy_orchestration_events(record: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    assigned = _text_list(record.get("assigned"))
    skipped = _text_list(record.get("skipped"))
    source = str(record.get("source") or "legacy_reasoning")
    mission_statement = _optional_text(record.get("mission_statement"))
    for assignment_id in assigned:
        events.append(
            _normalize_orchestration_event(
                orchestration_event(
                    "cycle_decision",
                    f"Assigned queued assignment {assignment_id}.",
                    source=source,
                    decision="assigned",
                    status="assigned",
                    mission_statement=mission_statement,
                    assignment_id=assignment_id,
                    assignment_ids=[assignment_id],
                ),
                record,
            )
        )
    for assignment_id in skipped:
        events.append(
            _normalize_orchestration_event(
                orchestration_event(
                    "cycle_decision",
                    f"Skipped queued assignment {assignment_id}.",
                    source=source,
                    decision="skipped",
                    status="skipped",
                    mission_statement=mission_statement,
                    assignment_id=assignment_id,
                    assignment_ids=[assignment_id],
                ),
                record,
            )
        )
    if not events:
        summary = str(record.get("decision_summary") or "").strip()
        if summary:
            events.append(
                _normalize_orchestration_event(
                    orchestration_event(
                        "reasoning_summary",
                        summary,
                        source=source,
                        decision="no-action" if "assigned=0" in summary else None,
                        mission_statement=mission_statement,
                        payload={"legacy_record": True},
                    ),
                    record,
                )
            )
    return events


def _orchestration_event_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        event_type = str(event.get("type") or "unknown")
        counts[event_type] = counts.get(event_type, 0) + 1
        decision = event.get("decision")
        if decision:
            key = f"decision:{decision}"
            counts[key] = counts.get(key, 0) + 1
    return counts


def _record_mission_continuation_skip(
    store: StateStore,
    *,
    reason: str,
    summary: str,
    config: ProactiveContinuationConfig,
    mission_statement: str | None = None,
    goal_statement: str | None = None,
    agent_id: str | None = None,
    idempotency_key: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = orchestration_event(
        "proactive_skip",
        summary,
        source=MISSION_CONTINUATION_SOURCE,
        decision="skipped",
        status="skipped",
        mission_statement=mission_statement,
        goal_statement=goal_statement,
        trigger=reason,
        agent_id=agent_id,
        idempotency_key=idempotency_key,
        payload={
            "reason": reason,
            "config": _proactive_config_payload(config),
            **(payload or {}),
        },
    )
    record_orchestration_events(
        store,
        source=MISSION_CONTINUATION_SOURCE,
        decision_summary=f"proactive continuation skipped: {reason}",
        events=[event],
        mission_statement=mission_statement,
        payload=event["payload"],
    )
    return {
        "status": "skipped",
        "proposal": None,
        "created": [],
        "skipped": [{"reason": reason, "summary": summary}],
    }


def _active_or_queued_assignments(assignments: list[Assignment]) -> list[Assignment]:
    return [
        assignment
        for assignment in assignments
        if assignment.status
        in {
            AssignmentStatus.QUEUED,
            AssignmentStatus.ASSIGNED,
            AssignmentStatus.WORKING,
        }
    ]


def _crew_chief_agents(store: StateStore) -> list[Agent]:
    agents = {agent.agent_id: agent for agent in store.agents()}
    chief_ids = {
        team.crew_chief_id
        for team in store.teams()
        if team.crew_chief_id and team.crew_chief_id in agents
    }
    for agent in agents.values():
        if agent.role == "crew_chief":
            chief_ids.add(agent.agent_id)
    return [agents[agent_id] for agent_id in sorted(chief_ids)]


def _supported_goal_for_chief(store: StateStore, chief_id: str) -> Goal | None:
    goals = store.goals().get(chief_id, [])
    if not goals:
        return None
    confirmed = [goal for goal in goals if goal.human_confirmed]
    return sorted(confirmed or goals, key=lambda goal: (goal.set_at, goal.statement))[0]


def _mission_continuation_assignment_text() -> str:
    return (
        "Review the current mission, define the next Crew Chief coordination plan, "
        "and identify which existing team or agent should own each follow-up."
    )


def _mission_continuation_idempotency_key(
    *,
    mission_statement: str,
    goal_statement: str | None,
    trigger: str,
    crew_chief_id: str,
    assignment_text: str,
) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {
                "mission": _normalize_identity_text(mission_statement),
                "goal": _normalize_identity_text(goal_statement),
                "trigger": trigger,
                "crew_chief": crew_chief_id,
                "assignment": _normalize_identity_text(assignment_text),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"orchestrator-proactive:v1:{digest}"


def _idempotency_seen(store: StateStore, idempotency_key: str) -> bool:
    if store.find_assignment_by_idempotency_key(idempotency_key) is not None:
        return True
    for record in store.orchestrator_reasoning():
        try:
            encoded = json.dumps(record, sort_keys=True)
        except TypeError:
            encoded = str(record)
        if idempotency_key in encoded:
            return True
    return False


def _proactive_config_payload(config: ProactiveContinuationConfig) -> dict[str, Any]:
    return {
        "mode": config.mode,
        "creation_enabled": config.creation_enabled,
        "max_proposals_per_cycle": config.max_proposals_per_cycle,
        "max_creations_per_cycle": config.max_creations_per_cycle,
    }


def _normalize_identity_text(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(value.lower().split())


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


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


def build_idle_agent_assignments(store: StateStore) -> list[Assignment]:
    agents = store.agents()
    assignments = store.assignments()
    goals_by_agent = store.goals()
    mission = store.mission()
    occupied_agents = {
        item.assigned_to
        for item in assignments
        if item.status
        in {
            AssignmentStatus.QUEUED,
            AssignmentStatus.ASSIGNED,
            AssignmentStatus.WORKING,
            AssignmentStatus.BLOCKED,
        }
    }
    active_keys = {item.idempotency_key for item in assignments if item.idempotency_key}
    crew_chiefs = {team.crew_chief_id for team in store.teams() if team.crew_chief_id}
    created: list[Assignment] = []

    for agent in agents:
        if agent.agent_id in occupied_agents:
            continue
        goal = next(iter(goals_by_agent.get(agent.agent_id, [])), None)
        if goal is not None:
            key = f"orchestrator-idle-goal:{agent.agent_id}:{goal.statement}"
            if key in active_keys:
                continue
            assignment_text = f"Advance goal: {goal.statement}"
            rationale = "Agent was idle while a confirmed goal had no active task."
            goal_statement = goal.statement
        elif mission is not None and (agent.agent_id in crew_chiefs or agent.role == "crew_chief"):
            key = f"orchestrator-idle-mission:{agent.agent_id}:{mission.statement}"
            if key in active_keys:
                continue
            assignment_text = (
                "Build the next concrete task plan for the current mission and identify "
                "which agent should execute each step."
            )
            rationale = "Crew Chief was idle while the mission had no active planning task."
            goal_statement = mission.statement
        else:
            continue

        assignment = Assignment(
            assignment=assignment_text,
            assigned_to=agent.agent_id,
            created_by="orchestrator",
            source="orchestrator_idle_task_builder",
            priority=Priority.NORMAL,
            work_mode=WorkMode.HEARTBEAT,
            goal_statement=goal_statement,
            assignment_rationale=rationale,
            created_by_role="orchestrator",
            idempotency_key=key,
        )
        persisted = store.add_assignment(assignment)
        active_keys.add(key)
        if persisted.assignment_id == assignment.assignment_id:
            created.append(persisted)

    if created:
        events = [
            orchestration_event(
                "created_work",
                f"Created idle-agent assignment for {item.assigned_to}.",
                source="orchestrator_idle_task_builder",
                decision="created",
                status="created",
                mission_statement=mission.statement if mission else None,
                goal_statement=item.goal_statement,
                trigger="idle_agent_without_active_work",
                assignment_id=item.assignment_id,
                assignment_ids=[item.assignment_id],
                agent_id=item.assigned_to,
                idempotency_key=item.idempotency_key,
                payload=item.to_dict(),
            )
            for item in created
        ]
        store.add_orchestrator_reasoning(
            {
                "reasoning_id": str(uuid4()),
                "cycle_id": str(uuid4()),
                "started_at": utc_now_iso(),
                "ended_at": utc_now_iso(),
                "source": "orchestrator_idle_task_builder",
                "mission_statement": mission.statement if mission else None,
                "queued_assignments": [item.assignment_id for item in created],
                "assigned": [],
                "skipped": [],
                "alerts": [],
                "agent_states": {},
                "decision_summary": f"queued={len(created)} idle-agent assignments",
                "events": events,
                "payload": {"created": [item.to_dict() for item in created]},
            }
        )
    return created


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
    try:
        parsed = parse_orchestrator_response(response.text)
    except ValueError as exc:
        summary = f"orchestrator escalation ignored malformed model response: {exc}"
        LOGGER.warning(
            "orchestrator_escalation_malformed_response",
            extra={
                "provider": response.provider,
                "model": response.model,
                "reason": str(exc),
            },
        )
        return {
            "status": "no_action",
            "summary": summary,
            "triggers": triggers,
            "actions_applied": [],
            "actions_rejected": [],
            "provider": response.provider,
            "model": response.model,
        }
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
    persisted = store.add_assignment(assignment)
    if persisted.assignment_id != assignment.assignment_id:
        return {
            "type": "create_assignment",
            "status": "skipped_existing",
            "assignment_id": persisted.assignment_id,
            "agent_id": agent_id,
            "idempotency_key": idempotency_key,
        }
    return {
        "type": "create_assignment",
        "status": "created",
        "assignment_id": persisted.assignment_id,
        "agent_id": agent_id,
        "idempotency_key": idempotency_key,
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
    events = _cycle_decision_events(
        mission_statement,
        result,
        escalation=escalation,
        floor_triggers=floor_triggers or [],
    )
    return {
        "reasoning_id": str(uuid4()),
        "cycle_id": str(uuid4()),
        "started_at": utc_now_iso(),
        "ended_at": utc_now_iso(),
        "source": "orchestrator_cycle",
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
        "events": events,
        "decision_summary": (
            "assigned="
            f"{len(result.assigned)} "
            "skipped="
            f"{len(result.skipped)} "
            "alerts="
            f"{len(result.alerts)}"
        ),
    }


def _cycle_decision_events(
    mission_statement: str | None,
    result: CycleResult,
    *,
    escalation: dict[str, Any] | None,
    floor_triggers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for assignment in result.assigned:
        events.append(
            orchestration_event(
                "cycle_decision",
                (
                    f"Assigned queued assignment {assignment.assignment_id} "
                    f"to {assignment.assigned_to}."
                ),
                source="orchestrator_cycle",
                decision="assigned",
                status=assignment.status.value,
                mission_statement=mission_statement,
                goal_statement=assignment.goal_statement,
                assignment_id=assignment.assignment_id,
                assignment_ids=[assignment.assignment_id],
                agent_id=assignment.assigned_to,
                parent_assignment_id=assignment.parent_assignment_id,
                idempotency_key=assignment.idempotency_key,
                payload=assignment.to_dict(),
            )
        )
    for assignment in result.skipped:
        decision = "blocked" if assignment.status == AssignmentStatus.BLOCKED else "skipped"
        summary = (
            assignment.progress_summary
            if assignment.progress_summary
            else (
                f"Skipped queued assignment {assignment.assignment_id} "
                f"for {assignment.assigned_to}."
            )
        )
        events.append(
            orchestration_event(
                "cycle_decision",
                summary,
                source="orchestrator_cycle",
                decision=decision,
                status=assignment.status.value,
                mission_statement=mission_statement,
                goal_statement=assignment.goal_statement,
                assignment_id=assignment.assignment_id,
                assignment_ids=[assignment.assignment_id],
                agent_id=assignment.assigned_to,
                parent_assignment_id=assignment.parent_assignment_id,
                idempotency_key=assignment.idempotency_key,
                payload=assignment.to_dict(),
            )
        )
    if not result.assigned and not result.skipped:
        trigger = "idle_no_next_work" if not floor_triggers else "no_assignment_action"
        events.append(
            orchestration_event(
                "cycle_decision",
                "No queued assignment was assigned during this orchestrator cycle.",
                source="orchestrator_cycle",
                decision="no-action",
                status="no-action",
                mission_statement=mission_statement,
                trigger=trigger,
                payload={"floor_triggers": floor_triggers},
            )
        )
    if escalation:
        events.extend(_escalation_events(mission_statement, escalation))
    return events


def _escalation_events(
    mission_statement: str | None,
    escalation: dict[str, Any],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for action in escalation.get("actions_applied") or []:
        if not isinstance(action, dict):
            continue
        action_type = str(action.get("type") or "")
        if action_type == "create_assignment":
            status = str(action.get("status") or "")
            assignment_id = _optional_text(action.get("assignment_id"))
            events.append(
                orchestration_event(
                    "created_work" if status == "created" else "proactive_skip",
                    (
                        f"Escalation created assignment {assignment_id}."
                        if status == "created" and assignment_id
                        else "Escalation create-assignment action found existing work."
                    ),
                    source="orchestrator_escalation",
                    decision="created" if status == "created" else "skipped",
                    status=status or None,
                    mission_statement=mission_statement,
                    trigger="orchestrator_floor_trigger",
                    assignment_id=assignment_id,
                    assignment_ids=[assignment_id] if assignment_id else [],
                    agent_id=_optional_text(action.get("agent_id")),
                    idempotency_key=_optional_text(action.get("idempotency_key")),
                    payload=action,
                )
            )
        elif action_type == "request_human":
            events.append(
                orchestration_event(
                    "cycle_decision",
                    str(action.get("message") or "Orchestrator requested human review."),
                    source="orchestrator_escalation",
                    decision="blocked",
                    status="request_human",
                    mission_statement=mission_statement,
                    trigger="orchestrator_floor_trigger",
                    payload=action,
                )
            )
    for rejected in escalation.get("actions_rejected") or []:
        if isinstance(rejected, dict):
            events.append(
                orchestration_event(
                    "cycle_decision",
                    str(rejected.get("reason") or "Orchestrator action was rejected."),
                    source="orchestrator_escalation",
                    decision="blocked",
                    status="rejected",
                    mission_statement=mission_statement,
                    trigger="orchestrator_floor_trigger",
                    payload=rejected,
                )
            )
    return events
