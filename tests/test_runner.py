from __future__ import annotations

from brigade.providers import FakeProvider
from brigade.runner import run_agent_once
from brigade.schemas import Agent, Assignment
from brigade.state import JsonStateStore
from brigade.workspace import write_heartbeat_assignment


def test_run_agent_once_completes_and_archives_assignment(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agent = Agent(agent_id="sage", display_name="SAGE", workspace_path="workspace-sage")
    assignment = Assignment(
        assignment="Draft a plan",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    assignment.transition_to(status=assignment.status.ASSIGNED)
    store.add_agent(agent)
    store.add_assignment(assignment)
    write_heartbeat_assignment(agent, assignment, tmp_path)

    result = run_agent_once("sage", store, FakeProvider())

    assert result.status == "complete"
    assert store.assignments() == []
    assert store.assignment_history()[0]["assignment_id"] == assignment.assignment_id
    heartbeat = tmp_path / "workspace-sage" / "HEARTBEAT.md"
    assert '"status": "complete"' in heartbeat.read_text(encoding="utf-8")
