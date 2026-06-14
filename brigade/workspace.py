from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from brigade.schemas import Agent, Assignment, assignment_from_dict

REQUIRED_AGENT_FILES = ("AGENTS.md", "USER.md", "IDENTITY.md", "MEMORY.md", "TOOLS.md", "SOUL.md")
ASSIGNMENT_MARKER = "```json brigade-assignment"
ASSIGNMENT_BLOCK_RE = re.compile(
    r"```json brigade-assignment\s*\n(.*?)\n```",
    re.DOTALL,
)
# Matches any assignment block region from its marker through the next closing
# fence, or to end-of-text when the block is truncated (no closing fence). Used
# to scrub stale, malformed, or duplicate blocks before writing a fresh one.
ASSIGNMENT_BLOCK_SCRUB_RE = re.compile(
    r"```json brigade-assignment\b.*?(?:\n```|\Z)",
    re.DOTALL,
)
REQUIRED_ASSIGNMENT_FIELDS = frozenset(
    {
        "assignment",
        "assigned_to",
        "created_by",
        "source",
        "assignment_id",
        "created_at",
        "updated_at",
        "status",
    }
)


class HeartbeatValidationError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        start: int | None = None,
        end: int | None = None,
        assignment_id: str | None = None,
        assigned_to: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.start = start
        self.end = end
        self.assignment_id = assignment_id
        self.assigned_to = assigned_to


@dataclass(frozen=True)
class HeartbeatAssignmentBlock:
    assignment: Assignment
    start: int
    end: int


@dataclass(frozen=True)
class ParsedHeartbeatAssignmentBlock:
    assignment: Assignment
    raw_payload: dict[str, Any]
    start: int
    end: int

    @property
    def assignment_id(self) -> str:
        return self.assignment.assignment_id

    @property
    def assigned_to(self) -> str:
        return self.assignment.assigned_to


@dataclass(frozen=True)
class WorkspaceDiagnostic:
    severity: str
    code: str
    path: str
    message: str
    suggestion: str

    def to_dict(self) -> dict[str, str]:
        return self.__dict__.copy()


def ensure_agent_workspace(agent: Agent, root: Path) -> Path:
    workspace = root / agent.workspace_path
    workspace.mkdir(parents=True, exist_ok=True)
    for filename in REQUIRED_AGENT_FILES:
        path = workspace / filename
        if not path.exists():
            path.write_text(_default_file(agent, filename), encoding="utf-8")
    # Seeded for rest/dream cycles but deliberately not required files, so
    # heartbeat validation of existing workspaces is untouched.
    for filename, content in (
        ("reflections.md", "# Reflections\n\n"),
        (
            "PONDER.md",
            "# Ponder\n\nOpen questions; any agent may append during normal work.\n",
        ),
    ):
        path = workspace / filename
        if not path.exists():
            path.write_text(content, encoding="utf-8")
    heartbeat = workspace / "HEARTBEAT.md"
    if not heartbeat.exists():
        heartbeat.write_text(_heartbeat_header(agent), encoding="utf-8")
    return workspace


