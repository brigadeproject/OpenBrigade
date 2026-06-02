from __future__ import annotations

import json

from brigade.orchestrator import (
    apply_orchestrator_actions,
    derive_agent_states,
    deterministic_cycle,
    evaluate_orchestrator_floor,
    run_orchestrator_escalation,
)
from brigade.prompt_floors import build_crew_chief_floor, build_orchestrator_floor
from brigade.providers import ModelResponse
from brigade.schemas import Agent, Assignment, AssignmentStatus, Goal, Mission, Priority, Team
from brigade.state import JsonStateStore
from brigade.time import add_seconds_iso, utc_now_iso


class RaisingProvider:
    def complete(self, prompt: str) -> ModelResponse:
        del prompt
        raise AssertionError("provider should not be called")


class ActionProvider:
    route_type = "simulated"
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
            provider="fake",
            model=self.model,
            route_type=self.route_type,
        )


class MalformedActionProvider:
    route_type = "simulated"
    model = "test-malformed-orchestrator"

    def complete(self, prompt: str) -> ModelResponse:
        assert "OpenBrigade orchestrator escalation protocol" in prompt
        return ModelResponse(
            text="not json",
            provider="fake",
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
    assert store.alerts()


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
