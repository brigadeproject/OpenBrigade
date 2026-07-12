from __future__ import annotations

import argparse
import json

import pytest

from brigade.cli import _chat_tui_provider_from_args, _live_chat_tui_command, _run_cycle, main
from brigade.config import Settings
from brigade.orchestrator import CycleResult
from brigade.schemas import Agent, Assignment, AssignmentStatus, Goal, Mission
from brigade.state import JsonStateStore
from brigade.workspace import write_heartbeat_assignment
from tests.helpers import TestProvider


def test_cli_config_show(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert main(["config", "show"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["data_dir"] == ".brigade"


def test_cli_task_create_and_cycle(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    # v1.0 cycle contract: a cycle without a mission records no_mission and
    # stops, so dispatch tests must set one first.
    assert main(["mission", "set", "--statement", "Prototype mission"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "agent",
                "add",
                "--id",
                "sage",
                "--name",
                "SAGE",
                "--workspace",
                "workspace-sage",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(
            [
                "task",
                "create",
                "--agent",
                "sage",
                "--assignment",
                "Draft first revenue experiment",
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert created["status"] == "queued"

    assert main(["orchestrator", "cycle"]) == 0
    cycle = json.loads(capsys.readouterr().out)
    assert cycle["assigned"] == [created["assignment_id"]]
    assert cycle["cycle_outcome"]["mode"] == "worked"

    assert main(["status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["assignments"][0]["status"] == "assigned"
    assert status["agent_states"]["sage"]["status"] == "working"
    cycle_events = [
        event
        for record in status["orchestrator_reasoning"]
        for event in record.get("events", [])
        if event.get("type") == "cycle_decision"
    ]
    assert any(
        event.get("decision") == "assigned"
        and event.get("provenance", {}).get("assignment_id") == created["assignment_id"]
        for event in cycle_events
    )


def test_cli_task_create_rejects_unknown_agent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="unknown agent: missing"):
        main(["task", "create", "--agent", "missing", "--assignment", "Do work"])


def test_cli_task_create_reuses_idempotency_key(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert (
        main(
            [
                "agent",
                "add",
                "--id",
                "sage",
                "--name",
                "SAGE",
                "--workspace",
                "workspace-sage",
            ]
        )
        == 0
    )
    capsys.readouterr()

    command = [
        "task",
        "create",
        "--agent",
        "sage",
        "--assignment",
        "Do work once",
        "--idempotency-key",
        "task-key",
    ]
    assert main(command) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(command) == 0
    second = json.loads(capsys.readouterr().out)

    assert second["assignment_id"] == first["assignment_id"]
    assert main(["task", "list"]) == 0
    tasks = json.loads(capsys.readouterr().out)
    assert [item["assignment_id"] for item in tasks] == [first["assignment_id"]]


def test_cli_agent_onboard_validates_workspace_and_assigns_team(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert (
        main(
            [
                "agent",
                "onboard",
                "--id",
                "scout",
                "--name",
                "SCOUT",
                "--role",
                "research",
                "--team",
                "discovery",
                "--create-team",
                "--crew-chief",
                "--provider",
                "ollama",
                "--model",
                "qwen2.5-coder:7b",
            ]
        )
        == 0
    )
    onboarded = json.loads(capsys.readouterr().out)
    assert onboarded["valid"] is True
    assert onboarded["agent"]["workspace_path"] == "workspace-scout"
    assert onboarded["agent"]["team_id"] == "discovery"
    assert onboarded["agent"]["model_provider"] == "ollama"
    assert onboarded["team"]["crew_chief_id"] == "scout"
    assert "scout" in onboarded["team"]["members"]

    workspace = tmp_path / ".brigade" / "workspace-scout"
    for filename in ("AGENTS.md", "IDENTITY.md", "MEMORY.md", "TOOLS.md", "USER.md", "SOUL.md"):
        assert (workspace / filename).exists()
    assert (workspace / "HEARTBEAT.md").exists()

    assert main(["agent", "validate", "--id", "scout"]) == 0
    validated = json.loads(capsys.readouterr().out)
    assert validated["valid"] is True
    assert validated["diagnostics"] == []

    assert main(["team", "show", "--id", "discovery"]) == 0
    team = json.loads(capsys.readouterr().out)
    assert team["crew_chief"]["agent_id"] == "scout"
    assert team["member_agents"][0]["agent_id"] == "scout"


def test_cli_team_delegate_reuses_existing_assignment(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert (
        main(
            [
                "agent",
                "onboard",
                "--id",
                "chief",
                "--name",
                "CHIEF",
                "--team",
                "alpha",
                "--create-team",
                "--crew-chief",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["agent", "onboard", "--id", "scout", "--name", "SCOUT", "--team", "alpha"]) == 0
    capsys.readouterr()

    command = [
        "team",
        "delegate",
        "--team",
        "alpha",
        "--chief",
        "chief",
        "--agent",
        "scout",
        "--assignment",
        "Do work once",
    ]
    assert main(command) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "queued"

    assert main(command) == 0
    second = json.loads(capsys.readouterr().out)

    assert second["status"] == "existing"
    assert second["assignment"]["assignment_id"] == first["assignment"]["assignment_id"]
    assert main(["task", "list"]) == 0
    tasks = json.loads(capsys.readouterr().out)
    assert [item["assignment_id"] for item in tasks] == [first["assignment"]["assignment_id"]]


def test_cli_team_create_assign_and_dashboard_view(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert main(["team", "create", "--id", "ops", "--name", "Operations"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["team_id"] == "ops"

    assert (
        main(
            [
                "agent",
                "add",
                "--id",
                "garde",
                "--name",
                "GARDE",
                "--workspace",
                "workspace-garde",
                "--role",
                "infrastructure",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["team", "assign", "--team", "ops", "--agent", "garde", "--crew-chief"]) == 0
    assigned = json.loads(capsys.readouterr().out)
    assert assigned["crew_chief_id"] == "garde"
    assert assigned["members"] == ["garde"]

    assert main(["dashboard", "--plain", "--view", "teams"]) == 0
    dashboard = capsys.readouterr().out
    assert "ops (Operations)" in dashboard
    assert "chief: garde" in dashboard
    assert "- garde (infrastructure)" in dashboard


def test_cli_chat_ask_agent_records_synchronous_exchange(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "brigade.cli.provider_from_settings",
        lambda *args, **kwargs: TestProvider(),
    )

    for agent_id, name in (("scout", "SCOUT"), ("builder", "BUILDER")):
        assert (
            main(
                [
                    "agent",
                    "onboard",
                    "--id",
                    agent_id,
                    "--name",
                    name,
                    "--role",
                    "prototype",
                ]
            )
            == 0
        )
        capsys.readouterr()

    assert (
        main(
            [
                "chat",
                "ask-agent",
                "--from-agent",
                "scout",
                "--to-agent",
                "builder",
                "--message",
                "What should we test next?",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "complete"
    assert result["from_agent"] == "scout"
    assert result["to_agent"] == "builder"
    assert result["route_type"] == "test"
    assert result["request_message_id"]
    assert result["response_message_id"]

    assert main(["chat", "list", "--channel", result["conversation_id"]]) == 0
    messages = json.loads(capsys.readouterr().out)
    assert [item["sender"] for item in messages] == ["scout", "builder"]
    assert messages[0]["metadata"]["kind"] == "agent_chat_request"
    assert messages[1]["metadata"]["kind"] == "agent_chat_response"

    assert main(["status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    chat_usage = [
        item
        for item in status["usage_records"]
        if item.get("conversation_id") == result["conversation_id"]
    ]
    assert len(chat_usage) == 1
    assert chat_usage[0]["agent_id"] == "builder"
    conversation_id = result["conversation_id"]
    episodes = [
        item for item in status["episodes"] if item.get("conversation_id") == conversation_id
    ]
    assert {item["agent_id"] for item in episodes} == {"scout", "builder"}


def test_cli_chat_tui_plain_uses_agent_channel(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert (
        main(["agent", "add", "--id", "sage", "--name", "SAGE", "--workspace", "workspace-sage"])
        == 0
    )
    capsys.readouterr()

    assert main(["chat", "tui", "--agent", "sage", "--plain"]) == 0
    rendered = capsys.readouterr().out

    assert "Channel: user:operator:sage" in rendered
    assert "No messages" in rendered


def test_cli_chat_tui_plain_defaults_to_first_agent(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert (
        main(["agent", "add", "--id", "sage", "--name", "SAGE", "--workspace", "workspace-sage"])
        == 0
    )
    capsys.readouterr()

    assert main(["chat", "tui", "--plain"]) == 0
    rendered = capsys.readouterr().out

    assert "Channel: user:operator:sage" in rendered


def test_cli_chat_tui_uses_available_recommended_model_when_not_overridden(tmp_path, monkeypatch):
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        default_provider="ollama",
        default_model="missing-model",
    )

    monkeypatch.setattr(
        "brigade.cli.available_model_options",
        lambda current_settings: {
            "recommended": {
                "provider": "ollama",
                "model": "test-model",
                "base_url": None,
            }
        },
    )
    monkeypatch.setattr(
        "brigade.cli.provider_from_settings",
        lambda current_settings, **kwargs: TestProvider(model=str(kwargs.get("model"))),
    )

    provider = _chat_tui_provider_from_args(
        argparse.Namespace(provider=None, model=None, base_url=None, api_key=None),
        settings,
    )

    assert provider.complete("hello").model == "test-model"


def test_cli_agent_model_updates_persisted_provider_route(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert (
        main(
            [
                "agent",
                "add",
                "--id",
                "builder",
                "--name",
                "Builder",
                "--workspace",
                "workspace-builder",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(
            [
                "agent",
                "model",
                "--id",
                "builder",
                "--provider",
                "openai-codex",
                "--model",
                "gpt-5.3-codex-spark",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["model_provider"] == "openai-codex"
    assert payload["model_name"] == "gpt-5.3-codex-spark"


def test_cli_model_probe_updates_cached_inventory(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    def fake_probe(settings, *, providers=None):
        assert providers == ["openai-codex"]
        return {
            "updated_at": "2026-06-29T00:00:00+00:00",
            "providers": {
                "openai-codex": {
                    "provider": "openai-codex",
                    "status": "ok",
                    "probed_at": "2026-06-29T00:00:00+00:00",
                    "models": [{"provider": "openai-codex", "model": "codex-test"}],
                }
            },
        }

    monkeypatch.setattr("brigade.cli.probe_model_inventory", fake_probe)

    assert main(["model", "probe", "--provider", "openai-codex"]) == 0
    payload = json.loads(capsys.readouterr().out)
    state = json.loads((tmp_path / ".brigade" / "state.json").read_text(encoding="utf-8"))

    assert payload["providers"]["openai-codex"]["status"] == "ok"
    assert state["model_inventory"]["providers"]["openai-codex"]["models"][0]["model"] == (
        "codex-test"
    )


def test_cli_orchestrator_daemon_probes_models_once_at_start(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    calls = []

    def fake_probe(settings, *, providers=None):
        calls.append(providers)
        return {"updated_at": "2026-06-29T00:00:00+00:00", "providers": {}}

    monkeypatch.setattr("brigade.cli.probe_model_inventory", fake_probe)

    assert main(["orchestrator", "daemon", "--max-cycles", "0", "--no-run-agents"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert calls == [None]
    assert payload["cycles"] == 0
    assert payload["model_inventory"]["providers"] == {}


def test_cli_chat_group_records_pass_the_mic_turns(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "brigade.cli.provider_from_settings",
        lambda *args, **kwargs: TestProvider(),
    )

    for agent_id, name in (
        ("scout", "SCOUT"),
        ("builder", "BUILDER"),
        ("abacus", "ABACUS"),
    ):
        assert (
            main(
                [
                    "agent",
                    "onboard",
                    "--id",
                    agent_id,
                    "--name",
                    name,
                    "--role",
                    "prototype",
                ]
            )
            == 0
        )
        capsys.readouterr()

    assert (
        main(
            [
                "chat",
                "group",
                "--participant",
                "scout",
                "--participant",
                "builder",
                "--participant",
                "abacus",
                "--agenda",
                "Pick the next v0.5 test.",
                "--max-turns",
                "3",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "complete"
    assert result["participants"] == ["scout", "builder", "abacus"]
    assert [turn["speaker"] for turn in result["turns"]] == ["scout", "builder", "abacus"]
    assert [turn["next_speaker"] for turn in result["turns"]] == [
        "builder",
        "abacus",
        "scout",
    ]

    assert main(["chat", "list", "--channel", result["conversation_id"]]) == 0
    messages = json.loads(capsys.readouterr().out)
    assert [item["metadata"]["kind"] for item in messages] == [
        "group_chat_start",
        "group_chat_turn",
        "group_chat_turn",
        "group_chat_turn",
    ]

    assert main(["status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    conversation_id = result["conversation_id"]
    usage = [
        item
        for item in status["usage_records"]
        if item.get("conversation_id") == conversation_id
    ]
    assert [item["agent_id"] for item in usage] == ["scout", "builder", "abacus"]
    episodes = [
        item for item in status["episodes"] if item.get("conversation_id") == conversation_id
    ]
    assert {item["agent_id"] for item in episodes} == {"scout", "builder", "abacus"}


def test_cli_team_delegate_creates_crew_chief_assignment(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert main(["team", "create", "--id", "ops", "--name", "Operations"]) == 0
    capsys.readouterr()
    for agent_id in ("chief", "worker"):
        assert (
            main(
                [
                    "agent",
                    "onboard",
                    "--id",
                    agent_id,
                    "--name",
                    agent_id.upper(),
                    "--team",
                    "ops",
                ]
            )
            == 0
        )
        capsys.readouterr()
    assert main(["team", "chief", "--team", "ops", "--agent", "chief"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "team",
                "delegate",
                "--team",
                "ops",
                "--chief",
                "chief",
                "--agent",
                "worker",
                "--assignment",
                "Write the next smoke test",
                "--goal-statement",
                "Harden v0.5",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assignment = result["assignment"]
    assert result["status"] == "queued"
    assert assignment["created_by"] == "chief"
    assert assignment["created_by_role"] == "crew_chief"
    assert assignment["source"] == "crew_chief_delegate"
    assert assignment["assigned_to"] == "worker"

    assert main(["status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["orchestrator_reasoning"][-1]["source"] == "crew_chief_delegate"


def test_cli_v06_hierarchical_delegation_policy_and_org_graph(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)

    assert (
        main(
            [
                "team",
                "create",
                "--id",
                "ops",
                "--name",
                "Operations",
                "--delegation-policy",
                "chief_only",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "team",
                "create",
                "--id",
                "infra",
                "--name",
                "Infrastructure",
                "--parent",
                "ops",
            ]
        )
        == 0
    )
    capsys.readouterr()
    for agent_id, team_id in (("ops-chief", "ops"), ("infra-worker", "infra")):
        assert (
            main(
                [
                    "agent",
                    "onboard",
                    "--id",
                    agent_id,
                    "--name",
                    agent_id.upper(),
                    "--team",
                    team_id,
                ]
            )
            == 0
        )
        capsys.readouterr()
    assert main(["team", "chief", "--team", "ops", "--agent", "ops-chief"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "team",
                "delegate",
                "--team",
                "ops",
                "--chief",
                "ops-chief",
                "--agent",
                "infra-worker",
                "--assignment",
                "Check child team status",
            ]
        )
        == 0
    )
    delegated = json.loads(capsys.readouterr().out)
    assert delegated["assignment"]["assigned_to"] == "infra-worker"

    assert (
        main(
            [
                "team",
                "policy",
                "--team",
                "infra",
                "--delegation-policy",
                "orchestrator_only",
            ]
        )
        == 0
    )
    policy = json.loads(capsys.readouterr().out)
    assert policy["delegation_policy"] == "orchestrator_only"

    with pytest.raises(PermissionError, match="orchestrator-issued"):
        main(
            [
                "team",
                "delegate",
                "--team",
                "ops",
                "--chief",
                "ops-chief",
                "--agent",
                "infra-worker",
                "--assignment",
                "Bypass child policy",
            ]
        )

    assert main(["org", "graph", "--persist"]) == 0
    graph = json.loads(capsys.readouterr().out)
    assert any(edge["kind"] == "parent_of" for edge in graph["edges"])
    assert any(edge["kind"] == "chief_of" for edge in graph["edges"])

    assert main(["status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    org_records = [
        item for item in status["provenance_records"] if item["node_type"] == "organization_graph"
    ]
    assert org_records


def test_cli_v06_team_route_status_and_cross_team_escalation(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    for team_id, name in (("research", "Research"), ("build", "Build")):
        assert main(["team", "create", "--id", team_id, "--name", name]) == 0
        capsys.readouterr()
    for agent_id, team_id in (
        ("research-chief", "research"),
        ("research-worker", "research"),
        ("build-chief", "build"),
    ):
        assert (
            main(
                [
                    "agent",
                    "onboard",
                    "--id",
                    agent_id,
                    "--name",
                    agent_id.upper(),
                    "--team",
                    team_id,
                ]
            )
            == 0
        )
        capsys.readouterr()
    assert main(["team", "chief", "--team", "research", "--agent", "research-chief"]) == 0
    capsys.readouterr()
    assert main(["team", "chief", "--team", "build", "--agent", "build-chief"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "goal",
                "add",
                "--agent",
                "research-worker",
                "--statement",
                "Find break-test cases",
                "--success",
                "cases documented",
                "--not",
                "touch production",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(
            [
                "team",
                "route-work",
                "--team",
                "research",
                "--scope",
                "individual",
                "--assignment",
                "Gather break-test cases",
                "--goal-statement",
                "Find break-test cases",
            ]
        )
        == 0
    )
    routed = json.loads(capsys.readouterr().out)
    assert routed["decision"]["assignee"] == "research-chief"
    assert routed["assignment"]["source"] == "team_route"

    assert main(["team", "policy", "--team", "research", "--delegation-policy", "open"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "team",
                "route-work",
                "--team",
                "research",
                "--scope",
                "individual",
                "--assignment",
                "Gather a second case",
            ]
        )
        == 0
    )
    routed_open = json.loads(capsys.readouterr().out)
    assert routed_open["decision"]["assignee"] == "research-worker"

    assert (
        main(
            [
                "team",
                "escalate",
                "--from-team",
                "research",
                "--to-team",
                "build",
                "--chief",
                "research-chief",
                "--assignment",
                "Prototype the selected break test",
                "--reason",
                "Needs build-team implementation",
            ]
        )
        == 0
    )
    escalation = json.loads(capsys.readouterr().out)
    assert escalation["assignment"]["assigned_to"] == "build-chief"
    assert escalation["escalation"]["to_team"] == "build"

    assert main(["team", "status", "--team", "research"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["team"]["team_id"] == "research"
    assert "research-worker" in status["goals"]
    assert len(status["active_assignments"]) >= 2

    assert main(["dashboard", "--plain", "--view", "teams"]) == 0
    dashboard = capsys.readouterr().out
    assert "policy: open" in dashboard
    assert "status: goals=" in dashboard


def test_cli_v06_agent_delegate_authority_rejects_cross_team_task(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)

    for team_id in ("alpha", "beta"):
        assert main(["team", "create", "--id", team_id, "--name", team_id.title()]) == 0
        capsys.readouterr()
    for agent_id, team_id in (("alpha-agent", "alpha"), ("beta-agent", "beta")):
        assert (
            main(
                [
                    "agent",
                    "onboard",
                    "--id",
                    agent_id,
                    "--name",
                    agent_id.upper(),
                    "--team",
                    team_id,
                ]
            )
            == 0
        )
        capsys.readouterr()

    with pytest.raises(PermissionError, match="cannot direct"):
        main(
            [
                "task",
                "create",
                "--agent",
                "beta-agent",
                "--assignment",
                "Unauthorized work",
                "--created-by",
                "alpha-agent",
                "--source",
                "agent_delegate",
            ]
        )

    assert main(["team", "policy", "--team", "alpha", "--delegation-policy", "open"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "task",
                "create",
                "--agent",
                "alpha-agent",
                "--assignment",
                "Self-directed work",
                "--created-by",
                "alpha-agent",
                "--source",
                "agent_delegate",
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert created["assigned_to"] == "alpha-agent"


def test_cli_orchestrator_proposes_stalled_goal_work_once(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert (
        main(
            [
                "agent",
                "add",
                "--id",
                "sage",
                "--name",
                "SAGE",
                "--workspace",
                "workspace-sage",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "goal",
                "add",
                "--agent",
                "sage",
                "--statement",
                "Keep goals moving",
                "--success",
                "queued task exists",
                "--not",
                "ignore operator direction",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["orchestrator", "propose-stalled-goals"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert len(first["created"]) == 1
    assert first["created"][0]["source"] == "goal_stall_detector"
    assert first["created"][0]["goal_statement"] == "Keep goals moving"

    assert main(["orchestrator", "propose-stalled-goals"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["created"] == []
    assert second["skipped"][0]["reason"] in {"already queued", "active goal work exists"}


def test_cli_model_route_uses_cost_and_cloud_state(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    store = JsonStateStore(tmp_path / ".brigade" / "state.json")
    store.add_usage_record(
        {
            "usage_id": "usage-1",
            "assignment_id": None,
            "agent_id": "sage",
            "provider": "ollama",
            "model": "qwen2.5-coder:7b",
            "route_type": "local",
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "estimated_cost_usd": 0.0,
            "recorded_at": "2026-05-21T00:00:00+00:00",
        }
    )
    store.upsert_cloud_job(
        {
            "job_id": "cloud-1",
            "assignment_id": "assignment-1",
            "agent_id": "sage",
            "status": "queued",
            "updated_at": "2026-05-21T00:00:00+00:00",
        }
    )

    assert (
        main(
            [
                "model",
                "route",
                "--task-type",
                "research",
                "--risk",
                "high",
                "--prefer",
                "cloud",
                "--local-model",
                "qwen2.5-coder:7b",
            ]
        )
        == 0
    )
    decision = json.loads(capsys.readouterr().out)
    assert decision["recommended_provider"] == "ollama"
    assert decision["recommended_model"] == "qwen2.5-coder:7b"
    assert decision["financial_report"]["block_cloud_dispatch"] is True


def test_cli_cloud_dispatch_queues_extended_work(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert (
        main(
            [
                "agent",
                "add",
                "--id",
                "builder",
                "--name",
                "BUILDER",
                "--workspace",
                "workspace-builder",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(
            [
                "cloud",
                "dispatch",
                "--agent",
                "builder",
                "--assignment",
                "Run extended synthesis",
                "--model",
                "test-cloud-model",
                "--max-cost-usd",
                "1.25",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "queued"
    assert result["assignment"]["work_mode"] == "extended"
    assert result["job"]["status"] == "queued"
    assert result["job"]["max_cost_usd"] == 1.25

    assert main(["cloud", "list", "--status", "queued"]) == 0
    jobs = json.loads(capsys.readouterr().out)
    assert [job["job_id"] for job in jobs] == [result["job"]["job_id"]]

    with pytest.raises(RuntimeError, match="another cloud job"):
        main(
            [
                "cloud",
                "dispatch",
                "--agent",
                "builder",
                "--assignment",
                "Second cloud job",
            ]
        )

    assert (
        main(
            [
                "cloud",
                "resolve",
                "--job-id",
                result["job"]["job_id"],
                "--status",
                "complete",
                "--summary",
                "smoke complete",
            ]
        )
        == 0
    )
    resolved = json.loads(capsys.readouterr().out)
    assert resolved["job"]["status"] == "complete"
    assert resolved["assignment"]["status"] == "complete"

    assert (
        main(
            [
                "cloud",
                "dispatch",
                "--agent",
                "builder",
                "--assignment",
                "Second cloud job",
            ]
        )
        == 0
    )
    capsys.readouterr()


def test_cli_alert_audit_reports_drift_and_repeated_failures(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    store = JsonStateStore(tmp_path / ".brigade" / "state.json")
    store.add_agent(Agent("sage", "SAGE", "workspace-sage"))
    store.add_goal(
        "sage",
        Goal(
            statement="Keep goals moving",
            success_criteria=["active work exists"],
            explicitly_not=["ignore operator direction"],
            set_by="human",
        ),
    )
    assignment = Assignment(
        assignment="Fail repeatedly",
        assigned_to="sage",
        created_by="human",
        source="test",
        goal_statement="Different goal",
    )
    for _ in range(5):
        assignment.register_failure("failed")
    store.add_assignment(assignment)

    assert main(["alert", "audit"]) == 0
    result = json.loads(capsys.readouterr().out)
    kinds = {finding["kind"] for finding in result["findings"]}
    assert {"goal_drift", "repeated_task_failure"} <= kinds
    assert len(result["alerts_created"]) == 2

    assert main(["alert", "audit"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["alerts_created"] == []


def test_cli_daemon_can_run_bounded_cycles(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert main(["orchestrator", "daemon", "--max-cycles", "2", "--sleep-seconds", "0"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["cycles"] == 2


def test_cli_daemon_runs_assigned_agents_by_default(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "brigade.cli.provider_from_settings",
        lambda *args, **kwargs: TestProvider(),
    )
    # v1.0 cycle contract: dispatch requires a mission (no_mission stops the cycle).
    assert main(["mission", "set", "--statement", "Prototype mission"]) == 0
    capsys.readouterr()
    assert (
        main(["agent", "add", "--id", "sage", "--name", "SAGE", "--workspace", "workspace-sage"])
        == 0
    )
    capsys.readouterr()
    assert main(["task", "create", "--agent", "sage", "--assignment", "Complete via daemon"]) == 0
    capsys.readouterr()

    assert main(["orchestrator", "daemon", "--max-cycles", "1", "--sleep-seconds", "0"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["cycles"] == 1
    assert payload["agent_runs"][0]["status"] == "complete"
    assert main(["status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["assignments"] == []
    assert status["assignment_history"][0]["executive_summary"].startswith("test provider:")


def test_cli_daemon_records_one_idle_mission_proposal_without_creating_assignment(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.chdir(tmp_path)
    # This test exercises the propose-only path; pin it explicitly since the
    # shipped default is now create-mode (1.0.2).
    monkeypatch.setenv("BRIGADE_PROACTIVE_MODE", "propose")
    monkeypatch.setenv("BRIGADE_PROACTIVE_CREATION_ENABLED", "false")
    monkeypatch.setattr(
        "brigade.cli.provider_from_settings",
        lambda *args, **kwargs: TestProvider(),
    )
    assert (
        main(
            [
                "mission",
                "set",
                "--statement",
                "Coordinate alpha",
                "--success",
                "plan exists",
                "--not",
                "spawn agents",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "agent",
                "onboard",
                "--id",
                "chief",
                "--name",
                "CHIEF",
                "--role",
                "crew_chief",
                "--team",
                "alpha",
                "--create-team",
                "--crew-chief",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["orchestrator", "daemon", "--max-cycles", "2", "--sleep-seconds", "0"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["cycles"] == 2
    assert payload["agent_runs"] == []
    assert main(["status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["assignments"] == []
    assert status["assignment_history"] == []
    proposals = [
        event
        for record in status["orchestrator_reasoning"]
        for event in record.get("events", [])
        if event.get("type") == "proactive_proposal"
    ]
    assert len(proposals) == 1
    assert proposals[0]["source"] == "orchestrator_mission_continuation"
    assert proposals[0]["provenance"]["idempotency_key"].startswith(
        "orchestrator-proactive:v1:"
    )


def test_cli_mission_agent_goal_and_heartbeat_flow(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "brigade.cli.provider_from_settings",
        lambda *args, **kwargs: TestProvider(),
    )

    assert (
        main(
            [
                "mission",
                "set",
                "--statement",
                "Make enough money to offset operating cost",
                "--success",
                "monthly revenue exceeds spend",
                "--not",
                "spam users",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(
            [
                "agent",
                "add",
                "--id",
                "abacus",
                "--name",
                "ABACUS",
                "--workspace",
                "workspace-abacus",
                "--role",
                "financial",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(
            [
                "goal",
                "add",
                "--agent",
                "abacus",
                "--statement",
                "Find sustainable revenue experiments",
                "--success",
                "one validated experiment",
                "--not",
                "make unsupported financial claims",
                "--human-confirmed",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(
            [
                "task",
                "create",
                "--agent",
                "abacus",
                "--assignment",
                "Estimate current monthly operating cost",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["orchestrator", "cycle"]) == 0
    cycle = json.loads(capsys.readouterr().out)
    assert len(cycle["assigned"]) == 1
    heartbeat = tmp_path / ".brigade" / "workspace-abacus" / "HEARTBEAT.md"
    assert "Estimate current monthly operating cost" in heartbeat.read_text(encoding="utf-8")

    assert main(["agent", "run", "--id", "abacus"]) == 0
    run = json.loads(capsys.readouterr().out)
    assert run["status"] == "complete"

    assert main(["status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["assignments"] == []
    assert status["assignment_history"][0]["assignment_id"] == cycle["assigned"][0]
    assert status["transcripts"][0]["assignment_id"] == cycle["assigned"][0]


def test_cli_knowledge_ingest_and_list(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "library" / "reference-article.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Reference Article\n\nUseful note.\n", encoding="utf-8")

    assert (
        main(
            [
                "knowledge",
                "ingest",
                "--title",
                "Reference Article",
                "--source",
                "https://example.invalid/article",
                "--type",
                "web_article",
                "--path",
                str(source),
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert created["title"] == "Reference Article"
    assert created["metadata"]["chunk_count"] >= 1

    assert main(["knowledge", "list"]) == 0
    documents = json.loads(capsys.readouterr().out)
    assert documents[0]["document_id"] == created["document_id"]


def test_cli_init_mvp_and_dashboard(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert main(["init", "mvp", "--mission", "Offset operating cost"]) == 0
    initialized = json.loads(capsys.readouterr().out)
    assert initialized == {"agents": 3, "status": "initialized"}

    assert main(["dashboard"]) == 0
    dashboard = capsys.readouterr().out
    assert "Mission: Offset operating cost" in dashboard
    assert "- sage (crew_chief): idle" in dashboard
    assert "- garde (infrastructure): idle" in dashboard
    assert "- abacus (financial): idle" in dashboard


def test_cli_init_mvp_rejects_second_run_without_force(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert main(["init", "mvp", "--mission", "Offset operating cost"]) == 0
    capsys.readouterr()

    with pytest.raises(RuntimeError, match="already initialized"):
        main(["init", "mvp", "--mission", "Offset operating cost"])


def test_run_cycle_does_not_reinsert_assignment_completed_concurrently(tmp_path, monkeypatch):
    store = JsonStateStore(tmp_path / "state.json")
    # v1.0 cycle contract: the cycle needs a mission to reach dispatch at all;
    # the concurrent-completion guard under test is unchanged.
    store.set_mission(Mission("Prototype mission", [], []))
    agent = Agent(agent_id="sage", display_name="SAGE", workspace_path="workspace-sage")
    assignment = Assignment(
        assignment="Concurrent completion",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    store.add_agent(agent)
    store.add_assignment(assignment)
    write_heartbeat_assignment(agent, assignment, tmp_path)

    def stub_cycle(assignments, **kwargs):
        current = store.find_assignment(assignment.assignment_id)
        assert current is not None
        current.mark_complete("finished elsewhere")
        store.archive_assignment(current, executive_summary="finished elsewhere")
        return CycleResult(assigned=[], skipped=[], alerts=[])

    monkeypatch.setattr("brigade.orchestrator.deterministic_cycle", stub_cycle)

    _run_cycle(store)

    assert store.find_assignment(assignment.assignment_id) is None
    history = store.assignment_history()
    assert len(history) == 1
    assert history[0]["assignment_id"] == assignment.assignment_id


def test_cli_init_mvp_force_preserves_single_default_goal(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert main(["init", "mvp", "--mission", "Offset operating cost"]) == 0
    capsys.readouterr()
    assert main(["init", "mvp", "--mission", "Offset operating cost", "--force"]) == 0
    capsys.readouterr()

    assert main(["goal", "list"]) == 0
    goals = json.loads(capsys.readouterr().out)
    assert len(goals["sage"]) == 1
    assert len(goals["garde"]) == 1
    assert len(goals["abacus"]) == 1


def test_cli_chat_help_includes_command_guidance(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["chat", "-h"])
    assert excinfo.value.code == 0

    help_text = capsys.readouterr().out
    assert "Send a message to an agent or list stored chat history." in help_text
    assert "Channels are free-form" in help_text
    assert "conversation ids such as 'user:alice' or 'team:ops'." in help_text
    assert "Send one chat message." in help_text
    assert "List chat messages." in help_text


def test_cli_auth_issue_help_shows_ttl_default(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["auth", "issue", "-h"])
    assert excinfo.value.code == 0

    help_text = capsys.readouterr().out
    assert "Token lifetime in seconds. Default: 3600." in help_text


def test_cli_task_help_includes_command_guidance(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["task", "-h"])
    assert excinfo.value.code == 0

    help_text = capsys.readouterr().out
    assert "Create work assignments for agents" in help_text
    assert "inspect one assignment in detail" in help_text
    assert "Create one assignment." in help_text
    assert "Inspect one assignment." in help_text
    assert "Create one assignment interactively." in help_text


def test_cli_task_create_help_shows_operational_defaults(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["task", "create", "-h"])
    assert excinfo.value.code == 0

    help_text = capsys.readouterr().out
    assert "Default: human." in help_text
    assert "Default: direct_command." in help_text
    assert "Task priority. Default: normal." in help_text
    assert "Execution mode for the agent. Default: heartbeat." in help_text
    assert "Default: 1." in help_text


def test_cli_knowledge_help_includes_command_guidance(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["knowledge", "-h"])
    assert excinfo.value.code == 0

    help_text = capsys.readouterr().out
    assert "Ingest local files into document and chunk records" in help_text
    assert "list stored knowledge documents" in help_text
    assert "Ingest a local file with explicit metadata." in help_text
    assert "Upload a local file with default metadata." in help_text
    assert "List stored knowledge documents." in help_text


def test_cli_knowledge_upload_help_shows_defaults(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["knowledge", "upload", "-h"])
    assert excinfo.value.code == 0

    help_text = capsys.readouterr().out
    assert "Default: local." in help_text
    assert "Default: upload." in help_text


def test_cli_memory_help_includes_command_guidance(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["memory", "-h"])
    assert excinfo.value.code == 0

    help_text = capsys.readouterr().out
    assert "Manage per-agent daily memory notes" in help_text
    assert "archive stale" in help_text
    assert "episodic records" in help_text
    assert "Append one memory note." in help_text
    assert "Curate active memory for one agent." in help_text
    assert "Archive stale memory for one agent." in help_text


def test_cli_memory_archive_help_shows_retention_default(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["memory", "archive", "-h"])
    assert excinfo.value.code == 0

    help_text = capsys.readouterr().out
    assert "Archive entries older than this many days. Default: 7." in help_text


def test_cli_user_agent_dashboard_and_model_help_show_defaults(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["user", "add", "-h"])
    assert excinfo.value.code == 0
    user_help = capsys.readouterr().out
    assert "User role to assign. Default: observer." in user_help

    with pytest.raises(SystemExit) as excinfo:
        main(["agent", "add", "-h"])
    assert excinfo.value.code == 0
    agent_help = capsys.readouterr().out
    assert "Agent role label. Default: line_worker." in agent_help

    with pytest.raises(SystemExit) as excinfo:
        main(["dashboard", "-h"])
    assert excinfo.value.code == 0
    dashboard_help = capsys.readouterr().out
    assert "Default: 2.0 seconds." in dashboard_help

    with pytest.raises(SystemExit) as excinfo:
        main(["model", "-h"])
    assert excinfo.value.code == 0
    model_top_help = capsys.readouterr().out
    assert "Run one-off model completions for provider testing and smoke checks." in model_top_help
    assert "This does not create tasks, run agents, or advance the orchestrator." in model_top_help
    assert 'brigade model complete --prompt "Summarize the mission"' in model_top_help
    assert "Run one completion request." in model_top_help

    with pytest.raises(SystemExit) as excinfo:
        main(["model", "complete", "-h"])
    assert excinfo.value.code == 0
    model_help = capsys.readouterr().out
    assert "Model provider to use. Default: resolved settings" in model_help
    assert "Ollama unless configured otherwise" in model_help
    assert "Model name. Default: resolved settings model." in model_help
    assert "BRIGADE_OLLAMA_BASE_URL" in model_help
    assert "http://127.0.0.1:11434." in model_help


def test_cli_model_help_uses_env_ollama_base_url(monkeypatch, capsys):
    monkeypatch.setenv("BRIGADE_OLLAMA_BASE_URL", "http://host.docker.internal:11434")

    with pytest.raises(SystemExit) as excinfo:
        main(["model", "complete", "-h"])
    assert excinfo.value.code == 0

    model_help = capsys.readouterr().out
    assert "BRIGADE_OLLAMA_BASE_URL" in model_help
    assert "http://127.0.0.1:11434." in model_help


def test_cli_orchestrator_help_includes_guidance_and_examples(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["orchestrator", "-h"])
    assert excinfo.value.code == 0

    help_text = capsys.readouterr().out
    assert "Advance assignment state once with a single cycle" in help_text
    assert "run the continuous orchestrator loop" in help_text
    assert "Run one orchestrator cycle." in help_text
    assert "Run the continuous orchestrator loop." in help_text
    assert "brigade orchestrator cycle" in help_text
    assert "brigade orchestrator daemon --max-cycles 10" in help_text


def test_cli_orchestrator_daemon_help_shows_sleep_default_behavior(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["orchestrator", "daemon", "-h"])
    assert excinfo.value.code == 0

    help_text = capsys.readouterr().out
    assert "Default: configured" in help_text
    assert "orchestrator cadence." in help_text


def test_cli_host_state_guard_blocks_repo_local_commands(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docker-compose.yml").write_text("name: brigade\n", encoding="utf-8")
    ops_dir = tmp_path / "ops"
    ops_dir.mkdir()
    (ops_dir / "brigade-live.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="host-state guard"):
        main(["status", "--json"])


def test_cli_host_state_guard_allows_override(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docker-compose.yml").write_text("name: brigade\n", encoding="utf-8")
    ops_dir = tmp_path / "ops"
    ops_dir.mkdir()
    (ops_dir / "brigade-live.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    assert main(["--allow-host-state", "status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["mission"] is None


def test_cli_host_state_guard_allows_override_after_subcommand(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docker-compose.yml").write_text("name: brigade\n", encoding="utf-8")
    ops_dir = tmp_path / "ops"
    ops_dir.mkdir()
    (ops_dir / "brigade-live.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    assert main(["status", "--json", "--allow-host-state"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["mission"] is None


def test_cli_chat_tui_delegates_to_live_wrapper_inside_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docker-compose.yml").write_text("name: brigade\n", encoding="utf-8")
    ops_dir = tmp_path / "ops"
    ops_dir.mkdir()
    wrapper = ops_dir / "brigade-live.sh"
    wrapper.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    args = argparse.Namespace(
        command="chat",
        chat_command="tui",
        allow_host_state=False,
    )

    command = _live_chat_tui_command(
        args,
        ["chat", "tui", "--agent", "sage"],
        cwd=tmp_path,
        in_container=False,
    )

    assert command == [str(wrapper), "chat", "tui", "--agent", "sage"]


def test_cli_chat_tui_delegation_respects_allow_host_state(tmp_path):
    (tmp_path / "docker-compose.yml").write_text("name: brigade\n", encoding="utf-8")
    ops_dir = tmp_path / "ops"
    ops_dir.mkdir()
    (ops_dir / "brigade-live.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    args = argparse.Namespace(
        command="chat",
        chat_command="tui",
        allow_host_state=True,
    )

    assert _live_chat_tui_command(args, ["chat", "tui"], cwd=tmp_path, in_container=False) is None


def test_cli_agent_update_role_and_specialties(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["agent", "add", "--id", "sage", "--name", "SAGE", "--workspace", "ws"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "agent",
                "update",
                "--id",
                "sage",
                "--role",
                "crew_chief",
                "--specialty",
                "telemetry",
                "--specialty",
                "web design",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["role"] == "crew_chief"
    assert payload["specialties"] == ["telemetry", "web design"]

    # Specialties replace wholesale; role stays untouched when omitted.
    assert main(["agent", "update", "--id", "sage", "--specialty", "css"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["role"] == "crew_chief"
    assert payload["specialties"] == ["css"]

    with pytest.raises(ValueError, match="nothing to update"):
        main(["agent", "update", "--id", "sage"])
