"""Bounded, tool-only Model Context Protocol client support.

OpenBrigade intentionally implements a small MCP subset.  This module owns the
wire lifecycle and configuration boundary so callers never need to handle
credentials, raw protocol messages, or unbounded tool results.
"""

from __future__ import annotations

import json
import os
import re
import select
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26")
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_ARGUMENT_BYTES = 65_536
DEFAULT_RESULT_BYTES = 131_072
DEFAULT_RATE_BUDGET_PER_MINUTE = 60
MAX_MESSAGE_BYTES = 1_048_576
_SERVER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SECRET_REF_RE = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")


class MCPError(RuntimeError):
    """A local, safe-to-display MCP failure."""


class MCPPolicyError(MCPError):
    """A server configuration or call policy rejected an operation."""


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    transport: str
    command: list[str] | None = None
    url: str | None = None
    enabled: bool = True
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    server_id: str | None = None
    allowed_principals: tuple[str, ...] = ("*",)
    allowed_tools: tuple[str, ...] = ("*",)
    max_argument_bytes: int = DEFAULT_ARGUMENT_BYTES
    max_result_bytes: int = DEFAULT_RESULT_BYTES
    rate_budget_per_minute: int = DEFAULT_RATE_BUDGET_PER_MINUTE
    retry_attempts: int = 1
    credential_ref: str | None = None

    @property
    def id(self) -> str:
        # The fallback preserves programmatic construction from the initial
        # slice; file-based configuration must declare a stable id explicitly.
        return self.server_id or _stable_server_id(self.name)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MCPServerConfig:
        if not isinstance(data, dict):
            raise ValueError("MCP server entry must be an object")
        allowed_fields = {
            "id",
            "name",
            "transport",
            "command",
            "url",
            "enabled",
            "timeout_seconds",
            "allowed_principals",
            "allowed_tools",
            "max_argument_bytes",
            "max_result_bytes",
            "rate_budget_per_minute",
            "retry_attempts",
            "credential_ref",
        }
        unknown_fields = sorted(set(data).difference(allowed_fields))
        if unknown_fields:
            raise ValueError(
                "MCP server configuration has unsupported fields: " + ", ".join(unknown_fields)
            )
        server_id = _required_server_id(data.get("id"))
        name = _required_text(data.get("name"), "name")
        transport = _required_text(data.get("transport"), "transport").lower()
        if transport not in {"stdio", "http"}:
            raise ValueError(f"MCP server {server_id} has unsupported transport {transport!r}")
        command = _string_list(data.get("command"), "command")
        url = _optional_text(data.get("url"), "url")
        if transport == "stdio" and not command:
            raise ValueError(f"MCP server {server_id} requires a non-empty command")
        if transport == "http" and not (url and url.startswith(("http://", "https://"))):
            raise ValueError(f"MCP server {server_id} requires an HTTP(S) url")
        if transport == "stdio" and url:
            raise ValueError(f"MCP server {server_id} cannot set url for stdio")
        if transport == "http" and command:
            raise ValueError(f"MCP server {server_id} cannot set command for http")
        credential_ref = _optional_text(data.get("credential_ref"), "credential_ref")
        if credential_ref and not _SECRET_REF_RE.fullmatch(credential_ref):
            raise ValueError(f"MCP server {server_id} has an invalid credential_ref")
        return cls(
            name=name,
            transport=transport,
            command=command or None,
            url=url,
            enabled=_strict_bool(data.get("enabled", True), "enabled"),
            timeout_seconds=_bounded_int(
                data.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS), "timeout_seconds", 1, 60
            ),
            server_id=server_id,
            allowed_principals=tuple(
                _nonempty_string_list(data.get("allowed_principals"), "allowed_principals")
            ),
            allowed_tools=tuple(_nonempty_string_list(data.get("allowed_tools"), "allowed_tools")),
            max_argument_bytes=_bounded_int(
                data.get("max_argument_bytes", DEFAULT_ARGUMENT_BYTES),
                "max_argument_bytes",
                1,
                MAX_MESSAGE_BYTES,
            ),
            max_result_bytes=_bounded_int(
                data.get("max_result_bytes", DEFAULT_RESULT_BYTES),
                "max_result_bytes",
                1,
                MAX_MESSAGE_BYTES,
            ),
            rate_budget_per_minute=_bounded_int(
                data.get("rate_budget_per_minute", DEFAULT_RATE_BUDGET_PER_MINUTE),
                "rate_budget_per_minute",
                1,
                10_000,
            ),
            retry_attempts=_bounded_int(data.get("retry_attempts", 1), "retry_attempts", 0, 2),
            credential_ref=credential_ref,
        )

    def allows_tool(self, tool_name: str) -> bool:
        return "*" in self.allowed_tools or tool_name in self.allowed_tools

    def allows_principal(self, principal: str | None, team_id: str | None = None) -> bool:
        if "*" in self.allowed_principals:
            return True
        candidates = {principal or ""}
        if principal:
            candidates.add(f"agent:{principal}")
        if team_id:
            candidates.add(f"team:{team_id}")
        return bool(candidates.intersection(self.allowed_principals))


