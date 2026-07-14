from __future__ import annotations

import json

from brigade.orchestrator import (
    CycleResult,
    ProactiveContinuationConfig,
    apply_orchestrator_actions,
    build_cycle_reasoning_record,
    build_orchestration_telemetry,
    classify_cycle_outcome,
    derive_agent_states,
    deterministic_cycle,
    evaluate_dispatch_starvation,
    evaluate_mission_continuation,
    evaluate_orchestrator_floor,
    run_orchestrator_escalation,
)
from brigade.prompt_floors import build_crew_chief_floor, build_orchestrator_floor
from brigade.providers import ModelResponse
from brigade.runner import run_agent_once
from brigade.schemas import (
    Agent,
    Assignment,
    AssignmentKind,
    AssignmentStatus,
    Goal,
    Mission,
    Priority,
    Team,
)
from brigade.state import JsonStateStore
from brigade.time import add_seconds_iso, utc_now_iso
from brigade.tools import ToolContext, default_tool_registry
from brigade.workspace import write_heartbeat_assignment
from tests.helpers import TestProvider


class RaisingProvider:
    def complete(self, prompt: str) -> ModelResponse:
        del prompt
        raise AssertionError("provider should not be called")


class ActionProvider:
    route_type = "test"
    model = "test-actions"

    def complete(self, prompt: str) -> ModelResponse:
        assert "OpenBrigade orchestrator escalation protocol" in prompt
        return ModelResponse(
            text=json.dumps(
                {
                    "status": "actions",
                    "summary": "create recovery work",
                    "actions": [
                        {
                            "type": "create_assignment",
                            "agent_id": "sage",
                            "assignment": "Restart stale goal work",
                            "goal_statement": "Move the goal",
                            "priority": "high",
                            "rationale": "Goal is stale.",
                        }
                    ],
                }
            ),
            provider="test",
            model=self.model,
            route_type=self.route_type,
        )


class MalformedActionProvider:
    route_type = "test"
    model = "test-malformed-orchestrator"

    def complete(self, prompt: str) -> ModelResponse:
        assert "OpenBrigade orchestrator escalation protocol" in prompt
        return ModelResponse(
            text="not json",
            provider="test",
            model=self.model,
            route_type=self.route_type,
        )


def test_human_tasks_are_assigned_before_orchestrator_tasks():
    orchestrator_task = Assignment(
        assignment="Generated task",
        assigned_to="sage",
        created_by="orchestrator",
        source="scheduled_cycle",
        priority=Priority.URGENT,
    )
    human_task = Assignment(
        assignment="Human task",
        assigned_to="garde",
        created_by="human",
        source="direct_command",
        priority=Priority.NORMAL,
    )

    result = deterministic_cycle([orchestrator_task, human_task])

    assert [item.assignment for item in result.assigned] == ["Human task", "Generated task"]
    assert all(item.status == AssignmentStatus.ASSIGNED for item in result.assigned)


def test_cycle_assigns_only_one_task_per_agent():
    first = Assignment(
        assignment="First",
        assigned_to="abacus",
        created_by="human",
        source="direct_command",
    )
    second = Assignment(
        assignment="Second",
        assigned_to="abacus",
        created_by="human",
        source="direct_command",
    )

    result = deterministic_cycle([first, second])

    assert [item.assignment for item in result.assigned] == ["First"]
    assert [item.assignment for item in result.skipped] == ["Second"]


def test_cycle_skips_agent_with_existing_runnable_assignment():
    active = Assignment(
        assignment="Already running",
        assigned_to="abacus",
        created_by="human",
        source="direct_command",
    )
    active.transition_to(AssignmentStatus.ASSIGNED)
    queued = Assignment(
        assignment="New queued work",
        assigned_to="abacus",
        created_by="human",
        source="direct_command",
    )

    result = deterministic_cycle([active, queued])

    assert result.assigned == []
    assert result.skipped == [queued]
    assert queued.status == AssignmentStatus.QUEUED


