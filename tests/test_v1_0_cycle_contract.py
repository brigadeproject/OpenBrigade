from __future__ import annotations

import pytest

from brigade.orchestrator import (
    NO_WORK_REASONS,
    CycleOutcome,
    CycleResult,
    OrchestrationConfig,
    apply_orchestrator_actions,
    build_cycle_reasoning_record,
    build_idle_agent_assignments,
    classify_cycle_outcome,
    derive_agent_states,
    deterministic_cycle,
    evaluate_mission_continuation,
    evaluate_orchestrator_floor,
    is_team_of_one,
    route_to_chief,
    run_full_cycle,
)
from brigade.prompt_floors import (
    CREW_CHIEF_SYSTEM_PROMPT,
    build_crew_chief_floor,
    build_orchestrator_floor,
)
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

EMPTY_DISPATCH = CycleResult(assigned=[], skipped=[], alerts=[])


def _assignment(text="Work", agent="sage", **kwargs) -> Assignment:
    return Assignment(
        assignment=text,
        assigned_to=agent,
        created_by=kwargs.pop("created_by", "human"),
        source=kwargs.pop("source", "direct_command"),
        **kwargs,
    )


def _store_with_team(tmp_path, *, chief_goal_mode="directive") -> JsonStateStore:
    store = JsonStateStore(tmp_path / "state.json")
    store.set_mission(Mission("Run the prototype", [], []))
    store.add_agent(Agent("sage", "SAGE", "workspace-sage", role="crew_chief"))
    store.add_agent(
        Agent("ada", "ADA", "workspace-ada", team_id="alpha", specialties=["python"])
    )
    store.upsert_team(
        Team(team_id="alpha", display_name="Alpha", crew_chief_id="sage", members=["ada"])
    )
    store.add_goal(
        "sage",
        Goal(
            statement="Deliver the prototype",
            success_criteria=[],
            explicitly_not=[],
            set_by="human",
            human_confirmed=True,
            engagement_mode=chief_goal_mode,
        ),
    )
    return store


# --- CycleOutcome taxonomy: one test per reason ---------------------------------


def test_outcome_no_mission():
    outcome = classify_cycle_outcome(mission_present=False, assignments=[])

    assert outcome.mode == "no_work"
    assert outcome.reason == "no_mission"


def test_outcome_all_blocked_awaiting_human():
    blocked = _assignment()
    blocked.transition_to(AssignmentStatus.BLOCKED)
    blocked.awaiting_human = True

    outcome = classify_cycle_outcome(
        mission_present=True,
        assignments=[blocked],
        dispatch=EMPTY_DISPATCH,
    )

    assert outcome.reason == "all_blocked_awaiting_human"


def test_outcome_dependencies_unmet():
    dependency = _assignment("Do first")
    dependency.transition_to(AssignmentStatus.BLOCKED)
    dependent = _assignment("Do second", agent="ada", dependency_ids=[dependency.assignment_id])

    dispatch = deterministic_cycle([dependent])
    outcome = classify_cycle_outcome(
        mission_present=True,
        assignments=[dependent],
        dispatch=dispatch,
    )

    assert outcome.reason == "dependencies_unmet"


def test_outcome_all_agents_busy():
    active = _assignment("Running")
    active.transition_to(AssignmentStatus.ASSIGNED)
    queued = _assignment("Waiting")

    dispatch = deterministic_cycle([active, queued])
    outcome = classify_cycle_outcome(
        mission_present=True,
        assignments=[active, queued],
        dispatch=dispatch,
    )

    assert outcome.reason == "all_agents_busy"


def test_outcome_provider_unavailable():
    outcome = classify_cycle_outcome(
        mission_present=True,
        assignments=[],
        dispatch=EMPTY_DISPATCH,
        provider_failed=True,
    )

    assert outcome.reason == "provider_unavailable"


def test_outcome_rest_window():
    outcome = classify_cycle_outcome(
        mission_present=True,
        assignments=[],
        dispatch=EMPTY_DISPATCH,
        rest={"enabled": True, "created": [], "already_rested": ["sage"]},
    )

    assert outcome.reason == "rest_window"


def test_outcome_intake_only_pending_approval():
    outcome = classify_cycle_outcome(
        mission_present=True,
        assignments=[],
        dispatch=EMPTY_DISPATCH,
        intake={"mode": "propose", "proposals": [{"source_id": "doc-1"}], "created": []},
    )

    assert outcome.reason == "intake_only_pending_approval"


def test_outcome_queue_empty_proposal_recorded():
    outcome = classify_cycle_outcome(
        mission_present=True,
        assignments=[],
        dispatch=EMPTY_DISPATCH,
        continuation={"status": "proposed", "created": [], "skipped": []},
    )

    assert outcome.reason == "queue_empty_proposal_recorded"


