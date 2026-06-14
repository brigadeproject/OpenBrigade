"""Rest and dream cycles: hybrid scheduled-window and opportunistic downtime.

A scheduled UTC window guarantees memory curation happens; an opportunistic
path uses idle capacity. At most one scheduled and one opportunistic rest per
agent per UTC day (``rest:v1:<agent>:<date>:<window|idle>``). Rest assignments
are ``kind=rest``, ``priority=low``, sort last in dispatch, and never preempt
queued mission work. A deterministic finalizer lands the dream output in
durable form regardless of model quality.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, time
from typing import Any

from brigade.memory import (
    archive_stale_daily_memories,
    curate_workspace_memory,
)
from brigade.orchestrator import (
    _idempotency_seen,
    orchestration_event,
    record_orchestration_events,
)
from brigade.schemas import (
    Agent,
    Assignment,
    AssignmentKind,
    AssignmentStatus,
    Priority,
    build_proposal,
)
from brigade.store import StateStore
from brigade.time import parse_utc_iso, utc_now
from brigade.workspace import ensure_agent_workspace

LOGGER = logging.getLogger("brigade.rest")

REST_SOURCE = "orchestrator_rest"
EVENT_REST_SCHEDULED = "rest_scheduled"
EVENT_REST_COMPLETED = "rest_completed"

TRIGGER_WINDOW = "window"
TRIGGER_IDLE = "idle"

PROPOSAL_TAG_KINDS = {
    "[efficiency]": "efficiency",
    "[tool_request]": "tool_request",
}


def rest_idempotency_key(agent_id: str, date_key: str, trigger: str) -> str:
    return f"rest:v1:{agent_id}:{date_key}:{trigger}"


def rest_assignment_text(date_key: str) -> str:
    """The dream protocol, executed with the agent's normal file tools."""
    return "\n".join(
        [
            "Rest cycle: curate memory, reflect, and ponder using your normal "
            "file tools.",
            "1. Read your daily notes (memory/*-MEMORY.md) and promote durable "
            "facts into MEMORY.md, keeping it at or under 2KB; prune outdated "
            "or irrelevant entries.",
            "2. Append entries to reflections.md — what was done, the outcome, "
            "the lesson — each with a status of candidate, promoted, or "
            "archived (candidates graduate when they prove useful, archive "
            "after long disuse).",
            "3. Process up to three questions from PONDER.md, the "
            "open-questions queue; write conclusions or sharper questions.",
            f"4. Write a structured report to rest/{date_key}-REST.md with "
            "sections '## Promoted', '## Pruned', '## Reflections', "
            "'## Ponderings', and '## Proposals' (each proposal a bullet "
            "tagged [efficiency] or [tool_request]).",
        ]
    )


