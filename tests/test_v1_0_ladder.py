"""Phase 3: blocker-resolution ladder — retry, analysis, reassign, human."""

from __future__ import annotations

from brigade.ladder import (
    EVENT_LADDER_ANALYSIS_CREATED,
    EVENT_LADDER_ESCALATED_HUMAN,
    EVENT_LADDER_REASSIGNED,
    EVENT_LADDER_RETRY,
    create_failure_analysis,
    find_analysis_child,
    ladder_idempotency_key,
    ladder_state,
    resolve_blockers,
)
from brigade.orchestrator import (
    OrchestrationConfig,
    apply_orchestrator_actions,
    build_orchestration_telemetry,
    record_orchestration_events,
    run_full_cycle,
)
from brigade.schemas import (
    Agent,
    Assignment,
    AssignmentKind,
    AssignmentStatus,
    Mission,
    Priority,
    Team,
)
from brigade.state import JsonStateStore


def _store_with_team(tmp_path) -> JsonStateStore:
    store = JsonStateStore(tmp_path / "state.json")
    store.set_mission(Mission("Run the prototype", [], []))
    store.add_agent(Agent("sage", "SAGE", "workspace-sage", role="crew_chief"))
    store.add_agent(
        Agent("ada", "ADA", "workspace-ada", team_id="alpha", specialties=["python"])
    )
    store.add_agent(
        Agent("lin", "LIN", "workspace-lin", team_id="alpha", specialties=["docs"])
    )
    store.upsert_team(
        Team(
            team_id="alpha",
            display_name="Alpha",
            crew_chief_id="sage",
            members=["ada", "lin"],
        )
    )
    return store


def _blocked_assignment(
    store: JsonStateStore,
    *,
    agent: str = "ada",
    failures: int = 1,
    text: str = "Fix the python import bug",
) -> Assignment:
    assignment = Assignment(
        assignment=text,
        assigned_to=agent,
        created_by="human",
        source="direct_command",
        transcript_path="transcripts/run.jsonl",
    )
    store.add_assignment(assignment)
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    for index in range(failures):
        assignment.register_failure(
            f"error {index + 1}: dependency import failed",
            blockers=["missing module"],
        )
    # Fixture-driven ladder state: undo the automatic awaiting_human flip so
    # the ladder's own human step is exercised.
    assignment.awaiting_human = False
    store.update_assignment(assignment)
    return assignment


def _complete_analysis(store: JsonStateStore, blocked: Assignment) -> Assignment:
    child = find_analysis_child(store.assignments(), blocked)
    assert child is not None
    child.transition_to(AssignmentStatus.ASSIGNED)
    child.mark_complete("Root cause: missing python module in the runtime image.")
    store.update_assignment(child)
    return child


# --- Step sequence driven by consecutive_failures fixtures -----------------------


def test_ladder_retry_on_first_failure(tmp_path):
    store = _store_with_team(tmp_path)
    blocked = _blocked_assignment(store, failures=1)

    result = resolve_blockers(store)

    assert [action["step"] for action in result["actions"]] == ["retry"]
    refreshed = store.find_assignment(blocked.assignment_id)
    assert refreshed.status == AssignmentStatus.ASSIGNED
    assert refreshed.state_row_written_to is not None
    assert [event["type"] for event in result["events"]] == [EVENT_LADDER_RETRY]
    key = ladder_idempotency_key(blocked.assignment_id, "retry", 1)
    assert result["actions"][0]["idempotency_key"] == key


def test_ladder_analysis_on_second_failure(tmp_path):
    store = _store_with_team(tmp_path)
    blocked = _blocked_assignment(store, failures=2)

    result = resolve_blockers(store)

    assert [action["step"] for action in result["actions"]] == ["analysis"]
    child = find_analysis_child(store.assignments(), blocked)
    assert child is not None
    assert child.kind == AssignmentKind.FAILURE_ANALYSIS
    assert child.priority == Priority.HIGH
    assert child.assigned_to == "sage"  # the blocked agent's chief
    assert child.parent_assignment_id == blocked.assignment_id
    assert "error 2" in child.assignment
    assert "missing module" in child.assignment
    assert "transcripts/run.jsonl" in child.assignment
    assert child.idempotency_key == ladder_idempotency_key(
        blocked.assignment_id, "analysis", 2
    )
    # The blocked task stays blocked until the analysis child completes.
    assert store.find_assignment(blocked.assignment_id).status == AssignmentStatus.BLOCKED
    assert [event["type"] for event in result["events"]] == [
        EVENT_LADDER_ANALYSIS_CREATED
    ]


