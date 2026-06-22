from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from brigade.auth import issue_token  # noqa: E402
from brigade.config import Settings  # noqa: E402
from brigade.schemas import Agent, Role, Team, User  # noqa: E402
from brigade.state import JsonStateStore  # noqa: E402
from brigade.web import create_app  # noqa: E402


def _owner_app(tmp_path):
    """App with a single OWNER and auth disabled -> implicit single-user owner."""
    store = JsonStateStore(tmp_path / "state.json")
    store.add_user(User(username="owner", role=Role.OWNER))
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        require_auth=False,
        allow_json_store=True,
    )
    return create_app(settings, store), store, settings


def test_create_agent_onboards_team_chief_and_workspace(tmp_path):
    app, store, _ = _owner_app(tmp_path)
    client = TestClient(app)

    response = client.post(
        "/api/agents",
        json={
            "agent_id": "chief",
            "display_name": "CHIEF",
            "role": "crew_chief",
            "team_id": "alpha",
            "create_team": True,
            "crew_chief": True,
            "model_provider": "ollama",
            "model_name": "gpt-oss:20b",
            "specialties": ["planning"],
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["agent"]["agent_id"] == "chief"
    assert body["agent"]["team_id"] == "alpha"
    assert body["team"]["crew_chief_id"] == "chief"
    assert "chief" in body["team"]["members"]
    assert body["valid"] is True

    # Agent + team are persisted.
    assert {agent.agent_id for agent in store.agents()} == {"chief"}
    team = next(team for team in store.teams() if team.team_id == "alpha")
    assert team.crew_chief_id == "chief"

    # The on-disk workspace was seeded under the data dir.
    workspace = tmp_path / "workspace-chief"
    assert workspace.is_dir()
    assert (workspace / "AGENTS.md").exists()


def test_create_agent_chief_requires_team(tmp_path):
    app, _, _ = _owner_app(tmp_path)
    client = TestClient(app)
    response = client.post("/api/agents", json={"agent_id": "loner", "crew_chief": True})
    assert response.status_code == 400


def test_create_agent_unknown_team_without_create_flag(tmp_path):
    app, _, _ = _owner_app(tmp_path)
    client = TestClient(app)
    response = client.post("/api/agents", json={"agent_id": "scout", "team_id": "ghost"})
    assert response.status_code == 400


def test_create_agent_requires_agent_write_permission(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    owner = User(username="owner", role=Role.OWNER)
    operator = User(username="op", role=Role.OPERATOR)
    store.add_user(owner)
    store.add_user(operator)
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        require_auth=True,
        jwt_secret="x" * 40,
        allow_json_store=True,
    )
    client = TestClient(create_app(settings, store))

    operator_headers = {"Authorization": f"Bearer {issue_token(settings, operator)}"}
    denied = client.post("/api/agents", json={"agent_id": "x"}, headers=operator_headers)
    assert denied.status_code == 403

    owner_headers = {"Authorization": f"Bearer {issue_token(settings, owner)}"}
    allowed = client.post("/api/agents", json={"agent_id": "x"}, headers=owner_headers)
    assert allowed.status_code == 200, allowed.text


def test_patch_team_membership_syncs_agent_team_id(tmp_path):
    app, store, _ = _owner_app(tmp_path)
    store.add_agent(Agent("scout", "SCOUT", "workspace-scout", "researcher"))
    store.upsert_team(Team(team_id="alpha", display_name="Alpha"))
    client = TestClient(app)

    add = client.patch("/api/teams/alpha", json={"members": ["scout"]})
    assert add.status_code == 200, add.text
    assert next(a for a in store.agents() if a.agent_id == "scout").team_id == "alpha"

    remove = client.patch("/api/teams/alpha", json={"members": []})
    assert remove.status_code == 200, remove.text
    assert next(a for a in store.agents() if a.agent_id == "scout").team_id is None


def test_delegate_endpoint_queues_work_for_member(tmp_path):
    app, store, _ = _owner_app(tmp_path)
    store.add_agent(Agent("chief", "CHIEF", "workspace-chief", "crew_chief", team_id="alpha"))
    store.add_agent(Agent("scout", "SCOUT", "workspace-scout", "researcher", team_id="alpha"))
    store.upsert_team(
        Team(
            team_id="alpha",
            display_name="Alpha",
            crew_chief_id="chief",
            members=["chief", "scout"],
        )
    )
    client = TestClient(app)

    ok = client.post(
        "/api/teams/alpha/delegate",
        json={
            "chief_agent_id": "chief",
            "target_agent_id": "scout",
            "assignment": "Research the demo scenario",
        },
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["status"] == "queued"
    assert body["assignment"]["assigned_to"] == "scout"
    assert body["assignment"]["source"] == "crew_chief_delegate"

    # A non-chief actor cannot delegate for the team.
    denied = client.post(
        "/api/teams/alpha/delegate",
        json={
            "chief_agent_id": "scout",
            "target_agent_id": "chief",
            "assignment": "nope",
        },
    )
    assert denied.status_code == 403


def test_delete_agent_scrubs_team_membership(tmp_path):
    app, store, _ = _owner_app(tmp_path)
    store.add_agent(Agent("chief", "CHIEF", "workspace-chief", "crew_chief", team_id="alpha"))
    store.add_agent(Agent("scout", "SCOUT", "workspace-scout", "researcher", team_id="alpha"))
    store.upsert_team(
        Team(
            team_id="alpha",
            display_name="Alpha",
            crew_chief_id="chief",
            members=["chief", "scout"],
        )
    )
    client = TestClient(app)

    response = client.delete("/api/agents/chief")
    assert response.status_code == 200, response.text
    assert {a.agent_id for a in store.agents()} == {"scout"}
    team = next(team for team in store.teams() if team.team_id == "alpha")
    assert team.crew_chief_id is None
    assert team.members == ["scout"]

    missing = client.delete("/api/agents/ghost")
    assert missing.status_code == 404
