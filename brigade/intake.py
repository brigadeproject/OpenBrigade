"""Intake triggers: pull-based scan turning persisted artifacts into work.

Each cycle scans already-persisted knowledge documents and connector inbound
messages, so connectors and the knowledge pipeline are unchanged and the scan
is replay-safe. A document or message becomes a task at most once via
``intake:v1:<sha256(source_kind, source_id)>`` — no watermark table needed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from brigade.orchestrator import (
    _crew_chief_agents,
    _idempotency_seen,
    orchestration_event,
)
from brigade.profile import derived_specialty_tokens
from brigade.schemas import Agent, Assignment, Priority
from brigade.store import StateStore

LOGGER = logging.getLogger("brigade.intake")

INTAKE_SOURCE = "orchestrator_intake"
EVENT_INTAKE_PROPOSAL = "intake_proposal"
EVENT_INTAKE_CREATED = "intake_created"

SOURCE_KIND_DOCUMENT = "knowledge_document"
SOURCE_KIND_MESSAGE = "inbound_message"


def intake_idempotency_key(source_kind: str, source_id: str) -> str:
    digest = hashlib.sha256(
        json.dumps([source_kind, source_id], separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"intake:v1:{digest}"


def evaluate_intake_queue(
    store: StateStore,
    *,
    mode: str = "propose",
    max_per_cycle: int = 2,
    route_chief: str | None = None,
    default_priority: str = "normal",
) -> dict[str, Any]:
    """One intake pass: scan sources oldest-first, dedupe, route, gate by mode.

    Returns the intake sub-result for the cycle reasoning record: ``proposals``
    (propose mode), ``created`` assignments (create mode), suppressed
    ``duplicates``, and the orchestration ``events`` emitted.
    """
    result: dict[str, Any] = {
        "mode": mode,
        "proposals": [],
        "created": [],
        "duplicates": [],
        "events": [],
    }
    if mode == "off":
        return result

    chiefs = _crew_chief_agents(store)
    processed = 0
    for item in _intake_items(store):
        if processed >= max_per_cycle:
            break
        key = intake_idempotency_key(item["source_kind"], item["source_id"])
        if _idempotency_seen(store, key):
            result["duplicates"].append(
                {
                    "source_kind": item["source_kind"],
                    "source_id": item["source_id"],
                    "idempotency_key": key,
                }
            )
            continue
        chief = _route_intake(store, item, chiefs, route_chief)
        if chief is None:
            # No crew chief exists at all; intake has nowhere to route.
            continue
        entry = {
            "source_kind": item["source_kind"],
            "source_id": item["source_id"],
            "title": item["title"],
            "agent_id": chief.agent_id,
            "idempotency_key": key,
        }
        if mode == "create":
            assignment = Assignment(
                assignment=_intake_assignment_text(item),
                assigned_to=chief.agent_id,
                created_by="orchestrator",
                source=INTAKE_SOURCE,
                priority=_priority(default_priority),
                assignment_rationale=(
                    f"Intake trigger from {item['source_kind']} "
                    f"{item['source_id']}: {item['title']}"
                ),
                created_by_role="orchestrator",
                idempotency_key=key,
            )
            persisted = store.add_assignment(assignment)
            entry["assignment_id"] = persisted.assignment_id
            result["created"].append(entry)
            event_type, summary = EVENT_INTAKE_CREATED, (
                f"Intake created assignment {persisted.assignment_id} for "
                f"{item['source_kind']} '{item['title']}' routed to {chief.agent_id}."
            )
        else:
            result["proposals"].append(entry)
            event_type, summary = EVENT_INTAKE_PROPOSAL, (
                f"Intake proposed work for {item['source_kind']} "
                f"'{item['title']}' routed to {chief.agent_id}; intake_mode is propose."
            )
        result["events"].append(
            orchestration_event(
                event_type,
                summary,
                source=INTAKE_SOURCE,
                decision="created" if mode == "create" else "proposed",
                trigger=item["source_kind"],
                assignment_id=entry.get("assignment_id"),
                agent_id=chief.agent_id,
                idempotency_key=key,
                payload=entry,
            )
        )
        processed += 1
        LOGGER.info(
            "intake_item_processed",
            extra={
                "source_kind": item["source_kind"],
                "source_id": item["source_id"],
                "mode": mode,
            },
        )
    return result


def _intake_items(store: StateStore) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for document in store.knowledge_documents():
        document_id = str(document.get("document_id") or "")
        if not document_id:
            continue
        metadata = document.get("metadata") or {}
        items.append(
            {
                "source_kind": SOURCE_KIND_DOCUMENT,
                "source_id": document_id,
                "title": str(document.get("title") or "untitled document"),
                "text": " ".join(
                    str(part)
                    for part in (
                        document.get("title"),
                        metadata.get("summary") if isinstance(metadata, dict) else None,
                        document.get("source"),
                    )
                    if part
                ),
                "created_at": str(document.get("ingested_at") or ""),
            }
        )
    for message in store.messages():
        if (message.metadata or {}).get("kind") != "external_inbound":
            continue
        items.append(
            {
                "source_kind": SOURCE_KIND_MESSAGE,
                "source_id": message.message_id,
                "title": message.content[:80],
                "text": message.content,
                "created_at": message.created_at,
            }
        )
    return sorted(items, key=lambda item: item["created_at"])


def _route_intake(
    store: StateStore,
    item: dict[str, Any],
    chiefs: list[Agent],
    route_chief: str | None,
) -> Agent | None:
    """Routing precedence: configured chief, token overlap with chief goals
    plus member specialties (this is how on-call chiefs get invoked), then the
    first crew chief."""
    if not chiefs:
        return None
    by_id = {chief.agent_id: chief for chief in chiefs}
    if route_chief and route_chief in by_id:
        return by_id[route_chief]
    item_tokens = _tokens(f"{item['title']} {item['text']}")
    goals_by_agent = store.goals()
    agents_by_id = {agent.agent_id: agent for agent in store.agents()}
    history = store.assignment_history()
    best: tuple[int, Agent] | None = None
    for chief in sorted(chiefs, key=lambda agent: agent.agent_id):
        vocabulary: set[str] = set()
        for goal in goals_by_agent.get(chief.agent_id, []):
            vocabulary |= _tokens(goal.statement)
        for team in store.teams():
            if team.crew_chief_id != chief.agent_id:
                continue
            for member_id in team.members:
                member = agents_by_id.get(member_id)
                if member is None:
                    continue
                for specialty in member.specialties:
                    vocabulary |= _tokens(specialty)
                vocabulary |= derived_specialty_tokens(store, member, history=history)
        score = len(item_tokens & vocabulary)
        if score > 0 and (best is None or score > best[0]):
            best = (score, chief)
    if best is not None:
        return best[1]
    return chiefs[0]


def _intake_assignment_text(item: dict[str, Any]) -> str:
    return (
        f"Review ingested item '{item['title']}': decide whether it advances "
        "the mission, create the follow-up subtasks for your team, or close "
        "it with a rationale."
    )


def _tokens(text: str) -> set[str]:
    return {token for token in text.lower().split() if len(token) > 2}


def _priority(value: str) -> Priority:
    try:
        return Priority(value.strip().lower())
    except ValueError:
        return Priority.NORMAL
