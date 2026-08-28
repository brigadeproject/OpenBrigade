"""Phase 5: rest and dream cycles — scheduling, protocol, deterministic finalizer."""

from __future__ import annotations

from datetime import datetime, timezone

from brigade.governance import ensure_policy_projections_current, policy_projection_diff
from brigade.memory import MAX_MEMORY_BYTES
from brigade.orchestrator import OrchestrationConfig, run_full_cycle
from brigade.rest import (
    EVENT_REST_COMPLETED,
    evaluate_rest_schedule,
    finalize_rest_assignment,
    rest_assignment_text,
    rest_idempotency_key,
)
from brigade.schemas import (
    Agent,
    AgentState,
    Assignment,
    AssignmentKind,
    AssignmentStatus,
    Mission,
    Priority,
)
from brigade.state import JsonStateStore
from brigade.time import utc_now_iso
from brigade.workspace import REQUIRED_AGENT_FILES, ensure_agent_workspace

IN_WINDOW = datetime(2026, 6, 12, 3, 30, tzinfo=timezone.utc)
OUT_OF_WINDOW = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)


def _store(tmp_path) -> JsonStateStore:
    store = JsonStateStore(tmp_path / "state.json")
    store.set_mission(Mission("Run the prototype", [], []))
    store.add_agent(Agent("ada", "ADA", "workspace-ada"))
    return store


# --- Eligibility -----------------------------------------------------------------


def test_window_rest_scheduled_inside_utc_window(tmp_path):
    store = _store(tmp_path)

    result = evaluate_rest_schedule(store, now=IN_WINDOW)

    assert [item["trigger"] for item in result["created"]] == ["window"]
    assignment = store.find_assignment(result["created"][0]["assignment_id"])
    assert assignment.kind == AssignmentKind.REST
    assert assignment.priority == Priority.LOW
    assert assignment.idempotency_key == rest_idempotency_key("ada", "20260612", "window")


def test_opportunistic_rest_after_idle_threshold(tmp_path):
    store = _store(tmp_path)
    store.upsert_agent_state(AgentState(agent="ada", status="idle", idle_cycles=6))

    result = evaluate_rest_schedule(store, now=OUT_OF_WINDOW)

    assert [item["trigger"] for item in result["created"]] == ["idle"]


def test_no_rest_outside_window_below_idle_threshold(tmp_path):
    store = _store(tmp_path)
    store.upsert_agent_state(AgentState(agent="ada", status="idle", idle_cycles=5))

    result = evaluate_rest_schedule(store, now=OUT_OF_WINDOW)

    assert result["created"] == []
    assert result["already_rested"] == []


def test_rest_never_offered_over_queued_mission_work(tmp_path):
    store = _store(tmp_path)
    store.add_assignment(
        Assignment(
            assignment="Mission work",
            assigned_to="ada",
            created_by="human",
            source="direct_command",
        )
    )

    result = evaluate_rest_schedule(store, now=IN_WINDOW)

    assert result["created"] == []


def test_rest_disabled_flag(tmp_path):
    store = _store(tmp_path)

    result = evaluate_rest_schedule(store, enabled=False, now=IN_WINDOW)

    assert result == {
        "enabled": False,
        "created": [],
        "already_rested": [],
        "events": [],
    }


# --- Idempotency and suppression ---------------------------------------------------


def test_one_scheduled_rest_per_agent_per_day(tmp_path):
    store = _store(tmp_path)

    first = evaluate_rest_schedule(store, now=IN_WINDOW)
    # Archive the first rest as completed, then ask again the same day.
    assignment = store.find_assignment(first["created"][0]["assignment_id"])
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    assignment.mark_complete("rested")
    store.archive_assignment(assignment, executive_summary="rested")
    second = evaluate_rest_schedule(store, min_interval_seconds=0, now=IN_WINDOW)

    assert len(first["created"]) == 1
    assert second["created"] == []
    assert [item["reason"] for item in second["already_rested"]] == [
        "already_rested_today"
    ]


def test_opportunistic_rest_allowed_after_window_rest_same_day(tmp_path):
    store = _store(tmp_path)
    window = evaluate_rest_schedule(store, now=IN_WINDOW)
    assignment = store.find_assignment(window["created"][0]["assignment_id"])
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    assignment.mark_complete("rested")
    assignment.updated_at = "2026-06-12T03:45:00+00:00"
    store.archive_assignment(assignment, executive_summary="rested")
    store.upsert_agent_state(AgentState(agent="ada", status="idle", idle_cycles=9))

    idle = evaluate_rest_schedule(store, min_interval_seconds=0, now=OUT_OF_WINDOW)

    assert [item["trigger"] for item in idle["created"]] == ["idle"]