def evaluate_rest_schedule(
    store: StateStore,
    *,
    enabled: bool = True,
    window_start_utc: str = "03:00",
    window_end_utc: str = "05:00",
    idle_cycles_threshold: int = 6,
    min_interval_seconds: int = 86_400,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One rest pass: offer eligible agents an idempotent rest assignment.

    Eligibility: rest enabled, no active or queued non-rest work, inside the
    UTC window or idle past the threshold, and the last completed rest older
    than the minimum interval.
    """
    result: dict[str, Any] = {
        "enabled": enabled,
        "created": [],
        "already_rested": [],
        "events": [],
    }
    if not enabled:
        return result
    now = now or utc_now()
    date_key = now.strftime("%Y%m%d")
    in_window = _in_window(now, window_start_utc, window_end_utc)
    assignments = store.assignments()
    occupied = {
        item.assigned_to
        for item in assignments
        if item.status
        in {
            AssignmentStatus.ASSIGNED,
            AssignmentStatus.WORKING,
            AssignmentStatus.BLOCKED,
        }
        or (item.status == AssignmentStatus.QUEUED and item.kind != AssignmentKind.REST)
    }
    states = store.agent_states()

    for agent in sorted(store.agents(), key=lambda item: item.agent_id):
        if agent.agent_id in occupied:
            continue
        idle_cycles = (
            states[agent.agent_id].idle_cycles if agent.agent_id in states else 0
        )
        if in_window:
            trigger = TRIGGER_WINDOW
        elif idle_cycles >= idle_cycles_threshold:
            trigger = TRIGGER_IDLE
        else:
            continue
        key = rest_idempotency_key(agent.agent_id, date_key, trigger)
        if _rest_key_seen(store, key):
            result["already_rested"].append(
                {"agent_id": agent.agent_id, "reason": "already_rested_today"}
            )
            continue
        last_rest = _last_completed_rest_at(store, agent.agent_id, assignments)
        if (
            last_rest is not None
            and (now - last_rest).total_seconds() < min_interval_seconds
        ):
            result["already_rested"].append(
                {"agent_id": agent.agent_id, "reason": "min_interval"}
            )
            continue
        assignment = Assignment(
            assignment=rest_assignment_text(date_key),
            assigned_to=agent.agent_id,
            created_by="orchestrator",
            source=REST_SOURCE,
            kind=AssignmentKind.REST,
            priority=Priority.LOW,
            assignment_rationale=(
                f"Rest cycle ({trigger} trigger) for {date_key}: memory "
                "curation, reflection, and pondering."
            ),
            created_by_role="orchestrator",
            idempotency_key=key,
            room_id="barracks",
        )
        persisted = store.add_assignment(assignment)
        entry = {
            "assignment_id": persisted.assignment_id,
            "agent_id": agent.agent_id,
            "trigger": trigger,
            "idempotency_key": key,
        }
        result["created"].append(entry)
        result["events"].append(
            orchestration_event(
                EVENT_REST_SCHEDULED,
                f"Rest cycle scheduled for {agent.agent_id} ({trigger} trigger).",
                source=REST_SOURCE,
                decision="created",
                trigger=trigger,
                assignment_id=persisted.assignment_id,
                agent_id=agent.agent_id,
                idempotency_key=key,
                payload=entry,
            )
        )
        LOGGER.info(
            "rest_scheduled",
            extra={"agent_id": agent.agent_id, "trigger": trigger},
        )
    return result


def finalize_rest_assignment(
    store: StateStore,
    agent: Agent,
    assignment: Assignment,
) -> dict[str, Any]:
    """Deterministic rest finalizer, run when a ``kind=rest`` assignment
    completes: enforce the MEMORY.md cap, archive stale daily notes into
    episodes, parse the rest report into one episode and one proposal row per
    ``## Proposals`` bullet, and emit ``rest_completed``."""
    workspace = ensure_agent_workspace(agent, store.data_dir)
    curate_workspace_memory(workspace)
    archived = archive_stale_daily_memories(workspace, agent.agent_id)
    for episode in archived:
        store.add_episode(episode)

    report_path = _latest_rest_report(workspace)
    proposals: list[dict[str, Any]] = []
    report_summary = "rest cycle completed; no rest report found"
    if report_path is not None:
        content = report_path.read_text(encoding="utf-8")
        report_summary = f"rest cycle report {report_path.name}"
        episode = {
            "episode_id": assignment.assignment_id,
            "agent_id": agent.agent_id,
            "source_kind": "rest_cycle",
            "source_id": report_path.name,
            "summary": report_summary,
            "learned_facts": _section_bullets(content, "Promoted")[:10],
            "open_threads": _section_bullets(content, "Ponderings")[:10],
            "source_refs": [str(report_path)],
            "created_at": assignment.updated_at,
        }
        store.add_episode(episode)
        for bullet in _section_bullets(content, "Proposals"):
            proposal = _proposal_from_bullet(agent, bullet)
            store.add_proposal(proposal)
            proposals.append(proposal)

    event = orchestration_event(
        EVENT_REST_COMPLETED,
        f"Rest cycle completed for {agent.agent_id}: {report_summary}; "
        f"{len(archived)} daily note(s) archived, "
        f"{len(proposals)} proposal(s) recorded.",
        source=REST_SOURCE,
        decision="completed",
        assignment_id=assignment.assignment_id,
        agent_id=agent.agent_id,
        idempotency_key=assignment.idempotency_key,
        payload={
            "report": str(report_path) if report_path else None,
            "archived_episodes": len(archived),
            "proposal_ids": [item["proposal_id"] for item in proposals],
        },
    )
    record_orchestration_events(
        store,
        source=REST_SOURCE,
        decision_summary=f"rest finalized for {agent.agent_id}",
        events=[event],
    )
    return {
        "report": str(report_path) if report_path else None,
        "archived_episodes": archived,
        "proposals": proposals,
        "event": event,
    }


def _in_window(now: datetime, start: str, end: str) -> bool:
    window_start = _parse_clock(start)
    window_end = _parse_clock(end)
    current = now.time()
    if window_start <= window_end:
        return window_start <= current < window_end
    # The window crosses midnight.
    return current >= window_start or current < window_end


def _parse_clock(value: str) -> time:
    hours, minutes = value.strip().split(":", 1)
    return time(int(hours), int(minutes))


def _last_completed_rest_at(
    store: StateStore,
    agent_id: str,
    assignments: list[Assignment],
) -> datetime | None:
    candidates: list[str] = [
        item.updated_at
        for item in assignments
        if item.assigned_to == agent_id
        and item.kind == AssignmentKind.REST
        and item.status == AssignmentStatus.COMPLETE
    ]
    for item in store.assignment_history():
        record = item.get("record") or {}
        if (
            record.get("assigned_to") == agent_id
            and record.get("kind") == AssignmentKind.REST.value
            and item.get("final_status") == AssignmentStatus.COMPLETE.value
        ):
            archived_at = item.get("archived_at")
            if isinstance(archived_at, str):
                candidates.append(archived_at)
    if not candidates:
        return None
    return max(parse_utc_iso(stamp) for stamp in candidates)


def _rest_key_seen(store: StateStore, key: str) -> bool:
    if _idempotency_seen(store, key):
        return True
    # Completed rest work archives out of the active assignment list; its
    # record keeps the key in assignment history.
    for item in store.assignment_history():
        record = item.get("record") or {}
        if record.get("idempotency_key") == key:
            return True
    return False


def _latest_rest_report(workspace) -> Any:
    rest_dir = workspace / "rest"
    if not rest_dir.exists():
        return None
    reports = sorted(rest_dir.glob("*-REST.md"))
    return reports[-1] if reports else None


def _section_bullets(content: str, section: str) -> list[str]:
    bullets: list[str] = []
    in_section = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_section = stripped[3:].strip().lower() == section.lower()
            continue
        if in_section and stripped.startswith("- "):
            bullets.append(stripped[2:].strip())
    return bullets


def _proposal_from_bullet(agent: Agent, bullet: str) -> dict[str, Any]:
    kind = "rest_insight"
    title = bullet
    for tag, tag_kind in PROPOSAL_TAG_KINDS.items():
        if bullet.lower().startswith(tag):
            kind = tag_kind
            title = bullet[len(tag) :].strip() or bullet
            break
    digest = hashlib.sha256(
        f"{agent.agent_id}:{bullet}".encode()
    ).hexdigest()
    return build_proposal(
        kind=kind,
        title=title,
        agent_id=agent.agent_id,
        team_id=agent.team_id,
        details={"source": "rest_cycle", "bullet": bullet},
        idempotency_key=f"rest-proposal:v1:{digest}",
    )