def test_cycle_waits_for_incomplete_dependencies():
    dependency = Assignment(
        assignment="Finish first",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    dependent = Assignment(
        assignment="Run second",
        assigned_to="garde",
        created_by="human",
        source="direct_command",
        dependency_ids=[dependency.assignment_id],
    )

    result = deterministic_cycle([dependency, dependent])

    assert result.assigned == [dependency]
    assert result.skipped == [dependent]
    assert dependent.status == AssignmentStatus.QUEUED
    assert dependency.assignment_id in dependent.progress_summary


def test_cycle_assigns_after_dependency_is_archived_complete():
    dependency = Assignment(
        assignment="Finished first",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    dependent = Assignment(
        assignment="Run second",
        assigned_to="garde",
        created_by="human",
        source="direct_command",
        dependency_ids=[dependency.assignment_id],
    )
    history = [
        {
            "assignment_id": dependency.assignment_id,
            "final_status": AssignmentStatus.COMPLETE.value,
        }
    ]

    result = deterministic_cycle([dependent], assignment_history=history)

    assert result.assigned == [dependent]
    assert dependent.status == AssignmentStatus.ASSIGNED


def test_derive_agent_states_ignores_queued_backlog():
    agent = Agent(agent_id="abacus", display_name="ABACUS", workspace_path="workspace-abacus")
    queued = Assignment(
        assignment="Queued backlog",
        assigned_to="abacus",
        created_by="human",
        source="direct_command",
    )

    states = derive_agent_states([agent], [queued])

    assert states["abacus"].status == "idle"
    assert states["abacus"].current_assignment_id is None


def test_cycle_writes_heartbeat_for_known_agent(tmp_path):
    agent = Agent(
        agent_id="sage",
        display_name="SAGE",
        workspace_path="workspace-sage",
    )
    assignment = Assignment(
        assignment="Continue mission work",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )

    result = deterministic_cycle([assignment], agents=[agent], workspace_root=tmp_path)

    assert result.alerts == []
    assert result.assigned == [assignment]
    assert assignment.state_row_written_to is not None
    assert (tmp_path / "workspace-sage" / "HEARTBEAT.md").exists()


def test_cycle_alerts_on_unknown_agent():
    assignment = Assignment(
        assignment="Unroutable work",
        assigned_to="missing",
        created_by="human",
        source="direct_command",
    )

    result = deterministic_cycle([assignment], agents=[])

    assert result.assigned == []
    assert result.skipped == [assignment]
    assert result.alerts == [
        f"assignment {assignment.assignment_id} targets unknown agent missing"
    ]


def test_cycle_blocks_goal_misaligned_assignment():
    agent = Agent(agent_id="abacus", display_name="ABACUS", workspace_path="workspace-abacus")
    assignment = Assignment(
        assignment="Spam users with unsupported financial claims",
        assigned_to="abacus",
        created_by="human",
        source="direct_command",
    )
    goal = Goal(
        statement="Find sustainable revenue",
        success_criteria=["validated experiment"],
        explicitly_not=["spam users"],
        set_by="human",
        human_confirmed=True,
    )

    result = deterministic_cycle(
        [assignment],
        agents=[agent],
        goals_by_agent={"abacus": [goal]},
    )

    assert result.assigned == []
    assert assignment.status == AssignmentStatus.BLOCKED
    assert "interrupted" in result.alerts[0]


def test_orchestrator_floor_contains_minimum_snapshot_only(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.set_mission(
        Mission(
            statement="Generate income quickly",
            success_criteria=["revenue exists"],
            explicitly_not=["break constraints"],
        )
    )
    chief = Agent("sage", "SAGE", "workspace-sage", role="crew_chief")
    worker = Agent("scout", "SCOUT", "workspace-scout", team_id="discovery")
    store.add_agent(chief)
    store.add_agent(worker)
    store.upsert_team(
        Team(
            team_id="discovery",
            display_name="Discovery",
            crew_chief_id="sage",
            members=["sage", "scout"],
        )
    )
    store.add_goal(
        "scout",
        Goal(
            statement="Move the goal",
            success_criteria=["work exists"],
            explicitly_not=[],
            set_by="human",
            set_at=add_seconds_iso(utc_now_iso(), -90_000),
        ),
    )
    store.add_knowledge_chunk({"chunk_id": "k1", "text": "domain content"})
    store.add_provenance_record(
        {
            "record_id": "p1",
            "node_id": "node",
            "node_type": "test",
            "created_at": utc_now_iso(),
        }
    )

    floor = build_orchestrator_floor(store)

    assert floor["mission"]["statement"] == "Generate income quickly"
    assert floor["goals"][0]["status"] == "stale"
    assert floor["crew_chief_load"][0]["chief"] == "sage"
    assert "targeted_provenance" not in floor
    assert "knowledge_snippets" not in floor


def test_crew_chief_floor_mirrors_owned_goal_freshness_and_agent_load(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    chief = Agent("sage", "SAGE", "workspace-sage", role="crew_chief")
    worker = Agent("scout", "SCOUT", "workspace-scout", team_id="discovery")
    store.add_agent(chief)
    store.add_agent(worker)
    store.upsert_team(
        Team(
            team_id="discovery",
            display_name="Discovery",
            crew_chief_id="sage",
            members=["sage", "scout"],
        )
    )
    store.add_goal(
        "scout",
        Goal(
            statement="Move the goal",
            success_criteria=["work exists"],
            explicitly_not=[],
            set_by="human",
        ),
    )
    store.add_assignment(
        Assignment(
            assignment="Queued team task",
            assigned_to="scout",
            created_by="human",
            source="test",
            goal_statement="Move the goal",
        )
    )

    floor = build_crew_chief_floor(store, "sage")

    assert [item["agent"] for item in floor["agent_load"]] == ["sage", "scout"]
    assert floor["agent_load"][1]["queue_depth"] == 1
    assert floor["goals"][0]["title"] == "Move the goal"


def test_floor_predicates_respect_future_checkpoint(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(Agent("sage", "SAGE", "workspace-sage"))
    goal = Goal(
        statement="Move the goal",
        success_criteria=["work exists"],
        explicitly_not=[],
        set_by="human",
        set_at=add_seconds_iso(utc_now_iso(), -90_000),
    )
    assignment = Assignment(
        assignment="Long-running work",
        assigned_to="sage",
        created_by="human",
        source="test",
        updated_at=add_seconds_iso(utc_now_iso(), -90_000),
        status=AssignmentStatus.WORKING,
        goal_statement=goal.statement,
        checkpoint_at=add_seconds_iso(utc_now_iso(), 7200),
    )
    store.add_goal("sage", goal)
    store.add_assignment(assignment)

    floor = build_orchestrator_floor(store)
    triggers = evaluate_orchestrator_floor(store, floor)

    assert floor["goals"][0]["status"] == "active"
    assert triggers == []


def test_orchestrator_escalation_skips_provider_when_no_predicates(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(Agent("sage", "SAGE", "workspace-sage"))

    result = run_orchestrator_escalation(store, RaisingProvider(), triggers=[])

    assert result["status"] == "not_needed"
    assert result["actions_applied"] == []


def test_orchestrator_escalation_applies_safe_create_assignment(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(Agent("sage", "SAGE", "workspace-sage"))
    store.add_goal(
        "sage",
        Goal(
            statement="Move the goal",
            success_criteria=["work exists"],
            explicitly_not=[],
            set_by="human",
            set_at=add_seconds_iso(utc_now_iso(), -90_000),
        ),
    )
    floor = build_orchestrator_floor(store)
    triggers = evaluate_orchestrator_floor(store, floor)

    result = run_orchestrator_escalation(
        store,
        ActionProvider(),
        floor=floor,
        triggers=triggers,
    )

    assert result["status"] == "actions"
    assert store.assignments()[0].assignment == "Restart stale goal work"
    assert store.assignments()[0].source == "orchestrator_escalation"


def test_orchestrator_escalation_degrades_on_malformed_model_output(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(Agent("sage", "SAGE", "workspace-sage"))

    result = run_orchestrator_escalation(
        store,
        MalformedActionProvider(),
        triggers=[{"kind": "stale_task", "summary": "task stalled"}],
    )

    assert result["status"] == "no_action"
    assert result["actions_applied"] == []
    assert result["actions_rejected"] == []
    assert "malformed model response" in result["summary"]
    assert store.alerts() == []


def test_orchestrator_rejects_active_task_rebalance(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(Agent("sage", "SAGE", "workspace-sage"))
    store.add_agent(Agent("garde", "GARDE", "workspace-garde"))
    assignment = Assignment(
        assignment="Already active",
        assigned_to="sage",
        created_by="human",
        source="test",
    )
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    store.add_assignment(assignment)

    result = apply_orchestrator_actions(
        store,
        [
            {
                "type": "rebalance_queued_assignment",
                "assignment_id": assignment.assignment_id,
                "to_agent_id": "garde",
            }
        ],
    )

    assert result["applied"] == []
    assert result["rejected"]
    assert store.find_assignment(assignment.assignment_id).assigned_to == "sage"


def test_orchestration_telemetry_normalizes_cycle_decisions():
    assignment = Assignment(
        assignment="Run the plan",
        assigned_to="sage",
        created_by="human",
        source="test",
        goal_statement="Move the mission",
    )
    result = deterministic_cycle([assignment])
    states = derive_agent_states([Agent("sage", "SAGE", "workspace-sage")], [assignment])
    record = build_cycle_reasoning_record(
        "Test mission",
        [assignment],
        result,
        states,
        cycle_outcome=classify_cycle_outcome(
            mission_present=True,
            assignments=[assignment],
            dispatch=result,
        ),
    )

    telemetry = build_orchestration_telemetry([record])

    # v1.0 appends a cycle_outcome event after the per-assignment decisions.
    assert telemetry["latest_event"]["type"] == "cycle_outcome"
    assert telemetry["latest_event"]["decision"] == "worked"
    decision_events = [
        event for event in telemetry["events"] if event["type"] == "cycle_decision"
    ]
    assert decision_events[0]["decision"] == "assigned"
    assert decision_events[0]["assignment_id"] == assignment.assignment_id
    assert telemetry["counts"]["cycle_decision"] == 1
    assert telemetry["counts"]["cycle_outcome"] == 1


def test_orchestration_telemetry_renders_legacy_reasoning_record():
    telemetry = build_orchestration_telemetry(
        [
            {
                "reasoning_id": "legacy",
                "cycle_id": "cycle",
                "ended_at": "2026-01-01T00:00:00+00:00",
                "source": "legacy",
                "mission_statement": "Legacy mission",
                "assigned": [],
                "skipped": [],
                "decision_summary": "assigned=0 skipped=0 alerts=0",
            }
        ]
    )

    assert telemetry["latest_event"]["type"] == "reasoning_summary"
    assert telemetry["latest_event"]["summary"] == "assigned=0 skipped=0 alerts=0"


def test_mission_continuation_propose_only_records_one_proposal_without_task(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.set_mission(Mission("Continue the mission", ["next plan"], []))
    store.add_agent(Agent("chief", "CHIEF", "workspace-chief", "crew_chief"))
    store.upsert_team(Team("alpha", "Alpha", crew_chief_id="chief"))

    result = evaluate_mission_continuation(store)
    telemetry = build_orchestration_telemetry(store.orchestrator_reasoning())

    assert result["status"] == "proposed"
    assert store.assignments() == []
    assert len(telemetry["proposals"]) == 1
    assert telemetry["proposals"][0]["idempotency_key"].startswith(
        "orchestrator-proactive:v1:"
    )


def test_mission_continuation_creation_mode_respects_caps(tmp_path):
    capped = JsonStateStore(tmp_path / "capped.json")
    capped.set_mission(Mission("Continue the mission", ["next plan"], []))
    capped.add_agent(Agent("chief", "CHIEF", "workspace-chief", "crew_chief"))
    capped.upsert_team(Team("alpha", "Alpha", crew_chief_id="chief"))

    capped_result = evaluate_mission_continuation(
        capped,
        ProactiveContinuationConfig(
            mode="create",
            creation_enabled=True,
            max_creations_per_cycle=0,
        ),
    )

    assert capped_result["status"] == "proposed"
    assert capped.assignments() == []

    created = JsonStateStore(tmp_path / "created.json")
    created.set_mission(Mission("Continue the mission", ["next plan"], []))
    created.add_agent(Agent("chief", "CHIEF", "workspace-chief", "crew_chief"))
    created.upsert_team(Team("alpha", "Alpha", crew_chief_id="chief"))

    created_result = evaluate_mission_continuation(
        created,
        ProactiveContinuationConfig(mode="create", creation_enabled=True),
    )

    assert created_result["status"] == "created"
    assert len(created.assignments()) == 1
    assert created.assignments()[0].source == "orchestrator_mission_continuation"


def test_mission_continuation_prevents_duplicate_proposals_by_idempotency(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.set_mission(Mission("Continue the mission", ["next plan"], []))
    store.add_agent(Agent("chief", "CHIEF", "workspace-chief", "crew_chief"))
    store.upsert_team(Team("alpha", "Alpha", crew_chief_id="chief"))

    first = evaluate_mission_continuation(store)
    second = evaluate_mission_continuation(store)

    assert first["status"] == "proposed"
    assert second["status"] == "skipped"
    assert second["skipped"][0]["reason"] == "duplicate_idempotency_key"


def test_mission_continuation_skip_reasons_are_visible(tmp_path):
    no_mission = JsonStateStore(tmp_path / "no-mission.json")
    assert evaluate_mission_continuation(no_mission)["skipped"][0]["reason"] == "no_mission"

    no_chief = JsonStateStore(tmp_path / "no-chief.json")
    no_chief.set_mission(Mission("Continue the mission", ["next plan"], []))
    assert evaluate_mission_continuation(no_chief)["skipped"][0]["reason"] == "no_crew_chief"

    active = JsonStateStore(tmp_path / "active.json")
    active.set_mission(Mission("Continue the mission", ["next plan"], []))
    active.add_agent(Agent("chief", "CHIEF", "workspace-chief", "crew_chief"))
    active.upsert_team(Team("alpha", "Alpha", crew_chief_id="chief"))
    active.add_assignment(
        Assignment(
            assignment="Existing work",
            assigned_to="chief",
            created_by="human",
            source="test",
        )
    )

    assert (
        evaluate_mission_continuation(active)["skipped"][0]["reason"]
        == "active_or_queued_work_exists"
    )


def test_delegate_tool_records_orchestration_event(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    chief = Agent("chief", "CHIEF", "workspace-chief", "crew_chief")
    worker = Agent("worker", "WORKER", "workspace-worker")
    parent = Assignment(
        assignment="Break down mission work",
        assigned_to="chief",
        created_by="human",
        source="test",
    )
    parent.transition_to(AssignmentStatus.ASSIGNED)
    store.add_agent(chief)
    store.add_agent(worker)
    store.add_assignment(parent)

    result = default_tool_registry().execute(
        "delegate",
        ToolContext(agent=chief, assignment=parent, store=store),
        {"agent_id": "worker", "assignment": "Execute the delegated work"},
    )
    telemetry = build_orchestration_telemetry(store.orchestrator_reasoning())

    assert result.ok is True
    assert telemetry["latest_event"]["type"] == "delegated_task"
    assert telemetry["latest_event"]["parent_assignment_id"] == parent.assignment_id
    assert telemetry["latest_event"]["child_assignment_ids"]


def test_parent_completion_records_synthesis_event(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    chief = Agent("chief", "CHIEF", "workspace-chief", "crew_chief")
    parent = Assignment(
        assignment="Synthesize child results",
        assigned_to="chief",
        created_by="human",
        source="test",
    )
    parent.transition_to(AssignmentStatus.ASSIGNED)
    child = Assignment(
        assignment="Child result",
        assigned_to="chief",
        created_by="chief",
        source="agent_delegate",
        parent_assignment_id=parent.assignment_id,
    )
    store.add_agent(chief)
    store.add_assignment(parent)
    store.add_assignment(child)
    write_heartbeat_assignment(chief, parent, tmp_path)

    run_agent_once("chief", store, TestProvider())
    telemetry = build_orchestration_telemetry(store.orchestrator_reasoning())

    assert telemetry["latest_event"]["type"] == "parent_synthesis"
    assert telemetry["latest_event"]["parent_assignment_id"] == parent.assignment_id
    assert child.assignment_id in telemetry["latest_event"]["child_assignment_ids"]


def test_blocked_agent_still_receives_its_own_failure_analysis():
    blocked = Assignment(
        assignment="Original stuck work",
        assigned_to="abacus",
        created_by="human",
        source="direct_command",
    )
    blocked.transition_to(AssignmentStatus.ASSIGNED)
    blocked.transition_to(AssignmentStatus.BLOCKED)
    analysis = Assignment(
        assignment="Failure analysis for the stuck work",
        assigned_to="abacus",
        created_by="orchestrator",
        source="orchestrator_ladder",
        kind=AssignmentKind.FAILURE_ANALYSIS,
        parent_assignment_id=blocked.assignment_id,
    )
    unrelated = Assignment(
        assignment="Unrelated queued work",
        assigned_to="abacus",
        created_by="human",
        source="direct_command",
    )

    result = deterministic_cycle([blocked, analysis, unrelated])

    assert [item.assignment_id for item in result.assigned] == [analysis.assignment_id]
    assert unrelated in result.skipped


# --- Dispatch starvation watchdog (Jul 4-6: 44h stall with no signal) -------------


def _starved_cycle_record(**overrides):
    record = {
        "source": "orchestrator_cycle",
        "assigned": [],
        "skipped": ["task-1", "task-2"],
        "skip_reasons": {"task-1": "agent_blocked", "task-2": "dependencies_unmet"},
    }
    record.update(overrides)
    return record


def _starved_dispatch() -> CycleResult:
    return CycleResult(
        assigned=[],
        skipped=[],
        alerts=[],
        skip_reasons={"task-1": "agent_blocked", "task-2": "dependencies_unmet"},
    )


def test_starvation_streak_counts_consecutive_starved_cycles():
    history = [_starved_cycle_record() for _ in range(3)]

    result = evaluate_dispatch_starvation(history, _starved_dispatch(), threshold=4)

    assert result["starved"] is True
    assert result["streak"] == 4
    assert result["alert"] is not None
    assert "4 consecutive cycles" in result["alert"]
    assert "agent_blocked" in result["alert"]


def test_starvation_alert_waits_for_threshold_and_realerts_periodically():
    dispatch = _starved_dispatch()

    below = evaluate_dispatch_starvation(
        [_starved_cycle_record() for _ in range(2)], dispatch, threshold=4
    )
    assert below["streak"] == 3
    assert below["alert"] is None

    at_double = evaluate_dispatch_starvation(
        [_starved_cycle_record() for _ in range(7)], dispatch, threshold=4
    )
    assert at_double["streak"] == 8
    assert at_double["alert"] is not None

    between = evaluate_dispatch_starvation(
        [_starved_cycle_record() for _ in range(5)], dispatch, threshold=4
    )
    assert between["streak"] == 6
    assert between["alert"] is None


def test_starvation_streak_broken_by_assigning_cycle():
    history = [
        _starved_cycle_record(),
        _starved_cycle_record(assigned=["task-9"]),  # progress happened here
        _starved_cycle_record(),
    ]

    result = evaluate_dispatch_starvation(history, _starved_dispatch(), threshold=4)

    assert result["streak"] == 2  # current cycle + the most recent starved record


def test_starvation_walk_skips_non_cycle_records():
    history = [
        _starved_cycle_record(),
        {"source": "orchestrator_idle_task_builder", "queued_assignments": ["x"]},
        {"source": "agent_delegate", "events": []},
        _starved_cycle_record(),
    ]

    result = evaluate_dispatch_starvation(history, _starved_dispatch(), threshold=3)

    assert result["streak"] == 3
    assert result["alert"] is not None


def test_busy_agents_are_not_starvation():
    busy_dispatch = CycleResult(
        assigned=[],
        skipped=[],
        alerts=[],
        skip_reasons={"task-1": "agent_busy", "task-2": "rest_deferred"},
    )
    history = [_starved_cycle_record() for _ in range(10)]

    result = evaluate_dispatch_starvation(history, busy_dispatch, threshold=2)

    assert result["starved"] is False
    assert result["streak"] == 0
    assert result["alert"] is None


def test_empty_queue_is_not_starvation():
    quiet = CycleResult(assigned=[], skipped=[], alerts=[], skip_reasons={})
    history = [_starved_cycle_record() for _ in range(10)]

    result = evaluate_dispatch_starvation(history, quiet, threshold=2)

    assert result["starved"] is False
    assert result["alert"] is None


def test_create_assignment_resolves_display_name_casing(tmp_path):
    # Prompts show display names (SAGE), ids are lowercase (sage) — the
    # model echoing either casing must not be rejected as unknown.
    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(Agent("sage", "SAGE", "workspace-sage"))
    store.add_agent(Agent("infrastructure", "Infrastructure", "workspace-infrastructure"))

    result = apply_orchestrator_actions(
        store,
        [
            {"type": "create_assignment", "agent_id": "SAGE", "assignment": "task one"},
            {"type": "create_assignment", "agent_id": "Sage", "assignment": "task two"},
            {"type": "create_assignment", "agent_id": "INFRASTRUCTURE", "assignment": "task three"},
        ],
    )

    assert result["rejected"] == []
    assert {item.assigned_to for item in store.assignments()} == {"sage", "infrastructure"}


def test_create_assignment_still_rejects_truly_unknown_agent(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(Agent("sage", "SAGE", "workspace-sage"))

    result = apply_orchestrator_actions(
        store,
        [{"type": "create_assignment", "agent_id": "MERLIN", "assignment": "task"}],
    )

    assert result["applied"] == []
    assert "unknown agent MERLIN" in result["rejected"][0]["reason"]
    assert store.assignments() == []


def test_rebalance_resolves_display_name_casing(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(Agent("sage", "SAGE", "workspace-sage"))
    store.add_agent(Agent("garde", "GARDE", "workspace-garde"))
    assignment = Assignment(
        assignment="Queued work",
        assigned_to="sage",
        created_by="human",
        source="test",
    )
    store.add_assignment(assignment)

    result = apply_orchestrator_actions(
        store,
        [
            {
                "type": "rebalance_queued_assignment",
                "assignment_id": assignment.assignment_id,
                "to_agent_id": "GARDE",
            }
        ],
    )

    assert result["rejected"] == []
    assert store.find_assignment(assignment.assignment_id).assigned_to == "garde"
