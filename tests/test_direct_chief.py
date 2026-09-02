from __future__ import annotations

import asyncio
import os

import pytest

from brigade.auth import issue_token
from brigade.config import Settings
from brigade.direct_chief import (
    public_telegram_account,
    remove_telegram_account,
    save_chief_chat_policy,
    save_telegram_account,
)
from brigade.schemas import Agent, Role, Team, User
from brigade.secrets import read_telegram_bot_token
from brigade.state import JsonStateStore


def _store(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(Agent("chief0", "Chief Zero", "workspace-chief0", role="crew_chief"))
    store.add_agent(Agent("worker0", "Worker Zero", "workspace-worker0"))
    store.upsert_team(
        Team(
            "team0",
            "Team Zero",
            crew_chief_id="chief0",
            members=["chief0", "worker0"],
        )
    )
    return store


def _settings(tmp_path):
    return Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        secret_store_path=tmp_path / "secrets",
        allow_json_store=True,
    )


def test_named_telegram_account_stores_token_outside_record(tmp_path):
    store = _store(tmp_path)
    settings = _settings(tmp_path)

    result = save_telegram_account(
        store,
        settings,
        account_id="infra-chief",
        chief_agent_id="chief0",
        label="Infrastructure Chief",
        token="123:secret-token",
        enabled=True,
        actor="owner",
    )

    assert result["account_id"] == "infra-chief"
    assert result["token_configured"] is True
    assert "token_fingerprint" not in result
    assert "secret_ref" not in result
    stored = store.find_telegram_account("infra-chief")
    assert "secret-token" not in str(stored)
    assert public_telegram_account(stored) == result
    assert read_telegram_bot_token(settings, "infra-chief") == "123:secret-token"
    token_mode = os.stat(
        settings.secret_store_path / "connectors/telegram/infra-chief.token"
    ).st_mode
    assert token_mode & 0o777 == 0o600


def test_account_requires_chief_unique_token_and_disable_before_remove(tmp_path):
    store = _store(tmp_path)
    settings = _settings(tmp_path)

    with pytest.raises(ValueError, match="not an active Crew Chief"):
        save_telegram_account(
            store,
            settings,
            account_id="worker",
            chief_agent_id="worker0",
            token="token-a",
        )

    save_telegram_account(
        store,
        settings,
        account_id="chief-a",
        chief_agent_id="chief0",
        token="token-a",
        enabled=True,
    )
    with pytest.raises(ValueError, match="already assigned"):
        save_telegram_account(
            store,
            settings,
            account_id="chief-b",
            chief_agent_id="chief0",
            token="token-a",
        )
    with pytest.raises(ValueError, match="disable"):
        remove_telegram_account(store, settings, "chief-a")

    save_telegram_account(
        store,
        settings,
        account_id="chief-a",
        chief_agent_id="chief0",
        enabled=False,
    )
    assert remove_telegram_account(
        store, settings, "chief-a", delete_secret=True, actor="owner"
    ) is True
    assert read_telegram_bot_token(settings, "chief-a") is None


def test_direct_policy_is_opt_in_and_requires_explicit_groups(tmp_path):
    store = _store(tmp_path)
    settings = _settings(tmp_path)

    with pytest.raises(ValueError, match="at least one"):
        save_chief_chat_policy(
            store,
            settings,
            chief_agent_id="chief0",
            direct_enabled=True,
            tool_groups=[],
        )
    policy = save_chief_chat_policy(
        store,
        settings,
        chief_agent_id="chief0",
        direct_enabled=True,
        tool_groups=["maintenance", "workspace_read", "maintenance"],
        actor="owner",
    )
    assert policy["direct_enabled"] is True
    assert policy["tool_groups"] == ["maintenance", "workspace_read"]
    assert policy["max_tool_calls"] == 60
    assert store.chief_chat_policy("chief0") == policy


def test_interactive_turn_store_filters_by_status_and_thread(tmp_path):
    store = _store(tmp_path)
    base = {
        "chief_agent_id": "chief0",
        "operator_username": "owner",
        "created_at": "2026-08-30T00:00:00+00:00",
        "updated_at": "2026-08-30T00:00:00+00:00",
    }
    store.upsert_chief_interactive_turn(
        {**base, "turn_id": "turn-a", "thread_id": "thread-a", "status": "queued"}
    )
    store.upsert_chief_interactive_turn(
        {**base, "turn_id": "turn-b", "thread_id": "thread-b", "status": "complete"}
    )

    assert [item["turn_id"] for item in store.chief_interactive_turns(status="queued")] == [
        "turn-a"
    ]
    assert store.try_claim_chief_interactive_turn(
        "turn-a", "worker-a", lease_seconds=60
    ) is True
    assert store.try_claim_chief_interactive_turn(
        "turn-a", "worker-b", lease_seconds=60
    ) is False
    assert store.find_chief_interactive_turn("turn-b")["status"] == "complete"
    assert store.delete_chief_interactive_turn("turn-b") is True
    assert store.chief_interactive_turns(thread_id="thread-b") == []


