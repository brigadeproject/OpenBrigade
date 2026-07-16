"""Release 1.1 phase 4: propose->confirm chief actions, team-scoped."""

from __future__ import annotations

import json

from brigade.chief_chat import resolve_persona, run_chief_chat_turn
from brigade.schemas import Agent, Assignment, AssignmentStatus, Priority, Team
from brigade.services import apply_chief_chat_actions
from brigade.state import JsonStateStore
from tests.helpers import SequencedTestProvider


def _fleet(tmp_path, teams: int = 2):
    store = JsonStateStore(tmp_path / "state.json")
    for index in range(teams):
        chief = f"chief{index}"
        worker = f"worker{index}"
        store.add_agent(Agent(chief, chief.upper(), f"workspace-{chief}", role="crew_chief"))
        store.add_agent(Agent(worker, worker.upper(), f"workspace-{worker}"))
        store.upsert_team(
            Team(
                team_id=f"team{index}",
                display_name=f"Team {index}",
                crew_chief_id=chief,
                members=[worker],
            )
        )
    return store


def _turn(store, provider, *, persona_name="chief0", content="", operator="owner"):
    persona = resolve_persona(store, persona_name)
    thread = store.resolve_active_conversation(operator, persona.persona_id)
    return run_chief_chat_turn(
        store,
        thread=thread,
        persona=persona,
        operator=operator,
        content=content,
        provider=provider,
    )


def _propose(actions, summary="Doing the thing."):
    return json.dumps({"status": "propose_actions", "summary": summary, "actions": actions})


def _audit_actions(store) -> list[str]:
    decisions = []
    for record in store.orchestrator_reasoning():
        for event in record.get("events", []):
            decision = event.get("decision")
            if decision:
                decisions.append(decision)
    return decisions


def test_stage_then_confirm_set_priority(tmp_path):
    store = _fleet(tmp_path)
    task = Assignment(
        assignment="Ship the thing",
        assigned_to="worker0",
        created_by="human",
        source="direct_command",
        priority=Priority.NORMAL,
    )
    store.add_assignment(task)

    provider = SequencedTestProvider(
        [_propose([{"type": "set_priority", "assignment_id": task.assignment_id,
                    "priority": "high"}])]
    )
    staged = _turn(store, provider, content="bump that task to high")
    assert staged["status"] == "proposed"
    # Nothing applied yet.
    assert store.find_assignment(task.assignment_id).priority == Priority.NORMAL

    # A bare confirm needs no model call.
    provider = SequencedTestProvider([])
    applied = _turn(store, provider, content="confirm")
    assert applied["status"] == "applied"
    assert applied["actions_applied"][0]["type"] == "set_priority"
    assert store.find_assignment(task.assignment_id).priority == Priority.HIGH
    assert provider.calls == []
    assert "chief_chat_set_priority" in _audit_actions(store)


def test_decline_discards_without_applying(tmp_path):
    store = _fleet(tmp_path)
    task = Assignment(
        assignment="Ship the thing",
        assigned_to="worker0",
        created_by="human",
        source="direct_command",
    )
    store.add_assignment(task)
    provider = SequencedTestProvider(
        [_propose([{"type": "cancel_assignment", "assignment_id": task.assignment_id}])]
    )
    _turn(store, provider, content="cancel that")

    provider = SequencedTestProvider([])
    declined = _turn(store, provider, content="cancel")
    assert declined["status"] == "declined"
    assert store.find_assignment(task.assignment_id) is not None


def test_confirm_creates_assignment_for_managed_agent(tmp_path):
    store = _fleet(tmp_path)
    provider = SequencedTestProvider(
        [
            _propose(
                [
                    {
                        "type": "create_assignment",
                        "agent_id": "worker0",
                        "assignment": "Write the migration",
                        "priority": "high",
                    }
                ]
            )
        ]
    )
    _turn(store, provider, content="have worker0 write the migration")
    provider = SequencedTestProvider([])
    applied = _turn(store, provider, content="confirm")

    assert applied["status"] == "applied"
    created = applied["actions_applied"][0]
    assert created["type"] == "create_assignment"
    assert created["agent_id"] == "worker0"
    persisted = store.find_assignment(created["assignment_id"])
    assert persisted is not None
    assert persisted.priority == Priority.HIGH
    assert persisted.source == "chief_chat"


def test_out_of_scope_actions_are_rejected(tmp_path):
    store = _fleet(tmp_path, teams=2)
    foreign = Assignment(
        assignment="Other team's task",
        assigned_to="worker1",
        created_by="human",
        source="direct_command",
    )
    store.add_assignment(foreign)

    # chief0 cannot cancel worker1's task, nor create work for worker1.
    result = apply_chief_chat_actions(
        store,
        [
            {"type": "cancel_assignment", "assignment_id": foreign.assignment_id},
            {"type": "create_assignment", "agent_id": "worker1", "assignment": "nope"},
            {"type": "set_routing_policy", "assignment_kind": "x", "target_team_id": "y"},
        ],
        chief_id="chief0",
        managed_agent_ids={"chief0", "worker0"},
        by="owner",
    )
    assert result["applied"] == []
    reasons = " ".join(item["reason"] for item in result["rejected"])
    assert "outside your team" in reasons
    assert "not on your team" in reasons
    assert "unsupported chief chat action type" in reasons
    assert store.find_assignment(foreign.assignment_id) is not None