def test_min_interval_suppresses_fresh_rest(tmp_path):
    store = _store(tmp_path)
    first = evaluate_rest_schedule(store, now=IN_WINDOW)
    assignment = store.find_assignment(first["created"][0]["assignment_id"])
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    assignment.mark_complete("rested")
    assignment.updated_at = "2026-06-12T03:45:00+00:00"
    store.archive_assignment(assignment, executive_summary="rested")
    store.upsert_agent_state(AgentState(agent="ada", status="idle", idle_cycles=9))

    later_same_day = evaluate_rest_schedule(store, now=OUT_OF_WINDOW)

    assert later_same_day["created"] == []
    assert [item["reason"] for item in later_same_day["already_rested"]] == [
        "min_interval"
    ]


def test_pending_rest_assignment_suppresses_duplicate(tmp_path):
    store = _store(tmp_path)
    first = evaluate_rest_schedule(store, now=IN_WINDOW)
    assert len(first["created"]) == 1

    # The rest assignment is still queued: the agent is not "occupied" by it
    # (queued rest does not block), but the day key already exists.
    second = evaluate_rest_schedule(store, now=IN_WINDOW)

    assert second["created"] == []
    assert [item["reason"] for item in second["already_rested"]] == [
        "already_rested_today"
    ]


# --- Dream protocol text -----------------------------------------------------------


def test_rest_protocol_text_snapshot():
    text = rest_assignment_text("20260612")
    assert "memory/*-MEMORY.md" in text
    assert "MEMORY.md, keeping it at or under 2KB" in text
    assert "reflections.md" in text
    assert "candidate, promoted, or archived" in text
    assert "up to three questions from PONDER.md" in text
    assert "rest/20260612-REST.md" in text
    for section in ("## Promoted", "## Pruned", "## Reflections", "## Ponderings", "## Proposals"):
        assert f"'{section}'" in text
    assert "[efficiency]" in text and "[tool_request]" in text


# --- Deterministic finalizer --------------------------------------------------------


def _finalizer_fixture(tmp_path):
    store = _store(tmp_path)
    agent = next(item for item in store.agents() if item.agent_id == "ada")
    workspace = ensure_agent_workspace(agent, store.data_dir)
    # Junk model output: an oversized MEMORY.md the cap must rein in.
    (workspace / "MEMORY.md").write_text(
        "# Memory\n\n" + "".join(f"- junk note {index}\n" for index in range(200)),
        encoding="utf-8",
    )
    memory_dir = workspace / "memory"
    memory_dir.mkdir(exist_ok=True)
    (memory_dir / "20260101-MEMORY.md").write_text(
        "- old durable fact\n", encoding="utf-8"
    )
    rest_dir = workspace / "rest"
    rest_dir.mkdir(exist_ok=True)
    (rest_dir / "20260612-REST.md").write_text(
        "\n".join(
            [
                "# Rest report",
                "## Promoted",
                "- learned the deploy flow",
                "## Pruned",
                "- stale note",
                "## Reflections",
                "- shipped the importer; worked; lesson: test fixtures first",
                "## Ponderings",
                "- should retries back off exponentially?",
                "## Proposals",
                "- [efficiency] automate the weekly digest",
                "- [tool_request] need a csv-diff tool",
                "- consider rotating reviewers",
            ]
        ),
        encoding="utf-8",
    )
    assignment = Assignment(
        assignment=rest_assignment_text("20260612"),
        assigned_to="ada",
        created_by="orchestrator",
        source="orchestrator_rest",
        kind=AssignmentKind.REST,
        priority=Priority.LOW,
        idempotency_key=rest_idempotency_key("ada", "20260612", "window"),
    )
    return store, agent, workspace, assignment


def test_finalizer_enforces_memory_cap_and_persists_outputs(tmp_path):
    store, agent, workspace, assignment = _finalizer_fixture(tmp_path)
    ensure_policy_projections_current(store, agent)

    result = finalize_rest_assignment(store, agent, assignment)

    assert len((workspace / "MEMORY.md").read_bytes()) <= MAX_MEMORY_BYTES
    assert policy_projection_diff(store, agent) == []
    reconciliations = [
        item
        for item in store.provenance_records()
        if item.get("event") == "policy_projection_reconciled"
    ]
    assert reconciliations[-1]["path"] == "MEMORY.md"
    assert reconciliations[-1]["source"] == "rest_cycle"
    # One proposal row per ## Proposals bullet, kinds from the tags.
    proposals = store.proposals()
    assert len(proposals) == 3
    kinds = sorted(item["kind"] for item in proposals)
    assert kinds == ["efficiency", "rest_insight", "tool_request"]
    # Stale daily note archived into an episode plus the rest_cycle episode.
    episodes = store.episodes()
    source_kinds = sorted(item["source_kind"] for item in episodes)
    assert source_kinds == ["daily_memory", "rest_cycle"]
    assert not (workspace / "memory" / "20260101-MEMORY.md").exists()
    rest_episode = next(
        item for item in episodes if item["source_kind"] == "rest_cycle"
    )
    assert rest_episode["learned_facts"] == ["learned the deploy flow"]
    # rest_completed event persisted in a reasoning record.
    events = [
        event
        for record in store.orchestrator_reasoning()
        for event in record.get("events", [])
    ]
    assert EVENT_REST_COMPLETED in {event["type"] for event in events}
    assert result["proposals"][0]["details"]["source"] == "rest_cycle"


