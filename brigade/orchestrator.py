from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from brigade.config import Settings
from brigade.finance import persist_financial_report
from brigade.meta import evaluate_assignment_alignment
from brigade.prompt_floors import (
    DEFAULT_STALE_WORK_SECONDS,
    IMBALANCED_QUEUE_DEPTH,
    build_orchestrator_floor,
    compact_json,
    orchestrator_system_prompt,
)
from brigade.providers import ModelProvider
from brigade.schemas import (
    Agent,
    AgentState,
    Assignment,
    AssignmentKind,
    AssignmentStatus,
    Goal,
    GoalEngagementMode,
    Priority,
    TERMINAL_STATUSES,
    WorkMode,
    extract_json_object,
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
CYCLE_REASONING_RECORD_VERSION = 2
MISSION_CONTINUATION_SOURCE = "orchestrator_mission_continuation"
MISSION_CONTINUATION_TRIGGER = "mission_idle_no_active_or_queued_work"

# Machine-readable dispatch skip reasons recorded per assignment each cycle.
SKIP_AGENT_BUSY = "agent_busy"
SKIP_AGENT_BLOCKED = "agent_blocked"
SKIP_DEPENDENCIES_UNMET = "dependencies_unmet"
SKIP_UNKNOWN_AGENT = "unknown_agent"
SKIP_GOAL_MISALIGNED = "goal_misaligned"
SKIP_REST_DEFERRED = "rest_deferred"

# The no-work taxonomy. A cycle that takes no action must carry exactly one of these.
NO_WORK_REASONS = (
    "no_mission",
    "all_blocked_awaiting_human",
    "dependencies_unmet",
    "all_agents_busy",
    "provider_unavailable",
    "rest_window",
    "intake_only_pending_approval",
    "queue_empty_proposal_recorded",
    "duplicate_suppressed",
    "budget_gate",
    "unclassified",
)


@dataclass(frozen=True)
class CycleResult:
    assigned: list[Assignment]
    skipped: list[Assignment]
    alerts: list[str]
    skip_reasons: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CycleOutcome:
    mode: str  # worked | work_in_flight | no_work
    reason: str | None
    summary: str
    actions: list[dict[str, Any]] = field(default_factory=list)
    in_flight_assignment_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.mode not in {"worked", "work_in_flight", "no_work"}:
            raise ValueError(f"invalid cycle outcome mode: {self.mode}")
        if self.mode == "no_work":
            if self.reason not in NO_WORK_REASONS:
                raise ValueError(f"invalid no_work reason: {self.reason}")
        elif self.reason is not None:
            raise ValueError("cycle outcome reason is only valid for no_work")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "summary": self.summary,
            "actions": list(self.actions),
            "in_flight_assignment_ids": list(self.in_flight_assignment_ids),
        }


@dataclass(frozen=True)
class OrchestrationConfig:
    """Frozen per-cycle view of every gate the orchestrator honors."""

    cadence_seconds: int = 900
    stale_work_seconds: int = 86_400
    proactive_mode: str = "propose"
    proactive_creation_enabled: bool = False
    max_proactive_proposals_per_cycle: int = 1
    max_proactive_creations_per_cycle: int = 1
    intake_mode: str = "propose"
    max_intake_assignments_per_cycle: int = 2
    intake_route_chief: str | None = None
    intake_default_priority: str = "normal"
    rest_enabled: bool = True
    rest_window_start_utc: str = "03:00"
    rest_window_end_utc: str = "05:00"
    rest_idle_cycles_threshold: int = 6
    rest_min_interval_seconds: int = 86_400
    blocker_resolution_enabled: bool = True
    dispatch_starvation_alert_cycles: int = 4
    recurrence_detection_threshold: int = 3
    recurrence_lookback_days: int = 14
    hung_task_seconds: int = 1800
    auto_recover_enabled: bool = True
    max_auto_reissue: int = 2
    telegram_bot_token: str | None = None
    operator_telegram_chat_id: str | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> OrchestrationConfig:
        return cls(
            cadence_seconds=settings.orchestrator_cadence_seconds,
            stale_work_seconds=settings.stale_work_seconds,
            proactive_mode=settings.proactive_mode,
            proactive_creation_enabled=settings.proactive_creation_enabled,
            max_proactive_proposals_per_cycle=settings.max_proactive_proposals_per_cycle,
            max_proactive_creations_per_cycle=settings.max_proactive_creations_per_cycle,
            intake_mode=settings.intake_mode,
            max_intake_assignments_per_cycle=settings.max_intake_assignments_per_cycle,
            intake_route_chief=settings.intake_route_chief,
            intake_default_priority=settings.intake_default_priority,
            rest_enabled=settings.rest_enabled,
            rest_window_start_utc=settings.rest_window_start_utc,
            rest_window_end_utc=settings.rest_window_end_utc,
            rest_idle_cycles_threshold=settings.rest_idle_cycles_threshold,
            rest_min_interval_seconds=settings.rest_min_interval_seconds,
            blocker_resolution_enabled=settings.blocker_resolution_enabled,
            dispatch_starvation_alert_cycles=settings.dispatch_starvation_alert_cycles,
            recurrence_detection_threshold=settings.recurrence_detection_threshold,
            recurrence_lookback_days=settings.recurrence_lookback_days,
            hung_task_seconds=settings.hung_task_seconds,
            auto_recover_enabled=settings.auto_recover_enabled,
            max_auto_reissue=settings.max_auto_reissue,
            telegram_bot_token=settings.telegram_bot_token,
            operator_telegram_chat_id=settings.operator_telegram_chat_id,
        )

    # Keys an operator may override live (Redis-backed). Kept in sync with
    # services.RUNTIME_OVERRIDE_KEYS — only these fields are layered per cycle.
    RUNTIME_OVERRIDE_FIELDS = (
        "proactive_mode",
        "proactive_creation_enabled",
        "max_proactive_creations_per_cycle",
    )

    def with_overrides(self, overrides: Mapping[str, Any] | None) -> OrchestrationConfig:
        """Layer operator runtime overrides onto this config (live, no restart).

        Only ``RUNTIME_OVERRIDE_FIELDS`` are applied, and only when the value
        type matches the existing field — stale or malformed overrides are
        ignored so a bad write can never break a cycle.
        """
        if not overrides:
            return self
        updates: dict[str, Any] = {}
        for key in self.RUNTIME_OVERRIDE_FIELDS:
            if key not in overrides:
                continue
            value = overrides[key]
            if value is None:
                continue
            current = getattr(self, key)
            if isinstance(current, bool):
                if not isinstance(value, bool):
                    continue
            elif isinstance(current, int) and not isinstance(value, bool):
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    continue
            elif isinstance(current, str):
                value = str(value)
            updates[key] = value
        if not updates:
            return self
        return replace(self, **updates)

    def proactive(self) -> ProactiveContinuationConfig:
        return ProactiveContinuationConfig(
            mode=self.proactive_mode,
            creation_enabled=self.proactive_creation_enabled,
            max_proposals_per_cycle=self.max_proactive_proposals_per_cycle,
            max_creations_per_cycle=self.max_proactive_creations_per_cycle,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "cadence_seconds": self.cadence_seconds,
            "stale_work_seconds": self.stale_work_seconds,
            "proactive_mode": self.proactive_mode,
            "proactive_creation_enabled": self.proactive_creation_enabled,
            "intake_mode": self.intake_mode,
            "max_intake_assignments_per_cycle": self.max_intake_assignments_per_cycle,
            "intake_route_chief": self.intake_route_chief,
            "rest_enabled": self.rest_enabled,
            "rest_window_start_utc": self.rest_window_start_utc,
            "rest_window_end_utc": self.rest_window_end_utc,
            "rest_idle_cycles_threshold": self.rest_idle_cycles_threshold,
            "blocker_resolution_enabled": self.blocker_resolution_enabled,
            "dispatch_starvation_alert_cycles": self.dispatch_starvation_alert_cycles,
            "recurrence_detection_threshold": self.recurrence_detection_threshold,
            "recurrence_lookback_days": self.recurrence_lookback_days,
            "hung_task_seconds": self.hung_task_seconds,
            "auto_recover_enabled": self.auto_recover_enabled,
            "max_auto_reissue": self.max_auto_reissue,
            "operator_notify_configured": bool(
                self.telegram_bot_token and self.operator_telegram_chat_id
            ),
        }


