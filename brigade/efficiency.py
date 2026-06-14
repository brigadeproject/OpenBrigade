"""Efficiency detection and recurrence materialization.

Completed assignment history is grouped by assignee and normalized assignment
text (dates and ids stripped). A group reaching the detection threshold within
the lookback window becomes an ``efficiency`` proposal carrying a recurrence
template with an interval derived from the median completion gap. Episode
similarity is attached as supporting evidence only — never the trigger — so
detection stays deterministic and testable offline.
"""

from __future__ import annotations

import hashlib
import logging
import re
import statistics
from datetime import datetime, timedelta
from typing import Any

from brigade.orchestrator import orchestration_event, route_to_chief
from brigade.schemas import (
    Assignment,
    AssignmentKind,
    AssignmentStatus,
    Priority,
    build_proposal,
)
from brigade.store import StateStore
from brigade.time import add_seconds_iso, parse_utc_iso, utc_now

LOGGER = logging.getLogger("brigade.efficiency")

EVENT_RECURRENCE_MATERIALIZED = "recurrence_materialized"
EVENT_PROPOSAL_CREATED = "proposal_created"

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}([t ]\d{2}:\d{2}(:\d{2})?(z|[+-]\d{2}:?\d{2})?)?",
    re.IGNORECASE,
)
_DATE_KEY_RE = re.compile(r"\b\d{8}\b")


def recurrence_idempotency_key(recurrence_id: str, next_due_at: str) -> str:
    return f"recurrence:v1:{recurrence_id}:{next_due_at}"


def normalize_pattern_text(text: str) -> str:
    """Identity normalization with dates and ids stripped, so repeated work
    with varying timestamps groups together."""
    value = _UUID_RE.sub(" ", text.lower())
    value = _TIMESTAMP_RE.sub(" ", value)
    value = _DATE_KEY_RE.sub(" ", value)
    return " ".join(value.split())


