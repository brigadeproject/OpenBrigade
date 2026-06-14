"""Blocker-resolution ladder: solve blockers without the human first.

The ladder runs before any fresh dispatch each cycle and processes every
assignment that is ``blocked`` and not yet ``awaiting_human``. Steps key off
the existing ``consecutive_failures`` counter: retry, failure analysis,
reassign, and only then the human. Every step action carries an idempotency
key (``ladder:v1:<assignment_id>:<step>:<failures>``) so a step fires at most
once per failure increment, safely across daemon restarts.
"""

from __future__ import annotations

import logging
from typing import Any

from brigade.orchestrator import (
    _idempotency_seen,
    orchestration_event,
    route_to_chief,
)
from brigade.schemas import (
    Agent,
    Assignment,
    AssignmentKind,
    AssignmentStatus,
    Priority,
)
from brigade.store import StateStore
from brigade.time import utc_now_iso
from brigade.workspace import write_heartbeat_assignment

LOGGER = logging.getLogger("brigade.ladder")

LADDER_STEP_RETRY = "retry"
LADDER_STEP_ANALYSIS = "analysis"
LADDER_STEP_REASSIGN = "reassign"
LADDER_STEP_HUMAN = "human"
LADDER_WAITING_ANALYSIS = "waiting_analysis"

EVENT_LADDER_RETRY = "ladder_retry"
EVENT_LADDER_ANALYSIS_CREATED = "ladder_analysis_created"
EVENT_LADDER_REASSIGNED = "ladder_reassigned"
EVENT_LADDER_ESCALATED_HUMAN = "ladder_escalated_human"

LADDER_SOURCE = "orchestrator_ladder"

HUMAN_ESCALATION_FAILURES = 5
REASSIGN_FAILURES = 3
ANALYSIS_FAILURES = 2


def ladder_idempotency_key(assignment_id: str, step: str, failures: int) -> str:
    return f"ladder:v1:{assignment_id}:{step}:{failures}"


def ladder_state(
    store: StateStore,
    blocked: Assignment,
    assignments: list[Assignment] | None = None,
) -> str:
    """The next ladder step owed to a blocked assignment.

    ``waiting_analysis`` means the failure-analysis child exists but has not
    completed yet; the ladder holds until it does.
    """
    failures = blocked.consecutive_failures
    if failures >= HUMAN_ESCALATION_FAILURES:
        return LADDER_STEP_HUMAN
    if failures >= REASSIGN_FAILURES:
        # The ==2 step can be skipped when failures jump between cycles, so
        # the analysis is created here as a catch-up rather than deadlocking.
        child = find_analysis_child(assignments or store.assignments(), blocked)
        if child is None:
            return LADDER_STEP_ANALYSIS
        if child.status == AssignmentStatus.COMPLETE:
            return LADDER_STEP_REASSIGN
        return LADDER_WAITING_ANALYSIS
    if failures >= ANALYSIS_FAILURES:
        return LADDER_STEP_ANALYSIS
    return LADDER_STEP_RETRY


def find_analysis_child(
    assignments: list[Assignment],
    blocked: Assignment,
) -> Assignment | None:
    children = [
        item
        for item in assignments
        if item.parent_assignment_id == blocked.assignment_id
        and item.kind == AssignmentKind.FAILURE_ANALYSIS
    ]
    return sorted(children, key=lambda item: item.created_at)[-1] if children else None


