from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from brigade.profile import derive_agent_profile
from brigade.schemas import Agent, Assignment, AssignmentStatus, Team
from brigade.store import StateStore
from brigade.time import parse_utc_iso, utc_now
from brigade.tools import ToolRegistry, tool_manifest, workspace_tool_manifest

DEFAULT_STALE_WORK_SECONDS = 86_400
IMBALANCED_QUEUE_DEPTH = 2

ORCHESTRATOR_SYSTEM_PROMPT = "\n".join(
    [
        "You are the OpenBrigade Orchestrator.",
        "Every tick, protect the mission by checking goal freshness and crew load.",
        "Do not escalate normal progress, queued work that has capacity, or long-running work "
        "with a future checkpoint.",
        "Escalate only stalled goals, stale active tasks, repeated blockers, or clear load "
        "imbalance.",
        "Prefer the smallest safe action that restores progress.",
    ]
)

ORCHESTRATOR_WORKSPACE_DIRNAME = "workspace-orchestrator"
ORCHESTRATOR_SYSTEM_PROMPT_FILENAME = "SYSTEM_PROMPT.md"
ORCHESTRATOR_NOTES_FILENAME = "NOTES.md"
MAX_ORCHESTRATOR_NOTES_CHARS = 8000


def orchestrator_workspace_path(store: StateStore) -> Path:
    return store.data_dir / ORCHESTRATOR_WORKSPACE_DIRNAME


def orchestrator_system_prompt(store: StateStore) -> str:
    """The orchestrator's effective system prompt.

    Falls back to the built-in default until the operator (via chat) writes
    workspace-orchestrator/SYSTEM_PROMPT.md, at which point that file's
    content wins for every subsequent cycle and chat turn."""
    path = orchestrator_workspace_path(store) / ORCHESTRATOR_SYSTEM_PROMPT_FILENAME
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    return ORCHESTRATOR_SYSTEM_PROMPT


def write_orchestrator_system_prompt(store: StateStore, content: str) -> None:
    workspace = orchestrator_workspace_path(store)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ORCHESTRATOR_SYSTEM_PROMPT_FILENAME).write_text(
        content.strip() + "\n", encoding="utf-8"
    )


def read_orchestrator_notes(store: StateStore) -> str:
    path = orchestrator_workspace_path(store) / ORCHESTRATOR_NOTES_FILENAME
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def write_orchestrator_notes(store: StateStore, content: str, *, append: bool = True) -> None:
    workspace = orchestrator_workspace_path(store)
    workspace.mkdir(parents=True, exist_ok=True)
    path = workspace / ORCHESTRATOR_NOTES_FILENAME
    if append and path.exists():
        existing = path.read_text(encoding="utf-8")
        combined = (existing.rstrip() + "\n" + content.strip() + "\n") if existing.strip() else (
            content.strip() + "\n"
        )
    else:
        combined = content.strip() + "\n"
    if len(combined) > MAX_ORCHESTRATOR_NOTES_CHARS:
        combined = combined[-MAX_ORCHESTRATOR_NOTES_CHARS :]
    path.write_text(combined, encoding="utf-8")

CHAT_MEMORY_FILENAME = "CHAT_MEMORY.md"
MAX_CHAT_MEMORY_CHARS = 8000


def _chat_memory_path(store: StateStore, agent_id: str | None) -> Path:
    """Curated chat memory lives next to the persona's other workspace files;
    the front desk (agent_id=None) shares the orchestrator workspace."""
    if agent_id is None:
        return orchestrator_workspace_path(store) / CHAT_MEMORY_FILENAME
    agent = next((item for item in store.agents() if item.agent_id == agent_id), None)
    if agent is None:
        return orchestrator_workspace_path(store) / CHAT_MEMORY_FILENAME
    return store.data_dir / agent.workspace_path / CHAT_MEMORY_FILENAME


