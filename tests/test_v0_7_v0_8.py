from __future__ import annotations

import json

import pytest

from brigade.auth import AuthResult
from brigade.cli import main
from brigade.config import Settings
from brigade.schemas import Agent, Role, User
from brigade.services import build_settings_payload, send_user_chat, set_config_value
from brigade.state import JsonStateStore
from brigade.time import add_seconds_iso, utc_now_iso
from brigade.tui import (
    _safe_addnstr,
    parse_chat_tui_command,
    render_chat_view,
    render_dashboard_view,
    render_settings_view,
)
from tests.helpers import TestProvider


def test_v07_config_inspect_set_and_db_status_without_postgres(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert main(["config", "set", "--key", "log_level", "--value", "DEBUG"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["key"] == "log_level"

    assert main(["config", "inspect"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["log_level"] == "DEBUG"
    assert payload["jwt_secret"] == "***redacted***"

    assert main(["db", "status"]) == 1
    status = json.loads(capsys.readouterr().out)
    assert status["store_backend"] == "unconfigured"
    assert "Postgres is required" in status["reason"]


def test_v08_user_chat_service_records_response_usage_and_episode(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    user = User(username="alice", role=Role.OPERATOR)
    store.add_user(user)
    store.add_agent(Agent("sage", "SAGE", "workspace-sage", "planner"))

    result = send_user_chat(
        store,
        AuthResult(ok=True, method="test", user=user),
        user=user,
        agent_id="sage",
        content="What should we do next?",
        provider=TestProvider(),
        idempotency_key="chat-1",
    )

    assert result["status"] == "complete"
    assert [message.sender for message in store.messages(result["conversation_id"])] == [
        "alice",
        "sage",
    ]
    assert store.usage_records()[0]["source"] == "user_chat"
    assert store.episodes()[0]["source"] == "user_chat"

    duplicate = send_user_chat(
        store,
        AuthResult(ok=True, method="test", user=user),
        user=user,
        agent_id="sage",
        content="What should we do next?",
        provider=TestProvider(),
        idempotency_key="chat-1",
    )
    assert duplicate["status"] == "duplicate"


def test_v08_user_chat_local_route_does_not_apply_agent_run_cooldown(tmp_path):
    class LocalProvider(TestProvider):
        route_type = "local"

    store = JsonStateStore(tmp_path / "state.json")
    user = User(username="alice", role=Role.OPERATOR)
    store.add_user(user)
    store.add_agent(Agent("sage", "SAGE", "workspace-sage", "planner"))
    actor = AuthResult(ok=True, method="test", user=user)
    now = utc_now_iso()
    store.set_local_inference(
        {
            "status": "idle",
            "holder": None,
            "last_completed": now,
            "next_available": add_seconds_iso(now, 900),
        }
    )

    first = send_user_chat(
        store,
        actor,
        user=user,
        agent_id="sage",
        content="first",
        provider=LocalProvider(),
    )
    second = send_user_chat(
        store,
        actor,
        user=user,
        agent_id="sage",
        content="second",
        provider=LocalProvider(),
    )

    assert first["status"] == "complete"
    assert second["status"] == "complete"
    assert store.local_inference()["status"] == "idle"


def test_v08_settings_payload_and_render_are_redacted(tmp_path):
    config_path = tmp_path / "brigade.config.json"
    set_config_value(config_path, "require_auth", "true")
    settings = Settings(config_path=config_path, data_dir=tmp_path, jwt_secret="secret")
    payload = build_settings_payload(settings)

    assert payload["jwt_secret"] == "***redacted***"
    assert "require_auth" in payload["editable_keys"]
    rendered = render_settings_view(payload)
    assert "jwt_secret: ***redacted***" in rendered
    assert "jwt_secret: secret" not in rendered


def test_v08_chat_tui_plain_render(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(Agent("sage", "SAGE", "workspace-sage"))
    payload = {"selected_channel": "user:alice:sage", "messages": [], "channels": [], "agents": []}

    assert "No messages" in render_chat_view(payload, "user:alice:sage")


def test_chat_tui_slash_commands_parse_agent_switches():
    assert parse_chat_tui_command("/agent sage").action == "agent"
    assert parse_chat_tui_command("/switch builder").argument == "builder"
    assert parse_chat_tui_command("/q").action == "quit"
    assert parse_chat_tui_command("hello") is None


def test_tui_plain_render_truncates_oversized_content():
    long_text = "x" * 500
    rendered = render_dashboard_view(
        {
            "mission": {
                "statement": long_text,
                "success_criteria": [long_text],
                "explicitly_not": [],
                "latest_reasoning": long_text,
                "latest_cycle_id": "cycle",
            }
        },
        "mission",
    )

    assert len(max(rendered.splitlines(), key=len)) <= 240


def test_tui_safe_addnstr_ignores_tiny_width():
    class Screen:
        calls = 0

        def addnstr(self, row, col, text, width):
            self.calls += 1

    screen = Screen()
    _safe_addnstr(screen, 0, 0, "hello", 1)
    _safe_addnstr(screen, 0, 0, "hello", 8)

    assert screen.calls == 1


def test_v08_web_app_exposes_gateway_routes(tmp_path):
    pytest.importorskip("fastapi")

    from brigade.web import create_app

    store = JsonStateStore(tmp_path / "state.json")
    user = User(username="owner", role=Role.OWNER)
    store.add_user(user)
    store.add_agent(Agent("sage", "SAGE", "workspace-sage"))
    settings = Settings(config_path=tmp_path / "brigade.config.json", data_dir=tmp_path)
    app = create_app(settings, store)
    paths = {route.path for route in app.routes}

    assert "/healthz" in paths
    assert "/api/chat/ask-agent" in paths
    assert "/api/settings/effective" in paths
    assert "/api/teams" in paths
