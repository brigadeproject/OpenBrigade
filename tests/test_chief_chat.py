"""Release 1.1 phase 2: the chief chat tool-use agent loop."""

from __future__ import annotations

import json

from brigade.chief_chat import (
    parse_chief_chat_reply,
    resolve_persona,
    run_chief_chat_turn,
)
from brigade.schemas import Agent, Assignment, AssignmentStatus, Team
from brigade.state import JsonStateStore
from tests.helpers import SequencedTestProvider


def _fleet(tmp_path, teams: int = 1):
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


def _turn(store, provider, *, persona_name="chief0", content="what's up?", **kwargs):
    persona = resolve_persona(store, persona_name)
    thread = store.resolve_active_conversation("owner", persona.persona_id)
    return run_chief_chat_turn(
        store,
        thread=thread,
        persona=persona,
        operator="owner",
        content=content,
        provider=provider,
        **kwargs,
    )


def _tool_call(tool: str, **arguments) -> str:
    return json.dumps({"status": "tool_call", "tool": tool, "arguments": arguments})


def test_parse_chief_chat_reply_variants():
    tool = parse_chief_chat_reply(_tool_call("list_tasks", status="blocked"))
    assert tool.kind == "tool_call"
    assert tool.tool_name == "list_tasks"
    assert tool.tool_arguments == {"status": "blocked"}

    actions = parse_chief_chat_reply(
        json.dumps(
            {
                "status": "propose_actions",
                "summary": "Cancel it.",
                "actions": [{"type": "cancel_assignment", "assignment_id": "a1"}],
            }
        )
    )
    assert actions.kind == "actions"
    assert actions.actions[0]["type"] == "cancel_assignment"

    assert parse_chief_chat_reply("All quiet on the team.").kind == "text"
    assert parse_chief_chat_reply('{"status":"tool_call"}').kind == "invalid"
    # Reasoning-only completions arrive as empty text; retry, don't store them.
    assert parse_chief_chat_reply("").kind == "invalid"
    assert parse_chief_chat_reply("  \n").kind == "invalid"
    # A propose_actions envelope without valid actions degrades to prose.
    empty = parse_chief_chat_reply('{"status":"propose_actions","summary":"hm","actions":[]}')
    assert empty.kind == "text"
    assert empty.text == "hm"


def test_tool_call_then_answer(tmp_path):
    store = _fleet(tmp_path)
    task = Assignment(
        assignment="Fix the flaky test",
        assigned_to="worker0",
        created_by="human",
        source="direct_command",
        status=AssignmentStatus.BLOCKED,
    )
    store.add_assignment(task)
    provider = SequencedTestProvider(
        [
            _tool_call("list_tasks", status="blocked"),
            "One blocked task: fix the flaky test.",
        ]
    )

    result = _turn(store, provider, content="what's blocked on your team?")

    assert result["status"] == "complete"
    assert result["tools_used"] == ["list_tasks"]
    assert result["iterations"] == 2
    # The second completion saw the first tool's observation.
    second_prompt = provider.calls[1]["prompt"]
    assert "tool_observations" in second_prompt
    assert task.assignment_id in second_prompt
    # Request + response persisted on the thread channel.
    messages = store.messages(result["conversation_id"])
    assert [item.metadata["kind"] for item in messages] == [
        "chief_chat_request",
        "chief_chat_response",
    ]
    assert "flaky test" in messages[-1].content
    # One usage record per iteration, tagged chief_chat.
    usage = [item for item in store.usage_records() if item["source"] == "chief_chat"]
    assert len(usage) == 2
    # The turn left an episode behind.
    assert store.episodes()[-1]["source"] == "chief_chat"


def test_empty_reply_retries_then_answers(tmp_path):
    store = _fleet(tmp_path)
    provider = SequencedTestProvider(["", "All quiet on the team."])

    result = _turn(store, provider)

    assert result["status"] == "complete"
    assert result["iterations"] == 2
    messages = store.messages(result["conversation_id"])
    assert messages[-1].content == "All quiet on the team."


def test_all_empty_replies_still_persist_a_response(tmp_path):
    store = _fleet(tmp_path)
    provider = SequencedTestProvider(["", "", ""])

    result = _turn(store, provider, max_iterations=3)

    assert result["status"] == "complete"
    messages = store.messages(result["conversation_id"])
    assert messages[-1].metadata["kind"] == "chief_chat_response"
    assert messages[-1].content.strip()