def validate_agent_workspace(agent: Agent, root: Path) -> list[WorkspaceDiagnostic]:
    workspace = root / agent.workspace_path
    diagnostics: list[WorkspaceDiagnostic] = []
    if not workspace.exists():
        return [
            WorkspaceDiagnostic(
                severity="error",
                code="workspace_missing",
                path=str(workspace),
                message=f"workspace for {agent.agent_id} does not exist",
                suggestion="Run 'brigade agent onboard' or repair the workspace explicitly.",
            )
        ]
    if not workspace.is_dir():
        return [
            WorkspaceDiagnostic(
                severity="error",
                code="workspace_not_directory",
                path=str(workspace),
                message=f"workspace path for {agent.agent_id} is not a directory",
                suggestion="Move the file aside and recreate the agent workspace.",
            )
        ]

    for filename in REQUIRED_AGENT_FILES:
        path = workspace / filename
        if not path.exists():
            diagnostics.append(
                WorkspaceDiagnostic(
                    severity="error",
                    code="required_file_missing",
                    path=str(path),
                    message=f"{filename} is missing",
                    suggestion="Run validation with --repair or recreate the workspace.",
                )
            )
        elif not path.is_file():
            diagnostics.append(
                WorkspaceDiagnostic(
                    severity="error",
                    code="required_file_not_file",
                    path=str(path),
                    message=f"{filename} is not a regular file",
                    suggestion="Replace it with a Markdown file.",
                )
            )

    heartbeat = workspace / "HEARTBEAT.md"
    if not heartbeat.exists():
        diagnostics.append(
            WorkspaceDiagnostic(
                severity="warning",
                code="heartbeat_missing",
                path=str(heartbeat),
                message="HEARTBEAT.md is missing",
                suggestion="Run validation with --repair to create a starter heartbeat.",
            )
        )
    elif not heartbeat.is_file():
        diagnostics.append(
            WorkspaceDiagnostic(
                severity="error",
                code="heartbeat_not_file",
                path=str(heartbeat),
                message="HEARTBEAT.md is not a regular file",
                suggestion="Replace it with a Markdown heartbeat file.",
            )
        )

    identity = workspace / "IDENTITY.md"
    if identity.exists() and identity.is_file():
        text = identity.read_text(encoding="utf-8")
        if agent.display_name not in text:
            diagnostics.append(
                WorkspaceDiagnostic(
                    severity="warning",
                    code="identity_name_mismatch",
                    path=str(identity),
                    message=f"IDENTITY.md does not mention display name {agent.display_name}",
                    suggestion="Review the identity file or run explicit repair.",
                )
            )
        if agent.role not in text:
            diagnostics.append(
                WorkspaceDiagnostic(
                    severity="warning",
                    code="identity_role_mismatch",
                    path=str(identity),
                    message=f"IDENTITY.md does not mention role {agent.role}",
                    suggestion="Review the identity file or run explicit repair.",
                )
            )
    return diagnostics


def read_heartbeat_assignment(path: Path) -> Assignment:
    block = extract_latest_assignment_block(path.read_text(encoding="utf-8"))
    return block.assignment


def extract_latest_assignment_block(text: str) -> HeartbeatAssignmentBlock:
    candidates: list[HeartbeatAssignmentBlock] = []
    for match in ASSIGNMENT_BLOCK_RE.finditer(text):
        payload = match.group(1).strip()
        try:
            assignment = assignment_from_dict(json.loads(payload))
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            continue
        candidates.append(
            HeartbeatAssignmentBlock(
                assignment=assignment,
                start=match.start(),
                end=match.end(),
            )
        )
    if not candidates:
        raise ValueError("no parseable assignment block found")
    return candidates[-1]


def parse_heartbeat_assignment_block(
    text: str,
    *,
    expected_agent_id: str | None = None,
) -> ParsedHeartbeatAssignmentBlock:
    marker_count = text.count(ASSIGNMENT_MARKER)
    matches = list(ASSIGNMENT_BLOCK_RE.finditer(text))
    if marker_count == 0:
        raise HeartbeatValidationError(
            "missing_assignment_block",
            "no brigade-assignment block found",
        )
    if marker_count > len(matches):
        raise HeartbeatValidationError(
            "truncated_block",
            "heartbeat assignment block is missing a closing fence",
        )
    if len(matches) > 1:
        raise HeartbeatValidationError(
            "duplicate_blocks",
            "heartbeat contains multiple brigade-assignment blocks",
            start=matches[1].start(),
            end=matches[1].end(),
        )

    match = matches[0]
    payload = match.group(1).strip()
    parsed_payload = _load_assignment_payload(payload, start=match.start(), end=match.end())
    assignment = _assignment_from_payload(parsed_payload, start=match.start(), end=match.end())
    block = ParsedHeartbeatAssignmentBlock(
        assignment=assignment,
        raw_payload=parsed_payload,
        start=match.start(),
        end=match.end(),
    )
    if expected_agent_id is not None:
        validate_heartbeat_assignment_agent(block, expected_agent_id)
    return block