def test_ladder_waits_for_analysis_completion(tmp_path):
    store = _store_with_team(tmp_path)
    blocked = _blocked_assignment(store, failures=2)
    resolve_blockers(store)
    blocked = store.find_assignment(blocked.assignment_id)
    blocked.register_failure("error 3: still failing")
    store.update_assignment(blocked)

    result = resolve_blockers(store)

    assert result["actions"] == []
    assert [item["step"] for item in result["waiting"]] == ["waiting_analysis"]
    assert store.find_assignment(blocked.assignment_id).status == AssignmentStatus.BLOCKED


def test_ladder_reassigns_to_idle_specialty_match_after_analysis(tmp_path):
    store = _store_with_team(tmp_path)
    blocked = _blocked_assignment(store, failures=2, text="Fix the python import bug")
    resolve_blockers(store)
    _complete_analysis(store, blocked)
    blocked = store.find_assignment(blocked.assignment_id)
    blocked.register_failure("error 3: still failing")
    store.update_assignment(blocked)

    # "ada" owns the blocked work; "lin" (docs) is the only idle teammate with
    # no specialty match, so seed a python-specialist teammate to win.
    store.add_agent(
        Agent("py", "PY", "workspace-py", team_id="alpha", specialties=["python"])
    )
    team = next(team for team in store.teams() if team.team_id == "alpha")
    store.upsert_team(
        Team(
            team_id="alpha",
            display_name=team.display_name,
            crew_chief_id="sage",
            members=["ada", "lin", "py"],
        )
    )

    result = resolve_blockers(store)

    assert [action["step"] for action in result["actions"]] == ["reassign"]
    refreshed = store.find_assignment(blocked.assignment_id)
    assert refreshed.assigned_to == "py"
    assert refreshed.status == AssignmentStatus.ASSIGNED
    assert "Root cause: missing python module" in refreshed.assignment_rationale
    assert [event["type"] for event in result["events"]] == [EVENT_LADDER_REASSIGNED]


def test_ladder_reassigns_to_any_idle_teammate_without_specialty_match(tmp_path):
    store = _store_with_team(tmp_path)
    blocked = _blocked_assignment(store, failures=2, text="Untangle the deploy halt")
    resolve_blockers(store)
    _complete_analysis(store, blocked)
    blocked = store.find_assignment(blocked.assignment_id)
    blocked.register_failure("error 3: still failing")
    store.update_assignment(blocked)

    result = resolve_blockers(store)

    assert [action["step"] for action in result["actions"]] == ["reassign"]
    assert store.find_assignment(blocked.assignment_id).assigned_to == "lin"


def test_ladder_reassigns_to_chief_when_no_teammate_is_idle(tmp_path):
    store = _store_with_team(tmp_path)
    blocked = _blocked_assignment(store, failures=2)
    resolve_blockers(store)
    _complete_analysis(store, blocked)
    blocked = store.find_assignment(blocked.assignment_id)
    blocked.register_failure("error 3: still failing")
    store.update_assignment(blocked)
    # Occupy the only teammate.
    busy = Assignment(
        assignment="Other work",
        assigned_to="lin",
        created_by="human",
        source="direct_command",
    )
    busy.transition_to(AssignmentStatus.ASSIGNED)
    store.add_assignment(busy)

    result = resolve_blockers(store)

    assert [action["step"] for action in result["actions"]] == ["reassign"]
    assert store.find_assignment(blocked.assignment_id).assigned_to == "sage"


def test_ladder_escalates_to_human_after_five_failures(tmp_path):
    store = _store_with_team(tmp_path)
    blocked = _blocked_assignment(store, failures=5)

    result = resolve_blockers(store)

    assert [action["step"] for action in result["actions"]] == ["human"]
    refreshed = store.find_assignment(blocked.assignment_id)
    assert refreshed.awaiting_human is True
    assert refreshed.status == AssignmentStatus.BLOCKED
    alerts = store.alerts()
    assert any("ladder exhaustion" in alert for alert in alerts)
    assert any("Ladder history" in alert for alert in alerts)
    assert [event["type"] for event in result["events"]] == [
        EVENT_LADDER_ESCALATED_HUMAN
    ]