def test_chief_scope_blocks_other_teams_tasks(tmp_path):
    store = _fleet(tmp_path, teams=2)
    foreign = Assignment(
        assignment="Other team's work",
        assigned_to="worker1",
        created_by="human",
        source="direct_command",
    )
    store.add_assignment(foreign)
    provider = SequencedTestProvider(
        [
            _tool_call("get_task", assignment_id=foreign.assignment_id),
            "I cannot see that task.",
        ]
    )

    result = _turn(store, provider, persona_name="chief0")

    assert result["status"] == "complete"
    second_prompt = provider.calls[1]["prompt"]
    assert "outside your team" in second_prompt
    # list_tasks likewise hides the other team's work.
    provider = SequencedTestProvider([_tool_call("list_tasks"), "Nothing to show."])
    _turn(store, provider, persona_name="chief0", content="list everything")
    listing = provider.calls[1]["prompt"]
    assert foreign.assignment_id not in listing


def test_front_desk_sees_the_whole_fleet(tmp_path):
    store = _fleet(tmp_path, teams=2)
    foreign = Assignment(
        assignment="Any team's work",
        assigned_to="worker1",
        created_by="human",
        source="direct_command",
    )
    store.add_assignment(foreign)
    provider = SequencedTestProvider([_tool_call("list_tasks"), "One task fleet-wide."])

    result = _turn(store, provider, persona_name="front_desk")

    assert result["agent_id"] == "front_desk"
    assert foreign.assignment_id in provider.calls[1]["prompt"]


def test_budget_exhaustion_answers_from_observations(tmp_path):
    store = _fleet(tmp_path)
    provider = SequencedTestProvider(
        [
            _tool_call("team_status"),
            _tool_call("team_status"),  # ignores the demand for a final answer
        ]
    )

    result = _turn(store, provider, max_iterations=2)

    assert result["status"] == "complete"
    assert provider.responses == []
    assert "final answer" in provider.calls[1]["prompt"]
    messages = store.messages(result["conversation_id"])
    assert "ran out of tool budget" in messages[-1].content
    assert "team_status" in messages[-1].content


def test_invalid_reply_gets_corrective_observation(tmp_path):
    store = _fleet(tmp_path)
    provider = SequencedTestProvider(
        ['{"status":"tool_call"}', "Sorry — all quiet on the team."]
    )

    result = _turn(store, provider)

    assert result["status"] == "complete"
    assert "not usable" in provider.calls[1]["prompt"]
    assert result["tools_used"] == []


def test_native_tools_passed_only_when_supported(tmp_path):
    store = _fleet(tmp_path)
    native = SequencedTestProvider(["All quiet."], supports_native_tools=True)
    _turn(store, native)
    assert native.calls[0]["tools"], "native provider should receive tool specs"
    assert native.calls[0]["tools"][0]["type"] == "function"

    plain = SequencedTestProvider(["All quiet."], supports_native_tools=False)
    _turn(store, plain)
    assert plain.calls[0]["tools"] is None


def test_provider_failure_returns_blocked(tmp_path):
    store = _fleet(tmp_path)
    provider = SequencedTestProvider([])  # first call raises

    result = _turn(store, provider)

    assert result["status"] == "blocked"
    assert "scripted responses" in result["summary"]
    assert any("chief chat" in alert for alert in store.alerts())


def test_idempotent_requests_are_deduplicated(tmp_path):
    store = _fleet(tmp_path)
    provider = SequencedTestProvider(["All quiet.", "should never be used"])

    first = _turn(store, provider, idempotency_key="abc")
    second = _turn(store, provider, idempotency_key="abc")

    assert first["status"] == "complete"
    assert second["status"] == "duplicate"
    assert len(provider.calls) == 1


def test_web_fetch_tool_fetches_when_enabled(tmp_path, monkeypatch):
    import io

    import brigade.tools as tools

    class _FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=0):
        assert request.full_url == "https://example.com/status"
        return _FakeResponse(b"release notes: all green")

    monkeypatch.setattr(tools.urllib.request, "urlopen", fake_urlopen)
    store = _fleet(tmp_path)
    provider = SequencedTestProvider(
        [
            _tool_call("web_fetch", url="https://example.com/status"),
            "The page says all green.",
        ]
    )

    result = _turn(store, provider, content="check https://example.com/status")

    assert result["tools_used"] == ["web_fetch"]
    assert "release notes: all green" in provider.calls[1]["prompt"]


