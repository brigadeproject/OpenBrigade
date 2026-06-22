from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from brigade.schemas import (
    Agent,
    AgentState,
    Assignment,
    AssignmentStatus,
    ChatMessage,
    Goal,
    Mission,
    Team,
    User,
    agent_from_dict,
    agent_state_from_dict,
    assignment_from_dict,
    chat_message_from_dict,
    goal_from_dict,
    mission_from_dict,
    team_from_dict,
    user_from_dict,
)

EMPTY_STATE: dict[str, Any] = {
    "mission": None,
    "users": [],
    "agents": [],
    "teams": [],
    "agent_states": {},
    "goals": {},
    "assignments": [],
    "assignment_history": [],
    "alerts": [],
    "knowledge_documents": [],
    "knowledge_chunks": [],
    "messages": [],
    "orchestrator_reasoning": [],
    "proposals": [],
    "recurrences": [],
    "usage_records": [],
    "cloud_jobs": [],
    "financial_reports": [],
    "transcripts": [],
    "episodes": [],
    "provenance_records": [],
    "connector_audit_events": [],
    "external_identities": [],
    "local_inference": {
        "status": "idle",
        "holder": None,
        "last_completed": None,
        "next_available": None,
    },
}


class JsonStateStore:
    """Small local repository layer used until external datastore adapters are wired in."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data_dir = path.parent

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return json.loads(json.dumps(EMPTY_STATE))
        state = json.loads(self.path.read_text(encoding="utf-8"))
        merged = json.loads(json.dumps(EMPTY_STATE))
        merged.update(state)
        return merged

    def save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

    def add_assignment(self, assignment: Assignment) -> Assignment:
        state = self.load()
        existing = _assignment_by_idempotency_key(state, assignment.idempotency_key)
        if existing is not None:
            return existing
        state.setdefault("assignments", []).append(assignment.to_dict())
        self.save(state)
        return assignment

    def assignments(self) -> list[Assignment]:
        return [assignment_from_dict(item) for item in self.load().get("assignments", [])]

    def active_assignment_for_agent(self, agent_id: str) -> Assignment | None:
        return _active_assignment_for_agent(self.assignments(), agent_id)

    def find_assignment(self, assignment_id: str) -> Assignment | None:
        return next(
            (item for item in self.assignments() if item.assignment_id == assignment_id),
            None,
        )

    def find_assignment_by_idempotency_key(self, idempotency_key: str) -> Assignment | None:
        return _assignment_by_idempotency_key(self.load(), idempotency_key)

    def replace_assignments(self, assignments: list[Assignment]) -> None:
        state = self.load()
        state["assignments"] = [item.to_dict() for item in assignments]
        self.save(state)

    def update_assignment(self, assignment: Assignment) -> None:
        assignments = self.assignments()
        replaced = False
        updated: list[Assignment] = []
        for item in assignments:
            if item.assignment_id == assignment.assignment_id:
                updated.append(assignment)
                replaced = True
            else:
                updated.append(item)
        if not replaced:
            updated.append(assignment)
        self.replace_assignments(updated)

    def archive_assignment(self, assignment: Assignment, executive_summary: str) -> None:
        state = self.load()
        state["assignments"] = [
            item
            for item in state.get("assignments", [])
            if item["assignment_id"] != assignment.assignment_id
        ]
        state.setdefault("assignment_history", []).append(
            {
                "assignment_id": assignment.assignment_id,
                "archived_at": assignment.updated_at,
                "final_status": assignment.status.value,
                "executive_summary": executive_summary,
                "failure_info": assignment.last_error,
                "record": assignment.to_dict(),
            }
        )
        self.save(state)

    def assignment_history(self) -> list[dict[str, Any]]:
        return list(self.load().get("assignment_history", []))

    def set_mission(self, mission: Mission) -> None:
        state = self.load()
        state["mission"] = mission.to_dict()
        self.save(state)

    def mission(self) -> Mission | None:
        mission = self.load().get("mission")
        return mission_from_dict(mission) if mission else None

    def add_user(self, user: User) -> None:
        state = self.load()
        users = [item for item in state.get("users", []) if item["username"] != user.username]
        users.append(user.to_dict())
        state["users"] = users
        self.save(state)

    def users(self) -> list[User]:
        return [user_from_dict(item) for item in self.load().get("users", [])]

    def add_agent(self, agent: Agent) -> None:
        state = self.load()
        agents = [item for item in state.get("agents", []) if item["agent_id"] != agent.agent_id]
        agents.append(agent.to_dict())
        state["agents"] = agents
        self.save(state)

    def agents(self) -> list[Agent]:
        return [agent_from_dict(item) for item in self.load().get("agents", [])]

    def delete_agent(self, agent_id: str) -> None:
        state = self.load()
        state["agents"] = [
            item for item in state.get("agents", []) if item["agent_id"] != agent_id
        ]
        self.save(state)

    def upsert_team(self, team: Team) -> None:
        state = self.load()
        teams = [item for item in state.get("teams", []) if item["team_id"] != team.team_id]
        teams.append(team.to_dict())
        state["teams"] = teams
        self.save(state)

    def teams(self) -> list[Team]:
        return [team_from_dict(item) for item in self.load().get("teams", [])]

    def upsert_agent_state(self, agent_state: AgentState) -> None:
        state = self.load()
        state.setdefault("agent_states", {})[agent_state.agent] = agent_state.to_dict()
        self.save(state)

    def agent_states(self) -> dict[str, AgentState]:
        return {
            agent: agent_state_from_dict(item)
            for agent, item in self.load().get("agent_states", {}).items()
        }

    def add_goal(self, agent_id: str, goal: Goal) -> None:
        state = self.load()
        goals = state.setdefault("goals", {})
        goals.setdefault(agent_id, []).append(goal.to_dict())
        self.save(state)

    def ensure_goal(self, agent_id: str, goal: Goal) -> bool:
        state = self.load()
        goals = state.setdefault("goals", {})
        existing = goals.setdefault(agent_id, [])
        goal_key = _goal_identity(goal.to_dict())
        if any(_goal_identity(item) == goal_key for item in existing):
            return False
        existing.append(goal.to_dict())
        self.save(state)
        return True

    def dedupe_goals(self, agent_id: str | None = None) -> bool:
        state = self.load()
        goals_by_agent = state.setdefault("goals", {})
        changed = False
        target_agents = [agent_id] if agent_id is not None else list(goals_by_agent.keys())
        for target in target_agents:
            existing = list(goals_by_agent.get(target, []))
            seen: set[tuple[object, ...]] = set()
            deduped: list[dict[str, Any]] = []
            for item in existing:
                goal_key = _goal_identity(item)
                if goal_key in seen:
                    changed = True
                    continue
                seen.add(goal_key)
                deduped.append(item)
            if target in goals_by_agent and deduped != existing:
                goals_by_agent[target] = deduped
        if changed:
            self.save(state)
        return changed

    def goals(self, agent_id: str | None = None) -> dict[str, list[Goal]]:
        raw_goals = self.load().get("goals", {})
        if agent_id is not None:
            return {agent_id: [goal_from_dict(item) for item in raw_goals.get(agent_id, [])]}
        return {
            key: [goal_from_dict(item) for item in values]
            for key, values in raw_goals.items()
        }

    def goal_records(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for current_agent_id, goals in self.goals(agent_id).items():
            for goal in goals:
                payload = {
                    "agent_id": current_agent_id,
                    "goal": goal.to_dict(),
                }
                digest = hashlib.sha256(
                    json.dumps(payload, sort_keys=True).encode("utf-8")
                ).hexdigest()[:16]
                records.append(
                    {
                        "goal_id": f"goal:{current_agent_id}:{digest}",
                        "agent_id": current_agent_id,
                        "goal": goal,
                    }
                )
        return records

    def add_alert(self, message: str) -> None:
        state = self.load()
        state.setdefault("alerts", []).append(message)
        self.save(state)

    def alerts(self) -> list[str]:
        return list(self.load().get("alerts", []))

    def clear_alerts(self) -> int:
        state = self.load()
        count = len(state.get("alerts", []))
        state["alerts"] = []
        self.save(state)
        return count

    def add_knowledge_document(self, document: dict[str, Any]) -> None:
        state = self.load()
        state.setdefault("knowledge_documents", []).append(document)
        self.save(state)

    def knowledge_documents(self) -> list[dict[str, Any]]:
        return list(self.load().get("knowledge_documents", []))

    def add_knowledge_chunk(self, chunk: dict[str, Any]) -> None:
        state = self.load()
        state.setdefault("knowledge_chunks", []).append(chunk)
        self.save(state)

    def knowledge_chunks(self, document_id: str | None = None) -> list[dict[str, Any]]:
        chunks = list(self.load().get("knowledge_chunks", []))
        if document_id is None:
            return chunks
        return [chunk for chunk in chunks if chunk.get("document_id") == document_id]

    def add_message(self, message: ChatMessage) -> None:
        state = self.load()
        state.setdefault("messages", []).append(message.to_dict())
        self.save(state)

    def messages(self, channel: str | None = None) -> list[ChatMessage]:
        messages = [chat_message_from_dict(item) for item in self.load().get("messages", [])]
        if channel is None:
            return messages
        return [message for message in messages if message.channel == channel]

    def add_orchestrator_reasoning(self, record: dict[str, Any]) -> None:
        state = self.load()
        state.setdefault("orchestrator_reasoning", []).append(record)
        self.save(state)

    def orchestrator_reasoning(self) -> list[dict[str, Any]]:
        return list(self.load().get("orchestrator_reasoning", []))

    def add_proposal(self, proposal: dict[str, Any]) -> dict[str, Any]:
        state = self.load()
        idempotency_key = proposal.get("idempotency_key")
        if idempotency_key:
            existing = next(
                (
                    item
                    for item in state.get("proposals", [])
                    if item.get("idempotency_key") == idempotency_key
                ),
                None,
            )
            if existing is not None:
                return dict(existing)
        state.setdefault("proposals", []).append(dict(proposal))
        self.save(state)
        return proposal

    def proposals(
        self,
        kind: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        records = [dict(item) for item in self.load().get("proposals", [])]
        if kind is not None:
            records = [item for item in records if item.get("kind") == kind]
        if status is not None:
            records = [item for item in records if item.get("status") == status]
        return records

    def find_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        return next(
            (
                dict(item)
                for item in self.load().get("proposals", [])
                if item.get("proposal_id") == proposal_id
            ),
            None,
        )

    def update_proposal(self, proposal: dict[str, Any]) -> None:
        state = self.load()
        records = state.setdefault("proposals", [])
        for index, item in enumerate(records):
            if item.get("proposal_id") == proposal.get("proposal_id"):
                records[index] = dict(proposal)
                break
        else:
            records.append(dict(proposal))
        self.save(state)

    def add_recurrence(self, recurrence: dict[str, Any]) -> dict[str, Any]:
        state = self.load()
        state.setdefault("recurrences", []).append(dict(recurrence))
        self.save(state)
        return recurrence

    def recurrences(self, enabled: bool | None = None) -> list[dict[str, Any]]:
        records = [dict(item) for item in self.load().get("recurrences", [])]
        if enabled is not None:
            records = [item for item in records if bool(item.get("enabled", True)) == enabled]
        return sorted(records, key=lambda item: str(item.get("next_due_at") or ""))

    def update_recurrence(self, recurrence: dict[str, Any]) -> None:
        state = self.load()
        records = state.setdefault("recurrences", [])
        for index, item in enumerate(records):
            if item.get("recurrence_id") == recurrence.get("recurrence_id"):
                records[index] = dict(recurrence)
                break
        else:
            records.append(dict(recurrence))
        self.save(state)

    def add_usage_record(self, record: dict[str, Any]) -> None:
        state = self.load()
        state.setdefault("usage_records", []).append(record)
        self.save(state)

    def usage_records(self) -> list[dict[str, Any]]:
        return list(self.load().get("usage_records", []))

    def upsert_cloud_job(self, job: dict[str, Any]) -> None:
        state = self.load()
        jobs = [item for item in state.get("cloud_jobs", []) if item.get("job_id") != job["job_id"]]
        jobs.append(job)
        state["cloud_jobs"] = jobs
        self.save(state)

    def cloud_jobs(self, status: str | None = None) -> list[dict[str, Any]]:
        jobs = list(self.load().get("cloud_jobs", []))
        if status is None:
            return jobs
        return [job for job in jobs if job.get("status") == status]

    def set_financial_report(self, report: dict[str, Any]) -> None:
        state = self.load()
        reports = state.setdefault("financial_reports", [])
        reports.append(report)
        state["financial_reports"] = reports[-20:]
        self.save(state)

    def latest_financial_report(self) -> dict[str, Any] | None:
        reports = self.load().get("financial_reports", [])
        return reports[-1] if reports else None

    def set_local_inference(self, record: dict[str, Any]) -> None:
        state = self.load()
        state["local_inference"] = record
        self.save(state)

    def local_inference(self) -> dict[str, Any]:
        return dict(self.load().get("local_inference", EMPTY_STATE["local_inference"]))

    def add_transcript(self, transcript: dict[str, Any]) -> None:
        state = self.load()
        state.setdefault("transcripts", []).append(transcript)
        self.save(state)

    def transcripts(self) -> list[dict[str, Any]]:
        return list(self.load().get("transcripts", []))

    def add_episode(self, episode: dict[str, Any]) -> None:
        state = self.load()
        state.setdefault("episodes", []).append(episode)
        self.save(state)

    def episodes(self) -> list[dict[str, Any]]:
        return list(self.load().get("episodes", []))

    def search_episodes(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        terms = [term.lower() for term in query.split() if len(term) >= 3]
        matches = []
        for episode in reversed(self.episodes()):
            encoded = json.dumps(episode, sort_keys=True).lower()
            if terms and not any(term in encoded for term in terms):
                continue
            matches.append({"score": None, "payload": episode})
            if len(matches) >= limit:
                break
        return matches

    def add_provenance_record(self, record: dict[str, Any]) -> None:
        state = self.load()
        state.setdefault("provenance_records", []).append(record)
        self.save(state)

    def provenance_records(self) -> list[dict[str, Any]]:
        return list(self.load().get("provenance_records", []))

    def external_datastore_status(self) -> dict[str, dict[str, object]]:
        return {
            "qdrant": {"backend": "qdrant", "ok": False, "detail": "not configured"},
            "neo4j": {"backend": "neo4j", "ok": False, "detail": "not configured"},
        }

    def add_connector_audit_event(self, record: dict[str, Any]) -> None:
        state = self.load()
        state.setdefault("connector_audit_events", []).append(dict(record))
        self.save(state)

    def connector_audit_events(
        self,
        provider: str | None = None,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        records = list(self.load().get("connector_audit_events", []))
        if provider is not None:
            records = [record for record in records if record.get("provider") == provider]
        if limit is not None:
            records = records[-limit:]
        return records

    def upsert_external_identity(self, record: dict[str, Any]) -> None:
        state = self.load()
        identities = [
            item
            for item in state.get("external_identities", [])
            if not (
                item.get("provider") == record.get("provider")
                and item.get("external_user_id") == record.get("external_user_id")
            )
        ]
        identities.append(dict(record))
        state["external_identities"] = identities
        self.save(state)

    def external_identity(self, provider: str, external_user_id: str) -> dict[str, Any] | None:
        return next(
            (
                dict(item)
                for item in self.load().get("external_identities", [])
                if item.get("provider") == provider
                and item.get("external_user_id") == external_user_id
            ),
            None,
        )

    def external_identities(
        self,
        provider: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        records = list(self.load().get("external_identities", []))
        if provider is not None:
            records = [record for record in records if record.get("provider") == provider]
        if status is not None:
            records = [record for record in records if record.get("status") == status]
        return records


RUNNABLE_ASSIGNMENT_STATUSES = {
    AssignmentStatus.ASSIGNED,
    AssignmentStatus.WORKING,
}


def _active_assignment_for_agent(assignments: list[Assignment], agent_id: str) -> Assignment | None:
    runnable = [
        assignment
        for assignment in assignments
        if assignment.assigned_to == agent_id and assignment.status in RUNNABLE_ASSIGNMENT_STATUSES
    ]
    if not runnable:
        return None
    return sorted(runnable, key=lambda item: (item.updated_at, item.created_at), reverse=True)[0]


def _assignment_by_idempotency_key(
    state: dict[str, Any],
    idempotency_key: str | None,
) -> Assignment | None:
    if not idempotency_key:
        return None
    for item in state.get("assignments", []):
        if item.get("idempotency_key") == idempotency_key:
            return assignment_from_dict(item)
    for item in reversed(state.get("assignment_history", [])):
        record = item.get("record") or {}
        if record.get("idempotency_key") == idempotency_key:
            return assignment_from_dict(record)
    return None


def _goal_identity(goal: dict[str, Any]) -> tuple[object, ...]:
    return (
        goal.get("statement"),
        tuple(goal.get("success_criteria", [])),
        tuple(goal.get("explicitly_not", [])),
        goal.get("set_by"),
        bool(goal.get("human_confirmed")),
    )