@dataclass(frozen=True)
class FullCycleResult:
    outcome: CycleOutcome
    dispatch: CycleResult
    reasoning_record: dict[str, Any]
    sub_results: dict[str, Any]

    @property
    def assigned(self) -> list[Assignment]:
        return self.dispatch.assigned

    @property
    def skipped(self) -> list[Assignment]:
        return self.dispatch.skipped

    @property
    def alerts(self) -> list[str]:
        return self.dispatch.alerts


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
        if event.get("type")
        in {
            "proactive_proposal",
            "created_work",
            "intake_proposal",
            "proposal_created",
            "proposal_decided",
        }
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

    directive_chiefs = _directive_chiefs(store)
    if not directive_chiefs:
        return _record_mission_continuation_skip(
            store,
            reason="all_chiefs_on_call",
            summary=(
                "Every Crew Chief holds an on-call goal; continuation work is not "
                "synthesized for on-call teams."
            ),
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

    target = directive_chiefs[0]
    supported_goal = _supported_goal_for_chief(store, target.agent_id)
    proposed_assignment = _mission_continuation_assignment_text(
        team_of_one=is_team_of_one(store, target.agent_id)
    )
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


def route_to_chief(
    store: StateStore,
    *,
    agent_id: str | None = None,
    prefer_chief_id: str | None = None,
) -> Agent | None:
    """Resolve the crew chief that should own orchestrator-created work.

    Precedence: an explicitly preferred chief, the agent itself when it is a
    chief, the chief of the agent's team, then the first registered chief.
    Returns None when the brigade has no crew chief at all.
    """
    chiefs = {agent.agent_id: agent for agent in _crew_chief_agents(store)}
    if prefer_chief_id and prefer_chief_id in chiefs:
        return chiefs[prefer_chief_id]
    if agent_id:
        if agent_id in chiefs:
            return chiefs[agent_id]
        for team in store.teams():
            if agent_id in team.members and team.crew_chief_id in chiefs:
                return chiefs[team.crew_chief_id]
    return next(iter(chiefs.values()), None) if chiefs else None


ORCHESTRATOR_POLICY_ROUTING_RULE = "routing_rule"


def _active_policy_summaries(store: StateStore) -> list[str]:
    """Human-readable lines for every active policy, for prompt injection.

    Structured routing rules are rendered alongside freeform statements so
    the LLM sees (and can honor) the same standing directives the ladder
    enforces mechanically via policy_routed_chief_id."""
    return [
        str(item.get("statement") or "")
        for item in store.orchestrator_policies(active_only=True)
        if item.get("statement")
    ]


def policy_routed_chief_id(store: StateStore, assignment_kind: str) -> str | None:
    """The crew chief a routing policy assigns work of this kind to, if any.

    When more than one active policy targets the same assignment_kind, the
    most recently created one wins (list is created_at-ascending).
    """
    policies = store.orchestrator_policies(
        active_only=True,
        rule_kind=ORCHESTRATOR_POLICY_ROUTING_RULE,
        assignment_kind=assignment_kind,
    )
    if not policies:
        return None
    target_team_id = policies[-1].get("target_team_id")
    if not target_team_id:
        return None
    team = next((team for team in store.teams() if team.team_id == target_team_id), None)
    return team.crew_chief_id if team else None


def is_team_of_one(store: StateStore, chief_id: str) -> bool:
    """A chief whose managed roster is only themself is their own specialist."""
    managed = {chief_id}
    for team in store.teams():
        if team.crew_chief_id == chief_id:
            managed.update(team.members)
    return managed == {chief_id}


def _goal_engagement_mode(goal: Goal | None) -> str:
    if goal is None:
        return GoalEngagementMode.DIRECTIVE.value
    return goal.engagement_mode


def _directive_chiefs(store: StateStore) -> list[Agent]:
    """Chiefs eligible for continuation and idle synthesis: on-call chiefs are
    activated by intake, the ladder, or direct human assignment instead."""
    eligible = []
    for chief in _crew_chief_agents(store):
        goal = _supported_goal_for_chief(store, chief.agent_id)
        if _goal_engagement_mode(goal) == GoalEngagementMode.ON_CALL.value:
            continue
        eligible.append(chief)
    return eligible


def _supported_goal_for_chief(store: StateStore, chief_id: str) -> Goal | None:
    goals = store.goals().get(chief_id, [])
    if not goals:
        return None
    confirmed = [goal for goal in goals if goal.human_confirmed]
    return sorted(confirmed or goals, key=lambda goal: (goal.set_at, goal.statement))[0]


def _mission_continuation_assignment_text(*, team_of_one: bool = False) -> str:
    if team_of_one:
        return (
            "Review the current mission and define the next concrete step toward it. "
            "You are your own subject-matter expert: decompose the work into subtasks "
            "assigned to yourself with create_subtasks and work them in order."
        )
    return (
        "Review the current mission, define the next Crew Chief coordination plan, "
        "and decompose it into team subtasks with create_subtasks or delegate, "
        "routing each piece to the member whose specialties fit best."
    )


def _idle_replan_bucket() -> str:
    """Hour bucket appended to proactive idempotency keys.

    ``add_assignment`` dedupes keys against the full assignment history, so a
    fully deterministic key fires exactly once per mission — after the first
    planning task archives, idle agents can never receive proactive work
    again. The bucket bounds that dedupe window: within the same hour a
    completed key still suppresses re-creation (cooldown against re-plan
    churn), while the occupied-agent and active-work checks prevent overlap
    across buckets.
    """
    return utc_now().strftime("%Y-%m-%dT%H")


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
    return f"orchestrator-proactive:v1:{digest}:{_idle_replan_bucket()}"


# Skip reasons that indicate the queue is stuck rather than agents simply
# being busy with real work: pinned by a blocked assignment, waiting on
# dependencies that cannot dispatch, or targeting a nonexistent agent.
STARVATION_SKIP_REASONS = frozenset(
    {SKIP_AGENT_BLOCKED, SKIP_DEPENDENCIES_UNMET, SKIP_UNKNOWN_AGENT}
)


def _cycle_record_starved(record: dict[str, Any]) -> bool | None:
    """Whether a persisted reasoning record describes a starved dispatch.

    Returns None for records that are not orchestrator cycles (idle builder,
    delegation events, ...) so the streak walk skips over them.
    """
    if record.get("source") != "orchestrator_cycle":
        return None
    if record.get("assigned"):
        return False
    reasons = record.get("skip_reasons")
    if not isinstance(reasons, dict) or not reasons:
        return False
    return any(reason in STARVATION_SKIP_REASONS for reason in reasons.values())


def evaluate_dispatch_starvation(
    previous_reasoning: list[dict[str, Any]],
    dispatch: CycleResult,
    *,
    threshold: int,
) -> dict[str, Any]:
    """Detect N consecutive cycles that assigned nothing while queued work was
    stuck behind blocked agents, unmet dependencies, or unknown agents.

    The streak is derived statelessly from the persisted reasoning records, so
    it survives daemon restarts. Returns the streak plus an ``alert`` message
    when the threshold is crossed (re-raised every ``threshold`` cycles while
    the starvation persists). The 2026-07-04..06 ladder wedge starved dispatch
    for ~44 hours and produced no signal — this is that signal.
    """
    stuck_reasons = sorted(
        {
            reason
            for reason in dispatch.skip_reasons.values()
            if reason in STARVATION_SKIP_REASONS
        }
    )
    starved_now = not dispatch.assigned and bool(stuck_reasons)
    if not starved_now:
        return {"starved": False, "streak": 0, "alert": None}
    streak = 1
    for record in reversed(previous_reasoning):
        state = _cycle_record_starved(record)
        if state is None:
            continue
        if not state:
            break
        streak += 1
    alert = None
    if threshold > 0 and streak >= threshold and (streak - threshold) % threshold == 0:
        stuck_count = sum(
            1
            for reason in dispatch.skip_reasons.values()
            if reason in STARVATION_SKIP_REASONS
        )
        alert = (
            f"dispatch starvation: {streak} consecutive cycles assigned no work "
            f"while {stuck_count} queued assignment(s) were stuck "
            f"({', '.join(stuck_reasons)}). The queue is not draining - check "
            "blocked agents and dependency chains."
        )
    return {"starved": True, "streak": streak, "alert": alert}


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
            # Rest sorts last so it never preempts queued mission work.
            1 if item.kind == AssignmentKind.REST else 0,
            PRIORITY_RANK[item.priority],
            item.created_at,
        ),
    )

    now = utc_now()
    busy_agents: set[str] = {
        item.assigned_to
        for item in assignments
        if item.status in {AssignmentStatus.ASSIGNED, AssignmentStatus.WORKING}
    }
    # Blocked work counts as occupancy only while the blocker-resolution ladder is
    # actively working it. Once a blocked task is parked for a human
    # (``awaiting_human``) or backed off (a future ``checkpoint_at``), it must no
    # longer pin the agent — otherwise a single stuck/escalated task wedges every
    # other queued item for that agent indefinitely (the "all agents stuck" case).
    blocked_agents: set[str] = {
        item.assigned_to
        for item in assignments
        if item.status == AssignmentStatus.BLOCKED
        and not item.awaiting_human
        and not _has_future_checkpoint(item, now)
    }
    blocked_ids_by_agent: dict[str, set[str]] = {}
    for item in assignments:
        if item.status == AssignmentStatus.BLOCKED:
            blocked_ids_by_agent.setdefault(item.assigned_to, set()).add(
                item.assignment_id
            )
    queued_non_rest_agents: set[str] = {
        item.assigned_to for item in queued if item.kind != AssignmentKind.REST
    }
    agent_map = {agent.agent_id: agent for agent in agents or []}
    completed_assignment_ids = _completed_assignment_ids(assignments, assignment_history or [])
    assigned: list[Assignment] = []
    skipped: list[Assignment] = []
    alerts: list[str] = []
    skip_reasons: dict[str, str] = {}

    def skip(item: Assignment, reason: str) -> None:
        skip_reasons[item.assignment_id] = reason
        skipped.append(item)

    for item in ordered:
        if item.assigned_to in busy_agents:
            skip(item, SKIP_AGENT_BUSY)
            continue
        if item.assigned_to in blocked_agents:
            # A blocked task must not starve its own diagnosis: the ladder's
            # failure-analysis child may target the same agent (no separate
            # chief), and the blocked task itself is inert until the analysis
            # completes — so let that one assignment through.
            diagnoses_own_blocker = (
                item.kind == AssignmentKind.FAILURE_ANALYSIS
                and item.parent_assignment_id
                in blocked_ids_by_agent.get(item.assigned_to, set())
            )
            if not diagnoses_own_blocker:
                skip(item, SKIP_AGENT_BLOCKED)
                continue
        if item.kind == AssignmentKind.REST and item.assigned_to in queued_non_rest_agents:
            item.progress_summary = "rest deferred until queued mission work is dispatched"
            skip(item, SKIP_REST_DEFERRED)
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
            skip(item, SKIP_DEPENDENCIES_UNMET)
            continue
        if agents is not None and item.assigned_to not in agent_map:
            alerts.append(
                f"assignment {item.assignment_id} targets unknown agent {item.assigned_to}"
            )
            skip(item, SKIP_UNKNOWN_AGENT)
            continue
        if goals_by_agent is not None and item.assigned_to in goals_by_agent:
            decision = evaluate_assignment_alignment(item, goals_by_agent.get(item.assigned_to, []))
            if decision.action == "interrupt":
                item.transition_to(AssignmentStatus.BLOCKED)
                item.awaiting_human = True
                item.progress_summary = decision.rationale
                alerts.append(f"assignment {item.assignment_id} interrupted: {decision.rationale}")
                skip(item, SKIP_GOAL_MISALIGNED)
                continue
        item.transition_to(AssignmentStatus.ASSIGNED)
        if workspace_root is not None and item.assigned_to in agent_map:
            heartbeat = write_heartbeat_assignment(
                agent_map[item.assigned_to], item, workspace_root
            )
            item.state_row_written_to = str(heartbeat)
        busy_agents.add(item.assigned_to)
        assigned.append(item)

    LOGGER.info(
        "orchestrator_cycle_completed",
        extra={
            "assigned": len(assigned),
            "skipped": len(skipped),
            "alerts": len(alerts),
        },
    )
    return CycleResult(
        assigned=assigned,
        skipped=skipped,
        alerts=alerts,
        skip_reasons=skip_reasons,
    )


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
    crew_chiefs = {agent.agent_id for agent in _crew_chief_agents(store)}
    created: list[Assignment] = []

    for agent in agents:
        if agent.agent_id in occupied_agents:
            continue
        goal = next(
            (
                item
                for item in goals_by_agent.get(agent.agent_id, [])
                if item.engagement_mode != GoalEngagementMode.ON_CALL.value
            ),
            None,
        )
        if goal is not None:
            # Chief-first: a line worker's idle goal becomes high-level work for
            # their chief to decompose; chiefs receive it directly.
            target = route_to_chief(store, agent_id=agent.agent_id)
            if target is None:
                target = agent
            key = (
                f"orchestrator-idle-goal:{target.agent_id}:{goal.statement}"
                f":{_idle_replan_bucket()}"
            )
            if key in active_keys:
                continue
            if target.agent_id == agent.agent_id:
                if is_team_of_one(store, target.agent_id):
                    assignment_text = (
                        f"Advance goal: {goal.statement}. You are your own subject-matter "
                        "expert: decompose the work into subtasks assigned to yourself "
                        "with create_subtasks."
                    )
                else:
                    assignment_text = (
                        f"Advance goal: {goal.statement}. Decompose the work into team "
                        "subtasks with create_subtasks or delegate, routing each piece "
                        "to the member whose specialties fit best."
                    )
            else:
                assignment_text = (
                    f"Advance goal: {goal.statement}. {agent.agent_id} is idle and owns "
                    "this goal; decompose the next steps and route them to your team."
                )
            rationale = "Agent was idle while a confirmed goal had no active task."
            goal_statement = goal.statement
        elif mission is not None and agent.agent_id in crew_chiefs:
            chief_goal = _supported_goal_for_chief(store, agent.agent_id)
            if _goal_engagement_mode(chief_goal) == GoalEngagementMode.ON_CALL.value:
                continue
            target = agent
            key = (
                f"orchestrator-idle-mission:{agent.agent_id}:{mission.statement}"
                f":{_idle_replan_bucket()}"
            )
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
            assigned_to=target.agent_id,
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
            occupied_agents.add(target.agent_id)

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
        "actions_skipped": action_result.get("skipped", []),
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
        "active_policies": _active_policy_summaries(store),
    }
    return "\n".join(
        [
            orchestrator_system_prompt(store),
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
            '{"type":"retry_blocked_assignment","assignment_id":"..."}',
            '{"type":"create_failure_analysis","assignment_id":"..."}',
            '{"type":"reassign_blocked_assignment","assignment_id":"..."}',
            '{"type":"request_human","message":"...","assignment_id":"..."}',
            "Do not move active assigned or working tasks.",
            "Ladder actions must match the assignment's ladder state; "
            "request_human is rejected unless the target's ladder is exhausted.",
            "",
            "Context JSON:",
            compact_json(context),
        ]
    )


