"""Deterministic queue-reconciliation steps for the orchestration cycle.

Two automatic remediations that keep the fleet from wedging without an
operator (both observed live on 2026-07-09, when two abandoned root tasks
transitively deadlocked every queued assignment and two agents had built
parallel near-duplicate task ladders for the same mission):

- ``remediate_dead_dependencies``: a dependency that ends in a terminal
  non-complete state (abandoned/failed/superseded-without-successor) never
  satisfies its dependents — the dispatch gate only honors COMPLETE. The
  remediation ladder reissues the dead task once to the same agent, once more
  to an escalated agent, then parks the dependents for the operator.

- ``reconcile_duplicate_assignments``: near-duplicate live assignments
  (token-Jaccard over assignment text, same machinery as delegation dedup)
  are collapsed onto one survivor; the loser is superseded and its dependents
  re-pointed at the survivor.
"""

from __future__ import annotations

import logging
from typing import Any

from brigade.schemas import (
    TERMINAL_STATUSES,
    Assignment,
    AssignmentKind,
    AssignmentStatus,
)
from brigade.store import StateStore
from brigade.time import utc_now_iso

LOGGER = logging.getLogger("brigade.reconcile")

# Source stamped on auto-reissued attempts; the attempt counter is derived by
# walking reissue lineage and counting records carrying this source, so the
# retry budget survives restarts without a dedicated counter field.
DEAD_DEP_SOURCE = "dead_dependency_remediation"

# Safety valve: a similarity-threshold miscalibration must never be able to
# mass-supersede a fleet in one cycle. Leftover pairs are handled next cycle.
DUPLICATE_SWEEP_MAX_PER_CYCLE = 3

_TEMPLATED_KINDS = {AssignmentKind.REST, AssignmentKind.FAILURE_ANALYSIS}
_IN_FLIGHT = {AssignmentStatus.ASSIGNED, AssignmentStatus.WORKING}


def _record_of(entry: dict[str, Any]) -> dict[str, Any]:
    record = entry.get("record")
    return record if isinstance(record, dict) else {}


