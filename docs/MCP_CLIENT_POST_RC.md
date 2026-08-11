# Governed MCP Client Contract

OpenBrigade consumes a deliberately narrow, tool-only subset of the Model Context Protocol (MCP). It is an MCP client, not an MCP server. The contract is for bounded operator-configured integrations, including community Google Workspace servers; it is not a general-purpose remote-agent transport.

## Supported subset

- Protocol revisions: `2024-11-05` and `2025-03-26`, negotiated during `initialize`; `notifications/initialized` is sent before tool use.
- Transports: local stdio and request/response HTTP(S) JSON-RPC. HTTP streaming and SSE are unsupported.
- Methods: lifecycle initialization plus `tools/list` and `tools/call` only.
- Tool content: text becomes an agent observation. Other content is represented by a bounded type marker; resources are not loaded into agent context.
- Lifecycle: stdio processes persist for a managed client session, terminate at close, and reconnect once after a safe failure. HTTP has one request/response connection per call and no server-side session state.

Resources, prompts, sampling, elicitation, streaming, roots, logging callbacks, server-to-client requests, and arbitrary notifications are deferred. OpenBrigade does not expose an MCP server endpoint.

## Configuration and policy

`BRIGADE_MCP_CONFIG` points to a JSON document (or `BRIGADE_MCP_SERVERS_JSON` contains the equivalent JSON). Every file-configured server must have a stable lowercase `id`, a display `name`, and this complete policy. Duplicate IDs or names, inline credentials, ambiguous transports, and invalid limits are rejected at load time.

```json
{
  "servers": [{
    "id": "workspace",
    "name": "Workspace tools",
    "transport": "stdio",
    "command": ["npx", "-y", "example-mcp-server"],
    "enabled": false,
    "allowed_principals": ["agent:exec", "team:research"],
    "allowed_tools": ["search_drive", "get_document"],
    "timeout_seconds": 20,
    "max_argument_bytes": 65536,
    "max_result_bytes": 131072,
    "rate_budget_per_minute": 60,
    "retry_attempts": 1,
    "credential_ref": "mcp/workspace"
  }]
}
```

For HTTP use `url` in place of `command`. `allowed_principals` accepts `agent:<id>`, `team:<id>`, or an explicit `*`; `allowed_tools` accepts named tools or `*`. These wildcards are deliberate policy decisions, not implicit defaults in file config. Messages cannot exceed 1 MiB; timeout is 1--60 seconds; results and arguments are bounded by their configuration (at most 1 MiB); retries are limited to two reconnect attempts.

`credential_ref` is an identifier only. It resolves to `<BRIGADE_SECRET_STORE_PATH or data-dir/secrets>/<ref>.json`, whose document must contain header values under `headers`. Header values never appear in configuration, agent context, observations, health output, or error messages. A missing or invalid reference fails only that server locally.

## Operation and diagnostics

Every discovery and tool use initializes the session, correlates JSON-RPC IDs, and writes the latest non-secret server state to `mcp_health.json` in the data directory. A tool call also creates an argument-free provenance audit record: success/failure, server ID, tool name, principal, policy outcome, and a safe failure reason. Arguments, raw results, headers, and credentials are excluded. Failures create an operator alert without removing built-in tools or unrelated MCP servers.

An authorized operator can inspect current policy and the latest state without access to a server secret:

```bash
brigade mcp status
```

This is an inspect-only Phase 1 surface. Config editing, enable/disable, test actions, and a GUI control plane are Phase 5 work.

## Validation boundary

The offline suite covers initialization, discovery, calls, reconnect, shutdown, configuration policy, missing credentials, oversized messages/results, malformed responses, rate limits, and principal/tool denial for stdio and HTTP fixture transports. To exercise a real non-production, credential-free stdio server, set `BRIGADE_RUN_MCP_INTEGRATION=1`, `BRIGADE_MCP_INTEGRATION_COMMAND` (a JSON argv list), `BRIGADE_MCP_INTEGRATION_TOOL`, and optional `BRIGADE_MCP_INTEGRATION_ARGUMENTS`, then run `python3 -m pytest tests/test_mcp_client.py -k nonproduction`. It is opt-in and must not use production credentials.