@dataclass(frozen=True)
class MCPCallResult:
    ok: bool
    output: str
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class MCPTool:
    server: MCPServerConfig
    name: str
    description: str
    argument_schema: dict[str, Any]

    @property
    def registry_name(self) -> str:
        return f"mcp__{_tool_name_part(self.server.id)}__{_tool_name_part(self.name)}"


def _tool_name_part(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
    return safe or "unnamed"


def configured_servers(data_dir: Path) -> list[MCPServerConfig]:
    """Load a complete, validated configuration without accepting secrets inline."""
    raw = os.environ.get("BRIGADE_MCP_SERVERS_JSON")
    if raw:
        payload = json.loads(raw)
    else:
        path = Path(os.environ.get("BRIGADE_MCP_CONFIG", data_dir / "mcp_servers.json"))
        if not path.exists():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
    servers = payload.get("servers") if isinstance(payload, dict) else payload
    if not isinstance(servers, list):
        raise ValueError("MCP configuration must be a server list or an object with servers")
    parsed = [MCPServerConfig.from_dict(item) for item in servers]
    ids = [server.id for server in parsed]
    if len(ids) != len(set(ids)):
        raise ValueError("MCP configuration contains duplicate server ids")
    names = [server.name for server in parsed]
    if len(names) != len(set(names)):
        raise ValueError("MCP configuration contains duplicate server names")
    return parsed


class MCPClient:
    """A managed single-server lifecycle with correlated JSON-RPC requests."""

    def __init__(self, server: MCPServerConfig, *, data_dir: Path | None = None) -> None:
        self.server = server
        self.data_dir = data_dir or Path(os.environ.get("BRIGADE_DATA_DIR", ".brigade"))
        self._process: subprocess.Popen[str] | None = None
        self._next_request_id = 1
        self._initialized = False
        self._calls_in_window = 0
        self._window_started = time.monotonic()

    def __enter__(self) -> MCPClient:
        self.initialize()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def initialize(self) -> dict[str, Any]:
        if self._initialized:
            return {"protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[-1]}
        result = self.request(
            "initialize",
            {
                "protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[-1],
                "capabilities": {},
                "clientInfo": {"name": "openbrigade", "version": "0.9"},
            },
            initialize=True,
        )
        version = str(result.get("protocolVersion") or "")
        if version not in SUPPORTED_PROTOCOL_VERSIONS:
            self.close()
            raise MCPError("MCP server negotiated an unsupported protocol revision")
        self.notify("notifications/initialized", {})
        self._initialized = True
        return result

    def discover_tools(self) -> list[MCPTool]:
        payload = self.request("tools/list", {})
        tools = payload.get("tools") or []
        if not isinstance(tools, list):
            raise MCPError("MCP server returned an invalid tools/list result")
        discovered: list[MCPTool] = []
        for tool in tools:
            if not isinstance(tool, dict) or not tool.get("name"):
                continue
            name = str(tool["name"])
            if self.server.allows_tool(name):
                input_schema = tool.get("inputSchema")
                discovered.append(
                    MCPTool(
                        server=self.server,
                        name=name,
                        description=str(tool.get("description") or ""),
                        argument_schema=dict((input_schema or {}).get("properties") or {})
                        if isinstance(input_schema, dict)
                        else {},
                    )
                )
        return discovered

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> MCPCallResult:
        if not self.server.allows_tool(tool_name):
            raise MCPPolicyError("MCP tool is not allowed by server policy")
        _ensure_size(arguments, self.server.max_argument_bytes, "MCP tool arguments")
        payload = self.request("tools/call", {"name": tool_name, "arguments": arguments})
        output, metadata = normalize_tool_result(payload, self.server.max_result_bytes)
        metadata.update({"server_id": self.server.id, "server": self.server.name})
        if payload.get("isError"):
            return MCPCallResult(False, output, metadata)
        return MCPCallResult(True, output, metadata)

    def request(
        self, method: str, params: dict[str, Any], *, initialize: bool = False
    ) -> dict[str, Any]:
        if not initialize and not self._initialized:
            self.initialize()
        self._check_rate_budget()
        last_error: MCPError | None = None
        for attempt in range(self.server.retry_attempts + 1):
            request_id = self._new_request_id()
            try:
                payload = self._rpc(
                    {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
                )
                return _rpc_result(payload, request_id)
            except MCPError as exc:
                last_error = exc
                if attempt >= self.server.retry_attempts:
                    break
                self._reconnect(initialize=method != "initialize")
        raise last_error or MCPError("MCP request failed")

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._rpc({"jsonrpc": "2.0", "method": method, "params": params}, notification=True)

    def close(self) -> None:
        self._initialized = False
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=min(self.server.timeout_seconds, 5))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def _reconnect(self, *, initialize: bool = True) -> None:
        self.close()
        if initialize:
            self.initialize()

    def _new_request_id(self) -> int:
        value = self._next_request_id
        self._next_request_id += 1
        return value

    def _check_rate_budget(self) -> None:
        now = time.monotonic()
        if now - self._window_started >= 60:
            self._window_started = now
            self._calls_in_window = 0
        if self._calls_in_window >= self.server.rate_budget_per_minute:
            raise MCPPolicyError("MCP server rate budget exhausted")
        self._calls_in_window += 1

    def _rpc(self, envelope: dict[str, Any], *, notification: bool = False) -> dict[str, Any]:
        _ensure_size(envelope, MAX_MESSAGE_BYTES, "MCP request")
        if self.server.transport == "http":
            return self._http_rpc(envelope, notification=notification)
        if self.server.transport == "stdio":
            return self._stdio_rpc(envelope, notification=notification)
        raise MCPError("MCP server has unsupported transport")

    def _http_rpc(self, envelope: dict[str, Any], *, notification: bool) -> dict[str, Any]:
        if not self.server.url:
            raise MCPError("MCP server is missing its HTTP url")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        headers.update(resolve_credential_headers(self.server, self.data_dir))
        request = urllib.request.Request(
            self.server.url,
            data=json.dumps(envelope, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.server.timeout_seconds) as response:
                if notification:
                    return {}
                body = response.read(MAX_MESSAGE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise MCPError(f"MCP HTTP request failed with status {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MCPError("MCP HTTP request failed") from exc
        if len(body) > MAX_MESSAGE_BYTES:
            raise MCPError("MCP response exceeded the message limit")
        return _json_object(body, "MCP HTTP response")

    def _stdio_rpc(self, envelope: dict[str, Any], *, notification: bool) -> dict[str, Any]:
        if not self.server.command:
            raise MCPError("MCP server is missing its stdio command")
        if self._process is None or self._process.poll() is not None:
            try:
                self._process = subprocess.Popen(
                    self.server.command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
            except OSError as exc:
                raise MCPError("MCP stdio server could not start") from exc
        assert self._process.stdin is not None and self._process.stdout is not None
        try:
            self._process.stdin.write(json.dumps(envelope, separators=(",", ":")) + "\n")
            self._process.stdin.flush()
        except OSError as exc:
            raise MCPError("MCP stdio server disconnected") from exc
        if notification:
            return {}
        ready, _, _ = select.select([self._process.stdout], [], [], self.server.timeout_seconds)
        if not ready:
            raise MCPError("MCP stdio request timed out")
        line = self._process.stdout.readline()
        if not line:
            raise MCPError("MCP stdio server disconnected")
        return _json_object(line.encode("utf-8"), "MCP stdio response")


def discover_tools(server: MCPServerConfig, *, data_dir: Path | None = None) -> list[MCPTool]:
    if not server.enabled:
        return []
    with MCPClient(server, data_dir=data_dir) as client:
        return client.discover_tools()


def call_tool(
    server: MCPServerConfig,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    data_dir: Path | None = None,
) -> MCPCallResult:
    if not server.enabled:
        return MCPCallResult(False, "MCP server is disabled", {"server_id": server.id})
    try:
        with MCPClient(server, data_dir=data_dir) as client:
            return client.call_tool(tool_name, arguments)
    except MCPError as exc:
        return MCPCallResult(False, str(exc), {"server_id": server.id, "server": server.name})


def normalize_tool_result(payload: dict[str, Any], limit: int) -> tuple[str, dict[str, Any]]:
    """Make every MCP content form a bounded, non-secret agent observation."""
    parts: list[str] = []
    content_types: list[str] = []
    for item in payload.get("content") or []:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "unknown")
        content_types.append(item_type)
        if item_type == "text":
            parts.append(str(item.get("text") or ""))
        elif item_type == "resource":
            resource = item.get("resource") or {}
            parts.append("[MCP resource content omitted from tool-only observation]")
            if isinstance(resource, dict) and resource.get("uri"):
                parts.append(f"resource: {resource['uri']}")
        else:
            # Structured/image/audio/unknown content is retained only as a
            # bounded shape, never as an arbitrary raw payload in context.
            parts.append(f"[MCP {item_type} content omitted]")
    if not parts:
        parts.append("[MCP tool returned no displayable content]")
    text = "\n".join(parts)
    encoded = text.encode("utf-8")
    truncated = len(encoded) > limit
    if truncated:
        marker = b"\n[truncated]"
        if limit <= len(marker):
            text = marker[:limit].decode("utf-8", errors="ignore")
        else:
            text = (
                encoded[: limit - len(marker)].decode("utf-8", errors="ignore")
                + marker.decode("utf-8")
            )
    return text, {
        "content_types": sorted(set(content_types)),
        "result_truncated": truncated,
        "audit_ref": f"mcp:{uuid4()}",
    }


def resolve_credential_headers(server: MCPServerConfig, data_dir: Path) -> dict[str, str]:
    """Resolve an optional header-only credential reference outside config.

    The referenced JSON document is ``{"headers": {"Authorization": "..."}}``
    below ``BRIGADE_SECRET_STORE_PATH`` or ``<data_dir>/secrets``.  The value
    never appears in client errors, health records, or returned metadata.
    """
    if not server.credential_ref:
        return {}
    root = Path(os.environ.get("BRIGADE_SECRET_STORE_PATH", data_dir / "secrets"))
    path = root.joinpath(*server.credential_ref.split("/")).with_suffix(".json")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MCPError("MCP credential reference is unavailable") from exc
    headers = payload.get("headers") if isinstance(payload, dict) else None
    if not isinstance(headers, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in headers.items()
    ):
        raise MCPError("MCP credential reference is invalid")
    return dict(headers)


def server_health(data_dir: Path, servers: list[MCPServerConfig]) -> list[dict[str, Any]]:
    """Stable non-secret status used by the Phase 1 CLI surface."""
    path = data_dir / "mcp_health.json"
    try:
        saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        saved = {}
    return [
        {
            "id": server.id,
            "name": server.name,
            "transport": server.transport,
            "enabled": server.enabled,
            "credential_ref_configured": bool(server.credential_ref),
            "allowed_principals": list(server.allowed_principals),
            "allowed_tools": list(server.allowed_tools),
            "limits": {
                "timeout_seconds": server.timeout_seconds,
                "max_argument_bytes": server.max_argument_bytes,
                "max_result_bytes": server.max_result_bytes,
                "rate_budget_per_minute": server.rate_budget_per_minute,
            },
            "health": dict(saved.get(server.id) or {"state": "unknown"}),
        }
        for server in servers
    ]


def record_server_health(
    data_dir: Path, server: MCPServerConfig, state: str, reason: str | None = None
) -> None:
    """Persist only a sanitized transition; config and diagnostic output stay secret-free."""
    path = data_dir / "mcp_health.json"
    try:
        records = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        records = {}
    record = {"state": state, "updated_at": int(time.time())}
    if reason:
        record["last_failure_reason"] = redact_failure_reason(reason)
    elif state == "healthy":
        record["last_failure_reason"] = None
    records[server.id] = record
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _rpc_result(payload: dict[str, Any], request_id: int) -> dict[str, Any]:
    if payload.get("jsonrpc") != "2.0" or payload.get("id") != request_id:
        raise MCPError("MCP response did not match its request")
    if payload.get("error"):
        raise MCPError("MCP server returned a protocol error")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise MCPError("MCP server returned an invalid result")
    return result


def _json_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MCPError(f"{label} was not valid JSON") from exc
    if not isinstance(value, dict):
        raise MCPError(f"{label} was not a JSON object")
    return value


def _ensure_size(value: object, limit: int, label: str) -> None:
    try:
        size = len(json.dumps(value, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise MCPPolicyError(f"{label} must be JSON-serializable") from exc
    if size > limit:
        raise MCPPolicyError(f"{label} exceeded its configured size limit")


def _stable_server_id(name: str) -> str:
    value = _tool_name_part(name).lower().replace("_", "-")
    if not value or not value[0].isalpha():
        value = f"server-{value}".strip("-")
    return value[:64]


def _required_server_id(value: object) -> str:
    server_id = _required_text(value, "id").lower()
    if not _SERVER_ID_RE.fullmatch(server_id):
        raise ValueError(
            "MCP server id must be 1-64 lowercase letters, digits, _ or -, starting with a letter"
        )
    return server_id


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"MCP server {field} is required")
    return value.strip()


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field)


def _string_list(value: object, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"MCP server {field} must be a list of non-empty strings")
    return list(value)


def _nonempty_string_list(value: object, field: str) -> list[str]:
    values = _string_list(value, field)
    if not values:
        raise ValueError(f"MCP server {field} must not be empty")
    return values


def _bounded_int(value: object, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"MCP server {field} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"MCP server {field} must be an integer") from exc
    if not minimum <= number <= maximum:
        raise ValueError(f"MCP server {field} must be between {minimum} and {maximum}")
    return number


def _strict_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"MCP server {field} must be a boolean")
    return value


def _safe_reason(reason: str) -> str:
    return re.sub(
        r"(?i)(authorization|token|secret|cookie|password)=?[^\s,]+", r"\1=***redacted***", reason
    )[:240]


def redact_failure_reason(reason: str) -> str:
    """Return a bounded diagnostic that cannot carry common credential forms."""
    return _safe_reason(reason)
