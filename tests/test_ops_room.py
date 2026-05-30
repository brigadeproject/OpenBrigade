from __future__ import annotations

import pytest

from brigade.config import Settings
from brigade.schemas import Agent, AgentState, Assignment, AssignmentStatus, Role, User
from brigade.services import OPS_ROOM_LAYOUT_KEY, build_ops_room_payload
from brigade.state import JsonStateStore


def test_ops_room_snapshot_maps_agent_state_and_layout(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(Agent("sage", "SAGE", "workspace-sage", "planner"))
    assignment = Assignment(
        assignment="Draft launch plan",
        assigned_to="sage",
        created_by="human",
        source="test",
    )
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    store.add_assignment(assignment)
    store.upsert_agent_state(
        AgentState(
            agent="sage",
            status="working",
            current_assignment_id=assignment.assignment_id,
            current_assignment_summary=assignment.assignment,
        )
    )
    store.set_ui_layout(
        "alice",
        OPS_ROOM_LAYOUT_KEY,
        {"version": 1, "seats": [{"agent_id": "sage", "x": 4, "y": 14}]},
    )

    payload = build_ops_room_payload(
        store,
        layout=store.ui_layout("alice", OPS_ROOM_LAYOUT_KEY),
    )

    assert payload["version"] == 1
    assert payload["agents"][0]["status"] == "working"
    assert payload["agents"][0]["activity"] == "typing"
    assert payload["agents"][0]["current_assignment"]["assignment"] == "Draft launch plan"
    assert payload["layout"]["seats"] == [{"agent_id": "sage", "x": 4, "y": 14}]


def test_json_state_store_keeps_per_user_ops_room_layouts(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")

    store.set_ui_layout(
        "alice",
        OPS_ROOM_LAYOUT_KEY,
        {"seats": [{"agent_id": "sage", "x": 2, "y": 3}]},
    )
    store.set_ui_layout(
        "bob",
        OPS_ROOM_LAYOUT_KEY,
        {"seats": [{"agent_id": "sage", "x": 8, "y": 9}]},
    )

    assert store.ui_layout("alice", OPS_ROOM_LAYOUT_KEY)["seats"][0]["x"] == 2
    assert store.ui_layout("bob", OPS_ROOM_LAYOUT_KEY)["seats"][0]["x"] == 8


def test_ops_room_web_routes_are_registered(tmp_path):
    pytest.importorskip("fastapi")

    from brigade.web import create_app

    store = JsonStateStore(tmp_path / "state.json")
    owner = User(username="owner", role=Role.OWNER)
    store.add_user(owner)
    store.add_agent(Agent("sage", "SAGE", "workspace-sage", "planner"))
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        require_auth=True,
        jwt_secret="x" * 40,
    )
    app = create_app(settings, store)
    paths = {route.path for route in app.routes}

    assert "/api/ops-room" in paths
    assert "/api/ops-room/events" in paths
    assert "/api/ops-room/layout" in paths
    assert "/api/mission" in paths
    assert "/api/goals" in paths
