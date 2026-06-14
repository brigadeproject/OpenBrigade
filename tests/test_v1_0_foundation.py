from __future__ import annotations

import json

import pytest

from brigade.cli import main
from brigade.config import load_settings
from brigade.schemas import (
    Agent,
    AgentState,
    Assignment,
    AssignmentKind,
    Goal,
    agent_from_dict,
    agent_state_from_dict,
    assignment_from_dict,
    build_proposal,
    build_recurrence,
    goal_from_dict,
)
from brigade.state import JsonStateStore


def test_assignment_kind_round_trips():
    assignment = Assignment(
        assignment="Diagnose the blocked task",
        assigned_to="sage",
        created_by="orchestrator",
        source="blocker_resolution_ladder",
        kind=AssignmentKind.FAILURE_ANALYSIS,
    )

    restored = assignment_from_dict(assignment.to_dict())

    assert restored.kind == AssignmentKind.FAILURE_ANALYSIS
    assert restored.to_dict()["kind"] == "failure_analysis"


def test_assignment_without_kind_loads_as_mission():
    assignment = Assignment(
        assignment="Pre-v1.0 record",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    payload = assignment.to_dict()
    payload.pop("kind")

    assert assignment_from_dict(payload).kind == AssignmentKind.MISSION


def test_goal_engagement_mode_round_trips_and_defaults():
    goal = Goal(
        statement="Keep infrastructure healthy",
        success_criteria=[],
        explicitly_not=[],
        set_by="human",
        engagement_mode="on_call",
    )

    assert goal_from_dict(goal.to_dict()).engagement_mode == "on_call"

    legacy = goal.to_dict()
    legacy.pop("engagement_mode")
    assert goal_from_dict(legacy).engagement_mode == "directive"


def test_goal_rejects_invalid_engagement_mode():
    with pytest.raises(ValueError, match="engagement_mode"):
        Goal(
            statement="Bad mode",
            success_criteria=[],
            explicitly_not=[],
            set_by="human",
            engagement_mode="standby",
        )


def test_agent_specialties_round_trip_and_default():
    agent = Agent(
        agent_id="ada",
        display_name="ADA",
        workspace_path="workspace-ada",
        specialties=["python", "finance"],
    )

    assert agent_from_dict(agent.to_dict()).specialties == ["python", "finance"]

    legacy = agent.to_dict()
    legacy.pop("specialties")
    assert agent_from_dict(legacy).specialties == []


def test_agent_state_idle_cycles_round_trip_and_default():
    state = AgentState(agent="ada", idle_cycles=4)

    assert agent_state_from_dict(state.to_dict()).idle_cycles == 4

    legacy = state.to_dict()
    legacy.pop("idle_cycles")
    assert agent_state_from_dict(legacy).idle_cycles == 0


def test_build_proposal_validates_kind_and_status():
    proposal = build_proposal(kind="tool_request", title="csv summarizer", agent_id="ada")

    assert proposal["status"] == "proposed"
    assert proposal["kind"] == "tool_request"
    assert proposal["proposal_id"]

    with pytest.raises(ValueError, match="proposal kind"):
        build_proposal(kind="wish", title="nope")
    with pytest.raises(ValueError, match="proposal status"):
        build_proposal(kind="efficiency", title="nope", status="maybe")


def test_build_recurrence_validates_template_and_interval():
    recurrence = build_recurrence(
        template={"assignment": "Weekly cost report", "assigned_to": "abacus"},
        interval_seconds=604_800,
        next_due_at="2026-06-12T00:00:00+00:00",
    )

    assert recurrence["enabled"] is True
    assert recurrence["interval_seconds"] == 604_800

    with pytest.raises(ValueError, match="assignment text"):
        build_recurrence(
            template={},
            interval_seconds=60,
            next_due_at="2026-06-12T00:00:00+00:00",
        )
    with pytest.raises(ValueError, match="interval_seconds"):
        build_recurrence(
            template={"assignment": "x"},
            interval_seconds=0,
            next_due_at="2026-06-12T00:00:00+00:00",
        )


def test_json_store_proposal_crud_round_trips(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    proposal = build_proposal(
        kind="efficiency",
        title="Weekly report recurs",
        agent_id="abacus",
        idempotency_key="efficiency:v1:abc",
    )

    store.add_proposal(proposal)
    duplicate = build_proposal(
        kind="efficiency",
        title="Weekly report recurs",
        agent_id="abacus",
        idempotency_key="efficiency:v1:abc",
    )
    persisted = store.add_proposal(duplicate)

    assert persisted["proposal_id"] == proposal["proposal_id"]
    assert len(store.proposals()) == 1
    assert store.proposals(kind="efficiency")[0]["title"] == "Weekly report recurs"
    assert store.proposals(kind="tool_request") == []
    assert store.find_proposal(proposal["proposal_id"]) is not None

    proposal["status"] = "approved"
    store.update_proposal(proposal)
    assert store.proposals(status="approved")[0]["proposal_id"] == proposal["proposal_id"]
    assert store.proposals(status="proposed") == []


def test_json_store_recurrence_crud_round_trips(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    recurrence = build_recurrence(
        template={"assignment": "Weekly cost report", "assigned_to": "abacus"},
        interval_seconds=604_800,
        next_due_at="2026-06-12T00:00:00+00:00",
    )

    store.add_recurrence(recurrence)
    assert len(store.recurrences()) == 1
    assert store.recurrences(enabled=True)[0]["recurrence_id"] == recurrence["recurrence_id"]

    recurrence["enabled"] = False
    recurrence["next_due_at"] = "2026-06-19T00:00:00+00:00"
    store.update_recurrence(recurrence)

    assert store.recurrences(enabled=True) == []
    assert store.recurrences(enabled=False)[0]["next_due_at"] == "2026-06-19T00:00:00+00:00"


def test_load_settings_orchestration_defaults(tmp_path):
    settings = load_settings(config_path=tmp_path / "missing.json", env_path=tmp_path / ".env")

    assert settings.intake_mode == "propose"
    assert settings.max_intake_assignments_per_cycle == 2
    assert settings.intake_route_chief is None
    assert settings.intake_default_priority == "normal"
    assert settings.rest_enabled is True
    assert settings.rest_window_start_utc == "03:00"
    assert settings.rest_window_end_utc == "05:00"
    assert settings.rest_idle_cycles_threshold == 6
    assert settings.rest_min_interval_seconds == 86_400
    assert settings.blocker_resolution_enabled is True
    assert settings.recurrence_detection_threshold == 3
    assert settings.recurrence_lookback_days == 14


def test_load_settings_orchestration_env_overrides(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "\n".join(
            [
                "BRIGADE_INTAKE_MODE=create",
                "BRIGADE_MAX_INTAKE_ASSIGNMENTS_PER_CYCLE=5",
                "BRIGADE_INTAKE_ROUTE_CHIEF=sage",
                "BRIGADE_INTAKE_DEFAULT_PRIORITY=high",
                "BRIGADE_REST_ENABLED=false",
                "BRIGADE_REST_WINDOW_START_UTC=01:30",
                "BRIGADE_REST_WINDOW_END_UTC=02:30",
                "BRIGADE_REST_IDLE_CYCLES_THRESHOLD=2",
                "BRIGADE_REST_MIN_INTERVAL_SECONDS=3600",
                "BRIGADE_BLOCKER_RESOLUTION_ENABLED=false",
                "BRIGADE_RECURRENCE_DETECTION_THRESHOLD=5",
                "BRIGADE_RECURRENCE_LOOKBACK_DAYS=30",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    settings = load_settings(config_path=tmp_path / "missing.json", env_path=env)

    assert settings.intake_mode == "create"
    assert settings.max_intake_assignments_per_cycle == 5
    assert settings.intake_route_chief == "sage"
    assert settings.intake_default_priority == "high"
    assert settings.rest_enabled is False
    assert settings.rest_window_start_utc == "01:30"
    assert settings.rest_window_end_utc == "02:30"
    assert settings.rest_idle_cycles_threshold == 2
    assert settings.rest_min_interval_seconds == 3600
    assert settings.blocker_resolution_enabled is False
    assert settings.recurrence_detection_threshold == 5
    assert settings.recurrence_lookback_days == 30


def test_orchestration_config_keys_are_editable(tmp_path):
    from brigade.services import SAFE_CONFIG_KEYS, set_config_value

    config_path = tmp_path / "brigade.config.json"
    for key in (
        "intake_mode",
        "max_intake_assignments_per_cycle",
        "intake_route_chief",
        "intake_default_priority",
        "rest_enabled",
        "rest_window_start_utc",
        "rest_window_end_utc",
        "rest_idle_cycles_threshold",
        "rest_min_interval_seconds",
        "blocker_resolution_enabled",
        "recurrence_detection_threshold",
        "recurrence_lookback_days",
    ):
        assert key in SAFE_CONFIG_KEYS, f"{key} must be operator-editable"

    set_config_value(config_path, "intake_mode", "create")
    set_config_value(config_path, "rest_window_start_utc", "00:00")
    set_config_value(config_path, "rest_enabled", "false")
    set_config_value(config_path, "recurrence_lookback_days", "30")
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["intake_mode"] == "create"
    assert saved["rest_window_start_utc"] == "00:00"
    assert saved["rest_enabled"] is False
    assert saved["recurrence_lookback_days"] == 30


def test_cli_task_create_with_kind(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["agent", "add", "--id", "sage", "--name", "SAGE", "--workspace", "ws"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "task",
                "create",
                "--agent",
                "sage",
                "--assignment",
                "Tidy the workspace",
                "--kind",
                "maintenance",
            ]
        )
        == 0
    )

    created = json.loads(capsys.readouterr().out)
    assert created["kind"] == "maintenance"


def test_cli_goal_add_with_engagement_mode(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["agent", "add", "--id", "garde", "--name", "GARDE", "--workspace", "ws"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "goal",
                "add",
                "--agent",
                "garde",
                "--statement",
                "Stand by for infrastructure work",
                "--not",
                "touch production",
                "--engagement-mode",
                "on_call",
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert created["engagement_mode"] == "on_call"

    store = JsonStateStore(tmp_path / ".brigade" / "state.json")
    assert store.goals("garde")["garde"][0].engagement_mode == "on_call"


def test_cli_agent_onboard_with_specialties(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert (
        main(
            [
                "agent",
                "onboard",
                "--id",
                "ada",
                "--name",
                "ADA",
                "--specialty",
                "python",
                "--specialty",
                "networking",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["agent"]["specialties"] == ["python", "networking"]

    store = JsonStateStore(tmp_path / ".brigade" / "state.json")
    assert store.agents()[0].specialties == ["python", "networking"]


def test_cli_proposal_list_approve_and_reject(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["init", "mvp", "--mission", "Prototype mission"]) == 0
    capsys.readouterr()

    store = JsonStateStore(tmp_path / ".brigade" / "state.json")
    first = build_proposal(kind="tool_request", title="csv summarizer", agent_id="sage")
    second = build_proposal(kind="efficiency", title="weekly report recurs", agent_id="abacus")
    store.add_proposal(first)
    store.add_proposal(second)

    assert main(["proposal", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert {item["proposal_id"] for item in listed} == {
        first["proposal_id"],
        second["proposal_id"],
    }

    assert main(["--as-user", "owner", "proposal", "approve", first["proposal_id"]]) == 0
    approved = json.loads(capsys.readouterr().out)
    assert approved["status"] == "approved"
    assert approved["decided_by"] == "owner"

    assert (
        main(
            [
                "--as-user",
                "owner",
                "proposal",
                "reject",
                second["proposal_id"],
                "--reason",
                "not repetitive enough",
            ]
        )
        == 0
    )
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["status"] == "rejected"
    assert rejected["details"]["decision_reason"] == "not repetitive enough"

    assert main(["proposal", "list", "--status", "proposed"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_cli_proposal_decision_requires_proposal_write(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["init", "mvp", "--mission", "Prototype mission"]) == 0
    capsys.readouterr()
    assert main(["user", "add", "--username", "obs", "--role", "observer"]) == 0
    capsys.readouterr()

    store = JsonStateStore(tmp_path / ".brigade" / "state.json")
    proposal = build_proposal(kind="tool_request", title="csv summarizer", agent_id="sage")
    store.add_proposal(proposal)

    with pytest.raises(PermissionError, match="proposal:write"):
        main(["--as-user", "obs", "proposal", "approve", proposal["proposal_id"]])

    assert main(["--as-user", "obs", "proposal", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["status"] == "proposed"


def test_cli_proposal_rejects_double_decision(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["init", "mvp", "--mission", "Prototype mission"]) == 0
    capsys.readouterr()

    store = JsonStateStore(tmp_path / ".brigade" / "state.json")
    proposal = build_proposal(kind="rest_insight", title="archive stale notes weekly")
    store.add_proposal(proposal)

    assert main(["--as-user", "owner", "proposal", "approve", proposal["proposal_id"]]) == 0
    capsys.readouterr()

    with pytest.raises(ValueError, match="already approved"):
        main(["--as-user", "owner", "proposal", "reject", proposal["proposal_id"]])
