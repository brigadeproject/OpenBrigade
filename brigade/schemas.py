from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4

from brigade.time import utc_now_iso


def extract_json_object(text: str) -> str:
    """Best-effort extraction of a single JSON object from model output.

    Tolerates Markdown code fences (```json ... ```) and leading/trailing prose by
    returning the first balanced ``{...}`` span. Returns the stripped input
    unchanged when no object is found, so callers still ``json.loads`` and surface
    a clear error.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        body = stripped[3:]
        if body[:4].lower() == "json":
            body = body[4:]
        end = body.rfind("```")
        if end != -1:
            body = body[:end]
        stripped = body.strip()
    if stripped.startswith("{"):
        return stripped
    start = stripped.find("{")
    if start == -1:
        return stripped
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(stripped)):
        char = stripped[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return stripped[start : index + 1]
    return stripped[start:]


class AssignmentStatus(str, Enum):
    QUEUED = "queued"
    ASSIGNED = "assigned"
    WORKING = "working"
    BLOCKED = "blocked"
    COMPLETE = "complete"
    FAILED = "failed"
    ABANDONED = "abandoned"
    SUPERSEDED = "superseded"


class Priority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class AssignmentKind(str, Enum):
    MISSION = "mission"
    REST = "rest"
    MAINTENANCE = "maintenance"
    FAILURE_ANALYSIS = "failure_analysis"
    TOOL_BUILD = "tool_build"


class GoalEngagementMode(str, Enum):
    DIRECTIVE = "directive"
    ON_CALL = "on_call"


class WorkMode(str, Enum):
    HEARTBEAT = "heartbeat"
    STANDARD = "standard"
    EXTENDED = "extended"


class Role(str, Enum):
    OWNER = "owner"
    OPERATOR = "operator"
    OBSERVER = "observer"


PROPOSAL_KINDS = frozenset({"efficiency", "tool_request", "rest_insight"})
PROPOSAL_STATUSES = frozenset({"proposed", "approved", "rejected", "implemented", "expired"})

# Marker embedded in the synthetic ``last_error`` a runner writes when a provider
# never produces a parseable response after retrying. Downstream consumers (the
# blocker-resolution ladder) key off this string to tell "the model can't format
# a response" apart from a genuine work blocker, since diagnosing the former with
# another agent turn is pointless and prone to the same failure.
MALFORMED_PROVIDER_OUTPUT_MARKER = "malformed provider output"

TERMINAL_STATUSES = {
    AssignmentStatus.COMPLETE,
    AssignmentStatus.FAILED,
    AssignmentStatus.ABANDONED,
    AssignmentStatus.SUPERSEDED,
}

ALLOWED_TRANSITIONS = {
    AssignmentStatus.QUEUED: {
        AssignmentStatus.ASSIGNED,
        AssignmentStatus.BLOCKED,
        AssignmentStatus.SUPERSEDED,
    },
    AssignmentStatus.ASSIGNED: {
        AssignmentStatus.WORKING,
        AssignmentStatus.BLOCKED,
        AssignmentStatus.COMPLETE,
        AssignmentStatus.FAILED,
        AssignmentStatus.ABANDONED,
    },
    AssignmentStatus.WORKING: {
        AssignmentStatus.ASSIGNED,
        AssignmentStatus.BLOCKED,
        AssignmentStatus.COMPLETE,
        AssignmentStatus.FAILED,
        AssignmentStatus.ABANDONED,
    },
    AssignmentStatus.BLOCKED: {
        AssignmentStatus.ASSIGNED,
        AssignmentStatus.COMPLETE,
        AssignmentStatus.FAILED,
        AssignmentStatus.ABANDONED,
    },
}


@dataclass(frozen=True)
class Mission:
    statement: str
    success_criteria: list[str]
    explicitly_not: list[str]
    set_at: str = field(default_factory=utc_now_iso)
    last_reviewed: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        _require_text(self.statement, "mission statement")

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class Goal:
    statement: str
    success_criteria: list[str]
    explicitly_not: list[str]
    set_by: str
    human_confirmed: bool = False
    set_at: str = field(default_factory=utc_now_iso)
    engagement_mode: str = GoalEngagementMode.DIRECTIVE.value

    def __post_init__(self) -> None:
        _require_text(self.statement, "goal statement")
        if self.explicitly_not is None:
            raise ValueError("goal explicitly_not is required")
        if self.engagement_mode not in {mode.value for mode in GoalEngagementMode}:
            raise ValueError(f"invalid goal engagement_mode: {self.engagement_mode}")

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Assignment:
    assignment: str
    assigned_to: str
    created_by: str
    source: str
    assignment_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    work_mode: WorkMode = WorkMode.HEARTBEAT
    status: AssignmentStatus = AssignmentStatus.QUEUED
    priority: Priority = Priority.NORMAL
    kind: AssignmentKind = AssignmentKind.MISSION
    estimated_cycles: int = 1
    cycle_count: int = 0
    checkpoint_at: str | None = None
    parent_assignment_id: str | None = None
    result_artifact_ids: list[str] = field(default_factory=list)
    transcript_path: str | None = None
    state_row_written_to: str | None = None
    progress_summary: str | None = None
    blockers: list[str] = field(default_factory=list)
    consecutive_failures: int = 0
    last_error: str | None = None
    awaiting_human: bool = False
    last_run_provider: str | None = None
    last_run_model: str | None = None
    last_run_at: str | None = None
    dependency_ids: list[str] = field(default_factory=list)
    goal_statement: str | None = None
    assignment_rationale: str | None = None
    created_by_user_id: str | None = None
    created_by_role: str | None = None
    idempotency_key: str | None = None
    room_id: str | None = None
    reissued_from_assignment_id: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.assignment, "assignment")
        _require_text(self.assigned_to, "assigned_to")

    def transition_to(self, status: AssignmentStatus) -> None:
        if self.status in TERMINAL_STATUSES:
            raise ValueError(f"cannot transition terminal assignment {self.status}")
        allowed = ALLOWED_TRANSITIONS.get(self.status, set())
        if status not in allowed:
            raise ValueError(f"invalid assignment transition {self.status} -> {status}")
        self.status = status
        self.updated_at = utc_now_iso()

    def record_run(self, provider: str, model: str) -> None:
        self.last_run_provider = provider
        self.last_run_model = model
        self.last_run_at = utc_now_iso()
        self.updated_at = self.last_run_at

    def mark_cycle_incomplete(
        self,
        summary: str | None = None,
        blockers: list[str] | None = None,
    ) -> None:
        if self.status in TERMINAL_STATUSES:
            raise ValueError(f"cannot update terminal assignment {self.status}")
        self.cycle_count += 1
        self.progress_summary = summary or self.progress_summary
        self.blockers = list(blockers or self.blockers)
        self.checkpoint_at = utc_now_iso()
        self.updated_at = self.checkpoint_at
        self.awaiting_human = False
        self.status = (
            AssignmentStatus.ABANDONED if self.cycle_count >= 10 else AssignmentStatus.WORKING
        )

    def register_failure(
        self,
        error: str,
        blockers: list[str] | None = None,
        awaiting_human: bool = False,
    ) -> None:
        if self.status in TERMINAL_STATUSES:
            raise ValueError(f"cannot update terminal assignment {self.status}")
        self.consecutive_failures += 1
        self.last_error = error.strip()
        self.progress_summary = error.strip()
        self.blockers = list(blockers or self.blockers)
        self.awaiting_human = awaiting_human or self.consecutive_failures >= 5
        self.status = AssignmentStatus.BLOCKED
        self.updated_at = utc_now_iso()
        self.checkpoint_at = self.updated_at

    def mark_complete(self, summary: str) -> None:
        self.progress_summary = summary.strip()
        self.awaiting_human = False
        self.blockers = []
        self.consecutive_failures = 0
        self.last_error = None
        self.transition_to(AssignmentStatus.COMPLETE)

    def to_dict(self) -> dict[str, Any]:
        payload = self.__dict__.copy()
        payload["work_mode"] = self.work_mode.value
        payload["status"] = self.status.value
        payload["priority"] = self.priority.value
        payload["kind"] = self.kind.value
        return payload


@dataclass(frozen=True)
class AgentState:
    agent: str
    status: str = "idle"
    current_assignment_id: str | None = None
    current_assignment_summary: str | None = None
    assignment_progress: str | None = None
    blockers: list[str] = field(default_factory=list)
    last_completed: str | None = None
    next_available: str = "now"
    idle_cycles: int = 0

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class Agent:
    agent_id: str
    display_name: str
    workspace_path: str
    role: str = "line_worker"
    team_id: str | None = None
    model_provider: str = "ollama"
    model_name: str = "qwen2.5-coder:7b"
    specialties: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        _require_text(self.agent_id, "agent_id")
        _require_text(self.display_name, "display_name")
        _require_text(self.workspace_path, "workspace_path")

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class Team:
    team_id: str
    display_name: str
    description: str | None = None
    parent_team_id: str | None = None
    crew_chief_id: str | None = None
    members: list[str] = field(default_factory=list)
    delegation_policy: str = "chief_only"
    escalation_team_id: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        _require_text(self.team_id, "team_id")
        _require_text(self.display_name, "display_name")

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class User:
    username: str
    role: Role = Role.OBSERVER
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        _require_text(self.username, "username")

    def to_dict(self) -> dict[str, Any]:
        payload = self.__dict__.copy()
        payload["role"] = self.role.value
        return payload


@dataclass(frozen=True)
class ChatMessage:
    channel: str
    sender: str
    recipient: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    message_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        _require_text(self.channel, "channel")
        _require_text(self.sender, "sender")
        _require_text(self.recipient, "recipient")
        _require_text(self.content, "content")

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def mission_from_dict(item: dict[str, Any]) -> Mission:
    return Mission(
        statement=item["statement"],
        success_criteria=item.get("success_criteria", []),
        explicitly_not=item.get("explicitly_not", []),
        set_at=item["set_at"],
        last_reviewed=item["last_reviewed"],
    )


def goal_from_dict(item: dict[str, Any]) -> Goal:
    return Goal(
        statement=item["statement"],
        success_criteria=item.get("success_criteria", []),
        explicitly_not=item.get("explicitly_not", []),
        set_by=item["set_by"],
        human_confirmed=item.get("human_confirmed", False),
        set_at=item["set_at"],
        engagement_mode=item.get("engagement_mode", GoalEngagementMode.DIRECTIVE.value),
    )


def assignment_from_dict(item: dict[str, Any]) -> Assignment:
    return Assignment(
        assignment=item["assignment"],
        assigned_to=item["assigned_to"],
        created_by=item["created_by"],
        source=item["source"],
        assignment_id=item["assignment_id"],
        created_at=item["created_at"],
        updated_at=item["updated_at"],
        work_mode=WorkMode(item.get("work_mode", WorkMode.HEARTBEAT.value)),
        status=AssignmentStatus(item["status"]),
        priority=Priority(item.get("priority", Priority.NORMAL.value)),
        kind=AssignmentKind(item.get("kind", AssignmentKind.MISSION.value)),
        estimated_cycles=item.get("estimated_cycles", 1),
        cycle_count=item.get("cycle_count", 0),
        checkpoint_at=item.get("checkpoint_at"),
        parent_assignment_id=item.get("parent_assignment_id"),
        result_artifact_ids=item.get("result_artifact_ids", []),
        transcript_path=item.get("transcript_path"),
        state_row_written_to=item.get("state_row_written_to"),
        progress_summary=item.get("progress_summary"),
        blockers=item.get("blockers", []),
        consecutive_failures=item.get("consecutive_failures", 0),
        last_error=item.get("last_error"),
        awaiting_human=item.get("awaiting_human", False),
        last_run_provider=item.get("last_run_provider"),
        last_run_model=item.get("last_run_model"),
        last_run_at=item.get("last_run_at"),
        dependency_ids=item.get("dependency_ids", []),
        goal_statement=item.get("goal_statement"),
        assignment_rationale=item.get("assignment_rationale"),
        created_by_user_id=item.get("created_by_user_id"),
        created_by_role=item.get("created_by_role"),
        idempotency_key=item.get("idempotency_key"),
        room_id=item.get("room_id"),
        reissued_from_assignment_id=item.get("reissued_from_assignment_id"),
    )


def agent_state_from_dict(item: dict[str, Any]) -> AgentState:
    return AgentState(
        agent=item["agent"],
        status=item.get("status", "idle"),
        current_assignment_id=item.get("current_assignment_id"),
        current_assignment_summary=item.get("current_assignment_summary"),
        assignment_progress=item.get("assignment_progress"),
        blockers=item.get("blockers", []),
        last_completed=item.get("last_completed"),
        next_available=item.get("next_available", "now"),
        idle_cycles=item.get("idle_cycles", 0),
    )


def agent_from_dict(item: dict[str, Any]) -> Agent:
    return Agent(
        agent_id=item["agent_id"],
        display_name=item["display_name"],
        workspace_path=item["workspace_path"],
        role=item.get("role", "line_worker"),
        team_id=item.get("team_id"),
        model_provider=item.get("model_provider", "ollama"),
        model_name=item.get("model_name", "gpt-oss:20b"),
        specialties=item.get("specialties", []),
        created_at=item["created_at"],
    )


def team_from_dict(item: dict[str, Any]) -> Team:
    return Team(
        team_id=item["team_id"],
        display_name=item["display_name"],
        description=item.get("description"),
        parent_team_id=item.get("parent_team_id"),
        crew_chief_id=item.get("crew_chief_id"),
        members=item.get("members", []),
        delegation_policy=item.get("delegation_policy", "chief_only"),
        escalation_team_id=item.get("escalation_team_id"),
        created_at=item["created_at"],
        updated_at=item.get("updated_at", item["created_at"]),
    )


def user_from_dict(item: dict[str, Any]) -> User:
    return User(
        username=item["username"],
        role=Role(item.get("role", Role.OBSERVER.value)),
        created_at=item["created_at"],
    )


def chat_message_from_dict(item: dict[str, Any]) -> ChatMessage:
    return ChatMessage(
        channel=item["channel"],
        sender=item["sender"],
        recipient=item["recipient"],
        content=item["content"],
        metadata=item.get("metadata", {}),
        message_id=item["message_id"],
        created_at=item["created_at"],
    )


def build_proposal(
    *,
    kind: str,
    title: str,
    agent_id: str | None = None,
    team_id: str | None = None,
    details: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    status: str = "proposed",
) -> dict[str, Any]:
    _require_text(title, "proposal title")
    if kind not in PROPOSAL_KINDS:
        raise ValueError(f"invalid proposal kind: {kind}")
    if status not in PROPOSAL_STATUSES:
        raise ValueError(f"invalid proposal status: {status}")
    now = utc_now_iso()
    return {
        "proposal_id": str(uuid4()),
        "kind": kind,
        "status": status,
        "title": title,
        "agent_id": agent_id,
        "team_id": team_id,
        "details": details or {},
        "idempotency_key": idempotency_key,
        "created_at": now,
        "updated_at": now,
        "decided_by": None,
        "decided_at": None,
    }


def build_recurrence(
    *,
    template: dict[str, Any],
    interval_seconds: int,
    next_due_at: str,
    proposal_id: str | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    if not isinstance(template, dict) or not str(template.get("assignment") or "").strip():
        raise ValueError("recurrence template requires assignment text")
    if interval_seconds <= 0:
        raise ValueError("recurrence interval_seconds must be positive")
    _require_text(next_due_at, "recurrence next_due_at")
    now = utc_now_iso()
    return {
        "recurrence_id": str(uuid4()),
        "enabled": enabled,
        "interval_seconds": int(interval_seconds),
        "next_due_at": next_due_at,
        "template": dict(template),
        "proposal_id": proposal_id,
        "created_at": now,
        "updated_at": now,
        "last_materialized_at": None,
    }


def _require_text(value: str, name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{name} is required")