def test_web_fetch_absent_when_disabled(tmp_path):
    store = _fleet(tmp_path)
    provider = SequencedTestProvider(["All quiet."])
    _turn(store, provider, enable_web_fetch=False)
    assert "web_fetch" not in provider.calls[0]["prompt"]

    enabled = SequencedTestProvider(["All quiet."])
    _turn(store, enabled, content="again")
    assert "web_fetch" in enabled.calls[0]["prompt"]


def test_list_recurrences_tool_is_team_scoped(tmp_path):
    from brigade.schemas import build_recurrence
    from brigade.time import add_seconds_iso, utc_now_iso

    store = _fleet(tmp_path, teams=2)
    store.add_recurrence(
        build_recurrence(
            template={"assignment": "Daily briefing for my team",
                      "assigned_to": "worker0"},
            interval_seconds=86400,
            next_due_at=add_seconds_iso(utc_now_iso(), 3600),
        )
    )
    store.add_recurrence(
        build_recurrence(
            template={"assignment": "Other team's job", "assigned_to": "worker1"},
            interval_seconds=86400,
            next_due_at=add_seconds_iso(utc_now_iso(), 3600),
        )
    )
    provider = SequencedTestProvider(
        [_tool_call("list_recurrences"), "One scheduled job."]
    )

    _turn(store, provider, content="what's scheduled?")

    observed = provider.calls[1]["prompt"]
    assert "Daily briefing for my team" in observed
    assert "Other team's job" not in observed


def test_history_window_carries_prior_turns(tmp_path):
    store = _fleet(tmp_path)
    provider = SequencedTestProvider(["Noted, the demo is Friday."])
    _turn(store, provider, content="heads up: the demo is Friday")

    provider = SequencedTestProvider(["You told me the demo is Friday."])
    _turn(store, provider, content="when is the demo?")

    prompt = provider.calls[0]["prompt"]
    assert "recent_thread_history" in prompt
    assert "heads up: the demo is Friday" in prompt
    # The current request is not duplicated into history.
    assert prompt.count("when is the demo?") == 1


def test_history_window_is_bounded(tmp_path):
    store = _fleet(tmp_path)
    for index in range(6):
        provider = SequencedTestProvider([f"reply {index}"])
        _turn(store, provider, content=f"question {index}", history_window=2)

    prompt = provider.calls[0]["prompt"]
    assert "reply 4" in prompt  # inside the 2-message window
    assert "question 0" not in prompt  # far outside it


def test_remember_round_trips_into_next_turn(tmp_path):
    from brigade.prompt_floors import read_agent_chat_notes

    store = _fleet(tmp_path)
    provider = SequencedTestProvider(
        [
            json.dumps(
                {
                    "status": "tool_call",
                    "tool": "remember",
                    "arguments": {"note": "Operator prefers Friday demos."},
                }
            ),
            "Remembered.",
        ]
    )
    _turn(store, provider, content="remember that I prefer Friday demos")

    assert "Friday demos" in read_agent_chat_notes(store, "chief0")

    provider = SequencedTestProvider(["You prefer Friday demos."])
    _turn(store, provider, content="what do I prefer?")
    prompt = provider.calls[0]["prompt"]
    assert "curated_notes" in prompt
    assert "Operator prefers Friday demos." in prompt


def test_episode_recall_block_present_only_on_match(tmp_path):
    store = _fleet(tmp_path)
    store.add_episode(
        {
            "episode_id": "e1",
            "agent_id": "chief0",
            "created_at": "2026-01-01T00:00:00+00:00",
            "source": "chief_chat",
            "conversation_id": "thread:old",
            "summary": "Shipped the ingest pipeline rewrite",
        }
    )
    provider = SequencedTestProvider(["We shipped it in January."])
    _turn(store, provider, content="ingest pipeline rewrite status?")
    assert "possibly_relevant_past_episodes" in provider.calls[0]["prompt"]
    assert "Shipped the ingest pipeline rewrite" in provider.calls[0]["prompt"]

    provider = SequencedTestProvider(["All quiet."])
    _turn(store, provider, content="zzz unrelated zzz")
    assert "possibly_relevant_past_episodes" not in provider.calls[0]["prompt"]


