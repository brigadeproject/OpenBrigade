from __future__ import annotations

from datetime import datetime, timezone

import pytest

from brigade.efficiency import run_recurrence_step
from brigade.orchestrator import OrchestrationConfig, run_full_cycle
from brigade.scheduling import next_cron_due, normalize_cron
from brigade.schemas import AGENT_ROLE_EXECUTIVE, Agent, build_recurrence
from brigade.services import create_scheduled_task
from brigade.state import JsonStateStore
from brigade.time import add_seconds_iso, utc_now_iso
from tests.helpers import SequencedTestProvider

NOW = datetime(2026, 8, 24, 8, 59, tzinfo=timezone.utc)


def test_cron_normalizes_macros_and_calculates_weekday_due_time():
    assert normalize_cron("@daily") == "0 0 * * *"
    due = next_cron_due("0 9 * * 1-5", NOW)
    assert due == datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)


def test_cron_rejects_non_five_field_expression():
    with pytest.raises(ValueError, match="five fields"):
        normalize_cron("every weekday")


def test_scheduled_executive_runs_once_in_owners_private_thread(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(
        Agent(
            "exec",
            "Executive",
            "workspace-exec",
            role=AGENT_ROLE_EXECUTIVE,
            owner_username="owner",
        )
    )
    recurrence = build_recurrence(
        template={
            "assignment": "Prepare the morning briefing.",
            "assigned_to": "exec",
            "priority": "normal",
            "target_kind": "executive",
            "owner_username": "owner",
        },
        cron="0 9 * * 1-5",
        next_due_at="2026-08-24T09:00:00+00:00",
    )
    store.add_recurrence(recurrence)
    provider = SequencedTestProvider(["Morning briefing prepared."])
    run_at = datetime(2026, 8, 24, 9, 1, tzinfo=timezone.utc)

    deferred = run_recurrence_step(store, now=run_at)
    first = run_recurrence_step(store, now=run_at, provider=provider)
    second = run_recurrence_step(store, now=run_at, provider=provider)

    assert deferred["deferred"][0]["reason"] == "no_provider"
    assert len(first["materialized"]) == 1
    assert second["materialized"] == []
    assert store.recurrences()[0]["next_due_at"] == "2026-08-25T09:00:00+00:00"
    thread = store.resolve_active_conversation("owner", "executive:exec")
    messages = store.messages(thread.channel)
    assert [message.sender for message in messages] == ["owner", "exec"]
    assert "Scheduled task configured by the operator" in messages[0].content
    assert messages[1].content == "Morning briefing prepared."


def test_only_owner_can_create_an_executive_schedule(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(
        Agent(
            "exec",
            "Executive",
            "workspace-exec",
            role=AGENT_ROLE_EXECUTIVE,
            owner_username="owner",
        )
    )

    with pytest.raises(ValueError, match="only be created"):
        create_scheduled_task(
            store,
            agent_id="exec",
            assignment="Morning check-in",
            owner_username="someone-else",
            cron="0 9 * * *",
        )

    schedule = create_scheduled_task(
        store,
        agent_id="exec",
        assignment="Morning check-in",
        owner_username="owner",
        cron="@daily",
    )
    assert schedule["cron"] == "0 0 * * *"
    assert schedule["template"]["target_kind"] == "executive"


def test_executive_schedule_runs_even_when_no_mission_is_configured(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(
        Agent(
            "exec",
            "Executive",
            "workspace-exec",
            role=AGENT_ROLE_EXECUTIVE,
            owner_username="owner",
        )
    )
    store.add_recurrence(
        build_recurrence(
            template={
                "assignment": "Prepare a private status note.",
                "assigned_to": "exec",
                "target_kind": "executive",
                "owner_username": "owner",
            },
            interval_seconds=300,
            next_due_at=add_seconds_iso(utc_now_iso(), -1),
        )
    )

    result = run_full_cycle(
        store,
        provider=SequencedTestProvider(["Status note prepared."]),
        config=OrchestrationConfig(rest_enabled=False),
    )

    assert result.outcome.reason == "no_mission"
    assert len(result.sub_results["recurrence"]["materialized"]) == 1
