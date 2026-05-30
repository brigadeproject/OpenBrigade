from __future__ import annotations

import json

import pytest

from brigade.cli import main


def test_auth_issue_and_verify_roundtrip(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert main(["init", "mvp", "--mission", "Prototype mission"]) == 0
    capsys.readouterr()
    assert main(["user", "add", "--username", "alice", "--role", "operator"]) == 0
    capsys.readouterr()

    assert main(["auth", "issue", "--username", "alice", "--ttl-seconds", "300"]) == 0
    token = json.loads(capsys.readouterr().out)["token"]

    assert main(["auth", "verify", "--token-value", token]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["ok"] is True
    assert verified["user"]["username"] == "alice"
    assert verified["claims"]["role"] == "operator"


def test_auth_issue_can_provision_missing_user_when_role_is_supplied(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.chdir(tmp_path)

    assert main(["init", "mvp", "--mission", "Prototype mission"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "auth",
                "issue",
                "--username",
                "tm",
                "--role",
                "operator",
                "--ttl-seconds",
                "300",
            ]
        )
        == 0
    )
    token = json.loads(capsys.readouterr().out)["token"]

    assert main(["auth", "verify", "--token-value", token]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["ok"] is True
    assert verified["user"]["username"] == "tm"
    assert verified["claims"]["role"] == "operator"


def test_auth_issue_missing_user_has_actionable_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert main(["init", "mvp", "--mission", "Prototype mission"]) == 0

    message = "create it first with 'brigade user add'|issue a token with --role"
    with pytest.raises(ValueError, match=message):
        main(["auth", "issue", "--username", "tm"])


def test_observer_cannot_create_task_and_operator_cannot_set_mission(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.chdir(tmp_path)

    assert main(["init", "mvp", "--mission", "Prototype mission"]) == 0
    capsys.readouterr()
    assert main(["user", "add", "--username", "obs", "--role", "observer"]) == 0
    capsys.readouterr()
    assert main(["user", "add", "--username", "alice", "--role", "operator"]) == 0
    capsys.readouterr()

    with pytest.raises(PermissionError, match="task:write"):
        main(
            [
                "--as-user",
                "obs",
                "task",
                "create",
                "--agent",
                "sage",
                "--assignment",
                "Unauthorized task",
            ]
        )

    with pytest.raises(PermissionError, match="mission:write"):
        main(
            [
                "--as-user",
                "alice",
                "mission",
                "set",
                "--statement",
                "New mission",
            ]
        )


def test_task_prompt_and_inspect_include_v0_4_fields(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["init", "mvp", "--mission", "Prototype mission"]) == 0
    capsys.readouterr()

    responses = iter(
        [
            "Create a test report",
            "1",
            "high",
            "2",
            "Support the prototype mission",
            "SAGE is the right owner",
            "",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": next(responses))

    assert main(["task", "prompt"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["goal_statement"] == "Support the prototype mission"
    assert created["assignment_rationale"] == "SAGE is the right owner"
    assert created["priority"] == "high"

    assert main(["task", "inspect", "--id", created["assignment_id"]]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["assignment"]["assignment_id"] == created["assignment_id"]
    assert inspected["why_this_agent"] == "SAGE is the right owner"
    assert inspected["goal_statement"] == "Support the prototype mission"


def test_chat_identity_metadata_and_knowledge_upload(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "upload.md"
    source.write_text("# Upload\n\nPrototype note.\n", encoding="utf-8")

    assert main(["init", "mvp", "--mission", "Prototype mission"]) == 0
    capsys.readouterr()
    assert main(["user", "add", "--username", "alice", "--role", "operator"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "--as-user",
                "alice",
                "chat",
                "send",
                "--channel",
                "user:alice",
                "--sender",
                "alice",
                "--recipient",
                "sage",
                "--message",
                "What changed?",
            ]
        )
        == 0
    )
    message = json.loads(capsys.readouterr().out)
    assert message["metadata"]["verified_user"]["username"] == "alice"
    assert "Current user:" in message["metadata"]["identity_context"]

    assert (
        main(
            [
                "--as-user",
                "alice",
                "knowledge",
                "upload",
                "--path",
                str(source),
            ]
        )
        == 0
    )
    uploaded = json.loads(capsys.readouterr().out)
    assert uploaded["title"] == "upload"


def test_dashboard_views_render_non_interactively(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["init", "mvp", "--mission", "Prototype mission"]) == 0
    capsys.readouterr()

    assert main(["dashboard", "--plain", "--view", "mission"]) == 0
    mission_view = capsys.readouterr().out
    assert "Mission" in mission_view
    assert "Prototype mission" in mission_view

    assert main(["dashboard", "--plain", "--view", "agents"]) == 0
    agents_view = capsys.readouterr().out
    assert "Agents" in agents_view
    assert "sage" in agents_view
