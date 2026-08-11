from __future__ import annotations

import asyncio
import io
import json

import pytest

import brigade.research_control as control
from brigade.auth import issue_token
from brigade.config import Settings
from brigade.schemas import Role, User
from brigade.state import JsonStateStore
from tests.test_v0_9 import _asgi_request


def test_research_status_is_redacted_and_handles_unconfigured_mcp(tmp_path):
    (tmp_path / "research_search_health.json").write_text(
        json.dumps({"searxng": {"state": "degraded", "reason": "timeout"}}),
        encoding="utf-8",
    )

    status = control.research_status(tmp_path)

    assert status["mcp"]["state"] == "not_configured"
    assert status["search"]["state"] == "degraded"
    assert "credential" not in json.dumps(status).lower()


def test_research_component_tests_persist_only_redacted_health(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "searxng_search", lambda query, limit: ([], "http://search"))
    search = control.test_research_component(tmp_path, "search")

    assert search["ok"] is True
    search_health = json.loads((tmp_path / "research_search_health.json").read_text())
    assert search_health["searxng"]["state"] == "healthy"

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        control.urllib.request,
        "urlopen",
        lambda request, timeout: Response(b'{"ok": true}'),
    )
    browser = control.test_research_component(tmp_path, "browser")

    assert browser["ok"] is True
    browser_health = json.loads((tmp_path / "browser_health.json").read_text())
    assert browser_health["browser"]["state"] == "healthy"


def test_research_api_allows_redacted_status_but_not_observer_mutation(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import brigade.web as web

    store = JsonStateStore(tmp_path / "state.json")
    owner = User("owner", Role.OWNER)
    observer = User("observer", Role.OBSERVER)
    store.add_user(owner)
    store.add_user(observer)
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        require_auth=True,
        jwt_secret="x" * 40,
        allow_json_store=True,
    )
    monkeypatch.setattr(web, "research_status", lambda data_dir: {"mcp": {"state": "unknown"}})
    monkeypatch.setattr(
        web,
        "test_research_component",
        lambda data_dir, component: {"ok": True, "component": component},
    )
    app = web.create_app(settings, store)
    observer_headers = {"Authorization": f"Bearer {issue_token(settings, observer)}"}
    owner_headers = {"Authorization": f"Bearer {issue_token(settings, owner)}"}

    status = asyncio.run(
        _asgi_request(app, "GET", "/api/research/status", headers=observer_headers)
    )
    denied = asyncio.run(
        _asgi_request(app, "POST", "/api/research/test/search", headers=observer_headers)
    )
    tested = asyncio.run(
        _asgi_request(app, "POST", "/api/research/test/search", headers=owner_headers)
    )

    assert status.status_code == 200
    assert denied.status_code == 403
    assert tested.status_code == 200
    assert tested.json()["ok"] is True


def test_staged_policy_validates_applies_and_rolls_back(tmp_path):
    applied: list[dict[str, bool]] = []

    with pytest.raises(ValueError, match="not editable"):
        control.stage_research_policy(tmp_path, {"credential": True}, actor="owner")
    with pytest.raises(ValueError, match="booleans"):
        control.stage_research_policy(tmp_path, {"research_mcp_enabled": "yes"}, actor="owner")

    disabled = control.stage_research_policy(
        tmp_path, {"research_browser_enabled": False}, actor="owner"
    )
    active = control.apply_research_policy(
        tmp_path, disabled["proposal_id"], apply_values=applied.append
    )
    assert active["active"]["research_browser_enabled"] is False
    assert applied[-1]["research_browser_enabled"] is False

    enabled = control.stage_research_policy(
        tmp_path, {"research_browser_enabled": True}, actor="owner"
    )
    control.apply_research_policy(tmp_path, enabled["proposal_id"], apply_values=applied.append)
    rollback = control.rollback_research_policy(
        tmp_path, apply_values=applied.append, actor="owner"
    )
    assert rollback["active"]["research_browser_enabled"] is False


