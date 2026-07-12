"""Phase 4: intake triggers — pull-based document and inbound-message scan."""

from __future__ import annotations

from brigade.intake import (
    EVENT_INTAKE_CREATED,
    EVENT_INTAKE_PROPOSAL,
    evaluate_intake_queue,
    intake_idempotency_key,
)
from brigade.orchestrator import OrchestrationConfig, run_full_cycle
from brigade.schemas import Agent, ChatMessage, Goal, Mission, Team
from brigade.state import JsonStateStore


def _store(tmp_path) -> JsonStateStore:
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
        ),
    )
    return store


def _add_on_call_infra_chief(store: JsonStateStore) -> None:
    store.add_agent(Agent("ops", "OPS", "workspace-ops", role="crew_chief"))
    store.upsert_team(
        Team(team_id="infra", display_name="Infra", crew_chief_id="ops", members=[])
    )
    store.add_goal(
        "ops",
        Goal(
            statement="Maintain the kubernetes cluster infrastructure",
            success_criteria=[],
            explicitly_not=[],
            set_by="human",
            human_confirmed=True,
            engagement_mode="on_call",
        ),
    )


def _ingest(store: JsonStateStore, *, title: str, document_id: str, when: str) -> None:
    store.add_knowledge_document(
        {
            "document_id": document_id,
            "title": title,
            "source": "test-source",
            "document_type": "article",
            "ingested_at": when,
            "metadata": {"title": title},
        }
    )


def test_ingested_document_produces_intake_proposal_on_next_cycle(tmp_path):
    store = _store(tmp_path)
    _ingest(store, title="Release notes", document_id="doc-1", when="2026-06-12T00:00:00Z")

    # rest_enabled=False keeps the outcome intake-only even when the suite
    # runs inside the UTC rest window.
    result = run_full_cycle(
        store, None, OrchestrationConfig(proactive_mode="off", rest_enabled=False)
    )

    intake = result.sub_results["intake"]
    assert intake["mode"] == "propose"
    assert [item["source_id"] for item in intake["proposals"]] == ["doc-1"]
    event_types = [event["type"] for event in result.reasoning_record["events"]]
    assert EVENT_INTAKE_PROPOSAL in event_types
    assert result.outcome.reason == "intake_only_pending_approval"


def test_intake_duplicate_suppression_across_cycles(tmp_path):
    store = _store(tmp_path)
    _ingest(store, title="Release notes", document_id="doc-1", when="2026-06-12T00:00:00Z")
    config = OrchestrationConfig(proactive_mode="off")

    first = run_full_cycle(store, None, config)
    second = run_full_cycle(store, None, config)

    assert len(first.sub_results["intake"]["proposals"]) == 1
    assert second.sub_results["intake"]["proposals"] == []
    assert [item["source_id"] for item in second.sub_results["intake"]["duplicates"]] == [
        "doc-1"
    ]


def test_intake_routes_to_configured_chief_first(tmp_path):
    store = _store(tmp_path)
    _add_on_call_infra_chief(store)
    _ingest(
        store,
        title="kubernetes cluster upgrade",
        document_id="doc-1",
        when="2026-06-12T00:00:00Z",
    )

    result = evaluate_intake_queue(store, route_chief="sage")

    assert result["proposals"][0]["agent_id"] == "sage"


def test_intake_routes_by_goal_token_overlap_to_on_call_chief(tmp_path):
    store = _store(tmp_path)
    _add_on_call_infra_chief(store)
    _ingest(
        store,
        title="kubernetes cluster upgrade notes",
        document_id="doc-1",
        when="2026-06-12T00:00:00Z",
    )

    result = evaluate_intake_queue(store)

    # On-call chiefs activate here: the infra goal matches the item tokens.
    assert result["proposals"][0]["agent_id"] == "ops"


def test_intake_routes_by_member_specialty_overlap(tmp_path):
    store = _store(tmp_path)
    _add_on_call_infra_chief(store)
    _ingest(
        store,
        title="python packaging cleanup",
        document_id="doc-1",
        when="2026-06-12T00:00:00Z",
    )

    result = evaluate_intake_queue(store)

    # "python" matches ada's specialty; ada's chief is sage.
    assert result["proposals"][0]["agent_id"] == "sage"


def test_intake_falls_back_to_first_chief_without_overlap(tmp_path):
    store = _store(tmp_path)
    _ingest(
        store,
        title="zzz unrelatable gibberish",
        document_id="doc-1",
        when="2026-06-12T00:00:00Z",
    )

    result = evaluate_intake_queue(store)

    assert result["proposals"][0]["agent_id"] == "sage"


def test_intake_caps_items_per_cycle_oldest_first(tmp_path):
    store = _store(tmp_path)
    _ingest(store, title="third", document_id="doc-3", when="2026-06-12T03:00:00Z")
    _ingest(store, title="first", document_id="doc-1", when="2026-06-12T01:00:00Z")
    _ingest(store, title="second", document_id="doc-2", when="2026-06-12T02:00:00Z")

    result = evaluate_intake_queue(store, max_per_cycle=2)

    assert [item["source_id"] for item in result["proposals"]] == ["doc-1", "doc-2"]


def test_intake_create_mode_builds_chief_assignment_with_provenance(tmp_path):
    store = _store(tmp_path)
    _ingest(store, title="Release notes", document_id="doc-1", when="2026-06-12T00:00:00Z")

    result = run_full_cycle(
        store, None, OrchestrationConfig(proactive_mode="off", intake_mode="create")
    )

    intake = result.sub_results["intake"]
    assert len(intake["created"]) == 1
    assignment = store.find_assignment(intake["created"][0]["assignment_id"])
    assert assignment.assigned_to == "sage"
    assert assignment.idempotency_key == intake_idempotency_key(
        "knowledge_document", "doc-1"
    )
    assert "doc-1" in assignment.assignment_rationale
    assert "Review ingested item 'Release notes'" in assignment.assignment
    event_types = [event["type"] for event in result.reasoning_record["events"]]
    assert EVENT_INTAKE_CREATED in event_types
    assert result.outcome.mode == "worked"


def test_intake_scans_external_inbound_messages_only(tmp_path):
    store = _store(tmp_path)
    store.add_message(
        ChatMessage(
            channel="telegram",
            sender="telegram:42",
            recipient="sage",
            content="Please look into the failing nightly build",
            metadata={"kind": "external_inbound", "provider": "telegram"},
        )
    )
    store.add_message(
        ChatMessage(
            channel="web",
            sender="tm",
            recipient="sage",
            content="ordinary chat message",
        )
    )

    result = evaluate_intake_queue(store)

    assert len(result["proposals"]) == 1
    assert result["proposals"][0]["source_kind"] == "inbound_message"


def test_intake_mode_off_disables_scan(tmp_path):
    store = _store(tmp_path)
    _ingest(store, title="Release notes", document_id="doc-1", when="2026-06-12T00:00:00Z")

    result = evaluate_intake_queue(store, mode="off")

    assert result == {
        "mode": "off",
        "proposals": [],
        "created": [],
        "duplicates": [],
        "events": [],
    }


def test_intake_sub_result_lands_in_reasoning_record(tmp_path):
    store = _store(tmp_path)
    _ingest(store, title="Release notes", document_id="doc-1", when="2026-06-12T00:00:00Z")

    result = run_full_cycle(store, None, OrchestrationConfig(proactive_mode="off"))

    record_intake = result.reasoning_record["sub_results"]["intake"]
    assert record_intake["proposals"][0]["source_id"] == "doc-1"
    assert "events" not in record_intake
