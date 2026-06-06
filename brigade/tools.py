from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from brigade.schemas import Agent, Assignment, Priority
from brigade.store import StateStore


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


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolSpec, ToolHandler]] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        self._tools[spec.name] = (spec, handler)

    def specs(self) -> list[ToolSpec]:
        return [item[0] for item in self._tools.values()]

    def execute(self, name: str, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        item = self._tools.get(name)
        if item is None:
            return ToolResult(False, f"unknown tool: {name}")
        _, handler = item
        try:
            return handler(context, arguments)
        except Exception as exc:
            return ToolResult(False, str(exc))


def default_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="list_files",
            description="List files under the assigned agent workspace.",
            argument_schema={"path": "optional relative workspace path"},
        ),
        _list_files,
    )
    registry.register(
        ToolSpec(
            name="read_file",
            description="Read a UTF-8 text file from the assigned agent workspace.",
            argument_schema={"path": "relative workspace file path"},
        ),
        _read_file,
    )
    registry.register(
        ToolSpec(
            name="write_file",
            description="Write or append UTF-8 text inside the assigned agent workspace.",
            argument_schema={
                "path": "relative workspace file path",
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
            description="Fetch a small HTTP(S) text response for reference.",
            argument_schema={"url": "http or https URL", "max_chars": "optional integer"},
        ),
        _web_fetch,
    )
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
    return registry


def tool_manifest(registry: ToolRegistry) -> list[dict[str, Any]]:
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "argument_schema": spec.argument_schema,
        }
        for spec in registry.specs()
    ]


def _safe_workspace_path(workspace: Path, raw_path: str | None) -> Path:
    relative = Path(raw_path or ".")
    if relative.is_absolute():
        raise ValueError("tool paths must be relative to the agent workspace")
    workspace = workspace.resolve()
    path = (workspace / relative).resolve()
    if workspace != path and workspace not in path.parents:
        raise ValueError("tool path escapes the agent workspace")
    return path


def _list_files(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    root = _safe_workspace_path(context.workspace, _arg_text(arguments, "path", "."))
    if not root.exists():
        return ToolResult(False, f"path does not exist: {root.relative_to(context.workspace)}")
    if root.is_file():
        return ToolResult(True, str(root.relative_to(context.workspace)))
    files = [
        str(path.relative_to(context.workspace))
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ][:100]
    return ToolResult(True, json.dumps(files, indent=2), {"count": len(files)})


def _read_file(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    path = _safe_workspace_path(context.workspace, _required_text(arguments, "path"))
    if not path.exists() or not path.is_file():
        return ToolResult(False, f"file does not exist: {path.relative_to(context.workspace)}")
    text = path.read_text(encoding="utf-8")
    truncated = text[:12_000]
    detail = "truncated" if len(text) > len(truncated) else "complete"
    return ToolResult(True, truncated, {"bytes": len(text.encode("utf-8")), "detail": detail})


def _write_file(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    path = _safe_workspace_path(context.workspace, _required_text(arguments, "path"))
    content = _required_text(arguments, "content")
    path.parent.mkdir(parents=True, exist_ok=True)
    if bool(arguments.get("append")):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(content)
    else:
        path.write_text(content, encoding="utf-8")
    return ToolResult(
        True,
        f"wrote {len(content)} characters to {path.relative_to(context.workspace)}",
    )


def _shell(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    command = arguments.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) for item in command)
    ):
        return ToolResult(False, "command must be a non-empty array of strings")
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


def _web_fetch(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    del context
    url = _required_text(arguments, "url")
    if not (url.startswith("https://") or url.startswith("http://")):
        return ToolResult(False, "url must start with http:// or https://")
    max_chars = min(int(arguments.get("max_chars") or 4000), 12_000)
    request = urllib.request.Request(url, headers={"User-Agent": "OpenBrigade/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read(max_chars + 1).decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        return ToolResult(False, f"web_fetch failed: {exc}")
    truncated = body[:max_chars]
    return ToolResult(
        True,
        truncated,
        {"detail": "truncated" if len(body) > max_chars else "complete"},
    )


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
    assignment = Assignment(
        assignment=_required_text(arguments, "assignment"),
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
    guard = _delegation_guard(context, requested_children=len(raw_subtasks))
    if guard is not None:
        return guard

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
        decision_summary=(
            f"{context.agent.agent_id} created {len(created)} child assignment(s)"
        ),
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
    return ToolResult(
        True,
        f"created {len(created)} queued subtasks",
        {"created": created},
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
