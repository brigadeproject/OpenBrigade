"""Derived agent capability profiles and their prompt/floor surfacing."""

from __future__ import annotations

import json

from brigade.profile import derive_agent_profile, derived_specialty_tokens
from brigade.prompt_floors import build_agent_floor, build_agent_load
from brigade.schemas import Agent, Assignment, AssignmentStatus
from brigade.state import JsonStateStore
from brigade.tools import default_tool_registry
from brigade.workspace import write_heartbeat_assignment


def _complete(store, agent_id: str, text: str, summary: str) -> Assignment:
    assignment = Assignment(
        assignment=text,
        assigned_to=agent_id,
        created_by="human",
        source="direct_command",
    )
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    assignment.transition_to(AssignmentStatus.COMPLETE)
    store.archive_assignment(assignment, summary)
    return assignment


def _agent(store, tmp_path, agent_id="designer", specialties=None) -> Agent:
    agent = Agent(
        agent_id=agent_id,
        display_name=agent_id.upper(),
        workspace_path=f"workspace-{agent_id}",
        specialties=specialties or [],
    )
    store.add_agent(agent)
    (tmp_path / agent.workspace_path).mkdir(parents=True, exist_ok=True)
    return agent


def test_derive_agent_profile_from_history_and_tools(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agent = _agent(store, tmp_path, specialties=["css"])
    _complete(store, "designer", "Build the telemetry dashboard layout", "telemetry layout shipped")
    _complete(store, "designer", "Refine telemetry dashboard charts", "chart polish done")
    latest = _complete(store, "designer", "Draft the css style guide", "style guide written")
    _complete(store, "researcher", "Summarize telemetry papers", "unrelated agent")

    tools_dir = tmp_path / agent.workspace_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "chartgen.json").write_text(
        json.dumps({"name": "chartgen", "description": "render svg charts"}),
        encoding="utf-8",
    )

    profile = derive_agent_profile(store, agent)

    assert "telemetry" in profile["derived_specialties"]
    assert "dashboard" in profile["derived_specialties"]
    # Curated specialties are not repeated as derived, and one-off tokens
    # below the recurrence floor stay out.
    assert "css" not in profile["derived_specialties"]
    assert "guide" not in profile["derived_specialties"]
    assert profile["built_tools"] == [
        {"name": "chartgen", "description": "render svg charts"}
    ]
    assert profile["recent_completions"][0] == {
        "assignment_id": latest.assignment_id,
        "summary": "style guide written",
    }
    assert len(profile["recent_completions"]) == 3

    tokens = derived_specialty_tokens(store, agent)
    assert {"telemetry", "dashboard", "chartgen", "svg", "charts"} <= tokens


def test_derive_agent_profile_empty_history(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agent = _agent(store, tmp_path)

    profile = derive_agent_profile(store, agent)

    assert profile == {
        "built_tools": [],
        "derived_specialties": [],
        "recent_completions": [],
    }


def test_build_agent_load_includes_profile(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    _agent(store, tmp_path, specialties=["css"])
    _complete(store, "designer", "Build the telemetry dashboard layout", "telemetry shipped")
    _complete(store, "designer", "Extend the telemetry dashboard", "more telemetry")

    rows = build_agent_load(store, ["designer"])

    assert len(rows) == 1
    row = rows[0]
    assert row["role"] == "line_worker"
    assert row["specialties"] == ["css"]
    assert "telemetry" in row["derived_specialties"]
    assert row["built_tools"] == []
    assert len(row["recent_completions"]) == 2


def test_build_agent_floor_injects_identity(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agent = _agent(store, tmp_path)
    assignment = Assignment(
        assignment="Design the landing page",
        assigned_to="designer",
        created_by="human",
        source="direct_command",
    )
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    store.add_assignment(assignment)
    write_heartbeat_assignment(agent, assignment, tmp_path)
    identity_path = tmp_path / agent.workspace_path / "IDENTITY.md"
    identity_path.write_text("# DESIGNER\nVisual design specialist." + "x" * 5000, encoding="utf-8")

    floor = build_agent_floor(agent, assignment, store, default_tool_registry())

    assert floor["identity"].startswith("# DESIGNER")
    assert len(floor["identity"]) == 4000


def test_build_agent_floor_omits_missing_identity(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agent = _agent(store, tmp_path)
    assignment = Assignment(
        assignment="Design the landing page",
        assigned_to="designer",
        created_by="human",
        source="direct_command",
    )
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    store.add_assignment(assignment)
    write_heartbeat_assignment(agent, assignment, tmp_path)
    # The workspace scaffold creates a default IDENTITY.md; remove it to
    # model an agent without one.
    (tmp_path / agent.workspace_path / "IDENTITY.md").unlink()

    floor = build_agent_floor(agent, assignment, store, default_tool_registry())

    assert "identity" not in floor