def test_front_desk_is_unrestricted(tmp_path):
    store = _fleet(tmp_path, teams=2)
    foreign = Assignment(
        assignment="Any team's task",
        assigned_to="worker1",
        created_by="human",
        source="direct_command",
        priority=Priority.LOW,
    )
    store.add_assignment(foreign)

    result = apply_chief_chat_actions(
        store,
        [{"type": "set_priority", "assignment_id": foreign.assignment_id, "priority": "high"}],
        chief_id=None,
        managed_agent_ids=None,
        by="owner",
    )
    assert result["rejected"] == []
    assert store.find_assignment(foreign.assignment_id).priority == Priority.HIGH


def test_set_priority_on_running_task_is_rejected(tmp_path):
    store = _fleet(tmp_path)
    running = Assignment(
        assignment="In-flight work",
        assigned_to="worker0",
        created_by="human",
        source="direct_command",
        status=AssignmentStatus.WORKING,
    )
    store.add_assignment(running)

    result = apply_chief_chat_actions(
        store,
        [{"type": "set_priority", "assignment_id": running.assignment_id, "priority": "high"}],
        chief_id="chief0",
        managed_agent_ids={"chief0", "worker0"},
        by="owner",
    )
    assert result["applied"] == []
    assert "running" in result["rejected"][0]["reason"]


def test_attach_guidance_records_and_audits(tmp_path):
    store = _fleet(tmp_path)
    task = Assignment(
        assignment="Needs direction",
        assigned_to="worker0",
        created_by="human",
        source="direct_command",
        status=AssignmentStatus.BLOCKED,
    )
    store.add_assignment(task)

    result = apply_chief_chat_actions(
        store,
        [
            {
                "type": "attach_guidance",
                "assignment_id": task.assignment_id,
                "message": "Try the fallback endpoint",
            }
        ],
        chief_id="chief0",
        managed_agent_ids={"chief0", "worker0"},
        by="owner",
    )
    assert result["applied"][0]["type"] == "attach_guidance"
    refreshed = store.find_assignment(task.assignment_id)
    assert refreshed.operator_guidance[-1]["operator_message"] == "Try the fallback endpoint"
    assert "chief_chat_attach_guidance" in _audit_actions(store)


def test_create_recurrence_staged_confirmed_and_delivered_to_thread(tmp_path):
    store = _fleet(tmp_path)
    provider = SequencedTestProvider(
        [
            _propose(
                [
                    {
                        "type": "create_recurrence",
                        "agent_id": "worker0",
                        "assignment": "Daily team briefing",
                        "interval_seconds": 86400,
                        "deliver_briefing": True,
                    }
                ]
            )
        ]
    )
    staged = _turn(store, provider, content="brief me daily")
    assert staged["status"] == "proposed"
    assert store.recurrences() == []

    applied = _turn(store, SequencedTestProvider([]), content="confirm")
    assert applied["status"] == "applied"
    created = applied["actions_applied"][0]
    assert created["type"] == "create_recurrence"
    assert created["delivers_briefing"] is True

    recurrence = store.recurrences()[0]
    assert recurrence["interval_seconds"] == 86400
    template = recurrence["template"]
    assert template["assigned_to"] == "worker0"
    # Briefings land back in the very thread the operator asked from.
    assert template["deliver_to"]["channel"] == staged["conversation_id"]
    assert template["deliver_to"]["operator"] == "owner"
    assert template["deliver_to"]["agent_id"] == "chief0"
    assert "chief_chat_create_recurrence" in _audit_actions(store)


def test_create_recurrence_validation_and_scope(tmp_path):
    store = _fleet(tmp_path, teams=2)
    managed = {"chief0", "worker0"}

    result = apply_chief_chat_actions(
        store,
        [
            {"type": "create_recurrence", "agent_id": "worker1",
             "assignment": "x", "interval_seconds": 86400},
            {"type": "create_recurrence", "agent_id": "worker0",
             "assignment": "x", "interval_seconds": 30},
            {"type": "create_recurrence", "agent_id": "worker0",
             "assignment": "x", "interval_seconds": 86400,
             "next_due_at": "tomorrow-ish"},
            {"type": "create_recurrence", "agent_id": "worker0",
             "assignment": "", "interval_seconds": 86400},
        ],
        chief_id="chief0",
        managed_agent_ids=managed,
        by="owner",
    )
    assert result["applied"] == []
    reasons = " ".join(item["reason"] for item in result["rejected"])
    assert "not on your team" in reasons
    assert "at least 300" in reasons
    assert "not a UTC ISO timestamp" in reasons
    assert "missing assignment" in reasons
    assert store.recurrences() == []

    # deliver_briefing without a conversation channel degrades to no delivery.
    ok = apply_chief_chat_actions(
        store,
        [{"type": "create_recurrence", "agent_id": "worker0",
          "assignment": "weekly report", "interval_seconds": 604800,
          "deliver_briefing": True}],
        chief_id="chief0",
        managed_agent_ids=managed,
        by="owner",
        conversation_channel=None,
    )
    assert ok["applied"][0]["delivers_briefing"] is False


