from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from uuid import uuid4

from brigade.evidence import classify_source, source_selection_hint
from brigade.governance import (
    build_policy_change_proposal,
    is_governing_workspace_path,
    normalize_workspace_relative_path,
)
from brigade.knowledge import (
    extract_document_text,
    html_to_text,
    ingest_text,
    pdf_text_with_page_map,
    store_ingest_result,
)
from brigade.mcp_client import call_tool as call_mcp_tool
from brigade.mcp_client import (
    configured_servers,
    discover_tools,
    record_server_health,
    redact_failure_reason,
)
from brigade.research import (
    call_browser_worker,
    record_search_health,
    search_with_retry,
    searxng_search,
    source_map,
)
from brigade.research_control import reconcile_research_alert
from brigade.schemas import Agent, Assignment, AssignmentKind, AssignmentStatus, Priority
from brigade.store import StateStore
from brigade.time import utc_now_iso


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    argument_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    output: str
    metadata: dict[str, Any] | None = None

    def to_observation(self, tool_name: str) -> dict[str, Any]:
        return {
            "tool": tool_name,
            "ok": self.ok,
            "output": self.output,
            "metadata": self.metadata or {},
        }


@dataclass(frozen=True)
class ToolContext:
    agent: Agent
    assignment: Assignment
    store: StateStore

    @property
    def workspace(self) -> Path:
        return self.store.data_dir / self.agent.workspace_path


ToolHandler = Callable[[ToolContext, dict[str, Any]], ToolResult]
MAX_DELEGATION_DEPTH = 2
MAX_CHILDREN_PER_ASSIGNMENT = 5
MAX_CREATE_SUBTASKS = 5
WEB_SEARCH_DEFAULT_URL = "https://duckduckgo.com/html/"
WEB_SEARCH_MAX_QUERY_CHARS = 200
WEB_SEARCH_MAX_RESULTS = 8
WEB_SEARCH_READ_CAP = 120_000
WEB_FETCH_PDF_MAX_BYTES = 100_000_000
WEB_FETCH_SAVE_MAX_CHARS = 2_000_000


def _research_storage_root(store: StateStore) -> Path:
    """Local default or an operator-mounted NAS path for retained sources."""
    configured = os.environ.get("BRIGADE_RESEARCH_STORAGE_PATH", "").strip()
    root = Path(configured) if configured else store.data_dir / "knowledge"
    if not root.is_absolute() and configured:
        raise ValueError("BRIGADE_RESEARCH_STORAGE_PATH must be an absolute mounted path")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _research_component_enabled(context: ToolContext, component: str) -> bool:
    """Live policy switches default to enabled until an operator changes them."""
    if context is None:
        return True
    key = f"research_{component}_enabled"
    try:
        return bool((context.store.runtime_overrides() or {}).get(key, True))
    except RuntimeError:
        # An unavailable runtime store must not turn an intentional external
        # denial into an accidental bypass; normal deployments have a durable
        # state store, while offline tests retain the safe default.
        return True


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolSpec, ToolHandler]] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        self._tools[spec.name] = (spec, handler)

    def specs(self) -> list[ToolSpec]:
        return [item[0] for item in self._tools.values()]

    def restricted(self, allowed_names: set[str]) -> ToolRegistry:
        restricted = ToolRegistry()
        for name, (spec, handler) in self._tools.items():
            if name in allowed_names:
                restricted.register(spec, handler)
        return restricted

    def extend(self, other: ToolRegistry) -> None:
        self._tools.update(other._tools)

    def execute(self, name: str, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        item = self._tools.get(name)
        if item is None:
            return ToolResult(False, f"unknown tool: {name}")
        _, handler = item
        try:
            return handler(context, arguments)
        except Exception as exc:
            return ToolResult(False, str(exc))


def native_tool_specs(registry: ToolRegistry) -> list[dict[str, Any]]:
    """Convert registry specs to the OpenAI/Ollama function-tool format."""
    specs = []
    for spec in registry.specs():
        properties = {
            name: {"description": description} for name, description in spec.argument_schema.items()
        }
        required = [
            name
            for name, description in spec.argument_schema.items()
            if "optional" not in str(description).lower()
        ]
        specs.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            }
        )
    return specs


def default_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="list_files",
            description=(
                "List files under your private workspace, or under the "
                "team-shared workspace when the path starts with shared/."
            ),
            argument_schema={
                "path": ("optional relative path; prefix shared/ for the team-shared workspace")
            },
        ),
        _list_files,
    )
    registry.register(
        ToolSpec(
            name="read_file",
            description=(
                "Read a UTF-8 text file from your private workspace, or from "
                "the team-shared workspace when the path starts with shared/."
            ),
            argument_schema={
                "path": ("relative file path; prefix shared/ for the team-shared workspace")
            },
        ),
        _read_file,
    )
    registry.register(
        ToolSpec(
            name="write_file",
            description=(
                "Write or append UTF-8 text in your private workspace, or in "
                "the team-shared workspace when the path starts with shared/."
            ),
            argument_schema={
                "path": ("relative file path; prefix shared/ for the team-shared workspace"),
                "content": "text to write",
                "append": "optional boolean, defaults false",
            },
        ),
        _write_file,
    )
    registry.register(
        ToolSpec(
            name="shell",
            description=(
                "Run a command in the assigned agent workspace without a shell interpreter."
            ),
            argument_schema={
                "command": "array of command arguments, for example ['python', '--version']",
                "timeout_seconds": "optional integer, maximum 30",
            },
        ),
        _shell,
    )
    registry.register(
        ToolSpec(
            name="web_fetch",
            description=(
                "Fetch a small HTTP(S) text or PDF response for reference. Pass "
                "save_to_knowledge true to keep extracted text in the shared knowledge base."
            ),
            argument_schema={
                "url": "http or https URL",
                "max_chars": "optional integer",
                "save_to_knowledge": (
                    "optional boolean; store the fetched page as a knowledge document"
                ),
            },
        ),
        _web_fetch,
    )
    registry.register(
        ToolSpec(
            name="web_search",
            description=(
                "Search the public web for source URLs through the configured search backend. "
                "Returns titles, URLs, snippets, engine, and rank; use web_fetch or "
                "browser_extract with save_to_knowledge to retrieve useful results."
            ),
            argument_schema={
                "query": "search query, maximum 200 characters",
                "limit": "optional integer, maximum 8",
                "intent": "optional general|legal, defaults general",
                "jurisdiction": "optional jurisdiction for legal research",
                "save_to_knowledge": (
                    "optional boolean; store the result list and source URLs as "
                    "a knowledge document"
                ),
            },
        ),
        _web_search,
    )
    _register_browser_tools(registry)
    _register_mcp_tools(registry)
    registry.register(
        ToolSpec(
            name="delegate",
            description="Create a queued assignment for another registered agent.",
            argument_schema={
                "agent_id": "target agent id",
                "assignment": "assignment text",
                "goal_statement": "optional linked goal statement",
                "priority": "optional low|normal|high|urgent",
            },
        ),
        _delegate,
    )
    registry.register(
        ToolSpec(
            name="create_subtasks",
            description=(
                "Create bounded child assignments for registered agents, optionally "
                "linking each item to the previous child as a dependency."
            ),
            argument_schema={
                "subtasks": (
                    "array of up to 5 objects with agent_id, assignment, optional "
                    "goal_statement, priority, and depends_on_previous"
                )
            },
        ),
        _create_subtasks,
    )
    registry.register(
        ToolSpec(
            name="request_tool",
            description=(
                "Request a new workspace tool: records a tool_request proposal "
                "for approval. Never builds anything directly."
            ),
            argument_schema={
                "name": "tool name (becomes tools/<name> after approval)",
                "purpose": "what problem the tool solves",
                "spec": "expected arguments and behavior",
            },
        ),
        _request_tool,
    )
    registry.register(
        ToolSpec(
            name="request_staff_meeting",
            description=(
                "Request that your Crew Chief consider convening a Staff Meeting for a "
                "large cross-domain decision or unresolved problem. This records a request; "
                "ordinary agents cannot convene the meeting themselves."
            ),
            argument_schema={
                "request": "the decision or unresolved problem",
                "reason": "why ordinary recovery or analysis is insufficient",
                "acceptance_criteria": "optional array of suggested criteria for the chair",
            },
        ),
        _request_staff_meeting,
    )
    registry.register(
        ToolSpec(
            name="propose_policy_change",
            description=(
                "Propose a governed change to AGENTS.md, USER.md, IDENTITY.md, "
                "SOUL.md, SKILLS.md, TOOLS.md, MEMORY.md, or a canonical "
                "skills/.../SKILL.md file. Records a policy_change proposal; "
                "does not edit the file directly."
            ),
            argument_schema={
                "path": "governed relative file path in your private workspace",
                "content": "proposed full content, or appended text when append is true",
                "append": "optional boolean, defaults false",
                "purpose": "why this governing file should change",
            },
        ),
        _propose_policy_change,
    )
    registry.register(
        ToolSpec(
            name="approve_proposal",
            description=("Crew chiefs only: approve a pending proposal raised by your own team."),
            argument_schema={"proposal_id": "the proposal to approve"},
        ),
        _approve_proposal,
    )
    registry.register(
        ToolSpec(
            name="run_workspace_tool",
            description=(
                "Run an approved executable from the workspace tools/ "
                "directory through the sandboxed subprocess guard."
            ),
            argument_schema={
                "name": "tool name under tools/",
                "args": "optional array of string arguments",
            },
        ),
        _run_workspace_tool,
    )
    return registry


