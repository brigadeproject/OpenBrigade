"""Release 1.1: scheduled-job briefing delivery back to chief-chat threads."""

from __future__ import annotations

from brigade.efficiency import (
    deliver_recurrence_briefings,
    materialize_due_recurrences,
)
from brigade.schemas import Agent, AssignmentStatus, Team, build_recurrence
from brigade.state import JsonStateStore
from brigade.time import add_seconds_iso, utc_now_iso


def _fleet(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(Agent("chief0", "CHIEF0", "workspace-chief0", role="crew_chief"))
    store.add_agent(Agent("worker0", "WORKER0", "workspace-worker0"))
    store.upsert_team(
        Team(
            team_id="team0",
            display_name="Team 0",
            crew_chief_id="chief0",
            members=["worker0"],
        )
    )
    return store


def _briefing_recurrence(channel="thread:t1", operator="owner"):
    return build_recurrence(
        template={
            "assignment": "Daily status briefing",
            "assigned_to": "chief0",
            "priority": "normal",
            "deliver_to": {
                "channel": channel,
                "operator": operator,
                "agent_id": "chief0",
            },
        },
        interval_seconds=86_400,
        next_due_at=add_seconds_iso(utc_now_iso(), -60),  # already due
    )


def test_briefing_delivered_once_after_completion(tmp_path):
    store = _fleet(tmp_path)
    recurrence = store.add_recurrence(_briefing_recurrence())

    result = materialize_due_recurrences(store)
    assignment_id = result["materialized"][0]["assignment_id"]
    stored = next(
        item
        for item in store.recurrences()
        if item["recurrence_id"] == recurrence["recurrence_id"]
    )
    assert stored["last_assignment_id"] == assignment_id

    # Still in flight: nothing delivered yet.
    assert deliver_recurrence_briefings(store)["delivered"] == []
    assert store.messages("thread:t1") == []

    assignment = store.find_assignment(assignment_id)
    assignment.status = AssignmentStatus.COMPLETE
    assignment.progress_summary = "All goals on track; nothing blocked."
    store.update_assignment(assignment)

    delivery = deliver_recurrence_briefings(store)
    assert [entry["assignment_id"] for entry in delivery["delivered"]] == [assignment_id]
    messages = store.messages("thread:t1")
    assert len(messages) == 1
    assert messages[0].metadata["kind"] == "chief_chat_briefing"
    assert messages[0].sender == "chief0"
    assert "All goals on track" in messages[0].content
    assert "Daily status briefing" in messages[0].content

    # Idempotent: the same materialization never delivers twice.
    assert deliver_recurrence_briefings(store)["delivered"] == []
    assert len(store.messages("thread:t1")) == 1


def test_briefing_delivered_from_archived_history(tmp_path):
    store = _fleet(tmp_path)
    store.add_recurrence(_briefing_recurrence())
    result = materialize_due_recurrences(store)
    assignment_id = result["materialized"][0]["assignment_id"]

    assignment = store.find_assignment(assignment_id)
    assignment.status = AssignmentStatus.COMPLETE
    store.update_assignment(assignment)
    store.archive_assignment(assignment, "Archived executive summary.")
    # Remove from the live queue so only history has it.
    state = store.load()
    state["assignments"] = [
        item for item in state["assignments"] if item["assignment_id"] != assignment_id
    ]
    store.save(state)

    delivery = deliver_recurrence_briefings(store)
    assert len(delivery["delivered"]) == 1
    assert "Archived executive summary." in store.messages("thread:t1")[0].content


def test_briefing_pushes_telegram_when_configured(tmp_path, monkeypatch):
    import brigade.connectors as connectors

    sent = []

    class _Result:
        status = "sent"

    def fake_send(bot_token, *, chat_id, text, http_post=None):
        sent.append({"bot_token": bot_token, "chat_id": chat_id, "text": text})
        return _Result()

    monkeypatch.setattr(connectors, "send_telegram_message", fake_send)

    store = _fleet(tmp_path)
    store.add_recurrence(_briefing_recurrence())
    result = materialize_due_recurrences(store)
    assignment = store.find_assignment(result["materialized"][0]["assignment_id"])
    assignment.status = AssignmentStatus.COMPLETE
    assignment.progress_summary = "Summary for telegram."
    store.update_assignment(assignment)

    delivery = deliver_recurrence_briefings(
        store, telegram_bot_token="bot", operator_telegram_chat_id="777"
    )

    assert delivery["delivered"][0]["telegram"] == "sent"
    assert sent[0]["chat_id"] == "777"
    assert "Summary for telegram." in sent[0]["text"]


def test_recurrences_without_deliver_to_are_untouched(tmp_path):
    store = _fleet(tmp_path)
    plain = build_recurrence(
        template={"assignment": "No delivery", "assigned_to": "chief0"},
        interval_seconds=86_400,
        next_due_at=add_seconds_iso(utc_now_iso(), -60),
    )
    store.add_recurrence(plain)
    result = materialize_due_recurrences(store)
    assignment = store.find_assignment(result["materialized"][0]["assignment_id"])
    assignment.status = AssignmentStatus.COMPLETE
    store.update_assignment(assignment)

    assert deliver_recurrence_briefings(store)["delivered"] == []
    assert store.messages() == []