def resolve_blockers(store: StateStore) -> dict[str, Any]:
    """Run one ladder pass over every blocked, non-awaiting-human assignment.

    Returns the ladder sub-result for the cycle reasoning record: applied
    ``actions``, assignments ``waiting`` on analysis children, idempotency
    ``suppressed`` duplicates, and the orchestration ``events`` emitted.
    """
    assignments = store.assignments()
    candidates = sorted(
        (
            item
            for item in assignments
            if item.status == AssignmentStatus.BLOCKED and not item.awaiting_human
        ),
        key=lambda item: item.created_at,
    )
    actions: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    for blocked in candidates:
        step = ladder_state(store, blocked, assignments)
        if step == LADDER_WAITING_ANALYSIS:
            child = find_analysis_child(assignments, blocked)
            waiting.append(
                {
                    "assignment_id": blocked.assignment_id,
                    "step": LADDER_WAITING_ANALYSIS,
                    "analysis_assignment_id": child.assignment_id if child else None,
                }
            )
            continue
        if step == LADDER_STEP_RETRY:
            result = retry_blocked(store, blocked)
        elif step == LADDER_STEP_ANALYSIS:
            result = create_failure_analysis(store, blocked)
        elif step == LADDER_STEP_REASSIGN:
            result = reassign_blocked(store, blocked, assignments=assignments)
        else:
            result = escalate_human(store, blocked)
        if result is None:
            suppressed.append(
                {
                    "assignment_id": blocked.assignment_id,
                    "step": step,
                    "idempotency_key": ladder_idempotency_key(
                        blocked.assignment_id, step, blocked.consecutive_failures
                    ),
                }
            )
            continue
        action, event = result
        actions.append(action)
        events.append(event)
        LOGGER.info(
            "ladder_step_applied",
            extra={"assignment_id": blocked.assignment_id, "step": step},
        )

    return {
        "enabled": True,
        "actions": actions,
        "waiting": waiting,
        "suppressed": suppressed,
        "events": events,
    }


