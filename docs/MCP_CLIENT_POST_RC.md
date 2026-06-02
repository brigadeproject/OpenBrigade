# MCP Client Post-RC Milestone

MCP client support is the first post-RC build. It is not a shipped RC feature.

## Goal

Let OpenBrigade consume external MCP servers as agent tools, including community Google Workspace
servers for Gmail, Drive, and Calendar. This avoids bespoke Google integrations and gives operators
one extensibility path for third-party tools.

## Shape

- Add an MCP client module alongside `brigade/tools.py`.
- Store configured MCP servers in settings/config with a name, command or URL, enabled flag, and
  optional environment-variable names for credentials.
- On startup, discover each enabled server's tools and register them into `ToolRegistry` with a
  stable `mcp.<server>.<tool>` name.
- Keep the existing tool sandbox posture: tool arguments are structured JSON, failures become tool
  observations, and unavailable servers do not hide core built-in tools.
- Add CLI and GUI flows to list, add, disable, and test MCP servers.

## Acceptance

- A configured local MCP server exposes at least one tool in the agent prompt.
- An agent can call an MCP tool and receives the result as a normal tool observation.
- Missing credentials or failed server startup marks only that MCP server unavailable and records an
  alert.
- README wording changes from "roadmap" to "available" only after the above is validated.
