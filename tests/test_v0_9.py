from __future__ import annotations

import argparse
import asyncio
import json

import pytest

from brigade.auth import issue_token
from brigade.cli import _provider_from_args, main
from brigade.config import Settings
from brigade.connectors import (
    IncomingConnectorMessage,
    InMemoryConnectorRateLimiter,
    approve_external_identity,
    google_chat_reply_sender,
    handle_google_chat_event,
    handle_telegram_update,
    process_live_connector_message,
    send_telegram_message,
)
from brigade.markdown import render_markdown_html
from brigade.schemas import Agent, Role, User
from brigade.services import build_settings_payload, set_config_value
from brigade.state import JsonStateStore
from tests.helpers import TestProvider


class _AsgiResponse:
    def __init__(self, status_code: int, headers: dict[str, str], body: bytes) -> None:
        self.status_code = status_code
        self.headers = headers
        self.text = body.decode("utf-8")
        self._body = body

    def json(self):
        return json.loads(self._body.decode("utf-8"))


async def _asgi_request(
    app,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    json_payload: dict[str, object] | None = None,
) -> _AsgiResponse:
    request_path, _, query = path.partition("?")
    body = b"" if json_payload is None else json.dumps(json_payload).encode("utf-8")
    raw_headers = [
        (key.lower().encode("ascii"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    if json_payload is not None:
        raw_headers.append((b"content-type", b"application/json"))
        raw_headers.append((b"content-length", str(len(body)).encode("ascii")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": request_path,
        "raw_path": request_path.encode("ascii"),
        "query_string": query.encode("ascii"),
        "headers": raw_headers,
        "client": ("test", 1),
        "server": ("test", 80),
    }
    sent_request = False
    messages: list[dict[str, object]] = []

    async def receive():
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    response_headers = {
        key.decode("latin-1"): value.decode("latin-1")
        for key, value in start.get("headers", [])
    }
    return _AsgiResponse(int(start["status"]), response_headers, response_body)


def test_config_set_rejects_stale_base_hash(tmp_path):
    config_path = tmp_path / "brigade.config.json"
    set_config_value(config_path, "log_level", "INFO")
    settings = Settings(config_path=config_path, data_dir=tmp_path)
    base_hash = build_settings_payload(settings)["config_hash"]

    set_config_value(config_path, "log_level", "DEBUG", base_hash=base_hash)

    with pytest.raises(ValueError, match="config changed"):
        set_config_value(config_path, "log_level", "INFO", base_hash=base_hash)


def test_cli_config_set_rejects_stale_base_hash_without_traceback(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "brigade.config.json"
    set_config_value(config_path, "log_level", "INFO")
    settings = Settings(config_path=config_path, data_dir=tmp_path)
    base_hash = build_settings_payload(settings)["config_hash"]
    set_config_value(config_path, "log_level", "DEBUG", base_hash=base_hash)

    assert (
        main(["config", "set", "--key", "log_level", "--value", "INFO", "--base-hash", base_hash])
        == 1
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is False
    assert "config changed" in payload["reason"]


def test_telegram_connector_allowlist_and_audit_metadata(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(Agent("sage", "SAGE", "workspace-sage"))

    rejected = handle_telegram_update(
        store,
        {"message": {"chat": {"id": 9}, "from": {"id": 123}, "text": "hello"}},
        default_agent="sage",
        allowlist={"999"},
    )
    assert rejected.status == "rejected"

    accepted = handle_telegram_update(
        store,
        {"update_id": 1, "message": {"chat": {"id": 9}, "from": {"id": 123}, "text": "hello"}},
        default_agent="sage",
        allowlist={"123"},
    )
    assert accepted.status == "accepted"
    message = store.messages()[0]
    assert message.channel == "telegram:9"
    assert message.metadata["provider"] == "telegram"
    assert message.metadata["kind"] == "external_inbound"


def test_google_chat_connector_allowlist_and_audit_metadata(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(Agent("sage", "SAGE", "workspace-sage"))

    accepted = handle_google_chat_event(
        store,
        {
            "user": {"name": "users/alice"},
            "message": {"space": {"name": "spaces/abc"}, "text": "status?"},
        },
        default_agent="sage",
        allowlist={"users/alice"},
    )
    assert accepted.status == "accepted"
    message = store.messages()[0]
    assert message.channel == "google-chat:spaces/abc"
    assert message.metadata["provider"] == "google_chat"


def test_cli_connector_telegram_smoke(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert main(["user", "add", "--username", "alice", "--role", "owner"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "agent",
                "add",
                "--id",
                "sage",
                "--name",
                "SAGE",
                "--workspace",
                "workspace-sage",
            ]
        )
        == 0
    )
    capsys.readouterr()
    payload = json.dumps(
        {"message": {"chat": {"id": 7}, "from": {"id": 42}, "text": "from telegram"}}
    )
    assert (
        main(
            [
                "connector",
                "telegram",
                "--agent",
                "sage",
                "--allow-user",
                "42",
                "--payload-json",
                payload,
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "accepted"


def test_provider_aliases_use_litellm_routes(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    default_base = "http://127.0.0.1:11434"

    openai = _provider_from_args(
        argparse.Namespace(
            provider="openai",
            model="gpt-4.1-mini",
            api_key=None,
            base_url=default_base,
        )
    )
    gemini = _provider_from_args(
        argparse.Namespace(
            provider="gemini",
            model="gemini-1.5-flash",
            api_key=None,
            base_url=default_base,
        )
    )

    assert openai.model == "gpt-4.1-mini"
    assert openai.api_key == "openai-key"
    assert openai.api_base is None
    assert gemini.model == "gemini/gemini-1.5-flash"
    assert gemini.api_key == "gemini-key"


def test_web_auth_routes_security_middleware_and_tokens(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")

    from brigade.web import create_app

    store = JsonStateStore(tmp_path / "state.json")
    owner = User(username="owner", role=Role.OWNER)
    observer = User(username="reader", role=Role.OBSERVER)
    store.add_user(owner)
    store.add_user(observer)
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        require_auth=True,
        jwt_secret="x" * 40,
    )
    app = create_app(settings, store)
    paths = {route.path for route in app.routes}
    middleware = [item.cls.__name__ for item in app.user_middleware]

    assert "/api/auth/me" in paths
    assert "/api/teams" in paths
    assert "SecurityHeadersMiddleware" in middleware
    assert issue_token(settings, observer)
    assert issue_token(settings, owner)


def test_live_connector_webhook_routes_disabled_by_default(tmp_path):
    pytest.importorskip("fastapi")
    from brigade.web import create_app

    store = JsonStateStore(tmp_path / "state.json")
    settings = Settings(config_path=tmp_path / "brigade.config.json", data_dir=tmp_path)
    app = create_app(settings, store)

    telegram = asyncio.run(
        _asgi_request(
            app,
            "POST",
            "/api/connectors/telegram/webhook",
            json_payload={},
        )
    )
    google = asyncio.run(
        _asgi_request(
            app,
            "POST",
            "/api/connectors/google-chat/webhook",
            json_payload={},
        )
    )

    assert telegram.status_code == 200
    assert telegram.json()["status"] == "disabled"
    assert google.status_code == 200
    assert google.json()["status"] == "disabled"


def test_telegram_webhook_bad_secret_rejected_before_processing(tmp_path):
    pytest.importorskip("fastapi")
    from brigade.web import create_app

    store = JsonStateStore(tmp_path / "state.json")
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        postgres_dsn="postgresql://unused",
        redis_url="redis://unused",
        telegram_webhook_enabled=True,
        telegram_webhook_secret="expected",
        telegram_bot_token="bot-token",
    )
    app = create_app(settings, store)

    response = asyncio.run(
        _asgi_request(
            app,
            "POST",
            "/api/connectors/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
            json_payload={
                "message": {"chat": {"id": 7}, "from": {"id": 42}, "text": "hello"}
            },
        )
    )

    assert response.status_code == 401
    assert store.messages() == []


def test_live_connector_unknown_user_creates_pending_approval(tmp_path):
    pytest.importorskip("fastapi")
    from brigade.web import create_app

    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(Agent("sage", "SAGE", "workspace-sage"))
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        postgres_dsn="postgresql://unused",
        redis_url="redis://unused",
        telegram_webhook_enabled=True,
        telegram_webhook_secret="secret",
        telegram_bot_token="bot-token",
        telegram_default_agent="sage",
    )
    app = create_app(
        settings,
        store,
        connector_rate_limiter=InMemoryConnectorRateLimiter(limit=10, window_seconds=60),
    )

    response = asyncio.run(
        _asgi_request(
            app,
            "POST",
            "/api/connectors/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
            json_payload={
                "message": {"chat": {"id": 7}, "from": {"id": 42}, "text": "hello"}
            },
        )
    )

    assert response.status_code == 200
    assert response.json()["status"] == "pending_approval"
    identity = store.external_identity("telegram", "42")
    assert identity is not None
    assert identity["status"] == "pending"
    assert store.messages() == []
    assert any("pending approval" in alert for alert in store.alerts())


def test_approved_telegram_user_auto_replies_and_audits_outbound(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import brigade.web

    monkeypatch.setattr(
        brigade.web,
        "provider_from_settings",
        lambda *args, **kwargs: TestProvider(),
    )

    sent: list[dict[str, object]] = []

    def stub_post(url: str, payload: bytes, headers: dict[str, str]):
        sent.append({"url": url, "payload": json.loads(payload), "headers": headers})
        return {"ok": True, "result": {"message_id": 99}}

    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(Agent("sage", "SAGE", "workspace-sage"))
    approve_external_identity(
        store,
        provider="telegram",
        external_user_id="42",
        username="alice",
        decided_by="owner",
    )
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        postgres_dsn="postgresql://unused",
        redis_url="redis://unused",
        telegram_webhook_enabled=True,
        telegram_webhook_secret="secret",
        telegram_bot_token="bot-token",
        telegram_default_agent="sage",
    )
    app = brigade.web.create_app(
        settings,
        store,
        connector_rate_limiter=InMemoryConnectorRateLimiter(limit=10, window_seconds=60),
        telegram_http_post=stub_post,
    )

    response = asyncio.run(
        _asgi_request(
            app,
            "POST",
            "/api/connectors/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
            json_payload={
                "message": {"chat": {"id": 7}, "from": {"id": 42}, "text": "hello"}
            },
        )
    )

    assert response.status_code == 200
    assert response.json()["status"] == "complete"
    assert len(sent) == 1
    assert sent[0]["payload"]["chat_id"] == "7"  # type: ignore[index]
    messages = store.messages("telegram:7")
    assert [message.metadata["kind"] for message in messages] == [
        "external_inbound",
        "external_outbound",
    ]
    audits = store.connector_audit_events("telegram")
    assert any(
        record["direction"] == "outbound" and record["status"] == "sent"
        for record in audits
    )


def test_approved_google_chat_user_returns_thread_reply(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import brigade.web

    monkeypatch.setattr(
        brigade.web,
        "provider_from_settings",
        lambda *args, **kwargs: TestProvider(),
    )

    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(Agent("sage", "SAGE", "workspace-sage"))
    approve_external_identity(
        store,
        provider="google_chat",
        external_user_id="users/alice",
        username="alice",
        decided_by="owner",
    )
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        postgres_dsn="postgresql://unused",
        redis_url="redis://unused",
        google_chat_webhook_enabled=True,
        google_chat_secret="secret",
        google_chat_default_agent="sage",
    )
    app = brigade.web.create_app(
        settings,
        store,
        connector_rate_limiter=InMemoryConnectorRateLimiter(limit=10, window_seconds=60),
    )

    response = asyncio.run(
        _asgi_request(
            app,
            "POST",
            "/api/connectors/google-chat/webhook?token=secret",
            json_payload={
                "user": {"name": "users/alice"},
                "message": {
                    "space": {"name": "spaces/abc"},
                    "thread": {"name": "spaces/abc/threads/def"},
                    "text": "status?",
                },
            },
        )
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["text"].startswith("test provider:")
    assert payload["thread"]["name"] == "spaces/abc/threads/def"
    assert any(
        record["provider"] == "google_chat" and record["status"] == "sent"
        for record in store.connector_audit_events()
    )


def test_live_connector_rate_limit_and_size_limit_are_audited(tmp_path):
    pytest.importorskip("fastapi")
    from brigade.web import create_app

    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(Agent("sage", "SAGE", "workspace-sage"))
    approve_external_identity(
        store,
        provider="telegram",
        external_user_id="42",
        username="alice",
        decided_by="owner",
    )
    base_settings = {
        "config_path": tmp_path / "brigade.config.json",
        "data_dir": tmp_path,
        "postgres_dsn": "postgresql://unused",
        "redis_url": "redis://unused",
        "telegram_webhook_enabled": True,
        "telegram_webhook_secret": "secret",
        "telegram_bot_token": "bot-token",
        "telegram_default_agent": "sage",
    }
    rate_limited_app = create_app(
        Settings(**base_settings),
        store,
        connector_rate_limiter=InMemoryConnectorRateLimiter(limit=0, window_seconds=60),
        telegram_http_post=lambda url, payload, headers: {"ok": True},
    )
    response = asyncio.run(
        _asgi_request(
            rate_limited_app,
            "POST",
            "/api/connectors/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
            json_payload={
                "message": {"chat": {"id": 7}, "from": {"id": 42}, "text": "hello"}
            },
        )
    )
    assert response.status_code == 429

    oversized_app = create_app(
        Settings(**base_settings, connector_max_inbound_chars=3),
        store,
        connector_rate_limiter=InMemoryConnectorRateLimiter(limit=10, window_seconds=60),
        telegram_http_post=lambda url, payload, headers: {"ok": True},
    )
    response = asyncio.run(
        _asgi_request(
            oversized_app,
            "POST",
            "/api/connectors/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
            json_payload={
                "message": {"chat": {"id": 7}, "from": {"id": 42}, "text": "hello"}
            },
        )
    )
    assert response.status_code == 403
    assert any(record["status"] == "rate_limited" for record in store.connector_audit_events())
    assert any(record["reason"] == "message too large" for record in store.connector_audit_events())


def test_live_connector_blocks_oversized_outbound_reply(tmp_path):
    class LoudProvider:
        route_type = "cloud"

        def complete(self, prompt: str):
            return type(
                "Response",
                (),
                {
                    "text": "x" * 20,
                    "input_tokens": 1,
                    "output_tokens": 20,
                    "provider": "test",
                    "model": "loud",
                    "route_type": "cloud",
                    "estimated_cost_usd": 0.0,
                },
            )()

    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(Agent("sage", "SAGE", "workspace-sage"))
    approve_external_identity(
        store,
        provider="telegram",
        external_user_id="42",
        username="alice",
        decided_by="owner",
    )
    result = process_live_connector_message(
        store,
        IncomingConnectorMessage(
            provider="telegram",
            external_user_id="42",
            conversation_id="7",
            external_message_id="1",
            text="hello",
            channel="telegram:7",
            reply_target="7",
        ),
        default_agent="sage",
        model_provider=LoudProvider(),
        outbound_sender=lambda incoming, text: pytest.fail("outbound should be blocked"),
        max_outbound_chars=5,
    )

    assert result.status == "blocked"
    assert "outbound reply too large" in result.reason
    assert any("blocked outbound" in alert for alert in store.alerts())


def test_telegram_and_google_chat_outbound_helpers_can_be_stubbed():
    calls: list[dict[str, object]] = []

    def stub_post(url: str, payload: bytes, headers: dict[str, str]):
        calls.append({"url": url, "payload": json.loads(payload), "headers": headers})
        return {"ok": True, "result": {"message_id": 12}}

    result = send_telegram_message(
        "token",
        chat_id="7",
        text="hello",
        http_post=stub_post,
    )
    assert result.status == "sent"
    assert calls[0]["payload"]["text"] == "hello"  # type: ignore[index]

    google_result = google_chat_reply_sender()(
        incoming=type(
            "Incoming",
            (),
            {"thread_name": "spaces/x/threads/y", "channel": "google-chat:spaces/x"},
        )(),
        text="hello",
    )
    assert google_result.response_body == {
        "text": "hello",
        "thread": {"name": "spaces/x/threads/y"},
    }


def test_cli_connector_approvals_owner_only(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert main(["user", "add", "--username", "owner", "--role", "owner"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "connector",
                "approvals",
                "approve",
                "--provider",
                "telegram",
                "--external-user",
                "42",
                "--username",
                "alice",
            ]
        )
        == 0
    )
    approved = json.loads(capsys.readouterr().out)
    assert approved["status"] == "approved"

    assert main(["connector", "approvals", "list", "--provider", "telegram"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["external_user_id"] == "42"


def test_cli_model_oauth_login_status_logout_redacts_tokens(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert (
        main(
            [
                "model",
                "auth",
                "login",
                "--provider",
                "openai",
                "--method",
                "oauth",
                "--access-token",
                "access-secret",
                "--refresh-token",
                "refresh-secret",
            ]
        )
        == 0
    )
    login_payload = json.loads(capsys.readouterr().out)
    assert login_payload["credential"]["access_token"] == "***redacted***"

    assert main(["model", "auth", "status", "--provider", "openai"]) == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert "access-secret" not in json.dumps(status_payload)
    assert status_payload["providers"][0]["configured"] is True

    assert main(["model", "auth", "logout", "--provider", "openai"]) == 0
    logout_payload = json.loads(capsys.readouterr().out)
    assert logout_payload["deleted"] is True


def test_web_auth_api_running_service_behaviour(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")

    import brigade.web

    monkeypatch.setattr(
        brigade.web,
        "provider_from_settings",
        lambda *args, **kwargs: TestProvider(),
    )

    store = JsonStateStore(tmp_path / "state.json")
    owner = User(username="owner", role=Role.OWNER)
    observer = User(username="reader", role=Role.OBSERVER)
    store.add_user(owner)
    store.add_user(observer)
    store.add_agent(Agent("sage", "SAGE", "workspace-sage", "planner"))
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        require_auth=True,
        jwt_secret="x" * 40,
    )
    app = brigade.web.create_app(settings, store)
    owner_headers = {"Authorization": f"Bearer {issue_token(settings, owner)}"}
    observer_headers = {"Authorization": f"Bearer {issue_token(settings, observer)}"}
    expired_headers = {"Authorization": f"Bearer {issue_token(settings, owner, ttl_seconds=-60)}"}

    assert asyncio.run(_asgi_request(app, "GET", "/api/auth/me")).status_code == 401
    invalid_scheme = _asgi_request(
        app,
        "GET",
        "/api/auth/me",
        headers={"Authorization": "Basic abc"},
    )
    assert (
        asyncio.run(invalid_scheme).status_code == 401
    )
    assert (
        asyncio.run(_asgi_request(app, "GET", "/api/auth/me", headers=expired_headers)).status_code
        == 401
    )

    me = asyncio.run(_asgi_request(app, "GET", "/api/auth/me", headers=owner_headers))
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "owner"
    assert "mission:write" in me.json()["permissions"]
    assert me.json()["token"]["expires_at"]

    observer_me = asyncio.run(
        _asgi_request(app, "GET", "/api/auth/me", headers=observer_headers)
    )
    assert observer_me.status_code == 200
    assert "task:write" not in observer_me.json()["permissions"]

    issued = asyncio.run(_asgi_request(
        app,
        "POST",
        "/api/auth/token",
        headers=owner_headers,
        json_payload={"username": "operator", "role": "operator"},
    ))
    assert issued.status_code == 200
    assert issued.json()["token"]

    denied = asyncio.run(_asgi_request(
        app,
        "POST",
        "/api/teams",
        headers=observer_headers,
        json_payload={"team_id": "ops", "display_name": "Ops"},
    ))
    assert denied.status_code == 403

    settings_response = asyncio.run(
        _asgi_request(app, "GET", "/api/settings/effective", headers=owner_headers)
    )
    assert settings_response.status_code == 200
    assert settings_response.json()["jwt_secret"] == "***redacted***"

    cockpit = asyncio.run(_asgi_request(app, "GET", "/api/cockpit", headers=owner_headers))
    assert cockpit.status_code == 200
    cockpit_payload = cockpit.json()
    assert cockpit_payload["auth"]["require_auth"] is True
    assert cockpit_payload["counts"]["agents"] == 1
    assert cockpit_payload["models"]["default_provider"] == "ollama"
    assert cockpit_payload["usage"]["total_tokens"] == 0

    models = asyncio.run(_asgi_request(app, "GET", "/api/models", headers=owner_headers))
    assert models.status_code == 200
    model_payload = models.json()
    assert model_payload["recommended"]["provider"] == "ollama"
    assert all(option["provider"] != "fake" for option in model_payload["options"])

    chat = asyncio.run(_asgi_request(
        app,
        "POST",
        "/api/chat/ask-agent",
        headers=owner_headers,
        json_payload={"agent_id": "sage", "content": "Status?"},
    ))
    assert chat.status_code == 200
    assert chat.json()["status"] == "complete"
    assert len(store.messages(chat.json()["conversation_id"])) == 2
    assert store.usage_records()[0]["source"] == "user_chat"

    orchestrator_chat = asyncio.run(_asgi_request(
        app,
        "POST",
        "/api/chat/ask-orchestrator",
        headers=owner_headers,
        json_payload={"content": "What needs attention?", "provider": "ollama"},
    ))
    assert orchestrator_chat.status_code == 200
    assert orchestrator_chat.json()["status"] == "complete"
    assert store.messages("orchestrator")[-1].sender == "orchestrator"
    assert store.usage_records()[-1]["source"] == "orchestrator_chat"

    orchestrator_markdown = asyncio.run(
        _asgi_request(
            app,
            "POST",
            "/api/chat/ask-orchestrator-markdown",
            headers=owner_headers,
            json_payload={
                "content": "Use **bold** and `code` in your answer.",
                "provider": "ollama",
            },
        )
    )
    assert orchestrator_markdown.status_code == 200
    markdown_payload = orchestrator_markdown.json()
    assert markdown_payload["status"] == "complete"
    assert isinstance(markdown_payload["response_markdown"], str)
    assert "<p>" in markdown_payload["response_html"]


def test_orchestration_api_payload_is_embedded_in_cockpit_and_ops_room(tmp_path):
    pytest.importorskip("fastapi")

    from brigade.web import create_app

    store = JsonStateStore(tmp_path / "state.json")
    store.add_orchestrator_reasoning(
        {
            "reasoning_id": "reason-1",
            "cycle_id": "cycle-1",
            "ended_at": "2026-01-01T00:00:00+00:00",
            "source": "test",
            "mission_statement": "Test mission",
            "assigned": [],
            "skipped": [],
            "decision_summary": "assigned=0 skipped=0 alerts=0",
        }
    )
    app = create_app(
        Settings(config_path=tmp_path / "brigade.config.json", data_dir=tmp_path),
        store,
    )

    orchestration = asyncio.run(_asgi_request(app, "GET", "/api/orchestration")).json()
    cockpit = asyncio.run(_asgi_request(app, "GET", "/api/cockpit")).json()
    ops_room = asyncio.run(_asgi_request(app, "GET", "/api/ops-room")).json()

    assert orchestration["latest_event"]["summary"] == "assigned=0 skipped=0 alerts=0"
    assert (
        cockpit["orchestration"]["latest_event"]["summary"]
        == orchestration["latest_event"]["summary"]
    )
    assert (
        ops_room["orchestration"]["latest_event"]["summary"]
        == orchestration["latest_event"]["summary"]
    )


def test_web_static_root_uses_first_candidate_with_index(tmp_path):
    from brigade.web import _static_root

    missing = tmp_path / "missing"
    container_like = tmp_path / "app" / "web" / "dist"
    container_like.mkdir(parents=True)
    (container_like / "index.html").write_text("<div>built</div>", encoding="utf-8")

    assert _static_root([missing, container_like]) == container_like


def test_web_index_serves_built_spa(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")

    from brigade.config import Settings
    from brigade.state import JsonStateStore
    from brigade.web import create_app

    static_root = tmp_path / "web" / "dist"
    static_root.mkdir(parents=True)
    (static_root / "index.html").write_text("<div id=\"root\">built spa</div>", encoding="utf-8")
    (static_root / "assets").mkdir()
    monkeypatch.setattr("brigade.web._static_root", lambda candidates=None: static_root)

    settings = Settings(config_path=tmp_path / "brigade.config.json", data_dir=tmp_path)
    response = asyncio.run(
        _asgi_request(
            create_app(settings, JsonStateStore(tmp_path / "state.json")),
            "GET",
            "/",
        )
    )

    assert response.status_code == 200
    assert "built spa" in response.text
    assert "OpenBrigade web UI is not built" not in response.text


def test_markdown_renderer_outputs_safe_html():
    rendered = render_markdown_html(
        "# Header\n\n- item\n\nUse **bold** and *italic* with `code` and [link](https://example.com)\n"
    )
    assert "<h1>Header</h1>" in rendered
    assert "<ul>" in rendered
    assert "<strong>bold</strong>" in rendered
    assert "<em>italic</em>" in rendered
    assert "<code>code</code>" in rendered
    assert 'href="https://example.com"' in rendered


def test_alert_audit_reports_weak_jwt_secret(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert main(["user", "add", "--username", "owner", "--role", "owner"]) == 0
    capsys.readouterr()
    assert main(["alert", "audit", "--include-health"]) == 0
    payload = json.loads(capsys.readouterr().out)
    kinds = {item["kind"] for item in payload["findings"]}
    assert "weak_jwt_secret" in kinds