def test_outcome_duplicate_suppressed():
    outcome = classify_cycle_outcome(
        mission_present=True,
        assignments=[],
        dispatch=EMPTY_DISPATCH,
        continuation={
            "status": "skipped",
            "created": [],
            "skipped": [{"reason": "duplicate_idempotency_key"}],
        },
    )

    assert outcome.reason == "duplicate_suppressed"


def test_outcome_budget_gate_is_reserved():
    # budget_gate is in the taxonomy for post-RC cost-aware routing but is
    # never emitted in v1.0; it must remain constructible for forward compat.
    assert "budget_gate" in NO_WORK_REASONS
    outcome = CycleOutcome(mode="no_work", reason="budget_gate", summary="reserved")
    assert outcome.reason == "budget_gate"


def test_outcome_unclassified_is_fallback():
    outcome = classify_cycle_outcome(
        mission_present=True,
        assignments=[],
        dispatch=EMPTY_DISPATCH,
    )

    assert outcome.reason == "unclassified"


def test_outcome_worked_and_work_in_flight():
    queued = _assignment("Dispatch me")
    dispatch = deterministic_cycle([queued])
    worked = classify_cycle_outcome(
        mission_present=True,
        assignments=[queued],
        dispatch=dispatch,
    )
    assert worked.mode == "worked"
    assert worked.actions[0]["assignment_id"] == queued.assignment_id

    working = _assignment("Long running")
    working.transition_to(AssignmentStatus.ASSIGNED)
    working.transition_to(AssignmentStatus.WORKING)
    in_flight = classify_cycle_outcome(
        mission_present=True,
        assignments=[working],
        dispatch=EMPTY_DISPATCH,
    )
    assert in_flight.mode == "work_in_flight"
    assert in_flight.in_flight_assignment_ids == [working.assignment_id]


def test_cycle_outcome_rejects_invalid_values():
    with pytest.raises(ValueError, match="mode"):
        CycleOutcome(mode="maybe", reason=None, summary="x")
    with pytest.raises(ValueError, match="no_work reason"):
        CycleOutcome(mode="no_work", reason="because", summary="x")
    with pytest.raises(ValueError, match="only valid for no_work"):
        CycleOutcome(mode="worked", reason="no_mission", summary="x")


# --- Reasoning record v2 ---------------------------------------------------------


def test_reasoning_record_requires_cycle_outcome():
    with pytest.raises(TypeError):
        build_cycle_reasoning_record("Mission", [], EMPTY_DISPATCH, {})  # type: ignore[call-arg]


def test_reasoning_record_v2_shape():
    queued = _assignment()
    dispatch = deterministic_cycle([queued])
    outcome = classify_cycle_outcome(
        mission_present=True, assignments=[queued], dispatch=dispatch
    )

    record = build_cycle_reasoning_record(
        "Mission",
        [queued],
        dispatch,
        {},
        cycle_outcome=outcome,
        sub_results={"ladder": {"actions": []}},
        config_snapshot=OrchestrationConfig().snapshot(),
    )

    assert record["record_version"] == 2
    assert record["cycle_outcome"]["mode"] == "worked"
    assert record["skip_reasons"] == {}
    assert record["sub_results"]["ladder"] == {"actions": []}
    assert record["config_snapshot"]["intake_mode"] == "propose"
    outcome_events = [e for e in record["events"] if e["type"] == "cycle_outcome"]
    assert len(outcome_events) == 1


# --- Dispatch policy changes -----------------------------------------------------


def test_blocked_agent_receives_no_fresh_work():
    blocked = _assignment("Stuck work")
    blocked.transition_to(AssignmentStatus.BLOCKED)
    queued = _assignment("Fresh work")

    result = deterministic_cycle([blocked, queued])

    assert result.assigned == []
    assert result.skip_reasons[queued.assignment_id] == "agent_blocked"
    assert queued.status == AssignmentStatus.QUEUED


def test_rest_sorts_last_and_never_preempts_mission_work():
    rest = _assignment(
        "Dream cycle",
        agent="ada",
        created_by="orchestrator",
        source="rest_scheduler",
        kind=AssignmentKind.REST,
        priority=Priority.LOW,
    )
    mission_low = _assignment(
        "Low priority mission work",
        agent="sage",
        created_by="orchestrator",
        source="scheduled_cycle",
        priority=Priority.LOW,
    )

    result = deterministic_cycle([rest, mission_low])

    assert [item.assignment_id for item in result.assigned] == [
        mission_low.assignment_id,
        rest.assignment_id,
    ]


