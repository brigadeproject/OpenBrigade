"""Release 1.1 phase 1: durable chief-chat threads and persona resolution."""

from __future__ import annotations

import asyncio

import pytest

from brigade.chief_chat import (
    FRONT_DESK_PERSONA,
    UnknownPersonaError,
    available_personas,
    resolve_persona,
)
from brigade.schemas import Agent, ChatMessage, Conversation, Role, Team, User
from brigade.state import JsonStateStore


def _store(tmp_path) -> JsonStateStore:
    return JsonStateStore(tmp_path / "state.json")


def _team_fleet(store, teams: int = 2) -> None:
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


def test_conversation_store_round_trip(tmp_path):
    store = _store(tmp_path)
    conversation = Conversation(operator_username="owner", persona="chief:chief0")

    store.upsert_conversation(conversation)
    found = store.find_conversation(conversation.thread_id)

    assert found is not None
    assert found.persona == "chief:chief0"
    assert found.channel == f"thread:{conversation.thread_id}"
    assert store.find_conversation("missing") is None

    store.set_conversation_summary(conversation.thread_id, "- talked about tasks")
    assert store.find_conversation(conversation.thread_id).rolling_summary == (
        "- talked about tasks"
    )


def test_conversations_filters_by_operator_and_status(tmp_path):
    store = _store(tmp_path)
    mine = Conversation(operator_username="owner", persona=FRONT_DESK_PERSONA)
    other = Conversation(operator_username="someone", persona=FRONT_DESK_PERSONA)
    archived = Conversation(
        operator_username="owner", persona="chief:chief0", status="archived"
    )
    for item in (mine, other, archived):
        store.upsert_conversation(item)

    assert {item.thread_id for item in store.conversations("owner")} == {
        mine.thread_id,
        archived.thread_id,
    }
    assert [item.thread_id for item in store.conversations("owner", status="active")] == [
        mine.thread_id
    ]


def test_resolve_active_conversation_is_get_or_create(tmp_path):
    store = _store(tmp_path)

    first = store.resolve_active_conversation("owner", "chief:chief0")
    second = store.resolve_active_conversation("owner", "chief:chief0")
    other_persona = store.resolve_active_conversation("owner", FRONT_DESK_PERSONA)
    other_operator = store.resolve_active_conversation("someone", "chief:chief0")

    assert first.thread_id == second.thread_id
    assert other_persona.thread_id != first.thread_id
    assert other_operator.thread_id != first.thread_id


def test_recent_messages_returns_tail_in_order(tmp_path):
    store = _store(tmp_path)
    for index in range(7):
        store.add_message(
            ChatMessage(
                channel="thread:t1",
                sender="owner",
                recipient="chief0",
                content=f"message {index}",
            )
        )
    store.add_message(
        ChatMessage(channel="other", sender="owner", recipient="chief0", content="noise")
    )

    recent = store.recent_messages("thread:t1", limit=3)

    assert [item.content for item in recent] == ["message 4", "message 5", "message 6"]


def test_resolve_persona_defaults(tmp_path):
    store = _store(tmp_path)
    # No teams at all: front desk.
    assert resolve_persona(store, None).is_front_desk

    _team_fleet(store, teams=1)
    single = resolve_persona(store, None)
    assert single.persona_id == "chief:chief0"
    assert single.managed_agent_ids == frozenset({"chief0", "worker0"})

    _team_fleet(store, teams=2)
    assert resolve_persona(store, None).is_front_desk
    # Explicit configured default wins over auto.
    assert resolve_persona(store, None, default="chief:chief1").persona_id == "chief:chief1"


def test_resolve_persona_accepts_aliases(tmp_path):
    store = _store(tmp_path)
    _team_fleet(store, teams=2)

    assert resolve_persona(store, "frontdesk").is_front_desk
    assert resolve_persona(store, "front_desk").is_front_desk
    assert resolve_persona(store, "chief:chief1").persona_id == "chief:chief1"
    assert resolve_persona(store, "chief0").persona_id == "chief:chief0"
    assert resolve_persona(store, "team1").persona_id == "chief:chief1"
    assert resolve_persona(store, "CHIEF1").persona_id == "chief:chief1"

    with pytest.raises(UnknownPersonaError):
        resolve_persona(store, "nobody")
    # "Team" matches both display names: ambiguous fragments are rejected.
    with pytest.raises(UnknownPersonaError):
        resolve_persona(store, "team ")


