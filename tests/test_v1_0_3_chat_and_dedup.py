"""1.0.3: operator chat replies reach escalated assignments; delegation dedup
covers recently completed work, not just the undone backlog."""

from __future__ import annotations

from datetime import timedelta

from brigade.auth import AuthResult
from brigade.schemas import Agent, Assignment, AssignmentStatus, Role, User
from brigade.services import send_user_chat
from brigade.state import JsonStateStore
from brigade.time import parse_utc_iso
from brigade.tools import ToolContext, default_tool_registry
from tests.helpers import TestProvider


def _escalated_assignment(agent_id: str = "sage") -> Assignment:
    assignment = Assignment(
        assignment="Define organizational roles for the non-profit",
        assigned_to=agent_id,
        created_by="orchestrator",
        source="test",
    )
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    assignment.register_failure("hung: no progress", awaiting_human=True)
    return assignment


def test_chat_reply_attaches_guidance_and_requeues_escalated_assignment(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    user = User(username="alice", role=Role.OPERATOR)
    store.add_user(user)
    store.add_agent(Agent("sage", "SAGE", "workspace-sage", "planner"))
    escalated = _escalated_assignment()
    store.add_assignment(escalated)

    result = send_user_chat(
        store,
        AuthResult(ok=True, method="test", user=user),
        user=user,
        agent_id="sage",
        content="Keep the board lean: founder plus two independent directors.",
        provider=TestProvider(text="Understood, resuming with that structure."),
        resume_escalations=True,
    )

    assert result["status"] == "complete"
    assert result["assignments_resumed"] == [
        {"assignment_id": escalated.assignment_id, "status": "assigned"}
    ]
    refreshed = store.find_assignment(escalated.assignment_id)
    assert refreshed is not None
    assert refreshed.status == AssignmentStatus.ASSIGNED
    assert refreshed.awaiting_human is False
    assert refreshed.consecutive_failures == 0
    guidance = refreshed.operator_guidance
    assert len(guidance) == 1
    assert guidance[0]["operator"] == "alice"
    assert "founder plus two independent directors" in guidance[0]["operator_message"]
    # Only the operator's words are guidance — the agent's chat reply is its own
    # speculation and must not be re-injected as instructions.
    assert "agent_reply" not in guidance[0]
    # The channel gets a visible note so the operator can see the effect.
    notes = [
        message
        for message in store.messages(result["conversation_id"])
        if message.metadata.get("kind") == "chat_guidance_applied"
    ]
    assert len(notes) == 1
    assert escalated.assignment_id in notes[0].content
    # Guidance rides into the agent's next prompt via the assignment floor.
    from brigade.runner import build_assignment_prompt

    prompt = build_assignment_prompt(
        Agent("sage", "SAGE", "workspace-sage", "planner"), refreshed, store
    )
    assert "founder plus two independent directors" in prompt


def test_chat_without_resume_flag_leaves_escalation_parked(tmp_path):
    """A diagnostic question ("what's blocking you?") must not burn the escalation."""
    store = JsonStateStore(tmp_path / "state.json")
    user = User(username="alice", role=Role.OPERATOR)
    store.add_user(user)
    store.add_agent(Agent("sage", "SAGE", "workspace-sage", "planner"))
    escalated = _escalated_assignment()
    store.add_assignment(escalated)

    result = send_user_chat(
        store,
        AuthResult(ok=True, method="test", user=user),
        user=user,
        agent_id="sage",
        content="What exactly is holding you up?",
        provider=TestProvider(text="I'm waiting on the board size decision."),
    )

    assert result["assignments_resumed"] == []
    refreshed = store.find_assignment(escalated.assignment_id)
    assert refreshed is not None
    assert refreshed.status == AssignmentStatus.BLOCKED
    assert refreshed.awaiting_human is True
    assert refreshed.operator_guidance == []


def test_chat_without_escalation_leaves_assignments_alone(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    user = User(username="alice", role=Role.OPERATOR)
    store.add_user(user)
    store.add_agent(Agent("sage", "SAGE", "workspace-sage", "planner"))
    working = Assignment(
        assignment="Ongoing work",
        assigned_to="sage",
        created_by="orchestrator",
        source="test",
    )
    working.transition_to(AssignmentStatus.ASSIGNED)
    store.add_assignment(working)

    result = send_user_chat(
        store,
        AuthResult(ok=True, method="test", user=user),
        user=user,
        agent_id="sage",
        content="How is it going?",
        provider=TestProvider(text="Fine."),
    )

    assert result["assignments_resumed"] == []
    refreshed = store.find_assignment(working.assignment_id)
    assert refreshed is not None
    assert refreshed.status == AssignmentStatus.ASSIGNED
    assert refreshed.operator_guidance == []


def test_chat_guidance_is_capped(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    user = User(username="alice", role=Role.OPERATOR)
    store.add_user(user)
    store.add_agent(Agent("sage", "SAGE", "workspace-sage", "planner"))
    escalated = _escalated_assignment()
    escalated.operator_guidance = [
        {"at": "2026-07-08T00:00:00+00:00", "operator": "alice", "operator_message": str(i)}
        for i in range(5)
    ]
    store.add_assignment(escalated)

    send_user_chat(
        store,
        AuthResult(ok=True, method="test", user=user),
        user=user,
        agent_id="sage",
        content="newest guidance",
        provider=TestProvider(text="ok"),
        resume_escalations=True,
    )

    refreshed = store.find_assignment(escalated.assignment_id)
    assert refreshed is not None
    assert len(refreshed.operator_guidance) == 5
    assert refreshed.operator_guidance[-1]["operator_message"] == "newest guidance"
    assert refreshed.operator_guidance[0]["operator_message"] == "1"


def _delegation_fixture(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    chief = Agent("chief", "CHIEF", "workspace-chief", "crew_chief")
    worker = Agent("worker", "WORKER", "workspace-worker")
    parent = Assignment(
        assignment="Break down mission work",
        assigned_to="chief",
        created_by="human",
        source="test",
    )
    parent.transition_to(AssignmentStatus.ASSIGNED)
    store.add_agent(chief)
    store.add_agent(worker)
    store.add_assignment(parent)
    return store, chief, parent


def _archive_completed(store, *, assigned_to: str, text: str, summary: str) -> Assignment:
    done = Assignment(
        assignment=text,
        assigned_to=assigned_to,
        created_by="chief",
        source="agent_delegate",
    )
    done.transition_to(AssignmentStatus.ASSIGNED)
    done.mark_complete(summary)
    store.archive_assignment(done, summary)
    return done


def test_delegate_dedups_against_recently_completed_history(tmp_path):
    store, chief, parent = _delegation_fixture(tmp_path)
    done = _archive_completed(
        store,
        assigned_to="worker",
        text="Create operational_roles.md defining roles for the non-profit",
        summary="Created shared/operational_roles.md",
    )

    result = default_tool_registry().execute(
        "delegate",
        ToolContext(agent=chief, assignment=parent, store=store),
        {
            "agent_id": "worker",
            "assignment": "Create operational_roles.md defining the roles for the non-profit",
        },
    )

    assert result.ok is True
    assert result.metadata["deduplicated"] is True
    assert result.metadata["already_completed"] is True
    assert result.metadata["assignment_id"] == done.assignment_id
    assert "already COMPLETED" in result.output
    assert "Created shared/operational_roles.md" in result.output
    # No new assignment was queued.
    assert [item.assignment_id for item in store.assignments()] == [parent.assignment_id]


def test_delegate_ignores_completed_history_outside_window(tmp_path):
    store, chief, parent = _delegation_fixture(tmp_path)
    done = _archive_completed(
        store,
        assigned_to="worker",
        text="Create operational_roles.md defining roles for the non-profit",
        summary="Created shared/operational_roles.md",
    )
    # Age the archived record beyond the 24h window.
    state = store.load()
    old = parse_utc_iso(state["assignment_history"][0]["archived_at"]) - timedelta(days=2)
    state["assignment_history"][0]["archived_at"] = old.isoformat()
    store.save(state)

    result = default_tool_registry().execute(
        "delegate",
        ToolContext(agent=chief, assignment=parent, store=store),
        {
            "agent_id": "worker",
            "assignment": "Create operational_roles.md defining the roles for the non-profit",
        },
    )

    assert result.ok is True
    assert not (result.metadata or {}).get("deduplicated")
    created = [item for item in store.assignments() if item.assignment_id != parent.assignment_id]
    assert len(created) == 1
    assert created[0].assignment_id != done.assignment_id


def test_delegate_allows_different_work_for_same_agent(tmp_path):
    store, chief, parent = _delegation_fixture(tmp_path)
    _archive_completed(
        store,
        assigned_to="worker",
        text="Create operational_roles.md defining roles for the non-profit",
        summary="Created shared/operational_roles.md",
    )

    result = default_tool_registry().execute(
        "delegate",
        ToolContext(agent=chief, assignment=parent, store=store),
        {
            "agent_id": "worker",
            "assignment": "Draft the Rhode Island 501(c)(3) filing checklist with deadlines",
        },
    )

    assert result.ok is True
    assert not (result.metadata or {}).get("deduplicated")


def test_create_subtasks_dedups_against_completed_history(tmp_path):
    store, chief, parent = _delegation_fixture(tmp_path)
    done = _archive_completed(
        store,
        assigned_to="worker",
        text="Create governance_model.md detailing the governance structure",
        summary="Created shared/governance_model.md",
    )

    result = default_tool_registry().execute(
        "create_subtasks",
        ToolContext(agent=chief, assignment=parent, store=store),
        {
            "subtasks": [
                {
                    "agent_id": "worker",
                    "assignment": "Create governance_model.md detailing the governance structure",
                },
                {
                    "agent_id": "worker",
                    "assignment": "Write the fundraising plan for year one",
                    "depends_on_previous": True,
                },
            ]
        },
    )

    assert result.ok is True
    entries = (result.metadata or {}).get("created") or []
    assert any(entry.get("already_completed") for entry in entries)
    new_assignments = [
        item for item in store.assignments() if item.assignment_id != parent.assignment_id
    ]
    # Only the genuinely-new subtask was queued, anchored on the archived id.
    assert len(new_assignments) == 1
    assert new_assignments[0].dependency_ids == [done.assignment_id]