def test_rest_deferred_when_agent_has_queued_mission_work():
    dependency = _assignment("Must finish first", agent="other")
    dependency.transition_to(AssignmentStatus.BLOCKED)
    gated_mission = _assignment(
        "Mission work waiting on dependency",
        dependency_ids=[dependency.assignment_id],
    )
    rest = _assignment(
        "Dream cycle",
        created_by="orchestrator",
        source="rest_scheduler",
        kind=AssignmentKind.REST,
        priority=Priority.LOW,
    )

    result = deterministic_cycle([gated_mission, rest])

    assert result.assigned == []
    assert result.skip_reasons[rest.assignment_id] == "rest_deferred"
    assert result.skip_reasons[gated_mission.assignment_id] == "dependencies_unmet"


def test_dispatch_records_machine_readable_skip_reasons():
    busy_active = _assignment("Active")
    busy_active.transition_to(AssignmentStatus.ASSIGNED)
    busy_queued = _assignment("More for busy agent")
    unknown = _assignment("Unroutable", agent="ghost")
    misaligned = _assignment("Spam users", agent="ada")
    goal = Goal(
        statement="Find revenue",
        success_criteria=[],
        explicitly_not=["spam users"],
        set_by="human",
        human_confirmed=True,
    )

    result = deterministic_cycle(
        [busy_active, busy_queued, unknown, misaligned],
        agents=[
            Agent("sage", "SAGE", "workspace-sage"),
            Agent("ada", "ADA", "workspace-ada"),
        ],
        goals_by_agent={"ada": [goal]},
    )

    assert result.skip_reasons[busy_queued.assignment_id] == "agent_busy"
    assert result.skip_reasons[unknown.assignment_id] == "unknown_agent"
    assert result.skip_reasons[misaligned.assignment_id] == "goal_misaligned"


# --- run_full_cycle and the persisted record ---------------------------------------


