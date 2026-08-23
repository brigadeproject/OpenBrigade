from __future__ import annotations

import json

import pytest

from brigade.config import Settings
from brigade.connectors import ConnectorResult
from brigade.state import JsonStateStore
from brigade.telegram_polling import TELEGRAM_POLLING_CURSOR_KEY, TelegramPollingWorker


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
        if len(calls) == 2:
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
    assert [name for name, _ in calls] == ["deleteWebhook", "getUpdates"]
    assert calls[0][1] == {"drop_pending_updates": False}
    assert calls[1][1]["allowed_updates"] == ["message", "edited_message"]
    assert "offset" not in calls[1][1]
    assert processed[0][0].external_user_id == "42"
    assert processed[0][0].channel == "telegram:7"
    assert callable(processed[0][1]["chat_turn"])
    assert cursor.values[TELEGRAM_POLLING_CURSOR_KEY]["last_update_id"] == 91

    assert worker.poll_once() == 0
    assert [name for name, _ in calls] == ["deleteWebhook", "getUpdates", "getUpdates"]
    assert calls[-1][1]["offset"] == 92


def test_polling_worker_rejects_conflicting_webhook_mode(tmp_path):
    worker = TelegramPollingWorker(
        _settings(tmp_path, telegram_webhook_enabled=True),
        JsonStateStore(tmp_path / "state.json"),
        cursor=_Cursor(),
    )

    with pytest.raises(RuntimeError, match="cannot both be enabled"):
        worker.poll_once()


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