def test_available_personas_lists_front_desk_and_chiefs(tmp_path):
    store = _store(tmp_path)
    _team_fleet(store, teams=2)

    personas = available_personas(store)

    assert personas[0].is_front_desk
    assert [item.persona_id for item in personas[1:]] == ["chief:chief0", "chief:chief1"]
    assert personas[1].team_id == "team0"


def _auth_app(tmp_path):
    pytest.importorskip("fastapi")
    from brigade.auth import issue_token
    from brigade.config import Settings
    from brigade.web import create_app

    store = _store(tmp_path)
    _team_fleet(store, teams=2)
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
    app = create_app(settings, store)
    headers = {
        "owner": {"Authorization": f"Bearer {issue_token(settings, owner)}"},
        "observer": {"Authorization": f"Bearer {issue_token(settings, observer)}"},
    }
    return app, store, headers


def test_thread_routes_round_trip(tmp_path):
    from tests.test_v0_9 import _asgi_request

    app, store, headers = _auth_app(tmp_path)

    opened = asyncio.run(
        _asgi_request(
            app,
            "POST",
            "/api/chat/threads",
            headers=headers["owner"],
            json_payload={"persona": "chief:chief0"},
        )
    )
    assert opened.status_code == 200, opened.text
    thread = opened.json()
    assert thread["persona"] == "chief:chief0"
    assert thread["operator_username"] == "owner"

    # Get-or-create: same persona resolves the same thread.
    again = asyncio.run(
        _asgi_request(
            app,
            "POST",
            "/api/chat/threads",
            headers=headers["owner"],
            json_payload={"persona": "chief:chief0"},
        )
    )
    assert again.json()["thread_id"] == thread["thread_id"]

    listed = asyncio.run(
        _asgi_request(app, "GET", "/api/chat/threads", headers=headers["owner"])
    )
    assert listed.status_code == 200
    payload = listed.json()
    assert [item["thread_id"] for item in payload["threads"]] == [thread["thread_id"]]
    assert payload["personas"][0]["persona_id"] == "front_desk"

    store.add_message(
        ChatMessage(
            channel=thread["channel"],
            sender="owner",
            recipient="chief0",
            content="hello chief",
        )
    )
    messages = asyncio.run(
        _asgi_request(
            app,
            "GET",
            f"/api/chat/threads/{thread['thread_id']}/messages",
            headers=headers["owner"],
        )
    )
    assert messages.status_code == 200
    assert [item["content"] for item in messages.json()["messages"]] == ["hello chief"]

    switched = asyncio.run(
        _asgi_request(
            app,
            "POST",
            f"/api/chat/threads/{thread['thread_id']}/persona",
            headers=headers["owner"],
            json_payload={"persona": "frontdesk"},
        )
    )
    assert switched.status_code == 200
    assert switched.json()["persona"] == "front_desk"
    assert switched.json()["thread_id"] != thread["thread_id"]


def test_thread_routes_reject_unknown_and_unauthorized(tmp_path):
    from tests.test_v0_9 import _asgi_request

    app, _store_unused, headers = _auth_app(tmp_path)

    bad_persona = asyncio.run(
        _asgi_request(
            app,
            "POST",
            "/api/chat/threads",
            headers=headers["owner"],
            json_payload={"persona": "nobody"},
        )
    )
    assert bad_persona.status_code == 400

    missing = asyncio.run(
        _asgi_request(
            app,
            "GET",
            "/api/chat/threads/nope/messages",
            headers=headers["owner"],
        )
    )
    assert missing.status_code == 404

    observer_open = asyncio.run(
        _asgi_request(
            app,
            "POST",
            "/api/chat/threads",
            headers=headers["observer"],
            json_payload={"persona": "front_desk"},
        )
    )
    assert observer_open.status_code == 403

    observer_list = asyncio.run(
        _asgi_request(app, "GET", "/api/chat/threads", headers=headers["observer"])
    )
    assert observer_list.status_code == 200
