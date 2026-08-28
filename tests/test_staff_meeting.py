from __future__ import annotations

import json
import math

import pytest

from brigade.chief_chat import ChatToolContext, chief_query_registry, resolve_persona
from brigade.schemas import (
    AGENT_ROLE_CREW_CHIEF,
    Agent,
    Assignment,
    Team,
)
from brigade.staff_meeting import (
    MeetingStatus,
    accept_veto_mitigation,
    advance_staff_meetings,
    amend_packet,
    append_meeting_record,
    approve_freeze_and_dispatch,
    approve_roster,
    create_staff_meeting,
    evaluate_vote,
    freeze_packet,
    meeting_packet,
    meeting_public_view,
    propose_roster,
    quorum_required,
    redact_sensitive_data,
    replace_veto_reviewer,
    replay_staff_meeting_state,
)
from brigade.state import JsonStateStore
from brigade.tools import ToolContext, default_tool_registry


def _agents() -> list[Agent]:
    return [
        Agent(
            agent_id="chief",
            display_name="Chief",
            workspace_path="workspace-chief",
            role=AGENT_ROLE_CREW_CHIEF,
            specialties=["planning", "architecture"],
        ),
        Agent(
            agent_id="builder",
            display_name="Builder",
            workspace_path="workspace-builder",
            specialties=["code", "implementation", "security"],
        ),
        Agent(
            agent_id="reviewer",
            display_name="Reviewer",
            workspace_path="workspace-reviewer",
            specialties=["risk", "operations", "research"],
        ),
    ]


def test_quorum_is_seventy_percent_rounded_up():
    assert quorum_required(5) == 4
    assert quorum_required(6) == 5
    assert quorum_required(7) == 5
    assert quorum_required(12) == 9


def test_vote_requires_quorum_and_three_substantive_agents():
    ballots = [
        {"vote": "support", "agent_id": "a"},
        {"vote": "support", "agent_id": "b"},
        {"vote": "support", "agent_id": "c"},
        {"vote": "abstain", "agent_id": "d"},
    ]

    result = evaluate_vote(ballots, seat_count=5)

    assert result["quorum"] is True
    assert result["consensus_support"] is True
    assert result["outcome"] == MeetingStatus.COMPLETED_WITH_DISSENT.value


def test_vote_does_not_treat_failed_support_threshold_as_oppose_consensus():
    ballots = [
        {"vote": "support", "agent_id": "a"},
        {"vote": "support", "agent_id": "b"},
        {"vote": "support", "agent_id": "c"},
        {"vote": "oppose", "agent_id": "d"},
        {"vote": "oppose", "agent_id": "e"},
    ]

    result = evaluate_vote(ballots, seat_count=5)

    assert result["required_supermajority"] == 4
    assert result["consensus_support"] is False
    assert result["consensus_oppose"] is False
    assert result["outcome"] == MeetingStatus.MOTION_NOT_ADOPTED.value


def test_vote_is_blocked_by_unresolved_veto():
    ballots = [
        {"vote": "support", "agent_id": agent_id}
        for agent_id in ("a", "b", "c", "d")
    ]

    result = evaluate_vote(
        ballots,
        seat_count=5,
        unresolved_vetoes=[{"veto_id": "v1", "status": "issued"}],
    )

    assert result["outcome"] == MeetingStatus.BLOCKED_BY_VETO.value
    assert result["unresolved_veto_count"] == 1


def test_four_responses_can_reach_consensus_but_three_fail_quorum():
    four = [
        {"vote": "support", "agent_id": "a"},
        {"vote": "support", "agent_id": "b"},
        {"vote": "support", "agent_id": "c"},
        {"vote": "oppose", "agent_id": "d"},
    ]
    three = four[:3]

    assert evaluate_vote(four, seat_count=5)["consensus_support"] is True
    assert evaluate_vote(three, seat_count=5)["outcome"] == "FAILED_NO_QUORUM"


def test_two_two_formal_vote_is_motion_not_adopted():
    result = evaluate_vote(
        [
            {"vote": "support", "agent_id": "a"},
            {"vote": "support", "agent_id": "b"},
            {"vote": "oppose", "agent_id": "c"},
            {"vote": "oppose", "agent_id": "d"},
        ],
        seat_count=5,
    )

    assert result["outcome"] == MeetingStatus.MOTION_NOT_ADOPTED.value
    assert result["consensus_support"] is False
    assert result["consensus_oppose"] is False