def test_ladder_creates_missing_analysis_as_catch_up(tmp_path):
    # Failures jumped past ==2 between cycles: at >=3 with no analysis child the
    # ladder creates one instead of deadlocking.
    store = _store_with_team(tmp_path)
    blocked = _blocked_assignment(store, failures=3)

    result = resolve_blockers(store)

    assert [action["step"] for action in result["actions"]] == ["analysis"]
    assert find_analysis_child(store.assignments(), blocked) is not None


# --- Idempotency -----------------------------------------------------------------


def test_analysis_child_is_created_at_most_once(tmp_path):
    store = _store_with_team(tmp_path)
    blocked = _blocked_assignment(store, failures=2)

    first = create_failure_analysis(store, store.find_assignment(blocked.assignment_id))
    second = create_failure_analysis(store, store.find_assignment(blocked.assignment_id))

    assert first is not None
    assert second is None
    children = [
        item
        for item in store.assignments()
        if item.parent_assignment_id == blocked.assignment_id
    ]
    assert len(children) == 1


def test_step_suppressed_when_key_already_persisted(tmp_path):
    store = _store_with_team(tmp_path)
    blocked = _blocked_assignment(store, failures=1)

    first = resolve_blockers(store)
    # Persist the cycle's events the way run_full_cycle does, then simulate a
    # daemon restart that lost the assignment mutation but kept the record.
    record_orchestration_events(
        store,
        source="orchestrator_ladder",
        decision_summary="ladder pass",
        events=first["events"],
    )
    replayed = store.find_assignment(blocked.assignment_id)
    replayed.status = AssignmentStatus.BLOCKED
    store.update_assignment(replayed)

    second = resolve_blockers(store)

    assert second["actions"] == []
    assert [item["step"] for item in second["suppressed"]] == ["retry"]
    assert store.find_assignment(blocked.assignment_id).status == AssignmentStatus.BLOCKED


def test_repeated_cycles_do_not_duplicate_step_actions(tmp_path):
    store = _store_with_team(tmp_path)
    blocked = _blocked_assignment(store, failures=2)
    config = OrchestrationConfig(proactive_mode="off")

    run_full_cycle(store, None, config)
    run_full_cycle(store, None, config)

    children = [
        item
        for item in store.assignments()
        if item.parent_assignment_id == blocked.assignment_id
    ]
    assert len(children) == 1


# --- run_full_cycle wiring --------------------------------------------------------


def test_ladder_runs_before_fresh_dispatch(tmp_path):
    store = _store_with_team(tmp_path)
    blocked = _blocked_assignment(store, failures=1)
    queued = Assignment(
        assignment="Fresh queued work",
        assigned_to="ada",
        created_by="human",
        source="direct_command",
    )
    store.add_assignment(queued)

    result = run_full_cycle(store, None, OrchestrationConfig(proactive_mode="off"))

    ladder = result.sub_results["ladder"]
    assert [action["step"] for action in ladder["actions"]] == ["retry"]
    # The retried assignment occupies ada, so fresh dispatch skips her work.
    assert store.find_assignment(blocked.assignment_id).status == AssignmentStatus.ASSIGNED
    assert result.reasoning_record["skip_reasons"][queued.assignment_id] == "agent_busy"
    assert result.outcome.mode == "worked"
    event_types = [event["type"] for event in result.reasoning_record["events"]]
    assert EVENT_LADDER_RETRY in event_types


def test_ladder_disabled_flag_bypasses_cleanly(tmp_path):
    store = _store_with_team(tmp_path)
    blocked = _blocked_assignment(store, failures=1)

    result = run_full_cycle(
        store,
        None,
        OrchestrationConfig(proactive_mode="off", blocker_resolution_enabled=False),
    )

    assert result.sub_results["ladder"] == {"enabled": False, "actions": []}
    assert store.find_assignment(blocked.assignment_id).status == AssignmentStatus.BLOCKED


def test_ladder_events_appear_in_telemetry(tmp_path):
    store = _store_with_team(tmp_path)
    _blocked_assignment(store, failures=1)

    run_full_cycle(store, None, OrchestrationConfig(proactive_mode="off"))

    telemetry = build_orchestration_telemetry(store.orchestrator_reasoning())
    assert EVENT_LADDER_RETRY in {event["type"] for event in telemetry["events"]}


# --- LLM escalation actions validated against ladder state ------------------------


def test_escalation_retry_applies_when_ladder_state_matches(tmp_path):
    store = _store_with_team(tmp_path)
    blocked = _blocked_assignment(store, failures=1)

    result = apply_orchestrator_actions(
        store,
        [{"type": "retry_blocked_assignment", "assignment_id": blocked.assignment_id}],
    )

    assert result["rejected"] == []
    assert result["applied"][0]["status"] == "applied"
    assert store.find_assignment(blocked.assignment_id).status == AssignmentStatus.ASSIGNED