STAFF_MEETING_READ_ONLY_TOOLS = {
    "list_files",
    "read_file",
    "web_fetch",
    "web_search",
    "browser_open",
    "browser_extract",
    "browser_screenshot",
}


def staff_meeting_tool_registry() -> ToolRegistry:
    """Evidence-gathering tools that cannot mutate Brigade or external state."""
    return default_tool_registry().restricted(STAFF_MEETING_READ_ONLY_TOOLS)


def _register_browser_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="browser_open",
            description=(
                "Open a public HTTP(S) URL in the isolated browser worker and return title/url. "
                "Optional profile is restricted to Crew Chiefs and Executive."
            ),
            argument_schema={
                "url": "public http or https URL",
                "session_id": "optional browser session id",
                "profile": "optional authenticated browser profile name",
            },
        ),
        _browser_open,
    )
    registry.register(
        ToolSpec(
            name="browser_extract",
            description=(
                "Use the isolated browser worker to render a URL or current session and extract "
                "readable body text and page HTML."
            ),
            argument_schema={
                "url": "optional public http or https URL",
                "session_id": "optional browser session id",
                "profile": "optional authenticated browser profile name",
                "save_to_knowledge": "optional boolean; save extracted text with source map",
            },
        ),
        _browser_extract,
    )
    registry.register(
        ToolSpec(
            name="browser_click",
            description="Click a CSS selector in an isolated browser session.",
            argument_schema={
                "selector": "CSS selector to click",
                "session_id": "optional browser session id",
                "profile": "optional authenticated browser profile name",
            },
        ),
        _browser_click,
    )
    registry.register(
        ToolSpec(
            name="browser_screenshot",
            description="Capture a screenshot from an isolated browser session.",
            argument_schema={
                "session_id": "optional browser session id",
                "profile": "optional authenticated browser profile name",
                "full_page": "optional boolean, defaults true",
            },
        ),
        _browser_screenshot,
    )
    registry.register(
        ToolSpec(
            name="browser_clear_profile",
            description="Revoke and clear an authorized authenticated browser profile.",
            argument_schema={"profile": "authenticated browser profile name"},
        ),
        _browser_clear_profile,
    )


def _register_mcp_tools(registry: ToolRegistry) -> None:
    data_dir = Path(os.environ.get("BRIGADE_DATA_DIR", ".brigade"))
    try:
        servers = configured_servers(data_dir)
    except Exception:
        return
    for server in servers:
        try:
            tools = discover_tools(server)
        except Exception as exc:  # The CLI health surface retains the safe reason.
            record_server_health(data_dir, server, "unhealthy", str(exc))
            continue
        record_server_health(data_dir, server, "healthy")
        for tool in tools:
            registry.register(
                ToolSpec(
                    name=tool.registry_name,
                    description=tool.description or f"MCP tool {tool.name} from {server.name}",
                    argument_schema={
                        key: str(value.get("description") or value.get("type") or "")
                        if isinstance(value, dict)
                        else str(value)
                        for key, value in tool.argument_schema.items()
                    },
                ),
                _mcp_handler(server, tool.name),
            )


def _mcp_handler(server, tool_name: str) -> ToolHandler:
    def _handler(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        if not _research_component_enabled(context, "mcp"):
            return ToolResult(False, "MCP research capability is disabled by operator policy")
        agent = context.agent
        principal = agent.agent_id if agent else None
        team_id = agent.team_id if agent else None
        if not server.allows_principal(principal, team_id):
            result = ToolResult(
                False, "MCP server policy denies this principal", {"server_id": server.id}
            )
            _record_mcp_tool_event(context, server, tool_name, result)
            return result
        result = call_mcp_tool(server, tool_name, arguments)
        observation = ToolResult(result.ok, result.output, result.metadata)
        _record_mcp_tool_event(context, server, tool_name, observation)
        return observation

    return _handler


def _record_mcp_tool_event(
    context: ToolContext, server: Any, tool_name: str, result: ToolResult
) -> None:
    """Record an argument-free MCP audit event and a safe health transition."""
    data_dir = Path(os.environ.get("BRIGADE_DATA_DIR", context.store.data_dir))
    reason = None if result.ok else redact_failure_reason(result.output)
    record_server_health(data_dir, server, "healthy" if result.ok else "unhealthy", reason)
    metadata = result.metadata or {}
    record = {
        "record_id": str(metadata.get("audit_ref") or uuid4()),
        "node_id": server.id,
        "node_type": "mcp_server",
        "created_at": utc_now_iso(),
        "event_type": "mcp_tool_call" if result.ok else "mcp_tool_failure",
        "server_id": server.id,
        "tool_name": tool_name,
        "principal": context.agent.agent_id if context.agent else None,
        "policy_outcome": "allowed" if result.ok else "denied_or_failed",
        "failure_reason": reason[:240] if reason else None,
    }
    context.store.add_provenance_record(record)
    _record_research_incident(
        context,
        rule=f"mcp_{server.id}",
        failed=not result.ok,
        message=f"MCP server {server.id} {record['event_type']}: {record['failure_reason'] or ''}",
    )


def _record_research_incident(
    context: ToolContext,
    *,
    rule: str,
    failed: bool,
    message: str,
) -> None:
    correlation_id = f"research:{uuid4()}"
    alert = reconcile_research_alert(
        context.store.data_dir,
        rule=rule,
        failed=failed,
        message=message,
        correlation_id=correlation_id,
    )
    if alert.get("new"):
        context.store.add_alert(str(alert["message"]))
    context.store.add_provenance_record(
        {
            "record_id": correlation_id,
            "node_id": rule,
            "node_type": "research_control",
            "created_at": utc_now_iso(),
            "principal": context.agent.agent_id if context.agent else None,
            "tool": "research_incident",
            "policy_outcome": "degraded" if failed else "healthy",
            "failure_reason": message[:240] if failed else None,
            "alert_id": alert.get("alert_id"),
        }
    )


def _record_research_event(
    context: ToolContext,
    *,
    tool: str,
    outcome: str,
    source_url: str | None = None,
    final_url: str | None = None,
    document_id: str | None = None,
    failure_reason: str | None = None,
) -> None:
    """Persist a correlation-safe external-action row without request content."""
    context.store.add_provenance_record(
        {
            "record_id": str(uuid4()),
            "node_id": tool,
            "node_type": "web_search" if tool == "web_search" else "browser",
            "created_at": utc_now_iso(),
            "principal": context.agent.agent_id if context.agent else None,
            "tool": tool,
            "policy_outcome": outcome,
            "source_url": _redacted_external_url(source_url),
            "final_url": _redacted_external_url(final_url),
            "document_id": document_id,
            "failure_reason": redact_failure_reason(failure_reason or "") or None,
        }
    )
    external_events = [
        item
        for item in context.store.provenance_records()
        if item.get("node_type") in {"mcp_server", "web_search", "browser"}
    ]
    _record_research_incident(
        context,
        rule="unexpected_research_usage_volume",
        failed=len(external_events) >= 100,
        message=(
            f"research action volume reached {len(external_events)} events; "
            "review the filtered research audit"
        ),
    )


def _redacted_external_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def tool_manifest(registry: ToolRegistry) -> list[dict[str, Any]]:
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "argument_schema": spec.argument_schema,
        }
        for spec in registry.specs()
    ]


def workspace_tool_manifest(workspace: Path) -> list[dict[str, Any]]:
    """Descriptors for agent-built tools (``tools/*.json``), merged into the
    agent floor so a new tool is usable on the very next heartbeat."""
    tools_dir = workspace / "tools"
    if not tools_dir.exists():
        return []
    manifest: list[dict[str, Any]] = []
    for path in sorted(tools_dir.glob("*.json")):
        try:
            descriptor = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(descriptor, dict):
            continue
        name = str(descriptor.get("name") or path.stem)
        manifest.append(
            {
                "name": name,
                "description": str(descriptor.get("description") or ""),
                "argument_schema": descriptor.get("argument_schema") or {},
                "workspace_tool": True,
                "invoke_with": "run_workspace_tool",
            }
        )
    return manifest


def _safe_workspace_path(workspace: Path, raw_path: str | None) -> Path:
    relative = Path(raw_path or ".")
    if relative.is_absolute():
        raise ValueError("tool paths must be relative to the agent workspace")
    workspace = workspace.resolve()
    path = (workspace / relative).resolve()
    if workspace != path and workspace not in path.parents:
        raise ValueError("tool path escapes the agent workspace")
    return path