def retry_blocked(
    store: StateStore,
    blocked: Assignment,
    *,
    source: str = LADDER_SOURCE,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Step 1: blocked -> assigned with a rewritten heartbeat block."""
    key = ladder_idempotency_key(
        blocked.assignment_id, LADDER_STEP_RETRY, blocked.consecutive_failures
    )
    if _idempotency_seen(store, key):
        return None
    blocked.transition_to(AssignmentStatus.ASSIGNED)
    blocked.progress_summary = (
        f"ladder retry after failure {blocked.consecutive_failures}: "
        f"{blocked.last_error or 'no error recorded'}"
    )
    store.update_assignment(blocked)
    _rewrite_heartbeat(store, blocked)
    action = {
        "step": LADDER_STEP_RETRY,
        "assignment_id": blocked.assignment_id,
        "agent_id": blocked.assigned_to,
        "idempotency_key": key,
        "status": "applied",
    }
    event = orchestration_event(
        EVENT_LADDER_RETRY,
        f"Ladder retried blocked assignment {blocked.assignment_id} "
        f"(failure {blocked.consecutive_failures}).",
        source=source,
        decision="retried",
        mission_statement=_mission_statement(store),
        assignment_id=blocked.assignment_id,
        agent_id=blocked.assigned_to,
        parent_assignment_id=blocked.parent_assignment_id,
        idempotency_key=key,
        payload=action,
    )
    return action, event


def create_failure_analysis(
    store: StateStore,
    blocked: Assignment,
    *,
    source: str = LADDER_SOURCE,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Step 2: a high-priority diagnosis child for the blocked agent's chief.

    The blocked task stays blocked until the analysis child completes.
    """
    key = ladder_idempotency_key(
        blocked.assignment_id, LADDER_STEP_ANALYSIS, blocked.consecutive_failures
    )
    if _idempotency_seen(store, key):
        return None
    if find_analysis_child(store.assignments(), blocked) is not None:
        return None
    chief = route_to_chief(store, agent_id=blocked.assigned_to)
    target = chief.agent_id if chief is not None else blocked.assigned_to
    child = Assignment(
        assignment=_failure_analysis_text(blocked),
        assigned_to=target,
        created_by="orchestrator",
        source=source,
        kind=AssignmentKind.FAILURE_ANALYSIS,
        priority=Priority.HIGH,
        parent_assignment_id=blocked.assignment_id,
        goal_statement=blocked.goal_statement,
        assignment_rationale=(
            f"Blocker-resolution ladder: {blocked.assignment_id} failed "
            f"{blocked.consecutive_failures} times; diagnose before reassignment."
        ),
        created_by_role="orchestrator",
        idempotency_key=key,
    )
    persisted = store.add_assignment(child)
    action = {
        "step": LADDER_STEP_ANALYSIS,
        "assignment_id": blocked.assignment_id,
        "analysis_assignment_id": persisted.assignment_id,
        "agent_id": target,
        "idempotency_key": key,
        "status": "applied",
    }
    event = orchestration_event(
        EVENT_LADDER_ANALYSIS_CREATED,
        f"Ladder created failure analysis {persisted.assignment_id} for blocked "
        f"assignment {blocked.assignment_id}.",
        source=source,
        decision="created",
        mission_statement=_mission_statement(store),
        assignment_id=persisted.assignment_id,
        agent_id=target,
        parent_assignment_id=blocked.assignment_id,
        child_assignment_ids=[persisted.assignment_id],
        idempotency_key=key,
        payload=action,
    )
    return action, event


def reassign_blocked(
    store: StateStore,
    blocked: Assignment,
    *,
    assignments: list[Assignment] | None = None,
    source: str = LADDER_SOURCE,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Step 3: deterministically pick a new owner once analysis completed.

    Precedence: idle teammate with a matching specialty, else any idle
    teammate, else the chief. The analysis summary lands in the rationale.
    """
    key = ladder_idempotency_key(
        blocked.assignment_id, LADDER_STEP_REASSIGN, blocked.consecutive_failures
    )
    if _idempotency_seen(store, key):
        return None
    assignments = assignments or store.assignments()
    analysis = find_analysis_child(assignments, blocked)
    analysis_summary = (
        analysis.progress_summary if analysis and analysis.progress_summary else None
    )
    target = _reassignment_target(store, blocked, assignments)
    previous = blocked.assigned_to
    if target is not None:
        blocked.assigned_to = target.agent_id
    blocked.transition_to(AssignmentStatus.ASSIGNED)
    blocked.assignment_rationale = (
        f"Ladder reassignment from {previous} after failure analysis. "
        f"Analysis summary: {analysis_summary or 'not available'}"
    )
    blocked.progress_summary = (
        f"reassigned from {previous} by the blocker-resolution ladder"
    )
    store.update_assignment(blocked)
    _rewrite_heartbeat(store, blocked)
    action = {
        "step": LADDER_STEP_REASSIGN,
        "assignment_id": blocked.assignment_id,
        "from_agent_id": previous,
        "agent_id": blocked.assigned_to,
        "analysis_assignment_id": analysis.assignment_id if analysis else None,
        "idempotency_key": key,
        "status": "applied",
    }
    event = orchestration_event(
        EVENT_LADDER_REASSIGNED,
        f"Ladder reassigned blocked assignment {blocked.assignment_id} from "
        f"{previous} to {blocked.assigned_to}.",
        source=source,
        decision="reassigned",
        mission_statement=_mission_statement(store),
        assignment_id=blocked.assignment_id,
        agent_id=blocked.assigned_to,
        parent_assignment_id=blocked.parent_assignment_id,
        child_assignment_ids=(
            [analysis.assignment_id] if analysis is not None else []
        ),
        idempotency_key=key,
        payload=action,
    )
    return action, event


def escalate_human(
    store: StateStore,
    blocked: Assignment,
    *,
    source: str = LADDER_SOURCE,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Step 4: mark awaiting_human and alert with the full ladder history.

    This is the only step that interrupts the human.
    """
    key = ladder_idempotency_key(
        blocked.assignment_id, LADDER_STEP_HUMAN, blocked.consecutive_failures
    )
    if _idempotency_seen(store, key):
        return None
    blocked.awaiting_human = True
    blocked.updated_at = utc_now_iso()
    store.update_assignment(blocked)
    history = _ladder_history(store, blocked)
    alert = (
        f"assignment {blocked.assignment_id} escalated to human after ladder "
        f"exhaustion ({blocked.consecutive_failures} failures). {history}"
    )
    store.add_alert(alert)
    action = {
        "step": LADDER_STEP_HUMAN,
        "assignment_id": blocked.assignment_id,
        "agent_id": blocked.assigned_to,
        "idempotency_key": key,
        "status": "applied",
        "alert": alert,
    }
    event = orchestration_event(
        EVENT_LADDER_ESCALATED_HUMAN,
        f"Ladder exhausted for assignment {blocked.assignment_id}; "
        "human attention requested.",
        source=source,
        decision="escalated_human",
        mission_statement=_mission_statement(store),
        assignment_id=blocked.assignment_id,
        agent_id=blocked.assigned_to,
        parent_assignment_id=blocked.parent_assignment_id,
        idempotency_key=key,
        payload=action,
    )
    return action, event


def _failure_analysis_text(blocked: Assignment) -> str:
    blockers = "; ".join(blocked.blockers) if blocked.blockers else "none recorded"
    return "\n".join(
        [
            (
                f"Failure analysis for blocked assignment {blocked.assignment_id} "
                f"owned by {blocked.assigned_to} "
                f"({blocked.consecutive_failures} consecutive failures)."
            ),
            f"Original assignment: {blocked.assignment}",
            f"Last error: {blocked.last_error or 'unknown'}",
            f"Blockers: {blockers}",
            f"Transcript: {blocked.transcript_path or 'not available'}",
            (
                "Diagnose the root cause and summarize what a new owner needs to "
                "know to finish the work. Your completion summary becomes the "
                "reassignment rationale."
            ),
        ]
    )


def _reassignment_target(
    store: StateStore,
    blocked: Assignment,
    assignments: list[Assignment],
) -> Agent | None:
    agent_map = {agent.agent_id: agent for agent in store.agents()}
    occupied: set[str] = {
        item.assigned_to
        for item in assignments
        if item.assignment_id != blocked.assignment_id
        and (
            item.status
            in {
                AssignmentStatus.ASSIGNED,
                AssignmentStatus.WORKING,
                AssignmentStatus.BLOCKED,
            }
            or (
                item.status == AssignmentStatus.QUEUED
                and item.kind != AssignmentKind.REST
            )
        )
    }
    teammate_ids: set[str] = set()
    for team in store.teams():
        roster = set(team.members)
        if team.crew_chief_id:
            roster.add(team.crew_chief_id)
        if blocked.assigned_to in roster:
            teammate_ids.update(roster)
    teammate_ids.discard(blocked.assigned_to)
    idle = [
        agent_map[agent_id]
        for agent_id in sorted(teammate_ids)
        if agent_id in agent_map and agent_id not in occupied
    ]
    text_tokens = set(blocked.assignment.lower().split())
    specialists = [
        agent
        for agent in idle
        if any(
            token in text_tokens
            for specialty in agent.specialties
            for token in specialty.lower().split()
        )
    ]
    if specialists:
        return specialists[0]
    if idle:
        return idle[0]
    return route_to_chief(store, agent_id=blocked.assigned_to)


def _ladder_history(store: StateStore, blocked: Assignment) -> str:
    steps: list[str] = []
    for record in store.orchestrator_reasoning():
        for event in record.get("events") or []:
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type") or "")
            if not event_type.startswith("ladder_"):
                continue
            provenance = event.get("provenance") or {}
            related = {
                provenance.get("assignment_id"),
                provenance.get("parent_assignment_id"),
            }
            if blocked.assignment_id in related:
                steps.append(event_type)
    history = " -> ".join(steps) if steps else "no prior ladder events recorded"
    return (
        f"Ladder history: {history}. "
        f"Last error: {blocked.last_error or 'unknown'}."
    )


def _rewrite_heartbeat(store: StateStore, assignment: Assignment) -> None:
    agent = next(
        (item for item in store.agents() if item.agent_id == assignment.assigned_to),
        None,
    )
    if agent is None:
        return
    heartbeat = write_heartbeat_assignment(agent, assignment, store.data_dir)
    assignment.state_row_written_to = str(heartbeat)
    store.update_assignment(assignment)


def _mission_statement(store: StateStore) -> str | None:
    mission = store.mission()
    return mission.statement if mission else None
