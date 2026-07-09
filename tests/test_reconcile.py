"""Queue reconciliation: dead-dependency remediation ladder and the
cross-agent duplicate sweep (brigade/reconcile.py)."""

from __future__ import annotations

import pytest

from brigade.orchestrator import (
    OrchestrationConfig,
    _notify_operator_escalations,
    deterministic_cycle,
)
from brigade.reconcile import (
    DEAD_DEP_SOURCE,
    DUPLICATE_SWEEP_MAX_PER_CYCLE,
    reconcile_duplicate_assignments,
    remediate_dead_dependencies,
)
from brigade.schemas import Agent, Assignment, AssignmentKind, AssignmentStatus, Team
from brigade.state import JsonStateStore
from brigade.tools import ToolContext, default_tool_registry


# --- helpers ---------------------------------------------------------------------


def _store(tmp_path) -> JsonStateStore:
    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(Agent("ada", "ADA", "workspace-ada"))
    store.add_agent(Agent("lin", "LIN", "workspace-lin"))
    return store


def _assignment(store, *, agent: str = "ada", text: str = "do the work", **kwargs):
    assignment = Assignment(
        assignment=text,
        assigned_to=agent,
        created_by="human",
        source="direct_command",
        **kwargs,
    )
    store.add_assignment(assignment)
    return assignment


def _archive_abandoned(store, assignment, *, reason: str = "gave up") -> Assignment:
    if assignment.status == AssignmentStatus.QUEUED:
        assignment.transition_to(AssignmentStatus.ASSIGNED)
    assignment.transition_to(AssignmentStatus.ABANDONED)
    store.archive_assignment(assignment, reason)
    return assignment


def _config(**kwargs) -> OrchestrationConfig:
    return OrchestrationConfig(**kwargs)


# --- dead-dependency remediation ---------------------------------------------------


def test_rung_one_reissues_dead_dependency_to_same_agent(tmp_path) -> None:
    store = _store(tmp_path)
    root = _assignment(store, agent="ada", text="produce the founding kit")
    dependent = _assignment(
        store,
        agent="lin",
        text="review the founding kit",
        dependency_ids=[root.assignment_id],
    )
    _archive_abandoned(store, root, reason="completion rejected: fabricated files")

    result = remediate_dead_dependencies(store, _config())

    assert [a["decision"] for a in result["actions"]] == ["reissued_same_agent"]
    new_id = result["actions"][0]["new_assignment_id"]
    reissued = store.find_assignment(new_id)
    assert reissued is not None
    assert reissued.assigned_to == "ada"
    assert reissued.source == DEAD_DEP_SOURCE
    assert reissued.reissued_from_assignment_id == root.assignment_id
    assert "fabricated files" in reissued.operator_guidance[0]["operator_message"]
    refreshed = store.find_assignment(dependent.assignment_id)
    assert refreshed.dependency_ids == [new_id]
    assert any(e["type"] == "dead_dependency_reissued" for e in result["events"])


def test_rung_one_is_idempotent_across_reruns(tmp_path) -> None:
    store = _store(tmp_path)
    root = _assignment(store, agent="ada")
    _assignment(store, agent="lin", dependency_ids=[root.assignment_id])
    _archive_abandoned(store, root)

    first = remediate_dead_dependencies(store, _config())
    new_id = first["actions"][0]["new_assignment_id"]
    second = remediate_dead_dependencies(store, _config())

    live_reissues = [a for a in store.assignments() if a.source == DEAD_DEP_SOURCE]
    assert [a.assignment_id for a in live_reissues] == [new_id]
    assert not any(
        action["decision"].startswith("reissued") for action in second["actions"]
    )


def test_rung_two_reassigns_to_idle_teammate(tmp_path) -> None:
    store = _store(tmp_path)
    store.upsert_team(
        Team("crew", "Crew", members=["ada", "lin"], crew_chief_id="lin")
    )
    root = _assignment(store, agent="ada", text="produce the compliance memo")
    _assignment(
        store,
        agent="lin",
        text="publish the compliance memo",
        dependency_ids=[root.assignment_id],
    )
    _archive_abandoned(store, root)

    first = remediate_dead_dependencies(store, _config())
    attempt_one = store.find_assignment(first["actions"][0]["new_assignment_id"])
    assert attempt_one.assigned_to == "ada"
    _archive_abandoned(store, attempt_one, reason="failed again")

    second = remediate_dead_dependencies(store, _config())

    assert [a["decision"] for a in second["actions"]] == ["reissued_escalated_agent"]
    attempt_two = store.find_assignment(second["actions"][0]["new_assignment_id"])
    assert attempt_two.assigned_to != "ada"