def test_rolling_summary_refreshes_and_feeds_back(tmp_path):
    store = _fleet(tmp_path)
    # Window of 1: each turn adds 2 messages, so the 2x threshold trips on
    # turn two and the provider gets an extra summary completion.
    provider = SequencedTestProvider(["first reply"])
    _turn(store, provider, content="first question", history_window=1)

    provider = SequencedTestProvider(
        ["second reply", "- demo Friday\n- task t1 blocked"]
    )
    result = _turn(store, provider, content="second question", history_window=1)

    thread = store.find_conversation(result["conversation_id"].removeprefix("thread:"))
    assert thread.rolling_summary == "- demo Friday\n- task t1 blocked"
    assert "Summarize this operator<->chief conversation" in provider.calls[1]["prompt"]

    provider = SequencedTestProvider(["third reply", "- summary again"])
    _turn(store, provider, content="third question", history_window=1)
    assert "conversation_summary" in provider.calls[0]["prompt"]
    assert "demo Friday" in provider.calls[0]["prompt"]


def test_summary_refresh_failure_never_blocks_the_turn(tmp_path):
    store = _fleet(tmp_path)
    provider = SequencedTestProvider(["first reply"])
    _turn(store, provider, content="first question", history_window=1)

    # Only one scripted response: the summary refresh call raises.
    provider = SequencedTestProvider(["second reply"])
    result = _turn(store, provider, content="second question", history_window=1)

    assert result["status"] == "complete"
    thread = store.find_conversation(result["conversation_id"].removeprefix("thread:"))
    assert thread.rolling_summary == ""


def test_thread_send_route_runs_the_loop(tmp_path, monkeypatch):
    import pytest

    pytest.importorskip("fastapi")
    import asyncio

    import brigade.web as web
    from brigade.auth import issue_token
    from brigade.config import Settings
    from brigade.schemas import Role, User
    from tests.test_v0_9 import _asgi_request

    store = _fleet(tmp_path)
    owner = User(username="owner", role=Role.OWNER)
    store.add_user(owner)
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        require_auth=True,
        jwt_secret="x" * 40,
        allow_json_store=True,
    )
    provider = SequencedTestProvider([_tool_call("team_status"), "All quiet."])
    monkeypatch.setattr(web, "_provider_from_payload", lambda payload, settings: provider)
    app = web.create_app(settings, store)
    headers = {"Authorization": f"Bearer {issue_token(settings, owner)}"}

    opened = asyncio.run(
        _asgi_request(
            app,
            "POST",
            "/api/chat/threads",
            headers=headers,
            json_payload={"persona": "chief0"},
        )
    )
    thread_id = opened.json()["thread_id"]

    sent = asyncio.run(
        _asgi_request(
            app,
            "POST",
            f"/api/chat/threads/{thread_id}/messages",
            headers=headers,
            json_payload={"content": "how is the team doing?"},
        )
    )
    assert sent.status_code == 200, sent.text
    body = sent.json()
    assert body["status"] == "complete"
    assert body["tools_used"] == ["team_status"]

    # Another user cannot post into this thread.
    stranger = User(username="stranger", role=Role.OWNER)
    store.add_user(stranger)
    stranger_headers = {"Authorization": f"Bearer {issue_token(settings, stranger)}"}
    denied = asyncio.run(
        _asgi_request(
            app,
            "POST",
            f"/api/chat/threads/{thread_id}/messages",
            headers=stranger_headers,
            json_payload={"content": "hi"},
        )
    )
    assert denied.status_code == 403


def test_usage_summary_and_episode_tools_answer(tmp_path):
    store = _fleet(tmp_path)
    store.add_usage_record(
        {
            "usage_id": "u1",
            "agent_id": "worker0",
            "provider": "ollama",
            "model": "qwen",
            "route_type": "local",
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "estimated_cost_usd": 0.0,
            "recorded_at": "2099-01-01T00:00:00+00:00",
            "source": "agent",
        }
    )
    store.add_episode(
        {
            "episode_id": "e1",
            "agent_id": "chief0",
            "created_at": "2099-01-01T00:00:00+00:00",
            "source": "chief_chat",
            "conversation_id": "thread:x",
            "summary": "Shipped the report pipeline",
        }
    )
    provider = SequencedTestProvider(
        [
            _tool_call("usage_summary", days=30000),
            _tool_call("search_episodes", query="report pipeline"),
            "Usage and history located.",
        ]
    )

    result = _turn(store, provider, content="what did we spend and ship?")

    assert result["tools_used"] == ["usage_summary", "search_episodes"]
    final_prompt = provider.calls[2]["prompt"]
    assert "ollama:qwen" in final_prompt
    assert "Shipped the report pipeline" in final_prompt
