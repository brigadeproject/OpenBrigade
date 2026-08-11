#!/usr/bin/env python3
"""Isolated, public-web Playwright worker.

The worker is intentionally a policy enforcement point, not a thin browser
proxy.  It performs its own URL, profile, resource, and audit checks because a
caller can be buggy or compromised.
"""
# ruff: noqa: E501
from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import re
import socket
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4


class BrowserPolicyError(RuntimeError):
    """A safe, structured worker policy or capacity failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class BrowserLimits:
    operation_timeout_ms: int = 30_000
    page_lifetime_seconds: int = 300
    session_ttl_seconds: int = 900
    max_requests_per_session: int = 100
    max_response_bytes: int = 5_000_000
    max_rendered_text_bytes: int = 1_000_000
    max_screenshot_bytes: int = 2_000_000
    max_screenshot_dimension: int = 4_096
    max_sessions: int = 8
    max_concurrent_operations: int = 4
    max_queue: int = 16


_SAFE_PROFILE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _redacted_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _public_http_url(url: str) -> tuple[bool, str | None]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False, "disallowed_scheme"
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return False, "private_destination"
    try:
        addresses = socket.getaddrinfo(
            host,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except (socket.gaierror, UnicodeError):
        return False, "dns_resolution_failed"
    if not addresses:
        return False, "dns_resolution_failed"
    for address in addresses:
        try:
            value = ipaddress.ip_address(address[4][0])
        except ValueError:
            return False, "dns_resolution_failed"
        if not value.is_global:
            return False, "private_destination"
    return True, None


def _profile_policy(raw: str | None) -> dict[str, dict[str, Any]]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("BRIGADE_BROWSER_PROFILE_POLICY must be JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("BRIGADE_BROWSER_PROFILE_POLICY must be an object")
    parsed: dict[str, dict[str, Any]] = {}
    for name, policy in data.items():
        if not isinstance(name, str) or not _SAFE_PROFILE.fullmatch(name):
            raise ValueError("browser profile names must be safe identifiers")
        if not isinstance(policy, dict):
            raise ValueError("browser profile policy must be an object")
        principals = policy.get("allowed_principals")
        if not isinstance(principals, list) or not principals or not all(
            isinstance(item, str) and item for item in principals
        ):
            raise ValueError("browser profile policy requires allowed_principals")
        if policy.get("storage") not in {"encrypted_volume", "external_provider"}:
            raise ValueError("browser profile policy requires encrypted or external storage")
        parsed[name] = {
            "allowed_principals": set(principals),
            "storage": policy["storage"],
            "ttl_seconds": max(60, min(int(policy.get("ttl_seconds", 900)), 86_400)),
        }
    return parsed


class BrowserState:
    def __init__(
        self,
        profile_root: Path,
        *,
        limits: BrowserLimits,
        profile_policy: dict[str, dict[str, Any]],
        audit_path: Path,
    ) -> None:
        self.profile_root = profile_root
        self.limits = limits
        self.profile_policy = profile_policy
        self.audit_path = audit_path
        self.lock = threading.RLock()
        self.sessions: dict[str, dict[str, Any]] = {}
        self.operation_slots = threading.BoundedSemaphore(limits.max_concurrent_operations)
        self.queued = 0

    def audit(
        self,
        event: str,
        *,
        payload: dict[str, Any],
        outcome: str,
        reason: str | None = None,
        final_url: str | None = None,
    ) -> str:
        record_id = str(uuid4())
        record = {
            "event_id": record_id,
            "at": int(time.time()),
            "category": "browser_public" if not payload.get("profile") else "browser_profile",
            "event": event,
            "principal": str(payload.get("principal") or ""),
            "profile": str(payload.get("profile") or "") or None,
            "url": _redacted_url(str(payload.get("url") or "")) or None,
            "final_url": _redacted_url(final_url or "") or None,
            "policy_outcome": outcome,
            "reason": reason,
        }
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        return record_id

    def close_expired(self) -> None:
        now = time.time()
        stale: list[str] = []
        with self.lock:
            for key, record in self.sessions.items():
                age = now - record["created_at"]
                ttl = record["ttl_seconds"]
                if now - record["used_at"] > ttl or age > self.limits.page_lifetime_seconds:
                    stale.append(key)
            for key in stale:
                self._close_session(key, "session_expired")

    def _close_session(self, key: str, reason: str) -> None:
        record = self.sessions.pop(key, None)
        if record is None:
            return
        try:
            record["context"].close()
        except Exception:
            pass
        self.audit("session_close", payload=record["payload"], outcome="allowed", reason=reason)


def _install_network_guard(context: Any, state: BrowserState, key: str) -> None:
    def _guard(route: Any, request: Any) -> None:
        record = state.sessions.get(key)
        payload = record["payload"] if record else {}
        url = str(request.url)
        allowed, reason = _public_http_url(url)
        method = str(request.method).upper()
        if method not in {"GET", "HEAD"}:
            allowed, reason = False, "disallowed_request_method"
        if record is not None:
            record["request_count"] += 1
            if record["request_count"] > state.limits.max_requests_per_session:
                allowed, reason = False, "request_budget_exhausted"
        if allowed:
            route.continue_()
        else:
            state.audit("blocked_request", payload=payload, outcome="blocked", reason=reason, final_url=url)
            route.abort()

    def _response(response: Any) -> None:
        record = state.sessions.get(key)
        if record is None:
            return
        length = str(response.headers.get("content-length") or "")
        try:
            size = int(length)
        except ValueError:
            return
        if size > state.limits.max_response_bytes:
            record["response_limit_hit"] = True
            state.audit(
                "limit_failure",
                payload=record["payload"],
                outcome="blocked",
                reason="response_body_budget_exceeded",
                final_url=str(response.url),
            )
            try:
                record["page"].close()
            except Exception:
                pass

    context.route("**/*", _guard)
    context.on("response", _response)


def _load_playwright():
    try:
        from playwright.sync_api import sync_playwright

        manager = sync_playwright().start()
        browser = manager.chromium.launch(headless=True)
        return manager, browser
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("Playwright/Chromium unavailable") from exc


class Handler(BaseHTTPRequestHandler):
    state: BrowserState
    manager: Any = None
    browser: Any = None

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._json(
                {
                    "ok": True,
                    "sessions": len(self.state.sessions),
                    "queue": self.state.queued,
                    "capacity": self.state.limits.max_concurrent_operations,
                }
            )
        else:
            self._json({"ok": False, "error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        self.state.close_expired()
        if not self._acquire_operation():
            self._json(self._failure("queue_saturated", "browser worker queue is saturated"), status=429)
            return
        payload: dict[str, Any] = {}
        action = self.path.lstrip("/")
        try:
            payload = self._read_json()
            if action == "open":
                result = self._open(payload)
            elif action == "extract":
                result = self._extract(payload)
            elif action == "click":
                result = self._click(payload)
            elif action == "screenshot":
                result = self._screenshot(payload)
            elif action == "clear-profile":
                result = self._clear_profile(payload)
            else:
                result = self._failure("not_found", "not found")
            self._json(result, status=200 if result.get("ok") else 400)
        except BrowserPolicyError as exc:
            self.state.audit(action or "request", payload=payload, outcome="blocked", reason=exc.code)
            self._json(self._failure(exc.code, str(exc)), status=400)
        except Exception:  # noqa: BLE001
            self.state.audit(action or "request", payload=payload, outcome="failed", reason="worker_error")
            self._json(self._failure("worker_error", "browser worker operation failed"), status=500)
        finally:
            self.state.operation_slots.release()
            with self.state.lock:
                self.state.queued = max(0, self.state.queued - 1)

    def _acquire_operation(self) -> bool:
        with self.state.lock:
            if self.state.queued >= self.state.limits.max_queue:
                return False
        if not self.state.operation_slots.acquire(blocking=False):
            return False
        with self.state.lock:
            self.state.queued += 1
        return True

    def _open(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = self._required_url(payload)
        page = self._page(payload)
        page.goto(url, wait_until="domcontentloaded", timeout=self.state.limits.operation_timeout_ms)
        result = self._page_payload(page, payload)
        self.state.audit("navigation", payload=payload, outcome="allowed", final_url=result["url"])
        return result

    def _extract(self, payload: dict[str, Any]) -> dict[str, Any]:
        page = self._page(payload)
        if payload.get("url"):
            opened = self._open(payload)
            if not opened.get("ok"):
                return opened
        record = self._record_for(payload)
        if record.get("response_limit_hit"):
            raise BrowserPolicyError("response_body_budget_exceeded", "response body exceeded worker limit")
        text = page.locator("body").inner_text(timeout=self.state.limits.operation_timeout_ms)
        encoded = text.encode("utf-8")
        if len(encoded) > self.state.limits.max_rendered_text_bytes:
            text = encoded[: self.state.limits.max_rendered_text_bytes].decode("utf-8", errors="ignore")
            truncated = True
        else:
            truncated = False
        result = self._page_payload(page, payload)
        result.update({"text": text, "rendered_text_truncated": truncated})
        self.state.audit("extract", payload=payload, outcome="allowed", final_url=result["url"])
        return result

    def _click(self, payload: dict[str, Any]) -> dict[str, Any]:
        selector = str(payload.get("selector") or "").strip()
        if not selector:
            raise BrowserPolicyError("invalid_request", "selector is required")
        page = self._page(payload)
        page.click(selector, timeout=self.state.limits.operation_timeout_ms)
        result = self._page_payload(page, payload)
        self.state.audit("click", payload=payload, outcome="allowed", final_url=result["url"])
        return result

    def _screenshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        page = self._page(payload)
        viewport = page.viewport_size or {}
        if max(int(viewport.get("width", 0)), int(viewport.get("height", 0))) > self.state.limits.max_screenshot_dimension:
            raise BrowserPolicyError("screenshot_dimension_exceeded", "screenshot dimensions exceed worker limit")
        data = page.screenshot(full_page=bool(payload.get("full_page", True)), type="jpeg", quality=70)
        if len(data) > self.state.limits.max_screenshot_bytes:
            raise BrowserPolicyError("screenshot_budget_exceeded", "screenshot exceeded worker byte limit")
        result = self._page_payload(page, payload)
        result["image_b64"] = base64.b64encode(data).decode("ascii")
        self.state.audit("screenshot", payload=payload, outcome="allowed", final_url=result["url"])
        return result

    def _clear_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        profile = self._authorize_profile(payload)
        principal = str(payload.get("principal") or "")
        allowed = self.state.profile_policy[profile]["allowed_principals"]
        candidates = {principal, f"agent:{principal}"}
        team_id = str(payload.get("team_id") or "")
        if team_id:
            candidates.add(f"team:{team_id}")
        if not candidates.intersection(allowed):
            raise BrowserPolicyError("profile_denied", "principal cannot clear this browser profile")
        with self.state.lock:
            for key in list(self.state.sessions):
                if key.startswith(f"profile:{profile}:"):
                    self.state._close_session(key, "profile_cleared")
        profile_path = self.state.profile_root / profile
        if profile_path.exists():
            import shutil

            shutil.rmtree(profile_path)
        self.state.audit("profile_clear", payload=payload, outcome="allowed")
        return {"ok": True, "profile": profile}

    def _page(self, payload: dict[str, Any]):
        if self.browser is None:
            self.manager, self.browser = _load_playwright()
        session_id = str(payload.get("session_id") or "default")
        if not _SAFE_PROFILE.fullmatch(session_id):
            raise BrowserPolicyError("invalid_session", "session id must be a safe identifier")
        profile = str(payload.get("profile") or "").strip()
        if profile:
            self._authorize_profile(payload)
        key = f"profile:{profile}:{session_id}" if profile else f"public:{session_id}"
        with self.state.lock:
            record = self.state.sessions.get(key)
            if record is None:
                if len(self.state.sessions) >= self.state.limits.max_sessions:
                    raise BrowserPolicyError("session_capacity_exceeded", "browser session capacity is exhausted")
                if profile:
                    context = self.manager.chromium.launch_persistent_context(
                        str(self.state.profile_root / profile),
                        headless=True,
                        accept_downloads=False,
                        permissions=[],
                        viewport={"width": 1280, "height": 720},
                    )
                    ttl = self.state.profile_policy[profile]["ttl_seconds"]
                else:
                    context = self.browser.new_context(
                        accept_downloads=False,
                        permissions=[],
                        viewport={"width": 1280, "height": 720},
                    )
                    ttl = self.state.limits.session_ttl_seconds
                record = {
                    "context": context,
                    "page": context.new_page(),
                    "created_at": time.time(),
                    "used_at": time.time(),
                    "ttl_seconds": ttl,
                    "request_count": 0,
                    "response_limit_hit": False,
                    "payload": dict(payload),
                }
                self.state.sessions[key] = record
                _install_network_guard(context, self.state, key)
                page = record["page"]
                page.on("popup", lambda popup: popup.close())
                self.state.audit("open", payload=payload, outcome="allowed")
            record["used_at"] = time.time()
            return record["page"]

    def _record_for(self, payload: dict[str, Any]) -> dict[str, Any]:
        profile = str(payload.get("profile") or "").strip()
        session_id = str(payload.get("session_id") or "default")
        key = f"profile:{profile}:{session_id}" if profile else f"public:{session_id}"
        record = self.state.sessions.get(key)
        if record is None:
            raise BrowserPolicyError("session_missing", "browser session was not found")
        return record

    def _authorize_profile(self, payload: dict[str, Any]) -> str:
        profile = str(payload.get("profile") or "").strip()
        principal = str(payload.get("principal") or "")
        if not _SAFE_PROFILE.fullmatch(profile):
            raise BrowserPolicyError("invalid_profile", "profile must be a safe identifier")
        policy = self.state.profile_policy.get(profile)
        candidates = {principal, f"agent:{principal}"}
        team_id = str(payload.get("team_id") or "")
        if team_id:
            candidates.add(f"team:{team_id}")
        if policy is None or not candidates.intersection(policy["allowed_principals"]):
            raise BrowserPolicyError("profile_denied", "profile is not authorized for this principal")
        return profile

    def _required_url(self, payload: dict[str, Any]) -> str:
        url = str(payload.get("url") or "").strip()
        allowed, reason = _public_http_url(url)
        if not allowed:
            raise BrowserPolicyError(reason or "invalid_url", "browser refused non-public HTTP(S) URL")
        return url

    def _page_payload(self, page: Any, payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "url": page.url, "title": page.title(), "audit_ref": self.state.audit("page", payload=payload, outcome="allowed", final_url=page.url)}

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length") or "0")
        if not 0 <= length <= 65_536:
            raise BrowserPolicyError("request_too_large", "request exceeded worker limit")
        value = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        if not isinstance(value, dict):
            raise BrowserPolicyError("invalid_request", "request must be a JSON object")
        return value

    @staticmethod
    def _failure(code: str, message: str) -> dict[str, Any]:
        return {"ok": False, "error": message, "error_code": code}

    def _json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--profile-root", default="/data/browser_profiles")
    parser.add_argument("--audit-path", default="/data/browser_audit.jsonl")
    parser.add_argument("--timeout-ms", type=int, default=30_000)
    args = parser.parse_args()
    limits = BrowserLimits(operation_timeout_ms=max(1_000, min(args.timeout_ms, 60_000)))
    Handler.state = BrowserState(
        Path(args.profile_root),
        limits=limits,
        profile_policy=_profile_policy(os.environ.get("BRIGADE_BROWSER_PROFILE_POLICY")),
        audit_path=Path(args.audit_path),
    )
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.request_queue_size = limits.max_queue
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
