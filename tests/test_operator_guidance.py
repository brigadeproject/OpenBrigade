"""Operator guidance for any live task: attach_operator_guidance, the chat
guidance_assignment_id target, and the explicit prompt rendering."""

from __future__ import annotations

import pytest

from brigade.auth import AuthResult
from brigade.runner import build_assignment_prompt
from brigade.schemas import Agent, Assignment, AssignmentStatus, Role, User
from brigade.services import (
    MAX_OPERATOR_GUIDANCE_ENTRIES,
    AssignmentActionError,
    attach_operator_guidance,
    send_user_chat,
)
from brigade.state import JsonStateStore
from tests.helpers import TestProvider


def _store(tmp_path) -> JsonStateStore:
    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(Agent("sage", "SAGE", "workspace-sage", "planner"))
    return store


def _assignment(store, *, status: AssignmentStatus = AssignmentStatus.QUEUED) -> Assignment:
    assignment = Assignment(
        assignment="Design the Stage 3 sovereignty server",
        assigned_to="sage",
        created_by="orchestrator",
        source="test",
    )
    if status != AssignmentStatus.QUEUED:
        assignment.transition_to(AssignmentStatus.ASSIGNED)
        if status not in {AssignmentStatus.ASSIGNED}:
            assignment.transition_to(status)
    store.add_assignment(assignment)
    return assignment


def test_guidance_attaches_to_queued_task(tmp_path):
    store = _store(tmp_path)
    queued = _assignment(store)

    result = attach_operator_guidance(
        store,
        queued.assignment_id,
        operator="tm",
        message="Prefer gpt-oss:20b; see https://example.com/local-llms for options.",
    )

    assert result == {
        "assignment_id": queued.assignment_id,
        "status": "queued",
        "resumed": False,
    }
    refreshed = store.find_assignment(queued.assignment_id)
    assert len(refreshed.operator_guidance) == 1
    entry = refreshed.operator_guidance[0]
    assert entry["operator"] == "tm"
    assert "gpt-oss:20b" in entry["operator_message"]
    # Operator sees a confirmation in the default conversation channel.
    notes = [
        message
        for message in store.messages("user:tm:sage")
        if message.metadata.get("kind") == "chat_guidance_applied"
    ]
    assert len(notes) == 1
    assert "next run" in notes[0].content


def test_guidance_attaches_to_working_task_without_status_change(tmp_path):
    store = _store(tmp_path)
    working = _assignment(store, status=AssignmentStatus.WORKING)

    result = attach_operator_guidance(
        store, working.assignment_id, operator="tm", message="Focus on security."
    )

    assert result["status"] == "working"
    assert result["resumed"] is False
    refreshed = store.find_assignment(working.assignment_id)
    assert refreshed.status == AssignmentStatus.WORKING
    assert refreshed.awaiting_human is False
    assert len(refreshed.operator_guidance) == 1


def test_guidance_rejects_terminal_and_unknown(tmp_path):
    store = _store(tmp_path)
    done = _assignment(store, status=AssignmentStatus.COMPLETE)

    with pytest.raises(AssignmentActionError):
        attach_operator_guidance(
            store, done.assignment_id, operator="tm", message="too late"
        )
    with pytest.raises(AssignmentActionError):
        attach_operator_guidance(
            store, "no-such-id", operator="tm", message="nobody home"
        )


def test_guidance_resumes_awaiting_human_blocked_task(tmp_path):
    store = _store(tmp_path)
    blocked = _assignment(store, status=AssignmentStatus.ASSIGNED)
    blocked.register_failure("hung: no progress", awaiting_human=True)
    store.update_assignment(blocked)
    assert store.find_assignment(blocked.assignment_id).status == AssignmentStatus.BLOCKED

    result = attach_operator_guidance(
        store, blocked.assignment_id, operator="tm", message="Try the other API."
    )

    assert result["resumed"] is True
    refreshed = store.find_assignment(blocked.assignment_id)
    assert refreshed.status == AssignmentStatus.ASSIGNED
    assert refreshed.awaiting_human is False
    assert len(refreshed.operator_guidance) == 1


