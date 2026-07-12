from __future__ import annotations

import json
import time

import pytest

from brigade.knowledge import ingest_local_document
from brigade.memory import (
    append_daily_memory,
    archive_stale_daily_memories,
    curate_workspace_memory,
)
from brigade.providers import ModelResponse, ModelUnavailableError
from brigade.runner import (
    _acquire_local_inference_lock,
    run_agent_once,
    run_managed_agents,
)
from brigade.schemas import Agent, Assignment, AssignmentStatus, Goal, Mission
from brigade.state import JsonStateStore
from brigade.time import add_seconds_iso, utc_now_iso
from brigade.tools import ToolContext, default_tool_registry
from brigade.workspace import (
    ASSIGNMENT_MARKER,
    parse_heartbeat_assignment_block,
    read_heartbeat_assignment,
    write_heartbeat_assignment,
)


class WorkingProvider:
    route_type = "test"
    model = "test-working"

    def complete(self, prompt: str) -> ModelResponse:
        del prompt
        return ModelResponse(
            text=json.dumps({"status": "working", "summary": "continue: Draft a longer plan"}),
            provider="test",
            model=self.model,
            route_type=self.route_type,
        )


class CompleteProvider:
    route_type = "test"

    def __init__(self, model: str = "test-complete") -> None:
        self.model = model

    def complete(self, prompt: str) -> ModelResponse:
        del prompt
        return ModelResponse(
            text=json.dumps({"status": "complete", "summary": f"done with {self.model}"}),
            provider="test",
            model=self.model,
            route_type=self.route_type,
        )


class FailingProvider:
    route_type = "cloud"
    model = "test-failing"

    def complete(self, prompt: str) -> ModelResponse:
        del prompt
        raise RuntimeError("provider unavailable")


class WaitingProvider:
    route_type = "test"
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
            provider="test",
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


class CompleteLocalProvider(CompleteProvider):
    route_type = "local"


class MissingLocalModelProvider:
    route_type = "local"
    model = "missing-local"

    def complete(self, prompt: str) -> ModelResponse:
        del prompt
        raise ModelUnavailableError(
            "ollama model 'missing-local' is not available at http://ollama; "
            "choose an installed model or pull it with Ollama"
        )


class EmptyThenCompleteProvider:
    route_type = "test"
    model = "test-empty-then-complete"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str) -> ModelResponse:
        del prompt
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text="",
                provider="test",
                model=self.model,
                route_type=self.route_type,
            )
        return ModelResponse(
            text=json.dumps({"status": "complete", "summary": "recovered after empty"}),
            provider="test",
            model=self.model,
            route_type=self.route_type,
        )


class ProseProvider:
    route_type = "test"
    model = "test-prose"

    def complete(self, prompt: str) -> ModelResponse:
        return ModelResponse(
            text="I made progress, but I am not done yet.",
            provider="test",
            model=self.model,
            route_type=self.route_type,
        )


class MalformedJsonProvider:
    route_type = "test"
    model = "test-malformed"

    def complete(self, prompt: str) -> ModelResponse:
        del prompt
        return ModelResponse(
            text='{"status": "complete", ',
            provider="test",
            model=self.model,
            route_type=self.route_type,
        )


class CapturingProvider:
    route_type = "test"
    model = "test-capture"

    def __init__(self) -> None:
        self.prompt = ""

    def complete(self, prompt: str) -> ModelResponse:
        self.prompt = prompt
        return ModelResponse(
            text=json.dumps({"status": "complete", "summary": "done"}),
            provider="test",
            model=self.model,
            route_type=self.route_type,
        )


class ToolUsingProvider:
    route_type = "test"
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
            provider="test",
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


