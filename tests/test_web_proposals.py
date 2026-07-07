from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")

from brigade.auth import issue_token  # noqa: E402
from brigade.config import Settings  # noqa: E402
from brigade.schemas import Role, User, build_proposal  # noqa: E402
from brigade.state import JsonStateStore  # noqa: E402
from brigade.web import create_app  # noqa: E402
from tests.test_v0_9 import _asgi_request  # noqa: E402


def _auth_app(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    owner = User(username="owner", role=Role.OWNER)
    operator = User(username="op", role=Role.OPERATOR)
    observer = User(username="obs", role=Role.OBSERVER)
    store.add_user(owner)
    store.add_user(operator)
    store.add_user(observer)
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        require_auth=True,
        jwt_secret="x" * 40,
        allow_json_store=True,
    )
    app = create_app(settings, store)
    headers = {
        "owner": {"Authorization": f"Bearer {issue_token(settings, owner)}"},
        "operator": {"Authorization": f"Bearer {issue_token(settings, operator)}"},
        "observer": {"Authorization": f"Bearer {issue_token(settings, observer)}"},
    }
    return app, store, headers


def test_proposal_routes_list_filter_and_decide(tmp_path):
    app, store, headers = _auth_app(tmp_path)
    first = build_proposal(kind="rest_insight", title="Archive stale notes", agent_id="sage")
    second = build_proposal(kind="efficiency", title="Weekly report recurs", agent_id="abacus")
    store.add_proposal(first)
    store.add_proposal(second)

    listed = asyncio.run(
        _asgi_request(
            app,
            "GET",
            "/api/proposals?status=proposed&kind=rest_insight",
            headers=headers["observer"],
        )
    )
    assert listed.status_code == 200, listed.text
    assert [item["proposal_id"] for item in listed.json()] == [first["proposal_id"]]

    limited = asyncio.run(
        _asgi_request(
            app,
            "GET",
            "/api/proposals?limit=1",
            headers=headers["observer"],
        )
    )
    assert limited.status_code == 200, limited.text
    assert [item["proposal_id"] for item in limited.json()] == [second["proposal_id"]]

    denied = asyncio.run(
        _asgi_request(
            app,
            "POST",
            f"/api/proposals/{first['proposal_id']}/decision",
            json_payload={"decision": "approved"},
            headers=headers["observer"],
        )
    )
    assert denied.status_code == 403

    decided = asyncio.run(
        _asgi_request(
            app,
            "POST",
            f"/api/proposals/{first['proposal_id']}/decision",
            json_payload={"decision": "rejected", "reason": "not needed"},
            headers=headers["operator"],
        )
    )
    assert decided.status_code == 200, decided.text
    body = decided.json()
    assert body["status"] == "rejected"
    assert body["decided_by"] == "op"
    assert body["details"]["decision_reason"] == "not needed"

    crafted = asyncio.run(
        _asgi_request(
            app,
            "POST",
            f"/api/proposals/{second['proposal_id']}/decision",
            json_payload={"decision": "it is already fine"},
            headers=headers["operator"],
        )
    )
    assert crafted.status_code == 400, crafted.text
    assert store.find_proposal(second["proposal_id"])["status"] == "proposed"

    again = asyncio.run(
        _asgi_request(
            app,
            "POST",
            f"/api/proposals/{first['proposal_id']}/decision",
            json_payload={"decision": "approved"},
            headers=headers["operator"],
        )
    )
    assert again.status_code == 409
    missing = asyncio.run(
        _asgi_request(
            app,
            "POST",
            "/api/proposals/missing/decision",
            json_payload={"decision": "approved"},
            headers=headers["operator"],
        )
    )
    assert missing.status_code == 404


def test_connector_approval_routes_are_admin_only_and_accept_slash_ids(tmp_path):
    app, store, headers = _auth_app(tmp_path)
    store.upsert_external_identity(
        {
            "provider": "google_chat",
            "external_user_id": "users/alice",
            "username": None,
            "status": "pending",
            "reason": "first inbound message",
            "redacted_metadata": {"space": "spaces/redacted"},
            "created_at": "2026-07-06T00:00:00Z",
            "updated_at": "2026-07-06T00:00:00Z",
            "decided_at": None,
            "decided_by": None,
        }
    )

    denied = asyncio.run(
        _asgi_request(
            app,
            "GET",
            "/api/connectors/approvals?status=pending",
            headers=headers["operator"],
        )
    )
    assert denied.status_code == 403

    listed = asyncio.run(
        _asgi_request(
            app,
            "GET",
            "/api/connectors/approvals?provider=google_chat&status=pending",
            headers=headers["owner"],
        )
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["external_user_id"] == "users/alice"

    missing_username = asyncio.run(
        _asgi_request(
            app,
            "POST",
            "/api/connectors/approvals/decision",
            json_payload={
                "provider": "google_chat",
                "external_user_id": "users/alice",
                "decision": "approved",
            },
            headers=headers["owner"],
        )
    )
    assert missing_username.status_code == 400

    approved = asyncio.run(
        _asgi_request(
            app,
            "POST",
            "/api/connectors/approvals/decision",
            json_payload={
                "provider": "google_chat",
                "external_user_id": "users/alice",
                "decision": "approved",
                "username": "alice",
                "reason": "known operator",
            },
            headers=headers["owner"],
        )
    )
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["status"] == "approved"
    assert body["username"] == "alice"
    assert body["decided_by"] == "owner"
    assert store.external_identity("google_chat", "users/alice")["status"] == "approved"
    assert next(user for user in store.users() if user.username == "alice")

    already_decided = asyncio.run(
        _asgi_request(
            app,
            "POST",
            "/api/connectors/approvals/decision",
            json_payload={
                "provider": "google_chat",
                "external_user_id": "users/alice",
                "decision": "rejected",
                "reason": "stale tab",
            },
            headers=headers["owner"],
        )
    )
    assert already_decided.status_code == 409, already_decided.text
    assert store.external_identity("google_chat", "users/alice")["status"] == "approved"

    unknown = asyncio.run(
        _asgi_request(
            app,
            "POST",
            "/api/connectors/approvals/decision",
            json_payload={
                "provider": "telegram",
                "external_user_id": "42",
                "decision": "rejected",
                "reason": "unknown",
            },
            headers=headers["owner"],
        )
    )
    assert unknown.status_code == 404, unknown.text
    assert store.external_identity("telegram", "42") is None