def test_named_account_and_policy_web_management(tmp_path):
    pytest.importorskip("fastapi")
    from brigade.web import create_app
    from tests.test_v0_9 import _asgi_request

    store = _store(tmp_path)
    owner = User("owner", Role.OWNER)
    store.add_user(owner)
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        secret_store_path=tmp_path / "secrets",
        require_auth=True,
        jwt_secret="x" * 40,
        allow_json_store=True,
    )
    telegram_calls = []

    def telegram_post(url, payload, headers):
        telegram_calls.append((url, payload, headers))
        return {"ok": True, "result": {"username": "chief_zero_bot"}}

    app = create_app(settings, store, telegram_http_post=telegram_post)
    headers = {"Authorization": f"Bearer {issue_token(settings, owner)}"}
    created = asyncio.run(
        _asgi_request(
            app,
            "POST",
            "/api/connectors/telegram/accounts",
            headers=headers,
            json_payload={
                "account_id": "chief-zero",
                "chief_agent_id": "chief0",
                "token": "123:secret",
            },
        )
    )
    assert created.status_code == 200
    assert "secret" not in created.text
    tested = asyncio.run(
        _asgi_request(
            app,
            "POST",
            "/api/connectors/telegram/accounts/chief-zero/test",
            headers=headers,
        )
    )
    assert tested.status_code == 200
    assert tested.json()["account"]["bot_username"] == "chief_zero_bot"
    assert telegram_calls

    policy = asyncio.run(
        _asgi_request(
            app,
            "PUT",
            "/api/chief-chat/policies/chief0",
            headers=headers,
            json_payload={
                "direct_enabled": True,
                "tool_groups": ["workspace_read", "maintenance"],
            },
        )
    )
    assert policy.status_code == 200
    assert policy.json()["direct_enabled"] is True


def test_owner_direct_chief_web_turn_is_queued_and_cancellable(tmp_path):
    pytest.importorskip("fastapi")
    from brigade.web import create_app
    from tests.test_v0_9 import _asgi_request

    store = _store(tmp_path)
    owner = User("owner", Role.OWNER)
    store.add_user(owner)
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        require_auth=True,
        jwt_secret="x" * 40,
        allow_json_store=True,
    )
    save_chief_chat_policy(
        store,
        settings,
        chief_agent_id="chief0",
        direct_enabled=True,
        tool_groups=["workspace_read"],
        actor="owner",
    )
    app = create_app(settings, store)
    headers = {"Authorization": f"Bearer {issue_token(settings, owner)}"}
    opened = asyncio.run(
        _asgi_request(
            app,
            "POST",
            "/api/chat/threads",
            headers=headers,
            json_payload={"persona": "chief:chief0"},
        )
    ).json()
    queued = asyncio.run(
        _asgi_request(
            app,
            "POST",
            f"/api/chat/threads/{opened['thread_id']}/messages",
            headers=headers,
            json_payload={"content": "inspect the service", "idempotency_key": "web-1"},
        )
    )
    assert queued.status_code == 202
    turn_id = queued.json()["turn_id"]
    status = asyncio.run(
        _asgi_request(app, "GET", f"/api/chat/turns/{turn_id}", headers=headers)
    )
    assert status.json()["status"] == "queued"
    cancelled = asyncio.run(
        _asgi_request(
            app,
            "POST",
            f"/api/chat/turns/{turn_id}/cancel",
            headers=headers,
        )
    )
    assert cancelled.json()["status"] == "cancelled"
    assert store.find_chief_interactive_turn(turn_id)["cancel_requested"] is True


def test_named_bot_cli_commands_parse_explicit_secret_and_policy_options():
    from brigade.cli import build_parser

    account = build_parser().parse_args(
        [
            "connector",
            "telegram-account",
            "add",
            "--id",
            "infra-chief",
            "--chief",
            "chief0",
            "--token-stdin",
        ]
    )
    assert account.telegram_account_command == "add"
    assert account.token_stdin is True

    policy = build_parser().parse_args(
        [
            "connector",
            "chief-policy",
            "set",
            "--chief",
            "chief0",
            "--direct",
            "on",
            "--tool-group",
            "maintenance",
        ]
    )
    assert policy.chief_policy_command == "set"
    assert policy.tool_group == ["maintenance"]
