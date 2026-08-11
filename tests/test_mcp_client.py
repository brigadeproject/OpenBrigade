from __future__ import annotations

import io
import json
import os
import sys
import urllib.error

import pytest

from brigade.cli import main
from brigade.mcp_client import (
    MCPClient,
    MCPError,
    MCPPolicyError,
    MCPServerConfig,
    configured_servers,
    normalize_tool_result,
    resolve_credential_headers,
)
from brigade.schemas import Agent
from brigade.state import JsonStateStore
from brigade.tools import ToolContext, _mcp_handler


def _config(**overrides):
    config = {
        "id": "fixture",
        "name": "Fixture server",
        "transport": "http",
        "url": "https://mcp.example.test/rpc",
        "enabled": True,
        "allowed_principals": ["agent:researcher"],
        "allowed_tools": ["echo"],
    }
    config.update(overrides)
    return MCPServerConfig.from_dict(config)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_http_lifecycle_initializes_discovers_calls_and_normalizes(monkeypatch, tmp_path):
    requests = []

    def fake_urlopen(request, timeout):
        del timeout
        request_payload = json.loads(request.data)
        requests.append(request_payload)
        method = request_payload["method"]
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {}}
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo",
                        "inputSchema": {"properties": {"text": {}}},
                    },
                    {"name": "blocked", "description": "Blocked"},
                ]
            }
        else:
            result = {
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "image", "data": "never context"},
                ]
            }
        if "id" not in request_payload:
            return _Response(b"")
        return _Response(
            json.dumps({"jsonrpc": "2.0", "id": request_payload["id"], "result": result}).encode()
        )

    monkeypatch.setattr("brigade.mcp_client.urllib.request.urlopen", fake_urlopen)
    client = MCPClient(_config(), data_dir=tmp_path)
    with client:
        tools = client.discover_tools()
        result = client.call_tool("echo", {"text": "hello"})

    assert [tool.name for tool in tools] == ["echo"]
    assert result.ok and result.output == "hello\n[MCP image content omitted]"
    assert result.metadata["content_types"] == ["image", "text"]
    assert [request["method"] for request in requests] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ]
    assert [request.get("id") for request in requests if "id" in request] == [1, 2, 3]


def test_stdio_lifecycle_fixture_initializes_discovers_calls_and_shuts_down(tmp_path):
    fixture = tmp_path / "server.py"
    fixture.write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "  request=json.loads(line)\n"
        "  if 'id' not in request: continue\n"
        "  method=request['method']\n"
        "  result=({'protocolVersion':'2024-11-05','capabilities':{}} if method=='initialize' else "
        "{'tools':[{'name':'echo','inputSchema':{'properties':{}}}]} if method=='tools/list' else "
        "{'content':[{'type':'text','text':'stdio ok'}]})\n"
        "  print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}), flush=True)\n",
        encoding="utf-8",
    )
    server = _config(
        transport="stdio",
        command=[sys.executable, str(fixture)],
        url=None,
    )
    client = MCPClient(server, data_dir=tmp_path)
    client.initialize()
    assert [tool.name for tool in client.discover_tools()] == ["echo"]
    assert client.call_tool("echo", {}).output == "stdio ok"
    process = client._process
    client.close()
    assert process is not None and process.poll() is not None


def test_request_reconnects_once_and_correlates_responses(monkeypatch, tmp_path):
    client = MCPClient(_config(), data_dir=tmp_path)
    client._initialized = True
    calls = []
    reconnects = []

    def fake_rpc(envelope, notification=False):
        del notification
        calls.append(envelope)
        if len(calls) == 1:
            raise MCPError("temporary disconnect")
        return {"jsonrpc": "2.0", "id": envelope["id"], "result": {"tools": []}}

    monkeypatch.setattr(client, "_rpc", fake_rpc)
    monkeypatch.setattr(client, "_reconnect", lambda **_kwargs: reconnects.append(True))
    assert client.request("tools/list", {}) == {"tools": []}
    assert reconnects == [True]
    assert [item["id"] for item in calls] == [1, 2]


def test_local_rejections_cover_config_policy_size_and_malformed_protocol(tmp_path):
    with pytest.raises(ValueError, match="unsupported fields"):
        MCPServerConfig.from_dict({**_config_dict(), "headers": {"Authorization": "secret"}})
    with pytest.raises(ValueError, match="allowed_principals"):
        MCPServerConfig.from_dict({**_config_dict(), "allowed_principals": []})

    server = _config(max_argument_bytes=5)
    client = MCPClient(server, data_dir=tmp_path)
    with pytest.raises(MCPPolicyError, match="arguments"):
        client.call_tool("echo", {"too": "large"})
    with pytest.raises(MCPPolicyError, match="not allowed"):
        client.call_tool("blocked", {})
    with pytest.raises(MCPError, match="did not match"):
        # This validates the protocol error before any content reaches an observation.
        from brigade.mcp_client import _rpc_result

        _rpc_result({"jsonrpc": "2.0", "id": 9, "result": {}}, 1)


def test_missing_credential_reference_is_safe_and_never_echoes_path(tmp_path):
    server = _config(credential_ref="mcp/missing")
    with pytest.raises(MCPError, match="credential reference is unavailable") as exc:
        resolve_credential_headers(server, tmp_path)
    assert "missing" not in str(exc.value)


