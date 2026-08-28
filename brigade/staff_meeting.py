"""Deterministic, durable Staff Meeting orchestration primitives.

The module owns governance and state validation. Model prompts and daemon
advancement build on these primitives; they never get to redefine quorum,
voting, veto, roster, or audit behavior in prose.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Iterable
from enum import Enum
from typing import Any
from uuid import uuid4

from brigade.schemas import (
    TERMINAL_STATUSES,
    Agent,
    Assignment,
    AssignmentKind,
    AssignmentStatus,
    ChatMessage,
    Conversation,
    Priority,
    WorkMode,
    extract_json_object,
)
from brigade.store import StateStore
from brigade.time import parse_utc_iso, utc_now, utc_now_iso

ROLE_CATALOG_VERSION = "staff-meeting-roles:v1"
RECORD_SCHEMA_VERSION = 1
DEFAULT_MIN_SEATS = 5
DEFAULT_MAX_SEATS = 12
DEFAULT_QUORUM_RATIO = 0.70
DEFAULT_MIN_DISTINCT_AGENTS = 3
DEFAULT_MIN_SUBSTANTIVE_VOTES = 3


class MeetingStatus(str, Enum):
    DRAFT = "DRAFT"
    ROSTER_PROPOSED = "ROSTER_PROPOSED"
    ROSTER_APPROVED = "ROSTER_APPROVED"
    PACKET_FROZEN = "PACKET_FROZEN"
    INDEPENDENT_REVIEW = "INDEPENDENT_REVIEW"
    SYNTHESIS = "SYNTHESIS"
    DELIBERATION = "DELIBERATION"
    TARGETED_FOLLOW_UP = "TARGETED_FOLLOW_UP"
    FINAL_VOTE = "FINAL_VOTE"
    VETO_PENDING = "VETO_PENDING"
    FINAL_REPORT = "FINAL_REPORT"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_DISSENT = "COMPLETED_WITH_DISSENT"
    COMPLETED_NO_CONSENSUS = "COMPLETED_NO_CONSENSUS"
    MOTION_NOT_ADOPTED = "MOTION_NOT_ADOPTED"
    BLOCKED_BY_VETO = "BLOCKED_BY_VETO"
    FAILED_NO_QUORUM = "FAILED_NO_QUORUM"
    CANCELLED = "CANCELLED"
    RESOURCE_LIMIT_REACHED = "RESOURCE_LIMIT_REACHED"


TERMINAL_MEETING_STATUSES = frozenset(
    {
        MeetingStatus.COMPLETED.value,
        MeetingStatus.COMPLETED_WITH_DISSENT.value,
        MeetingStatus.COMPLETED_NO_CONSENSUS.value,
        MeetingStatus.MOTION_NOT_ADOPTED.value,
        MeetingStatus.BLOCKED_BY_VETO.value,
        MeetingStatus.FAILED_NO_QUORUM.value,
        MeetingStatus.CANCELLED.value,
        MeetingStatus.RESOURCE_LIMIT_REACHED.value,
    }
)

ALLOWED_MEETING_TRANSITIONS: dict[str, frozenset[str]] = {
    MeetingStatus.DRAFT.value: frozenset({MeetingStatus.ROSTER_PROPOSED.value}),
    MeetingStatus.ROSTER_PROPOSED.value: frozenset({MeetingStatus.ROSTER_APPROVED.value}),
    MeetingStatus.ROSTER_APPROVED.value: frozenset({MeetingStatus.PACKET_FROZEN.value}),
    MeetingStatus.PACKET_FROZEN.value: frozenset({MeetingStatus.INDEPENDENT_REVIEW.value}),
    MeetingStatus.INDEPENDENT_REVIEW.value: frozenset(
        {
            MeetingStatus.SYNTHESIS.value,
            MeetingStatus.FAILED_NO_QUORUM.value,
            MeetingStatus.RESOURCE_LIMIT_REACHED.value,
        }
    ),
    MeetingStatus.SYNTHESIS.value: frozenset(
        {
            MeetingStatus.DELIBERATION.value,
            MeetingStatus.TARGETED_FOLLOW_UP.value,
            MeetingStatus.FINAL_VOTE.value,
            MeetingStatus.VETO_PENDING.value,
            MeetingStatus.RESOURCE_LIMIT_REACHED.value,
        }
    ),
    MeetingStatus.DELIBERATION.value: frozenset(
        {
            MeetingStatus.SYNTHESIS.value,
            MeetingStatus.FINAL_VOTE.value,
            MeetingStatus.FAILED_NO_QUORUM.value,
            MeetingStatus.RESOURCE_LIMIT_REACHED.value,
        }
    ),
    MeetingStatus.TARGETED_FOLLOW_UP.value: frozenset(
        {
            MeetingStatus.SYNTHESIS.value,
            MeetingStatus.FAILED_NO_QUORUM.value,
            MeetingStatus.RESOURCE_LIMIT_REACHED.value,
        }
    ),
    MeetingStatus.FINAL_VOTE.value: frozenset(
        {
            MeetingStatus.VETO_PENDING.value,
            MeetingStatus.FINAL_REPORT.value,
            MeetingStatus.FAILED_NO_QUORUM.value,
            MeetingStatus.RESOURCE_LIMIT_REACHED.value,
        }
    ),
    MeetingStatus.VETO_PENDING.value: frozenset(
        {
            MeetingStatus.SYNTHESIS.value,
            MeetingStatus.FINAL_VOTE.value,
            MeetingStatus.BLOCKED_BY_VETO.value,
            MeetingStatus.RESOURCE_LIMIT_REACHED.value,
        }
    ),
    MeetingStatus.FINAL_REPORT.value: frozenset(
        {
            MeetingStatus.SYNTHESIS.value,
            MeetingStatus.COMPLETED.value,
            MeetingStatus.COMPLETED_WITH_DISSENT.value,
            MeetingStatus.COMPLETED_NO_CONSENSUS.value,
            MeetingStatus.MOTION_NOT_ADOPTED.value,
            MeetingStatus.BLOCKED_BY_VETO.value,
        }
    ),
}


ROLE_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "role_key": "chair",
        "role_family": "leadership",
        "lens": "Define acceptance criteria, facilitate, synthesize, evaluate, and vote.",
        "keywords": ["decision", "plan", "coordinate", "acceptance", "scope"],
        "veto_domain": None,
    },
    {
        "role_key": "designer",
        "role_family": "design_build",
        "lens": "Architecture, coherence, interfaces, usability, and fit to purpose.",
        "keywords": ["architecture", "design", "interface", "user", "workflow", "api"],
        "veto_domain": None,
    },
    {
        "role_key": "engineer",
        "role_family": "design_build",
        "lens": "Technical feasibility, implementation, dependencies, and tradeoffs.",
        "keywords": ["implement", "code", "technical", "dependency", "build", "system"],
        "veto_domain": None,
    },
    {
        "role_key": "critic",
        "role_family": "counterpoint",
        "lens": "Contradictions, weak assumptions, failure modes, and counterarguments.",
        "keywords": ["risk", "failure", "assumption", "tradeoff", "decision"],
        "veto_domain": None,
    },
    {
        "role_key": "operator",
        "role_family": "operations",
        "lens": "Deployment, maintenance, observability, recovery, and operational cost.",
        "keywords": ["deploy", "operate", "runtime", "recover", "monitor", "maintenance"],
        "veto_domain": None,
    },
    {
        "role_key": "security_reviewer",
        "role_family": "security",
        "lens": "Threats, permissions, data exposure, abuse cases, and security vetoes.",
        "keywords": ["security", "secret", "permission", "auth", "data", "threat", "abuse"],
        "veto_domain": "security",
    },
    {
        "role_key": "safety_reviewer",
        "role_family": "safety",
        "lens": "Physical or systemic harms, unsafe states, and safety vetoes.",
        "keywords": ["safety", "harm", "physical", "systemic", "unsafe"],
        "veto_domain": "safety",
    },
    {
        "role_key": "ethicist",
        "role_family": "accountability",
        "lens": "Stakeholders, rights, distributional effects, legitimacy, and governance.",
        "keywords": ["ethics", "rights", "stakeholder", "governance", "fairness"],
        "veto_domain": None,
    },
    {
        "role_key": "researcher",
        "role_family": "evidence",
        "lens": "Evidence quality, factual uncertainty, provenance, and unanswered questions.",
        "keywords": ["research", "evidence", "source", "uncertain", "fact", "compare"],
        "veto_domain": None,
    },
    {
        "role_key": "budget_analyst",
        "role_family": "resources",
        "lens": "Financial, compute, time, staffing, and opportunity costs.",
        "keywords": ["budget", "cost", "compute", "time", "staff", "resource"],
        "veto_domain": None,
    },
    {
        "role_key": "devils_advocate",
        "role_family": "counterpoint",
        "lens": "Strongest competing approach and adversarial challenge.",
        "keywords": ["alternative", "competing", "challenge", "adversarial", "decision"],
        "veto_domain": None,
    },
    {
        "role_key": "user_advocate",
        "role_family": "accountability",
        "lens": "Alignment with the original request and real user needs.",
        "keywords": ["user", "experience", "request", "need", "usability", "workflow"],
        "veto_domain": None,
    },
)

_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|"
    r"authorization|cookie)\b(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_KNOWN_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[opusr]_[A-Za-z0-9]{20,}|"
    r"[0-9]{6,12}:[A-Za-z0-9_-]{20,})\b"
)


def default_role_catalog() -> dict[str, Any]:
    return {
        "version": ROLE_CATALOG_VERSION,
        "active": True,
        "created_at": utc_now_iso(),
        "roles": [dict(item) for item in ROLE_DEFINITIONS],
    }


def ensure_default_role_catalog(store: StateStore) -> dict[str, Any]:
    existing = next(
        (
            item
            for item in store.staff_meeting_role_catalogs()
            if item.get("version") == ROLE_CATALOG_VERSION
        ),
        None,
    )
    if existing is not None:
        return existing
    catalog = default_role_catalog()
    store.upsert_staff_meeting_role_catalog(catalog)
    return catalog


def redact_sensitive_text(value: str) -> str:
    redacted = _SECRET_ASSIGNMENT_RE.sub(r"\1\2***redacted***", value)
    redacted = _BEARER_RE.sub("Bearer ***redacted***", redacted)
    return _KNOWN_TOKEN_RE.sub("***redacted***", redacted)


def redact_sensitive_data(value: Any) -> Any:
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, list):
        return [redact_sensitive_data(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive_data(item) for item in value]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in ("token", "secret", "password", "api_key")):
                result[str(key)] = "***redacted***"
            else:
                result[str(key)] = redact_sensitive_data(item)
        return result
    return value


def quorum_required(seat_count: int, ratio: float = DEFAULT_QUORUM_RATIO) -> int:
    if seat_count < DEFAULT_MIN_SEATS:
        raise ValueError(f"Staff Meeting requires at least {DEFAULT_MIN_SEATS} seats")
    if not 0 < ratio <= 1:
        raise ValueError("quorum ratio must be greater than zero and at most one")
    return math.ceil(seat_count * ratio)


def evaluate_vote(
    ballots: Iterable[dict[str, Any]],
    *,
    seat_count: int,
    quorum_ratio: float = DEFAULT_QUORUM_RATIO,
    min_distinct_agents: int = DEFAULT_MIN_DISTINCT_AGENTS,
    min_substantive_votes: int = DEFAULT_MIN_SUBSTANTIVE_VOTES,
    unresolved_vetoes: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    effective = list(ballots)
    responded_agents = {
        str(item.get("agent_id")) for item in effective if item.get("agent_id")
    }
    required_quorum = quorum_required(seat_count, quorum_ratio)
    quorum = len(effective) >= required_quorum and len(responded_agents) >= min_distinct_agents
    counts = Counter(str(item.get("vote") or "").lower() for item in effective)
    support = counts["support"]
    oppose = counts["oppose"]
    abstain = counts["abstain"]
    substantive = support + oppose
    substantive_agents = {
        str(item.get("agent_id"))
        for item in effective
        if str(item.get("vote") or "").lower() in {"support", "oppose"}
        and item.get("agent_id")
    }
    required_supermajority = math.ceil((2 / 3) * substantive) if substantive else 0
    has_substantive_floor = (
        substantive >= min_substantive_votes
        and len(substantive_agents) >= min_distinct_agents
    )
    vetoes = [item for item in unresolved_vetoes if item.get("status") != "withdrawn"]
    if not quorum:
        outcome = MeetingStatus.FAILED_NO_QUORUM.value
    elif vetoes:
        outcome = MeetingStatus.BLOCKED_BY_VETO.value
    elif not has_substantive_floor:
        outcome = MeetingStatus.COMPLETED_NO_CONSENSUS.value
    elif support >= required_supermajority:
        outcome = (
            MeetingStatus.COMPLETED_WITH_DISSENT.value
            if oppose or abstain
            else MeetingStatus.COMPLETED.value
        )
    elif oppose >= required_supermajority:
        outcome = MeetingStatus.MOTION_NOT_ADOPTED.value
    else:
        outcome = MeetingStatus.MOTION_NOT_ADOPTED.value
    return {
        "outcome": outcome,
        "quorum": quorum,
        "responses": len(effective),
        "required_quorum": required_quorum,
        "distinct_agents": len(responded_agents),
        "support": support,
        "oppose": oppose,
        "abstain": abstain,
        "substantive_votes": substantive,
        "substantive_agents": len(substantive_agents),
        "required_supermajority": required_supermajority,
        "consensus_support": bool(
            quorum and not vetoes and has_substantive_floor and support >= required_supermajority
        ),
        "consensus_oppose": bool(
            quorum and not vetoes and has_substantive_floor and oppose >= required_supermajority
        ),
        "unresolved_veto_count": len(vetoes),
    }


def _role_relevance(role: dict[str, Any], request: str) -> tuple[int, str]:
    haystack = request.lower()
    score = sum(1 for keyword in role.get("keywords", []) if keyword in haystack)
    return (-score, str(role["role_key"]))


def _agent_role_score(agent: Agent, role: dict[str, Any]) -> int:
    haystack = " ".join(
        [
            agent.agent_id,
            agent.display_name,
            agent.role,
            *agent.specialties,
        ]
    ).lower()
    return sum(1 for keyword in role.get("keywords", []) if keyword in haystack)


def propose_roster(
    request: str,
    *,
    chair_agent_id: str,
    eligible_agents: Iterable[Agent],
    seat_count: int = DEFAULT_MIN_SEATS,
    catalog: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    catalog = catalog or default_role_catalog()
    roles = [dict(item) for item in catalog["roles"]]
    if not DEFAULT_MIN_SEATS <= seat_count <= min(DEFAULT_MAX_SEATS, len(roles)):
        raise ValueError(
            f"seat_count must be between {DEFAULT_MIN_SEATS} and "
            f"{min(DEFAULT_MAX_SEATS, len(roles))}"
        )
    agents = {item.agent_id: item for item in eligible_agents}
    if chair_agent_id not in agents:
        raise ValueError("chair must be an eligible agent")
    if len(agents) < DEFAULT_MIN_DISTINCT_AGENTS:
        raise ValueError("a Staff Meeting roster needs at least three distinct agents")
    chair_role = next(item for item in roles if item["role_key"] == "chair")
    specialists = sorted(
        (item for item in roles if item["role_key"] != "chair"),
        key=lambda item: _role_relevance(item, request),
    )[: seat_count - 1]
    selected_roles = [chair_role, *specialists]
    roster: list[dict[str, Any]] = []
    use_count: Counter[str] = Counter()
    veto_agents: set[str] = set()
    for index, role in enumerate(selected_roles):
        if role["role_key"] == "chair":
            selected_agent = agents[chair_agent_id]
        else:
            candidates = [
                item
                for item in agents.values()
                if not (role.get("veto_domain") and item.agent_id in veto_agents)
            ]
            if not candidates:
                raise ValueError("security and safety veto roles require different agents")
            candidates.sort(
                key=lambda item: (
                    use_count[item.agent_id],
                    -_agent_role_score(item, role),
                    item.agent_id,
                )
            )
            selected_agent = candidates[0]
        use_count[selected_agent.agent_id] += 1
        if role.get("veto_domain"):
            veto_agents.add(selected_agent.agent_id)
        roster.append(
            {
                "role_seat_id": str(uuid4()),
                "seat_number": index + 1,
                "role_key": role["role_key"],
                "role_family": role["role_family"],
                "lens": role["lens"],
                "agent_id": selected_agent.agent_id,
                "agent_display_name": selected_agent.display_name,
                "veto_domain": role.get("veto_domain"),
                "catalog_version": catalog["version"],
                "reason": "Selected by deterministic request relevance and agent specialty fit.",
            }
        )
    warnings: list[str] = []
    family_by_agent: dict[str, Counter[str]] = {}
    for seat in roster:
        family_by_agent.setdefault(seat["agent_id"], Counter())[seat["role_family"]] += 1
    for agent_id, families in family_by_agent.items():
        for family, count in families.items():
            if count > 1:
                warnings.append(
                    f"agent {agent_id} holds {count} seats in role family {family}"
                )
    return roster, warnings


def _append_record(
    store: StateStore,
    meeting: dict[str, Any],
    record_kind: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str,
    role_seat_id: str | None = None,
    role_key: str | None = None,
    round_number: int | None = None,
    phase: str | None = None,
    assignment_id: str | None = None,
    agent_id: str | None = None,
    supersedes_record_id: str | None = None,
) -> dict[str, Any]:
    existing = next(
        (
            item
            for item in store.staff_meeting_records(meeting["meeting_id"])
            if item.get("idempotency_key") == idempotency_key
        ),
        None,
    )
    if existing is not None:
        store.add_staff_meeting_record(
            {
                "record_id": str(uuid4()),
                "schema_version": RECORD_SCHEMA_VERSION,
                "meeting_id": meeting["meeting_id"],
                "conversation_id": meeting["conversation_id"],
                "record_kind": "event",
                "role_seat_id": role_seat_id,
                "role_key": role_key,
                "round_number": round_number,
                "phase": phase,
                "assignment_id": assignment_id,
                "agent_id": agent_id,
                "supersedes_record_id": existing["record_id"],
                "idempotency_key": f"idempotency-replay:{idempotency_key}:{uuid4()}",
                "payload": {
                    "event_type": "idempotency_replay",
                    "replayed_idempotency_key": idempotency_key,
                    "existing_record_id": existing["record_id"],
                },
                "created_at": utc_now_iso(),
            }
        )
        return existing
    record = {
        "record_id": str(uuid4()),
        "schema_version": RECORD_SCHEMA_VERSION,
        "meeting_id": meeting["meeting_id"],
        "conversation_id": meeting["conversation_id"],
        "record_kind": record_kind,
        "role_seat_id": role_seat_id,
        "role_key": role_key,
        "round_number": round_number,
        "phase": phase,
        "assignment_id": assignment_id,
        "agent_id": agent_id,
        "supersedes_record_id": supersedes_record_id,
        "idempotency_key": idempotency_key,
        "payload": redact_sensitive_data(payload),
        "created_at": utc_now_iso(),
    }
    return store.add_staff_meeting_record(record)


def _record_transition(
    store: StateStore,
    meeting: dict[str, Any],
    previous: str | None,
    current: str,
    *,
    actor_id: str,
    reason: str,
) -> dict[str, Any]:
    return _append_record(
        store,
        meeting,
        "event",
        {
            "event_type": "state_transition",
            "previous_status": previous,
            "status": current,
            "actor_id": actor_id,
            "reason": reason,
        },
        idempotency_key=(
            f"transition:{previous or 'NONE'}:{current}:packet:"
            f"{meeting.get('packet_version', 0)}:full:"
            f"{meeting.get('current_round', 0)}:follow:"
            f"{meeting.get('follow_up_round', 0)}:ballot:"
            f"{meeting.get('ballot_round', 0)}:wave:"
            f"{meeting.get('wave_sequence', 0)}"
        ),
        phase=current,
        agent_id=actor_id,
    )


def transition_meeting(
    store: StateStore,
    meeting: dict[str, Any],
    status: str,
    *,
    expected_status: str,
    actor_id: str,
    reason: str,
) -> dict[str, Any]:
    current = str(meeting["status"])
    if current == status:
        return meeting
    if current != expected_status:
        raise ValueError(f"expected meeting status {expected_status}, found {current}")
    allowed = ALLOWED_MEETING_TRANSITIONS.get(current, frozenset())
    if status not in allowed:
        raise ValueError(f"invalid Staff Meeting transition: {current} -> {status}")
    meeting["status"] = status
    meeting["updated_at"] = utc_now_iso()
    if status in TERMINAL_MEETING_STATUSES:
        meeting["completed_at"] = meeting["updated_at"]
    store.upsert_staff_meeting(meeting)
    _record_transition(
        store,
        meeting,
        current,
        status,
        actor_id=actor_id,
        reason=reason,
    )
    return meeting


def create_staff_meeting(
    store: StateStore,
    *,
    original_request: str,
    acceptance_criteria: Iterable[str],
    chair_agent_id: str,
    owner_username: str,
    caller_kind: str,
    eligible_agents: Iterable[Agent],
    team_id: str | None = None,
    scope: dict[str, Any] | None = None,
    desired_deliverable: str | None = None,
    known_constraints: Iterable[str] = (),
    declared_assumptions: Iterable[str] = (),
    evidence_refs: Iterable[dict[str, Any] | str] = (),
    seat_count: int = DEFAULT_MIN_SEATS,
    resource_limits: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    request = redact_sensitive_text(original_request.strip())
    if not request:
        raise ValueError("original_request is required")
    criteria = [redact_sensitive_text(str(item).strip()) for item in acceptance_criteria]
    criteria = [item for item in criteria if item]
    if not criteria:
        raise ValueError("acceptance_criteria must contain at least one item")
    if caller_kind not in {"executive", "crew_chief"}:
        raise ValueError("only an Executive or Crew Chief may convene a Staff Meeting")
    if idempotency_key:
        duplicate = next(
            (
                item
                for item in store.staff_meetings(owner_username=owner_username)
                if item.get("idempotency_key") == idempotency_key
            ),
            None,
        )
        if duplicate is not None:
            return duplicate
    catalog = ensure_default_role_catalog(store)
    eligible_agents_list = list(eligible_agents)
    roster, warnings = propose_roster(
        request,
        chair_agent_id=chair_agent_id,
        eligible_agents=eligible_agents_list,
        seat_count=seat_count,
        catalog=catalog,
    )
    meeting_id = str(uuid4())
    conversation = Conversation(
        operator_username=owner_username,
        persona=f"staff_meeting:{meeting_id}",
        chief_agent_id=chair_agent_id if caller_kind == "crew_chief" else None,
        team_id=team_id,
        title=f"Staff Meeting: {request[:72]}",
    )
    store.upsert_conversation(conversation)
    now = utc_now_iso()
    limit_defaults = {
        "max_deliberation_rounds": 3,
        "max_follow_up_discussions": 1,
        "max_follow_up_rounds": 3,
        "max_elapsed_seconds": 7200,
        "max_total_tokens": 100_000,
        "max_tool_calls": 60,
    }
    limits = dict(limit_defaults)
    for key, value in (resource_limits or {}).items():
        if key not in limit_defaults:
            raise ValueError(f"unsupported Staff Meeting resource limit: {key}")
        try:
            requested = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Staff Meeting resource limit {key} must be an integer") from exc
        if requested < 1 or requested > limit_defaults[key]:
            raise ValueError(
                f"Staff Meeting resource limit {key} must be between 1 and "
                f"{limit_defaults[key]}"
            )
        limits[key] = requested
    meeting = {
        "meeting_id": meeting_id,
        "conversation_id": conversation.thread_id,
        "conversation_channel": conversation.channel,
        "owner_username": owner_username,
        "team_id": team_id,
        "caller_kind": caller_kind,
        "chair_agent_id": chair_agent_id,
        "status": MeetingStatus.DRAFT.value,
        "original_request": request,
        "original_request_hash": hashlib.sha256(request.encode("utf-8")).hexdigest(),
        "acceptance_criteria": criteria,
        "scope": redact_sensitive_data(scope or {}),
        "desired_deliverable": redact_sensitive_text(desired_deliverable or "Decision report"),
        "known_constraints": redact_sensitive_data(list(known_constraints)),
        "declared_assumptions": redact_sensitive_data(list(declared_assumptions)),
        "evidence_refs": redact_sensitive_data(list(evidence_refs)),
        "eligible_agent_ids": sorted(item.agent_id for item in eligible_agents_list),
        "proposed_roster": roster,
        "approved_roster": [],
        "roster_warnings": warnings,
        "seat_count": len(roster),
        "quorum_required": quorum_required(len(roster)),
        "minimum_distinct_agents": DEFAULT_MIN_DISTINCT_AGENTS,
        "minimum_substantive_votes": DEFAULT_MIN_SUBSTANTIVE_VOTES,
        "quorum_ratio": DEFAULT_QUORUM_RATIO,
        "role_catalog_version": catalog["version"],
        "packet_version": 0,
        "current_round": 0,
        "follow_up_discussions_used": 0,
        "ballot_round": 0,
        "ballots_revealed": False,
        "revealed_ballot_rounds": [],
        "resource_limits": limits,
        "idempotency_key": idempotency_key,
        "termination_reason": None,
        "final_report_record_id": None,
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
    }
    store.upsert_staff_meeting(meeting)
    _record_transition(
        store,
        meeting,
        None,
        MeetingStatus.DRAFT.value,
        actor_id=chair_agent_id,
        reason="meeting created",
    )
    transition_meeting(
        store,
        meeting,
        MeetingStatus.ROSTER_PROPOSED.value,
        expected_status=MeetingStatus.DRAFT.value,
        actor_id="orchestrator",
        reason="deterministic catalog roster proposed",
    )
    for seat in roster:
        _append_record(
            store,
            meeting,
            "role_seat",
            seat,
            idempotency_key=f"seat:{seat['role_seat_id']}",
            role_seat_id=seat["role_seat_id"],
            role_key=seat["role_key"],
            phase=MeetingStatus.ROSTER_PROPOSED.value,
            agent_id=seat["agent_id"],
        )
    store.add_message(
        ChatMessage(
            channel=conversation.channel,
            sender=chair_agent_id,
            recipient="staff_meeting",
            content=request,
            metadata={
                "kind": "staff_meeting_request",
                "meeting_id": meeting_id,
                "conversation_id": conversation.thread_id,
                "request_hash": meeting["original_request_hash"],
            },
        )
    )
    return meeting


def approve_roster(
    store: StateStore,
    meeting_id: str,
    *,
    actor_id: str,
    roster: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    meeting = store.find_staff_meeting(meeting_id)
    if meeting is None:
        raise ValueError(f"unknown Staff Meeting: {meeting_id}")
    if actor_id != meeting["chair_agent_id"]:
        raise PermissionError("only the chair may approve the Staff Meeting roster")
    selected = _normalize_roster(store, meeting, roster or meeting["proposed_roster"])
    _validate_roster(selected, meeting["chair_agent_id"])
    meeting["approved_roster"] = selected
    meeting["seat_count"] = len(selected)
    meeting["quorum_required"] = quorum_required(len(selected))
    meeting["updated_at"] = utc_now_iso()
    store.upsert_staff_meeting(meeting)
    return transition_meeting(
        store,
        meeting,
        MeetingStatus.ROSTER_APPROVED.value,
        expected_status=MeetingStatus.ROSTER_PROPOSED.value,
        actor_id=actor_id,
        reason="chair approved roster",
    )


def _normalize_roster(
    store: StateStore,
    meeting: dict[str, Any],
    roster: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    definitions = {item["role_key"]: item for item in ROLE_DEFINITIONS}
    agents = {item.agent_id: item for item in store.agents()}
    eligible = set(meeting.get("eligible_agent_ids") or agents)
    normalized: list[dict[str, Any]] = []
    for index, supplied in enumerate(roster):
        role_key = str(supplied.get("role_key") or "")
        role = definitions.get(role_key)
        if role is None:
            raise ValueError("roster contains a role outside the fixed catalog")
        agent_id = str(supplied.get("agent_id") or "")
        agent = agents.get(agent_id)
        if agent is None or agent_id not in eligible:
            raise ValueError("roster contains an ineligible agent assignment")
        normalized.append(
            {
                "role_seat_id": str(supplied.get("role_seat_id") or uuid4()),
                "seat_number": index + 1,
                "role_key": role_key,
                "role_family": role["role_family"],
                "lens": role["lens"],
                "agent_id": agent_id,
                "agent_display_name": agent.display_name,
                "veto_domain": role.get("veto_domain"),
                "catalog_version": meeting["role_catalog_version"],
                "reason": str(
                    supplied.get("reason")
                    or "Selected by the chair from the fixed role catalog."
                ),
            }
        )
    return normalized


def _validate_roster(roster: list[dict[str, Any]], chair_agent_id: str) -> None:
    if not DEFAULT_MIN_SEATS <= len(roster) <= DEFAULT_MAX_SEATS:
        raise ValueError(
            f"approved roster must contain {DEFAULT_MIN_SEATS} to {DEFAULT_MAX_SEATS} seats"
        )
    role_keys = [str(item.get("role_key") or "") for item in roster]
    if len(role_keys) != len(set(role_keys)):
        raise ValueError("each catalog role may occupy at most one seat")
    definitions = {item["role_key"]: item for item in ROLE_DEFINITIONS}
    if any(role not in definitions for role in role_keys):
        raise ValueError("roster contains a role outside the fixed catalog")
    for seat in roster:
        definition = definitions[seat["role_key"]]
        if any(
            seat.get(key) != definition.get(key)
            for key in ("role_family", "lens", "veto_domain")
        ):
            raise ValueError("roster role policy must match the fixed catalog")
    chairs = [item for item in roster if item.get("role_key") == "chair"]
    if len(chairs) != 1 or chairs[0].get("agent_id") != chair_agent_id:
        raise ValueError("the calling agent must occupy the single chair seat")
    agents = {str(item.get("agent_id") or "") for item in roster}
    if "" in agents or len(agents) < DEFAULT_MIN_DISTINCT_AGENTS:
        raise ValueError("approved roster needs at least three distinct agents")
    veto_assignments = [item for item in roster if item.get("veto_domain")]
    veto_agents = [str(item["agent_id"]) for item in veto_assignments]
    if len(veto_agents) != len(set(veto_agents)):
        raise ValueError("one agent may hold at most one veto-capable role")


def freeze_packet(store: StateStore, meeting_id: str, *, actor_id: str) -> dict[str, Any]:
    meeting = store.find_staff_meeting(meeting_id)
    if meeting is None:
        raise ValueError(f"unknown Staff Meeting: {meeting_id}")
    if actor_id != meeting["chair_agent_id"]:
        raise PermissionError("only the chair may freeze the meeting packet")
    if meeting["status"] != MeetingStatus.ROSTER_APPROVED.value:
        raise ValueError("packet may only be frozen after roster approval")
    meeting["packet_version"] = int(meeting.get("packet_version") or 0) + 1
    packet = {
        "packet_version": meeting["packet_version"],
        "original_request": meeting["original_request"],
        "original_request_hash": meeting["original_request_hash"],
        "acceptance_criteria": meeting["acceptance_criteria"],
        "scope": meeting["scope"],
        "desired_deliverable": meeting["desired_deliverable"],
        "known_constraints": meeting["known_constraints"],
        "declared_assumptions": meeting["declared_assumptions"],
        "evidence_refs": meeting["evidence_refs"],
        "approved_roster": meeting["approved_roster"],
        "resource_limits": meeting["resource_limits"],
        "quorum_ratio": meeting["quorum_ratio"],
        "quorum_required": meeting["quorum_required"],
        "minimum_distinct_agents": meeting["minimum_distinct_agents"],
        "minimum_substantive_votes": meeting["minimum_substantive_votes"],
        "role_catalog_version": meeting["role_catalog_version"],
        "prompt_version": "staff-meeting-prompts:v1",
        "frozen_at": utc_now_iso(),
        "frozen_by": actor_id,
    }
    _append_record(
        store,
        meeting,
        "packet",
        packet,
        idempotency_key=f"packet:{meeting['packet_version']}",
        phase=MeetingStatus.PACKET_FROZEN.value,
        agent_id=actor_id,
    )
    return transition_meeting(
        store,
        meeting,
        MeetingStatus.PACKET_FROZEN.value,
        expected_status=MeetingStatus.ROSTER_APPROVED.value,
        actor_id=actor_id,
        reason=f"packet version {meeting['packet_version']} frozen",
    )


def meeting_packet(store: StateStore, meeting_id: str) -> dict[str, Any]:
    packets = store.staff_meeting_records(meeting_id, record_kind="packet")
    if not packets:
        raise ValueError("Staff Meeting has no frozen packet")
    return dict(packets[-1]["payload"])


def meeting_public_view(
    store: StateStore,
    meeting: dict[str, Any],
    *,
    include_records: bool = False,
) -> dict[str, Any]:
    payload = dict(meeting)
    records = store.staff_meeting_records(meeting["meeting_id"])
    ballots_revealed = bool(meeting.get("ballots_revealed"))
    revealed_rounds = {
        int(item) for item in meeting.get("revealed_ballot_rounds", [])
    }
    if include_records:
        payload["records"] = [
            item
            for item in records
            if (
                item.get("record_kind") != "ballot"
                or ballots_revealed
                or int(item.get("round_number") or 0) in revealed_rounds
            )
        ]
    payload["record_counts"] = dict(
        Counter(str(item.get("record_kind")) for item in records)
    )
    return payload


def append_meeting_record(
    store: StateStore,
    meeting: dict[str, Any],
    record_kind: str,
    payload: dict[str, Any],
    **metadata: Any,
) -> dict[str, Any]:
    """Harness-facing append helper with the same redaction/idempotency gates."""
    return _append_record(store, meeting, record_kind, payload, **metadata)


def _role_seat(meeting: dict[str, Any], role_seat_id: str) -> dict[str, Any]:
    seat = next(
        (
            item
            for item in meeting.get("approved_roster", [])
            if item.get("role_seat_id") == role_seat_id
        ),
        None,
    )
    if seat is None:
        raise ValueError(f"unknown role seat: {role_seat_id}")
    return seat


def _phase_output_contract(phase: str) -> dict[str, Any]:
    if phase == MeetingStatus.INDEPENDENT_REVIEW.value:
        return {
            "disposition": "support | revise | oppose | abstain",
            "summary": "string",
            "findings": ["string"],
            "risks": ["string"],
            "assumptions": ["string"],
            "evidence_refs": ["string or structured source reference"],
            "proposed_changes": ["string"],
            "open_questions": ["string"],
            "veto": None,
            "confidence": 0.0,
        }
    if phase == MeetingStatus.SYNTHESIS.value:
        return {
            "areas_of_agreement": ["string"],
            "material_disagreements": ["string"],
            "unique_findings": ["string"],
            "conflicting_assumptions": ["string"],
            "evidence_gaps": ["string"],
            "proposed_combined_solution": "string",
            "rejected_alternatives": [{"alternative": "string", "reason": "string"}],
            "unresolved_questions": ["string"],
            "acceptance_criteria_evaluation": [
                {"criterion": "string", "status": "met | partial | unmet", "reason": "string"}
            ],
            "next_action": (
                "deliberate | continue_deliberation | targeted_follow_up | "
                "continue_follow_up | vote"
            ),
            "follow_up_roles": ["role_key"],
            "follow_up_questions": ["string"],
        }
    if phase in {
        MeetingStatus.DELIBERATION.value,
        MeetingStatus.TARGETED_FOLLOW_UP.value,
    }:
        return {
            "inaccurate_or_omitted_claims": ["string"],
            "assessment_changes": ["string"],
            "remaining_objections": ["string"],
            "new_contradictions": ["string"],
            "requested_revisions": ["string"],
            "acceptance_criteria_assessment": [
                {"criterion": "string", "status": "met | partial | unmet"}
            ],
            "evidence_refs": ["string or structured source reference"],
            "veto": None,
            "confidence": 0.0,
        }
    if phase == MeetingStatus.FINAL_VOTE.value:
        return {
            "vote": "support | oppose | abstain",
            "rationale": "string",
            "conditions": ["string"],
            "dissent": ["string"],
            "confidence": 0.0,
            "veto_id": None,
        }
    if phase == MeetingStatus.FINAL_REPORT.value:
        return {
            "next_action": "finalize | reopen_deliberation",
            "report_markdown": "complete final report as Markdown; optional only when reopening",
            "acceptance_criteria_evaluation": [
                {"criterion": "string", "status": "met | partial | unmet", "reason": "string"}
            ],
        }
    raise ValueError(f"unsupported Staff Meeting phase: {phase}")


def _visible_context(store: StateStore, meeting: dict[str, Any], phase: str) -> dict[str, Any]:
    packet = meeting_packet(store, meeting["meeting_id"])
    records = store.staff_meeting_records(meeting["meeting_id"])
    context: dict[str, Any] = {"packet": packet}
    if phase != MeetingStatus.INDEPENDENT_REVIEW.value:
        syntheses = [item for item in records if item.get("record_kind") == "synthesis"]
        excerpts = [item for item in records if item.get("record_kind") == "excerpt"]
        if syntheses:
            context["current_synthesis"] = syntheses[-1]["payload"]
        context["attributed_excerpts"] = [item["payload"] for item in excerpts[-40:]]
    if phase == MeetingStatus.SYNTHESIS.value:
        source_kinds = {"review", "deliberation", "follow_up"}
        context["panel_responses"] = [
            {
                "role_key": item.get("role_key"),
                "agent_id": item.get("agent_id"),
                "record_id": item.get("record_id"),
                "response": item.get("payload"),
            }
            for item in records
            if item.get("record_kind") in source_kinds
        ][-40:]
    if phase == MeetingStatus.FINAL_REPORT.value:
        context["vote_result"] = meeting.get("vote_result")
        ballot_round = int(meeting.get("ballot_round") or 1)
        context["ballots"] = [
            item["payload"]
            for item in records
            if item.get("record_kind") == "ballot"
            and int(item.get("round_number") or 0) == ballot_round
        ]
        context["vetoes"] = _effective_vetoes(records)
    return context


def _assignment_text(
    store: StateStore,
    meeting: dict[str, Any],
    seat: dict[str, Any],
    *,
    phase: str,
    round_number: int,
    bounded_questions: list[str] | None = None,
) -> str:
    contract = _phase_output_contract(phase)
    context = _visible_context(store, meeting, phase)
    if bounded_questions:
        context["bounded_follow_up_questions"] = bounded_questions
    return "\n".join(
        [
            f"Staff Meeting {meeting['meeting_id']} — {phase}, round {round_number}.",
            f"You occupy role seat {seat['role_seat_id']} as {seat['role_key']}.",
            f"Your lens: {seat['lens']}",
            "Work independently when this is INDEPENDENT_REVIEW. Do not infer other votes.",
            "All available tools are read-only evidence tools. Do not mutate files or systems.",
            "Return the ordinary OpenBrigade outer response JSON with status complete.",
            (
                "The outer summary value must be a JSON-encoded string containing exactly one "
                "object matching the Staff Meeting output contract below."
            ),
            (
                "Example shape: {\"status\":\"complete\","
                "\"summary\":\"{...escaped JSON...}\",\"blockers\":[]}"
            ),
            "Staff Meeting output contract:",
            json.dumps(contract, sort_keys=True),
            "Staff Meeting context:",
            json.dumps(context, sort_keys=True),
        ]
    )


def _queue_wave(
    store: StateStore,
    meeting: dict[str, Any],
    *,
    phase: str,
    round_number: int,
    seats: list[dict[str, Any]],
    bounded_questions: list[str] | None = None,
) -> list[Assignment]:
    if (
        meeting.get("active_wave_id")
        and meeting.get("active_phase") == phase
        and int(meeting.get("active_round") or 0) == round_number
    ):
        wave_id = str(meeting["active_wave_id"])
        existing_records = [
            item
            for item in store.staff_meeting_records(
                meeting["meeting_id"], record_kind="assignment"
            )
            if item.get("payload", {}).get("wave_id") == wave_id
        ]
        if existing_records:
            return [
                assignment
                for item in existing_records
                if (assignment := store.find_assignment(str(item["assignment_id"])))
                is not None
            ]
    wave_sequence = int(meeting.get("wave_sequence") or 0) + 1
    wave_id = hashlib.sha256(
        (
            f"{meeting['meeting_id']}:{meeting['packet_version']}:{phase}:"
            f"{round_number}:{wave_sequence}"
        ).encode()
    ).hexdigest()[:24]
    meeting["wave_sequence"] = wave_sequence
    meeting["active_wave_id"] = wave_id
    meeting["active_phase"] = phase
    meeting["active_round"] = round_number
    meeting["updated_at"] = utc_now_iso()
    # Persist the deterministic wave identity before creating any assignment.
    # A crash can therefore replay the same idempotency keys without fan-out.
    store.upsert_staff_meeting(meeting)
    assignments: list[Assignment] = []
    for seat in seats:
        key = (
            f"staff-meeting:{meeting['meeting_id']}:packet:{meeting['packet_version']}:"
            f"{phase}:round:{round_number}:seat:{seat['role_seat_id']}:wave:{wave_id}"
        )
        assignment = Assignment(
            assignment=_assignment_text(
                store,
                meeting,
                seat,
                phase=phase,
                round_number=round_number,
                bounded_questions=bounded_questions,
            ),
            assigned_to=seat["agent_id"],
            created_by=meeting["chair_agent_id"],
            source="staff_meeting",
            work_mode=WorkMode.STANDARD,
            priority=Priority.HIGH,
            kind=AssignmentKind.STAFF_MEETING,
            estimated_cycles=1,
            goal_statement=meeting["original_request"],
            assignment_rationale=(
                f"Staff Meeting {meeting['meeting_id']} role {seat['role_key']} phase {phase}."
            ),
            created_by_user_id=meeting.get("owner_username"),
            created_by_role=meeting["caller_kind"],
            idempotency_key=key,
        )
        persisted = store.add_assignment(assignment)
        assignments.append(persisted)
        _append_record(
            store,
            meeting,
            "assignment",
            {
                "wave_id": wave_id,
                "phase": phase,
                "round_number": round_number,
                "assignment_id": persisted.assignment_id,
                "role_seat_id": seat["role_seat_id"],
                "role_key": seat["role_key"],
                "agent_id": seat["agent_id"],
                "bounded_questions": bounded_questions or [],
            },
            idempotency_key=f"assignment:{persisted.assignment_id}",
            role_seat_id=seat["role_seat_id"],
            role_key=seat["role_key"],
            round_number=round_number,
            phase=phase,
            assignment_id=persisted.assignment_id,
            agent_id=seat["agent_id"],
        )
    return assignments


def dispatch_independent_review(
    store: StateStore,
    meeting_id: str,
    *,
    actor_id: str,
) -> dict[str, Any]:
    meeting = store.find_staff_meeting(meeting_id)
    if meeting is None:
        raise ValueError(f"unknown Staff Meeting: {meeting_id}")
    if actor_id != meeting["chair_agent_id"]:
        raise PermissionError("only the chair may dispatch independent review")
    transition_meeting(
        store,
        meeting,
        MeetingStatus.INDEPENDENT_REVIEW.value,
        expected_status=MeetingStatus.PACKET_FROZEN.value,
        actor_id=actor_id,
        reason="independent role reviews dispatched",
    )
    _queue_wave(
        store,
        meeting,
        phase=MeetingStatus.INDEPENDENT_REVIEW.value,
        round_number=0,
        seats=list(meeting["approved_roster"]),
    )
    return meeting


def approve_freeze_and_dispatch(
    store: StateStore,
    meeting_id: str,
    *,
    actor_id: str,
) -> dict[str, Any]:
    meeting = store.find_staff_meeting(meeting_id)
    if meeting is None:
        raise ValueError(f"unknown Staff Meeting: {meeting_id}")
    status = meeting["status"]
    if status == MeetingStatus.ROSTER_PROPOSED.value:
        meeting = approve_roster(store, meeting_id, actor_id=actor_id)
        status = meeting["status"]
    if status == MeetingStatus.ROSTER_APPROVED.value:
        meeting = freeze_packet(store, meeting_id, actor_id=actor_id)
        status = meeting["status"]
    if status == MeetingStatus.PACKET_FROZEN.value:
        meeting = dispatch_independent_review(store, meeting_id, actor_id=actor_id)
        status = meeting["status"]
    if status != MeetingStatus.INDEPENDENT_REVIEW.value:
        raise ValueError(f"Staff Meeting cannot start review from status {status}")
    return meeting


def _parse_structured_summary(summary: str, phase: str) -> dict[str, Any]:
    try:
        parsed = json.loads(extract_json_object(summary))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Staff Meeting response summary is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Staff Meeting response summary must be an object")
    required_by_phase = {
        MeetingStatus.INDEPENDENT_REVIEW.value: {
            "disposition",
            "summary",
            "findings",
            "risks",
            "assumptions",
            "evidence_refs",
            "proposed_changes",
            "open_questions",
            "veto",
            "confidence",
        },
        MeetingStatus.SYNTHESIS.value: {
            "areas_of_agreement",
            "material_disagreements",
            "unique_findings",
            "conflicting_assumptions",
            "evidence_gaps",
            "proposed_combined_solution",
            "rejected_alternatives",
            "unresolved_questions",
            "acceptance_criteria_evaluation",
            "next_action",
            "follow_up_roles",
            "follow_up_questions",
        },
        MeetingStatus.DELIBERATION.value: {
            "inaccurate_or_omitted_claims",
            "assessment_changes",
            "remaining_objections",
            "new_contradictions",
            "requested_revisions",
            "acceptance_criteria_assessment",
            "evidence_refs",
            "veto",
            "confidence",
        },
        MeetingStatus.TARGETED_FOLLOW_UP.value: {
            "inaccurate_or_omitted_claims",
            "assessment_changes",
            "remaining_objections",
            "new_contradictions",
            "requested_revisions",
            "acceptance_criteria_assessment",
            "evidence_refs",
            "veto",
            "confidence",
        },
        MeetingStatus.FINAL_VOTE.value: {
            "vote",
            "rationale",
            "conditions",
            "dissent",
            "confidence",
            "veto_id",
        },
    }
    missing = required_by_phase.get(phase, set()) - parsed.keys()
    if missing:
        raise ValueError(
            "Staff Meeting response is missing fields: " + ", ".join(sorted(missing))
        )
    if phase == MeetingStatus.INDEPENDENT_REVIEW.value:
        disposition = str(parsed.get("disposition") or "").lower()
        if disposition not in {"support", "revise", "oppose", "abstain"}:
            raise ValueError("independent review disposition is invalid")
    elif phase == MeetingStatus.FINAL_VOTE.value:
        vote = str(parsed.get("vote") or "").lower()
        if vote not in {"support", "oppose", "abstain"}:
            raise ValueError("formal ballot vote is invalid")
        parsed["vote"] = vote
    elif phase == MeetingStatus.SYNTHESIS.value:
        if not str(parsed.get("proposed_combined_solution") or "").strip():
            raise ValueError("synthesis is missing proposed_combined_solution")
        action = str(parsed.get("next_action") or "").lower()
        if action not in {
            "deliberate",
            "continue_deliberation",
            "targeted_follow_up",
            "continue_follow_up",
            "vote",
        }:
            raise ValueError("synthesis next_action is invalid")
        parsed["next_action"] = action
    elif phase == MeetingStatus.FINAL_REPORT.value:
        next_action = str(parsed.get("next_action") or "finalize").lower()
        parsed["next_action"] = next_action
        if next_action not in {"finalize", "reopen_deliberation"}:
            raise ValueError("final report next_action is invalid")
        if next_action == "finalize" and not str(parsed.get("report_markdown") or "").strip():
            raise ValueError("final report is missing report_markdown")
    if "confidence" in parsed:
        try:
            confidence = float(parsed["confidence"])
        except (TypeError, ValueError) as exc:
            raise ValueError("Staff Meeting confidence must be numeric") from exc
        if not 0 <= confidence <= 1:
            raise ValueError("Staff Meeting confidence must be between zero and one")
        parsed["confidence"] = confidence
    veto = parsed.get("veto")
    if isinstance(veto, dict):
        action = str(veto.get("action") or "issue").lower()
        required_veto = (
            {"veto_id", "reason"}
            if action == "withdraw"
            else {
                "risk_statement",
                "affected_criterion",
                "severity",
                "likelihood",
                "required_mitigation",
                "validation_method",
            }
        )
        if action not in {"issue", "withdraw"} or not required_veto.issubset(veto):
            raise ValueError("Staff Meeting veto payload is incomplete or invalid")
        veto["action"] = action
    return redact_sensitive_data(parsed)


def _assignment_completion(
    store: StateStore,
    assignment_id: str,
) -> tuple[str, str] | None:
    history = next(
        (
            item
            for item in store.assignment_history()
            if item.get("assignment_id") == assignment_id
        ),
        None,
    )
    if history is not None:
        return (
            str(history.get("final_status") or "failed"),
            str(history.get("executive_summary") or history.get("failure_info") or ""),
        )
    active = store.find_assignment(assignment_id)
    if active is not None and (
        active.status in TERMINAL_STATUSES or active.status == AssignmentStatus.BLOCKED
    ):
        return active.status.value, str(active.progress_summary or active.last_error or "")
    return None


def _response_kind(phase: str) -> str:
    return {
        MeetingStatus.INDEPENDENT_REVIEW.value: "review",
        MeetingStatus.SYNTHESIS.value: "synthesis",
        MeetingStatus.DELIBERATION.value: "deliberation",
        MeetingStatus.TARGETED_FOLLOW_UP.value: "follow_up",
        MeetingStatus.FINAL_VOTE.value: "ballot",
        MeetingStatus.FINAL_REPORT.value: "final_report",
    }[phase]


def _record_participant_message(
    store: StateStore,
    meeting: dict[str, Any],
    record: dict[str, Any],
) -> None:
    if record["record_kind"] == "ballot" and not meeting.get("ballots_revealed"):
        return
    store.add_message(
        ChatMessage(
            channel=meeting["conversation_channel"],
            sender=str(record.get("agent_id") or "orchestrator"),
            recipient="staff_meeting",
            content=json.dumps(record["payload"], sort_keys=True),
            metadata={
                "kind": f"staff_meeting_{record['record_kind']}",
                "meeting_id": meeting["meeting_id"],
                "conversation_id": meeting["conversation_id"],
                "record_id": record["record_id"],
                "role_seat_id": record.get("role_seat_id"),
                "role_key": record.get("role_key"),
                "round_number": record.get("round_number"),
                "phase": record.get("phase"),
            },
        )
    )


def _consume_active_wave(
    store: StateStore,
    meeting: dict[str, Any],
) -> tuple[bool, list[dict[str, Any]]]:
    wave_id = meeting.get("active_wave_id")
    phase = str(meeting.get("active_phase") or "")
    if not wave_id or not phase:
        return False, []
    all_records = store.staff_meeting_records(meeting["meeting_id"])
    assignments = [
        item
        for item in all_records
        if item.get("record_kind") == "assignment"
        and item.get("payload", {}).get("wave_id") == wave_id
    ]
    response_kind = _response_kind(phase)
    responses: list[dict[str, Any]] = []
    complete = True
    for assignment_record in assignments:
        assignment_id = str(assignment_record["assignment_id"])
        existing = next(
            (
                item
                for item in all_records
                if item.get("idempotency_key") == f"response:{assignment_id}"
            ),
            None,
        )
        if existing is not None:
            if existing.get("record_kind") == response_kind:
                responses.append(existing)
            continue
        completion = _assignment_completion(store, assignment_id)
        if completion is None:
            complete = False
            continue
        status, summary = completion
        if status != AssignmentStatus.COMPLETE.value:
            active = store.find_assignment(assignment_id)
            if active is not None:
                if active.status == AssignmentStatus.BLOCKED:
                    active.transition_to(AssignmentStatus.FAILED)
                if active.status in TERMINAL_STATUSES:
                    store.update_assignment(active)
                    store.archive_assignment(
                        active,
                        executive_summary=summary or "Staff Meeting seat was absent.",
                    )
            _append_record(
                store,
                meeting,
                "absence",
                {"status": status, "reason": summary or "assignment did not complete"},
                idempotency_key=f"response:{assignment_id}",
                role_seat_id=assignment_record.get("role_seat_id"),
                role_key=assignment_record.get("role_key"),
                round_number=assignment_record.get("round_number"),
                phase=phase,
                assignment_id=assignment_id,
                agent_id=assignment_record.get("agent_id"),
            )
            continue
        try:
            payload = _parse_structured_summary(summary, phase)
        except ValueError as exc:
            _append_record(
                store,
                meeting,
                "absence",
                {"status": "invalid_response", "reason": str(exc)},
                idempotency_key=f"response:{assignment_id}",
                role_seat_id=assignment_record.get("role_seat_id"),
                role_key=assignment_record.get("role_key"),
                round_number=assignment_record.get("round_number"),
                phase=phase,
                assignment_id=assignment_id,
                agent_id=assignment_record.get("agent_id"),
            )
            continue
        superseded_ballot = None
        if response_kind == "ballot":
            superseded_ballot = next(
                (
                    item
                    for item in reversed(all_records)
                    if item.get("record_kind") == "ballot"
                    and item.get("role_seat_id") == assignment_record.get("role_seat_id")
                ),
                None,
            )
        response = _append_record(
            store,
            meeting,
            response_kind,
            payload,
            idempotency_key=f"response:{assignment_id}",
            role_seat_id=assignment_record.get("role_seat_id"),
            role_key=assignment_record.get("role_key"),
            round_number=assignment_record.get("round_number"),
            phase=phase,
            assignment_id=assignment_id,
            agent_id=assignment_record.get("agent_id"),
            supersedes_record_id=(
                superseded_ballot.get("record_id") if superseded_ballot else None
            ),
        )
        responses.append(response)
        _record_veto_from_response(store, meeting, response)
        _record_participant_message(store, meeting, response)
    return complete, responses


def _record_veto_from_response(
    store: StateStore,
    meeting: dict[str, Any],
    response: dict[str, Any],
) -> None:
    veto = response.get("payload", {}).get("veto")
    if not isinstance(veto, dict):
        return
    seat = _role_seat(meeting, str(response["role_seat_id"]))
    veto_domain = seat.get("veto_domain")
    if not veto_domain:
        return
    action = str(veto.get("action") or "issue").lower()
    if action == "withdraw":
        veto_id = str(veto.get("veto_id") or "")
        previous = next(
            (
                item
                for item in reversed(store.staff_meeting_records(meeting["meeting_id"]))
                if item.get("record_kind") == "veto"
                and item.get("payload", {}).get("veto_id") == veto_id
                and (
                    item.get("agent_id") == response.get("agent_id")
                    or item.get("payload", {}).get("replacement_agent_id")
                    == response.get("agent_id")
                )
            ),
            None,
        )
        if previous is None:
            return
        _append_record(
            store,
            meeting,
            "veto",
            {**previous["payload"], "status": "withdrawn", "withdrawal_reason": veto.get("reason")},
            idempotency_key=f"veto-withdraw:{veto_id}:{response['record_id']}",
            role_seat_id=response.get("role_seat_id"),
            role_key=response.get("role_key"),
            round_number=response.get("round_number"),
            phase=response.get("phase"),
            agent_id=response.get("agent_id"),
            supersedes_record_id=previous["record_id"],
        )
        effective = _effective_vetoes(
            store.staff_meeting_records(meeting["meeting_id"])
        )
        meeting["veto_status"] = (
            "pending"
            if any(item.get("status") != "withdrawn" for item in effective)
            else "clear"
        )
        store.upsert_staff_meeting(meeting)
        return
    required = {
        "risk_statement",
        "affected_criterion",
        "severity",
        "likelihood",
        "required_mitigation",
        "validation_method",
    }
    if not required.issubset(veto):
        return
    veto_id = str(veto.get("veto_id") or uuid4())
    _append_record(
        store,
        meeting,
        "veto",
        {
            **veto,
            "veto_id": veto_id,
            "veto_domain": veto_domain,
            "status": "issued",
            "issued_by_agent_id": response.get("agent_id"),
            "issued_by_role_seat_id": response.get("role_seat_id"),
        },
        idempotency_key=f"veto-issue:{veto_id}",
        role_seat_id=response.get("role_seat_id"),
        role_key=response.get("role_key"),
        round_number=response.get("round_number"),
        phase=response.get("phase"),
        agent_id=response.get("agent_id"),
    )
    meeting["veto_status"] = "pending"
    store.upsert_staff_meeting(meeting)


def _effective_vetoes(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.get("record_kind") != "veto":
            continue
        veto_id = str(record.get("payload", {}).get("veto_id") or "")
        if veto_id:
            latest[veto_id] = dict(record["payload"])
    return list(latest.values())


def accept_veto_mitigation(
    store: StateStore,
    meeting_id: str,
    *,
    veto_id: str,
    actor_id: str,
    mitigation: str,
    validation_evidence: Iterable[dict[str, Any] | str],
    idempotency_key: str,
) -> dict[str, Any]:
    """Clear a veto only through acceptance by its issuer or replacement reviewer."""
    meeting = store.find_staff_meeting(meeting_id)
    if meeting is None:
        raise ValueError(f"unknown Staff Meeting: {meeting_id}")
    if meeting["status"] in TERMINAL_MEETING_STATUSES:
        raise ValueError("a terminal Staff Meeting veto cannot be changed")
    records = store.staff_meeting_records(meeting_id)
    veto_record = next(
        (
            item
            for item in reversed(records)
            if item.get("record_kind") == "veto"
            and item.get("payload", {}).get("veto_id") == veto_id
        ),
        None,
    )
    if veto_record is None:
        raise ValueError("unknown veto")
    veto = veto_record["payload"]
    if veto.get("status") == "withdrawn":
        return veto_record
    authorized = {
        str(veto.get("issued_by_agent_id") or ""),
        str(veto.get("replacement_agent_id") or ""),
    }
    if actor_id not in authorized:
        raise PermissionError("only the issuing or replacement reviewer may clear a veto")
    mitigation_text = redact_sensitive_text(mitigation.strip())
    evidence = redact_sensitive_data(list(validation_evidence))
    if not mitigation_text or not evidence:
        raise ValueError("veto clearance requires mitigation and validation evidence")
    cleared = _append_record(
        store,
        meeting,
        "veto",
        {
            **veto,
            "status": "withdrawn",
            "accepted_mitigation": mitigation_text,
            "validation_evidence": evidence,
            "accepted_by_agent_id": actor_id,
        },
        idempotency_key=idempotency_key,
        role_seat_id=veto_record.get("role_seat_id"),
        role_key=veto_record.get("role_key"),
        round_number=veto_record.get("round_number"),
        phase=meeting["status"],
        agent_id=actor_id,
        supersedes_record_id=veto_record["record_id"],
    )
    effective = _effective_vetoes(store.staff_meeting_records(meeting_id))
    meeting["veto_status"] = (
        "pending"
        if any(item.get("status") != "withdrawn" for item in effective)
        else "clear"
    )
    store.upsert_staff_meeting(meeting)
    return cleared


def _wave_has_quorum(meeting: dict[str, Any], responses: list[dict[str, Any]]) -> bool:
    agents = {str(item.get("agent_id")) for item in responses if item.get("agent_id")}
    return (
        len(responses) >= int(meeting["quorum_required"])
        and len(agents) >= int(meeting["minimum_distinct_agents"])
    )


def _chair_seat(meeting: dict[str, Any]) -> dict[str, Any]:
    return next(item for item in meeting["approved_roster"] if item["role_key"] == "chair")


def _dispatch_synthesis(
    store: StateStore,
    meeting: dict[str, Any],
    *,
    previous_status: str,
    reason: str,
) -> None:
    transition_meeting(
        store,
        meeting,
        MeetingStatus.SYNTHESIS.value,
        expected_status=previous_status,
        actor_id=meeting["chair_agent_id"],
        reason=reason,
    )
    _queue_wave(
        store,
        meeting,
        phase=MeetingStatus.SYNTHESIS.value,
        round_number=int(meeting.get("active_round") or 0),
        seats=[_chair_seat(meeting)],
    )


def _token_usage(store: StateStore, meeting: dict[str, Any]) -> int:
    assignment_ids = {
        str(item.get("assignment_id"))
        for item in store.staff_meeting_records(meeting["meeting_id"], record_kind="assignment")
        if item.get("assignment_id")
    }
    return sum(
        int(item.get("total_tokens") or 0)
        for item in store.usage_records()
        if str(item.get("assignment_id")) in assignment_ids
    )


def _resource_limit_reason(store: StateStore, meeting: dict[str, Any]) -> str | None:
    limits = meeting["resource_limits"]
    elapsed = (utc_now() - parse_utc_iso(meeting["created_at"])).total_seconds()
    if elapsed >= int(limits["max_elapsed_seconds"]):
        return "elapsed-time limit reached"
    if _token_usage(store, meeting) >= int(limits["max_total_tokens"]):
        return "token limit reached"
    assignment_ids = {
        str(item.get("assignment_id"))
        for item in store.staff_meeting_records(
            meeting["meeting_id"], record_kind="assignment"
        )
        if item.get("assignment_id")
    }
    tool_calls = sum(
        int(item.get("tool_calls") or 0)
        for item in store.transcripts()
        if str(item.get("assignment_id")) in assignment_ids
    )
    if tool_calls >= int(limits["max_tool_calls"]):
        return "tool-call limit reached"
    return None


def _record_excerpts(
    store: StateStore,
    meeting: dict[str, Any],
    synthesis: dict[str, Any],
) -> None:
    records = store.staff_meeting_records(meeting["meeting_id"])
    sources = [
        item
        for item in records
        if item.get("record_kind") in {"review", "deliberation", "follow_up"}
    ]
    query_terms = set(
        re.findall(
            r"[a-z0-9_]{3,}",
            " ".join(
                [
                    meeting["original_request"],
                    *meeting["acceptance_criteria"],
                    json.dumps(synthesis.get("payload", {}), sort_keys=True),
                ]
            ).lower(),
        )
    )
    candidates: list[tuple[int, str, dict[str, Any]]] = []
    mandatory_fields = {
        "remaining_objections",
        "new_contradictions",
        "risks",
        "open_questions",
    }
    for source in sources:
        payload = source.get("payload", {})
        dissenting_review = str(payload.get("disposition") or "").lower() in {
            "oppose",
            "revise",
        }
        for field, value in payload.items():
            if field == "veto" and isinstance(value, dict):
                value = [
                    str(value.get("risk_statement") or ""),
                    str(value.get("required_mitigation") or ""),
                ]
            values = value if isinstance(value, list) else [value]
            for index, item in enumerate(values):
                if not isinstance(item, str) or not item.strip():
                    continue
                terms = set(re.findall(r"[a-z0-9_]{3,}", item.lower()))
                score = len(terms & query_terms)
                mandatory = field in mandatory_fields or (
                    dissenting_review
                    and field in {"summary", "findings", "proposed_changes", "risks"}
                )
                if mandatory:
                    score += 1000
                span_id = f"{source['record_id']}:{field}:{index}"
                candidates.append(
                    (
                        -score,
                        span_id,
                        {
                            "source_record_id": source["record_id"],
                            "source_span_id": span_id,
                            "role_key": source.get("role_key"),
                            "agent_id": source.get("agent_id"),
                            "field": field,
                            "text": item,
                            "relevance_score": score,
                            "mandatory": mandatory,
                        },
                    )
                )
    candidates.sort(key=lambda item: (item[0], item[1]))
    selected = [item[2] for item in candidates if item[2]["mandatory"]]
    selected_ids = {item["source_span_id"] for item in selected}
    for _, _, candidate in candidates:
        if len(selected) >= 24:
            break
        if candidate["source_span_id"] not in selected_ids:
            selected.append(candidate)
            selected_ids.add(candidate["source_span_id"])
    excerpt_generation = len(
        [item for item in records if item.get("record_kind") == "synthesis"]
    )
    for excerpt in selected:
        _append_record(
            store,
            meeting,
            "excerpt",
            excerpt,
            idempotency_key=(
                f"excerpt:{excerpt_generation}:{excerpt['source_span_id']}"
            ),
            role_key=excerpt.get("role_key"),
            phase=MeetingStatus.SYNTHESIS.value,
            agent_id=excerpt.get("agent_id"),
        )


def _dispatch_deliberation(store: StateStore, meeting: dict[str, Any], round_number: int) -> None:
    meeting["current_round"] = round_number
    transition_meeting(
        store,
        meeting,
        MeetingStatus.DELIBERATION.value,
        expected_status=MeetingStatus.SYNTHESIS.value,
        actor_id=meeting["chair_agent_id"],
        reason=f"full-panel deliberation round {round_number} dispatched",
    )
    _queue_wave(
        store,
        meeting,
        phase=MeetingStatus.DELIBERATION.value,
        round_number=round_number,
        seats=list(meeting["approved_roster"]),
    )


def _dispatch_follow_up(
    store: StateStore,
    meeting: dict[str, Any],
    synthesis_payload: dict[str, Any],
    *,
    round_number: int,
) -> None:
    requested_roles = {
        str(item) for item in synthesis_payload.get("follow_up_roles", []) if str(item)
    }
    seats = [
        item for item in meeting["approved_roster"] if item["role_key"] in requested_roles
    ]
    if not seats:
        seats = [item for item in meeting["approved_roster"] if item["role_key"] != "chair"][:2]
    meeting["follow_up_round"] = round_number
    meeting["follow_up_role_keys"] = [item["role_key"] for item in seats]
    meeting["follow_up_questions"] = [
        str(item) for item in synthesis_payload.get("follow_up_questions", []) if str(item)
    ]
    transition_meeting(
        store,
        meeting,
        MeetingStatus.TARGETED_FOLLOW_UP.value,
        expected_status=MeetingStatus.SYNTHESIS.value,
        actor_id=meeting["chair_agent_id"],
        reason=f"targeted follow-up round {round_number} dispatched",
    )
    _queue_wave(
        store,
        meeting,
        phase=MeetingStatus.TARGETED_FOLLOW_UP.value,
        round_number=round_number,
        seats=seats,
        bounded_questions=meeting["follow_up_questions"],
    )


def _dispatch_vote(store: StateStore, meeting: dict[str, Any]) -> None:
    transition_meeting(
        store,
        meeting,
        MeetingStatus.FINAL_VOTE.value,
        expected_status=MeetingStatus.SYNTHESIS.value,
        actor_id=meeting["chair_agent_id"],
        reason="chair called the formal secret ballot",
    )
    meeting["ballot_round"] = int(meeting.get("ballot_round") or 0) + 1
    meeting["ballots_revealed"] = False
    store.upsert_staff_meeting(meeting)
    _queue_wave(
        store,
        meeting,
        phase=MeetingStatus.FINAL_VOTE.value,
        round_number=meeting["ballot_round"],
        seats=list(meeting["approved_roster"]),
    )


def _dispatch_report(store: StateStore, meeting: dict[str, Any]) -> None:
    transition_meeting(
        store,
        meeting,
        MeetingStatus.FINAL_REPORT.value,
        expected_status=MeetingStatus.FINAL_VOTE.value,
        actor_id=meeting["chair_agent_id"],
        reason="ballots revealed and final report requested",
    )
    _queue_wave(
        store,
        meeting,
        phase=MeetingStatus.FINAL_REPORT.value,
        round_number=int(meeting.get("ballot_round") or 1),
        seats=[_chair_seat(meeting)],
    )


def _finalize_report(
    store: StateStore,
    meeting: dict[str, Any],
    report_record: dict[str, Any],
) -> None:
    result = meeting["vote_result"]
    records = store.staff_meeting_records(meeting["meeting_id"])
    usage = {
        "total_tokens": _token_usage(store, meeting),
        "elapsed_seconds": int(
            (utc_now() - parse_utc_iso(meeting["created_at"])).total_seconds()
        ),
    }
    report_markdown = _render_final_report(
        meeting,
        records,
        result,
        chair_report=str(report_record["payload"]["report_markdown"]),
        usage=usage,
    )
    final_record = _append_record(
        store,
        meeting,
        "report",
        {
            "report_markdown": report_markdown,
            "vote_result": result,
            "resource_usage": usage,
            "chair_report_record_id": report_record["record_id"],
        },
        idempotency_key=f"final-report:{meeting.get('ballot_round', 1)}",
        phase=MeetingStatus.FINAL_REPORT.value,
        agent_id=meeting["chair_agent_id"],
        supersedes_record_id=report_record["record_id"],
    )
    _record_participant_message(store, meeting, final_record)
    meeting["final_report_record_id"] = final_record["record_id"]
    meeting["termination_reason"] = result["outcome"]
    meeting["resource_usage"] = usage
    store.upsert_staff_meeting(meeting)
    transition_meeting(
        store,
        meeting,
        result["outcome"],
        expected_status=MeetingStatus.FINAL_REPORT.value,
        actor_id=meeting["chair_agent_id"],
        reason="final report persisted",
    )
    store.set_conversation_summary(meeting["conversation_id"], report_markdown[:4000])
    store.add_episode(
        {
            "episode_id": f"staff-meeting:{meeting['meeting_id']}",
            "agent_id": meeting["chair_agent_id"],
            "summary": report_markdown,
            "status": meeting["status"],
            "conversation_id": meeting["conversation_channel"],
            "meeting_id": meeting["meeting_id"],
            "request_hash": meeting["original_request_hash"],
            "created_at": meeting["completed_at"],
        }
    )
    store.add_provenance_record(
        {
            "record_id": f"staff-meeting-decision:{meeting['meeting_id']}",
            "node_id": meeting["meeting_id"],
            "node_type": "decision",
            "source_refs": [
                item["record_id"]
                for item in store.staff_meeting_records(meeting["meeting_id"])
            ],
            "metadata": {
                "meeting_id": meeting["meeting_id"],
                "status": meeting["status"],
                "vote_result": result,
                "final_report_record_id": final_record["record_id"],
                "participants": [
                    {
                        "agent_id": item["agent_id"],
                        "role_seat_id": item["role_seat_id"],
                        "role_key": item["role_key"],
                    }
                    for item in meeting["approved_roster"]
                ],
            },
            "created_at": meeting["completed_at"],
        }
    )


def _json_section(value: Any) -> str:
    return "```json\n" + json.dumps(value, indent=2, sort_keys=True) + "\n```"


def _render_final_report(
    meeting: dict[str, Any],
    records: list[dict[str, Any]],
    result: dict[str, Any],
    *,
    chair_report: str,
    usage: dict[str, Any],
) -> str:
    syntheses = [item["payload"] for item in records if item.get("record_kind") == "synthesis"]
    current_synthesis = syntheses[-1] if syntheses else {}
    ballot_round = int(meeting.get("ballot_round") or 1)
    ballots = [
        item
        for item in records
        if item.get("record_kind") == "ballot"
        and int(item.get("round_number") or 0) == ballot_round
    ]
    vetoes = _effective_vetoes(records)
    agent_roles: dict[str, list[str]] = {}
    for seat in meeting["approved_roster"]:
        agent_roles.setdefault(seat["agent_id"], []).append(seat["role_key"])
    duplicated = {
        agent_id: roles for agent_id, roles in agent_roles.items() if len(roles) > 1
    }
    dissent = [
        {
            "role_key": item.get("role_key"),
            "agent_id": item.get("agent_id"),
            "vote": item["payload"].get("vote"),
            "rationale": item["payload"].get("rationale"),
            "dissent": item["payload"].get("dissent", []),
        }
        for item in ballots
        if item["payload"].get("vote") != "support"
        or item["payload"].get("dissent")
    ]
    audit_refs = [item["record_id"] for item in records]
    return "\n\n".join(
        [
            "# Staff Meeting Final Report",
            "## 1. Original request and hash\n\n"
            + meeting["original_request"]
            + "\n\n`"
            + meeting["original_request_hash"]
            + "`",
            "## 2. Acceptance criteria\n\n"
            + "\n".join(f"- {item}" for item in meeting["acceptance_criteria"]),
            "## 3. Roster and concentration disclosure\n\n"
            + _json_section(
                {
                    "roster": meeting["approved_roster"],
                    "duplicated_underlying_agents": duplicated,
                }
            ),
            "## 4. Recommended conclusion or majority view\n\n" + chair_report.strip(),
            "## 5. Consensus status and vote arithmetic\n\n" + _json_section(result),
            "## 6. Acceptance-criteria evaluation\n\n"
            + _json_section(current_synthesis.get("acceptance_criteria_evaluation", [])),
            "## 7. Alternatives considered\n\n"
            + _json_section(current_synthesis.get("rejected_alternatives", [])),
            "## 8. Material agreements\n\n"
            + _json_section(current_synthesis.get("areas_of_agreement", [])),
            "## 9. Attributed dissent and abstentions\n\n" + _json_section(dissent),
            "## 10. Security and safety vetoes\n\n" + _json_section(vetoes),
            "## 11. Assumptions, constraints, and evidence\n\n"
            + _json_section(
                {
                    "assumptions": meeting["declared_assumptions"],
                    "constraints": meeting["known_constraints"],
                    "evidence_refs": meeting["evidence_refs"],
                }
            ),
            "## 12. Known unknowns and external decisions\n\n"
            + _json_section(current_synthesis.get("unresolved_questions", [])),
            "## 13. Risks and mitigations\n\n"
            + _json_section(
                [
                    {
                        "role_key": item.get("role_key"),
                        "agent_id": item.get("agent_id"),
                        "risks": item.get("payload", {}).get("risks", []),
                        "requested_revisions": item.get("payload", {}).get(
                            "requested_revisions", []
                        ),
                    }
                    for item in records
                    if item.get("record_kind") in {"review", "deliberation", "follow_up"}
                    and (
                        item.get("payload", {}).get("risks")
                        or item.get("payload", {}).get("requested_revisions")
                    )
                ]
            ),
            "## 14. Resource usage and termination\n\n"
            + _json_section({**usage, "termination_reason": result["outcome"]}),
            "## 15. Audit references\n\n" + _json_section(audit_refs),
        ]
    )


def _advance_completed_wave(
    store: StateStore,
    meeting: dict[str, Any],
    responses: list[dict[str, Any]],
) -> None:
    phase = str(meeting["active_phase"])
    if phase == MeetingStatus.INDEPENDENT_REVIEW.value:
        if not _wave_has_quorum(meeting, responses):
            transition_meeting(
                store,
                meeting,
                MeetingStatus.FAILED_NO_QUORUM.value,
                expected_status=MeetingStatus.INDEPENDENT_REVIEW.value,
                actor_id="orchestrator",
                reason="independent review did not reach quorum",
            )
            return
        _dispatch_synthesis(
            store,
            meeting,
            previous_status=MeetingStatus.INDEPENDENT_REVIEW.value,
            reason="independent reviews complete",
        )
        return
    if phase in {
        MeetingStatus.DELIBERATION.value,
        MeetingStatus.TARGETED_FOLLOW_UP.value,
    }:
        if not _wave_has_quorum(meeting, responses) and phase == MeetingStatus.DELIBERATION.value:
            transition_meeting(
                store,
                meeting,
                MeetingStatus.FAILED_NO_QUORUM.value,
                expected_status=phase,
                actor_id="orchestrator",
                reason="full-panel deliberation did not reach quorum",
            )
            return
        _dispatch_synthesis(
            store,
            meeting,
            previous_status=phase,
            reason=f"{phase.lower()} responses complete",
        )
        return
    if phase == MeetingStatus.SYNTHESIS.value:
        if not responses:
            _terminate_resource_limit(store, meeting, "chair synthesis failed")
            return
        synthesis = responses[-1]
        _record_excerpts(store, meeting, synthesis)
        previous_assignment_records = store.staff_meeting_records(
            meeting["meeting_id"], record_kind="assignment"
        )
        prior_non_synthesis = next(
            (
                item
                for item in reversed(previous_assignment_records)
                if item.get("payload", {}).get("wave_id") != meeting["active_wave_id"]
            ),
            None,
        )
        origin = str(prior_non_synthesis.get("phase") if prior_non_synthesis else "")
        action = str(synthesis["payload"].get("next_action") or "").lower()
        if origin == MeetingStatus.INDEPENDENT_REVIEW.value:
            _dispatch_deliberation(store, meeting, 1)
            return
        if origin == MeetingStatus.TARGETED_FOLLOW_UP.value:
            follow_up_round = int(meeting.get("follow_up_round") or 1)
            max_rounds = int(meeting["resource_limits"]["max_follow_up_rounds"])
            if action == "continue_follow_up" and follow_up_round < max_rounds:
                _dispatch_follow_up(
                    store,
                    meeting,
                    {
                        **synthesis["payload"],
                        "follow_up_roles": meeting.get("follow_up_role_keys", []),
                        "follow_up_questions": meeting.get("follow_up_questions", []),
                    },
                    round_number=follow_up_round + 1,
                )
            else:
                _dispatch_vote(store, meeting)
            return
        current_round = int(meeting.get("current_round") or 1)
        max_rounds = int(meeting["resource_limits"]["max_deliberation_rounds"])
        if action == "targeted_follow_up" and not meeting.get("follow_up_discussions_used"):
            meeting["follow_up_discussions_used"] = 1
            store.upsert_staff_meeting(meeting)
            _dispatch_follow_up(store, meeting, synthesis["payload"], round_number=1)
        elif action == "continue_deliberation" and current_round < max_rounds:
            _dispatch_deliberation(store, meeting, current_round + 1)
        else:
            _dispatch_vote(store, meeting)
        return
    if phase == MeetingStatus.FINAL_VOTE.value:
        meeting["ballots_revealed"] = True
        revealed_rounds = {
            int(item) for item in meeting.get("revealed_ballot_rounds", [])
        }
        revealed_rounds.add(int(meeting.get("ballot_round") or 1))
        meeting["revealed_ballot_rounds"] = sorted(revealed_rounds)
        records = store.staff_meeting_records(meeting["meeting_id"])
        vetoes = _effective_vetoes(records)
        vote_result = evaluate_vote(
            [
                {**item["payload"], "agent_id": item.get("agent_id")}
                for item in responses
            ],
            seat_count=int(meeting["seat_count"]),
            quorum_ratio=float(meeting["quorum_ratio"]),
            min_distinct_agents=int(meeting["minimum_distinct_agents"]),
            min_substantive_votes=int(meeting["minimum_substantive_votes"]),
            unresolved_vetoes=vetoes,
        )
        meeting["vote_result"] = vote_result
        store.upsert_staff_meeting(meeting)
        for ballot in responses:
            _record_participant_message(store, meeting, ballot)
        if vote_result["outcome"] == MeetingStatus.FAILED_NO_QUORUM.value:
            transition_meeting(
                store,
                meeting,
                MeetingStatus.FAILED_NO_QUORUM.value,
                expected_status=MeetingStatus.FINAL_VOTE.value,
                actor_id="orchestrator",
                reason="formal ballot did not reach quorum",
            )
            return
        _dispatch_report(store, meeting)
        return
    if phase == MeetingStatus.FINAL_REPORT.value:
        if not responses:
            _terminate_resource_limit(store, meeting, "final report generation failed")
            return
        next_action = str(responses[-1]["payload"].get("next_action") or "finalize")
        max_rounds = int(meeting["resource_limits"]["max_deliberation_rounds"])
        current_round = int(meeting.get("current_round") or 1)
        if next_action == "reopen_deliberation" and current_round < max_rounds:
            transition_meeting(
                store,
                meeting,
                MeetingStatus.SYNTHESIS.value,
                expected_status=MeetingStatus.FINAL_REPORT.value,
                actor_id=meeting["chair_agent_id"],
                reason="chair reopened deliberation after revealed ballots",
            )
            _dispatch_deliberation(store, meeting, current_round + 1)
            return
        _finalize_report(store, meeting, responses[-1])


def _terminate_resource_limit(
    store: StateStore,
    meeting: dict[str, Any],
    reason: str,
) -> None:
    current = str(meeting["status"])
    records = store.staff_meeting_records(meeting["meeting_id"])
    syntheses = [item for item in records if item.get("record_kind") == "synthesis"]
    report_markdown = "\n\n".join(
        [
            "# Staff Meeting Resource-Limit Report",
            f"**Result:** non-consensual `{MeetingStatus.RESOURCE_LIMIT_REACHED.value}`",
            "## Original request\n\n"
            + meeting["original_request"]
            + f"\n\n`{meeting['original_request_hash']}`",
            "## Acceptance criteria\n\n"
            + "\n".join(f"- {item}" for item in meeting["acceptance_criteria"]),
            "## Best available synthesis\n\n"
            + _json_section(syntheses[-1]["payload"] if syntheses else {}),
            "## Available panel records\n\n"
            + _json_section(
                [
                    {
                        "record_id": item["record_id"],
                        "kind": item["record_kind"],
                        "role_key": item.get("role_key"),
                        "agent_id": item.get("agent_id"),
                    }
                    for item in records
                    if item.get("record_kind")
                    in {"review", "deliberation", "follow_up", "veto"}
                ]
            ),
            "## Termination reason\n\n" + reason,
        ]
    )
    final_record = _append_record(
        store,
        meeting,
        "report",
        {
            "report_markdown": report_markdown,
            "consensual": False,
            "termination_reason": reason,
        },
        idempotency_key="resource-limit-report",
        phase=current,
        agent_id="orchestrator",
    )
    meeting["final_report_record_id"] = final_record["record_id"]
    _record_participant_message(store, meeting, final_record)
    if MeetingStatus.RESOURCE_LIMIT_REACHED.value not in ALLOWED_MEETING_TRANSITIONS.get(
        current, frozenset()
    ):
        # A report-generation failure still needs a truthful terminal record.
        meeting["status"] = MeetingStatus.RESOURCE_LIMIT_REACHED.value
        meeting["termination_reason"] = reason
        meeting["updated_at"] = utc_now_iso()
        meeting["completed_at"] = meeting["updated_at"]
        store.upsert_staff_meeting(meeting)
        _record_transition(
            store,
            meeting,
            current,
            MeetingStatus.RESOURCE_LIMIT_REACHED.value,
            actor_id="orchestrator",
            reason=reason,
        )
    else:
        meeting["termination_reason"] = reason
        store.upsert_staff_meeting(meeting)
        transition_meeting(
            store,
            meeting,
            MeetingStatus.RESOURCE_LIMIT_REACHED.value,
            expected_status=current,
            actor_id="orchestrator",
            reason=reason,
        )
    completed = store.find_staff_meeting(meeting["meeting_id"]) or meeting
    store.set_conversation_summary(meeting["conversation_id"], report_markdown[:4000])
    store.add_episode(
        {
            "episode_id": f"staff-meeting:{meeting['meeting_id']}",
            "agent_id": meeting["chair_agent_id"],
            "summary": report_markdown,
            "status": MeetingStatus.RESOURCE_LIMIT_REACHED.value,
            "conversation_id": meeting["conversation_channel"],
            "meeting_id": meeting["meeting_id"],
            "request_hash": meeting["original_request_hash"],
            "created_at": completed.get("completed_at") or utc_now_iso(),
        }
    )


def advance_staff_meetings(store: StateStore) -> dict[str, Any]:
    """Advance every active meeting by at most one durable state boundary."""
    advanced: list[str] = []
    waiting: list[str] = []
    for meeting in store.staff_meetings():
        if meeting["status"] in TERMINAL_MEETING_STATUSES:
            continue
        limit_reason = _resource_limit_reason(store, meeting)
        if limit_reason:
            _terminate_resource_limit(store, meeting, limit_reason)
            advanced.append(meeting["meeting_id"])
            continue
        complete, responses = _consume_active_wave(store, meeting)
        if not complete:
            waiting.append(meeting["meeting_id"])
            continue
        _advance_completed_wave(store, meeting, responses)
        advanced.append(meeting["meeting_id"])
    return {"advanced": advanced, "waiting": waiting}


def _archive_active_wave(store: StateStore, meeting: dict[str, Any], *, reason: str) -> None:
    wave_id = meeting.get("active_wave_id")
    if not wave_id:
        return
    for record in store.staff_meeting_records(meeting["meeting_id"], record_kind="assignment"):
        if record.get("payload", {}).get("wave_id") != wave_id:
            continue
        assignment = store.find_assignment(str(record.get("assignment_id") or ""))
        if assignment is None:
            continue
        if assignment.status == AssignmentStatus.QUEUED:
            assignment.transition_to(AssignmentStatus.SUPERSEDED)
        elif assignment.status in {
            AssignmentStatus.ASSIGNED,
            AssignmentStatus.WORKING,
            AssignmentStatus.BLOCKED,
        }:
            assignment.transition_to(AssignmentStatus.ABANDONED)
        else:
            continue
        assignment.progress_summary = reason
        store.update_assignment(assignment)
        store.archive_assignment(assignment, executive_summary=reason)
    meeting["active_wave_id"] = None
    meeting["active_phase"] = None
    meeting["active_round"] = None


def amend_packet(
    store: StateStore,
    meeting_id: str,
    *,
    actor_id: str,
    evidence_refs: Iterable[dict[str, Any] | str] | None = None,
    original_request: str | None = None,
    acceptance_criteria: Iterable[str] | None = None,
    scope: dict[str, Any] | None = None,
    known_constraints: Iterable[str] | None = None,
    declared_assumptions: Iterable[str] | None = None,
    approved_roster: list[dict[str, Any]] | None = None,
    replacement_review: bool = False,
) -> dict[str, Any]:
    meeting = store.find_staff_meeting(meeting_id)
    if meeting is None:
        raise ValueError(f"unknown Staff Meeting: {meeting_id}")
    if actor_id != meeting["chair_agent_id"]:
        raise PermissionError("only the chair may unfreeze and amend the packet")
    if meeting["status"] in TERMINAL_MEETING_STATUSES:
        raise ValueError("a terminal Staff Meeting packet cannot be amended")
    if not replacement_review and int(meeting.get("unfreeze_count") or 0) >= 1:
        raise ValueError("the Staff Meeting packet may be unfrozen only once")
    material = any(
        value is not None
        for value in (
            original_request,
            acceptance_criteria,
            scope,
            known_constraints,
            declared_assumptions,
            approved_roster,
        )
    )
    if evidence_refs is None and not material:
        raise ValueError("packet amendment requires evidence or a material field change")
    previous_status = str(meeting["status"])
    _archive_active_wave(store, meeting, reason="superseded by packet amendment")
    if original_request is not None:
        request = redact_sensitive_text(original_request.strip())
        if not request:
            raise ValueError("original_request cannot be empty")
        meeting["original_request"] = request
        meeting["original_request_hash"] = hashlib.sha256(request.encode("utf-8")).hexdigest()
    if acceptance_criteria is not None:
        criteria = [
            redact_sensitive_text(str(item).strip())
            for item in acceptance_criteria
            if str(item).strip()
        ]
        if not criteria:
            raise ValueError("acceptance_criteria cannot be empty")
        meeting["acceptance_criteria"] = criteria
    if scope is not None:
        meeting["scope"] = redact_sensitive_data(scope)
    if known_constraints is not None:
        meeting["known_constraints"] = redact_sensitive_data(list(known_constraints))
    if declared_assumptions is not None:
        meeting["declared_assumptions"] = redact_sensitive_data(list(declared_assumptions))
    if approved_roster is not None:
        selected = _normalize_roster(store, meeting, approved_roster)
        _validate_roster(selected, meeting["chair_agent_id"])
        meeting["approved_roster"] = selected
        meeting["seat_count"] = len(selected)
        meeting["quorum_required"] = quorum_required(len(selected))
    if evidence_refs is not None:
        meeting["evidence_refs"] = [
            *meeting.get("evidence_refs", []),
            *redact_sensitive_data(list(evidence_refs)),
        ]
    if replacement_review:
        meeting["replacement_review_count"] = int(
            meeting.get("replacement_review_count") or 0
        ) + 1
    else:
        meeting["unfreeze_count"] = 1
    meeting["packet_version"] = int(meeting["packet_version"]) + 1
    meeting["ballots_revealed"] = False
    meeting["vote_result"] = None
    meeting["status"] = MeetingStatus.PACKET_FROZEN.value
    meeting["updated_at"] = utc_now_iso()
    store.upsert_staff_meeting(meeting)
    _record_transition(
        store,
        meeting,
        previous_status,
        MeetingStatus.PACKET_FROZEN.value,
        actor_id=actor_id,
        reason="chair unfroze and amended the packet",
    )
    packet = {
        "packet_version": meeting["packet_version"],
        "original_request": meeting["original_request"],
        "original_request_hash": meeting["original_request_hash"],
        "acceptance_criteria": meeting["acceptance_criteria"],
        "scope": meeting["scope"],
        "desired_deliverable": meeting["desired_deliverable"],
        "known_constraints": meeting["known_constraints"],
        "declared_assumptions": meeting["declared_assumptions"],
        "evidence_refs": meeting["evidence_refs"],
        "approved_roster": meeting["approved_roster"],
        "resource_limits": meeting["resource_limits"],
        "quorum_ratio": meeting["quorum_ratio"],
        "quorum_required": meeting["quorum_required"],
        "minimum_distinct_agents": meeting["minimum_distinct_agents"],
        "minimum_substantive_votes": meeting["minimum_substantive_votes"],
        "role_catalog_version": meeting["role_catalog_version"],
        "prompt_version": "staff-meeting-prompts:v1",
        "frozen_at": utc_now_iso(),
        "frozen_by": actor_id,
        "amendment_kind": (
            "replacement_review"
            if replacement_review
            else ("material" if material else "evidence_only")
        ),
    }
    _append_record(
        store,
        meeting,
        "packet",
        packet,
        idempotency_key=f"packet:{meeting['packet_version']}",
        phase=MeetingStatus.PACKET_FROZEN.value,
        agent_id=actor_id,
    )
    if material:
        meeting["current_round"] = 0
        return dispatch_independent_review(store, meeting_id, actor_id=actor_id)
    meeting["status"] = MeetingStatus.SYNTHESIS.value
    meeting["updated_at"] = utc_now_iso()
    store.upsert_staff_meeting(meeting)
    _record_transition(
        store,
        meeting,
        MeetingStatus.PACKET_FROZEN.value,
        MeetingStatus.SYNTHESIS.value,
        actor_id=actor_id,
        reason="evidence-only amendment grants one new full-panel discussion loop",
    )
    _dispatch_deliberation(store, meeting, int(meeting.get("current_round") or 0) + 1)
    return meeting


def replace_veto_reviewer(
    store: StateStore,
    meeting_id: str,
    *,
    actor_id: str,
    veto_id: str,
    replacement_agent: Agent,
) -> dict[str, Any]:
    meeting = store.find_staff_meeting(meeting_id)
    if meeting is None:
        raise ValueError(f"unknown Staff Meeting: {meeting_id}")
    if actor_id != meeting["chair_agent_id"]:
        raise PermissionError("only the chair may replace an unavailable veto reviewer")
    records = store.staff_meeting_records(meeting_id)
    veto_record = next(
        (
            item
            for item in reversed(records)
            if item.get("record_kind") == "veto"
            and item.get("payload", {}).get("veto_id") == veto_id
            and item.get("payload", {}).get("status") != "withdrawn"
        ),
        None,
    )
    if veto_record is None:
        raise ValueError("unknown or already withdrawn veto")
    seat_id = str(veto_record["payload"]["issued_by_role_seat_id"])
    roster = [dict(item) for item in meeting["approved_roster"]]
    seat = next(item for item in roster if item["role_seat_id"] == seat_id)
    if replacement_agent.agent_id == seat["agent_id"]:
        raise ValueError("replacement reviewer must be a different agent")
    if any(
        item.get("veto_domain") and item.get("agent_id") == replacement_agent.agent_id
        for item in roster
        if item["role_seat_id"] != seat_id
    ):
        raise ValueError("replacement agent already holds a veto-capable role")
    previous_agent = seat["agent_id"]
    seat["agent_id"] = replacement_agent.agent_id
    seat["agent_display_name"] = replacement_agent.display_name
    seat["replacement_for_agent_id"] = previous_agent
    meeting["eligible_agent_ids"] = sorted(
        {*(meeting.get("eligible_agent_ids") or []), replacement_agent.agent_id}
    )
    store.upsert_staff_meeting(meeting)
    _append_record(
        store,
        meeting,
        "veto",
        {
            **veto_record["payload"],
            "replacement_agent_id": replacement_agent.agent_id,
            "status": "issued",
        },
        idempotency_key=f"veto-replacement:{veto_id}:{replacement_agent.agent_id}",
        role_seat_id=seat_id,
        role_key=seat["role_key"],
        phase=meeting["status"],
        agent_id=replacement_agent.agent_id,
        supersedes_record_id=veto_record["record_id"],
    )
    return amend_packet(
        store,
        meeting_id,
        actor_id=actor_id,
        approved_roster=roster,
        replacement_review=True,
    )


def cancel_staff_meeting(
    store: StateStore,
    meeting_id: str,
    *,
    actor_id: str,
    reason: str,
) -> dict[str, Any]:
    meeting = store.find_staff_meeting(meeting_id)
    if meeting is None:
        raise ValueError(f"unknown Staff Meeting: {meeting_id}")
    if actor_id != meeting["chair_agent_id"]:
        raise PermissionError("only the chair may cancel a Staff Meeting")
    if meeting["status"] in TERMINAL_MEETING_STATUSES:
        return meeting
    previous = str(meeting["status"])
    _archive_active_wave(store, meeting, reason="Staff Meeting cancelled")
    meeting["status"] = MeetingStatus.CANCELLED.value
    meeting["termination_reason"] = redact_sensitive_text(reason)
    meeting["updated_at"] = utc_now_iso()
    meeting["completed_at"] = meeting["updated_at"]
    store.upsert_staff_meeting(meeting)
    _record_transition(
        store,
        meeting,
        previous,
        MeetingStatus.CANCELLED.value,
        actor_id=actor_id,
        reason=reason,
    )
    return meeting


def replay_staff_meeting_state(
    meeting: dict[str, Any],
    records: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Fold immutable records into the auditable state fields used for replay checks."""
    state = {
        "meeting_id": meeting["meeting_id"],
        "status": None,
        "packet_version": 0,
        "current_round": 0,
        "ballot_round": 0,
        "ballots_revealed": False,
        "vote_result": None,
        "record_counts": Counter(),
    }
    ordered_records = list(records)
    for record in ordered_records:
        kind = str(record.get("record_kind") or "")
        state["record_counts"][kind] += 1
        payload = record.get("payload", {})
        if kind == "event" and payload.get("event_type") == "state_transition":
            state["status"] = payload.get("status")
        elif kind == "packet":
            state["packet_version"] = max(
                state["packet_version"], int(payload.get("packet_version") or 0)
            )
        elif kind == "assignment":
            phase = payload.get("phase")
            round_number = int(payload.get("round_number") or 0)
            if phase == MeetingStatus.DELIBERATION.value:
                state["current_round"] = max(state["current_round"], round_number)
            elif phase == MeetingStatus.FINAL_VOTE.value:
                state["ballot_round"] = max(state["ballot_round"], round_number)
        elif kind == "ballot":
            state["ballots_revealed"] = bool(meeting.get("ballots_revealed"))
    if state["ballot_round"]:
        latest_ballots: dict[str, dict[str, Any]] = {}
        for record in ordered_records:
            if (
                record.get("record_kind") == "ballot"
                and int(record.get("round_number") or 0) == state["ballot_round"]
            ):
                latest_ballots[str(record.get("role_seat_id") or record["record_id"])] = {
                    **record.get("payload", {}),
                    "agent_id": record.get("agent_id"),
                }
        state["vote_result"] = evaluate_vote(
            latest_ballots.values(),
            seat_count=int(meeting["seat_count"]),
            quorum_ratio=float(meeting.get("quorum_ratio") or DEFAULT_QUORUM_RATIO),
            min_distinct_agents=int(
                meeting.get("minimum_distinct_agents") or DEFAULT_MIN_DISTINCT_AGENTS
            ),
            min_substantive_votes=int(
                meeting.get("minimum_substantive_votes")
                or DEFAULT_MIN_SUBSTANTIVE_VOTES
            ),
            unresolved_vetoes=_effective_vetoes(ordered_records),
        )
    state["record_counts"] = dict(state["record_counts"])
    return state
