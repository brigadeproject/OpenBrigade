from __future__ import annotations

import json

from brigade.cli import main


def test_user_chat_model_and_db_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert main(["user", "add", "--username", "alice", "--role", "operator"]) == 0
    user = json.loads(capsys.readouterr().out)
    assert user["role"] == "operator"

    assert (
        main(
            [
                "chat",
                "send",
                "--channel",
                "user:alice",
                "--sender",
                "alice",
                "--recipient",
                "sage",
                "--message",
                "What are you working on?",
            ]
        )
        == 0
    )
    message = json.loads(capsys.readouterr().out)
    assert message["channel"] == "user:alice"

    assert main(["chat", "list", "--channel", "user:alice"]) == 0
    messages = json.loads(capsys.readouterr().out)
    assert messages[0]["message_id"] == message["message_id"]

    assert main(["model", "complete", "--provider", "fake", "--prompt", "Summarize"]) == 0
    completion = json.loads(capsys.readouterr().out)
    assert completion["provider"] == "fake"

    assert main(["db", "migrations"]) == 0
    migrations = json.loads(capsys.readouterr().out)
    assert any(item.endswith("migrations/0001_core_state.sql") for item in migrations)

    assert main(["db", "schema"]) == 0
    assert "brigade_assignments" in capsys.readouterr().out