def test_slow_http_server_fails_locally_without_raw_transport_detail(monkeypatch, tmp_path):
    def timeout(*_args, **_kwargs):
        raise urllib.error.URLError("socket timeout with token=not-for-context")

    monkeypatch.setattr("brigade.mcp_client.urllib.request.urlopen", timeout)
    result = MCPClient(_config(), data_dir=tmp_path)
    with pytest.raises(MCPError, match="MCP HTTP request failed") as exc:
        result.initialize()
    assert "token" not in str(exc.value)


def test_oversized_tool_result_is_bounded_before_it_reaches_context():
    text, metadata = normalize_tool_result(
        {"content": [{"type": "text", "text": "x" * 100}]}, 20
    )
    assert text.startswith("x" * 8)
    assert text.endswith("[truncated]")
    assert len(text.encode("utf-8")) <= 20
    assert metadata["result_truncated"] is True


def test_per_server_rate_budget_rejects_excess_calls_locally(monkeypatch, tmp_path):
    client = MCPClient(_config(rate_budget_per_minute=1), data_dir=tmp_path)
    client._initialized = True
    monkeypatch.setattr(
        client,
        "_rpc",
        lambda envelope, notification=False: {
            "jsonrpc": "2.0",
            "id": envelope["id"],
            "result": {"tools": []},
        },
    )
    assert client.request("tools/list", {}) == {"tools": []}
    with pytest.raises(MCPPolicyError, match="rate budget"):
        client.request("tools/list", {})


def test_configured_servers_requires_complete_policy(tmp_path, monkeypatch):
    path = tmp_path / "mcp_servers.json"
    path.write_text(json.dumps({"servers": [_config_dict()]}), encoding="utf-8")
    monkeypatch.setenv("BRIGADE_MCP_CONFIG", str(path))
    assert configured_servers(tmp_path)[0].id == "fixture"
    path.write_text(
        json.dumps({"servers": [{"name": "unsafe", "transport": "stdio", "command": ["x"]}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="id is required"):
        configured_servers(tmp_path)


def test_cli_status_exposes_only_non_secret_mcp_health(tmp_path, monkeypatch, capsys):
    config = tmp_path / "servers.json"
    config.write_text(json.dumps({"servers": [_config_dict()]}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRIGADE_MCP_CONFIG", str(config))

    assert main(["mcp", "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    server = payload["servers"][0]
    assert server["id"] == "fixture"
    assert server["credential_ref_configured"] is False
    assert "url" not in server and "command" not in server


def test_policy_denial_records_redacted_audit_health_and_alert(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("BRIGADE_DATA_DIR", str(data_dir))
    store = JsonStateStore(tmp_path / "state.json")
    store.data_dir = data_dir
    context = ToolContext(
        agent=Agent("unauthorized", "UNAUTHORIZED", "workspace-unauthorized"),
        assignment=None,
        store=store,
    )
    server = _config()
    result = _mcp_handler(server, "echo")(context, {"text": "unused"})

    assert result.ok is False
    assert store.alerts() == [
        "MCP server fixture mcp_tool_failure: MCP server policy denies this principal"
    ]
    audit = store.provenance_records()[0]
    assert audit["principal"] == "unauthorized"
    assert audit["policy_outcome"] == "denied_or_failed"
    health = json.loads((data_dir / "mcp_health.json").read_text(encoding="utf-8"))
    assert health["fixture"]["state"] == "unhealthy"


@pytest.mark.skipif(
    not os.environ.get("BRIGADE_RUN_MCP_INTEGRATION"),
    reason="set BRIGADE_RUN_MCP_INTEGRATION=1 for a non-production MCP server",
)
def test_opt_in_nonproduction_stdio_mcp_server(tmp_path):
    """Run only with a locally approved, credential-free MCP fixture server.

    Required variables: BRIGADE_MCP_INTEGRATION_COMMAND (a JSON argv list),
    BRIGADE_MCP_INTEGRATION_TOOL, and optional BRIGADE_MCP_INTEGRATION_ARGUMENTS.
    """
    command = json.loads(os.environ["BRIGADE_MCP_INTEGRATION_COMMAND"])
    tool_name = os.environ["BRIGADE_MCP_INTEGRATION_TOOL"]
    arguments = json.loads(os.environ.get("BRIGADE_MCP_INTEGRATION_ARGUMENTS", "{}"))
    server = MCPServerConfig.from_dict(
        {
            "id": "integration",
            "name": "Non-production integration fixture",
            "transport": "stdio",
            "command": command,
            "allowed_principals": ["*"],
            "allowed_tools": [tool_name],
        }
    )
    with MCPClient(server, data_dir=tmp_path) as client:
        assert tool_name in [item.name for item in client.discover_tools()]
        assert client.call_tool(tool_name, arguments).ok


def _config_dict():
    return {
        "id": "fixture",
        "name": "Fixture server",
        "transport": "http",
        "url": "https://mcp.example.test/rpc",
        "allowed_principals": ["agent:researcher"],
        "allowed_tools": ["echo"],
    }