def test_run_agent_once_retries_empty_provider_response(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agent = Agent(agent_id="sage", display_name="SAGE", workspace_path="workspace-sage")
    assignment = Assignment(
        assignment="Recover from empty output",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    store.add_agent(agent)
    store.add_assignment(assignment)
    write_heartbeat_assignment(agent, assignment, tmp_path)
    provider = EmptyThenCompleteProvider()

    result = run_agent_once("sage", store, provider)

    assert result.status == "complete"
    assert result.summary == "recovered after empty"
    assert provider.calls == 2


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


def test_run_agent_once_respects_local_inference_lock(tmp_path, monkeypatch):
    monkeypatch.setattr("brigade.runner.LOCAL_INFERENCE_ACQUIRE_WAIT_SECONDS", 0)
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


def test_run_managed_agents_defers_local_cooldown_without_crashing(tmp_path, monkeypatch):
    monkeypatch.setattr("brigade.runner.LOCAL_INFERENCE_ACQUIRE_WAIT_SECONDS", 0)
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
    assert store.alerts() == []


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


def test_run_managed_agents_uses_per_agent_provider_factory(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    sage = Agent(agent_id="sage", display_name="SAGE", workspace_path="workspace-sage")
    garde = Agent(agent_id="garde", display_name="GARDE", workspace_path="workspace-garde")
    providers = {
        "sage": CompleteProvider("sage-model"),
        "garde": CompleteProvider("garde-model"),
    }
    for agent in (sage, garde):
        assignment = Assignment(
            assignment=f"Complete work for {agent.agent_id}",
            assigned_to=agent.agent_id,
            created_by="human",
            source="direct_command",
        )
        assignment.transition_to(AssignmentStatus.ASSIGNED)
        store.add_agent(agent)
        store.add_assignment(assignment)
        write_heartbeat_assignment(agent, assignment, tmp_path)

    results = run_managed_agents(
        store,
        CompleteProvider("default-model"),
        provider_factory=lambda agent_id: providers[agent_id],
    )

    assert {item.assignment_id for item in results} == {
        item["assignment_id"] for item in store.assignment_history()
    }
    assert {record["model"] for record in store.usage_records()} == {
        "sage-model",
        "garde-model",
    }


def test_run_managed_agents_runs_next_local_worker_without_cooldown(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    for agent_id in ("sage", "garde"):
        agent = Agent(
            agent_id=agent_id,
            display_name=agent_id.upper(),
            workspace_path=f"workspace-{agent_id}",
        )
        assignment = Assignment(
            assignment=f"Complete local work for {agent_id}",
            assigned_to=agent_id,
            created_by="human",
            source="direct_command",
        )
        assignment.transition_to(AssignmentStatus.ASSIGNED)
        store.add_agent(agent)
        store.add_assignment(assignment)
        write_heartbeat_assignment(agent, assignment, tmp_path)

    results = run_managed_agents(store, CompleteLocalProvider("installed-local"))

    assert [item.status for item in results] == [
        AssignmentStatus.COMPLETE.value,
        AssignmentStatus.COMPLETE.value,
    ]
    assert all("local inference unavailable" not in item.summary for item in results)
    state = store.local_inference()
    assert state["status"] == "idle"
    assert state["next_available"] == state["last_completed"]


def test_local_lock_held_per_model_call_not_across_tool_execution(tmp_path, monkeypatch):
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

    lock_status_during_completion: list[str] = []

    class LocalToolProvider(ToolUsingProvider):
        route_type = "local"

        def complete(self, prompt: str) -> ModelResponse:
            lock_status_during_completion.append(store.local_inference().get("status"))
            return super().complete(prompt)

    registry = default_tool_registry()
    lock_status_during_tools: list[str] = []
    original_execute = registry.execute

    def probing_execute(name, context, arguments):
        lock_status_during_tools.append(store.local_inference().get("status"))
        return original_execute(name, context, arguments)

    monkeypatch.setattr(registry, "execute", probing_execute)

    result = run_agent_once("sage", store, LocalToolProvider(), tool_registry=registry)

    assert result.status == "complete"
    assert lock_status_during_completion == ["busy", "busy"]
    assert lock_status_during_tools == ["idle"]
    assert store.local_inference()["status"] == "idle"


def test_acquire_local_inference_lock_waits_out_short_cooldown(tmp_path, monkeypatch):
    monkeypatch.setattr("brigade.runner.LOCAL_INFERENCE_ACQUIRE_POLL_SECONDS", 0.05)
    store = JsonStateStore(tmp_path / "state.json")
    store.set_local_inference(
        {
            "status": "idle",
            "holder": None,
            "last_completed": utc_now_iso(),
            "next_available": add_seconds_iso(utc_now_iso(), 1),
        }
    )

    _acquire_local_inference_lock(store, "sage", wait_seconds=10)

    state = store.local_inference()
    assert state["status"] == "busy"
    assert state["holder"] == "sage"


def test_acquire_local_inference_lock_fails_fast_when_cooldown_exceeds_budget(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.set_local_inference(
        {
            "status": "idle",
            "holder": None,
            "last_completed": utc_now_iso(),
            "next_available": add_seconds_iso(utc_now_iso(), 900),
        }
    )

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="local inference unavailable"):
        _acquire_local_inference_lock(store, "sage", wait_seconds=30)
    assert time.monotonic() - started < 5


def test_run_managed_agents_serves_least_recently_served_first(tmp_path, monkeypatch):
    store = JsonStateStore(tmp_path / "state.json")
    assignments: dict[str, str] = {}
    for agent_id in ("sage", "garde"):
        agent = Agent(
            agent_id=agent_id,
            display_name=agent_id.upper(),
            workspace_path=f"workspace-{agent_id}",
        )
        assignment = Assignment(
            assignment=f"Complete work for {agent_id}",
            assigned_to=agent_id,
            created_by="human",
            source="direct_command",
        )
        assignment.transition_to(AssignmentStatus.ASSIGNED)
        store.add_agent(agent)
        store.add_assignment(assignment)
        write_heartbeat_assignment(agent, assignment, tmp_path)
        assignments[agent_id] = assignment.assignment_id

    # sage was served recently; garde never — garde must go first this cycle.
    monkeypatch.setattr("brigade.runner._LAST_SERVED_AT", {"sage": time.monotonic()})

    results = run_managed_agents(store, CompleteProvider())

    assert [item.assignment_id for item in results] == [
        assignments["garde"],
        assignments["sage"],
    ]


def test_run_managed_agents_falls_back_to_default_provider(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agent = Agent(agent_id="sage", display_name="SAGE", workspace_path="workspace-sage")
    assignment = Assignment(
        assignment="Retry with default model",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    store.add_agent(agent)
    store.add_assignment(assignment)
    write_heartbeat_assignment(agent, assignment, tmp_path)

    results = run_managed_agents(
        store,
        CompleteProvider("default-model"),
        provider_factory=lambda agent_id: FailingProvider(),
        fallback_provider=CompleteProvider("default-model"),
    )

    assert len(results) == 1
    assert results[0].status == AssignmentStatus.COMPLETE.value
    assert store.usage_records()[0]["model"] == "default-model"
    assert any("retrying with default provider" in alert for alert in store.alerts())


def test_missing_local_model_does_not_cooldown_before_fallback(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agent = Agent(agent_id="sage", display_name="SAGE", workspace_path="workspace-sage")
    assignment = Assignment(
        assignment="Retry with installed local model",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    store.add_agent(agent)
    store.add_assignment(assignment)
    write_heartbeat_assignment(agent, assignment, tmp_path)

    results = run_managed_agents(
        store,
        CompleteLocalProvider("installed-local"),
        provider_factory=lambda agent_id: MissingLocalModelProvider(),
        fallback_provider=CompleteLocalProvider("installed-local"),
    )

    assert len(results) == 1
    assert results[0].status == AssignmentStatus.COMPLETE.value
    assert store.usage_records()[0]["model"] == "installed-local"
    assert all("local inference unavailable" not in item.summary for item in results)


def test_create_subtasks_tool_creates_dependency_linked_children(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    chief = Agent(agent_id="chief", display_name="CHIEF", workspace_path="workspace-chief")
    worker = Agent(agent_id="worker", display_name="WORKER", workspace_path="workspace-worker")
    parent = Assignment(
        assignment="Break down mission work",
        assigned_to="chief",
        created_by="human",
        source="direct_command",
    )
    parent.transition_to(AssignmentStatus.ASSIGNED)
    store.add_agent(chief)
    store.add_agent(worker)
    store.add_assignment(parent)

    result = default_tool_registry().execute(
        "create_subtasks",
        ToolContext(agent=chief, assignment=parent, store=store),
        {
            "subtasks": [
                {"agent_id": "worker", "assignment": "First step"},
                {
                    "agent_id": "worker",
                    "assignment": "Second step",
                    "depends_on_previous": True,
                },
            ]
        },
    )

    children = [
        item for item in store.assignments() if item.parent_assignment_id == parent.assignment_id
    ]
    assert result.ok is True
    assert len(children) == 2
    assert children[1].dependency_ids == [children[0].assignment_id]


def test_create_subtasks_tool_rejects_invalid_batch_without_partial_writes(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    chief = Agent(agent_id="chief", display_name="CHIEF", workspace_path="workspace-chief")
    worker = Agent(agent_id="worker", display_name="WORKER", workspace_path="workspace-worker")
    parent = Assignment(
        assignment="Break down mission work",
        assigned_to="chief",
        created_by="human",
        source="direct_command",
    )
    parent.transition_to(AssignmentStatus.ASSIGNED)
    store.add_agent(chief)
    store.add_agent(worker)
    store.add_assignment(parent)

    result = default_tool_registry().execute(
        "create_subtasks",
        ToolContext(agent=chief, assignment=parent, store=store),
        {
            "subtasks": [
                {"agent_id": "worker", "assignment": "Valid first step"},
                {"agent_id": "missing", "assignment": "Invalid second step"},
            ]
        },
    )

    assert result.ok is False
    assert [item.assignment_id for item in store.assignments()] == [parent.assignment_id]


def test_delegate_tool_rejects_depth_and_fan_out_overflow(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agent = Agent(agent_id="sage", display_name="SAGE", workspace_path="workspace-sage")
    store.add_agent(agent)
    root = Assignment(
        assignment="Root work",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    child = Assignment(
        assignment="Child work",
        assigned_to="sage",
        created_by="sage",
        source="agent_delegate",
        parent_assignment_id=root.assignment_id,
    )
    grandchild = Assignment(
        assignment="Grandchild work",
        assigned_to="sage",
        created_by="sage",
        source="agent_delegate",
        parent_assignment_id=child.assignment_id,
    )
    for assignment in (root, child, grandchild):
        store.add_assignment(assignment)

    registry = default_tool_registry()
    depth_result = registry.execute(
        "delegate",
        ToolContext(agent=agent, assignment=grandchild, store=store),
        {"agent_id": "sage", "assignment": "Too deep"},
    )
    assert depth_result.ok is False
    assert "depth limit" in depth_result.output

    for index in range(5):
        store.add_assignment(
            Assignment(
                assignment=f"Existing child {index}",
                assigned_to="sage",
                created_by="sage",
                source="agent_delegate",
                parent_assignment_id=root.assignment_id,
            )
        )
    fanout_result = registry.execute(
        "delegate",
        ToolContext(agent=agent, assignment=root, store=store),
        {"agent_id": "sage", "assignment": "Too many children"},
    )
    assert fanout_result.ok is False
    assert "fan-out limit" in fanout_result.output


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

    result = run_agent_once("sage", store, CompleteProvider())

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

    result = run_agent_once("sage", store, CompleteProvider())

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

    result = run_agent_once("sage", store, CompleteProvider())

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

    result = run_agent_once("sage", store, CompleteProvider())

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


def test_write_heartbeat_assignment_scrubs_malformed_blocks(tmp_path):
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
    # The malformed block must be scrubbed, not left stranded alongside the new one.
    assert text.count(ASSIGNMENT_MARKER) == 1
    assert "broken" not in text
    assert read_heartbeat_assignment(heartbeat).assignment == "Second"


def test_write_heartbeat_assignment_scrubs_truncated_block(tmp_path):
    agent = Agent(agent_id="sage", display_name="SAGE", workspace_path="workspace-sage")
    assignment = Assignment(
        assignment="Recovered",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    heartbeat = write_heartbeat_assignment(agent, assignment, tmp_path)
    # A truncated block (marker with no closing fence) has no parseable content
    # and would survive a naive parse-and-replace repair.
    heartbeat.write_text(
        heartbeat.read_text(encoding="utf-8")
        + f'\nTrailing operator note.\n\n{ASSIGNMENT_MARKER}\n{{"assignment": "truncated"\n',
        encoding="utf-8",
    )

    write_heartbeat_assignment(agent, assignment, tmp_path)

    text = heartbeat.read_text(encoding="utf-8")
    assert "Trailing operator note." in text
    assert text.count(ASSIGNMENT_MARKER) == 1
    assert "truncated" not in text
    # Exactly one well-formed block remains, so strict parsing succeeds.
    assert parse_heartbeat_assignment_block(text).assignment.assignment == "Recovered"


def test_stale_heartbeat_block_recovers_to_single_clean_block(tmp_path):
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
    heartbeat = write_heartbeat_assignment(agent, stale, tmp_path)

    # The stale block points at an assignment_id the store does not consider
    # active, so the run blocks without touching the heartbeat.
    result = run_agent_once("sage", store, CompleteProvider())
    assert result.status == AssignmentStatus.BLOCKED.value
    assert "does not match the active stored assignment" in result.summary

    # Recovery: write the genuine active assignment back. The stale block must be
    # replaced, not appended around, leaving one clean parseable block.
    write_heartbeat_assignment(agent, assignment, tmp_path)
    text = heartbeat.read_text(encoding="utf-8")
    assert text.count(ASSIGNMENT_MARKER) == 1
    recovered = parse_heartbeat_assignment_block(text, expected_agent_id="sage").assignment
    assert recovered.assignment_id == assignment.assignment_id
    assert recovered.assignment == "Stored assignment"
