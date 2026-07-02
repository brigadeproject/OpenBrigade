"""Phase 6: tool requests, workspace tools, efficiency detection, recurrences."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone

from brigade.efficiency import (
    EVENT_RECURRENCE_MATERIALIZED,
    detect_recurring_work,
    materialize_due_recurrences,
    normalize_pattern_text,
)
from brigade.orchestrator import OrchestrationConfig, run_full_cycle
from brigade.prompt_floors import build_agent_floor
from brigade.schemas import (
    Agent,
    Assignment,
    AssignmentKind,
    AssignmentStatus,
    Mission,
    Team,
    build_recurrence,
)
from brigade.services import decide_proposal
from brigade.state import JsonStateStore
from brigade.tools import (
    ToolContext,
    default_tool_registry,
    workspace_tool_manifest,
)
from brigade.workspace import ensure_agent_workspace

NOW = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)


def _store(tmp_path) -> JsonStateStore:
    store = JsonStateStore(tmp_path / "state.json")
    store.set_mission(Mission("Run the prototype", [], []))
    store.add_agent(Agent("sage", "SAGE", "workspace-sage", role="crew_chief"))
    store.add_agent(Agent("ada", "ADA", "workspace-ada", team_id="alpha"))
    store.upsert_team(
        Team(team_id="alpha", display_name="Alpha", crew_chief_id="sage", members=["ada"])
    )
    return store


def _context(store: JsonStateStore, agent_id: str) -> ToolContext:
    agent = next(item for item in store.agents() if item.agent_id == agent_id)
    assignment = Assignment(
        assignment="Active work",
        assigned_to=agent_id,
        created_by="human",
        source="direct_command",
    )
    store.add_assignment(assignment)
    return ToolContext(agent=agent, assignment=assignment, store=store)


def _archive_completed(store, *, agent_id: str, text: str, when: str) -> Assignment:
    assignment = Assignment(
        assignment=text,
        assigned_to=agent_id,
        created_by="human",
        source="direct_command",
    )
    store.add_assignment(assignment)
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    assignment.mark_complete("done")
    assignment.updated_at = when
    store.archive_assignment(assignment, executive_summary="done")
    return assignment


# --- request_tool -------------------------------------------------------------------


def test_request_tool_records_proposal_alert_and_event(tmp_path):
    store = _store(tmp_path)
    registry = default_tool_registry()
    context = _context(store, "ada")

    result = registry.execute(
        "request_tool",
        context,
        {"name": "csv-diff", "purpose": "compare csv files", "spec": "csv-diff A B"},
    )

    assert result.ok
    proposals = store.proposals(kind="tool_request")
    assert len(proposals) == 1
    assert proposals[0]["agent_id"] == "ada"
    assert proposals[0]["team_id"] == "alpha"
    assert proposals[0]["details"]["name"] == "csv-diff"
    assert any("csv-diff" in alert for alert in store.alerts())
    events = [
        event
        for record in store.orchestrator_reasoning()
        for event in record.get("events", [])
    ]
    assert "proposal_created" in {event["type"] for event in events}
    # Never builds directly: no tool_build assignment yet.
    assert not [
        item for item in store.assignments() if item.kind == AssignmentKind.TOOL_BUILD
    ]


def test_request_tool_is_idempotent_per_agent_and_name(tmp_path):
    store = _store(tmp_path)
    registry = default_tool_registry()
    context = _context(store, "ada")
    args = {"name": "csv-diff", "purpose": "p", "spec": "s"}

    registry.execute("request_tool", context, args)
    second = registry.execute("request_tool", context, args)

    assert second.ok
    assert second.metadata["status"] == "existing"
    assert len(store.proposals(kind="tool_request")) == 1


# --- Approval paths -----------------------------------------------------------------


def test_approving_tool_request_creates_tool_build_for_chief(tmp_path):
    store = _store(tmp_path)
    registry = default_tool_registry()
    registry.execute(
        "request_tool",
        _context(store, "ada"),
        {"name": "csv-diff", "purpose": "compare csv files", "spec": "csv-diff A B"},
    )
    proposal = store.proposals(kind="tool_request")[0]

    decided = decide_proposal(
        store,
        proposal_id=proposal["proposal_id"],
        decision="approved",
        decided_by="tm",
    )

    assignment_id = decided["details"]["approval_effects"]["assignment_id"]
    assignment = store.find_assignment(assignment_id)
    assert assignment.kind == AssignmentKind.TOOL_BUILD
    assert assignment.assigned_to == "sage"  # the requesting team's chief
    assert "tools/csv-diff.json" in assignment.assignment
    assert "TOOLS.md" in assignment.assignment
    assert assignment.idempotency_key == f"tool-build:v1:{proposal['proposal_id']}"


def test_chief_can_approve_only_own_team_proposals(tmp_path):
    store = _store(tmp_path)
    store.add_agent(Agent("ops", "OPS", "workspace-ops", role="crew_chief"))
    store.upsert_team(
        Team(team_id="infra", display_name="Infra", crew_chief_id="ops", members=[])
    )
    registry = default_tool_registry()
    registry.execute(
        "request_tool",
        _context(store, "ada"),
        {"name": "csv-diff", "purpose": "p", "spec": "s"},
    )
    proposal = store.proposals(kind="tool_request")[0]

    foreign = registry.execute(
        "approve_proposal",
        _context(store, "ops"),
        {"proposal_id": proposal["proposal_id"]},
    )
    own = registry.execute(
        "approve_proposal",
        _context(store, "sage"),
        {"proposal_id": proposal["proposal_id"]},
    )

    assert not foreign.ok
    assert "own team" in foreign.output
    assert own.ok
    assert own.metadata["approval_effects"]["assignment_id"]


def test_non_chief_cannot_approve_proposals(tmp_path):
    store = _store(tmp_path)
    registry = default_tool_registry()
    registry.execute(
        "request_tool",
        _context(store, "ada"),
        {"name": "csv-diff", "purpose": "p", "spec": "s"},
    )
    proposal = store.proposals(kind="tool_request")[0]

    result = registry.execute(
        "approve_proposal",
        _context(store, "ada"),
        {"proposal_id": proposal["proposal_id"]},
    )

    assert not result.ok
    assert "crew chiefs" in result.output


# --- run_workspace_tool -------------------------------------------------------------


def test_run_workspace_tool_executes_script(tmp_path):
    store = _store(tmp_path)
    context = _context(store, "ada")
    workspace = ensure_agent_workspace(context.agent, store.data_dir)
    tools_dir = workspace / "tools"
    tools_dir.mkdir(exist_ok=True)
    script = tools_dir / "echoer"
    script.write_text('#!/bin/sh\necho "tool says: $1"\n', encoding="utf-8")
    os.chmod(script, 0o755)

    result = default_tool_registry().execute(
        "run_workspace_tool", context, {"name": "echoer", "args": ["hi"]}
    )

    assert result.ok
    assert result.output == "tool says: hi"


def test_run_workspace_tool_rejects_path_escape(tmp_path):
    store = _store(tmp_path)
    context = _context(store, "ada")
    ensure_agent_workspace(context.agent, store.data_dir)
    registry = default_tool_registry()

    outside = registry.execute(
        "run_workspace_tool", context, {"name": "../HEARTBEAT.md"}
    )
    absolute = registry.execute(
        "run_workspace_tool", context, {"name": "/bin/sh"}
    )

    assert not outside.ok
    assert not absolute.ok


def test_run_workspace_tool_times_out(tmp_path, monkeypatch):
    store = _store(tmp_path)
    context = _context(store, "ada")
    workspace = ensure_agent_workspace(context.agent, store.data_dir)
    tools_dir = workspace / "tools"
    tools_dir.mkdir(exist_ok=True)
    script = tools_dir / "sleeper"
    script.write_text("#!/bin/sh\nsleep 60\n", encoding="utf-8")
    os.chmod(script, 0o755)

    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout", 30))

    monkeypatch.setattr("brigade.tools.subprocess.run", raise_timeout)
    result = default_tool_registry().execute(
        "run_workspace_tool", context, {"name": "sleeper"}
    )

    assert not result.ok
    assert "timed out" in result.output.lower() or "30" in result.output


# --- Floor manifest merge -----------------------------------------------------------


def test_agent_floor_merges_workspace_tool_manifest(tmp_path):
    store = _store(tmp_path)
    context = _context(store, "ada")
    workspace = ensure_agent_workspace(context.agent, store.data_dir)
    tools_dir = workspace / "tools"
    tools_dir.mkdir(exist_ok=True)
    (tools_dir / "csv-diff.json").write_text(
        '{"name": "csv-diff", "description": "compare csv files", '
        '"argument_schema": {"a": "first file", "b": "second file"}}',
        encoding="utf-8",
    )
    (tools_dir / "broken.json").write_text("{not json", encoding="utf-8")

    floor = build_agent_floor(
        context.agent, context.assignment, store, default_tool_registry()
    )

    workspace_tools = [
        tool for tool in floor["available_tools"] if tool.get("workspace_tool")
    ]
    assert [tool["name"] for tool in workspace_tools] == ["csv-diff"]
    assert workspace_tools[0]["invoke_with"] == "run_workspace_tool"
    assert workspace_tool_manifest(workspace)[0]["description"] == "compare csv files"


# --- Efficiency detection -----------------------------------------------------------


def test_normalize_pattern_strips_dates_and_ids():
    assert (
        normalize_pattern_text("Send weekly digest 2026-06-01")
        == normalize_pattern_text("send Weekly  digest 2026-06-08")
    )
    assert (
        normalize_pattern_text(
            "retry 123e4567-e89b-12d3-a456-426614174000 import"
        )
        == "retry import"
    )


def test_detect_recurring_work_at_threshold(tmp_path):
    store = _store(tmp_path)
    for day in (1, 4, 7):
        _archive_completed(
            store,
            agent_id="ada",
            text=f"Send weekly digest 2026-06-0{day}",
            when=f"2026-06-0{day}T09:00:00+00:00",
        )

    result = detect_recurring_work(store, threshold=3, lookback_days=14, now=NOW)

    assert len(result["proposals"]) == 1
    proposal = result["proposals"][0]
    assert proposal["kind"] == "efficiency"
    assert proposal["agent_id"] == "ada"
    assert proposal["details"]["count"] == 3
    # Median completion gap: 3 days.
    assert proposal["details"]["interval_seconds"] == 3 * 86_400
    assert proposal["details"]["template"]["assigned_to"] == "ada"
    assert len(proposal["details"]["sample_assignment_ids"]) == 3


def test_detect_recurring_work_below_threshold_is_silent(tmp_path):
    store = _store(tmp_path)
    for day in (1, 4):
        _archive_completed(
            store,
            agent_id="ada",
            text=f"Send weekly digest 2026-06-0{day}",
            when=f"2026-06-0{day}T09:00:00+00:00",
        )

    result = detect_recurring_work(store, threshold=3, lookback_days=14, now=NOW)

    assert result["proposals"] == []


def test_detect_recurring_work_is_idempotent_per_pattern(tmp_path):
    store = _store(tmp_path)
    for day in (1, 4, 7):
        _archive_completed(
            store,
            agent_id="ada",
            text=f"Send weekly digest 2026-06-0{day}",
            when=f"2026-06-0{day}T09:00:00+00:00",
        )

    first = detect_recurring_work(store, now=NOW)
    second = detect_recurring_work(store, now=NOW)

    assert len(first["proposals"]) == 1
    assert second["proposals"] == []
    assert len(store.proposals(kind="efficiency")) == 1


def test_detect_recurring_work_survives_episode_search_failure(tmp_path, monkeypatch):
    store = _store(tmp_path)
    for day in (1, 4, 7):
        _archive_completed(
            store,
            agent_id="ada",
            text=f"Send weekly digest 2026-06-0{day}",
            when=f"2026-06-0{day}T09:00:00+00:00",
        )

    def broken_search(*args, **kwargs):
        raise RuntimeError("qdrant offline")

    monkeypatch.setattr(store, "search_episodes", broken_search)
    result = detect_recurring_work(store, now=NOW)

    assert len(result["proposals"]) == 1
    assert result["proposals"][0]["details"]["evidence"] == []


# --- Recurrence materialization -----------------------------------------------------


def _add_due_recurrence(store) -> dict:
    recurrence = build_recurrence(
        template={"assignment": "Send weekly digest", "assigned_to": "ada"},
        interval_seconds=7 * 86_400,
        next_due_at="2026-06-10T00:00:00+00:00",
    )
    return store.add_recurrence(recurrence)


def test_due_recurrence_materializes_exactly_once(tmp_path):
    store = _store(tmp_path)
    recurrence = _add_due_recurrence(store)

    first = materialize_due_recurrences(store, now=NOW)
    second = materialize_due_recurrences(store, now=NOW)

    assert len(first["materialized"]) == 1
    assert second["materialized"] == []
    created = store.find_assignment(first["materialized"][0]["assignment_id"])
    # Chief-first: ada's chief receives the materialized work.
    assert created.assigned_to == "sage"
    assert "suggested agent was ada" in created.assignment_rationale
    assert created.idempotency_key == (
        f"recurrence:v1:{recurrence['recurrence_id']}:2026-06-10T00:00:00+00:00"
    )
    refreshed = store.recurrences()[0]
    assert refreshed["next_due_at"] > NOW.isoformat()
    assert refreshed["last_materialized_at"] == "2026-06-10T00:00:00+00:00"
    assert [event["type"] for event in first["events"]] == [
        EVENT_RECURRENCE_MATERIALIZED
    ]


def test_disabled_recurrence_never_materializes(tmp_path):
    store = _store(tmp_path)
    recurrence = _add_due_recurrence(store)
    recurrence["enabled"] = False
    store.update_recurrence(recurrence)

    result = materialize_due_recurrences(store, now=NOW)

    assert result["materialized"] == []


def test_full_cycle_materializes_approved_efficiency_proposal(tmp_path):
    store = _store(tmp_path)
    for day in (1, 4, 7):
        _archive_completed(
            store,
            agent_id="ada",
            text=f"Send weekly digest 2026-06-0{day}",
            when=f"2026-06-0{day}T09:00:00+00:00",
        )
    detection = detect_recurring_work(store, now=NOW)
    proposal = detection["proposals"][0]
    decided = decide_proposal(
        store,
        proposal_id=proposal["proposal_id"],
        decision="approved",
        decided_by="tm",
    )
    recurrence_id = decided["details"]["approval_effects"]["recurrence_id"]
    # Make the recurrence due now.
    recurrence = next(
        item
        for item in store.recurrences()
        if item["recurrence_id"] == recurrence_id
    )
    recurrence["next_due_at"] = "2026-06-10T00:00:00+00:00"
    store.update_recurrence(recurrence)

    first = run_full_cycle(store, None, OrchestrationConfig(proactive_mode="off"))
    second = run_full_cycle(store, None, OrchestrationConfig(proactive_mode="off"))

    assert len(first.sub_results["recurrence"]["materialized"]) == 1
    assert second.sub_results["recurrence"]["materialized"] == []
    event_types = [event["type"] for event in first.reasoning_record["events"]]
    assert EVENT_RECURRENCE_MATERIALIZED in event_types
    assert first.outcome.mode == "worked"


def test_create_subtasks_accepts_partial_batch_at_capacity(tmp_path):
    store = _store(tmp_path)
    parent_agent = Agent(agent_id="chief", display_name="Chief", workspace_path="workspace-chief")
    worker = Agent(agent_id="worker", display_name="Worker", workspace_path="workspace-worker")
    store.add_agent(parent_agent)
    store.add_agent(worker)
    parent = Assignment(
        assignment="Plan the work",
        assigned_to="chief",
        created_by="orchestrator",
        source="orchestrator_idle_task_builder",
    )
    store.add_assignment(parent)
    for index in range(4):
        store.add_assignment(
            Assignment(
                assignment=f"existing child {index}",
                assigned_to="worker",
                created_by="chief",
                source="agent_delegate",
                parent_assignment_id=parent.assignment_id,
            )
        )
    registry = default_tool_registry()
    context = ToolContext(agent=parent_agent, assignment=parent, store=store)

    result = registry.execute(
        "create_subtasks",
        context,
        {
            "subtasks": [
                {"agent_id": "worker", "assignment": "new child A"},
                {"agent_id": "worker", "assignment": "new child B"},
            ]
        },
    )

    assert result.ok
    assert "created 1 queued subtasks" in result.output
    assert "1 trimmed" in result.output


def test_create_subtasks_at_full_capacity_reports_children_instead_of_blocking(tmp_path):
    store = _store(tmp_path)
    parent_agent = Agent(agent_id="chief", display_name="Chief", workspace_path="workspace-chief")
    worker = Agent(agent_id="worker", display_name="Worker", workspace_path="workspace-worker")
    store.add_agent(parent_agent)
    store.add_agent(worker)
    parent = Assignment(
        assignment="Plan the work",
        assigned_to="chief",
        created_by="orchestrator",
        source="orchestrator_idle_task_builder",
    )
    store.add_assignment(parent)
    for index in range(5):
        store.add_assignment(
            Assignment(
                assignment=f"existing child {index}",
                assigned_to="worker",
                created_by="chief",
                source="agent_delegate",
                parent_assignment_id=parent.assignment_id,
            )
        )
    registry = default_tool_registry()
    context = ToolContext(agent=parent_agent, assignment=parent, store=store)

    result = registry.execute(
        "create_subtasks",
        context,
        {"subtasks": [{"agent_id": "worker", "assignment": "one more"}]},
    )

    assert result.ok
    assert "no capacity" in result.output
    assert "plan in motion" in result.output
