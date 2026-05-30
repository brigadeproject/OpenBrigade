from __future__ import annotations

import json

import pytest

from brigade.knowledge import ingest_local_document
from brigade.memory import (
    append_daily_memory,
    archive_stale_daily_memories,
    curate_workspace_memory,
)
from brigade.providers import FakeProvider, ModelResponse
from brigade.runner import run_agent_once, run_managed_agents
from brigade.schemas import Agent, Assignment, AssignmentStatus, Goal, Mission
from brigade.state import JsonStateStore
from brigade.time import add_seconds_iso, utc_now_iso
from brigade.workspace import (
    ASSIGNMENT_MARKER,
    read_heartbeat_assignment,
    write_heartbeat_assignment,
)


class WorkingProvider:
    route_type = "simulated"
    model = "test-working"

    def complete(self, prompt: str) -> ModelResponse:
        del prompt
        return ModelResponse(
            text=json.dumps({"status": "working", "summary": "continue: Draft a longer plan"}),
            provider="fake",
            model=self.model,
            route_type=self.route_type,
        )


class WaitingProvider:
    route_type = "simulated"
    model = "test-waiting"

    def __init__(self, expected_next_activity_at: str) -> None:
        self.expected_next_activity_at = expected_next_activity_at

    def complete(self, prompt: str) -> ModelResponse:
        del prompt
        return ModelResponse(
            text=json.dumps(
                {
                    "status": "working",
                    "summary": "waiting for a scheduled input",
                    "expected_next_activity_at": self.expected_next_activity_at,
                }
            ),
            provider="fake",
            model=self.model,
            route_type=self.route_type,
        )


class LocalProvider:
    route_type = "local"
    model = "local-test"

    def complete(self, prompt: str) -> ModelResponse:
        return ModelResponse(
            text=f"LOCAL: {prompt}",
            provider="ollama",
            model=self.model,
            route_type=self.route_type,
        )


class ProseProvider:
    route_type = "simulated"
    model = "test-prose"

    def complete(self, prompt: str) -> ModelResponse:
        return ModelResponse(
            text="I made progress, but I am not done yet.",
            provider="fake",
            model=self.model,
            route_type=self.route_type,
        )


class MalformedJsonProvider:
    route_type = "simulated"
    model = "test-malformed"

    def complete(self, prompt: str) -> ModelResponse:
        del prompt
        return ModelResponse(
            text='{"status": "complete", ',
            provider="fake",
            model=self.model,
            route_type=self.route_type,
        )


class CapturingProvider:
    route_type = "simulated"
    model = "test-capture"

    def __init__(self) -> None:
        self.prompt = ""

    def complete(self, prompt: str) -> ModelResponse:
        self.prompt = prompt
        return ModelResponse(
            text=json.dumps({"status": "complete", "summary": "done"}),
            provider="fake",
            model=self.model,
            route_type=self.route_type,
        )


class ToolUsingProvider:
    route_type = "simulated"
    model = "test-tool"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> ModelResponse:
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            text = json.dumps(
                {
                    "status": "tool_call",
                    "tool": "read_file",
                    "arguments": {"path": "MEMORY.md"},
                    "summary": "read memory",
                }
            )
        else:
            text = json.dumps({"status": "complete", "summary": "used memory"})
        return ModelResponse(
            text=text,
            provider="fake",
            model=self.model,
            route_type=self.route_type,
        )