def test_run_full_cycle_without_mission_records_no_mission(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")

    result = run_full_cycle(store)

    assert result.outcome.reason == "no_mission"
    records = store.orchestrator_reasoning()
    assert records[-1]["cycle_outcome"]["reason"] == "no_mission"
    assert any(e["type"] == "cycle_outcome" for e in records[-1]["events"])


def test_run_full_cycle_dispatches_and_persists_v2_record(tmp_path):
    store = _store_with_team(tmp_path)
    queued = _assignment("Human work for the chief")
    store.add_assignment(queued)

    result = run_full_cycle(store, config=OrchestrationConfig())

    assert result.outcome.mode == "worked"
    record = store.orchestrator_reasoning()[-1]
    assert record["record_version"] == 2
    assert record["cycle_outcome"]["mode"] == "worked"
    assert "config_snapshot" in record and "sub_results" in record
    stored = store.find_assignment(queued.assignment_id)
    assert stored is not None
    assert stored.status == AssignmentStatus.ASSIGNED


def test_run_full_cycle_empty_queue_records_proposal_outcome(tmp_path):
    store = _store_with_team(tmp_path)

    # rest_enabled=False: inside the UTC rest window the cycle would create a
    # rest assignment and report mode "worked" instead of the quiet outcome
    # this test asserts.
    result = run_full_cycle(store, config=OrchestrationConfig(rest_enabled=False))

    assert result.outcome.mode == "no_work"
    assert result.outcome.reason == "queue_empty_proposal_recorded"


def test_idle_on_call_chief_is_clean_no_work_not_stale(tmp_path):
    store = _store_with_team(tmp_path, chief_goal_mode="on_call")

    result = run_full_cycle(store, config=OrchestrationConfig(rest_enabled=False))

    # No synthesized work for the on-call chief and a clean taxonomy outcome.
    assert store.assignments() == []
    assert result.outcome.mode == "no_work"
    assert result.outcome.reason == "queue_empty_proposal_recorded"
    assert "all_chiefs_on_call" in result.outcome.summary

    floor = build_orchestrator_floor(store)
    assert all(not goal["stale"] for goal in floor["goals"])
    triggers = evaluate_orchestrator_floor(store, floor)
    assert [t for t in triggers if t["kind"] == "stale_goal"] == []


# --- Chief-first creation paths -----------------------------------------------------


def test_mission_continuation_targets_directive_chief_with_decomposition_text(tmp_path):
    store = _store_with_team(tmp_path)

    result = evaluate_mission_continuation(store)

    assert result["status"] == "proposed"
    assert result["proposal"]["assigned_to"] == "sage"
    assert "create_subtasks" in result["proposal"]["assignment"]


def test_mission_continuation_team_of_one_text_variant(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.set_mission(Mission("Run the prototype", [], []))
    store.add_agent(Agent("solo", "SOLO", "workspace-solo", role="crew_chief"))

    result = evaluate_mission_continuation(store)

    assert result["proposal"]["assigned_to"] == "solo"
    assert "your own subject-matter expert" in result["proposal"]["assignment"]


def test_route_to_chief_and_team_of_one_helpers(tmp_path):
    store = _store_with_team(tmp_path)

    chief = route_to_chief(store, agent_id="ada")
    assert chief is not None and chief.agent_id == "sage"
    assert route_to_chief(store, agent_id="sage").agent_id == "sage"
    assert not is_team_of_one(store, "sage")

    solo_store = JsonStateStore(tmp_path / "solo.json")
    solo_store.add_agent(Agent("solo", "SOLO", "workspace-solo", role="crew_chief"))
    assert is_team_of_one(solo_store, "solo")


def test_escalation_create_assignment_routes_to_chief(tmp_path):
    store = _store_with_team(tmp_path)

    outcome = apply_orchestrator_actions(
        store,
        [
            {
                "type": "create_assignment",
                "agent_id": "ada",
                "assignment": "Recover the stale goal",
                "rationale": "Goal is stale.",
            }
        ],
    )

    assert outcome["rejected"] == []
    created = store.assignments()[0]
    assert created.assigned_to == "sage"
    assert "suggested agent was ada" in created.assignment_rationale


def test_idle_synthesis_targets_chief_for_worker_goal(tmp_path):
    store = _store_with_team(tmp_path)
    store.add_goal(
        "ada",
        Goal(
            statement="Harden the deployment scripts",
            success_criteria=[],
            explicitly_not=[],
            set_by="human",
            human_confirmed=True,
        ),
    )

    created = build_idle_agent_assignments(store)

    worker_goal_items = [
        item for item in created if "Harden the deployment scripts" in item.assignment
    ]
    assert worker_goal_items
    assert all(item.assigned_to == "sage" for item in worker_goal_items)


def test_idle_synthesis_skips_on_call_goals(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.set_mission(Mission("Run the prototype", [], []))
    store.add_agent(Agent("garde", "GARDE", "workspace-garde", role="crew_chief"))
    store.add_goal(
        "garde",
        Goal(
            statement="Stand by for infrastructure work",
            success_criteria=[],
            explicitly_not=[],
            set_by="human",
            human_confirmed=True,
            engagement_mode="on_call",
        ),
    )

    created = build_idle_agent_assignments(store)

    assert created == []


# --- Agent state idle-cycle tracking ------------------------------------------------


def test_derive_agent_states_tracks_idle_cycles_across_cycles():
    agent = Agent("sage", "SAGE", "workspace-sage")

    first = derive_agent_states([agent], [])
    assert first["sage"].idle_cycles == 1

    second = derive_agent_states([agent], [], existing=first)
    assert second["sage"].idle_cycles == 2

    active = _assignment("Now working")
    active.transition_to(AssignmentStatus.ASSIGNED)
    third = derive_agent_states([agent], [active], existing=second)
    assert third["sage"].idle_cycles == 0

    fourth = derive_agent_states([agent], [], existing=third)
    assert fourth["sage"].idle_cycles == 1


# --- Crew chief floor -----------------------------------------------------------------


def test_crew_chief_floor_lists_member_specialties(tmp_path):
    store = _store_with_team(tmp_path)

    floor = build_crew_chief_floor(store, "sage")

    by_agent = {row["agent"]: row for row in floor["agent_load"]}
    assert by_agent["ada"]["specialties"] == ["python"]
    assert "specialties match" in CREW_CHIEF_SYSTEM_PROMPT


def test_idle_synthesis_refires_after_previous_plan_archives(tmp_path, monkeypatch):
    store = JsonStateStore(tmp_path / "state.json")
    store.set_mission(Mission("Run the prototype", [], []))
    store.add_agent(Agent("sage", "SAGE", "workspace-sage", role="crew_chief"))
    store.upsert_team(Team("alpha", "Alpha", crew_chief_id="sage"))

    first = build_idle_agent_assignments(store)
    assert len(first) == 1

    # Complete and archive the planning task; within the same bucket the
    # history dedupe acts as a cooldown.
    done = first[0]
    done.transition_to(AssignmentStatus.ASSIGNED)
    done.mark_complete("plan queued")
    store.archive_assignment(done, executive_summary="plan queued")
    assert build_idle_agent_assignments(store) == []

    # A new bucket must re-fire the planner instead of being suppressed by
    # the archived key forever.
    monkeypatch.setattr(
        "brigade.orchestrator._idle_replan_bucket", lambda: "2099-01-01T00"
    )
    refired = build_idle_agent_assignments(store)
    assert len(refired) == 1
    assert refired[0].assigned_to == "sage"
