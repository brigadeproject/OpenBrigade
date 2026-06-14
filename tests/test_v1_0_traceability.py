"""Phase 7: training-data export, chat status context, telemetry filters."""

from __future__ import annotations

import json

import pytest

from brigade.cli import main as cli_main
from brigade.export import export_training_data
from brigade.intake import EVENT_INTAKE_PROPOSAL
from brigade.orchestrator import (
    OrchestrationConfig,
    build_orchestration_telemetry,
    orchestration_event,
    record_orchestration_events,
    run_full_cycle,
)
from brigade.prompt_floors import build_chat_status_context
from brigade.schemas import (
    Agent,
    Assignment,
    AssignmentStatus,
    Goal,
    Mission,
    Team,
)
from brigade.services import _user_chat_prompt
from brigade.state import JsonStateStore


def _store(tmp_path) -> JsonStateStore:
    store = JsonStateStore(tmp_path / "state.json")
    store.set_mission(Mission("Run the prototype", [], []))
    store.add_agent(
        Agent("sage", "SAGE", "workspace-sage", role="crew_chief")
    )
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
        ),
    )
    return store


def _blocked(store, agent="ada") -> Assignment:
    assignment = Assignment(
        assignment="Fix the importer",
        assigned_to=agent,
        created_by="human",
        source="direct_command",
    )
    store.add_assignment(assignment)
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    assignment.register_failure("import explodes", blockers=["missing module"])
    store.update_assignment(assignment)
    return assignment


# --- Export -------------------------------------------------------------------------


def test_export_training_data_writes_parseable_jsonl(tmp_path):
    store = _store(tmp_path)
    _blocked(store)
    run_full_cycle(store, None, OrchestrationConfig(proactive_mode="off"))
    transcript_file = tmp_path / "transcript.json"
    transcript_file.write_text(json.dumps({"prompt": "p", "responses": ["r"]}))
    store.add_transcript(
        {
            "transcript_id": "t-1",
            "assignment_id": "a-1",
            "agent_id": "ada",
            "path": str(transcript_file),
            "created_at": "2026-06-12T00:00:00+00:00",
        }
    )
    out_dir = tmp_path / "export"

    manifest = export_training_data(store, out_dir=out_dir)

    for filename in (
        "cycles.jsonl",
        "assignments.jsonl",
        "transcripts.jsonl",
        "usage.jsonl",
        "episodes.jsonl",
        "proposals.jsonl",
    ):
        path = out_dir / filename
        assert path.exists()
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        assert manifest["counts"][filename] == len(rows)
    cycles = [
        json.loads(line) for line in (out_dir / "cycles.jsonl").read_text().splitlines()
    ]
    assert cycles
    assert all("cycle_outcome" in cycle for cycle in cycles)
    transcripts = [
        json.loads(line)
        for line in (out_dir / "transcripts.jsonl").read_text().splitlines()
    ]
    assert transcripts[0]["content"] == {"prompt": "p", "responses": ["r"]}
    saved_manifest = json.loads((out_dir / "manifest.json").read_text())
    assert saved_manifest["counts"] == manifest["counts"]
    assert saved_manifest["schema_versions"]["cycle_reasoning_record"] == 2