def _history_by_id(history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    # History is ordered by archived_at; the latest entry per id wins.
    by_id: dict[str, dict[str, Any]] = {}
    for entry in history:
        assignment_id = entry.get("assignment_id")
        if isinstance(assignment_id, str):
            by_id[assignment_id] = entry
    return by_id


def _lineage_ancestors(
    assignment_id: str,
    live_by_id: dict[str, Assignment],
    history_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    """Ids of the reissue chain starting at ``assignment_id`` walking backward
    (inclusive). Visited-guarded so corrupt circular lineage terminates."""
    chain: list[str] = []
    seen: set[str] = set()
    cursor: str | None = assignment_id
    while cursor and cursor not in seen:
        seen.add(cursor)
        chain.append(cursor)
        live = live_by_id.get(cursor)
        if live is not None:
            cursor = live.reissued_from_assignment_id
            continue
        cursor = _record_of(history_by_id.get(cursor, {})).get(
            "reissued_from_assignment_id"
        )
    return chain


def remediate_dead_dependencies(
    store: StateStore, config: Any
) -> dict[str, Any]:
    """Unblock dependents of terminally-dead dependencies.

    Per dead root: attempt 1 reissues to the same agent (failure context
    attached as guidance), attempt 2 reissues to an escalated agent via the
    ladder's reassignment selection, then dependents are parked
    ``blocked + awaiting_human`` and the operator is notified once.
    Superseded roots with a live successor are silently re-linked (a repair,
    not a retry); unknown ids are stripped from dependents.
    """
    from brigade.ladder import reassignment_target
    from brigade.orchestrator import (
        _completed_assignment_ids,
        _deliver_operator_notification,
        _idempotency_seen,
        orchestration_event,
    )
    from brigade.services import _repoint_dependents, reissue_archived_assignment

    if not config.auto_recover_enabled:
        return {"enabled": False, "actions": [], "events": []}

    assignments = store.assignments()
    history = store.assignment_history()
    completed = _completed_assignment_ids(assignments, history)
    live_by_id = {item.assignment_id: item for item in assignments}
    history_by_id = _history_by_id(history)

    # Forward lineage: original id -> reissued successor id. Live successors
    # win over archived ones.
    successor_by_original: dict[str, str] = {}
    for entry in history:
        record = _record_of(entry)
        origin = record.get("reissued_from_assignment_id")
        successor = record.get("assignment_id")
        if isinstance(origin, str) and isinstance(successor, str):
            successor_by_original[origin] = successor
    for item in assignments:
        if item.reissued_from_assignment_id:
            successor_by_original[item.reissued_from_assignment_id] = (
                item.assignment_id
            )

    dependents_by_root: dict[str, list[Assignment]] = {}
    for item in assignments:
        if item.status in TERMINAL_STATUSES:
            continue
        for dep in item.dependency_ids or []:
            if dep in completed or dep in live_by_id:
                continue
            dependents_by_root.setdefault(dep, []).append(item)

    agent_ids = {agent.agent_id for agent in store.agents()}
    actions: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    for root_id, dependents in sorted(dependents_by_root.items()):
        # Follow reissue lineage forward first: a superseded dep whose
        # successor is alive just needs re-linking (reissue-as-new normally
        # does this; repair the miss defensively).
        effective_id = root_id
        seen_forward: set[str] = set()
        while (
            effective_id not in live_by_id
            and effective_id in successor_by_original
            and effective_id not in seen_forward
        ):
            seen_forward.add(effective_id)
            effective_id = successor_by_original[effective_id]
        successor = live_by_id.get(effective_id)
        if successor is not None:
            repointed = _repoint_dependents(store, root_id, successor.assignment_id)
            action = {
                "dead_dependency_id": root_id,
                "decision": "relinked",
                "successor_id": successor.assignment_id,
                "repointed_dependents": repointed,
            }
            actions.append(action)
            events.append(
                orchestration_event(
                    "dead_dependency_relinked",
                    (
                        f"Dependency {root_id} was superseded; dependents "
                        f"re-linked to live successor {successor.assignment_id}."
                    ),
                    source="orchestrator_reconcile",
                    decision="relinked",
                    assignment_id=successor.assignment_id,
                    assignment_ids=repointed,
                    payload=action,
                )
            )
            continue

        entry = history_by_id.get(effective_id)
        if entry is None:
            # Neither live nor archived: corrupted reference. Strip it so the
            # dependents are not pinned to an id that can never resolve.
            stripped: list[str] = []
            for dependent in dependents:
                dependent.dependency_ids = [
                    dep
                    for dep in dependent.dependency_ids
                    if dep != root_id
                ]
                dependent.updated_at = utc_now_iso()
                store.update_assignment(dependent)
                stripped.append(dependent.assignment_id)
            store.add_alert(
                f"dependency {root_id} is unknown (not live, not archived); "
                f"stripped from {len(stripped)} dependent(s)."
            )
            action = {
                "dead_dependency_id": root_id,
                "decision": "stripped_unknown",
                "dependents": stripped,
            }
            actions.append(action)
            events.append(
                orchestration_event(
                    "dead_dependency_stripped_unknown",
                    (
                        f"Unknown dependency {root_id} stripped from "
                        f"{len(stripped)} dependent assignment(s)."
                    ),
                    source="orchestrator_reconcile",
                    decision="stripped_unknown",
                    assignment_ids=stripped,
                    payload=action,
                )
            )
            continue

        record = _record_of(entry)
        ancestors = _lineage_ancestors(effective_id, live_by_id, history_by_id)
        lineage_root_id = ancestors[-1]
        attempts = sum(
            1
            for ancestor in ancestors
            if _record_of(history_by_id.get(ancestor, {})).get("source")
            == DEAD_DEP_SOURCE
        )
        final_status = str(entry.get("final_status") or "unknown")
        reason = str(
            entry.get("failure_info")
            or entry.get("executive_summary")
            or "no failure detail recorded"
        )
        cycle_count = record.get("cycle_count")
        original_agent = str(record.get("assigned_to") or "")

        target_agent: str | None = None
        decision: str | None = None
        if attempts < config.max_auto_reissue:
            if attempts == 0 and original_agent in agent_ids:
                target_agent = original_agent
                decision = "reissued_same_agent"
            else:
                # Second attempt (or the original agent no longer exists):
                # escalate to a different agent using the ladder's selector.
                probe = Assignment(
                    assignment=str(record.get("assignment") or "unknown"),
                    assigned_to=original_agent or "unknown",
                    created_by="orchestrator",
                    source=DEAD_DEP_SOURCE,
                )
                candidate = reassignment_target(store, probe, assignments)
                if candidate is not None and candidate.agent_id != original_agent:
                    target_agent = candidate.agent_id
                    decision = "reissued_escalated_agent"

        if target_agent is not None:
            guidance = [
                {
                    "at": utc_now_iso(),
                    "operator": "orchestrator",
                    "operator_message": (
                        f"Automatic reissue (attempt {attempts + 1} of "
                        f"{config.max_auto_reissue}): prior attempt "
                        f"{effective_id} ended {final_status}"
                        + (
                            f" after {cycle_count} cycles"
                            if cycle_count
                            else ""
                        )
                        + f": {reason} — address that failure mode instead of "
                        "repeating the same approach."
                    ),
                }
            ]
            persisted = reissue_archived_assignment(
                store,
                entry,
                assigned_to=target_agent,
                by="orchestrator",
                source=DEAD_DEP_SOURCE,
                note=f"auto-reissue of dead dependency ({final_status})",
                idempotency_key=(
                    f"dead-dep:v1:{lineage_root_id}:attempt:{attempts + 1}"
                ),
                guidance=guidance,
            )
            repointed = _repoint_dependents(
                store, root_id, persisted.assignment_id
            )
            store.add_alert(
                f"dead dependency {root_id} ({final_status}) auto-reissued as "
                f"{persisted.assignment_id} to {target_agent} "
                f"(attempt {attempts + 1}/{config.max_auto_reissue})."
            )
            action = {
                "dead_dependency_id": root_id,
                "decision": decision,
                "new_assignment_id": persisted.assignment_id,
                "agent_id": target_agent,
                "attempt": attempts + 1,
                "repointed_dependents": repointed,
            }
            actions.append(action)
            events.append(
                orchestration_event(
                    "dead_dependency_reissued",
                    (
                        f"Dead dependency {root_id} ({final_status}) reissued "
                        f"as {persisted.assignment_id} to {target_agent} "
                        f"(attempt {attempts + 1}/{config.max_auto_reissue})."
                    ),
                    source="orchestrator_reconcile",
                    decision=decision,
                    assignment_id=persisted.assignment_id,
                    agent_id=target_agent,
                    assignment_ids=repointed,
                    payload=action,
                )
            )
            continue

        # Retry budget exhausted (or no alternate agent exists): park the
        # dependents for the operator. In-flight dependents were already
        # dispatched and are left running.
        parked: list[str] = []
        for dependent in dependents:
            if dependent.status in _IN_FLIGHT:
                continue
            if dependent.status == AssignmentStatus.QUEUED:
                dependent.transition_to(AssignmentStatus.BLOCKED)
            dependent.awaiting_human = True
            dependent.progress_summary = (
                f"dependency {root_id} died ({final_status}) and exhausted "
                f"{attempts} auto-reissue attempt(s): {reason}; operator "
                "decision required"
            )
            dependent.updated_at = utc_now_iso()
            store.update_assignment(dependent)
            parked.append(dependent.assignment_id)
        store.add_alert(
            f"dead dependency {root_id} exhausted auto-reissue; "
            f"{len(parked)} dependent(s) parked for the operator."
        )
        action = {
            "dead_dependency_id": root_id,
            "decision": "escalated_operator",
            "attempts": attempts,
            "parked_dependents": parked,
        }
        actions.append(action)
        notify_key = f"dead-dep-notify:v1:{lineage_root_id}"
        event_kwargs: dict[str, Any] = {}
        if not _idempotency_seen(store, notify_key):
            text = (
                "OpenBrigade needs an operator. Dependency "
                f"{root_id} ended {final_status} and auto-reissue is "
                f"exhausted ({attempts} attempt(s)). {len(parked)} dependent "
                f"assignment(s) are parked awaiting a decision. "
                f"Last failure: {reason}"
            )
            delivery = _deliver_operator_notification(store, config, text)
            action["delivery"] = delivery
            if delivery["status"] == "sent":
                # Burning the key = landing it in the reasoning record via
                # this event; a failed delivery retries next cycle.
                event_kwargs["idempotency_key"] = notify_key
        events.append(
            orchestration_event(
                "dead_dependency_escalated_human",
                (
                    f"Dead dependency {root_id} ({final_status}) exhausted "
                    f"auto-reissue; {len(parked)} dependent(s) parked for "
                    "the operator."
                ),
                source="orchestrator_reconcile",
                decision="escalated_operator",
                assignment_ids=parked,
                payload=action,
                **event_kwargs,
            )
        )

    return {"enabled": True, "actions": actions, "events": events}


def _shares_lineage(
    first: Assignment,
    second: Assignment,
    live_by_id: dict[str, Assignment],
    history_by_id: dict[str, dict[str, Any]],
) -> bool:
    chain_a = set(
        _lineage_ancestors(first.assignment_id, live_by_id, history_by_id)
    )
    chain_b = set(
        _lineage_ancestors(second.assignment_id, live_by_id, history_by_id)
    )
    return bool(chain_a & chain_b)


def reconcile_duplicate_assignments(
    store: StateStore, config: Any
) -> dict[str, Any]:
    """Collapse live near-duplicate assignments onto one survivor.

    Cross-agent duplicates slip past creation-time dedup when two agents plan
    the same mission independently. The survivor is the older assignment
    unless the newer one is already WORKING (in-flight progress beats a
    queued twin). Reissue-lineage relatives, parent/child pairs, and
    templated kinds (rest, failure-analysis) never count as duplicates, and
    an in-flight assignment is never the one superseded.
    """
    from brigade.orchestrator import orchestration_event
    from brigade.services import _repoint_dependents
    from brigade.tools import BACKLOG_DEDUP_THRESHOLD, UNDONE_STATUSES, dedup_tokens

    if not getattr(config, "duplicate_reconciliation_enabled", True):
        return {"enabled": False, "actions": [], "events": []}

    assignments = store.assignments()
    live_by_id = {item.assignment_id: item for item in assignments}
    history_by_id = _history_by_id(store.assignment_history())
    candidates = [
        item
        for item in assignments
        if item.status in UNDONE_STATUSES and item.kind not in _TEMPLATED_KINDS
    ]
    candidates.sort(key=lambda item: (item.created_at, item.assignment_id))
    tokens = {
        item.assignment_id: dedup_tokens(item.assignment) for item in candidates
    }

    actions: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    superseded_ids: set[str] = set()

    for index, older in enumerate(candidates):
        if older.assignment_id in superseded_ids:
            continue
        for newer in candidates[index + 1 :]:
            if len(actions) >= DUPLICATE_SWEEP_MAX_PER_CYCLE:
                break
            if (
                newer.assignment_id in superseded_ids
                or older.assignment_id in superseded_ids
            ):
                continue
            first_tokens = tokens[older.assignment_id]
            second_tokens = tokens[newer.assignment_id]
            if not first_tokens or not second_tokens:
                continue
            overlap = len(first_tokens & second_tokens) / len(
                first_tokens | second_tokens
            )
            if overlap < BACKLOG_DEDUP_THRESHOLD:
                continue
            if (
                older.parent_assignment_id == newer.assignment_id
                or newer.parent_assignment_id == older.assignment_id
            ):
                continue
            if (
                older.idempotency_key
                and older.idempotency_key == newer.idempotency_key
            ):
                continue
            if _shares_lineage(older, newer, live_by_id, history_by_id):
                # A reissue is intentionally near-identical to its original.
                continue
            survivor, loser = older, newer
            rule = "older_survives"
            if (
                newer.status == AssignmentStatus.WORKING
                and older.status not in _IN_FLIGHT
            ):
                survivor, loser = newer, older
                rule = "in_flight_survives"
            if loser.status in _IN_FLIGHT:
                # Never yank an assignment out from under a running agent.
                continue

            terminal = (
                AssignmentStatus.SUPERSEDED
                if loser.status == AssignmentStatus.QUEUED
                else AssignmentStatus.ABANDONED
            )
            summary = (
                f"auto-superseded as duplicate of {survivor.assignment_id} "
                "by duplicate reconciliation"
            )
            loser.transition_to(terminal)
            loser.progress_summary = summary
            loser.updated_at = utc_now_iso()
            store.archive_assignment(loser, summary)
            superseded_ids.add(loser.assignment_id)

            repointed = _repoint_dependents(
                store, loser.assignment_id, survivor.assignment_id
            )
            # Union the loser's upstream ordering into the survivor and drop
            # any self-reference the repoint may have introduced.
            refreshed = store.find_assignment(survivor.assignment_id)
            if refreshed is not None:
                merged = [
                    dep
                    for dep in [*refreshed.dependency_ids, *loser.dependency_ids]
                    if dep != refreshed.assignment_id
                ]
                deduped: list[str] = []
                for dep in merged:
                    if dep not in deduped and dep not in superseded_ids:
                        deduped.append(dep)
                if deduped != refreshed.dependency_ids:
                    refreshed.dependency_ids = deduped
                    refreshed.updated_at = utc_now_iso()
                    store.update_assignment(refreshed)

            store.add_alert(
                f"duplicate assignment {loser.assignment_id} "
                f"({loser.assigned_to}) auto-superseded; "
                f"{survivor.assignment_id} ({survivor.assigned_to}) survives "
                f"(similarity {overlap:.2f})."
            )
            action = {
                "superseded_id": loser.assignment_id,
                "superseded_agent": loser.assigned_to,
                "survivor_id": survivor.assignment_id,
                "survivor_agent": survivor.assigned_to,
                "similarity": round(overlap, 3),
                "rule": rule,
                "final_status": terminal.value,
                "repointed_dependents": repointed,
            }
            actions.append(action)
            events.append(
                orchestration_event(
                    "duplicate_superseded",
                    (
                        f"Duplicate assignment {loser.assignment_id} "
                        f"({loser.assigned_to}) superseded by near-identical "
                        f"{survivor.assignment_id} ({survivor.assigned_to}), "
                        f"similarity {overlap:.2f}."
                    ),
                    source="orchestrator_reconcile",
                    decision="duplicate_superseded",
                    assignment_id=survivor.assignment_id,
                    agent_id=survivor.assigned_to,
                    assignment_ids=[loser.assignment_id, *repointed],
                    payload=action,
                )
            )
        if len(actions) >= DUPLICATE_SWEEP_MAX_PER_CYCLE:
            break

    return {"enabled": True, "actions": actions, "events": events}