def test_guidance_entries_are_capped(tmp_path):
    store = _store(tmp_path)
    queued = _assignment(store)

    for index in range(MAX_OPERATOR_GUIDANCE_ENTRIES + 2):
        attach_operator_guidance(
            store, queued.assignment_id, operator="tm", message=f"note {index}"
        )

    refreshed = store.find_assignment(queued.assignment_id)
    assert len(refreshed.operator_guidance) == MAX_OPERATOR_GUIDANCE_ENTRIES
    assert refreshed.operator_guidance[-1]["operator_message"] == (
        f"note {MAX_OPERATOR_GUIDANCE_ENTRIES + 1}"
    )


def test_chat_with_guidance_target_attaches_to_queued_task(tmp_path):
    store = _store(tmp_path)
    user = User(username="alice", role=Role.OPERATOR)
    store.add_user(user)
    queued = _assignment(store)

    result = send_user_chat(
        store,
        AuthResult(ok=True, method="test", user=user),
        user=user,
        agent_id="sage",
        content="Use local models only; here are the candidates I like.",
        provider=TestProvider(text="Noted, thanks."),
        guidance_assignment_id=queued.assignment_id,
    )

    assert result["status"] == "complete"
    assert result["guidance_attached"] == {
        "assignment_id": queued.assignment_id,
        "status": "queued",
        "resumed": False,
    }
    refreshed = store.find_assignment(queued.assignment_id)
    assert "local models only" in refreshed.operator_guidance[0]["operator_message"]


def test_chat_with_bad_guidance_target_does_not_fail_the_chat(tmp_path):
    store = _store(tmp_path)
    user = User(username="alice", role=Role.OPERATOR)
    store.add_user(user)

    result = send_user_chat(
        store,
        AuthResult(ok=True, method="test", user=user),
        user=user,
        agent_id="sage",
        content="Steer that task please.",
        provider=TestProvider(text="Which task?"),
        guidance_assignment_id="no-such-id",
    )

    assert result["status"] == "complete"
    assert result["guidance_attached"]["assignment_id"] == "no-such-id"
    assert "unknown assignment" in result["guidance_attached"]["error"]


def test_resume_and_guidance_target_do_not_double_attach(tmp_path):
    """Targeting an escalated task that resume_escalations already handled
    must not append the same message twice."""
    store = _store(tmp_path)
    user = User(username="alice", role=Role.OPERATOR)
    store.add_user(user)
    escalated = _assignment(store, status=AssignmentStatus.ASSIGNED)
    escalated.register_failure("hung", awaiting_human=True)
    store.update_assignment(escalated)

    result = send_user_chat(
        store,
        AuthResult(ok=True, method="test", user=user),
        user=user,
        agent_id="sage",
        content="Same message for both paths.",
        provider=TestProvider(text="OK."),
        resume_escalations=True,
        guidance_assignment_id=escalated.assignment_id,
    )

    assert result["assignments_resumed"] == [
        {"assignment_id": escalated.assignment_id, "status": "assigned"}
    ]
    assert result["guidance_attached"] is None
    refreshed = store.find_assignment(escalated.assignment_id)
    assert len(refreshed.operator_guidance) == 1


def test_guidance_route_attaches_and_maps_errors(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from brigade.config import Settings
    from brigade.web import create_app

    store = _store(tmp_path)
    queued = _assignment(store)
    settings = Settings(config_path=tmp_path / "brigade.config.json", data_dir=tmp_path)
    client = TestClient(create_app(settings, store))

    ok = client.post(
        f"/api/tasks/{queued.assignment_id}/guidance",
        json={"message": "Use the smaller model first."},
    )
    assert ok.status_code == 200
    assert ok.json()["assignment_id"] == queued.assignment_id
    refreshed = store.find_assignment(queued.assignment_id)
    assert "smaller model" in refreshed.operator_guidance[0]["operator_message"]

    assert (
        client.post("/api/tasks/nope/guidance", json={"message": "x"}).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/tasks/{queued.assignment_id}/guidance", json={"message": "  "}
        ).status_code
        == 422
    )


def test_prompt_renders_operator_guidance_block(tmp_path):
    store = _store(tmp_path)
    agent = store.agents()[0]
    queued = _assignment(store)
    plain = build_assignment_prompt(agent, queued, store)
    assert "OPERATOR GUIDANCE" not in plain

    attach_operator_guidance(
        store, queued.assignment_id, operator="tm", message="Ship it lean."
    )
    guided = store.find_assignment(queued.assignment_id)
    prompt = build_assignment_prompt(agent, guided, store)
    assert "OPERATOR GUIDANCE (direct instruction from the human operator" in prompt
    assert "tm: Ship it lean." in prompt