def test_export_since_filters_records(tmp_path):
    store = _store(tmp_path)
    store.add_transcript(
        {
            "transcript_id": "old",
            "path": None,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )
    store.add_transcript(
        {
            "transcript_id": "new",
            "path": None,
            "created_at": "2026-06-11T00:00:00+00:00",
        }
    )
    out_dir = tmp_path / "export"

    manifest = export_training_data(
        store, out_dir=out_dir, since="2026-06-01T00:00:00+00:00"
    )

    assert manifest["counts"]["transcripts.jsonl"] == 1
    rows = [
        json.loads(line)
        for line in (out_dir / "transcripts.jsonl").read_text().splitlines()
    ]
    assert rows[0]["transcript_id"] == "new"
    assert manifest["since"] == "2026-06-01T00:00:00+00:00"


def test_export_cli_requires_operator_permission(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli_main(["init", "mvp", "--mission", "Prototype mission"]) == 0
    capsys.readouterr()
    assert cli_main(["user", "add", "--username", "op", "--role", "operator"]) == 0
    assert cli_main(["user", "add", "--username", "obs", "--role", "observer"]) == 0
    capsys.readouterr()

    out_dir = tmp_path / "training"
    assert (
        cli_main(["--as-user", "op", "export", "training-data", "--out", str(out_dir)])
        == 0
    )
    manifest = json.loads(capsys.readouterr().out)
    assert (out_dir / "manifest.json").exists()
    assert manifest["counts"]["cycles.jsonl"] >= 0

    with pytest.raises(PermissionError, match="export:read"):
        cli_main(
            [
                "--as-user",
                "obs",
                "export",
                "training-data",
                "--out",
                str(tmp_path / "training2"),
            ]
        )


# --- Chat status context ------------------------------------------------------------


def test_chief_chat_context_contains_live_state(tmp_path):
    store = _store(tmp_path)
    blocked = _blocked(store)
    queued = Assignment(
        assignment="Write the docs",
        assigned_to="ada",
        created_by="human",
        source="direct_command",
    )
    store.add_assignment(queued)
    store.add_alert(f"assignment {blocked.assignment_id} needs attention")

    context = build_chat_status_context(store, "sage")

    assert context["role"] == "crew_chief"
    assert context["queue_depth"] == 1
    assert [item["statement"] for item in context["goals"]] == [
        "Deliver the prototype"
    ]
    assert context["goals"][0]["engagement_mode"] == "directive"
    member_ids = {item["agent"] for item in context["member_load"]}
    assert member_ids == {"sage", "ada"}
    ada_load = next(item for item in context["member_load"] if item["agent"] == "ada")
    assert ada_load["specialties"] == ["python"]
    assert context["blockers"][0]["assignment_id"] == blocked.assignment_id
    assert context["blockers"][0]["blockers"] == ["missing module"]
    assert context["awaiting_human"] == []
    assert any(blocked.assignment_id in alert for alert in context["team_alerts"])


def test_worker_chat_context_contains_own_state_only(tmp_path):
    store = _store(tmp_path)
    blocked = _blocked(store)

    context = build_chat_status_context(store, "ada")

    assert context["role"] == "line_worker"
    assert context["active_assignments"][0]["assignment_id"] == blocked.assignment_id
    assert "member_load" not in context
    assert "queue_depth" not in context


def test_user_chat_prompt_embeds_status_context(tmp_path):
    store = _store(tmp_path)
    blocked = _blocked(store)

    prompt = _user_chat_prompt("SAGE", "sage", "what's the team status?", None, store)

    assert "Live status context" in prompt
    assert blocked.assignment_id in prompt
    assert "missing module" in prompt
    assert "queue_depth" in prompt


# --- Telemetry filters ---------------------------------------------------------------


def test_telemetry_filters_include_new_event_kinds(tmp_path):
    store = _store(tmp_path)
    record_orchestration_events(
        store,
        source="orchestrator_intake",
        decision_summary="intake pass",
        events=[
            orchestration_event(
                EVENT_INTAKE_PROPOSAL,
                "Intake proposed work for a document.",
                source="orchestrator_intake",
                decision="proposed",
            ),
            orchestration_event(
                "ladder_retry",
                "Ladder retried a blocked assignment.",
                source="orchestrator_ladder",
                decision="retried",
            ),
            orchestration_event(
                "rest_completed",
                "Rest cycle completed.",
                source="orchestrator_rest",
                decision="completed",
            ),
            orchestration_event(
                "recurrence_materialized",
                "Recurrence materialized an assignment.",
                source="orchestrator_recurrence",
                decision="created",
            ),
        ],
    )

    telemetry = build_orchestration_telemetry(store.orchestrator_reasoning())

    types = {event["type"] for event in telemetry["events"]}
    assert {
        "intake_proposal",
        "ladder_retry",
        "rest_completed",
        "recurrence_materialized",
    } <= types
    proposal_types = {event["type"] for event in telemetry["proposals"]}
    assert "intake_proposal" in proposal_types
    assert telemetry["counts"]["ladder_retry"] == 1