def test_research_policy_api_stages_before_apply(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import brigade.web as web

    store = JsonStateStore(tmp_path / "state.json")
    owner = User("owner", Role.OWNER)
    store.add_user(owner)
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        require_auth=True,
        jwt_secret="x" * 40,
        allow_json_store=True,
    )
    app = web.create_app(settings, store)
    headers = {"Authorization": f"Bearer {issue_token(settings, owner)}"}

    invalid = asyncio.run(
        _asgi_request(
                app,
                "POST",
                "/api/research/policy/proposals",
                headers=headers,
                json_payload={"values": {"credential": True}},
        )
    )
    staged = asyncio.run(
        _asgi_request(
                app,
                "POST",
                "/api/research/policy/proposals",
                headers=headers,
                json_payload={"values": {"research_search_enabled": False}},
        )
    )
    proposal_id = staged.json()["proposal"]["proposal_id"]
    applied = asyncio.run(
        _asgi_request(
            app,
            "POST",
            f"/api/research/policy/proposals/{proposal_id}/apply",
            headers=headers,
        )
    )

    assert invalid.status_code == 400
    assert staged.status_code == 200
    assert applied.status_code == 200
    assert applied.json()["active"]["research_search_enabled"] is False
    assert store.runtime_overrides()["research_search_enabled"] is False


def test_research_audit_is_filtered_and_requires_operator_role(tmp_path, monkeypatch):
    import brigade.web as web

    store = JsonStateStore(tmp_path / "state.json")
    owner = User("owner", Role.OWNER)
    observer = User("observer", Role.OBSERVER)
    store.add_user(owner)
    store.add_user(observer)
    store.add_provenance_record(
        {
            "record_id": "search-1",
            "node_type": "web_search",
            "created_at": "2026-08-11T00:00:00+00:00",
            "principal": "sage",
            "tool": "web_search",
            "policy_outcome": "allowed",
            "source_url": "https://example.test/search",
            "final_url": "https://example.test/search",
            "document_id": "doc-1",
        }
    )
    store.add_provenance_record({"record_id": "ignore", "node_type": "assignment"})
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        require_auth=True,
        jwt_secret="x" * 40,
        allow_json_store=True,
    )
    app = web.create_app(settings, store)
    owner_headers = {"Authorization": f"Bearer {issue_token(settings, owner)}"}
    observer_headers = {"Authorization": f"Bearer {issue_token(settings, observer)}"}

    visible = asyncio.run(_asgi_request(app, "GET", "/api/research/audit", headers=owner_headers))
    hidden = asyncio.run(_asgi_request(app, "GET", "/api/research/audit", headers=observer_headers))

    assert visible.status_code == 200
    assert visible.json()["events"][0]["document_id"] == "doc-1"
    assert hidden.status_code == 403


def test_research_alert_is_deduplicated_and_resolved(tmp_path):
    first = control.reconcile_research_alert(
        tmp_path,
        rule="search_health",
        failed=True,
        message="search backend unavailable",
        correlation_id="first",
    )
    repeated = control.reconcile_research_alert(
        tmp_path,
        rule="search_health",
        failed=True,
        message="search backend unavailable",
        correlation_id="second",
    )
    recovered = control.reconcile_research_alert(
        tmp_path,
        rule="search_health",
        failed=False,
        message="search backend unavailable",
        correlation_id="third",
    )

    assert first["new"] is True
    assert repeated["new"] is False
    assert repeated["count"] == 2
    assert recovered["status"] == "resolved"
    assert control.research_alerts(tmp_path)["active"] == []


def test_citation_validation_alert_keeps_no_answer_text(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")

    alert = control.record_citation_validation(
        store,
        errors=["citation source is unknown"],
        principal="sage",
        correlation_id="citation-1",
    )

    assert alert["rule"] == "citation_validation"
    assert "citation source" in store.alerts()[0]
    audit = control.research_audit(store)["events"]
    assert audit[0]["tool"] == "citation_validator"