def read_agent_chat_notes(store: StateStore, agent_id: str | None) -> str:
    path = _chat_memory_path(store, agent_id)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def write_agent_chat_notes(
    store: StateStore,
    agent_id: str | None,
    content: str,
    *,
    append: bool = True,
) -> None:
    path = _chat_memory_path(store, agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if append and path.exists():
        existing = path.read_text(encoding="utf-8")
        combined = (
            (existing.rstrip() + "\n" + content.strip() + "\n")
            if existing.strip()
            else (content.strip() + "\n")
        )
    else:
        combined = content.strip() + "\n"
    if len(combined) > MAX_CHAT_MEMORY_CHARS:
        combined = combined[-MAX_CHAT_MEMORY_CHARS:]
    path.write_text(combined, encoding="utf-8")


CREW_CHIEF_SYSTEM_PROMPT = "\n".join(
    [
        "You are an OpenBrigade Crew Chief.",
        "Keep your team's goals moving before the Orchestrator has to intervene.",
        "Reassign or delegate team work when a goal is stale or an agent is overloaded.",
        "Route each task using the member profiles in agent_load: prefer the "
        "member whose declared or demonstrated specialties match it, then one "
        "whose built tools or recent completions fit; give generalists the "
        "remainder.",
    ]
)

CREW_CHIEF_CHAT_PROMPT = "\n".join(
    [
        "You are chatting directly with a human operator.",
        "Answer questions about live or historical work by CALLING TOOLS, never "
        "from memory: tool results in this prompt are real current state.",
        "To call a tool, reply with exactly one JSON object and nothing else:",
        '{"status":"tool_call","tool":"<name>","arguments":{...}}',
        "Call one tool at a time; its result appears under tool_observations on "
        "your next turn.",
        "When you have what you need, reply with your final answer as plain "
        "Markdown prose (no JSON). Keep answers short and concrete; cite task "
        "ids when you reference tasks.",
        "If the operator asks you to change state (create or cancel tasks, set "
        "priority, attach guidance, retry blocked work), do NOT apply it yet. "
        "Reply with exactly one JSON object describing your plan:",
        '{"status":"propose_actions","summary":"one sentence","actions":[...]}',
        "The operator must reply confirm before anything is applied.",
    ]
)

BASE_AGENT_SYSTEM_PROMPT = "\n".join(
    [
        "You are running inside OpenBrigade as an orchestrated agent harness.",
        "Work only on the active assignment and use tools for any needed local context.",
        "Do not invent completed work, external actions, files, or tool results.",
        (
            "Workspace convention: your own workspace is private to you. Paths "
            "prefixed shared/ live in the team-shared workspace that every agent "
            "can read and write. Put any deliverable other agents need under "
            "shared/, and when your assignment references files you did not "
            "create, check shared/ (list_files on shared/) before reporting "
            "them missing."
        ),
    ]
)


def build_orchestrator_floor(
    store: StateStore,
    *,
    stale_seconds: int = DEFAULT_STALE_WORK_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or utc_now()
    mission = store.mission()
    return {
        "system_prompt": orchestrator_system_prompt(store),
        "mission": mission.to_dict() if mission else None,
        "stale_work_seconds": stale_seconds,
        "goals": build_goal_snapshots(store, stale_seconds=stale_seconds, now=now),
        "crew_chief_load": build_crew_chief_load(store),
    }


def build_crew_chief_floor(
    store: StateStore,
    chief_id: str,
    *,
    stale_seconds: int = DEFAULT_STALE_WORK_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or utc_now()
    mission = store.mission()
    managed_agent_ids = sorted(_managed_agent_ids(store.teams(), chief_id))
    return {
        "system_prompt": CREW_CHIEF_SYSTEM_PROMPT,
        "mission": mission.to_dict() if mission else None,
        "chief": chief_id,
        "stale_work_seconds": stale_seconds,
        "goals": build_goal_snapshots(
            store,
            stale_seconds=stale_seconds,
            now=now,
            agent_ids=set(managed_agent_ids),
        ),
        "agent_load": build_agent_load(store, managed_agent_ids),
    }


def build_agent_floor(
    agent: Agent,
    assignment: Assignment,
    store: StateStore,
    registry: ToolRegistry,
    *,
    observations: list[dict[str, Any]] | None = None,
    stale_seconds: int = DEFAULT_STALE_WORK_SECONDS,
) -> dict[str, Any]:
    mission = store.mission()
    payload: dict[str, Any] = {
        "system_prompt": BASE_AGENT_SYSTEM_PROMPT,
        "mission": mission.to_dict() if mission else None,
        "agent": agent.to_dict(),
        "identity": _agent_identity(store, agent),
        "assignment": assignment.to_dict(),
        "goals": build_goal_snapshots(
            store,
            stale_seconds=stale_seconds,
            agent_ids={agent.agent_id},
        ),
        "dependency_state": dependency_state(store, assignment),
        "recent_agent_state": agent_state_context(store, agent.agent_id),
        "tool_observations": observations or [],
        "available_tools": [
            *tool_manifest(registry),
            *workspace_tool_manifest(store.data_dir / agent.workspace_path),
        ],
    }
    if payload["identity"] is None:
        del payload["identity"]
    if _is_crew_chief(agent, store.teams()):
        payload["crew_chief_floor"] = build_crew_chief_floor(
            store,
            agent.agent_id,
            stale_seconds=stale_seconds,
        )
    return payload


MAX_IDENTITY_CHARS = 4000


def _agent_identity(store: StateStore, agent: Agent) -> str | None:
    """The agent's own IDENTITY.md, truncated, so it acts in character
    without spending an iteration on read_file."""
    path = store.data_dir / agent.workspace_path / "IDENTITY.md"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    return text[:MAX_IDENTITY_CHARS]


def build_goal_snapshots(
    store: StateStore,
    *,
    stale_seconds: int = DEFAULT_STALE_WORK_SECONDS,
    now: datetime | None = None,
    agent_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    now = now or utc_now()
    assignments = store.assignments()
    history = store.assignment_history()
    chief_by_agent = _chief_by_agent(store.teams())
    snapshots = []
    for record in store.goal_records():
        agent_id = str(record["agent_id"])
        if agent_ids is not None and agent_id not in agent_ids:
            continue
        goal = record["goal"]
        linked_open = _linked_open_assignments(assignments, agent_id, goal.statement)
        linked_done = _linked_history(history, agent_id, goal.statement)
        activity_values = [goal.set_at]
        activity_values.extend(item.updated_at for item in linked_open)
        activity_values.extend(str(item.get("archived_at") or "") for item in linked_done)
        last_activity = _latest_iso(activity_values)
        suppressed_until = _future_checkpoint(linked_open, now)
        on_call = goal.engagement_mode == "on_call"
        stale = (
            not on_call
            and last_activity is not None
            and _age_seconds(last_activity, now) > stale_seconds
            and suppressed_until is None
            and not (not linked_open and linked_done)
        )
        status = _goal_status(stale, linked_open, linked_done)
        if on_call and status == "unworked":
            status = "on_call"
        snapshots.append(
            {
                "id": str(record["goal_id"]),
                "agent_id": agent_id,
                "title": goal.statement,
                "status": status,
                "engagement_mode": goal.engagement_mode,
                "last_activity": last_activity,
                "tasks_open": len(linked_open),
                "tasks_done": len(linked_done),
                "owning_crew_chief": chief_by_agent.get(agent_id),
                "expected_next_activity_at": suppressed_until,
                "stale": stale,
            }
        )
    return sorted(snapshots, key=lambda item: (item["agent_id"], item["title"]))


def build_crew_chief_load(store: StateStore) -> list[dict[str, Any]]:
    agents = {agent.agent_id: agent for agent in store.agents()}
    teams = store.teams()
    chiefs = sorted(
        {
            *(team.crew_chief_id for team in teams if team.crew_chief_id),
            *(agent.agent_id for agent in agents.values() if agent.role == "crew_chief"),
        }
    )
    return [
        _load_for_chief(chief_id, teams, store.assignments(), store.agent_states())
        for chief_id in chiefs
        if chief_id in agents
    ]


def build_chat_status_context(store: StateStore, agent_id: str) -> dict[str, Any]:
    """Live status context for chat, so a chief answers about current work,
    priorities, and blockers from state, not memory.

    Chiefs see goals with engagement modes, member load and specialties,
    queue depth, active work with progress, blockers, awaiting-human items,
    and recent team alerts. Line workers see their own state and active
    assignment.
    """
    agent = next(
        (item for item in store.agents() if item.agent_id == agent_id), None
    )
    teams = store.teams()
    assignments = store.assignments()
    own_active = [
        item.to_dict()
        for item in assignments
        if item.assigned_to == agent_id
        and item.status
        in {
            AssignmentStatus.ASSIGNED,
            AssignmentStatus.WORKING,
            AssignmentStatus.BLOCKED,
        }
    ]
    context: dict[str, Any] = {
        "agent_id": agent_id,
        "role": "crew_chief"
        if agent is not None and _is_crew_chief(agent, teams)
        else "line_worker",
        "state": agent_state_context(store, agent_id),
        "active_assignments": own_active,
    }
    if context["role"] != "crew_chief":
        return context

    managed = sorted(_managed_agent_ids(teams, agent_id))
    goals = [
        {
            "agent_id": goal_agent_id,
            "statement": goal.statement,
            "engagement_mode": getattr(goal, "engagement_mode", "directive"),
        }
        for goal_agent_id, agent_goals in store.goals().items()
        if goal_agent_id in managed
        for goal in agent_goals
    ]
    team_assignments = [item for item in assignments if item.assigned_to in managed]
    queued = [
        item for item in team_assignments if item.status == AssignmentStatus.QUEUED
    ]
    active = [
        item
        for item in team_assignments
        if item.status
        in {
            AssignmentStatus.ASSIGNED,
            AssignmentStatus.WORKING,
            AssignmentStatus.BLOCKED,
        }
    ]
    blocked = [item for item in active if item.status == AssignmentStatus.BLOCKED]
    managed_terms = set(managed) | {
        item.assignment_id for item in team_assignments
    }
    team_alerts = [
        alert
        for alert in store.alerts()
        if any(term in alert for term in managed_terms)
    ][-5:]
    context.update(
        {
            "goals": goals,
            "member_load": build_agent_load(store, managed),
            "queue_depth": len(queued),
            "queued": [
                {
                    "assignment_id": item.assignment_id,
                    "assigned_to": item.assigned_to,
                    "priority": item.priority.value,
                    "assignment": item.assignment[:160],
                }
                for item in queued
            ],
            "active_work": [
                {
                    "assignment_id": item.assignment_id,
                    "assigned_to": item.assigned_to,
                    "status": item.status.value,
                    "priority": item.priority.value,
                    "assignment": item.assignment[:160],
                    "progress_summary": item.progress_summary,
                }
                for item in active
            ],
            "blockers": [
                {
                    "assignment_id": item.assignment_id,
                    "assigned_to": item.assigned_to,
                    "blockers": item.blockers,
                    "last_error": item.last_error,
                    "consecutive_failures": item.consecutive_failures,
                }
                for item in blocked
            ],
            "awaiting_human": [
                item.assignment_id for item in blocked if item.awaiting_human
            ],
            "team_alerts": team_alerts,
        }
    )
    return context


def build_agent_load(store: StateStore, agent_ids: list[str]) -> list[dict[str, Any]]:
    assignments = store.assignments()
    states = store.agent_states()
    agents_by_id = {agent.agent_id: agent for agent in store.agents()}
    history = store.assignment_history()
    rows = []
    for agent_id in agent_ids:
        queued = [
            item
            for item in assignments
            if item.assigned_to == agent_id and item.status == AssignmentStatus.QUEUED
        ]
        open_items = [
            item
            for item in assignments
            if item.assigned_to == agent_id
            and item.status
            in {
                AssignmentStatus.ASSIGNED,
                AssignmentStatus.WORKING,
                AssignmentStatus.BLOCKED,
            }
        ]
        state = states.get(agent_id)
        agent = agents_by_id.get(agent_id)
        row = {
            "agent": agent_id,
            "state": state.status if state else ("busy" if open_items else "idle"),
            "queue_depth": len(queued),
            "open_tasks": len(open_items),
            "role": agent.role if agent else "line_worker",
            "specialties": agent.specialties if agent else [],
        }
        if agent is not None:
            row.update(derive_agent_profile(store, agent, history=history))
        rows.append(row)
    return rows


def dependency_state(store: StateStore, assignment: Assignment) -> list[dict[str, Any]]:
    if not assignment.dependency_ids:
        return []
    active = {item.assignment_id: item for item in store.assignments()}
    history = {
        item.get("assignment_id"): item
        for item in store.assignment_history()
        if item.get("assignment_id")
    }
    dependencies = []
    for dependency_id in assignment.dependency_ids:
        active_assignment = active.get(dependency_id)
        if active_assignment is not None:
            dependencies.append(
                {
                    "assignment_id": dependency_id,
                    "status": active_assignment.status.value,
                    "complete": active_assignment.status == AssignmentStatus.COMPLETE,
                    "summary": active_assignment.progress_summary,
                }
            )
            continue
        archived = history.get(dependency_id)
        if archived is not None:
            dependencies.append(
                {
                    "assignment_id": dependency_id,
                    "status": archived.get("final_status"),
                    "complete": archived.get("final_status")
                    == AssignmentStatus.COMPLETE.value,
                    "summary": archived.get("executive_summary"),
                }
            )
            continue
        dependencies.append(
            {
                "assignment_id": dependency_id,
                "status": "unknown",
                "complete": False,
                "summary": None,
            }
        )
    return dependencies


def agent_state_context(store: StateStore, agent_id: str) -> dict[str, Any] | None:
    state = store.agent_states().get(agent_id)
    return state.to_dict() if state else None


def _load_for_chief(
    chief_id: str,
    teams: list[Team],
    assignments: list[Assignment],
    states: dict[str, Any],
) -> dict[str, Any]:
    managed = _managed_agent_ids(teams, chief_id)
    queued = [
        item
        for item in assignments
        if item.assigned_to in managed and item.status == AssignmentStatus.QUEUED
    ]
    open_items = [
        item
        for item in assignments
        if item.assigned_to in managed
        and item.status
        in {
            AssignmentStatus.ASSIGNED,
            AssignmentStatus.WORKING,
            AssignmentStatus.BLOCKED,
        }
    ]
    chief_active = any(item.assigned_to == chief_id for item in open_items)
    state = states.get(chief_id)
    team_ids = [team.team_id for team in teams if team.crew_chief_id == chief_id]
    return {
        "chief": chief_id,
        "state": state.status if state else ("busy" if chief_active else "idle"),
        "queue_depth": len(queued),
        "open_tasks": len(open_items),
        "team_ids": sorted(team_ids),
        "agents": sorted(managed),
    }


def _managed_agent_ids(teams: list[Team], chief_id: str) -> set[str]:
    managed = {chief_id}
    for team in teams:
        if team.crew_chief_id != chief_id:
            continue
        managed.update(team.members)
    return managed


def _chief_by_agent(teams: list[Team]) -> dict[str, str]:
    chief_by_agent: dict[str, str] = {}
    for team in teams:
        if not team.crew_chief_id:
            continue
        for agent_id in team.members:
            chief_by_agent[agent_id] = team.crew_chief_id
        chief_by_agent[team.crew_chief_id] = team.crew_chief_id
    return chief_by_agent


def _is_crew_chief(agent: Agent, teams: list[Team]) -> bool:
    return agent.role == "crew_chief" or any(
        team.crew_chief_id == agent.agent_id for team in teams
    )


def _linked_open_assignments(
    assignments: list[Assignment],
    agent_id: str,
    goal_statement: str,
) -> list[Assignment]:
    return [
        item
        for item in assignments
        if item.assigned_to == agent_id and item.goal_statement == goal_statement
    ]


def _linked_history(
    history: list[dict[str, Any]],
    agent_id: str,
    goal_statement: str,
) -> list[dict[str, Any]]:
    linked = []
    for item in history:
        record = item.get("record")
        if not isinstance(record, dict):
            continue
        if record.get("assigned_to") != agent_id:
            continue
        if record.get("goal_statement") != goal_statement:
            continue
        if item.get("final_status") == AssignmentStatus.COMPLETE.value:
            linked.append(item)
    return linked


def _goal_status(
    stale: bool,
    linked_open: list[Assignment],
    linked_done: list[dict[str, Any]],
) -> str:
    if stale:
        return "stale"
    if linked_open:
        return "active"
    if linked_done:
        return "done"
    return "unworked"


def _latest_iso(values: list[str]) -> str | None:
    parsed = []
    for value in values:
        if not value:
            continue
        try:
            parsed.append(parse_utc_iso(value))
        except ValueError:
            continue
    if not parsed:
        return None
    return max(parsed).isoformat()


def _future_checkpoint(assignments: list[Assignment], now: datetime) -> str | None:
    future = []
    for item in assignments:
        if not item.checkpoint_at:
            continue
        try:
            checkpoint = parse_utc_iso(item.checkpoint_at)
        except ValueError:
            continue
        if checkpoint > now:
            future.append(checkpoint)
    if not future:
        return None
    return min(future).isoformat()


def _age_seconds(value: str, now: datetime) -> float:
    return (now - parse_utc_iso(value)).total_seconds()


def compact_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)
