from __future__ import annotations

import importlib.util
import socket
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("browser_worker", ROOT / "ops" / "browser_worker.py")
assert SPEC and SPEC.loader
worker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = worker
SPEC.loader.exec_module(worker)


def test_public_url_policy_rejects_private_and_non_http_destinations(monkeypatch):
    monkeypatch.setattr(
        worker.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    assert worker._public_http_url("https://example.com/path") == (True, None)
    assert worker._public_http_url("file:///etc/passwd")[0] is False
    assert worker._public_http_url("http://localhost:8080")[0] is False
    monkeypatch.setattr(
        worker.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))],
    )
    assert worker._public_http_url("https://rebound.example")[1] == "private_destination"


def test_profile_policy_requires_explicit_principal_and_secure_storage():
    with pytest.raises(ValueError, match="allowed_principals"):
        worker._profile_policy('{"research": {"storage": "encrypted_volume"}}')
    with pytest.raises(ValueError, match="encrypted or external"):
        worker._profile_policy('{"research": {"allowed_principals": ["agent:exec"]}}')
    policy = worker._profile_policy(
        '{"research": {"allowed_principals": ["agent:exec"], "storage": "external_provider"}}'
    )
    assert policy["research"]["allowed_principals"] == {"agent:exec"}


def test_worker_enforces_profile_principal_not_only_the_tool_caller(tmp_path):
    state = worker.BrowserState(
        tmp_path / "profiles",
        limits=worker.BrowserLimits(),
        profile_policy=worker._profile_policy(
            '{"research": {"allowed_principals": ["agent:exec"], "storage": "external_provider"}}'
        ),
        audit_path=tmp_path / "audit.jsonl",
    )
    handler = object.__new__(worker.Handler)
    handler.state = state
    assert handler._authorize_profile({"profile": "research", "principal": "exec"}) == "research"
    with pytest.raises(worker.BrowserPolicyError, match="not authorized"):
        handler._authorize_profile({"profile": "research", "principal": "researcher"})


class _Route:
    def __init__(self) -> None:
        self.action = ""

    def continue_(self) -> None:
        self.action = "continued"

    def abort(self) -> None:
        self.action = "aborted"


class _Request:
    def __init__(self, url: str, method: str = "GET") -> None:
        self.url = url
        self.method = method


class _Context:
    def __init__(self) -> None:
        self.guard = None
        self.response_handler = None

    def route(self, _pattern: str, guard) -> None:
        self.guard = guard

    def on(self, event: str, handler) -> None:
        if event == "response":
            self.response_handler = handler


def test_worker_revalidates_each_browser_request_and_blocks_redirect_to_private_space(
    tmp_path, monkeypatch
):
    state = worker.BrowserState(
        tmp_path / "profiles",
        limits=worker.BrowserLimits(),
        profile_policy={},
        audit_path=tmp_path / "audit.jsonl",
    )
    state.sessions["public:default"] = {
        "payload": {"url": "https://public.example/path", "principal": "agent"},
        "request_count": 0,
    }
    context = _Context()
    worker._install_network_guard(context, state, "public:default")
    monkeypatch.setattr(
        worker.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", 443))],
    )
    route = _Route()
    context.guard(route, _Request("https://redirected.example/internal"))
    assert route.action == "aborted"
    assert "private_destination" in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


def test_public_session_key_cannot_resolve_or_authorize_a_named_profile(tmp_path):
    state = worker.BrowserState(
        tmp_path / "profiles",
        limits=worker.BrowserLimits(),
        profile_policy=worker._profile_policy(
            '{"research": {"allowed_principals": ["agent:exec"], "storage": "external_provider"}}'
        ),
        audit_path=tmp_path / "audit.jsonl",
    )
    state.sessions["public:default"] = {"payload": {}, "request_count": 0}
    handler = object.__new__(worker.Handler)
    handler.state = state
    with pytest.raises(worker.BrowserPolicyError, match="not authorized"):
        handler._authorize_profile({"profile": "research", "principal": "researcher"})
    with pytest.raises(worker.BrowserPolicyError, match="session was not found"):
        handler._record_for({"profile": "research", "session_id": "default"})


def test_saturated_worker_rejects_later_operations_without_waiting_for_hung_page(tmp_path):
    state = worker.BrowserState(
        tmp_path / "profiles",
        limits=worker.BrowserLimits(max_concurrent_operations=1, max_queue=1),
        profile_policy={},
        audit_path=tmp_path / "audit.jsonl",
    )
    handler = object.__new__(worker.Handler)
    handler.state = state
    assert handler._acquire_operation() is True
    started = time.monotonic()
    assert handler._acquire_operation() is False
    assert time.monotonic() - started < 0.1
    state.operation_slots.release()
    state.queued = 0


def test_audit_redacts_query_parameters_and_records_policy_outcome(tmp_path):
    state = worker.BrowserState(
        tmp_path / "profiles",
        limits=worker.BrowserLimits(),
        profile_policy={},
        audit_path=tmp_path / "audit.jsonl",
    )
    state.audit(
        "blocked_request",
        payload={"url": "https://example.com/path?token=secret", "principal": "agent:exec"},
        outcome="blocked",
        reason="private_destination",
    )
    record = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "token" not in record and "secret" not in record
    assert '"policy_outcome": "blocked"' in record


def test_queue_limit_rejects_without_leaking_capacity(tmp_path):
    state = worker.BrowserState(
        tmp_path / "profiles",
        limits=worker.BrowserLimits(max_concurrent_operations=1, max_queue=1),
        profile_policy={},
        audit_path=tmp_path / "audit.jsonl",
    )
    assert state.operation_slots.acquire(blocking=False)
    try:
        assert state.operation_slots.acquire(blocking=False) is False
    finally:
        state.operation_slots.release()