def test_rung_three_parks_dependents_and_notifies_once(tmp_path) -> None:
    store = _store(tmp_path)
    store.upsert_team(
        Team("crew", "Crew", members=["ada", "lin"], crew_chief_id="lin")
    )
    root = _assignment(store, agent="ada", text="produce the impossible artifact")
    dependent = _assignment(
        store,
        agent="lin",
        text="consume the impossible artifact",
        dependency_ids=[root.assignment_id],
    )
    _archive_abandoned(store, root, reason="cannot be done")
    for _ in range(2):
        result = remediate_dead_dependencies(store, _config())
        reissued_id = result["actions"][0]["new_assignment_id"]
        _archive_abandoned(
            store, store.find_assignment(reissued_id), reason="cannot be done"
        )

    third = remediate_dead_dependencies(store, _config())

    assert [a["decision"] for a in third["actions"]] == ["escalated_operator"]
    refreshed = store.find_assignment(dependent.assignment_id)
    assert refreshed.status == AssignmentStatus.BLOCKED
    assert refreshed.awaiting_human is True
    assert "operator decision required" in refreshed.progress_summary
    operator_messages = [
        message
        for message in store.messages("orchestrator")
        if "auto-reissue is exhausted" in message.content
    ]
    assert len(operator_messages) == 1
    # The aggregated notification is idempotency-keyed: record the cycle,
    # rerun, and expect no second message.
    store.add_orchestrator_reasoning({"events": third["events"]})
    remediate_dead_dependencies(store, _config())
    operator_messages = [
        message
        for message in store.messages("orchestrator")
        if "auto-reissue is exhausted" in message.content
    ]
    assert len(operator_messages) == 1
    # Step 3.5 then produces the per-assignment escalation notification.
    notify = _notify_operator_escalations(store, _config())
    assert [n["assignment_id"] for n in notify["notified"]] == [
        dependent.assignment_id
    ]


def test_superseded_dependency_with_live_successor_is_relinked(tmp_path) -> None:
    store = _store(tmp_path)
    root = _assignment(store, agent="ada", text="draft the charter")
    successor = _assignment(
        store,
        agent="ada",
        text="draft the charter",
        reissued_from_assignment_id=root.assignment_id,
    )
    dependent = _assignment(
        store, agent="lin", dependency_ids=[root.assignment_id]
    )
    root.transition_to(AssignmentStatus.SUPERSEDED)
    store.archive_assignment(root, "superseded manually")

    result = remediate_dead_dependencies(store, _config())

    assert [a["decision"] for a in result["actions"]] == ["relinked"]
    refreshed = store.find_assignment(dependent.assignment_id)
    assert refreshed.dependency_ids == [successor.assignment_id]
    # A repair consumes no retry budget: no reissue was created.
    assert not [a for a in store.assignments() if a.source == DEAD_DEP_SOURCE]


def test_unknown_dependency_id_is_stripped(tmp_path) -> None:
    store = _store(tmp_path)
    dependent = _assignment(
        store, agent="ada", dependency_ids=["never-existed"]
    )

    result = remediate_dead_dependencies(store, _config())

    assert [a["decision"] for a in result["actions"]] == ["stripped_unknown"]
    refreshed = store.find_assignment(dependent.assignment_id)
    assert refreshed.dependency_ids == []
    assert any("never-existed" in alert for alert in store.alerts())


def test_dead_dependency_without_dependents_is_ignored(tmp_path) -> None:
    store = _store(tmp_path)
    root = _assignment(store, agent="ada")
    _archive_abandoned(store, root)

    result = remediate_dead_dependencies(store, _config())

    assert result["actions"] == []


def test_corrupt_lineage_cycle_terminates(tmp_path) -> None:
    store = _store(tmp_path)
    first = _assignment(store, agent="ada", text="task alpha payload")
    second = _assignment(
        store,
        agent="ada",
        text="task alpha payload",
        reissued_from_assignment_id=first.assignment_id,
    )
    first.reissued_from_assignment_id = second.assignment_id
    store.update_assignment(first)
    _assignment(store, agent="lin", dependency_ids=[first.assignment_id])
    _archive_abandoned(store, store.find_assignment(first.assignment_id))
    _archive_abandoned(store, store.find_assignment(second.assignment_id))

    # Must not hang or crash on the circular lineage.
    result = remediate_dead_dependencies(store, _config())
    assert result["enabled"] is True


def test_disabled_by_auto_recover_flag(tmp_path) -> None:
    store = _store(tmp_path)
    result = remediate_dead_dependencies(
        store, _config(auto_recover_enabled=False)
    )
    assert result == {"enabled": False, "actions": [], "events": []}


