from __future__ import annotations

import json
import threading
import time

import pytest

from brigade.config import Settings
from brigade.connectors import ConnectorResult, telegram_typing_indicator
from brigade.secrets import write_telegram_bot_token
from brigade.state import JsonStateStore
from brigade.telegram_polling import (
    TELEGRAM_POLLING_CURSOR_KEY,
    TelegramPollingSupervisor,
    TelegramPollingWorker,
)


class _Cursor:
    def __init__(self, initial: dict | None = None):
        self.values = dict(initial or {})

    def get_json(self, key: str):
        return self.values.get(key)

    def set_json(self, key: str, payload: dict):
        self.values[key] = dict(payload)


def _settings(tmp_path, **overrides) -> Settings:
    values = {
        "config_path": tmp_path / "brigade.config.json",
        "data_dir": tmp_path,
        "redis_url": "redis://unused",
        "telegram_bot_token": "telegram-test-token",
        "telegram_polling_enabled": True,
        "telegram_polling_timeout_seconds": 30,
        "telegram_default_agent": "exec",
        "connector_executive_chat_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


def test_polling_worker_clears_webhook_processes_updates_and_persists_cursor(tmp_path):
    calls: list[tuple[str, dict]] = []
    processed = []
    cursor = _Cursor()

    def post(url: str, payload: bytes, headers: dict[str, str]):
        calls.append((url.rsplit("/", 1)[-1], json.loads(payload)))
        if calls[-1][0] == "deleteWebhook":
            return {"ok": True, "result": True}
        if calls[-1][0] == "getUpdates" and not processed:
            return {
                "ok": True,
                "result": [
                    {
                        "update_id": 91,
                        "message": {
                            "message_id": 13,
                            "chat": {"id": 7},
                            "from": {"id": 42},
                            "text": "hello Executive",
                        },
                    }
                ],
            }
        return {"ok": True, "result": []}

    def process(store, incoming, **kwargs):
        with kwargs["working_indicator"]():
            pass
        processed.append((incoming, kwargs))
        return ConnectorResult("telegram", "complete", incoming.channel)

    worker = TelegramPollingWorker(
        _settings(tmp_path),
        JsonStateStore(tmp_path / "state.json"),
        http_post=post,
        cursor=cursor,
        process_message=process,
    )

    assert worker.poll_once() == 1
    assert [name for name, _ in calls] == [
        "deleteWebhook",
        "setMyCommands",
        "getUpdates",
        "sendChatAction",
    ]
    assert calls[0][1] == {"drop_pending_updates": False}
    assert [item["command"] for item in calls[1][1]["commands"]] == [
        "help",
        "who",
        "model",
        "new",
        "clear",
        "status",
    ]
    assert calls[2][1]["allowed_updates"] == ["message", "edited_message"]
    assert "offset" not in calls[2][1]
    assert calls[3][1] == {"chat_id": "7", "action": "typing"}
    assert processed[0][0].external_user_id == "42"
    assert processed[0][0].channel == "telegram:7"
    assert callable(processed[0][1]["chat_turn"])
    assert cursor.values[TELEGRAM_POLLING_CURSOR_KEY]["last_update_id"] == 91

    assert worker.poll_once() == 0
    assert [name for name, _ in calls] == [
        "deleteWebhook",
        "setMyCommands",
        "getUpdates",
        "sendChatAction",
        "getUpdates",
    ]
    assert calls[-1][1]["offset"] == 92


def test_polling_worker_rejects_conflicting_webhook_mode(tmp_path):
    worker = TelegramPollingWorker(
        _settings(tmp_path, telegram_webhook_enabled=True),
        JsonStateStore(tmp_path / "state.json"),
        cursor=_Cursor(),
    )

    with pytest.raises(RuntimeError, match="cannot both be enabled"):
        worker.poll_once()


def test_named_polling_worker_namespaces_provider_channel_and_cursor(tmp_path):
    calls: list[tuple[str, dict]] = []
    processed = []
    cursor = _Cursor()

    def post(url: str, payload: bytes, headers: dict[str, str]):
        method = url.rsplit("/", 1)[-1]
        body = json.loads(payload)
        calls.append((method, body))
        if method == "getUpdates":
            return {
                "ok": True,
                "result": [
                    {
                        "update_id": 12,
                        "message": {
                            "message_id": 4,
                            "chat": {"id": 7},
                            "from": {"id": 42},
                            "text": "hello chief",
                        },
                    }
                ]
                if not processed
                else [],
            }
        return {"ok": True, "result": True}

    def process(store, incoming, **kwargs):
        with kwargs["working_indicator"]():
            pass
        processed.append((incoming, kwargs))
        return ConnectorResult(incoming.provider, "complete", incoming.channel)

    worker = TelegramPollingWorker(
        _settings(
            tmp_path,
            telegram_polling_enabled=False,
            telegram_webhook_enabled=True,
        ),
        JsonStateStore(tmp_path / "state.json"),
        http_post=post,
        cursor=cursor,
        process_message=process,
        account={"account_id": "sage", "chief_agent_id": "sage", "enabled": True},
        bot_token="named-token",
    )

    assert worker.poll_once() == 1
    incoming, kwargs = processed[0]
    assert incoming.provider == "telegram:sage"
    assert incoming.channel == "telegram:sage:7"
    assert callable(kwargs["chat_turn"])
    assert cursor.values[f"{TELEGRAM_POLLING_CURSOR_KEY}:sage"]["last_update_id"] == 12


def test_named_polling_supervisor_restarts_rotated_token_binding(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    settings = _settings(
        tmp_path,
        postgres_dsn="postgresql://unused",
        telegram_polling_enabled=False,
    )
    account = {
        "account_id": "sage",
        "chief_agent_id": "sage",
        "enabled": True,
        "token_fingerprint": "first",
        "created_at": "2026-08-30T00:00:00+00:00",
        "updated_at": "2026-08-30T00:00:00+00:00",
    }
    store.upsert_telegram_account(account)
    write_telegram_bot_token(settings, "sage", "first-token")

    class FakeThread:
        alive = True

        def is_alive(self):
            return self.alive

    class FakeWorker:
        instances = []

        def __init__(self, settings, store, *, account, bot_token):
            self.account = dict(account)
            self.bot_token = bot_token
            self._thread = FakeThread()
            self.stopped = False
            self.__class__.instances.append(self)

        def start(self):
            return None

        def stop(self, timeout=45):
            self.stopped = True
            self._thread.alive = False

    supervisor = TelegramPollingSupervisor(settings, store, worker_factory=FakeWorker)
    supervisor.reconcile_once()
    first = FakeWorker.instances[-1]
    assert first.bot_token == "first-token"

    store.upsert_telegram_account(
        {**account, "token_fingerprint": "second", "updated_at": "2026-08-30T00:01:00+00:00"}
    )
    write_telegram_bot_token(settings, "sage", "second-token")
    supervisor.reconcile_once()

    assert first.stopped is True
    assert FakeWorker.instances[-1].bot_token == "second-token"


def test_polling_worker_rate_limits_duplicate_error_audits(tmp_path, monkeypatch):
    worker = TelegramPollingWorker(
        _settings(tmp_path),
        JsonStateStore(tmp_path / "state.json"),
        cursor=_Cursor(),
    )
    monotonic_values = iter((1.0, 2.0, 62.0))
    monkeypatch.setattr("brigade.telegram_polling.time.monotonic", lambda: next(monotonic_values))

    assert worker._should_audit_error("Conflict") is True
    assert worker._should_audit_error("Conflict") is False
    assert worker._should_audit_error("Conflict") is True


def test_typing_indicator_refreshes_until_turn_finishes():
    calls: list[tuple[str, dict]] = []
    refreshed = threading.Event()

    def post(url: str, payload: bytes, headers: dict[str, str]):
        calls.append((url.rsplit("/", 1)[-1], json.loads(payload)))
        if len(calls) >= 2:
            refreshed.set()
        return {"ok": True, "result": True}

    with telegram_typing_indicator(
        "test-token",
        chat_id="7",
        http_post=post,
        refresh_seconds=0.01,
    ):
        assert refreshed.wait(0.5)

    calls_after_exit = len(calls)
    time.sleep(0.03)

    assert len(calls) == calls_after_exit
    assert calls[0] == ("sendChatAction", {"chat_id": "7", "action": "typing"})


def test_typing_indicator_failure_does_not_block_turn():
    def failing_post(url: str, payload: bytes, headers: dict[str, str]):
        raise RuntimeError("temporary failure")

    with telegram_typing_indicator(
        "test-token",
        chat_id="7",
        http_post=failing_post,
    ):
        completed = True

    assert completed is True
