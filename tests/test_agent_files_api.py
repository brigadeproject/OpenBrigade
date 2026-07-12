"""Workspace personality-file editing via /api/agents/{agent_id}/files/{filename}."""

from __future__ import annotations

import pytest

from brigade.config import Settings
from brigade.schemas import Agent
from brigade.state import JsonStateStore
from brigade.workspace import ensure_agent_workspace


def _app(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from brigade.web import create_app

    store = JsonStateStore(tmp_path / "state.json")
    agent = Agent("ada", "ADA", "workspace-ada")
    store.add_agent(agent)
    ensure_agent_workspace(agent, tmp_path)
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
    )
    return create_app(settings, store), store


def test_get_returns_scaffolded_identity(tmp_path):
    from fastapi.testclient import TestClient

    app, _ = _app(tmp_path)
    resp = TestClient(app).get("/api/agents/ada/files/IDENTITY.md")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["agent_id"] == "ada"
    assert payload["filename"] == "IDENTITY.md"
    assert payload["content"]  # scaffold is non-empty


def test_put_then_get_round_trip(tmp_path):
    from fastapi.testclient import TestClient

    app, _ = _app(tmp_path)
    client = TestClient(app)
    body = "# ADA\nMeticulous systems archivist.\n"
    resp = client.put("/api/agents/ada/files/IDENTITY.md", json={"content": body})
    assert resp.status_code == 200
    assert resp.json()["status"] == "saved"
    assert (tmp_path / "workspace-ada" / "IDENTITY.md").read_text(encoding="utf-8") == body
    assert client.get("/api/agents/ada/files/IDENTITY.md").json()["content"] == body


def test_unknown_agent_is_404(tmp_path):
    from fastapi.testclient import TestClient

    app, _ = _app(tmp_path)
    assert TestClient(app).get("/api/agents/nope/files/IDENTITY.md").status_code == 404


def test_non_whitelisted_filename_is_400(tmp_path):
    from fastapi.testclient import TestClient

    app, _ = _app(tmp_path)
    client = TestClient(app)
    assert client.get("/api/agents/ada/files/HEARTBEAT.md").status_code == 400
    assert (
        client.put(
            "/api/agents/ada/files/evil.sh", json={"content": "x"}
        ).status_code
        == 400
    )


def test_missing_file_is_404(tmp_path):
    from fastapi.testclient import TestClient

    app, _ = _app(tmp_path)
    (tmp_path / "workspace-ada" / "SOUL.md").unlink()
    assert TestClient(app).get("/api/agents/ada/files/SOUL.md").status_code == 404


def test_oversize_content_is_400(tmp_path):
    from fastapi.testclient import TestClient

    app, _ = _app(tmp_path)
    resp = TestClient(app).put(
        "/api/agents/ada/files/IDENTITY.md", json={"content": "x" * (64 * 1024 + 1)}
    )
    assert resp.status_code == 400


def test_non_string_content_is_400(tmp_path):
    from fastapi.testclient import TestClient

    app, _ = _app(tmp_path)
    resp = TestClient(app).put(
        "/api/agents/ada/files/IDENTITY.md", json={"content": ["not", "text"]}
    )
    assert resp.status_code == 400