def test_reissued_dependency_dispatches_once_complete(tmp_path) -> None:
    """End to end: the wedge clears — reissue, complete it, dependent dispatches."""
    store = _store(tmp_path)
    root = _assignment(store, agent="ada", text="produce the design package")
    dependent = _assignment(
        store,
        agent="lin",
        text="review the design package",
        dependency_ids=[root.assignment_id],
    )
    _archive_abandoned(store, root)

    result = remediate_dead_dependencies(store, _config())
    new_id = result["actions"][0]["new_assignment_id"]
    reissued = store.find_assignment(new_id)
    reissued.transition_to(AssignmentStatus.ASSIGNED)
    reissued.transition_to(AssignmentStatus.COMPLETE)
    store.archive_assignment(reissued, "done")

    cycle = deterministic_cycle(
        store.assignments(),
        agents=store.agents(),
        assignment_history=store.assignment_history(),
    )
    assert dependent.assignment_id in {
        item.assignment_id for item in cycle.assigned
    }


def test_full_cycle_self_heals_wedged_fleet(tmp_path) -> None:
    """The live incident: every queued task waits on an abandoned root. One
    full cycle must reissue the root and dispatch it."""
    from brigade.orchestrator import run_full_cycle
    from brigade.schemas import Goal, Mission

    store = JsonStateStore(tmp_path / "state.json")
    store.set_mission(Mission("Run the prototype", [], []))
    store.add_agent(Agent("sage", "SAGE", "workspace-sage", role="crew_chief"))
    store.add_agent(
        Agent("ada", "ADA", "workspace-ada", team_id="alpha", specialties=["python"])
    )
    store.upsert_team(
        Team(
            team_id="alpha",
            display_name="Alpha",
            crew_chief_id="sage",
            members=["ada"],
        )
    )
    store.add_goal(
        "sage",
        Goal(
            statement="Deliver the prototype",
            success_criteria=[],
            explicitly_not=[],
            set_by="human",
            human_confirmed=True,
            engagement_mode="directive",
        ),
    )
    root = _assignment(store, agent="ada", text="produce the founding kit")
    dependent = _assignment(
        store,
        agent="ada",
        text="review the founding kit",
        dependency_ids=[root.assignment_id],
    )
    _archive_abandoned(store, root, reason="completion rejected: fabricated files")

    result = run_full_cycle(store, config=OrchestrationConfig())

    record = store.orchestrator_reasoning()[-1]
    dead_dep = record["sub_results"]["dead_dependencies"]
    assert [a["decision"] for a in dead_dep["actions"]] == ["reissued_same_agent"]
    new_id = dead_dep["actions"][0]["new_assignment_id"]
    # The reissued root dispatches in this same cycle.
    assert new_id in {item.assignment_id for item in result.dispatch.assigned}
    # The dependent now waits on the live reissue, not the dead root.
    assert store.find_assignment(dependent.assignment_id).dependency_ids == [new_id]


# --- duplicate reconciliation ------------------------------------------------------


DUP_TEXT_A = (
    "Design Stage 3 sovereignty server architecture for local AI agent "
    "operation: local stack components, security model, deployment roadmap"
)
DUP_TEXT_B = (
    "Finalize Stage 3 sovereignty server design: architecture, local stack "
    "components, security model, and deployment roadmap for agent operation"
)


def test_sweep_supersedes_newer_cross_agent_duplicate(tmp_path) -> None:
    store = _store(tmp_path)
    older = _assignment(
        store, agent="ada", text=DUP_TEXT_A, created_at="2026-07-09T09:00:00+00:00"
    )
    upstream = _assignment(store, agent="ada", text="unrelated upstream prerequisite")
    newer = _assignment(
        store,
        agent="lin",
        text=DUP_TEXT_B,
        created_at="2026-07-09T10:00:00+00:00",
        dependency_ids=[upstream.assignment_id],
    )
    dependent = _assignment(
        store,
        agent="lin",
        text="run QA over the finished server design",
        dependency_ids=[newer.assignment_id],
    )

    result = reconcile_duplicate_assignments(store, _config())

    assert [a["superseded_id"] for a in result["actions"]] == [newer.assignment_id]
    assert result["actions"][0]["survivor_id"] == older.assignment_id
    history_ids = {
        entry["assignment_id"]: entry for entry in store.assignment_history()
    }
    archived = history_ids[newer.assignment_id]
    assert archived["final_status"] == AssignmentStatus.SUPERSEDED.value
    assert older.assignment_id in archived["executive_summary"]
    # Dependents move to the survivor; the loser's own upstream ordering is
    # unioned into the survivor.
    assert store.find_assignment(dependent.assignment_id).dependency_ids == [
        older.assignment_id
    ]
    assert upstream.assignment_id in store.find_assignment(
        older.assignment_id
    ).dependency_ids