def test_set_recurrence_enabled_toggles_and_scope_checks(tmp_path):
    from brigade.schemas import build_recurrence
    from brigade.time import add_seconds_iso
    from brigade.time import utc_now_iso as now_iso

    store = _fleet(tmp_path, teams=2)
    mine = store.add_recurrence(
        build_recurrence(
            template={"assignment": "mine", "assigned_to": "worker0"},
            interval_seconds=86400,
            next_due_at=add_seconds_iso(now_iso(), 3600),
        )
    )
    theirs = store.add_recurrence(
        build_recurrence(
            template={"assignment": "theirs", "assigned_to": "worker1"},
            interval_seconds=86400,
            next_due_at=add_seconds_iso(now_iso(), 3600),
        )
    )

    result = apply_chief_chat_actions(
        store,
        [
            {"type": "set_recurrence_enabled",
             "recurrence_id": mine["recurrence_id"], "enabled": False},
            {"type": "set_recurrence_enabled",
             "recurrence_id": theirs["recurrence_id"], "enabled": False},
            {"type": "set_recurrence_enabled",
             "recurrence_id": "missing", "enabled": False},
        ],
        chief_id="chief0",
        managed_agent_ids={"chief0", "worker0"},
        by="owner",
    )

    assert [item["recurrence_id"] for item in result["applied"]] == [
        mine["recurrence_id"]
    ]
    reasons = " ".join(item["reason"] for item in result["rejected"])
    assert "outside your team" in reasons
    assert "unknown recurrence" in reasons
    by_id = {item["recurrence_id"]: item for item in store.recurrences()}
    assert by_id[mine["recurrence_id"]]["enabled"] is False
    assert by_id[theirs["recurrence_id"]]["enabled"] is True


def test_route_stage_and_confirm_flow(tmp_path, monkeypatch):
    import asyncio

    import pytest

    pytest.importorskip("fastapi")
    import brigade.web as web
    from brigade.auth import issue_token
    from brigade.config import Settings
    from brigade.schemas import Role, User
    from tests.test_v0_9 import _asgi_request

    store = _fleet(tmp_path)
    task = Assignment(
        assignment="Ship the thing",
        assigned_to="worker0",
        created_by="human",
        source="direct_command",
    )
    store.add_assignment(task)
    owner = User(username="owner", role=Role.OWNER)
    observer = User(username="obs", role=Role.OBSERVER)
    store.add_user(owner)
    store.add_user(observer)
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        require_auth=True,
        jwt_secret="x" * 40,
        allow_json_store=True,
    )
    provider = SequencedTestProvider(
        [_propose([{"type": "set_priority", "assignment_id": task.assignment_id,
                    "priority": "high"}])]
    )
    monkeypatch.setattr(web, "_provider_from_payload", lambda payload, settings: provider)
    app = web.create_app(settings, store)
    owner_headers = {"Authorization": f"Bearer {issue_token(settings, owner)}"}
    observer_headers = {"Authorization": f"Bearer {issue_token(settings, observer)}"}

    opened = asyncio.run(
        _asgi_request(
            app, "POST", "/api/chat/threads", headers=owner_headers,
            json_payload={"persona": "chief0"},
        )
    )
    thread_id = opened.json()["thread_id"]

    staged = asyncio.run(
        _asgi_request(
            app, "POST", f"/api/chat/threads/{thread_id}/messages", headers=owner_headers,
            json_payload={"content": "bump it to high"},
        )
    )
    assert staged.json()["status"] == "proposed"

    # Observer lacks chat:write, so cannot drive the confirm path at all.
    denied = asyncio.run(
        _asgi_request(
            app, "POST", f"/api/chat/threads/{thread_id}/messages", headers=observer_headers,
            json_payload={"content": "confirm"},
        )
    )
    assert denied.status_code == 403

    confirmed = asyncio.run(
        _asgi_request(
            app, "POST", f"/api/chat/threads/{thread_id}/messages", headers=owner_headers,
            json_payload={"content": "confirm"},
        )
    )
    assert confirmed.json()["status"] == "applied"
    assert store.find_assignment(task.assignment_id).priority == Priority.HIGH


def test_retry_blocked_assignment_scope_checked(tmp_path):
    store = _fleet(tmp_path, teams=2)
    foreign = Assignment(
        assignment="Other team's blocked task",
        assigned_to="worker1",
        created_by="human",
        source="direct_command",
        status=AssignmentStatus.BLOCKED,
    )
    store.add_assignment(foreign)

    result = apply_chief_chat_actions(
        store,
        [{"type": "retry_blocked_assignment", "assignment_id": foreign.assignment_id}],
        chief_id="chief0",
        managed_agent_ids={"chief0", "worker0"},
        by="owner",
    )
    assert result["applied"] == []
    assert "outside your team" in result["rejected"][0]["reason"]