def test_redaction_happens_before_persistence_and_hashing(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agents = _agents()
    for agent in agents:
        store.add_agent(agent)

    meeting = create_staff_meeting(
        store,
        original_request="Compare vendors with api_key=super-secret-value and Bearer abcdef12345",
        acceptance_criteria=["Do not expose password=hunter2"],
        chair_agent_id="chief",
        owner_username="owner",
        caller_kind="crew_chief",
        eligible_agents=agents,
        idempotency_key="meeting:one",
    )

    encoded_state = (tmp_path / "state.json").read_text(encoding="utf-8")
    assert "super-secret-value" not in encoded_state
    assert "abcdef12345" not in encoded_state
    assert "hunter2" not in encoded_state
    assert "***redacted***" in meeting["original_request"]
    assert len(meeting["original_request_hash"]) == 64


def test_roster_is_fixed_catalog_and_has_three_distinct_agents():
    roster, _warnings = propose_roster(
        "Design and securely operate a new API",
        chair_agent_id="chief",
        eligible_agents=_agents(),
        seat_count=5,
    )

    assert len(roster) == 5
    assert roster[0]["role_key"] == "chair"
    assert roster[0]["agent_id"] == "chief"
    assert len({item["agent_id"] for item in roster}) >= 3
    assert len({item["role_key"] for item in roster}) == 5
    veto_agents = [item["agent_id"] for item in roster if item["veto_domain"]]
    assert len(veto_agents) == len(set(veto_agents))


def test_create_approve_and_freeze_packet_is_durable_and_idempotent(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agents = _agents()
    for agent in agents:
        store.add_agent(agent)
    first = create_staff_meeting(
        store,
        original_request="Choose an implementation plan",
        acceptance_criteria=["The plan is deployable", "Dissent remains visible"],
        chair_agent_id="chief",
        owner_username="owner",
        caller_kind="crew_chief",
        eligible_agents=agents,
        idempotency_key="meeting:same",
    )
    duplicate = create_staff_meeting(
        store,
        original_request="Choose an implementation plan",
        acceptance_criteria=["The plan is deployable", "Dissent remains visible"],
        chair_agent_id="chief",
        owner_username="owner",
        caller_kind="crew_chief",
        eligible_agents=agents,
        idempotency_key="meeting:same",
    )

    assert duplicate["meeting_id"] == first["meeting_id"]
    approved = approve_roster(store, first["meeting_id"], actor_id="chief")
    frozen = freeze_packet(store, first["meeting_id"], actor_id="chief")

    assert approved["status"] == MeetingStatus.ROSTER_APPROVED.value
    assert frozen["status"] == MeetingStatus.PACKET_FROZEN.value
    assert meeting_packet(store, first["meeting_id"])["packet_version"] == 1
    assert len(store.staff_meetings()) == 1
    assert len(store.staff_meeting_records(first["meeting_id"], record_kind="role_seat")) == 5


def test_roster_rejects_less_than_three_agents():
    agents = _agents()[:2]

    with pytest.raises(ValueError, match="three distinct agents"):
        propose_roster(
            "Review this plan",
            chair_agent_id="chief",
            eligible_agents=agents,
        )


def test_roster_edits_are_limited_to_catalog_roles_and_eligible_agents(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agents = _agents()
    outsider = Agent(
        agent_id="outsider",
        display_name="Outsider",
        workspace_path="workspace-outsider",
    )
    for agent in [*agents, outsider]:
        store.add_agent(agent)
    meeting = create_staff_meeting(
        store,
        original_request="Review",
        acceptance_criteria=["Complete"],
        chair_agent_id="chief",
        owner_username="owner",
        caller_kind="crew_chief",
        eligible_agents=agents,
    )
    ineligible = [dict(item) for item in meeting["proposed_roster"]]
    ineligible[1]["agent_id"] = "outsider"
    with pytest.raises(ValueError, match="ineligible agent"):
        approve_roster(
            store,
            meeting["meeting_id"],
            actor_id="chief",
            roster=ineligible,
        )

    forged = [dict(item) for item in meeting["proposed_roster"]]
    forged[1]["lens"] = "caller-defined lens"
    forged[1]["role_family"] = "caller-defined family"
    forged[1]["veto_domain"] = "security"
    approved = approve_roster(
        store,
        meeting["meeting_id"],
        actor_id="chief",
        roster=forged,
    )

    approved_seat = approved["approved_roster"][1]
    catalog_seat = meeting["proposed_roster"][1]
    assert approved_seat["lens"] == catalog_seat["lens"]
    assert approved_seat["role_family"] == catalog_seat["role_family"]
    assert approved_seat["veto_domain"] == catalog_seat["veto_domain"]


def test_quorum_matches_ceil_formula_for_all_supported_panel_sizes():
    for seat_count in range(5, 13):
        assert quorum_required(seat_count) == math.ceil(seat_count * 0.70)


def test_nested_sensitive_values_are_redacted():
    assert redact_sensitive_data(
        {"headers": {"Authorization": "Bearer x"}, "access_token": "raw"}
    ) == {
        "headers": {"Authorization": "Bearer ***redacted***"},
        "access_token": "***redacted***",
    }


def _complete_active_wave(
    store: JsonStateStore,
    meeting_id: str,
    payload_for,
) -> None:
    meeting = store.find_staff_meeting(meeting_id)
    assert meeting is not None
    wave_id = meeting["active_wave_id"]
    records = store.staff_meeting_records(meeting_id, record_kind="assignment")
    wave = [item for item in records if item["payload"]["wave_id"] == wave_id]
    for index, record in enumerate(wave):
        assignment = store.find_assignment(record["assignment_id"])
        assert assignment is not None
        assignment.transition_to(assignment.status.ASSIGNED)
        assignment.mark_complete(json.dumps(payload_for(record, index), sort_keys=True))
        store.update_assignment(assignment)
        store.archive_assignment(assignment, assignment.progress_summary or "")


def test_harness_advances_reviews_deliberation_vote_and_report(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agents = _agents()
    for agent in agents:
        store.add_agent(agent)
    meeting = create_staff_meeting(
        store,
        original_request="Choose a deployable implementation plan",
        acceptance_criteria=["Deployable", "Auditable"],
        chair_agent_id="chief",
        owner_username="owner",
        caller_kind="crew_chief",
        eligible_agents=agents,
    )
    approve_freeze_and_dispatch(store, meeting["meeting_id"], actor_id="chief")
    _complete_active_wave(
        store,
        meeting["meeting_id"],
        lambda record, index: {
            "disposition": "support",
            "summary": f"review {index}",
            "findings": [f"finding {index}"],
            "risks": [],
            "assumptions": [],
            "evidence_refs": [],
            "proposed_changes": [],
            "open_questions": [],
            "veto": None,
            "confidence": 0.8,
        },
    )
    assert advance_staff_meetings(store)["advanced"] == [meeting["meeting_id"]]
    assert store.find_staff_meeting(meeting["meeting_id"])["status"] == "SYNTHESIS"

    _complete_active_wave(
        store,
        meeting["meeting_id"],
        lambda record, index: {
            "areas_of_agreement": ["Use the plan"],
            "material_disagreements": [],
            "unique_findings": [],
            "conflicting_assumptions": [],
            "evidence_gaps": [],
            "proposed_combined_solution": "Implement the plan",
            "rejected_alternatives": [],
            "unresolved_questions": [],
            "acceptance_criteria_evaluation": [],
            "next_action": "deliberate",
            "follow_up_roles": [],
            "follow_up_questions": [],
        },
    )
    advance_staff_meetings(store)
    assert store.find_staff_meeting(meeting["meeting_id"])["status"] == "DELIBERATION"

    _complete_active_wave(
        store,
        meeting["meeting_id"],
        lambda record, index: {
            "inaccurate_or_omitted_claims": [],
            "assessment_changes": [],
            "remaining_objections": [],
            "new_contradictions": [],
            "requested_revisions": [],
            "acceptance_criteria_assessment": [],
            "evidence_refs": [],
            "veto": None,
            "confidence": 0.9,
        },
    )
    advance_staff_meetings(store)
    assert store.find_staff_meeting(meeting["meeting_id"])["status"] == "SYNTHESIS"

    _complete_active_wave(
        store,
        meeting["meeting_id"],
        lambda record, index: {
            "areas_of_agreement": ["Use the plan"],
            "material_disagreements": [],
            "unique_findings": [],
            "conflicting_assumptions": [],
            "evidence_gaps": [],
            "proposed_combined_solution": "Implement the plan",
            "rejected_alternatives": [],
            "unresolved_questions": [],
            "acceptance_criteria_evaluation": [],
            "next_action": "vote",
            "follow_up_roles": [],
            "follow_up_questions": [],
        },
    )
    advance_staff_meetings(store)
    assert store.find_staff_meeting(meeting["meeting_id"])["status"] == "FINAL_VOTE"

    _complete_active_wave(
        store,
        meeting["meeting_id"],
        lambda record, index: {
            "vote": "support" if index < 4 else "oppose",
            "rationale": "reason",
            "conditions": [],
            "dissent": ["minority"] if index == 4 else [],
            "confidence": 0.9,
            "veto_id": None,
        },
    )
    advance_staff_meetings(store)
    current = store.find_staff_meeting(meeting["meeting_id"])
    assert current["status"] == "FINAL_REPORT"
    assert current["ballots_revealed"] is True

    _complete_active_wave(
        store,
        meeting["meeting_id"],
        lambda record, index: {
            "report_markdown": "# Decision\n\nImplement the plan with recorded dissent.",
            "acceptance_criteria_evaluation": [],
        },
    )
    advance_staff_meetings(store)
    completed = store.find_staff_meeting(meeting["meeting_id"])
    assert completed["status"] == MeetingStatus.COMPLETED_WITH_DISSENT.value
    assert completed["final_report_record_id"]
    assert store.episodes()[0]["meeting_id"] == meeting["meeting_id"]
    report = next(
        item
        for item in store.staff_meeting_records(meeting["meeting_id"])
        if item["record_kind"] == "report"
    )["payload"]["report_markdown"]
    assert "## 5. Consensus status and vote arithmetic" in report
    assert "duplicated_underlying_agents" in report
    replay = replay_staff_meeting_state(
        completed,
        store.staff_meeting_records(meeting["meeting_id"]),
    )
    assert replay["status"] == completed["status"]
    assert replay["packet_version"] == completed["packet_version"]
    assert replay["current_round"] == completed["current_round"]
    assert replay["vote_result"] == completed["vote_result"]


def test_ballots_are_hidden_from_public_view_until_revealed(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agents = _agents()
    for agent in agents:
        store.add_agent(agent)
    meeting = create_staff_meeting(
        store,
        original_request="Decide",
        acceptance_criteria=["Decide audibly"],
        chair_agent_id="chief",
        owner_username="owner",
        caller_kind="crew_chief",
        eligible_agents=agents,
    )
    meeting["ballots_revealed"] = False
    store.upsert_staff_meeting(meeting)
    append_meeting_record(
        store,
        meeting,
        "ballot",
        {"vote": "support"},
        idempotency_key="test-ballot",
        role_seat_id=meeting["proposed_roster"][0]["role_seat_id"],
    )

    hidden = meeting_public_view(store, meeting, include_records=True)
    assert all(item["record_kind"] != "ballot" for item in hidden["records"])

    meeting["ballots_revealed"] = True
    visible = meeting_public_view(store, meeting, include_records=True)
    assert any(item["record_kind"] == "ballot" for item in visible["records"])


def test_crew_chief_chat_tool_convenes_and_alerts(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agents = _agents()
    for agent in agents:
        store.add_agent(agent)
    store.upsert_team(
        Team(
            team_id="alpha",
            display_name="Alpha",
            crew_chief_id="chief",
            members=[item.agent_id for item in agents],
        )
    )
    persona = resolve_persona(store, "chief")
    context = ChatToolContext(
        store=store,
        persona=persona,
        operator="owner",
        request_message_id="request-1",
    )

    result = chief_query_registry(include_web_fetch=False).execute(
        "convene_staff_meeting",
        context,
        {
            "request": "Choose a deployment architecture",
            "acceptance_criteria": ["Secure", "Recoverable"],
        },
    )

    assert result.ok is True
    meeting = store.find_staff_meeting(result.metadata["meeting_id"])
    assert meeting["status"] == MeetingStatus.INDEPENDENT_REVIEW.value
    assert "convened Staff Meeting" in store.alerts()[-1]


def test_ordinary_agent_can_only_request_staff_meeting(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agents = _agents()
    for agent in agents:
        store.add_agent(agent)
    store.upsert_team(
        Team(
            team_id="alpha",
            display_name="Alpha",
            crew_chief_id="chief",
            members=[item.agent_id for item in agents],
        )
    )
    assignment = Assignment(
        assignment="Investigate architecture",
        assigned_to="builder",
        created_by="chief",
        source="test",
    )
    store.add_assignment(assignment)
    context = ToolContext(agent=agents[1], assignment=assignment, store=store)

    result = default_tool_registry().execute(
        "request_staff_meeting",
        context,
        {"request": "Architecture is unresolved", "reason": "Multiple domains conflict"},
    )

    assert result.ok is True
    assert store.staff_meetings() == []
    assert store.proposals(kind="staff_meeting_request")[0]["details"][
        "requested_chief_agent_id"
    ] == "chief"


def test_executive_registry_exposes_staff_meeting_tool():
    from brigade.executive import executive_query_registry

    names = {item.name for item in executive_query_registry(include_web_fetch=False).specs()}
    assert "convene_staff_meeting" in names


def test_staff_meeting_web_routes_are_registered(tmp_path):
    pytest.importorskip("fastapi")

    from brigade.config import Settings
    from brigade.web import create_app

    store = JsonStateStore(tmp_path / "state.json")
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        allow_json_store=True,
    )
    app = create_app(settings, store)
    paths = {route.path for route in app.routes}

    assert "/api/staff-meetings" in paths
    assert "/api/staff-meetings/{meeting_id}" in paths
    assert "/api/staff-meetings/{meeting_id}/events" in paths


def test_blocked_seat_is_absent_and_meeting_continues_with_quorum(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agents = _agents()
    for agent in agents:
        store.add_agent(agent)
    meeting = create_staff_meeting(
        store,
        original_request="Review",
        acceptance_criteria=["Complete"],
        chair_agent_id="chief",
        owner_username="owner",
        caller_kind="crew_chief",
        eligible_agents=agents,
    )
    approve_freeze_and_dispatch(store, meeting["meeting_id"], actor_id="chief")
    current = store.find_staff_meeting(meeting["meeting_id"])
    records = [
        item
        for item in store.staff_meeting_records(meeting["meeting_id"], record_kind="assignment")
        if item["payload"]["wave_id"] == current["active_wave_id"]
    ]
    for index, record in enumerate(records):
        assignment = store.find_assignment(record["assignment_id"])
        assert assignment is not None
        assignment.transition_to(assignment.status.ASSIGNED)
        if index == len(records) - 1:
            assignment.register_failure("provider unavailable", blockers=["provider unavailable"])
            store.update_assignment(assignment)
            continue
        payload = {
            "disposition": "support",
            "summary": "review",
            "findings": [],
            "risks": [],
            "assumptions": [],
            "evidence_refs": [],
            "proposed_changes": [],
            "open_questions": [],
            "veto": None,
            "confidence": 0.8,
        }
        assignment.mark_complete(json.dumps(payload))
        store.update_assignment(assignment)
        store.archive_assignment(assignment, assignment.progress_summary or "")

    advance_staff_meetings(store)

    assert store.find_staff_meeting(meeting["meeting_id"])["status"] == "SYNTHESIS"
    assert len(
        store.staff_meeting_records(meeting["meeting_id"], record_kind="absence")
    ) == 1


def test_evidence_amendment_is_single_use_and_adds_one_discussion(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agents = _agents()
    for agent in agents:
        store.add_agent(agent)
    meeting = create_staff_meeting(
        store,
        original_request="Review",
        acceptance_criteria=["Complete"],
        chair_agent_id="chief",
        owner_username="owner",
        caller_kind="crew_chief",
        eligible_agents=agents,
    )
    approve_freeze_and_dispatch(store, meeting["meeting_id"], actor_id="chief")

    amended = amend_packet(
        store,
        meeting["meeting_id"],
        actor_id="chief",
        evidence_refs=["document:new"],
    )

    assert amended["packet_version"] == 2
    assert amended["status"] == MeetingStatus.DELIBERATION.value
    assert "document:new" in amended["evidence_refs"]
    with pytest.raises(ValueError, match="only once"):
        amend_packet(
            store,
            meeting["meeting_id"],
            actor_id="chief",
            evidence_refs=["document:another"],
        )


def test_replacement_veto_reviewer_restarts_fresh_review(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agents = _agents()
    replacement = Agent(
        agent_id="replacement",
        display_name="Replacement",
        workspace_path="workspace-replacement",
        specialties=["security"],
    )
    for agent in [*agents, replacement]:
        store.add_agent(agent)
    meeting = create_staff_meeting(
        store,
        original_request="Review security and safety",
        acceptance_criteria=["Secure"],
        chair_agent_id="chief",
        owner_username="owner",
        caller_kind="crew_chief",
        eligible_agents=agents,
        seat_count=6,
    )
    approve_freeze_and_dispatch(store, meeting["meeting_id"], actor_id="chief")
    veto_seat = next(
        seat for seat in meeting["proposed_roster"] if seat["veto_domain"]
    )
    veto = append_meeting_record(
        store,
        store.find_staff_meeting(meeting["meeting_id"]),
        "veto",
        {
            "veto_id": "veto-1",
            "veto_domain": veto_seat["veto_domain"],
            "status": "issued",
            "risk_statement": "risk",
            "affected_criterion": "Secure",
            "severity": "high",
            "likelihood": "possible",
            "required_mitigation": "mitigate",
            "validation_method": "review",
            "issued_by_agent_id": veto_seat["agent_id"],
            "issued_by_role_seat_id": veto_seat["role_seat_id"],
        },
        idempotency_key="veto-1",
        role_seat_id=veto_seat["role_seat_id"],
        role_key=veto_seat["role_key"],
        agent_id=veto_seat["agent_id"],
    )

    restarted = replace_veto_reviewer(
        store,
        meeting["meeting_id"],
        actor_id="chief",
        veto_id=veto["payload"]["veto_id"],
        replacement_agent=replacement,
    )

    assert restarted["status"] == MeetingStatus.INDEPENDENT_REVIEW.value
    new_seat = next(
        seat
        for seat in restarted["approved_roster"]
        if seat["role_seat_id"] == veto_seat["role_seat_id"]
    )
    assert new_seat["agent_id"] == "replacement"


def test_abstentions_cannot_bypass_substantive_vote_floor():
    result = evaluate_vote(
        [
            {"vote": "support", "agent_id": "a"},
            {"vote": "support", "agent_id": "b"},
            {"vote": "abstain", "agent_id": "c"},
            {"vote": "abstain", "agent_id": "d"},
        ],
        seat_count=5,
    )

    assert result["quorum"] is True
    assert result["substantive_votes"] == 2
    assert result["consensus_support"] is False
    assert result["outcome"] == MeetingStatus.COMPLETED_NO_CONSENSUS.value


def test_two_absent_seats_fail_quorum(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agents = _agents()
    for agent in agents:
        store.add_agent(agent)
    meeting = create_staff_meeting(
        store,
        original_request="Review",
        acceptance_criteria=["Complete"],
        chair_agent_id="chief",
        owner_username="owner",
        caller_kind="crew_chief",
        eligible_agents=agents,
    )
    approve_freeze_and_dispatch(store, meeting["meeting_id"], actor_id="chief")
    current = store.find_staff_meeting(meeting["meeting_id"])
    records = [
        item
        for item in store.staff_meeting_records(meeting["meeting_id"], record_kind="assignment")
        if item["payload"]["wave_id"] == current["active_wave_id"]
    ]
    for index, record in enumerate(records):
        assignment = store.find_assignment(record["assignment_id"])
        assert assignment is not None
        assignment.transition_to(assignment.status.ASSIGNED)
        if index >= 3:
            assignment.register_failure("timed out", blockers=["timed out"])
            store.update_assignment(assignment)
            continue
        assignment.mark_complete(
            json.dumps(
                {
                    "disposition": "support",
                    "summary": "review",
                    "findings": [],
                    "risks": [],
                    "assumptions": [],
                    "evidence_refs": [],
                    "proposed_changes": [],
                    "open_questions": [],
                    "veto": None,
                    "confidence": 0.8,
                }
            )
        )
        store.update_assignment(assignment)
        store.archive_assignment(assignment, assignment.progress_summary or "")

    advance_staff_meetings(store)

    assert (
        store.find_staff_meeting(meeting["meeting_id"])["status"]
        == MeetingStatus.FAILED_NO_QUORUM.value
    )


def test_duplicate_dispatch_and_record_keys_do_not_duplicate_work(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agents = _agents()
    for agent in agents:
        store.add_agent(agent)
    meeting = create_staff_meeting(
        store,
        original_request="Review",
        acceptance_criteria=["Complete"],
        chair_agent_id="chief",
        owner_username="owner",
        caller_kind="crew_chief",
        eligible_agents=agents,
    )
    approve_freeze_and_dispatch(store, meeting["meeting_id"], actor_id="chief")
    first_assignments = store.staff_meeting_records(
        meeting["meeting_id"], record_kind="assignment"
    )

    approve_freeze_and_dispatch(store, meeting["meeting_id"], actor_id="chief")
    current = store.find_staff_meeting(meeting["meeting_id"])
    first = append_meeting_record(
        store,
        current,
        "ballot",
        {"vote": "support"},
        idempotency_key="same-ballot",
        role_seat_id=current["approved_roster"][0]["role_seat_id"],
    )
    duplicate = append_meeting_record(
        store,
        current,
        "ballot",
        {"vote": "oppose"},
        idempotency_key="same-ballot",
        role_seat_id=current["approved_roster"][0]["role_seat_id"],
    )

    assert len(
        store.staff_meeting_records(meeting["meeting_id"], record_kind="assignment")
    ) == len(first_assignments)
    assert duplicate["record_id"] == first["record_id"]
    assert len(store.staff_meeting_records(meeting["meeting_id"], record_kind="ballot")) == 1
    assert any(
        item["payload"].get("event_type") == "idempotency_replay"
        for item in store.staff_meeting_records(meeting["meeting_id"], record_kind="event")
    )


def test_veto_clears_only_after_issuing_reviewer_accepts_mitigation(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agents = _agents()
    for agent in agents:
        store.add_agent(agent)
    meeting = create_staff_meeting(
        store,
        original_request="Review security",
        acceptance_criteria=["Secure"],
        chair_agent_id="chief",
        owner_username="owner",
        caller_kind="crew_chief",
        eligible_agents=agents,
    )
    approved = approve_roster(store, meeting["meeting_id"], actor_id="chief")
    veto_seat = next(item for item in approved["approved_roster"] if item["veto_domain"])
    veto = append_meeting_record(
        store,
        approved,
        "veto",
        {
            "veto_id": "security-veto",
            "veto_domain": veto_seat["veto_domain"],
            "status": "issued",
            "risk_statement": "credential exposure",
            "affected_criterion": "Secure",
            "severity": "high",
            "likelihood": "possible",
            "required_mitigation": "isolate credentials",
            "validation_method": "security review",
            "issued_by_agent_id": veto_seat["agent_id"],
            "issued_by_role_seat_id": veto_seat["role_seat_id"],
        },
        idempotency_key="security-veto",
        role_seat_id=veto_seat["role_seat_id"],
        role_key=veto_seat["role_key"],
        agent_id=veto_seat["agent_id"],
    )
    with pytest.raises(PermissionError, match="issuing or replacement"):
        accept_veto_mitigation(
            store,
            meeting["meeting_id"],
            veto_id="security-veto",
            actor_id="chief",
            mitigation="isolated",
            validation_evidence=["test:passed"],
            idempotency_key="clear-veto",
        )

    cleared = accept_veto_mitigation(
        store,
        meeting["meeting_id"],
        veto_id="security-veto",
        actor_id=veto_seat["agent_id"],
        mitigation="credentials are isolated",
        validation_evidence=["test:passed"],
        idempotency_key="clear-veto",
    )

    assert veto["payload"]["status"] == "issued"
    assert cleared["payload"]["status"] == "withdrawn"
    assert store.find_staff_meeting(meeting["meeting_id"])["veto_status"] == "clear"


def test_chair_synthesis_cannot_omit_mandatory_dissent_excerpt(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agents = _agents()
    for agent in agents:
        store.add_agent(agent)
    meeting = create_staff_meeting(
        store,
        original_request="Choose a secure plan",
        acceptance_criteria=["Secure"],
        chair_agent_id="chief",
        owner_username="owner",
        caller_kind="crew_chief",
        eligible_agents=agents,
    )
    approve_freeze_and_dispatch(store, meeting["meeting_id"], actor_id="chief")
    _complete_active_wave(
        store,
        meeting["meeting_id"],
        lambda record, index: {
            "disposition": "oppose" if index == 4 else "support",
            "summary": "Unresolved credential exposure" if index == 4 else "acceptable",
            "findings": [],
            "risks": ["Secrets may leak"] if index == 4 else [],
            "assumptions": [],
            "evidence_refs": [],
            "proposed_changes": [],
            "open_questions": [],
            "veto": None,
            "confidence": 0.8,
        },
    )
    advance_staff_meetings(store)
    _complete_active_wave(
        store,
        meeting["meeting_id"],
        lambda record, index: {
            "areas_of_agreement": ["Proceed"],
            "material_disagreements": [],
            "unique_findings": [],
            "conflicting_assumptions": [],
            "evidence_gaps": [],
            "proposed_combined_solution": "Proceed without mentioning dissent",
            "rejected_alternatives": [],
            "unresolved_questions": [],
            "acceptance_criteria_evaluation": [],
            "next_action": "deliberate",
            "follow_up_roles": [],
            "follow_up_questions": [],
        },
    )

    advance_staff_meetings(store)

    excerpts = store.staff_meeting_records(meeting["meeting_id"], record_kind="excerpt")
    assert any(
        item["payload"]["mandatory"]
        and item["payload"]["text"] == "Unresolved credential exposure"
        for item in excerpts
    )
    assert any(item["payload"]["text"] == "Secrets may leak" for item in excerpts)


def test_elapsed_limit_produces_non_consensual_report(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agents = _agents()
    for agent in agents:
        store.add_agent(agent)
    meeting = create_staff_meeting(
        store,
        original_request="Review",
        acceptance_criteria=["Complete"],
        chair_agent_id="chief",
        owner_username="owner",
        caller_kind="crew_chief",
        eligible_agents=agents,
        resource_limits={"max_elapsed_seconds": 1},
    )
    approve_freeze_and_dispatch(store, meeting["meeting_id"], actor_id="chief")
    current = store.find_staff_meeting(meeting["meeting_id"])
    current["created_at"] = "2000-01-01T00:00:00+00:00"
    store.upsert_staff_meeting(current)

    advance_staff_meetings(store)

    completed = store.find_staff_meeting(meeting["meeting_id"])
    assert completed["status"] == MeetingStatus.RESOURCE_LIMIT_REACHED.value
    report = store.staff_meeting_records(meeting["meeting_id"], record_kind="report")[0]
    assert report["payload"]["consensual"] is False
    assert "non-consensual" in report["payload"]["report_markdown"]


def test_one_targeted_follow_up_discussion_is_capped_at_three_rounds(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agents = _agents()
    for agent in agents:
        store.add_agent(agent)
    meeting = create_staff_meeting(
        store,
        original_request="Resolve a cross-domain deployment risk",
        acceptance_criteria=["Secure", "Operable"],
        chair_agent_id="chief",
        owner_username="owner",
        caller_kind="crew_chief",
        eligible_agents=agents,
    )
    approve_freeze_and_dispatch(store, meeting["meeting_id"], actor_id="chief")
    _complete_active_wave(
        store,
        meeting["meeting_id"],
        lambda record, index: {
            "disposition": "revise",
            "summary": f"review {index}",
            "findings": [],
            "risks": [],
            "assumptions": [],
            "evidence_refs": [],
            "proposed_changes": [],
            "open_questions": [],
            "veto": None,
            "confidence": 0.7,
        },
    )
    advance_staff_meetings(store)
    selected_role = next(
        item["role_key"]
        for item in meeting["proposed_roster"]
        if item["role_key"] != "chair"
    )
    _complete_active_wave(
        store,
        meeting["meeting_id"],
        lambda record, index: {
            "areas_of_agreement": [],
            "material_disagreements": ["deployment risk"],
            "unique_findings": [],
            "conflicting_assumptions": [],
            "evidence_gaps": [],
            "proposed_combined_solution": "Investigate the risk",
            "rejected_alternatives": [],
            "unresolved_questions": ["Is the risk controlled?"],
            "acceptance_criteria_evaluation": [],
            "next_action": "deliberate",
            "follow_up_roles": [selected_role],
            "follow_up_questions": ["Validate the deployment risk"],
        },
    )
    advance_staff_meetings(store)
    assert store.find_staff_meeting(meeting["meeting_id"])["status"] == "DELIBERATION"
    _complete_active_wave(
        store,
        meeting["meeting_id"],
        lambda record, index: {
            "inaccurate_or_omitted_claims": [],
            "assessment_changes": [],
            "remaining_objections": ["deployment risk"],
            "new_contradictions": [],
            "requested_revisions": [],
            "acceptance_criteria_assessment": [],
            "evidence_refs": [],
            "veto": None,
            "confidence": 0.7,
        },
    )
    advance_staff_meetings(store)
    _complete_active_wave(
        store,
        meeting["meeting_id"],
        lambda record, index: {
            "areas_of_agreement": [],
            "material_disagreements": ["deployment risk"],
            "unique_findings": [],
            "conflicting_assumptions": [],
            "evidence_gaps": [],
            "proposed_combined_solution": "Investigate the risk",
            "rejected_alternatives": [],
            "unresolved_questions": ["Is the risk controlled?"],
            "acceptance_criteria_evaluation": [],
            "next_action": "targeted_follow_up",
            "follow_up_roles": [selected_role],
            "follow_up_questions": ["Validate the deployment risk"],
        },
    )
    advance_staff_meetings(store)
    assert (
        store.find_staff_meeting(meeting["meeting_id"])["status"]
        == MeetingStatus.TARGETED_FOLLOW_UP.value
    )

    for round_number in range(1, 4):
        _complete_active_wave(
            store,
            meeting["meeting_id"],
            lambda record, index, round_number=round_number: {
                "inaccurate_or_omitted_claims": [],
                "assessment_changes": [],
                "remaining_objections": ["needs validation"],
                "new_contradictions": [],
                "requested_revisions": [],
                "acceptance_criteria_assessment": [],
                "evidence_refs": [f"follow-up:{round_number}"],
                "veto": None,
                "confidence": 0.8,
            },
        )
        advance_staff_meetings(store)
        assert store.find_staff_meeting(meeting["meeting_id"])["status"] == "SYNTHESIS"
        _complete_active_wave(
            store,
            meeting["meeting_id"],
            lambda record, index: {
                "areas_of_agreement": [],
                "material_disagreements": [],
                "unique_findings": [],
                "conflicting_assumptions": [],
                "evidence_gaps": [],
                "proposed_combined_solution": "Continue bounded validation",
                "rejected_alternatives": [],
                "unresolved_questions": [],
                "acceptance_criteria_evaluation": [],
                "next_action": "continue_follow_up",
                "follow_up_roles": [selected_role],
                "follow_up_questions": ["Validate the deployment risk"],
            },
        )
        advance_staff_meetings(store)
        expected = (
            MeetingStatus.TARGETED_FOLLOW_UP.value
            if round_number < 3
            else MeetingStatus.FINAL_VOTE.value
        )
        assert store.find_staff_meeting(meeting["meeting_id"])["status"] == expected

    current = store.find_staff_meeting(meeting["meeting_id"])
    assert current["follow_up_discussions_used"] == 1
    follow_up_rounds = {
        int(item["round_number"])
        for item in store.staff_meeting_records(meeting["meeting_id"], record_kind="assignment")
        if item["phase"] == MeetingStatus.TARGETED_FOLLOW_UP.value
    }
    assert follow_up_rounds == {1, 2, 3}
    transitions = [
        item["payload"]
        for item in store.staff_meeting_records(meeting["meeting_id"], record_kind="event")
        if item["payload"].get("event_type") == "state_transition"
    ]
    assert sum(
        item.get("previous_status") == MeetingStatus.SYNTHESIS.value
        and item.get("status") == MeetingStatus.TARGETED_FOLLOW_UP.value
        for item in transitions
    ) == 3
    assert sum(
        item.get("previous_status") == MeetingStatus.TARGETED_FOLLOW_UP.value
        and item.get("status") == MeetingStatus.SYNTHESIS.value
        for item in transitions
    ) == 3


def test_multiple_role_seats_have_distinct_assignments_and_disclose_agent_reuse(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agents = _agents()
    for agent in agents:
        store.add_agent(agent)
    meeting = create_staff_meeting(
        store,
        original_request="Review",
        acceptance_criteria=["Complete"],
        chair_agent_id="chief",
        owner_username="owner",
        caller_kind="crew_chief",
        eligible_agents=agents,
    )
    approve_freeze_and_dispatch(store, meeting["meeting_id"], actor_id="chief")
    records = store.staff_meeting_records(meeting["meeting_id"], record_kind="assignment")

    assert len(records) == 5
    assert len({item["role_seat_id"] for item in records}) == 5
    assert len({item["assignment_id"] for item in records}) == 5
    assert len({item["agent_id"] for item in records}) == 3
    assert any(
        sum(item["agent_id"] == candidate["agent_id"] for item in records) > 1
        for candidate in records
    )


def test_new_secret_ballot_round_preserves_previously_revealed_round(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agents = _agents()
    for agent in agents:
        store.add_agent(agent)
    meeting = create_staff_meeting(
        store,
        original_request="Decide",
        acceptance_criteria=["Auditable"],
        chair_agent_id="chief",
        owner_username="owner",
        caller_kind="crew_chief",
        eligible_agents=agents,
    )
    seat = meeting["proposed_roster"][0]
    meeting["ballot_round"] = 2
    meeting["ballots_revealed"] = False
    meeting["revealed_ballot_rounds"] = [1]
    store.upsert_staff_meeting(meeting)
    append_meeting_record(
        store,
        meeting,
        "ballot",
        {"vote": "support"},
        idempotency_key="round-1",
        role_seat_id=seat["role_seat_id"],
        round_number=1,
    )
    append_meeting_record(
        store,
        meeting,
        "ballot",
        {"vote": "oppose"},
        idempotency_key="round-2",
        role_seat_id=seat["role_seat_id"],
        round_number=2,
    )

    visible = meeting_public_view(store, meeting, include_records=True)["records"]

    assert [item["round_number"] for item in visible if item["record_kind"] == "ballot"] == [1]
