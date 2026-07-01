from __future__ import annotations

import json

from brigade.auth import AuthResult
from brigade.ladder import create_failure_analysis
from brigade.prompt_floors import orchestrator_system_prompt, read_orchestrator_notes
from brigade.schemas import (
    Agent,
    Assignment,
    AssignmentKind,
    AssignmentStatus,
    Role,
    Team,
    User,
)
from brigade.services import apply_orchestrator_chat_actions, send_orchestrator_chat
from brigade.state import JsonStateStore
from tests.helpers import TestProvider


def _store_with_owner(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    owner = User(username="owner", role=Role.OWNER)
    store.add_user(owner)
    return store, owner


def _actions_provider(actions: list[dict], summary: str = "Doing the thing.") -> TestProvider:
    return TestProvider(
        text=json.dumps({"status": "propose_actions", "summary": summary, "actions": actions})
    )


def test_propose_actions_stages_without_applying(tmp_path):
    store, owner = _store_with_owner(tmp_path)
    store.add_agent(Agent("ada", "ADA", "workspace-ada"))
    assignment = Assignment(
        assignment="Do something",
        assigned_to="ada",
        created_by="human",
        source="direct_command",
    )
    store.add_assignment(assignment)
    provider = _actions_provider(
        [{"type": "cancel_assignment", "assignment_id": assignment.assignment_id}]
    )

    result = send_orchestrator_chat(
        store,
        AuthResult(ok=True, method="test", user=owner),
        user=owner,
        content="cancel that task",
        provider=provider,
    )

    assert result["status"] == "proposed"
    assert result["actions_proposed"][0]["type"] == "cancel_assignment"
    assert store.find_assignment(assignment.assignment_id).status == AssignmentStatus.QUEUED
    response = store.messages("orchestrator")[-1]
    assert response.metadata["kind"] == "orchestrator_chat_proposal"
    assert "confirm" in response.content.lower()


def test_confirm_applies_the_staged_action(tmp_path):
    store, owner = _store_with_owner(tmp_path)
    store.add_agent(Agent("ada", "ADA", "workspace-ada"))
    assignment = Assignment(
        assignment="Do something",
        assigned_to="ada",
        created_by="human",
        source="direct_command",
    )
    store.add_assignment(assignment)
    provider = _actions_provider(
        [{"type": "cancel_assignment", "assignment_id": assignment.assignment_id}]
    )
    send_orchestrator_chat(
        store,
        AuthResult(ok=True, method="test", user=owner),
        user=owner,
        content="cancel that task",
        provider=provider,
    )

    result = send_orchestrator_chat(
        store,
        AuthResult(ok=True, method="test", user=owner),
        user=owner,
        content="confirm",
        provider=provider,
    )

    assert result["status"] == "applied"
    assert result["actions_applied"][0]["type"] == "cancel_assignment"
    assert store.find_assignment(assignment.assignment_id) is None
    archived = store.assignment_history()[-1]
    assert archived["final_status"] == AssignmentStatus.SUPERSEDED.value


def test_decline_discards_the_staged_action(tmp_path):
    store, owner = _store_with_owner(tmp_path)
    store.add_agent(Agent("ada", "ADA", "workspace-ada"))
    assignment = Assignment(
        assignment="Do something",
        assigned_to="ada",
        created_by="human",
        source="direct_command",
    )
    store.add_assignment(assignment)
    provider = _actions_provider(
        [{"type": "cancel_assignment", "assignment_id": assignment.assignment_id}]
    )
    send_orchestrator_chat(
        store,
        AuthResult(ok=True, method="test", user=owner),
        user=owner,
        content="cancel that task",
        provider=provider,
    )

    result = send_orchestrator_chat(
        store,
        AuthResult(ok=True, method="test", user=owner),
        user=owner,
        content="cancel",
        provider=provider,
    )

    assert result["status"] == "declined"
    assert store.find_assignment(assignment.assignment_id).status == AssignmentStatus.QUEUED


def test_plain_question_does_not_stage_anything(tmp_path):
    store, owner = _store_with_owner(tmp_path)

    result = send_orchestrator_chat(
        store,
        AuthResult(ok=True, method="test", user=owner),
        user=owner,
        content="what needs attention?",
        provider=TestProvider(),
    )

    assert result["status"] == "complete"
    assert "actions_proposed" not in result
    response = store.messages("orchestrator")[-1]
    assert response.metadata["kind"] == "orchestrator_chat_response"


def test_a_bare_confirm_with_no_pending_proposal_is_a_normal_message(tmp_path):
    store, owner = _store_with_owner(tmp_path)

    result = send_orchestrator_chat(
        store,
        AuthResult(ok=True, method="test", user=owner),
        user=owner,
        content="confirm",
        provider=TestProvider(),
    )

    assert result["status"] == "complete"


def test_set_routing_policy_action_routes_future_failure_analysis(tmp_path):
    store, owner = _store_with_owner(tmp_path)
    store.add_agent(Agent("sage", "SAGE", "workspace-sage", role="crew_chief"))
    store.add_agent(Agent("ada", "ADA", "workspace-ada", team_id="alpha"))
    store.add_agent(Agent("bolt", "BOLT", "workspace-bolt", role="crew_chief"))
    store.upsert_team(
        Team(team_id="alpha", display_name="Alpha", crew_chief_id="sage", members=["ada"])
    )
    store.upsert_team(
        Team(team_id="infra", display_name="Infra", crew_chief_id="bolt", members=["bolt"])
    )
    provider = _actions_provider(
        [
            {
                "type": "set_routing_policy",
                "assignment_kind": "failure_analysis",
                "target_team_id": "infra",
                "statement": "All failure analysis tasks go to the infrastructure team.",
            }
        ]
    )
    send_orchestrator_chat(
        store,
        AuthResult(ok=True, method="test", user=owner),
        user=owner,
        content="route all failure analysis work to infra",
        provider=provider,
    )
    result = send_orchestrator_chat(
        store,
        AuthResult(ok=True, method="test", user=owner),
        user=owner,
        content="confirm",
        provider=provider,
    )
    assert result["status"] == "applied"
    assert len(store.orchestrator_policies()) == 1

    blocked = Assignment(
        assignment="Fix the bug",
        assigned_to="ada",
        created_by="human",
        source="direct_command",
    )
    store.add_assignment(blocked)
    blocked.transition_to(AssignmentStatus.ASSIGNED)
    blocked.register_failure("error 1")
    blocked.register_failure("error 2")
    store.update_assignment(blocked)

    create_failure_analysis(store, blocked)

    children = [
        item for item in store.assignments() if item.parent_assignment_id == blocked.assignment_id
    ]
    assert len(children) == 1
    assert children[0].assigned_to == "bolt"


def test_write_note_and_update_system_prompt_actions_persist_to_workspace(tmp_path):
    store, _owner = _store_with_owner(tmp_path)

    result = apply_orchestrator_chat_actions(
        store,
        [
            {"type": "write_note", "content": "Ops prefers ollama for local agents."},
            {"type": "update_system_prompt", "content": "You are the Brigade Orchestrator v2."},
        ],
        by="owner",
    )

    assert [item["type"] for item in result["applied"]] == ["write_note", "update_system_prompt"]
    assert "ollama" in read_orchestrator_notes(store)
    assert orchestrator_system_prompt(store) == "You are the Brigade Orchestrator v2."


def test_cancel_assignments_where_action_bulk_cancels_by_kind(tmp_path):
    store, owner = _store_with_owner(tmp_path)
    store.add_agent(Agent("sage", "SAGE", "workspace-sage", role="crew_chief"))
    for _ in range(2):
        store.add_assignment(
            Assignment(
                assignment="Diagnose",
                assigned_to="sage",
                created_by="orchestrator",
                source="orchestrator_ladder",
                kind=AssignmentKind.FAILURE_ANALYSIS,
            )
        )
    store.add_assignment(
        Assignment(
            assignment="Real work",
            assigned_to="sage",
            created_by="human",
            source="direct_command",
        )
    )
    provider = _actions_provider(
        [{"type": "cancel_assignments_where", "kind": "failure_analysis"}]
    )
    send_orchestrator_chat(
        store,
        AuthResult(ok=True, method="test", user=owner),
        user=owner,
        content="cancel all failure analysis tasks",
        provider=provider,
    )

    result = send_orchestrator_chat(
        store,
        AuthResult(ok=True, method="test", user=owner),
        user=owner,
        content="confirm",
        provider=provider,
    )

    assert result["status"] == "applied"
    remaining = store.assignments()
    assert len(remaining) == 1
    assert remaining[0].kind == AssignmentKind.MISSION
