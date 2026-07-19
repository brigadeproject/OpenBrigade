"""Release 1.1 phase 5: connector routing and persona switching."""

from __future__ import annotations

from functools import partial

from brigade.chief_chat import parse_control_command, run_connector_chief_chat
from brigade.connectors import (
    ConnectorResult,
    IncomingConnectorMessage,
    approve_external_identity,
    process_live_connector_message,
)
from brigade.schemas import Agent, Assignment, AssignmentStatus, Team
from brigade.state import JsonStateStore
from tests.helpers import SequencedTestProvider, TestProvider


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
    approve_external_identity(
        store,
        provider="telegram",
        external_user_id="42",
        username="alice",
        decided_by="owner",
    )
    return store


def _incoming(text: str, message_id: str = "m1") -> IncomingConnectorMessage:
    return IncomingConnectorMessage(
        provider="telegram",
        external_user_id="42",
        conversation_id="chat7",
        external_message_id=message_id,
        text=text,
        channel="telegram:chat7",
        reply_target="chat7",
        metadata={"username": "alice"},
    )


def _run(store, provider, text, *, message_id="m1", **kwargs):
    sent: list[str] = []

    def outbound(incoming, reply_text):
        sent.append(reply_text)
        return ConnectorResult(incoming.provider, "sent", incoming.channel)

    chat_turn = partial(run_connector_chief_chat, provider=provider, **kwargs)
    result = process_live_connector_message(
        store,
        _incoming(text, message_id),
        default_agent="chief0",
        model_provider=provider,
        outbound_sender=outbound,
        allowlist=None,
        chat_turn=chat_turn,
    )
    return result, sent


def test_parse_control_command():
    assert parse_control_command("/frontdesk").verb == "frontdesk"
    assert parse_control_command("/who").verb == "who"
    assert parse_control_command("/new").verb == "new"
    chief = parse_control_command("/chief sales team")
    assert chief.verb == "chief"
    assert chief.argument == "sales team"
    assert parse_control_command("/chief").argument == ""
    assert parse_control_command("hello there") is None
    assert parse_control_command("/unknown") is None


def test_approved_identity_creates_thread_and_replies(tmp_path):
    store = _fleet(tmp_path, teams=1)
    task = Assignment(
        assignment="Ship it",
        assigned_to="worker0",
        created_by="human",
        source="direct_command",
        status=AssignmentStatus.BLOCKED,
    )
    store.add_assignment(task)
    provider = SequencedTestProvider(['{"status":"tool_call","tool":"list_tasks",'
                                       '"arguments":{}}', "One blocked task."])

    result, sent = _run(store, provider, "what's blocked?")

    assert result.status == "complete"
    assert sent == ["One blocked task."]
    # A durable thread now exists for this operator, shared with the SPA.
    threads = store.conversations("alice", status="active")
    assert len(threads) == 1
    assert threads[0].persona == "chief:chief0"
    # And its history holds the chief-chat request/response.
    kinds = [item.metadata.get("kind") for item in store.messages(threads[0].channel)]
    assert "chief_chat_request" in kinds
    assert "chief_chat_response" in kinds


def test_control_commands_switch_persona(tmp_path):
    store = _fleet(tmp_path, teams=2)

    # Default (two teams) => front desk.
    provider = TestProvider(text="Fleet-wide answer.")
    result, sent = _run(store, provider, "/who", message_id="c1")
    assert "Front desk" in sent[0]

    # Switch to a specific chief.
    result, sent = _run(store, provider, "/chief team1", message_id="c2")
    assert "Switched to" in sent[0]
    active = store.conversations("alice", status="active")
    assert active[0].persona == "chief:chief1"

    # A plain message now continues in that persona's thread.
    provider = TestProvider(text="Team 1 is busy.")
    result, sent = _run(store, provider, "how's the team?", message_id="c3")
    assert sent == ["Team 1 is busy."]
    # /who reflects the switch.
    result, sent = _run(store, TestProvider(text="x"), "/who", message_id="c4")
    assert "chief1".upper() in sent[0] or "Team 1" in sent[0]


def test_new_command_archives_and_starts_fresh(tmp_path):
    store = _fleet(tmp_path, teams=1)
    _run(store, TestProvider(text="hi"), "first message", message_id="n1")
    first = store.conversations("alice", status="active")[0]

    _run(store, TestProvider(text="ok"), "/new", message_id="n2")

    active = store.conversations("alice", status="active")
    assert len(active) == 1
    assert active[0].thread_id != first.thread_id
    assert store.find_conversation(first.thread_id).status == "archived"


def test_unknown_chief_is_reported_not_applied(tmp_path):
    store = _fleet(tmp_path, teams=2)
    result, sent = _run(store, TestProvider(text="x"), "/chief nobody")
    assert "Sorry" in sent[0]
    # No thread switch happened for a bad persona.
    assert store.conversations("alice", status="active") == []


def test_flag_off_falls_back_to_default_agent(tmp_path):
    store = _fleet(tmp_path, teams=1)
    provider = TestProvider(text="single-shot reply")
    sent: list[str] = []

    def outbound(incoming, reply_text):
        sent.append(reply_text)
        return ConnectorResult(incoming.provider, "sent", incoming.channel)

    # chat_turn=None is the default single-shot path.
    result = process_live_connector_message(
        store,
        _incoming("hello"),
        default_agent="chief0",
        model_provider=provider,
        outbound_sender=outbound,
        allowlist=None,
    )
    assert result.status == "complete"
    # No chief-chat thread was created.
    assert store.conversations("alice") == []
    # The outbound rode the legacy external_outbound channel, not a thread.
    outbound_messages = [
        item for item in store.messages() if item.metadata.get("kind") == "external_outbound"
    ]
    assert len(outbound_messages) == 1


def test_pending_identity_never_reaches_chat_turn(tmp_path):
    store = _fleet(tmp_path, teams=1)
    called = {"turn": False}

    def chat_turn(store_, incoming, username):  # pragma: no cover - must not run
        called["turn"] = True
        raise AssertionError("chat_turn ran for a pending identity")

    incoming = IncomingConnectorMessage(
        provider="telegram",
        external_user_id="999",  # not approved
        conversation_id="chatX",
        external_message_id="p1",
        text="hi",
        channel="telegram:chatX",
        reply_target="chatX",
    )
    result = process_live_connector_message(
        store,
        incoming,
        default_agent="chief0",
        model_provider=TestProvider(text="x"),
        outbound_sender=lambda i, t: ConnectorResult(i.provider, "sent", i.channel),
        allowlist=None,
        chat_turn=chat_turn,
    )
    assert result.status == "pending_approval"
    assert called["turn"] is False


def test_connector_and_spa_share_one_thread(tmp_path):
    store = _fleet(tmp_path, teams=1)
    # The connector opens the operator's chief thread.
    _run(store, TestProvider(text="hi"), "hello over telegram", message_id="s1")
    thread = store.conversations("alice", status="active")[0]

    # The SPA's get-or-create for the same operator+persona resolves the SAME
    # thread (this is exactly what POST /api/chat/threads does).
    resolved = store.resolve_active_conversation("alice", "chief:chief0")
    assert resolved.thread_id == thread.thread_id