# The team-shared workspace: every agent reads and writes it through the
# ``shared/`` path prefix, while workspace-<agent> stays private. This is the
# artifact-handoff surface between agents — a dependency's outputs are only
# visible to the dependent task if they land here.
SHARED_WORKSPACE_DIRNAME = "shared-workspace"
_SHARED_PATH_PREFIXES = ("shared", SHARED_WORKSPACE_DIRNAME)


def _tool_path(context: ToolContext, raw_path: str | None) -> tuple[Path, Path, str]:
    """Resolve a tool path to (root, path, display_prefix).

    ``shared/...`` (or ``shared-workspace/...``) routes into the team-shared
    workspace, jailed there; anything else is jailed in the agent's private
    workspace. ``display_prefix`` reconstructs agent-facing paths.
    """
    relative = Path(raw_path or ".")
    if not relative.is_absolute() and relative.parts and relative.parts[0] in _SHARED_PATH_PREFIXES:
        root = context.store.data_dir / SHARED_WORKSPACE_DIRNAME
        root.mkdir(parents=True, exist_ok=True)
        remainder = str(Path(*relative.parts[1:])) if len(relative.parts) > 1 else "."
        return root.resolve(), _safe_workspace_path(root, remainder), "shared/"
    return (
        context.workspace.resolve(),
        _safe_workspace_path(context.workspace, raw_path),
        "",
    )


