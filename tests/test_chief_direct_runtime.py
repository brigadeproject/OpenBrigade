from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

from brigade.chief_direct_runtime import (
    ChiefDirectTurnWorker,
    chief_context_snapshot,
    direct_turn_control,
    queue_direct_chief_turn,
    run_direct_chief_turn,
)
from brigade.config import Settings
from brigade.direct_chief import save_chief_chat_policy
from brigade.governance import ensure_policy_projections_current
from brigade.schemas import Agent, Team
from brigade.state import JsonStateStore
from brigade.workspace import ensure_agent_workspace
from tests.helpers import SequencedTestProvider


def _tool_call(tool_name: str, **arguments) -> str:
    return json.dumps({"status": "tool_call", "tool": tool_name, "arguments": arguments})


def _fixture(tmp_path, *, groups=("workspace_read",)):
    store = JsonStateStore(tmp_path / "state.json")
    chief = Agent(
        "chief0",
        "Chief Zero",
        "workspace-chief0",
        role="crew_chief",
        model_provider="ollama",
        model_name="test",
    )
    store.add_agent(chief)
    store.upsert_team(
        Team("team0", "Team Zero", crew_chief_id="chief0", members=["chief0"])
    )
    workspace = ensure_agent_workspace(chief, tmp_path)
    ensure_policy_projections_current(store, chief, actor="test")
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        secret_store_path=tmp_path / "secrets",
        allow_json_store=True,
    )
    save_chief_chat_policy(
        store,
        settings,
        chief_agent_id="chief0",
        direct_enabled=True,
        tool_groups=list(groups),
        actor="owner",
    )
    thread = store.resolve_active_conversation(
        "owner", "chief:chief0", chief_agent_id="chief0", team_id="team0"
    )
    return store, settings, chief, workspace, thread


def test_context_snapshot_includes_governed_files_and_rolling_memory(tmp_path):
    store, settings, chief, workspace, thread = _fixture(tmp_path)
    del settings, thread
    (workspace / "USER.md").write_text("# User\n\nPrefers concise updates.\n", encoding="utf-8")
    memory_dir = workspace / "memory"
    memory_dir.mkdir()
    recent = memory_dir / "20260830-MEMORY.md"
    recent.write_text("- restarted the database\n", encoding="utf-8")
    old = memory_dir / "20200101-MEMORY.md"
    old.write_text("- old note\n", encoding="utf-8")
    os.utime(old, (1, 1))

    snapshot = chief_context_snapshot(store, chief, max_chars=32_000)

    assert set(snapshot["governed_workspace"]) == {
        "AGENTS.md",
        "USER.md",
        "IDENTITY.md",
        "SOUL.md",
        "TOOLS.md",
        "MEMORY.md",
    }
    assert "Prefers concise updates" in snapshot["governed_workspace"]["USER.md"]
    assert "20260830-MEMORY.md" in snapshot["rolling_24h_daily_memory"]
    assert "20200101-MEMORY.md" not in snapshot["rolling_24h_daily_memory"]


def test_direct_turn_uses_workspace_tool_and_persists_shared_thread(tmp_path):
    store, settings, chief, workspace, thread = _fixture(tmp_path)
    del chief
    (workspace / "runbook.txt").write_text("database restart steps", encoding="utf-8")
    turn = queue_direct_chief_turn(
        store,
        thread=thread,
        chief_agent_id="chief0",
        operator_username="owner",
        content="read the runbook",
        idempotency_key="direct-1",
    )
    provider = SequencedTestProvider(
        [_tool_call("read_file", path="runbook.txt"), "The runbook covers database restarts."]
    )

    result = run_direct_chief_turn(
        store, settings, turn["turn_id"], provider=provider
    )

    assert result["status"] == "complete"
    assert result["tools_used"] == ["read_file"]
    assert result["tool_count"] == 1
    assert "database restart steps" in provider.calls[-1]["prompt"]
    messages = store.messages(thread.channel)
    assert [item.metadata["kind"] for item in messages] == [
        "chief_direct_chat_request",
        "chief_direct_chat_response",
    ]


def test_tool_ceiling_requires_one_turn_confirmation_then_resumes(tmp_path):
    store, settings, chief, workspace, thread = _fixture(tmp_path)
    del chief, workspace
    save_chief_chat_policy(
        store,
        settings,
        chief_agent_id="chief0",
        direct_enabled=True,
        tool_groups=["workspace_read"],
        max_tool_calls=1,
        actor="owner",
    )
    turn = queue_direct_chief_turn(
        store,
        thread=thread,
        chief_agent_id="chief0",
        operator_username="owner",
        content="inspect files",
        idempotency_key="direct-budget",
    )
    first = SequencedTestProvider([_tool_call("list_files", path="")])

    waiting = run_direct_chief_turn(store, settings, turn["turn_id"], provider=first)

    assert waiting["status"] == "awaiting_permission"
    assert waiting["pending_permission"]["kind"] == "remove_tool_ceiling"
    control = direct_turn_control(
        store, thread=thread, operator_username="owner", command="confirm"
    )
    assert control["status"] == "resumed"

    completed = run_direct_chief_turn(
        store,
        settings,
        turn["turn_id"],
        provider=SequencedTestProvider(["Inspection is complete."]),
    )
    assert completed["status"] == "complete"
    assert completed["unbounded_tool_calls"] is True