def test_run_agent_once_marks_assignment_working_until_complete(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agent = Agent(agent_id="sage", display_name="SAGE", workspace_path="workspace-sage")
    assignment = Assignment(
        assignment="Draft a longer plan",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    store.add_agent(agent)
    store.add_assignment(assignment)
    write_heartbeat_assignment(agent, assignment, tmp_path)

    result = run_agent_once("sage", store, WorkingProvider())

    assert result.status == "working"
    active = store.assignments()[0]
    assert active.cycle_count == 1
    assert active.progress_summary == "continue: Draft a longer plan"
    assert (
        read_heartbeat_assignment(tmp_path / "workspace-sage" / "HEARTBEAT.md").status
        == AssignmentStatus.WORKING
    )


def test_run_agent_once_records_expected_next_activity_checkpoint(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agent = Agent(agent_id="sage", display_name="SAGE", workspace_path="workspace-sage")
    assignment = Assignment(
        assignment="Wait for scheduled data",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    store.add_agent(agent)
    store.add_assignment(assignment)
    write_heartbeat_assignment(agent, assignment, tmp_path)
    expected = add_seconds_iso(utc_now_iso(), 7200)

    result = run_agent_once("sage", store, WaitingProvider(expected))

    assert result.status == "working"
    active = store.find_assignment(assignment.assignment_id)
    assert active is not None
    assert active.checkpoint_at == expected


def test_run_agent_once_treats_non_json_prose_as_working_not_complete(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agent = Agent(agent_id="sage", display_name="SAGE", workspace_path="workspace-sage")
    assignment = Assignment(
        assignment="Draft a careful plan",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    store.add_agent(agent)
    store.add_assignment(assignment)
    write_heartbeat_assignment(agent, assignment, tmp_path)

    result = run_agent_once("sage", store, ProseProvider())

    assert result.status == "working"
    assert store.assignment_history() == []
    assert store.find_assignment(assignment.assignment_id).status == AssignmentStatus.WORKING


def test_run_agent_once_blocks_after_repeated_malformed_json(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agent = Agent(agent_id="sage", display_name="SAGE", workspace_path="workspace-sage")
    assignment = Assignment(
        assignment="Return valid status",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    store.add_agent(agent)
    store.add_assignment(assignment)
    write_heartbeat_assignment(agent, assignment, tmp_path)

    result = run_agent_once("sage", store, MalformedJsonProvider())

    assert result.status == "blocked"
    assert "malformed provider output" in result.summary
    assert store.assignment_history() == []
    assert store.find_assignment(assignment.assignment_id).status == AssignmentStatus.BLOCKED


def test_run_agent_once_prompt_injects_agent_floor_without_memory_dump(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agent = Agent(
        agent_id="sage",
        display_name="SAGE",
        workspace_path="workspace-sage",
        role="planner",
    )
    assignment = Assignment(
        assignment="Use the mission context",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    store.set_mission(
        Mission(
            statement="Build an orchestrated agent harness",
            success_criteria=["agents execute assigned work"],
            explicitly_not=["pretend work is complete"],
        )
    )
    store.add_goal(
        "sage",
        Goal(
            statement="Keep harness work grounded",
            success_criteria=["context reaches model"],
            explicitly_not=["ignore memory"],
            set_by="human",
        ),
    )
    store.add_agent(agent)
    store.add_assignment(assignment)
    heartbeat = write_heartbeat_assignment(agent, assignment, tmp_path)
    (heartbeat.parent / "MEMORY.md").write_text(
        "# Memory\n\nRemember operator constraints.\n",
        encoding="utf-8",
    )
    provider = CapturingProvider()

    result = run_agent_once("sage", store, provider)

    assert result.status == "complete"
    assert "Build an orchestrated agent harness" in provider.prompt
    assert "Keep harness work grounded" in provider.prompt
    assert "Remember operator constraints" not in provider.prompt
    assert "Floor JSON" in provider.prompt
    assert "OpenBrigade agent response protocol" in provider.prompt


def test_run_agent_once_executes_tool_call_then_applies_final_response(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agent = Agent(agent_id="sage", display_name="SAGE", workspace_path="workspace-sage")
    assignment = Assignment(
        assignment="Read memory before answering",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    store.add_agent(agent)
    store.add_assignment(assignment)
    heartbeat = write_heartbeat_assignment(agent, assignment, tmp_path)
    (heartbeat.parent / "MEMORY.md").write_text("Useful memory.", encoding="utf-8")
    provider = ToolUsingProvider()

    result = run_agent_once("sage", store, provider)

    assert result.status == "complete"
    assert len(provider.prompts) == 2
    assert '"tool": "read_file"' in provider.prompts[1]
    assert len(store.usage_records()) == 2


def test_run_agent_once_abandons_after_ten_cycles(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agent = Agent(agent_id="sage", display_name="SAGE", workspace_path="workspace-sage")
    assignment = Assignment(
        assignment="Still not done",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
        cycle_count=9,
        status=AssignmentStatus.WORKING,
    )
    store.add_agent(agent)
    store.add_assignment(assignment)
    write_heartbeat_assignment(agent, assignment, tmp_path)

    result = run_agent_once("sage", store, WorkingProvider())

    assert result.status == "abandoned"
    assert store.assignments() == []
    assert store.assignment_history()[0]["final_status"] == "abandoned"
    assert store.alerts()


def test_run_agent_once_respects_local_inference_lock(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agent = Agent(agent_id="sage", display_name="SAGE", workspace_path="workspace-sage")
    assignment = Assignment(
        assignment="Use the local model",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    store.add_agent(agent)
    store.add_assignment(assignment)
    write_heartbeat_assignment(agent, assignment, tmp_path)
    store.set_local_inference(
        {
            "status": "idle",
            "holder": None,
            "last_completed": utc_now_iso(),
            "next_available": add_seconds_iso(utc_now_iso(), 60),
        }
    )

    with pytest.raises(RuntimeError, match="local inference unavailable"):
        run_agent_once("sage", store, LocalProvider())

    assert store.assignment_execution_claim(assignment.assignment_id) is None


def test_run_managed_agents_defers_local_cooldown_without_crashing(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agent = Agent(agent_id="sage", display_name="SAGE", workspace_path="workspace-sage")
    assignment = Assignment(
        assignment="Use the local model later",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    store.add_agent(agent)
    store.add_assignment(assignment)
    write_heartbeat_assignment(agent, assignment, tmp_path)
    store.set_local_inference(
        {
            "status": "idle",
            "holder": None,
            "last_completed": utc_now_iso(),
            "next_available": add_seconds_iso(utc_now_iso(), 60),
        }
    )

    results = run_managed_agents(store, LocalProvider())

    assert len(results) == 1
    assert results[0].status == AssignmentStatus.ASSIGNED.value
    assert "local inference unavailable" in results[0].summary
    assert store.find_assignment(assignment.assignment_id).status == AssignmentStatus.ASSIGNED
    assert store.alerts()


def test_run_managed_agents_uses_explicit_agent_manifest(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    sage = Agent(agent_id="sage", display_name="SAGE", workspace_path="workspace-sage")
    garde = Agent(agent_id="garde", display_name="GARDE", workspace_path="workspace-garde")
    assignment = Assignment(
        assignment="One task only",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    for agent in (sage, garde):
        store.add_agent(agent)
    store.add_assignment(assignment)
    write_heartbeat_assignment(sage, assignment, tmp_path)

    results = run_managed_agents(store, WorkingProvider())

    assert len(results) == 1
    assert results[0].assignment_id == assignment.assignment_id


def test_run_agent_once_ignores_queued_or_blocked_backlog_for_agent(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agent = Agent(agent_id="sage", display_name="SAGE", workspace_path="workspace-sage")
    queued = Assignment(
        assignment="Older queued backlog",
        assigned_to="sage",
        created_by="orchestrator",
        source="goal_stall_detector",
    )
    blocked = Assignment(
        assignment="Older blocked backlog",
        assigned_to="sage",
        created_by="orchestrator",
        source="goal_stall_detector",
    )
    blocked.transition_to(AssignmentStatus.ASSIGNED)
    blocked.register_failure("needs a later retry")
    active = Assignment(
        assignment="Current assigned work",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    active.transition_to(AssignmentStatus.ASSIGNED)
    store.add_agent(agent)
    for assignment in (queued, blocked, active):
        store.add_assignment(assignment)
    write_heartbeat_assignment(agent, active, tmp_path)

    result = run_agent_once("sage", store, FakeProvider())

    assert result.assignment_id == active.assignment_id
    assert result.status == AssignmentStatus.COMPLETE.value
    assert store.find_assignment(queued.assignment_id) is not None
    assert store.find_assignment(blocked.assignment_id) is not None


def test_run_agent_once_rejects_duplicate_execution_claim_without_side_effects(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agent = Agent(agent_id="sage", display_name="SAGE", workspace_path="workspace-sage")
    assignment = Assignment(
        assignment="Only run this once",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    store.add_agent(agent)
    store.add_assignment(assignment)
    write_heartbeat_assignment(agent, assignment, tmp_path)
    assert store.try_claim_assignment_execution(
        assignment.assignment_id,
        "runner-a",
        agent_id="sage",
    )

    result = run_agent_once("sage", store, FakeProvider())

    assert result.status == AssignmentStatus.ASSIGNED.value
    assert result.summary == "assignment already being executed by runner-a"
    assert store.usage_records() == []
    assert store.transcripts() == []
    assert store.assignment_history() == []
    assert store.find_assignment(assignment.assignment_id) is not None


def test_run_agent_once_blocks_on_invalid_heartbeat_without_completion_side_effects(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agent = Agent(agent_id="sage", display_name="SAGE", workspace_path="workspace-sage")
    assignment = Assignment(
        assignment="Validate heartbeat parsing",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    store.add_agent(agent)
    store.add_assignment(assignment)
    heartbeat = write_heartbeat_assignment(agent, assignment, tmp_path)
    heartbeat.write_text(f"{ASSIGNMENT_MARKER}\n{{ invalid json\n```\n", encoding="utf-8")

    result = run_agent_once("sage", store, FakeProvider())

    assert result.status == AssignmentStatus.BLOCKED.value
    assert "invalid JSON" in result.summary
    assert store.assignment_history() == []
    assert store.usage_records() == []
    assert store.transcripts() == []
    assert store.alerts()
    active = store.find_assignment(assignment.assignment_id)
    assert active is not None
    assert active.status == AssignmentStatus.BLOCKED


def test_run_agent_once_blocks_on_stale_heartbeat_assignment_id(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agent = Agent(agent_id="sage", display_name="SAGE", workspace_path="workspace-sage")
    assignment = Assignment(
        assignment="Stored assignment",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    stale = Assignment(
        assignment="Stale heartbeat assignment",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    stale.transition_to(AssignmentStatus.ASSIGNED)
    store.add_agent(agent)
    store.add_assignment(assignment)
    write_heartbeat_assignment(agent, stale, tmp_path)

    result = run_agent_once("sage", store, FakeProvider())

    assert result.status == AssignmentStatus.BLOCKED.value
    assert "does not match the active stored assignment" in result.summary
    assert store.assignment_history() == []
    assert store.usage_records() == []
    assert store.transcripts() == []
    assert store.alerts()


def test_memory_archive_moves_old_daily_files_into_episode_records(tmp_path):
    append_daily_memory(tmp_path, "20200101", "Observed durable preference")
    append_daily_memory(tmp_path, "20990101", "Too recent to archive")
    curated = curate_workspace_memory(tmp_path)

    archived = archive_stale_daily_memories(tmp_path, agent_id="sage", retention_days=7)

    assert curated.exists()
    assert len(archived) == 1
    assert archived[0]["summary"] == "Observed durable preference"
    assert not (tmp_path / "memory" / "20200101-MEMORY.md").exists()
    assert (tmp_path / "memory" / "20990101-MEMORY.md").exists()


def test_ingest_local_document_builds_chunks_episode_and_provenance(tmp_path):
    source = tmp_path / "notes.md"
    source.write_text("# Notes\n\nalpha\n\nbeta\n", encoding="utf-8")

    document, chunks, episode, provenance = ingest_local_document(
        title="Notes",
        source="local",
        document_type="note",
        content_path=str(source),
    )

    assert document.metadata["chunk_count"] >= 1
    assert chunks[0]["document_id"] == document.document_id
    assert episode["source_id"] == document.document_id
    assert provenance[0]["node_type"] == "document"
    assert any(record["node_type"] == "chunk" for record in provenance)


def test_write_heartbeat_assignment_replaces_last_parseable_block_only(tmp_path):
    agent = Agent(agent_id="sage", display_name="SAGE", workspace_path="workspace-sage")
    first = Assignment(
        assignment="First",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    second = Assignment(
        assignment="Second",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    heartbeat = write_heartbeat_assignment(agent, first, tmp_path)
    heartbeat.write_text(
        heartbeat.read_text(encoding="utf-8")
        + "\nNotes above replacement.\n\n"
        + f'{ASSIGNMENT_MARKER}\n{{"assignment": "broken"}}\n```\n',
        encoding="utf-8",
    )

    write_heartbeat_assignment(agent, second, tmp_path)

    text = heartbeat.read_text(encoding="utf-8")
    assert "Notes above replacement." in text
    assert read_heartbeat_assignment(heartbeat).assignment == "Second"