def test_sweep_keeps_working_newer_over_queued_older(tmp_path) -> None:
    store = _store(tmp_path)
    older = _assignment(
        store, agent="ada", text=DUP_TEXT_A, created_at="2026-07-09T09:00:00+00:00"
    )
    newer = _assignment(
        store, agent="lin", text=DUP_TEXT_B, created_at="2026-07-09T10:00:00+00:00"
    )
    newer.transition_to(AssignmentStatus.ASSIGNED)
    newer.transition_to(AssignmentStatus.WORKING)
    store.update_assignment(newer)

    result = reconcile_duplicate_assignments(store, _config())

    assert [a["superseded_id"] for a in result["actions"]] == [older.assignment_id]
    assert result["actions"][0]["rule"] == "in_flight_survives"


def test_sweep_skips_reissue_lineage_and_parent_child(tmp_path) -> None:
    store = _store(tmp_path)
    original = _assignment(store, agent="ada", text=DUP_TEXT_A)
    _assignment(
        store,
        agent="lin",
        text=DUP_TEXT_A,
        reissued_from_assignment_id=original.assignment_id,
    )
    parent = _assignment(
        store,
        agent="ada",
        text="curate the knowledge base ingestion pipeline for telegram exports",
    )
    _assignment(
        store,
        agent="lin",
        text="curate the knowledge base ingestion pipeline for telegram exports",
        parent_assignment_id=parent.assignment_id,
    )

    result = reconcile_duplicate_assignments(store, _config())

    assert result["actions"] == []


def test_sweep_never_supersedes_in_flight_pairs(tmp_path) -> None:
    store = _store(tmp_path)
    for agent, text in (("ada", DUP_TEXT_A), ("lin", DUP_TEXT_B)):
        item = _assignment(store, agent=agent, text=text)
        item.transition_to(AssignmentStatus.ASSIGNED)
        item.transition_to(AssignmentStatus.WORKING)
        store.update_assignment(item)

    result = reconcile_duplicate_assignments(store, _config())

    assert result["actions"] == []


def test_sweep_honors_per_cycle_cap(tmp_path) -> None:
    store = _store(tmp_path)
    for index in range(DUPLICATE_SWEEP_MAX_PER_CYCLE + 2):
        for agent in ("ada", "lin"):
            _assignment(
                store,
                agent=agent,
                text=(
                    f"compile quarterly telemetry report number {index} covering "
                    "gpu utilization power draw and thermal envelope trends"
                ),
                created_at=f"2026-07-09T0{index}:00:00+00:00",
            )

    result = reconcile_duplicate_assignments(store, _config())

    assert len(result["actions"]) == DUPLICATE_SWEEP_MAX_PER_CYCLE


def test_sweep_disabled_by_flag(tmp_path) -> None:
    store = _store(tmp_path)
    result = reconcile_duplicate_assignments(
        store, _config(duplicate_reconciliation_enabled=False)
    )
    assert result == {"enabled": False, "actions": [], "events": []}


# --- cross-agent creation-time dedup -----------------------------------------------


def test_delegation_dedups_against_other_agents_backlog(tmp_path) -> None:
    store = _store(tmp_path)
    existing = _assignment(store, agent="ada", text=DUP_TEXT_A)
    registry = default_tool_registry()
    delegator = _assignment(store, agent="lin", text="plan the mission")
    delegator.transition_to(AssignmentStatus.ASSIGNED)
    store.update_assignment(delegator)
    context = ToolContext(
        agent=store.agents()[1], assignment=delegator, store=store
    )
    result = registry.execute(
        "delegate",
        context,
        {"agent_id": "lin", "assignment": DUP_TEXT_B},
    )
    assert result.ok
    assert result.metadata["deduplicated"] is True
    assert result.metadata["assignment_id"] == existing.assignment_id
    assert "owned by ada" in result.output


def test_rest_tasks_never_count_as_cross_agent_duplicates(tmp_path) -> None:
    from brigade.rest import rest_assignment_text
    from brigade.tools import _find_backlog_duplicate

    store = _store(tmp_path)
    _assignment(
        store,
        agent="ada",
        text=rest_assignment_text("ada"),
        kind=AssignmentKind.REST,
    )
    assert (
        _find_backlog_duplicate(store, "lin", rest_assignment_text("lin")) is None
    )
