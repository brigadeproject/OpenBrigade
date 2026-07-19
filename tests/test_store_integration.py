from __future__ import annotations

import os
from uuid import uuid4

import pytest

from brigade.db import apply_migrations
from brigade.schemas import (
    Agent,
    Assignment,
    AssignmentKind,
    Conversation,
    Goal,
    build_proposal,
    build_recurrence,
)
from brigade.store import PostgresStateStore

pytestmark = pytest.mark.integration


def _live_store(tmp_path) -> PostgresStateStore:
    dsn = os.environ.get("BRIGADE_POSTGRES_DSN")
    if not dsn:
        pytest.skip("BRIGADE_POSTGRES_DSN is required for live Postgres store tests")
    apply_migrations(dsn)
    return PostgresStateStore(dsn, tmp_path)


def test_postgres_proposal_crud_round_trips(tmp_path):
    store = _live_store(tmp_path)
    # Unique per run: the live database keeps rows across test sessions.
    key = f"test:{uuid4()}"
    proposal = build_proposal(
        kind="tool_request",
        title="integration csv summarizer",
        agent_id=None,
        idempotency_key=key,
    )

    store.add_proposal(proposal)
    duplicate = build_proposal(
        kind="tool_request",
        title="integration csv summarizer",
        idempotency_key=key,
    )
    persisted = store.add_proposal(duplicate)
    assert persisted["proposal_id"] == proposal["proposal_id"]

    found = store.find_proposal(proposal["proposal_id"])
    assert found is not None
    assert found["title"] == "integration csv summarizer"

    proposal["status"] = "rejected"
    store.update_proposal(proposal)
    assert any(
        item["proposal_id"] == proposal["proposal_id"]
        for item in store.proposals(kind="tool_request", status="rejected")
    )


def test_postgres_recurrence_crud_round_trips(tmp_path):
    store = _live_store(tmp_path)
    recurrence = build_recurrence(
        template={"assignment": "integration weekly report", "assigned_to": "abacus"},
        interval_seconds=604_800,
        next_due_at="2026-06-12T00:00:00+00:00",
    )

    store.add_recurrence(recurrence)
    assert any(
        item["recurrence_id"] == recurrence["recurrence_id"]
        for item in store.recurrences(enabled=True)
    )

    recurrence["enabled"] = False
    store.update_recurrence(recurrence)
    assert any(
        item["recurrence_id"] == recurrence["recurrence_id"]
        for item in store.recurrences(enabled=False)
    )


def test_postgres_conversation_crud_round_trips(tmp_path):
    store = _live_store(tmp_path)
    operator = f"op-{uuid4().hex[:12]}"

    first = store.resolve_active_conversation(operator, "front_desk")
    again = store.resolve_active_conversation(operator, "front_desk")
    assert again.thread_id == first.thread_id  # partial unique index holds

    chief = store.resolve_active_conversation(
        operator, "chief:sage", chief_agent_id="sage", team_id="alpha"
    )
    assert chief.thread_id != first.thread_id

    store.set_conversation_summary(first.thread_id, "- discussed the roadmap")
    reloaded = store.find_conversation(first.thread_id)
    assert reloaded is not None
    assert reloaded.rolling_summary == "- discussed the roadmap"

    active = {item.thread_id for item in store.conversations(operator, status="active")}
    assert active == {first.thread_id, chief.thread_id}

    # Archiving frees the active slot, so a fresh resolve mints a new thread.
    archived = Conversation(
        operator_username=operator,
        persona="front_desk",
        status="archived",
        thread_id=first.thread_id,
        created_at=first.created_at,
    )
    store.upsert_conversation(archived)
    replacement = store.resolve_active_conversation(operator, "front_desk")
    assert replacement.thread_id != first.thread_id


def test_postgres_persists_new_contract_columns(tmp_path):
    store = _live_store(tmp_path)
    # Unique per run: the live database keeps rows across test sessions.
    agent_id = f"it-{uuid4().hex[:12]}"
    store.add_agent(
        Agent(
            agent_id=agent_id,
            display_name="Integration Agent",
            workspace_path=f"workspace-{agent_id}",
            specialties=["python"],
        )
    )
    store.add_goal(
        agent_id,
        Goal(
            statement="Integration on-call goal",
            success_criteria=[],
            explicitly_not=[],
            set_by="human",
            engagement_mode="on_call",
        ),
    )
    assignment = store.add_assignment(
        Assignment(
            assignment="Integration rest assignment",
            assigned_to=agent_id,
            created_by="orchestrator",
            source="rest_scheduler",
            kind=AssignmentKind.REST,
            idempotency_key=f"rest-test:{uuid4()}",
        )
    )

    stored_agent = next(item for item in store.agents() if item.agent_id == agent_id)
    assert stored_agent.specialties == ["python"]
    stored_goal = store.goals(agent_id)[agent_id][0]
    assert stored_goal.engagement_mode == "on_call"
    stored_assignment = store.find_assignment(assignment.assignment_id)
    assert stored_assignment is not None
    assert stored_assignment.kind == AssignmentKind.REST