def test_escalation_rejects_out_of_order_ladder_action(tmp_path):
    store = _store_with_team(tmp_path)
    blocked = _blocked_assignment(store, failures=1)

    result = apply_orchestrator_actions(
        store,
        [
            {
                "type": "reassign_blocked_assignment",
                "assignment_id": blocked.assignment_id,
            }
        ],
    )

    assert result["applied"] == []
    assert "out of ladder order" in result["rejected"][0]["reason"]
    assert store.find_assignment(blocked.assignment_id).assigned_to == "ada"


def test_escalation_rejects_premature_request_human(tmp_path):
    store = _store_with_team(tmp_path)
    blocked = _blocked_assignment(store, failures=1)

    targeted = apply_orchestrator_actions(
        store,
        [
            {
                "type": "request_human",
                "assignment_id": blocked.assignment_id,
                "message": "help",
            }
        ],
    )
    generic = apply_orchestrator_actions(
        store,
        [{"type": "request_human", "message": "help"}],
    )

    assert targeted["applied"] == []
    assert "not exhausted" in targeted["rejected"][0]["reason"]
    assert generic["applied"] == []
    assert "still in progress" in generic["rejected"][0]["reason"]


def test_escalation_allows_request_human_when_ladder_exhausted(tmp_path):
    store = _store_with_team(tmp_path)
    blocked = _blocked_assignment(store, failures=5)

    result = apply_orchestrator_actions(
        store,
        [
            {
                "type": "request_human",
                "assignment_id": blocked.assignment_id,
                "message": "ladder exhausted, need a decision",
            }
        ],
    )

    assert result["rejected"] == []
    assert result["applied"][0]["type"] == "request_human"


# --- Malformed-provider-output cascade guard --------------------------------------


def test_malformed_output_failure_skips_analysis_and_reassigns_directly(tmp_path):
    store = _store_with_team(tmp_path)
    blocked = _blocked_assignment(store, failures=0)
    blocked.register_failure(
        "malformed provider output after 3 attempts: empty model response",
        blockers=["empty model response"],
    )
    blocked.register_failure(
        "malformed provider output after 3 attempts: empty model response",
        blockers=["empty model response"],
    )
    blocked.register_failure(
        "malformed provider output after 3 attempts: empty model response",
        blockers=["empty model response"],
    )
    blocked.awaiting_human = False
    store.update_assignment(blocked)

    result = resolve_blockers(store)

    assert [action["step"] for action in result["actions"]] == ["reassign"]
    assert find_analysis_child(store.assignments(), blocked) is None


def test_failure_analysis_assignment_never_spawns_its_own_analysis_child(tmp_path):
    store = _store_with_team(tmp_path)
    parent = _blocked_assignment(store, failures=2)
    resolve_blockers(store)
    analysis = find_analysis_child(store.assignments(), parent)
    assert analysis is not None

    analysis.transition_to(AssignmentStatus.ASSIGNED)
    analysis.register_failure("still failing")
    analysis.register_failure("still failing")
    analysis.register_failure("still failing")
    analysis.awaiting_human = False
    store.update_assignment(analysis)

    assert ladder_state(store, analysis) == "reassign"
    assert create_failure_analysis(store, analysis) is None

    result = resolve_blockers(store)

    grandchildren = [
        item
        for item in store.assignments()
        if item.parent_assignment_id == analysis.assignment_id
    ]
    assert grandchildren == []
    assert [action["step"] for action in result["actions"]] == ["reassign"]


def test_ladder_state_progression(tmp_path):
    store = _store_with_team(tmp_path)
    blocked = _blocked_assignment(store, failures=1)
    assert ladder_state(store, blocked) == "retry"

    blocked.register_failure("error 2")
    assert ladder_state(store, blocked) == "analysis"

    blocked.register_failure("error 3")
    assert ladder_state(store, blocked) == "analysis"  # catch-up creation
    create_failure_analysis(store, blocked)
    assert ladder_state(store, blocked) == "waiting_analysis"
    _complete_analysis(store, blocked)
    assert ladder_state(store, blocked) == "reassign"

    blocked.register_failure("error 4")
    blocked.register_failure("error 5")
    blocked.awaiting_human = False
    assert ladder_state(store, blocked) == "human"
