"""Per-agent model update via PATCH /api/agents/{agent_id}."""

from __future__ import annotations

import pytest

from brigade.config import Settings
from brigade.schemas import Agent
from brigade.state import JsonStateStore


def _app(tmp_path):
    pytest.importorskip("fastapi")
    from brigade.web import create_app

    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(Agent("ada", "ADA", "workspace-ada", model_name="gpt-oss:20b"))
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
    )
    return create_app(settings, store), store


def test_patch_agent_route_registered(tmp_path):
    app, _ = _app(tmp_path)
    patch_paths = {
        route.path
        for route in app.routes
        if "PATCH" in getattr(route, "methods", set())
    }
    assert "/api/agents/{agent_id}" in patch_paths


def test_patch_agent_persists_model(tmp_path):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    app, store = _app(tmp_path)
    client = TestClient(app)
    resp = client.patch("/api/agents/ada", json={"model_name": "qwen2.5-coder:7b"})
    assert resp.status_code == 200
    assert resp.json()["model_name"] == "qwen2.5-coder:7b"
    stored = next(a for a in store.agents() if a.agent_id == "ada")
    assert stored.model_name == "qwen2.5-coder:7b"
    assert stored.model_provider == "ollama"  # preserved


def test_patch_unknown_agent_is_404(tmp_path):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    app, _ = _app(tmp_path)
    resp = TestClient(app).patch("/api/agents/nope", json={"model_name": "x"})
    assert resp.status_code == 404


def test_patch_with_no_fields_is_400(tmp_path):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    app, _ = _app(tmp_path)
    resp = TestClient(app).patch("/api/agents/ada", json={})
    assert resp.status_code == 400


def test_patch_agent_persists_role_and_specialties(tmp_path):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    app, store = _app(tmp_path)
    client = TestClient(app)
    resp = client.patch(
        "/api/agents/ada",
        json={"role": "crew_chief", "specialties": ["web design", " css ", ""]},
    )
    assert resp.status_code == 200
    stored = next(a for a in store.agents() if a.agent_id == "ada")
    assert stored.role == "crew_chief"
    assert stored.specialties == ["web design", "css"]
    assert stored.model_name == "gpt-oss:20b"  # preserved

    cleared = client.patch("/api/agents/ada", json={"specialties": []})
    assert cleared.status_code == 200
    assert next(a for a in store.agents() if a.agent_id == "ada").specialties == []


def test_patch_agent_rejects_unknown_role(tmp_path):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    app, _ = _app(tmp_path)
    resp = TestClient(app).patch("/api/agents/ada", json={"role": "supervisor"})
    assert resp.status_code == 400