def test_finalizer_is_idempotent_per_proposal_bullet(tmp_path):
    store, agent, _workspace, assignment = _finalizer_fixture(tmp_path)

    finalize_rest_assignment(store, agent, assignment)
    finalize_rest_assignment(store, agent, assignment)

    assert len(store.proposals()) == 3  # deduped by proposal idempotency key


def test_finalizer_without_report_still_curates(tmp_path):
    store, agent, workspace, assignment = _finalizer_fixture(tmp_path)
    (workspace / "rest" / "20260612-REST.md").unlink()

    result = finalize_rest_assignment(store, agent, assignment)

    assert result["report"] is None
    assert store.proposals() == []
    assert len((workspace / "MEMORY.md").read_bytes()) <= MAX_MEMORY_BYTES


def test_empty_ponder_records_daily_task_failure_and_candidate_remedy(tmp_path):
    store, agent, workspace, assignment = _finalizer_fixture(tmp_path)
    date_key = utc_now_iso()[:10].replace("-", "")
    assignment.idempotency_key = rest_idempotency_key(agent.agent_id, date_key, "window")
    completed = Assignment(
        assignment="Completed daily deployment check",
        assigned_to=agent.agent_id,
        created_by="human",
        source="direct_command",
    )
    completed.transition_to(AssignmentStatus.ASSIGNED)
    completed.mark_complete("done")
    store.archive_assignment(completed, executive_summary="done")
    failed = Assignment(
        assignment="Repair unavailable reporting tool",
        assigned_to=agent.agent_id,
        created_by="human",
        source="direct_command",
    )
    failed.transition_to(AssignmentStatus.ASSIGNED)
    failed.register_failure("required tool capability is unavailable")
    store.archive_assignment(failed, executive_summary="blocked")

    result = finalize_rest_assignment(store, agent, assignment)

    daily_note = workspace / "memory" / f"{date_key}-MEMORY.md"
    content = daily_note.read_text(encoding="utf-8")
    assert result["daily_review"]["status"] == "recorded"
    assert "Completed daily deployment check" in content
    assert "Repair unavailable reporting tool" in content
    assert "propose the missing tool" in content
    # Finalization is idempotent; it must not write the nightly review twice.
    finalize_rest_assignment(store, agent, assignment)
    assert content == daily_note.read_text(encoding="utf-8")


def test_ponder_question_leaves_daily_review_to_the_agent(tmp_path):
    store, agent, workspace, assignment = _finalizer_fixture(tmp_path)
    (workspace / "PONDER.md").write_text("# Ponder\n\n- How should retries back off?\n")

    result = finalize_rest_assignment(store, agent, assignment)

    assert result["daily_review"] is None


# --- Ops Room projection ------------------------------------------------------------


def test_resting_agents_project_into_the_barracks(tmp_path):
    from brigade.services import _agent_room

    store = _store(tmp_path)
    result = evaluate_rest_schedule(store, now=IN_WINDOW)
    assignment = store.find_assignment(result["created"][0]["assignment_id"])

    # Stamped at creation, and kind=rest is a backstop even without room_id.
    assert assignment.room_id == "barracks"
    assignment.room_id = None
    room = _agent_room({"agent_id": "ada"}, "working", assignment)
    assert room["id"] == "barracks"
    assert room["domain"] == "dreaming"


# --- Workspace seeding --------------------------------------------------------------


def test_workspace_seeds_reflections_and_ponder_without_requiring_them(tmp_path):
    store = _store(tmp_path)
    agent = next(item for item in store.agents() if item.agent_id == "ada")

    workspace = ensure_agent_workspace(agent, store.data_dir)

    assert (workspace / "reflections.md").exists()
    assert (workspace / "PONDER.md").exists()
    assert "reflections.md" not in REQUIRED_AGENT_FILES
    assert "PONDER.md" not in REQUIRED_AGENT_FILES


# --- run_full_cycle wiring ----------------------------------------------------------


def test_full_cycle_schedules_and_dispatches_rest(tmp_path):
    store = _store(tmp_path)

    result = run_full_cycle(
        store,
        None,
        OrchestrationConfig(
            proactive_mode="off",
            rest_window_start_utc="00:00",
            rest_window_end_utc="23:59",
        ),
    )

    rest = result.sub_results["rest"]
    assert len(rest["created"]) == 1
    assignment = store.find_assignment(rest["created"][0]["assignment_id"])
    assert assignment.status == AssignmentStatus.ASSIGNED  # dispatched same cycle
    assert result.outcome.mode == "worked"