def _list_files(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    raw_path = _arg_text(arguments, "path", ".") or "."
    workspace_root, root, prefix = _tool_path(context, raw_path)
    if not root.exists():
        # A missing path in the agent's own workspace is empty scratch space, not an
        # error: report it empty so the agent creates what it needs instead of
        # blocking. write_file makes parent folders on demand.
        return ToolResult(
            True,
            "[]",
            {
                "count": 0,
                "exists": False,
                "note": (
                    f"'{raw_path}' does not exist yet; it is yours to create — "
                    "write_file makes parent folders automatically"
                ),
            },
        )
    if root.is_file():
        return ToolResult(True, prefix + str(root.relative_to(workspace_root)))
    files = [
        prefix + str(path.relative_to(workspace_root))
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ][:100]
    return ToolResult(True, json.dumps(files, indent=2), {"count": len(files)})


def _read_file(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    raw_path = _required_text(arguments, "path")
    _, path, _ = _tool_path(context, raw_path)
    if not path.exists() or not path.is_file():
        # Missing workspace file == empty, not an error: lets the agent proceed and
        # create it with write_file rather than treating it as a blocker.
        return ToolResult(
            True,
            "",
            {
                "exists": False,
                "note": f"'{raw_path}' does not exist yet; create it with write_file",
            },
        )
    text = path.read_text(encoding="utf-8")
    truncated = text[:12_000]
    detail = "truncated" if len(text) > len(truncated) else "complete"
    return ToolResult(True, truncated, {"bytes": len(text.encode("utf-8")), "detail": detail})


def _write_file(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    workspace_root, path, prefix = _tool_path(context, _required_text(arguments, "path"))
    content = _required_text(arguments, "content")
    relative_path = path.relative_to(workspace_root).as_posix()
    if not prefix and is_governing_workspace_path(relative_path):
        proposal = build_policy_change_proposal(
            context.store,
            context.agent,
            relative_path=relative_path,
            content=content,
            append=bool(arguments.get("append")),
            purpose=str(arguments.get("purpose") or "requested through write_file"),
            source_tool="write_file",
        )
        return ToolResult(
            False,
            (
                f"{relative_path} is a governed workspace policy file. Created "
                f"policy_change proposal {proposal['proposal_id']} instead of "
                "writing it directly."
            ),
            {
                "proposal_id": proposal["proposal_id"],
                "proposal_kind": "policy_change",
                "path": relative_path,
            },
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    if bool(arguments.get("append")):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(content)
    else:
        path.write_text(content, encoding="utf-8")
    return ToolResult(
        True,
        f"wrote {len(content)} characters to {prefix + str(path.relative_to(workspace_root))}",
    )


def _propose_policy_change(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    raw_path = _required_text(arguments, "path")
    content = _required_text(arguments, "content")
    purpose = _required_text(arguments, "purpose")
    try:
        relative_path = normalize_workspace_relative_path(raw_path)
    except ValueError as exc:
        return ToolResult(False, str(exc))
    if not is_governing_workspace_path(relative_path):
        return ToolResult(False, f"{relative_path} is not a governed policy file")
    proposal = build_policy_change_proposal(
        context.store,
        context.agent,
        relative_path=relative_path,
        content=content,
        append=bool(arguments.get("append")),
        purpose=purpose,
        source_tool="propose_policy_change",
    )
    return ToolResult(
        True,
        f"created policy_change proposal {proposal['proposal_id']} for {relative_path}",
        {
            "proposal_id": proposal["proposal_id"],
            "proposal_kind": "policy_change",
            "path": relative_path,
            "status": proposal.get("status"),
        },
    )


def _shell(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    command = arguments.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) for item in command)
    ):
        return ToolResult(False, "command must be a non-empty array of strings")
    if getattr(context, "direct_chief_turn", False):
        executable = Path(command[0]).name.lower()
        if executable in {"sudo", "su", "doas"}:
            return ToolResult(
                False,
                "privilege brokers are unavailable through shell; use a named maintenance action",
            )
    timeout = min(int(arguments.get("timeout_seconds") or 30), 30)
    completed = subprocess.run(
        command,
        cwd=context.workspace,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    output = "\n".join(
        part for part in (completed.stdout.strip(), completed.stderr.strip()) if part
    )
    return ToolResult(
        completed.returncode == 0,
        output[:12_000] or f"exit code {completed.returncode}",
        {"exit_code": completed.returncode},
    )


def _private_address_reason(url: str) -> str | None:
    """Return why a URL must not be fetched, or None if it looks public.

    Model-directed fetches run inside the deployment network, so anything
    that resolves to loopback, RFC1918, link-local, or otherwise non-global
    space (the Docker service mesh, the host, cloud metadata endpoints) is
    an SSRF vector, not a reference lookup.
    """
    parsed = urllib.parse.urlsplit(url)
    hostname = parsed.hostname
    if not hostname:
        return "url has no hostname"
    try:
        resolved = socket.getaddrinfo(hostname, parsed.port or 0, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as exc:
        return f"hostname did not resolve: {exc}"
    for info in resolved:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            return f"{hostname} resolves to non-public address {address}"
    return None


class _PublicOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect target so a public URL cannot bounce the
    fetch into private address space."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith(("https://", "http://")):
            raise urllib.error.URLError("redirect blocked: non-http(s) target")
        reason = _private_address_reason(newurl)
        if reason is not None:
            raise urllib.error.URLError(f"redirect blocked: {reason}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


WEB_FETCH_SAVE_MIN_CHARS = 500


def _browser_open(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    payload = _browser_payload(context, arguments, require_url=True)
    if isinstance(payload, ToolResult):
        return payload
    result = call_browser_worker("open", payload)
    return _browser_result(result, context=context)


def _browser_extract(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    payload = _browser_payload(context, arguments, require_url=False)
    if isinstance(payload, ToolResult):
        return payload
    result = call_browser_worker("extract", payload)
    if not result.get("ok"):
        return _browser_result(result, context=context)
    text = str(result.get("text") or "")
    metadata = {
        "url": result.get("url"),
        "title": result.get("title"),
        "detail": "truncated" if len(text) > 12_000 else "complete",
    }
    store = getattr(context, "store", None)
    if bool(arguments.get("save_to_knowledge")) and store is not None and text.strip():
        try:
            metadata.update(
                _save_browser_page(
                    store,
                    url=str(result.get("url") or payload.get("url") or ""),
                    title=str(result.get("title") or ""),
                    text=text[:WEB_FETCH_SAVE_MAX_CHARS],
                    html=str(result.get("html") or ""),
                )
            )
        except Exception as exc:  # noqa: BLE001
            metadata["knowledge_save"] = f"failed: {exc}"
    _record_research_event(
        context,
        tool="browser_extract",
        outcome="allowed",
        source_url=payload.get("url"),
        final_url=str(result.get("url") or ""),
        document_id=str(metadata.get("saved_document_id") or "") or None,
    )
    _record_research_incident(
        context,
        rule="browser_policy_or_capacity",
        failed=False,
        message="browser extract succeeded",
    )
    return ToolResult(True, text[:12_000], metadata)


def _browser_click(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    payload = _browser_payload(context, arguments, require_url=False)
    if isinstance(payload, ToolResult):
        return payload
    payload["selector"] = _required_text(arguments, "selector")
    return _browser_result(call_browser_worker("click", payload), context=context)


def _browser_screenshot(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    payload = _browser_payload(context, arguments, require_url=False)
    if isinstance(payload, ToolResult):
        return payload
    payload["full_page"] = bool(arguments.get("full_page", True))
    result = call_browser_worker("screenshot", payload)
    if not result.get("ok"):
        return _browser_result(result, context=context)
    image = str(result.get("image_b64") or "")
    return ToolResult(
        True,
        f"screenshot captured ({len(image)} base64 chars)",
        {"image_b64": image, "url": result.get("url"), "title": result.get("title")},
    )


def _browser_clear_profile(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    payload = _browser_payload(context, arguments, require_url=False)
    if isinstance(payload, ToolResult):
        return payload
    if not payload["profile"]:
        return ToolResult(False, "profile is required")
    return _browser_result(call_browser_worker("clear-profile", payload), context=context)


def _browser_payload(
    context: ToolContext, arguments: dict[str, Any], *, require_url: bool
) -> dict[str, Any] | ToolResult:
    if not _research_component_enabled(context, "browser"):
        return ToolResult(False, "browser research capability is disabled by operator policy")
    url = str(arguments.get("url") or "").strip()
    if require_url and not url:
        return ToolResult(False, "url is required")
    if url:
        if not url.startswith(("http://", "https://")):
            return ToolResult(False, "url must start with http:// or https://")
        reason = _private_address_reason(url)
        if reason is not None:
            return ToolResult(False, f"browser refused: {reason}")
    profile = str(arguments.get("profile") or "").strip()
    if profile and not _browser_profile_allowed(context):
        return ToolResult(
            False,
            "authenticated browser profiles are restricted to Crew Chiefs and Executive",
        )
    return {
        "url": url,
        "session_id": str(arguments.get("session_id") or "default"),
        "profile": profile,
        "principal": str(getattr(getattr(context, "agent", None), "agent_id", "") or ""),
        "team_id": str(getattr(getattr(context, "agent", None), "team_id", "") or ""),
    }


def _browser_profile_allowed(context: ToolContext) -> bool:
    persona = getattr(context, "persona", None)
    if persona is not None:
        return str(getattr(persona, "kind", "")) in {"chief", "front_desk", "executive"}
    agent = getattr(context, "agent", None)
    if agent is None:
        return False
    if str(getattr(agent, "role", "")) in {"crew_chief", "executive"}:
        return True
    try:
        return any(team.crew_chief_id == agent.agent_id for team in context.store.teams())
    except Exception:  # noqa: BLE001
        return False


def _browser_result(result: dict[str, Any], *, context: ToolContext | None = None) -> ToolResult:
    if not result.get("ok"):
        if context is not None:
            _record_research_incident(
                context,
                rule="browser_policy_or_capacity",
                failed=True,
                message=f"browser action failed: {result.get('error') or 'unknown error'}",
            )
        return ToolResult(
            False,
            f"browser failed: {result.get('error') or 'unknown error'}",
            {"error_code": result.get("error_code"), "audit_ref": result.get("audit_ref")},
        )
    if context is not None:
        _record_research_incident(
            context,
            rule="browser_policy_or_capacity",
            failed=False,
            message="browser action succeeded",
        )
    output = json.dumps(
        {key: value for key, value in result.items() if key != "image_b64"},
        sort_keys=True,
        indent=2,
    )
    return ToolResult(True, output[:12_000], result)


def _web_fetch(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    if not _research_component_enabled(context, "search"):
        return ToolResult(False, "web retrieval capability is disabled by operator policy")
    url = _required_text(arguments, "url")
    if not (url.startswith("https://") or url.startswith("http://")):
        return ToolResult(False, "url must start with http:// or https://")
    reason = _private_address_reason(url)
    if reason is not None:
        return ToolResult(False, f"web_fetch refused: {reason}")
    max_chars = min(int(arguments.get("max_chars") or 4000), 12_000)
    store = getattr(context, "store", None)
    save_requested = bool(arguments.get("save_to_knowledge")) and store is not None
    autosave = False
    if store is not None and not save_requested:
        try:
            autosave = bool((store.runtime_overrides() or {}).get("web_fetch_autosave"))
        except RuntimeError:
            autosave = False
    wants_more_for_save = save_requested or autosave
    read_cap = (
        WEB_FETCH_PDF_MAX_BYTES
        if _looks_like_pdf_url(url)
        else (WEB_FETCH_SAVE_MAX_CHARS if wants_more_for_save else max_chars)
    )
    request = urllib.request.Request(url, headers={"User-Agent": "OpenBrigade/1.0"})
    opener = urllib.request.build_opener(_PublicOnlyRedirectHandler())
    try:
        with opener.open(request, timeout=20) as response:
            final_url = response.geturl() if hasattr(response, "geturl") else url
            hint_url = final_url if _looks_like_pdf_url(final_url) else url
            content_type = _response_content_type(response, hint_url)
            response_read_cap = (
                WEB_FETCH_PDF_MAX_BYTES if content_type == "application/pdf" else read_cap
            )
            raw_body = response.read(response_read_cap + 1)
    except urllib.error.URLError as exc:
        return ToolResult(False, f"web_fetch failed: {exc}")
    if len(raw_body) > response_read_cap:
        raw_body = raw_body[:response_read_cap]
    try:
        body, page_map = _web_response_to_text(final_url, raw_body, content_type)
    except ValueError as exc:
        return ToolResult(False, f"web_fetch failed: {exc}")
    truncated = body[:max_chars]
    metadata: dict[str, Any] = {
        "detail": "truncated" if len(body) > max_chars else "complete",
        "content_type": content_type,
        "source_url": url,
        "http_final_url": final_url,
    }
    should_save = save_requested or (autosave and len(body) >= WEB_FETCH_SAVE_MIN_CHARS)
    if should_save:
        # A failed save must never fail the fetch the model asked for.
        try:
            metadata.update(
                _save_fetched_page(
                    store,
                    url=url,
                    body=body[:WEB_FETCH_SAVE_MAX_CHARS],
                    raw_body=raw_body,
                    final_url=final_url,
                    content_type=content_type,
                    page_map=page_map,
                )
            )
        except Exception as exc:  # noqa: BLE001
            metadata["knowledge_save"] = f"failed: {exc}"
    return ToolResult(True, truncated, metadata)


def _looks_like_pdf_url(url: str) -> bool:
    return urllib.parse.urlparse(url).path.lower().endswith(".pdf")


def _response_content_type(response: Any, final_url: str) -> str:
    headers = getattr(response, "headers", None)
    raw = ""
    if headers is not None:
        try:
            raw = headers.get("content-type", "") or headers.get("Content-Type", "")
        except AttributeError:
            raw = ""
    content_type = raw.split(";", 1)[0].strip().lower()
    if content_type:
        return content_type
    return "application/pdf" if _looks_like_pdf_url(final_url) else "text/plain"


def _web_response_to_text(
    final_url: str, body: bytes, content_type: str
) -> tuple[str, list[dict[str, int]] | None]:
    if content_type == "application/pdf" or _looks_like_pdf_url(final_url):
        try:
            return pdf_text_with_page_map(body)
        except Exception:  # Compatibility path for an extractor supplied by an integration.
            return extract_document_text("document.pdf", body), None
    if content_type in {"text/html", "application/xhtml+xml"}:
        return html_to_text(body.decode("utf-8", errors="replace")), None
    return body.decode("utf-8", errors="replace"), None


@dataclass(frozen=True)
class WebSearchResult:
    title: str
    url: str
    snippet: str
    engine: str = ""
    rank: int = 0
    published_at: str | None = None
    publication_status: str | None = None


class _DuckDuckGoHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[WebSearchResult] = []
        self._in_title = False
        self._in_snippet = False
        self._href = ""
        self._title_parts: list[str] = []
        self._snippet_parts: list[str] = []
        self._pending_title: tuple[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {name: value or "" for name, value in attrs}
        classes = set(attr_map.get("class", "").split())
        if tag == "a" and "result__a" in classes:
            self._in_title = True
            self._href = attr_map.get("href", "")
            self._title_parts = []
        elif tag in {"a", "div"} and "result__snippet" in classes:
            self._in_snippet = True
            self._snippet_parts = []

    def handle_endtag(self, tag: str) -> None:
        if self._in_title and tag == "a":
            self._in_title = False
            title = " ".join(" ".join(self._title_parts).split())
            url = _decode_duckduckgo_result_url(self._href)
            if title and url:
                self._pending_title = (title, url)
                self._append_pending(snippet="")
        elif self._in_snippet and tag in {"a", "div"}:
            self._in_snippet = False
            snippet = " ".join(" ".join(self._snippet_parts).split())
            self._append_pending(snippet=snippet)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        if self._in_snippet:
            self._snippet_parts.append(data)

    def _append_pending(self, *, snippet: str) -> None:
        if self._pending_title is None:
            return
        title, url = self._pending_title
        for index, result in enumerate(self.results):
            if result.url == url:
                if snippet and not result.snippet:
                    self.results[index] = WebSearchResult(
                        title=result.title, url=url, snippet=snippet
                    )
                return
        self.results.append(WebSearchResult(title=title, url=url, snippet=snippet))


def _decode_duckduckgo_result_url(raw_url: str) -> str:
    if not raw_url:
        return ""
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.path == "/l/":
        values = urllib.parse.parse_qs(parsed.query)
        target = (values.get("uddg") or [""])[0]
        if target:
            raw_url = target
    if raw_url.startswith("//"):
        raw_url = "https:" + raw_url
    if raw_url.startswith(("http://", "https://")):
        return raw_url
    return ""


def _web_search(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    if not _research_component_enabled(context, "search"):
        return ToolResult(False, "web search capability is disabled by operator policy")
    query = _required_text(arguments, "query").strip()
    if not query:
        return ToolResult(False, "query is required")
    if len(query) > WEB_SEARCH_MAX_QUERY_CHARS:
        return ToolResult(False, f"query exceeds {WEB_SEARCH_MAX_QUERY_CHARS} characters")
    limit = max(1, min(int(arguments.get("limit") or 5), WEB_SEARCH_MAX_RESULTS))
    intent = str(arguments.get("intent") or "general").strip().lower()
    if intent not in {"general", "legal"}:
        return ToolResult(False, "intent must be general or legal")
    jurisdiction = str(arguments.get("jurisdiction") or "").strip() or None
    backend = os.environ.get("BRIGADE_SEARCH_BACKEND", "searxng").strip().lower()
    final_url = ""
    search_error = ""
    try:
        if backend == "searxng":
            found, final_url = search_with_retry(
                query, limit=limit, search=lambda value: searxng_search(value, limit=limit)
            )
            results = [
                WebSearchResult(
                    title=item.title,
                    url=item.url,
                    snippet=item.snippet,
                    engine=item.engine,
                    rank=item.rank,
                    published_at=item.published_at,
                    publication_status=item.publication_status,
                )
                for item in found
            ]
        else:
            results, final_url = _duckduckgo_search(query, limit=limit)
    except Exception as exc:  # noqa: BLE001
        search_error = str(exc)
        if backend != "searxng":
            _record_research_incident(
                context,
                rule="search_backend_degraded",
                failed=True,
                message=f"search backend failed: {search_error}",
            )
            return ToolResult(False, f"web_search failed: {search_error}")
        store = getattr(context, "store", None)
        if store is not None:
            record_search_health(
                store.data_dir,
                backend="searxng",
                state="degraded",
                reason=search_error,
            )
        try:
            results, final_url = _duckduckgo_search(query, limit=limit)
            backend = "duckduckgo-fallback"
        except Exception as fallback_exc:  # noqa: BLE001
            _record_research_incident(
                context,
                rule="search_backend_degraded",
                failed=True,
                message=f"search backend failed: {search_error}; fallback failed: {fallback_exc}",
            )
            _record_research_event(
                context,
                tool="web_search",
                outcome="failed",
                failure_reason=f"{search_error}; fallback failed: {fallback_exc}",
            )
            return ToolResult(
                False,
                f"web_search failed: {search_error}; fallback failed: {fallback_exc}",
            )
    else:
        store = getattr(context, "store", None)
        if store is not None and backend == "searxng":
            record_search_health(store.data_dir, backend="searxng", state="healthy")
    results = [result for result in results if _private_address_reason(result.url) is None]
    classified = []
    for result in results:
        tier, detected_jurisdiction, authority = classify_source(
            result.url, publication_status=result.publication_status
        )
        row = result.__dict__.copy()
        row.update(
            {
                "source_tier": tier,
                "jurisdiction": detected_jurisdiction or jurisdiction,
                "authority": authority,
                "result_timestamp": utc_now_iso(),
                "publication_date": result.published_at,
                "publication_status": result.publication_status,
            }
        )
        classified.append(row)
    if intent == "legal":
        priority = {
            "court_material": 0,
            "primary_legal": 0,
            "official_government": 1,
            "secondary_legal": 2,
            "academic": 3,
            "academic_index": 3,
            "academic_preprint": 3,
            "academic_peer_reviewed": 3,
            "general_web": 4,
            "discovery_only": 5,
        }
        classified.sort(key=lambda item: (priority.get(str(item["source_tier"]), 4), item["rank"]))
    for selection_rank, row in enumerate(classified, start=1):
        row["selection_rank"] = selection_rank
    results = [
        WebSearchResult(
            **{
                key: value
                for key, value in item.items()
                if key in WebSearchResult.__dataclass_fields__
            }
        )
        for item in classified
    ]
    if not results:
        _record_research_event(
            context,
            tool="web_search",
            outcome="allowed",
            final_url=final_url,
        )
        return ToolResult(
            True, "No search results found.", {"results": [], "source_url": final_url}
        )
    lines: list[str] = []
    for index, result in enumerate(results, start=1):
        lines.append(f"{index}. {result.title}\n   {result.url}")
        classification = classified[index - 1]
        if classification["source_tier"] == "discovery_only":
            lines.append(
                "   Discovery pointer only; retrieve an independent source before relying on it."
            )
        if result.snippet:
            lines.append(f"   {result.snippet[:300]}")
    metadata: dict[str, Any] = {
        "source_url": final_url,
        "query": query,
        "backend": backend,
        "backend_error": search_error,
        "mode": "fallback" if backend == "duckduckgo-fallback" else "normal",
        "intent": intent,
        "jurisdiction": jurisdiction,
        "selection_hint": source_selection_hint(intent, jurisdiction),
        "results": classified,
        "source_urls": [result.url for result in results],
    }
    store = getattr(context, "store", None)
    if bool(arguments.get("save_to_knowledge")) and store is not None:
        try:
            metadata.update(
                _save_search_results(
                    store,
                    query=query,
                    source_url=final_url,
                    results=results,
                    classified_results=classified,
                    backend=backend,
                    mode="fallback" if backend == "duckduckgo-fallback" else "normal",
                    backend_error=search_error,
                )
            )
        except Exception as exc:  # noqa: BLE001
            metadata["knowledge_save"] = f"failed: {exc}"
    _record_research_event(
        context,
        tool="web_search",
        outcome="fallback" if backend == "duckduckgo-fallback" else "allowed",
        final_url=final_url,
        document_id=str(metadata.get("saved_document_id") or "") or None,
    )
    _record_research_incident(
        context,
        rule="search_backend_degraded",
        failed=backend == "duckduckgo-fallback",
        message=(
            f"SearXNG degraded; serving DuckDuckGo fallback: {search_error}"
            if backend == "duckduckgo-fallback"
            else "search backend succeeded"
        ),
    )
    return ToolResult(True, "\n".join(lines), metadata)


def _duckduckgo_search(query: str, *, limit: int) -> tuple[list[WebSearchResult], str]:
    search_url = WEB_SEARCH_DEFAULT_URL + "?" + urllib.parse.urlencode({"q": query})
    reason = _private_address_reason(search_url)
    if reason is not None:
        raise urllib.error.URLError(f"web_search refused: {reason}")
    request = urllib.request.Request(search_url, headers={"User-Agent": "OpenBrigade/1.0"})
    opener = urllib.request.build_opener(_PublicOnlyRedirectHandler())
    with opener.open(request, timeout=20) as response:
        html = response.read(WEB_SEARCH_READ_CAP + 1).decode("utf-8", errors="replace")
        final_url = response.geturl() if hasattr(response, "geturl") else search_url
    parser = _DuckDuckGoHTMLParser()
    parser.feed(html)
    return parser.results[:limit], final_url


def _save_search_results(
    store: StateStore,
    *,
    query: str,
    source_url: str,
    results: list[WebSearchResult],
    classified_results: list[dict[str, Any]] | None = None,
    backend: str = "",
    mode: str = "normal",
    backend_error: str = "",
) -> dict[str, Any]:
    body_lines = [f"Search query: {query}", f"Search URL: {source_url}", ""]
    retained_results = []
    for index, result in enumerate(results, start=1):
        body_lines.append(f"{index}. {result.title}")
        body_lines.append(f"URL: {result.url}")
        if result.engine:
            body_lines.append(f"Engine: {result.engine}")
        body_lines.append("")
        source_row = (
            (classified_results or [])[index - 1]
            if classified_results
            else result.__dict__.copy()
        )
        retained_results.append(
            {key: value for key, value in source_row.items() if key != "snippet"}
        )
    body = "\n".join(body_lines).strip()
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    for document in store.knowledge_documents():
        doc_metadata = document.get("metadata") or {}
        if (
            doc_metadata.get("content_type") == "web_search"
            and doc_metadata.get("query") == query
            and doc_metadata.get("content_hash") == content_hash
        ):
            return {
                "knowledge_save": "skipped-duplicate",
                "saved_document_id": document.get("document_id"),
            }
    content_dir = _research_storage_root(store) / "web_search"
    content_dir.mkdir(parents=True, exist_ok=True)
    content_path = content_dir / f"{content_hash}.txt"
    content_path.write_text(body, encoding="utf-8")
    result = ingest_text(
        title=f"Web search: {query}",
        source=source_url,
        document_type="web_search",
        content=body,
        content_path=str(content_path),
        extra_metadata={
            "content_type": "web_search",
            "source_url": source_url,
            "query": query,
            "result_urls": [item.url for item in results],
            "searched_at": utc_now_iso(),
            "content_hash": content_hash,
            "backend": backend,
            "search_mode": mode,
            "backend_error": backend_error or None,
            "source_map": source_map(
                source_url=source_url,
                title=f"Web search: {query}",
                retrieval_tool="web_search",
                content_type="web_search",
                content_hash=content_hash,
                byte_size=len(body.encode("utf-8")),
                extra={
                    "results": retained_results,
                    "snippets_retained": False,
                    "backend": backend,
                    "search_mode": mode,
                    "backend_error": backend_error or None,
                },
            ),
        },
    )
    saved = store_ingest_result(store, result)
    return {
        "knowledge_save": "saved",
        "saved_document_id": saved["document_id"],
    }


def _save_browser_page(
    store: StateStore,
    *,
    url: str,
    title: str,
    text: str,
    html: str,
) -> dict[str, Any]:
    source_tier, jurisdiction, authority = classify_source(url)
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    content_dir = _research_storage_root(store) / "browser"
    content_dir.mkdir(parents=True, exist_ok=True)
    content_path = content_dir / f"{content_hash}.txt"
    content_path.write_text(text, encoding="utf-8")
    retained_path: Path | None = None
    if html:
        retained_path = content_dir / f"{content_hash}.html"
        retained_path.write_text(html, encoding="utf-8")
    result = ingest_text(
        title=title or url,
        source=url,
        document_type="web",
        content=text,
        content_path=str(content_path),
        extra_metadata={
            "content_type": "text/html",
            "source_url": url,
            "http_final_url": url,
            "fetched_at": utc_now_iso(),
            "content_hash": content_hash,
            "retained_source_path": str(retained_path) if retained_path else None,
            "source_tier": source_tier,
            "jurisdiction": jurisdiction,
            "authority": authority,
            "source_map": source_map(
                source_url=url,
                final_url=url,
                title=title or url,
                retrieval_tool="browser_extract",
                content_type="text/html",
                content_hash=content_hash,
                byte_size=len(html.encode("utf-8")) if html else len(text.encode("utf-8")),
                quote=text[:500],
                extra={
                    "retained_source_path": str(retained_path) if retained_path else None,
                    "rendered_text_byte_size": len(text.encode("utf-8")),
                    "source_tier": source_tier,
                    "jurisdiction": jurisdiction,
                    "authority": authority,
                },
            ),
        },
    )
    saved = store_ingest_result(store, result)
    return {"knowledge_save": "saved", "saved_document_id": saved["document_id"]}


def _save_fetched_page(
    store: StateStore,
    *,
    url: str,
    body: str,
    raw_body: bytes,
    final_url: str,
    content_type: str = "text/plain",
    page_map: list[dict[str, int]] | None = None,
) -> dict[str, Any]:
    """Retain the original response file and the parsed text used by knowledge stores."""
    content_hash = hashlib.sha256(raw_body).hexdigest()
    source_tier, jurisdiction, authority = classify_source(final_url)
    previous_versions: list[str] = []
    for document in store.knowledge_documents():
        doc_metadata = document.get("metadata") or {}
        if doc_metadata.get("source_url") != url:
            continue
        if doc_metadata.get("content_hash") == content_hash:
            return {
                "knowledge_save": "skipped-duplicate",
                "saved_document_id": document.get("document_id"),
            }
        if not doc_metadata.get("superseded_by"):
            previous_versions.append(str(document.get("document_id")))
    content_dir = _research_storage_root(store) / "web"
    content_dir.mkdir(parents=True, exist_ok=True)
    if content_type == "application/pdf":
        suffix = ".pdf"
    elif content_type in {"text/html", "application/xhtml+xml"}:
        suffix = ".html"
    else:
        suffix = ".bin"
    retained_path = content_dir / f"{content_hash}{suffix}"
    retained_path.write_bytes(raw_body)
    # Ingest clean text, not raw markup or PDF bytes: chunks/embeddings hold prose.
    extracted = html_to_text(body).strip() or body
    content_path = content_dir / f"{content_hash}.txt"
    content_path.write_text(extracted, encoding="utf-8")
    parsed = urllib.parse.urlparse(url)
    title = f"{parsed.netloc}{parsed.path}".rstrip("/") or url
    result = ingest_text(
        title=title,
        source=url,
        document_type="web",
        content=extracted,
        content_path=str(content_path),
        extra_metadata={
            "content_type": content_type,
            "source_url": url,
            "http_final_url": final_url,
            "fetched_at": utc_now_iso(),
            "content_hash": content_hash,
            "retained_source_path": str(retained_path),
            "source_tier": source_tier,
            "jurisdiction": jurisdiction,
            "authority": authority,
            "source_map": source_map(
                source_url=url,
                final_url=final_url,
                title=title,
                retrieval_tool="web_fetch",
                content_type=content_type,
                content_hash=content_hash,
                byte_size=len(raw_body),
                quote=extracted[:500],
                extra={
                    "retained_source_path": str(retained_path),
                    "raw_byte_size": len(raw_body),
                    "source_tier": source_tier,
                    "jurisdiction": jurisdiction,
                    "authority": authority,
                },
            ),
        },
        page_map=page_map,
    )
    saved = store_ingest_result(store, result)
    outcome: dict[str, Any] = {
        "knowledge_save": "saved",
        "saved_document_id": saved["document_id"],
    }
    # The page changed: retire earlier versions of the same URL so retrieval
    # only ever surfaces the current content.
    for old_document_id in previous_versions:
        store.supersede_knowledge_document(old_document_id, str(saved["document_id"]))
    if previous_versions:
        outcome["superseded_documents"] = previous_versions
    return outcome


# Delegated tasks carry no idempotency key, so a planner re-run cheerfully
# re-delegates the same work: during the Jul 4 observation window one agent
# queued four near-identical copies of a task behind a pinned teammate.
# Near-duplicate detection is token overlap (Jaccard) over the assignment
# text against the target agent's undone backlog.
_BACKLOG_DEDUP_THRESHOLD = 0.6
_BACKLOG_DEDUP_STOPWORDS = frozenset(
    "and the for with that this into from your each are was all".split()
)
_UNDONE_STATUSES = frozenset(
    {
        AssignmentStatus.QUEUED,
        AssignmentStatus.ASSIGNED,
        AssignmentStatus.WORKING,
        AssignmentStatus.BLOCKED,
    }
)


def _dedup_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if len(token) > 2 and token not in _BACKLOG_DEDUP_STOPWORDS
    }


# Public names for the duplicate-detection machinery so the orchestrator's
# reconciliation sweep shares one similarity definition with delegation dedup.
dedup_tokens = _dedup_tokens
BACKLOG_DEDUP_THRESHOLD = _BACKLOG_DEDUP_THRESHOLD
UNDONE_STATUSES = _UNDONE_STATUSES


def _find_backlog_duplicate(
    store: StateStore, target_agent_id: str, text: str
) -> Assignment | None:
    """A live undone assignment (any agent's queue) whose text is
    near-identical to ``text``, or None.

    Cross-agent on purpose: two agents planning the same mission
    independently produce parallel near-identical ladders (observed Jul 9:
    Stage-2/Stage-3 tasks duplicated across infrastructure and designer).
    Rest and failure-analysis tasks are templated text, near-identical
    across agents by construction, so they never count as duplicates.
    """
    tokens = _dedup_tokens(text)
    if not tokens:
        return None
    for item in store.assignments():
        if item.status not in _UNDONE_STATUSES:
            continue
        if item.kind in {AssignmentKind.REST, AssignmentKind.FAILURE_ANALYSIS}:
            continue
        other = _dedup_tokens(item.assignment)
        if not other:
            continue
        overlap = len(tokens & other) / len(tokens | other)
        if overlap >= _BACKLOG_DEDUP_THRESHOLD:
            return item
    return None


# Backlog dedup alone lets the same work run repeatedly: once the first copy
# COMPLETES it leaves the backlog, and the next planner pass re-delegates it
# (Jul 8: shared/operational_roles.md was "created" by three separate
# completions in one night). Completed history inside this window counts as a
# duplicate too — the delegator is told the deliverable already exists.
_COMPLETED_DEDUP_WINDOW_SECONDS = 24 * 3600


def _find_completed_duplicate(
    store: StateStore, target_agent_id: str, text: str
) -> dict[str, Any] | None:
    """A recently COMPLETED archived assignment (any owner's history for
    ``target_agent_id``) whose text is near-identical to ``text``, or None."""
    from brigade.time import parse_utc_iso, utc_now

    tokens = _dedup_tokens(text)
    if not tokens:
        return None
    now = utc_now()
    for item in reversed(store.assignment_history()):
        if item.get("final_status") != AssignmentStatus.COMPLETE.value:
            continue
        record = item.get("record") or {}
        if record.get("assigned_to") != target_agent_id:
            continue
        archived_at = item.get("archived_at")
        if archived_at:
            try:
                age = (now - parse_utc_iso(str(archived_at))).total_seconds()
            except ValueError:
                age = None
            if age is not None and age > _COMPLETED_DEDUP_WINDOW_SECONDS:
                # History is ordered by archived_at; everything earlier is
                # older still.
                break
        other = _dedup_tokens(str(record.get("assignment") or ""))
        if not other:
            continue
        overlap = len(tokens & other) / len(tokens | other)
        if overlap >= _BACKLOG_DEDUP_THRESHOLD:
            return item
    return None


def _delegate(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    from brigade.orchestrator import orchestration_event, record_orchestration_events

    guard = _delegation_guard(context, requested_children=1)
    if guard is not None:
        return guard
    target_agent_id = _required_text(arguments, "agent_id")
    target = next(
        (agent for agent in context.store.agents() if agent.agent_id == target_agent_id),
        None,
    )
    if target is None:
        return ToolResult(False, f"unknown target agent: {target_agent_id}")
    try:
        priority = _priority_from_arguments(arguments)
    except ValueError as exc:
        return ToolResult(False, str(exc))
    assignment_text = _required_text(arguments, "assignment")
    duplicate = _find_backlog_duplicate(context.store, target_agent_id, assignment_text)
    if duplicate is not None:
        return ToolResult(
            True,
            (
                f"skipped duplicate delegation: assignment "
                f"{duplicate.assignment_id} already covers near-identical work "
                f"(owned by {duplicate.assigned_to}, status: "
                f"{duplicate.status.value}). "
                "Treat that assignment as your delegation in motion."
            ),
            {
                "assignment_id": duplicate.assignment_id,
                "status": duplicate.status.value,
                "deduplicated": True,
            },
        )
    completed = _find_completed_duplicate(context.store, target_agent_id, assignment_text)
    if completed is not None:
        summary = completed.get("executive_summary") or "no summary recorded"
        return ToolResult(
            True,
            (
                f"skipped duplicate delegation: assignment "
                f"{completed.get('assignment_id')} already COMPLETED near-identical "
                f"work for {target_agent_id} at {completed.get('archived_at')}. "
                f"Its result: {summary} "
                "Check the existing deliverable (e.g. with read_file/list_files) "
                "before delegating again; only re-delegate with a materially "
                "different assignment if the deliverable is missing or inadequate."
            ),
            {
                "assignment_id": completed.get("assignment_id"),
                "status": AssignmentStatus.COMPLETE.value,
                "deduplicated": True,
                "already_completed": True,
            },
        )
    assignment = Assignment(
        assignment=assignment_text,
        assigned_to=target_agent_id,
        created_by=context.agent.agent_id,
        source="agent_delegate",
        priority=priority,
        parent_assignment_id=context.assignment.assignment_id,
        goal_statement=_arg_text(arguments, "goal_statement", None),
        assignment_rationale=f"Delegated by {context.agent.agent_id} during active work.",
    )
    persisted = context.store.add_assignment(assignment)
    mission = context.store.mission()
    record_orchestration_events(
        context.store,
        source="agent_delegate",
        decision_summary=(
            f"{context.agent.agent_id} delegated one assignment to {target_agent_id}"
        ),
        mission_statement=mission.statement if mission else None,
        events=[
            orchestration_event(
                "delegated_task",
                (
                    f"{context.agent.agent_id} delegated assignment "
                    f"{persisted.assignment_id} to {target_agent_id}."
                ),
                source="agent_delegate",
                decision="delegated",
                status=persisted.status.value,
                mission_statement=mission.statement if mission else None,
                goal_statement=persisted.goal_statement,
                assignment_id=persisted.assignment_id,
                assignment_ids=[persisted.assignment_id],
                agent_id=target_agent_id,
                parent_assignment_id=context.assignment.assignment_id,
                child_assignment_ids=[persisted.assignment_id],
                payload={
                    "parent_assignment": context.assignment.to_dict(),
                    "child_assignment": persisted.to_dict(),
                },
            )
        ],
    )
    return ToolResult(
        True,
        f"created queued assignment {persisted.assignment_id} for {target_agent_id}",
        {"assignment_id": persisted.assignment_id, "status": persisted.status.value},
    )


def _create_subtasks(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    from brigade.orchestrator import orchestration_event, record_orchestration_events

    raw_subtasks = arguments.get("subtasks")
    if not isinstance(raw_subtasks, list) or not raw_subtasks:
        return ToolResult(False, "subtasks must be a non-empty array")
    if len(raw_subtasks) > MAX_CREATE_SUBTASKS:
        return ToolResult(False, f"subtasks is limited to {MAX_CREATE_SUBTASKS} items")
    depth = _delegation_depth(context.store, context.assignment)
    if depth >= MAX_DELEGATION_DEPTH:
        return ToolResult(
            False,
            f"delegation depth limit reached for assignment {context.assignment.assignment_id}",
            {"max_depth": MAX_DELEGATION_DEPTH, "depth": depth},
        )
    # Capacity is not an error: extra children simply queue behind the active
    # ones. Accept up to the remaining slots and tell the planner what was
    # trimmed instead of rejecting the whole batch (which used to block
    # planners whose plan was already partially in motion).
    existing_children = [
        assignment.assignment_id
        for assignment in context.store.assignments()
        if assignment.parent_assignment_id == context.assignment.assignment_id
    ]
    remaining_capacity = MAX_CHILDREN_PER_ASSIGNMENT - len(existing_children)
    if remaining_capacity <= 0:
        return ToolResult(
            True,
            (
                f"no capacity for new subtasks: {len(existing_children)} child "
                f"assignments already exist ({', '.join(existing_children)}). "
                "Treat the existing queued children as your plan in motion; "
                "review or extend them rather than recreating the plan."
            ),
            {"existing_children": existing_children, "created": []},
        )
    trimmed_count = max(0, len(raw_subtasks) - remaining_capacity)
    raw_subtasks = raw_subtasks[:remaining_capacity]

    known_agent_ids = {agent.agent_id for agent in context.store.agents()}
    normalized: list[dict[str, Any]] = []
    for index, raw_subtask in enumerate(raw_subtasks, start=1):
        if not isinstance(raw_subtask, dict):
            return ToolResult(False, f"subtask {index} must be an object")
        target_agent_id = _required_text(raw_subtask, "agent_id")
        if target_agent_id not in known_agent_ids:
            return ToolResult(False, f"unknown target agent in subtask {index}: {target_agent_id}")
        try:
            priority = _priority_from_arguments(raw_subtask)
        except ValueError as exc:
            return ToolResult(False, f"subtask {index}: {exc}")
        normalized.append(
            {
                "agent_id": target_agent_id,
                "assignment": _required_text(raw_subtask, "assignment"),
                "priority": priority,
                "goal_statement": _arg_text(raw_subtask, "goal_statement", None),
                "depends_on_previous": bool(raw_subtask.get("depends_on_previous")),
                "index": index,
            }
        )

    created: list[dict[str, Any]] = []
    previous_assignment_id: str | None = None
    for item in normalized:
        dependency_ids = (
            [previous_assignment_id]
            if item["depends_on_previous"] and previous_assignment_id
            else []
        )
        duplicate = _find_backlog_duplicate(
            context.store, str(item["agent_id"]), str(item["assignment"])
        )
        if duplicate is not None:
            # Reuse the existing assignment: it anchors the dependency chain
            # for subsequent subtasks instead of queueing a near-identical
            # copy behind it.
            previous_assignment_id = duplicate.assignment_id
            created.append(
                {
                    "assignment_id": duplicate.assignment_id,
                    "agent_id": item["agent_id"],
                    "dependency_ids": list(duplicate.dependency_ids),
                    "status": duplicate.status.value,
                    "deduplicated": True,
                }
            )
            continue
        completed = _find_completed_duplicate(
            context.store, str(item["agent_id"]), str(item["assignment"])
        )
        if completed is not None:
            # Already done: anchor the chain on the archived assignment
            # (dependency lookups resolve archived ids via history).
            previous_assignment_id = str(completed.get("assignment_id"))
            created.append(
                {
                    "assignment_id": completed.get("assignment_id"),
                    "agent_id": item["agent_id"],
                    "dependency_ids": [],
                    "status": AssignmentStatus.COMPLETE.value,
                    "deduplicated": True,
                    "already_completed": True,
                }
            )
            continue
        assignment = Assignment(
            assignment=str(item["assignment"]),
            assigned_to=str(item["agent_id"]),
            created_by=context.agent.agent_id,
            source="agent_delegate",
            priority=item["priority"],
            parent_assignment_id=context.assignment.assignment_id,
            dependency_ids=dependency_ids,
            goal_statement=item["goal_statement"],
            assignment_rationale=(
                f"Structured subtask {item['index']} created by {context.agent.agent_id}."
            ),
        )
        persisted = context.store.add_assignment(assignment)
        previous_assignment_id = persisted.assignment_id
        created.append(
            {
                "assignment_id": persisted.assignment_id,
                "agent_id": item["agent_id"],
                "dependency_ids": dependency_ids,
                "status": persisted.status.value,
            }
        )
    mission = context.store.mission()
    record_orchestration_events(
        context.store,
        source="create_subtasks",
        decision_summary=(f"{context.agent.agent_id} created {len(created)} child assignment(s)"),
        mission_statement=mission.statement if mission else None,
        events=[
            orchestration_event(
                "delegated_task",
                (
                    f"{context.agent.agent_id} created {len(created)} child assignment(s) "
                    f"for parent {context.assignment.assignment_id}."
                ),
                source="create_subtasks",
                decision="delegated",
                status="queued",
                mission_statement=mission.statement if mission else None,
                goal_statement=context.assignment.goal_statement,
                assignment_ids=[item["assignment_id"] for item in created],
                agent_id=context.agent.agent_id,
                parent_assignment_id=context.assignment.assignment_id,
                child_assignment_ids=[item["assignment_id"] for item in created],
                payload={"created": created, "parent_assignment": context.assignment.to_dict()},
            )
        ],
    )
    output = f"created {len(created)} queued subtasks"
    if trimmed_count:
        output += (
            f" ({trimmed_count} trimmed: child capacity of "
            f"{MAX_CHILDREN_PER_ASSIGNMENT} reached; resubmit the rest after "
            "existing children complete)"
        )
    return ToolResult(
        True,
        output,
        {"created": created, "trimmed": trimmed_count},
    )


def _request_tool(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    from brigade.orchestrator import orchestration_event, record_orchestration_events
    from brigade.schemas import build_proposal

    name = _required_text(arguments, "name").strip()
    purpose = _required_text(arguments, "purpose")
    spec = _required_text(arguments, "spec")
    proposal = build_proposal(
        kind="tool_request",
        title=f"Tool request: {name}",
        agent_id=context.agent.agent_id,
        team_id=context.agent.team_id,
        details={
            "name": name,
            "purpose": purpose,
            "spec": spec,
            "requested_in_assignment": context.assignment.assignment_id,
        },
        idempotency_key=f"tool-request:v1:{context.agent.agent_id}:{name}",
    )
    persisted = context.store.add_proposal(proposal)
    if persisted.get("proposal_id") != proposal["proposal_id"]:
        return ToolResult(
            True,
            f"tool request for '{name}' already pending as proposal {persisted.get('proposal_id')}",
            {"proposal_id": persisted.get("proposal_id"), "status": "existing"},
        )
    context.store.add_alert(
        f"tool request from {context.agent.agent_id}: '{name}' "
        f"(proposal {proposal['proposal_id']}) awaits approval"
    )
    record_orchestration_events(
        context.store,
        source="tool_request",
        decision_summary=f"tool request '{name}' proposed by {context.agent.agent_id}",
        events=[
            orchestration_event(
                "proposal_created",
                f"Agent {context.agent.agent_id} requested tool '{name}'.",
                source="tool_request",
                decision="proposed",
                status="proposed",
                assignment_id=context.assignment.assignment_id,
                agent_id=context.agent.agent_id,
                idempotency_key=proposal["idempotency_key"],
                payload=proposal,
            )
        ],
    )
    return ToolResult(
        True,
        f"tool request '{name}' recorded as proposal {proposal['proposal_id']}; "
        "it will be built after approval",
        {"proposal_id": proposal["proposal_id"], "status": "proposed"},
    )


def _request_staff_meeting(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    from brigade.orchestrator import route_to_chief
    from brigade.schemas import build_proposal

    request = _required_text(arguments, "request")
    reason = _required_text(arguments, "reason")
    chief = route_to_chief(context.store, agent_id=context.agent.agent_id)
    if chief is None:
        return ToolResult(False, "no Crew Chief is available to receive the request")
    criteria = [
        str(item).strip()
        for item in arguments.get("acceptance_criteria") or []
        if str(item).strip()
    ]
    proposal = build_proposal(
        kind="staff_meeting_request",
        title=f"Staff Meeting request from {context.agent.agent_id}",
        agent_id=context.agent.agent_id,
        team_id=context.agent.team_id,
        details={
            "request": request,
            "reason": reason,
            "suggested_acceptance_criteria": criteria,
            "requested_in_assignment": context.assignment.assignment_id,
            "requested_chief_agent_id": chief.agent_id,
        },
        idempotency_key=(
            f"staff-meeting-request:v1:{context.assignment.assignment_id}:"
            f"{context.agent.agent_id}"
        ),
    )
    persisted = context.store.add_proposal(proposal)
    context.store.add_alert(
        f"Staff Meeting requested by {context.agent.agent_id} for Crew Chief "
        f"{chief.agent_id}: {request[:160]}"
    )
    return ToolResult(
        True,
        f"Staff Meeting request sent to Crew Chief {chief.agent_id}",
        {
            "proposal_id": persisted["proposal_id"],
            "chief_agent_id": chief.agent_id,
            "status": persisted["status"],
        },
    )


def _approve_proposal(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    from brigade.services import decide_proposal

    proposal_id = _required_text(arguments, "proposal_id")
    proposal = context.store.find_proposal(proposal_id)
    if proposal is None:
        return ToolResult(False, f"unknown proposal: {proposal_id}")
    own_teams = {
        team.team_id
        for team in context.store.teams()
        if team.crew_chief_id == context.agent.agent_id
    }
    if not own_teams:
        return ToolResult(False, "approve_proposal is limited to crew chiefs")
    if proposal.get("team_id") not in own_teams:
        return ToolResult(
            False,
            "approve_proposal is limited to proposals raised by your own team",
        )
    decided = decide_proposal(
        context.store,
        proposal_id=proposal_id,
        decision="approved",
        decided_by=context.agent.agent_id,
    )
    effects = (decided.get("details") or {}).get("approval_effects") or {}
    return ToolResult(
        True,
        f"proposal {proposal_id} approved",
        {"proposal_id": proposal_id, "approval_effects": effects},
    )


def _run_workspace_tool(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    name = _required_text(arguments, "name")
    args = arguments.get("args") or []
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        return ToolResult(False, "args must be an array of strings")
    tools_dir = _safe_workspace_path(context.workspace, "tools")
    tool_path = _safe_workspace_path(context.workspace, f"tools/{name}")
    if tool_path.parent != tools_dir:
        return ToolResult(False, "tool name must resolve directly under tools/")
    if not tool_path.exists() or not tool_path.is_file():
        return ToolResult(False, f"workspace tool does not exist: tools/{name}")
    # Same subprocess guard as shell: 30s cap, no shell interpreter.
    completed = subprocess.run(
        [str(tool_path), *args],
        cwd=context.workspace,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    output = "\n".join(
        part for part in (completed.stdout.strip(), completed.stderr.strip()) if part
    )
    return ToolResult(
        completed.returncode == 0,
        output[:12_000] or f"exit code {completed.returncode}",
        {"exit_code": completed.returncode, "tool": name},
    )


def _delegation_guard(context: ToolContext, *, requested_children: int) -> ToolResult | None:
    depth = _delegation_depth(context.store, context.assignment)
    if depth >= MAX_DELEGATION_DEPTH:
        return ToolResult(
            False,
            f"delegation depth limit reached for assignment {context.assignment.assignment_id}",
            {"max_depth": MAX_DELEGATION_DEPTH, "depth": depth},
        )
    child_count = sum(
        1
        for assignment in context.store.assignments()
        if assignment.parent_assignment_id == context.assignment.assignment_id
    )
    if child_count + requested_children > MAX_CHILDREN_PER_ASSIGNMENT:
        return ToolResult(
            False,
            (
                f"delegation fan-out limit exceeded for assignment "
                f"{context.assignment.assignment_id}"
            ),
            {
                "max_children": MAX_CHILDREN_PER_ASSIGNMENT,
                "existing_children": child_count,
                "requested_children": requested_children,
            },
        )
    return None


def _delegation_depth(store: StateStore, assignment: Assignment) -> int:
    depth = 0
    parent_id = assignment.parent_assignment_id
    seen = {assignment.assignment_id}
    while parent_id:
        if parent_id in seen:
            break
        seen.add(parent_id)
        parent = store.find_assignment(parent_id)
        if parent is None:
            break
        depth += 1
        parent_id = parent.parent_assignment_id
    return depth


def _priority_from_arguments(arguments: dict[str, Any]) -> Priority:
    priority_value = str(arguments.get("priority") or Priority.NORMAL.value).lower()
    try:
        return Priority(priority_value)
    except ValueError as exc:
        raise ValueError(f"unsupported priority: {priority_value}") from exc


def _required_text(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value


def _arg_text(arguments: dict[str, Any], key: str, default: str | None) -> str | None:
    value = arguments.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return value