def validate_heartbeat_assignment_agent(
    block: ParsedHeartbeatAssignmentBlock,
    expected_agent_id: str,
) -> None:
    if block.assigned_to != expected_agent_id:
        raise HeartbeatValidationError(
            "assigned_to_mismatch",
            f"heartbeat assignment targets {block.assigned_to}, not {expected_agent_id}",
            start=block.start,
            end=block.end,
            assignment_id=block.assignment_id,
            assigned_to=block.assigned_to,
        )


def write_heartbeat_assignment(agent: Agent, assignment: Assignment, root: Path) -> Path:
    workspace = ensure_agent_workspace(agent, root)
    heartbeat = workspace / "HEARTBEAT.md"
    existing = heartbeat.read_text(encoding="utf-8")
    block = render_assignment_block(assignment)
    # Scrub every existing assignment block -- valid, malformed, or truncated --
    # so the heartbeat is left with exactly one parseable block. Surrounding
    # notes are preserved above the block, matching the heartbeat header's
    # "Preserve any notes above the block" contract. Appending around a broken
    # block would otherwise strand malformed content and poison later recovery.
    cleaned = _strip_assignment_blocks(existing)
    updated = f"{cleaned}\n\n{block}\n" if cleaned else f"{block}\n"
    heartbeat.write_text(updated.rstrip() + "\n", encoding="utf-8")
    return heartbeat


def _strip_assignment_blocks(text: str) -> str:
    without_blocks = ASSIGNMENT_BLOCK_SCRUB_RE.sub("", text)
    collapsed = re.sub(r"\n{3,}", "\n\n", without_blocks)
    return collapsed.rstrip()


def render_assignment_block(assignment: Assignment) -> str:
    payload = json.dumps(assignment.to_dict(), indent=2, sort_keys=True)
    return f"{ASSIGNMENT_MARKER}\n{payload}\n```"


def _default_file(agent: Agent, filename: str) -> str:
    title = filename.removesuffix(".md")
    if filename == "IDENTITY.md":
        return f"# Identity\n\nName: {agent.display_name}\nRole: {agent.role}\n"
    if filename == "MEMORY.md":
        return "# Memory\n\n"
    return f"# {title.title()}\n\n"


def _heartbeat_header(agent: Agent) -> str:
    return (
        f"# Heartbeat: {agent.display_name}\n\n"
        "Work only on the active assignment block below. Preserve any notes above the block.\n"
    )


def _load_assignment_payload(payload: str, *, start: int, end: int) -> dict[str, Any]:
    try:
        raw_payload = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise HeartbeatValidationError(
            "invalid_json",
            f"heartbeat assignment block contains invalid JSON: {exc.msg}",
            start=start,
            end=end,
        ) from exc
    if not isinstance(raw_payload, dict):
        raise HeartbeatValidationError(
            "missing_required_fields",
            "heartbeat assignment block must decode to a JSON object",
            start=start,
            end=end,
        )
    missing = sorted(field for field in REQUIRED_ASSIGNMENT_FIELDS if field not in raw_payload)
    if missing:
        raise HeartbeatValidationError(
            "missing_required_fields",
            f"heartbeat assignment block is missing required fields: {', '.join(missing)}",
            start=start,
            end=end,
            assignment_id=_optional_text(raw_payload.get("assignment_id")),
            assigned_to=_optional_text(raw_payload.get("assigned_to")),
        )
    return raw_payload


def _assignment_from_payload(
    payload: dict[str, Any],
    *,
    start: int,
    end: int,
) -> Assignment:
    try:
        return assignment_from_dict(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise HeartbeatValidationError(
            "invalid_assignment_payload",
            f"heartbeat assignment block is invalid: {exc}",
            start=start,
            end=end,
            assignment_id=_optional_text(payload.get("assignment_id")),
            assigned_to=_optional_text(payload.get("assigned_to")),
        ) from exc


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None