def test_one_shot_tool_grant_executes_exact_call_once(tmp_path):
    store, settings, chief, workspace, thread = _fixture(tmp_path)
    del chief, workspace
    turn = queue_direct_chief_turn(
        store,
        thread=thread,
        chief_agent_id="chief0",
        operator_username="owner",
        content="run an exact diagnostic",
        idempotency_key="direct-grant",
    )
    request = _tool_call(
        "request_permission",
        tool="shell",
        arguments={"command": ["printf", "diagnostic-ok"]},
        reason="need a local diagnostic",
    )

    waiting = run_direct_chief_turn(
        store,
        settings,
        turn["turn_id"],
        provider=SequencedTestProvider([request]),
    )
    assert waiting["pending_permission"]["kind"] == "tool_call"
    direct_turn_control(store, thread=thread, operator_username="owner", command="confirm")

    completed = run_direct_chief_turn(
        store,
        settings,
        turn["turn_id"],
        provider=SequencedTestProvider(["The diagnostic succeeded."]),
    )

    granted = completed["observations"][0]
    assert granted["tool"] == "shell"
    assert granted["one_shot_grant"] is True
    assert "diagnostic-ok" in granted["output"]
    assert "granted_tool_call" not in completed


def test_direct_chief_bare_create_task_json_creates_task_immediately(tmp_path):
    store, settings, chief, workspace, thread = _fixture(tmp_path)
    del chief, workspace
    turn = queue_direct_chief_turn(
        store,
        thread=thread,
        chief_agent_id="chief0",
        operator_username="owner",
        content="Create a task for chief0 to inspect the service.",
        idempotency_key="direct-create-task",
    )
    response = json.dumps(
        {
            "type": "create_task",
            "description": "Inspect the service",
            "priority": "high",
        }
    )

    completed = run_direct_chief_turn(
        store,
        settings,
        turn["turn_id"],
        provider=SequencedTestProvider([response]),
    )

    assert completed["status"] == "complete"
    assert completed["result_status"] == "applied"
    assignment = store.assignments()[0]
    assert assignment.assignment == "Inspect the service"
    assert assignment.assigned_to == "chief0"
    reply = store.messages(thread.channel)[-1]
    assert "Created task" in reply.content
    assert "{" not in reply.content


def test_root_configured_maintenance_action_uses_exact_argv(tmp_path):
    store, settings, chief, workspace, thread = _fixture(tmp_path, groups=("maintenance",))
    del chief, workspace
    actions = tmp_path / "maintenance-actions.json"
    actions.write_text(
        json.dumps(
            {
                "actions": [
                    {
                        "id": "service-status",
                        "description": "bounded status",
                        "argv": ["printf", "service-is-up"],
                        "allowed_chief_ids": ["chief0"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    settings = replace(settings, maintenance_actions_path=Path(actions))
    turn = queue_direct_chief_turn(
        store,
        thread=thread,
        chief_agent_id="chief0",
        operator_username="owner",
        content="check service",
        idempotency_key="direct-maintenance",
    )
    provider = SequencedTestProvider(
        [
            _tool_call("run_maintenance_action", action_id="service-status"),
            "The service is up.",
        ]
    )

    completed = run_direct_chief_turn(store, settings, turn["turn_id"], provider=provider)

    assert completed["status"] == "complete"
    assert "service-is-up" in completed["observations"][0]["output"]


def test_recovery_marks_in_flight_action_uncertain_and_requires_exact_retry(tmp_path):
    store, settings, chief, workspace, thread = _fixture(tmp_path)
    del chief, workspace
    turn = queue_direct_chief_turn(
        store,
        thread=thread,
        chief_agent_id="chief0",
        operator_username="owner",
        content="inspect files",
        idempotency_key="direct-recovery",
    )
    arguments = {"path": ""}
    from brigade.chief_direct_runtime import _arguments_hash

    fingerprint = _arguments_hash("list_files", arguments)
    turn.update(
        {
            "status": "running",
            "claim_owner": "dead-worker",
            "claim_expires_at": "2020-01-01T00:00:00+00:00",
            "in_flight_tool": {
                "tool": "list_files",
                "arguments": arguments,
                "arguments_hash": fingerprint,
            },
        }
    )
    store.upsert_chief_interactive_turn(turn)

    ChiefDirectTurnWorker(settings, store)._recover_interrupted_turns()
    recovered = store.find_chief_interactive_turn(turn["turn_id"])
    assert recovered["status"] == "queued"
    assert recovered["in_flight_tool"] is None
    assert recovered["uncertain_tool_calls"] == [fingerprint]
    assert recovered["observations"][-1]["uncertain_execution"] is True

    waiting = run_direct_chief_turn(
        store,
        settings,
        turn["turn_id"],
        provider=SequencedTestProvider([_tool_call("list_files", **arguments)]),
    )
    assert waiting["status"] == "awaiting_permission"
    assert waiting["pending_permission"]["arguments_hash"] == fingerprint