def parse_orchestrator_response(text: str) -> ParsedOrchestratorResponse:
    try:
        payload = json.loads(extract_json_object(text))
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


class StaleAssignmentTarget(ValueError):
    """An action targeted an assignment that no longer exists.

    Between the LLM building its escalation snapshot and the actions being
    applied, the deterministic cycle may have completed/superseded/archived the
    target. That is a benign race, not an operator-facing rejection — callers
    treat it as a silent skip instead of a surfaced ``cycle_decision``.
    """


def apply_orchestrator_actions(
    store: StateStore,
    actions: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    applied: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for action in actions:
        action_type = str(action.get("type") or "").strip()
        try:
            if action_type == "create_assignment":
                applied.append(_apply_create_assignment(store, action))
            elif action_type == "rebalance_queued_assignment":
                applied.append(_apply_rebalance_queued_assignment(store, action))
            elif action_type in LADDER_ACTION_STEPS:
                applied.append(_apply_ladder_action(store, action, action_type))
            elif action_type == "request_human":
                _validate_request_human(store, action)
                message = str(action.get("message") or "orchestrator requested human review")
                store.add_alert(message)
                applied.append({"type": action_type, "message": message})
            elif action_type in {"", "no_action"}:
                continue
            else:
                raise ValueError(f"unsupported action type: {action_type}")
        except StaleAssignmentTarget as exc:
            # The target resolved itself between snapshot and apply — a benign
            # race. Track it for debugging but do not surface it as a rejected
            # decision or raise an operator alert.
            skipped.append({"action": action, "reason": str(exc)})
            LOGGER.debug("orchestrator_action_skipped_stale_target", extra={"reason": str(exc)})
        except ValueError as exc:
            record = {"action": action, "reason": str(exc)}
            rejected.append(record)
            store.add_alert(f"orchestrator action rejected: {exc}")
    return {"applied": applied, "rejected": rejected, "skipped": skipped}


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
    # Chief-first: orchestrator-created work goes to the crew chief managing the
    # suggested agent; the chief decomposes. Falls back when no chief exists.
    chief = route_to_chief(store, agent_id=agent_id)
    if chief is not None and chief.agent_id != agent_id:
        action = {
            **action,
            "rationale": (
                f"{action.get('rationale') or 'Orchestrator escalation.'} "
                f"(routed to crew chief; suggested agent was {agent_id})"
            ),
        }
        agent_id = chief.agent_id
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
        raise StaleAssignmentTarget(f"unknown assignment {assignment_id}")
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


# Escalation action type -> the ladder step it is allowed to perform.
LADDER_ACTION_STEPS = {
    "retry_blocked_assignment": "retry",
    "create_failure_analysis": "analysis",
    "reassign_blocked_assignment": "reassign",
}


def _apply_ladder_action(
    store: StateStore,
    action: dict[str, Any],
    action_type: str,
) -> dict[str, Any]:
    """Apply an LLM-requested ladder step, validated against ladder state.

    Out-of-order actions are rejected; the deterministic ladder has already
    acted this cycle, so a duplicate request is a no-op, not an error.
    """
    from brigade.ladder import (
        create_failure_analysis,
        ladder_state,
        reassign_blocked,
        retry_blocked,
    )

    expected_step = LADDER_ACTION_STEPS[action_type]
    assignment_id = str(action.get("assignment_id") or "").strip()
    if not assignment_id:
        raise ValueError(f"{action_type} is missing assignment_id")
    assignment = store.find_assignment(assignment_id)
    if assignment is None:
        raise StaleAssignmentTarget(f"unknown assignment {assignment_id}")
    if assignment.status != AssignmentStatus.BLOCKED:
        raise ValueError(f"assignment {assignment_id} is not blocked")
    if assignment.awaiting_human:
        raise ValueError(f"assignment {assignment_id} is already awaiting the human")
    state = ladder_state(store, assignment)
    if state != expected_step:
        raise ValueError(
            f"{action_type} is out of ladder order for {assignment_id}; "
            f"the ladder state is {state}"
        )
    step_fn = {
        "retry": retry_blocked,
        "analysis": create_failure_analysis,
        "reassign": reassign_blocked,
    }[expected_step]
    result = step_fn(store, assignment, source="orchestrator_escalation")
    if result is None:
        return {
            "type": action_type,
            "status": "skipped_existing",
            "assignment_id": assignment_id,
        }
    step_action, event = result
    return {
        "type": action_type,
        "status": "applied",
        "event": event,
        **step_action,
    }


def _validate_request_human(store: StateStore, action: dict[str, Any]) -> None:
    """Human is last resort: reject unless the target's ladder is exhausted."""
    from brigade.ladder import HUMAN_ESCALATION_FAILURES

    assignment_id = str(action.get("assignment_id") or "").strip()
    if assignment_id:
        assignment = store.find_assignment(assignment_id)
        if assignment is None:
            raise StaleAssignmentTarget(f"unknown assignment {assignment_id}")
        if assignment.status == AssignmentStatus.BLOCKED and not (
            assignment.awaiting_human
            or assignment.consecutive_failures >= HUMAN_ESCALATION_FAILURES
        ):
            raise ValueError(
                f"request_human rejected: the ladder for {assignment_id} "
                "is not exhausted"
            )
        return
    in_progress = [
        item.assignment_id
        for item in store.assignments()
        if item.status == AssignmentStatus.BLOCKED
        and not item.awaiting_human
        and item.consecutive_failures < HUMAN_ESCALATION_FAILURES
    ]
    if in_progress:
        raise ValueError(
            "request_human rejected: the blocker-resolution ladder is still "
            "in progress for " + ", ".join(sorted(in_progress))
        )


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
                idle_cycles=(previous.idle_cycles if previous else 0) + 1,
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


def classify_cycle_outcome(
    *,
    mission_present: bool,
    assignments: list[Assignment],
    dispatch: CycleResult | None = None,
    continuation: dict[str, Any] | None = None,
    idle_synthesis: list[Assignment] | None = None,
    ladder: dict[str, Any] | None = None,
    intake: dict[str, Any] | None = None,
    recurrence: dict[str, Any] | None = None,
    rest: dict[str, Any] | None = None,
    escalation: dict[str, Any] | None = None,
    provider_failed: bool = False,
) -> CycleOutcome:
    """Enforce the work-or-reason invariant for one cycle.

    Either at least one action was taken (mode ``worked``), work is currently
    in flight (mode ``work_in_flight``), or the first matching reason from the
    no-work taxonomy is returned. ``unclassified`` is a bug, never a feature.
    """
    actions: list[dict[str, Any]] = []
    if dispatch is not None:
        actions.extend(
            {"type": "dispatched", "assignment_id": item.assignment_id}
            for item in dispatch.assigned
        )
    for item in idle_synthesis or []:
        actions.append({"type": "created", "assignment_id": item.assignment_id})
    for action in (ladder or {}).get("actions", []):
        actions.append({"type": f"ladder_{action.get('step', 'action')}", **action})
    for item in (intake or {}).get("created", []):
        actions.append({"type": "intake_created", "assignment_id": _record_id(item)})
    for item in (recurrence or {}).get("materialized", []):
        actions.append({"type": "recurrence_materialized", "assignment_id": _record_id(item)})
    for item in (continuation or {}).get("created", []):
        actions.append({"type": "continuation_created", "assignment_id": _record_id(item)})
    for item in (rest or {}).get("created", []):
        actions.append({"type": "rest_created", "assignment_id": _record_id(item)})
    for action in (escalation or {}).get("actions_applied", []):
        if isinstance(action, dict) and action.get("status") in {
            "created",
            "updated",
            "applied",
        }:
            actions.append({"type": "escalation_action", **action})

    if actions:
        return CycleOutcome(
            mode="worked",
            reason=None,
            summary=f"{len(actions)} action(s) taken this cycle.",
            actions=actions,
        )

    in_flight = [
        item.assignment_id
        for item in assignments
        if item.status == AssignmentStatus.WORKING
    ]
    if in_flight:
        return CycleOutcome(
            mode="work_in_flight",
            reason=None,
            summary=f"{len(in_flight)} assignment(s) currently working.",
            in_flight_assignment_ids=in_flight,
        )

    reason, summary = _no_work_reason(
        mission_present=mission_present,
        assignments=assignments,
        dispatch=dispatch,
        continuation=continuation,
        intake=intake,
        rest=rest,
        provider_failed=provider_failed,
    )
    return CycleOutcome(mode="no_work", reason=reason, summary=summary)


def _no_work_reason(
    *,
    mission_present: bool,
    assignments: list[Assignment],
    dispatch: CycleResult | None,
    continuation: dict[str, Any] | None,
    intake: dict[str, Any] | None,
    rest: dict[str, Any] | None,
    provider_failed: bool,
) -> tuple[str, str]:
    if not mission_present:
        return "no_mission", "No mission is set; nothing else runs."

    active = [
        item
        for item in assignments
        if item.status in {AssignmentStatus.ASSIGNED, AssignmentStatus.BLOCKED}
    ]
    if active and all(
        item.status == AssignmentStatus.BLOCKED and item.awaiting_human for item in active
    ):
        return (
            "all_blocked_awaiting_human",
            "Every active assignment is blocked awaiting the human; "
            "the ladder is exhausted everywhere.",
        )

    queued = [item for item in assignments if item.status == AssignmentStatus.QUEUED]
    skip_reasons = dispatch.skip_reasons if dispatch is not None else {}
    if queued:
        queued_reasons = {
            skip_reasons.get(item.assignment_id) for item in queued
        }
        if queued_reasons and queued_reasons <= {SKIP_DEPENDENCIES_UNMET}:
            return (
                "dependencies_unmet",
                "Queued work exists but every item is waiting on incomplete dependencies.",
            )
        if queued_reasons and None not in queued_reasons:
            return (
                "all_agents_busy",
                "Queued work exists but every eligible agent is occupied or blocked.",
            )

    if provider_failed:
        return (
            "provider_unavailable",
            "Creation or escalation was attempted but the model provider was unavailable.",
        )

    intake_proposals = (intake or {}).get("proposals", [])
    rest_suppressed = (rest or {}).get("already_rested", [])
    if rest_suppressed and not queued and not intake_proposals:
        return (
            "rest_window",
            "The only possible activity was rest and rest for this window already happened.",
        )

    if intake_proposals and (intake or {}).get("mode") == "propose":
        return (
            "intake_only_pending_approval",
            f"{len(intake_proposals)} intake proposal(s) recorded; intake_mode is propose.",
        )

    continuation_status = (continuation or {}).get("status")
    continuation_skips = (continuation or {}).get("skipped", [])
    skip_reason = continuation_skips[0].get("reason") if continuation_skips else None
    if skip_reason == "duplicate_idempotency_key":
        return (
            "duplicate_suppressed",
            "Every candidate action was suppressed by an existing idempotency key.",
        )
    if not queued and continuation_status == "proposed":
        return (
            "queue_empty_proposal_recorded",
            "The queue is empty; a continuation proposal was recorded but creation is gated off.",
        )
    if not queued and continuation_status == "skipped" and skip_reason:
        # The queue is empty and continuation deliberately stood down (mode off,
        # on-call chiefs, caps). The sub-result carries the precise reason.
        return (
            "queue_empty_proposal_recorded",
            f"The queue is empty; continuation stood down: {skip_reason}.",
        )

    return (
        "unclassified",
        "No action was taken and no taxonomy reason matched; this is a bug.",
    )


def _record_id(item: Any) -> str | None:
    if isinstance(item, Assignment):
        return item.assignment_id
    if isinstance(item, dict):
        return item.get("assignment_id")
    return None


def build_cycle_reasoning_record(
    mission_statement: str | None,
    assignments: list[Assignment],
    result: CycleResult,
    agent_states: dict[str, AgentState],
    previous_reasoning_id: str | None = None,
    floor: dict[str, Any] | None = None,
    floor_triggers: list[dict[str, Any]] | None = None,
    escalation: dict[str, Any] | None = None,
    *,
    cycle_outcome: CycleOutcome,
    sub_results: dict[str, Any] | None = None,
    config_snapshot: dict[str, Any] | None = None,
    extra_events: list[dict[str, Any]] | None = None,
) -> dict[str, object]:
    events = list(extra_events or [])
    events.extend(
        _cycle_decision_events(
            mission_statement,
            result,
            escalation=escalation,
            floor_triggers=floor_triggers or [],
        )
    )
    events.append(
        orchestration_event(
            "cycle_outcome",
            cycle_outcome.summary,
            source="orchestrator_cycle",
            decision=cycle_outcome.mode,
            status=cycle_outcome.reason or cycle_outcome.mode,
            mission_statement=mission_statement,
            trigger=cycle_outcome.reason,
            assignment_ids=cycle_outcome.in_flight_assignment_ids,
            payload=cycle_outcome.to_dict(),
        )
    )
    return {
        "reasoning_id": str(uuid4()),
        "cycle_id": str(uuid4()),
        "record_version": CYCLE_REASONING_RECORD_VERSION,
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
        "skip_reasons": dict(result.skip_reasons),
        "alerts": result.alerts,
        "agent_states": {agent: state.to_dict() for agent, state in agent_states.items()},
        "floor": floor,
        "floor_triggers": floor_triggers or [],
        "escalation": escalation,
        "cycle_outcome": cycle_outcome.to_dict(),
        "sub_results": sub_results or {},
        "config_snapshot": config_snapshot or {},
        "events": events,
        "decision_summary": (
            "assigned="
            f"{len(result.assigned)} "
            "skipped="
            f"{len(result.skipped)} "
            "alerts="
            f"{len(result.alerts)} "
            "outcome="
            f"{cycle_outcome.reason or cycle_outcome.mode}"
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


def recover_hung_tasks(store: StateStore, config: OrchestrationConfig) -> dict[str, Any]:
    """Detect and act on hung tasks (no progress past ``hung_task_seconds``).

    Hybrid by severity: a hung task with no related work (no parent, children, or
    dependents) is routed into the blocker-resolution ladder for automatic
    retry/reassignment; a hung task with related work is escalated to the operator
    (``awaiting_human``) rather than killed, so dependent work is never orphaned.
    Either way the owning agent is freed, because a parked/escalated blocked task no
    longer counts as occupancy in ``deterministic_cycle``.
    """
    if not config.auto_recover_enabled:
        return {"enabled": False, "actions": [], "events": []}
    now = utc_now()
    assignments = store.assignments()
    actions: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for assignment in assignments:
        if assignment.status not in {
            AssignmentStatus.ASSIGNED,
            AssignmentStatus.WORKING,
        }:
            continue
        if _has_future_checkpoint(assignment, now):
            continue
        try:
            age = (now - parse_utc_iso(assignment.updated_at)).total_seconds()
        except ValueError:
            continue
        if age <= config.hung_task_seconds:
            continue
        children = [
            item
            for item in assignments
            if item.parent_assignment_id == assignment.assignment_id
            and item.status not in TERMINAL_STATUSES
        ]
        dependents = [
            item
            for item in assignments
            if assignment.assignment_id in (item.dependency_ids or [])
            and item.status not in TERMINAL_STATUSES
        ]
        structural = bool(children or dependents or assignment.parent_assignment_id)
        error = (
            f"hung: no progress for {int(age)}s (threshold {config.hung_task_seconds}s)"
        )
        if structural:
            assignment.register_failure(
                error,
                blockers=[
                    f"hung task ({int(age)}s); structural — escalated to operator"
                ],
                awaiting_human=True,
            )
            store.update_assignment(assignment)
            store.add_alert(
                f"assignment {assignment.assignment_id} hung for {int(age)}s with "
                f"related work (children={len(children)}, dependents={len(dependents)}); "
                "escalated to operator instead of auto-killing."
            )
            decision = "escalated_operator"
            event_type = "hung_task_escalated"
            summary = (
                f"Hung assignment {assignment.assignment_id} ({assignment.assigned_to}) "
                f"idle {int(age)}s has related work; escalated to operator."
            )
        else:
            assignment.register_failure(error, blockers=[f"hung task ({int(age)}s)"])
            store.update_assignment(assignment)
            decision = "auto_recover"
            event_type = "hung_task_recovered"
            summary = (
                f"Hung assignment {assignment.assignment_id} ({assignment.assigned_to}) "
                f"idle {int(age)}s routed to the recovery ladder."
            )
        action = {
            "assignment_id": assignment.assignment_id,
            "agent_id": assignment.assigned_to,
            "age_seconds": int(age),
            "classification": "structural" if structural else "transient",
            "decision": decision,
        }
        actions.append(action)
        events.append(
            orchestration_event(
                event_type,
                summary,
                source="orchestrator_recovery",
                decision=decision,
                assignment_id=assignment.assignment_id,
                agent_id=assignment.assigned_to,
                parent_assignment_id=assignment.parent_assignment_id,
                payload=action,
            )
        )
    return {"enabled": True, "actions": actions, "events": events}


def _deliver_operator_notification(
    config: OrchestrationConfig, text: str
) -> dict[str, Any]:
    bot_token = config.telegram_bot_token
    chat_id = config.operator_telegram_chat_id
    if not bot_token or not chat_id:
        return {
            "channel": "none",
            "status": "skipped",
            "reason": "operator telegram not configured",
        }
    try:
        from brigade.connectors import send_telegram_message

        result = send_telegram_message(bot_token, chat_id=chat_id, text=text)
    except Exception as exc:  # pragma: no cover - defensive
        return {"channel": "telegram", "status": "failed", "reason": str(exc)}
    return {
        "channel": "telegram",
        "status": getattr(result, "status", "unknown"),
        "reason": getattr(result, "reason", None),
    }


def _notify_operator_escalations(
    store: StateStore, config: OrchestrationConfig
) -> dict[str, Any]:
    """Send one outbound operator notification per awaiting-human assignment.

    Covers every path that parks work for a human (ladder escalation, hung-task
    escalation, goal-misalignment interrupt). De-duped via an idempotency event so
    each assignment notifies the operator at most once.
    """
    notified: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for assignment in store.assignments():
        if not assignment.awaiting_human:
            continue
        key = f"operator-notify:v1:{assignment.assignment_id}"
        if _idempotency_seen(store, key):
            continue
        reason = (
            assignment.last_error
            or assignment.progress_summary
            or (assignment.blockers[0] if assignment.blockers else "see ladder history")
        )
        text = (
            "OpenBrigade needs an operator. Assignment "
            f"{assignment.assignment_id} (agent {assignment.assigned_to}) is awaiting "
            f"human attention. Reason: {reason}"
        )
        delivery = _deliver_operator_notification(config, text)
        notified.append(
            {"assignment_id": assignment.assignment_id, "delivery": delivery}
        )
        events.append(
            orchestration_event(
                "operator_escalation_notified",
                text,
                source="orchestrator_recovery",
                decision="escalated_operator",
                assignment_id=assignment.assignment_id,
                agent_id=assignment.assigned_to,
                parent_assignment_id=assignment.parent_assignment_id,
                idempotency_key=key,
                payload={"delivery": delivery},
            )
        )
    return {"notified": notified, "events": events}


def run_full_cycle(
    store: StateStore,
    provider: ModelProvider | None = None,
    config: OrchestrationConfig | None = None,
) -> FullCycleResult:
    """One full orchestrator cycle: blockers, intake, recurrences, continuation,
    rest, dispatch, escalation, then a reasoning record that cannot be persisted
    without a CycleOutcome."""
    config = config or OrchestrationConfig()
    empty_dispatch = CycleResult(assigned=[], skipped=[], alerts=[])
    sub_results: dict[str, Any] = {}

    # Step 1: mission and previous reasoning. No mission stops the cycle.
    mission = store.mission()
    previous_reasoning = store.orchestrator_reasoning()
    previous_reasoning_id = (
        previous_reasoning[-1].get("reasoning_id") if previous_reasoning else None
    )
    if mission is None:
        outcome = classify_cycle_outcome(mission_present=False, assignments=[])
        record = build_cycle_reasoning_record(
            None,
            [],
            empty_dispatch,
            {},
            previous_reasoning_id=previous_reasoning_id,
            cycle_outcome=outcome,
            sub_results=sub_results,
            config_snapshot=config.snapshot(),
        )
        store.add_orchestrator_reasoning(record)
        return FullCycleResult(
            outcome=outcome,
            dispatch=empty_dispatch,
            reasoning_record=record,
            sub_results=sub_results,
        )

    step_events: list[dict[str, Any]] = []

    def collect(name: str, result: dict[str, Any]) -> None:
        # Events land in the record's events list once, not inside sub_results.
        step_events.extend(result.get("events") or [])
        sub_results[name] = {
            key: value for key, value in result.items() if key != "events"
        }

    # Step 2.5: recover hung tasks before the ladder so freshly-blocked hung work
    # flows into the same cycle's ladder and dispatch.
    collect("hung_recovery", recover_hung_tasks(store, config))

    # Step 3: blocker-resolution ladder runs before any fresh dispatch.
    ladder_result: dict[str, Any] = {
        "enabled": config.blocker_resolution_enabled,
        "actions": [],
    }
    if config.blocker_resolution_enabled:
        ladder_result = _run_ladder_step(store, config)
    collect("ladder", ladder_result)

    # Step 3.5: notify the operator about any awaiting-human work (de-duped).
    collect("operator_escalation", _notify_operator_escalations(store, config))

    # Step 4: intake drain.
    intake_result = _run_intake_step(store, config)
    collect("intake", intake_result)

    # Step 5: recurrence materialization.
    recurrence_result = _run_recurrence_step(store, config)
    collect("recurrence", recurrence_result)

    # Step 6: mission continuation and idle synthesis (chief-first, on-call aware).
    continuation_result = evaluate_mission_continuation(store, config.proactive())
    sub_results["continuation"] = {
        "status": continuation_result.get("status"),
        "created": [
            _record_id(item) for item in continuation_result.get("created", [])
        ],
        "skipped": continuation_result.get("skipped", []),
    }
    idle_created: list[Assignment] = []
    if config.proactive_mode == "create" and config.proactive_creation_enabled:
        idle_created = build_idle_agent_assignments(store)
    sub_results["idle_synthesis"] = [item.assignment_id for item in idle_created]

    # Step 7: rest scheduling.
    rest_result = _run_rest_step(store, config)
    collect("rest", rest_result)

    # Step 9 prep: floor triggers and bounded LLM escalation.
    floor = build_orchestrator_floor(store, stale_seconds=config.stale_work_seconds)
    floor_triggers = evaluate_orchestrator_floor(
        store,
        floor,
        stale_seconds=config.stale_work_seconds,
    )
    escalation = None
    provider_failed = False
    if provider is not None and floor_triggers:
        try:
            escalation = run_orchestrator_escalation(
                store,
                provider,
                floor=floor,
                triggers=floor_triggers,
                stale_seconds=config.stale_work_seconds,
            )
        except Exception as exc:
            message = f"orchestrator escalation failed: {exc}"
            store.add_alert(message)
            provider_failed = True
            escalation = {
                "status": "failed",
                "summary": message,
                "triggers": floor_triggers,
                "actions_applied": [],
                "actions_rejected": [],
            }

    # Step 8: deterministic dispatch over the post-creation queue.
    assignments = store.assignments()
    original_assignments = {
        item.assignment_id: {
            "status": item.status.value,
            "updated_at": item.updated_at,
        }
        for item in assignments
    }
    agents = store.agents()
    existing_states = store.agent_states()
    dispatch = deterministic_cycle(
        assignments,
        agents=agents,
        goals_by_agent=store.goals(),
        workspace_root=store.data_dir,
        assignment_history=store.assignment_history(),
    )
    _persist_dispatch_mutations(store, assignments, dispatch, original_assignments)

    # Step 8.5: starvation watchdog — N consecutive zero-assignment cycles
    # with stuck queued work is an operator-visible incident, not business as
    # usual (the Jul 4-6 ladder wedge ran 44h before anyone noticed).
    starvation = evaluate_dispatch_starvation(
        previous_reasoning,
        dispatch,
        threshold=config.dispatch_starvation_alert_cycles,
    )
    sub_results["starvation"] = starvation
    if starvation["alert"]:
        store.add_alert(starvation["alert"])
        LOGGER.warning(
            "dispatch_starvation_detected",
            extra={
                "streak": starvation["streak"],
                "threshold": config.dispatch_starvation_alert_cycles,
            },
        )

    # Step 2/11: agent states with idle-cycle tracking.
    agent_states = derive_agent_states(agents, assignments, existing=existing_states)
    for state in agent_states.values():
        store.upsert_agent_state(state)

    # Step 10: cycle outcome classification. There is no third state.
    outcome = classify_cycle_outcome(
        mission_present=True,
        assignments=assignments,
        dispatch=dispatch,
        continuation=continuation_result,
        idle_synthesis=idle_created,
        ladder=ladder_result,
        intake=intake_result,
        recurrence=recurrence_result,
        rest=rest_result,
        escalation=escalation,
        provider_failed=provider_failed,
    )

    # Step 11: persist the reasoning record, alerts, and the financial report.
    record = build_cycle_reasoning_record(
        mission.statement,
        assignments,
        dispatch,
        agent_states,
        previous_reasoning_id=previous_reasoning_id,
        floor=floor,
        floor_triggers=floor_triggers,
        escalation=escalation,
        cycle_outcome=outcome,
        sub_results=sub_results,
        config_snapshot=config.snapshot(),
        extra_events=step_events,
    )
    store.add_orchestrator_reasoning(record)
    for alert in dispatch.alerts:
        store.add_alert(alert)
    if outcome.reason == "unclassified":
        store.add_alert(
            "orchestrator cycle outcome was unclassified; "
            "the work-or-reason invariant has a gap"
        )
    persist_financial_report(store, store.data_dir)
    return FullCycleResult(
        outcome=outcome,
        dispatch=dispatch,
        reasoning_record=record,
        sub_results=sub_results,
    )


def _run_ladder_step(store: StateStore, config: OrchestrationConfig) -> dict[str, Any]:
    # Imported here: brigade.ladder imports orchestrator helpers at module level.
    from brigade.ladder import resolve_blockers

    del config
    return resolve_blockers(store)


def _run_intake_step(store: StateStore, config: OrchestrationConfig) -> dict[str, Any]:
    # Imported here: brigade.intake imports orchestrator helpers at module level.
    from brigade.intake import evaluate_intake_queue

    return evaluate_intake_queue(
        store,
        mode=config.intake_mode,
        max_per_cycle=config.max_intake_assignments_per_cycle,
        route_chief=config.intake_route_chief,
        default_priority=config.intake_default_priority,
    )


def _run_recurrence_step(store: StateStore, config: OrchestrationConfig) -> dict[str, Any]:
    # Imported here: brigade.efficiency imports orchestrator helpers at module level.
    from brigade.efficiency import run_recurrence_step

    return run_recurrence_step(
        store,
        threshold=config.recurrence_detection_threshold,
        lookback_days=config.recurrence_lookback_days,
    )


def _run_rest_step(store: StateStore, config: OrchestrationConfig) -> dict[str, Any]:
    # Imported here: brigade.rest imports orchestrator helpers at module level.
    from brigade.rest import evaluate_rest_schedule

    return evaluate_rest_schedule(
        store,
        enabled=config.rest_enabled,
        window_start_utc=config.rest_window_start_utc,
        window_end_utc=config.rest_window_end_utc,
        idle_cycles_threshold=config.rest_idle_cycles_threshold,
        min_interval_seconds=config.rest_min_interval_seconds,
    )


def _persist_dispatch_mutations(
    store: StateStore,
    assignments: list[Assignment],
    result: CycleResult,
    original_assignments: dict[str, dict[str, str]],
) -> None:
    mutated_ids = {item.assignment_id for item in [*result.assigned, *result.skipped]}
    for assignment in assignments:
        if assignment.assignment_id not in mutated_ids:
            continue
        current = store.find_assignment(assignment.assignment_id)
        if current is None:
            continue
        original = original_assignments.get(assignment.assignment_id)
        if original is None:
            continue
        if (
            current.status.value != original["status"]
            or current.updated_at != original["updated_at"]
        ):
            continue
        store.update_assignment(assignment)


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
        elif action_type in LADDER_ACTION_STEPS:
            event = action.get("event")
            if isinstance(event, dict):
                events.append(event)
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