def detect_recurring_work(
    store: StateStore,
    *,
    threshold: int = 3,
    lookback_days: int = 14,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Group completed history by (assignee, normalized text) and propose a
    recurrence for every group at or past the threshold."""
    now = now or utc_now()
    cutoff = now - timedelta(days=lookback_days)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in store.assignment_history():
        if item.get("final_status") != AssignmentStatus.COMPLETE.value:
            continue
        record = item.get("record") or {}
        assigned_to = str(record.get("assigned_to") or "")
        text = str(record.get("assignment") or "")
        archived_at = str(item.get("archived_at") or "")
        if not assigned_to or not text or not archived_at:
            continue
        try:
            archived = parse_utc_iso(archived_at)
        except ValueError:
            continue
        if archived < cutoff:
            continue
        pattern = normalize_pattern_text(text)
        if not pattern:
            continue
        groups.setdefault((assigned_to, pattern), []).append(
            {
                "assignment_id": record.get("assignment_id"),
                "archived_at": archived_at,
                "archived": archived,
                "text": text,
            }
        )

    proposals: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for (assigned_to, pattern), samples in sorted(groups.items()):
        if len(samples) < threshold:
            continue
        samples.sort(key=lambda sample: sample["archived"])
        gaps = [
            (later["archived"] - earlier["archived"]).total_seconds()
            for earlier, later in zip(samples, samples[1:], strict=False)
        ]
        interval_seconds = max(int(statistics.median(gaps)), 60)
        pattern_hash = hashlib.sha256(
            f"{assigned_to}:{pattern}".encode()
        ).hexdigest()[:16]
        proposal = build_proposal(
            kind="efficiency",
            title=f"Recurring work detected for {assigned_to}: {pattern[:80]}",
            agent_id=assigned_to,
            details={
                "pattern": pattern,
                "count": len(samples),
                "sample_assignment_ids": [
                    sample["assignment_id"] for sample in samples
                ],
                "interval_seconds": interval_seconds,
                "template": {
                    "assignment": samples[-1]["text"],
                    "assigned_to": assigned_to,
                    "priority": Priority.NORMAL.value,
                },
                "evidence": _episode_evidence(store, pattern),
            },
            idempotency_key=f"efficiency:v1:{assigned_to}:{pattern_hash}",
        )
        persisted = store.add_proposal(proposal)
        if persisted.get("proposal_id") != proposal["proposal_id"]:
            continue  # already proposed for this agent/pattern
        proposals.append(proposal)
        events.append(
            orchestration_event(
                EVENT_PROPOSAL_CREATED,
                f"Efficiency proposal: {assigned_to} completed "
                f"'{pattern[:60]}' {len(samples)} times; recurrence suggested.",
                source="efficiency_detection",
                decision="proposed",
                status="proposed",
                agent_id=assigned_to,
                idempotency_key=proposal["idempotency_key"],
                payload=proposal,
            )
        )
        LOGGER.info(
            "recurring_work_detected",
            extra={"agent_id": assigned_to, "count": len(samples)},
        )
    return {"proposals": proposals, "events": events}


def materialize_due_recurrences(
    store: StateStore,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Turn due recurrences into queued assignments exactly once per due slot,
    then advance ``next_due_at`` past now."""
    now = now or utc_now()
    materialized: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for recurrence in store.recurrences(enabled=True):
        next_due_at = str(recurrence.get("next_due_at") or "")
        try:
            due = parse_utc_iso(next_due_at)
        except ValueError:
            continue
        if due > now:
            continue
        interval_seconds = int(recurrence.get("interval_seconds") or 0)
        if interval_seconds <= 0:
            continue
        recurrence_id = str(recurrence.get("recurrence_id"))
        key = recurrence_idempotency_key(recurrence_id, next_due_at)
        template = recurrence.get("template") or {}
        if store.find_assignment_by_idempotency_key(key) is None:
            # Chief-first: orchestrator-created work targets the crew chief
            # managing the template's agent; the chief decomposes or delegates.
            suggested = str(template.get("assigned_to") or "")
            chief = route_to_chief(store, agent_id=suggested or None)
            target = chief.agent_id if chief is not None else suggested
            rationale = f"Recurrence {recurrence_id} due at {next_due_at}."
            if target != suggested:
                rationale += f" (routed to crew chief; suggested agent was {suggested})"
            assignment = Assignment(
                assignment=str(template.get("assignment") or ""),
                assigned_to=target,
                created_by="orchestrator",
                source="orchestrator_recurrence",
                kind=_kind(template.get("kind")),
                priority=_priority(template.get("priority")),
                assignment_rationale=rationale,
                created_by_role="orchestrator",
                idempotency_key=key,
            )
            persisted = store.add_assignment(assignment)
            entry = {
                "recurrence_id": recurrence_id,
                "assignment_id": persisted.assignment_id,
                "due_at": next_due_at,
                "idempotency_key": key,
            }
            materialized.append(entry)
            events.append(
                orchestration_event(
                    EVENT_RECURRENCE_MATERIALIZED,
                    f"Recurrence {recurrence_id} materialized assignment "
                    f"{persisted.assignment_id} for slot {next_due_at}.",
                    source="orchestrator_recurrence",
                    decision="created",
                    trigger="recurrence_due",
                    assignment_id=persisted.assignment_id,
                    agent_id=persisted.assigned_to,
                    idempotency_key=key,
                    payload=entry,
                )
            )
        # Advance past now even when the slot was already materialized, so a
        # missed window never double-fires.
        advanced = next_due_at
        while parse_utc_iso(advanced) <= now:
            advanced = add_seconds_iso(advanced, interval_seconds)
        recurrence["next_due_at"] = advanced
        recurrence["last_materialized_at"] = next_due_at
        recurrence["updated_at"] = now.isoformat()
        store.update_recurrence(recurrence)
    return {"materialized": materialized, "events": events}


def run_recurrence_step(
    store: StateStore,
    *,
    threshold: int = 3,
    lookback_days: int = 14,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Cycle step 5: materialize due recurrences, then detect new patterns."""
    materialization = materialize_due_recurrences(store, now=now)
    detection = detect_recurring_work(
        store,
        threshold=threshold,
        lookback_days=lookback_days,
        now=now,
    )
    return {
        "materialized": materialization["materialized"],
        "proposals": detection["proposals"],
        "events": [*materialization["events"], *detection["events"]],
    }


def _episode_evidence(store: StateStore, pattern: str) -> list[dict[str, Any]]:
    # Evidence only, never the trigger: detection works with search absent.
    try:
        matches = store.search_episodes(pattern, limit=3)
    except Exception:
        return []
    evidence = []
    for match in matches:
        payload = match.get("payload") if isinstance(match, dict) else None
        if isinstance(payload, dict):
            evidence.append(
                {
                    "episode_id": payload.get("episode_id"),
                    "summary": payload.get("summary"),
                    "score": match.get("score"),
                }
            )
    return evidence


def _kind(value: Any) -> AssignmentKind:
    try:
        return AssignmentKind(str(value or AssignmentKind.MISSION.value))
    except ValueError:
        return AssignmentKind.MISSION


def _priority(value: Any) -> Priority:
    try:
        return Priority(str(value or Priority.NORMAL.value).lower())
    except ValueError:
        return Priority.NORMAL
