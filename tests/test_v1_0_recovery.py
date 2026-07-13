"""Stuck-agent recovery: provider hardening, cancel/reissue, hung detection,
occupancy release, and operator escalation."""

from __future__ import annotations

import json

import pytest

from brigade.connectors import ConnectorResult
from brigade.orchestrator import (
    OrchestrationConfig,
    _notify_operator_escalations,
    deterministic_cycle,
    recover_hung_tasks,
)
from brigade.providers import ModelUnavailableError, OllamaProvider
from brigade.rest import rest_assignment_text
from brigade.runner import parse_agent_response
from brigade.schemas import (
    Agent,
    Assignment,
    AssignmentStatus,
    Priority,
    extract_json_object,
)
from brigade.services import (
    AssignmentActionError,
    assignment_relations,
    cancel_assignment,
    reissue_assignment,
)
from brigade.state import JsonStateStore
from brigade.tools import ToolContext, default_tool_registry


# --- helpers ---------------------------------------------------------------------


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._data = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._data


def _store(tmp_path) -> JsonStateStore:
    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(Agent("ada", "ADA", "workspace-ada"))
    store.add_agent(Agent("lin", "LIN", "workspace-lin"))
    return store


def _assignment(store: JsonStateStore, *, agent: str = "ada", **kwargs) -> Assignment:
    assignment = Assignment(
        assignment=kwargs.pop("text", "do the work"),
        assigned_to=agent,
        created_by="human",
        source="direct_command",
        **kwargs,
    )
    store.add_assignment(assignment)
    return assignment


def _assigned(store: JsonStateStore, **kwargs) -> Assignment:
    assignment = _assignment(store, **kwargs)
    assignment.transition_to(AssignmentStatus.ASSIGNED)
    store.update_assignment(assignment)
    return assignment


def _blocked(store: JsonStateStore, *, awaiting_human: bool = False, **kwargs) -> Assignment:
    assignment = _assigned(store, **kwargs)
    assignment.register_failure("boom", blockers=["broken"])
    assignment.awaiting_human = awaiting_human
    store.update_assignment(assignment)
    return assignment


# --- provider hardening ----------------------------------------------------------


def test_ollama_provider_uses_chat_endpoint_and_reads_content(monkeypatch):
    captured: dict = {}

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResp(
            {"message": {"content": "the answer"}, "prompt_eval_count": 5, "eval_count": 7}
        )

    monkeypatch.setattr("brigade.providers.urllib.request.urlopen", fake_urlopen)
    response = OllamaProvider(model="qwen2.5-coder:7b").complete("hello")

    assert response.text == "the answer"
    assert response.input_tokens == 5 and response.output_tokens == 7
    assert captured["url"].endswith("/api/chat")
    assert captured["body"]["messages"][0]["content"] == "hello"


def test_ollama_provider_empty_content_raises(monkeypatch):
    def fake_urlopen(request, timeout=0):
        return _FakeResp({"message": {"content": "", "thinking": "...reasoning..."}})

    monkeypatch.setattr("brigade.providers.urllib.request.urlopen", fake_urlopen)
    with pytest.raises(ModelUnavailableError):
        OllamaProvider(model="gpt-oss:20b").complete("hello")


# --- cancel / reissue ------------------------------------------------------------


def test_cancel_queued_becomes_superseded_and_archived(tmp_path):
    store = _store(tmp_path)
    queued = _assignment(store)
    result = cancel_assignment(store, queued.assignment_id, by="op")
    assert result["status"] == AssignmentStatus.SUPERSEDED.value
    assert store.find_assignment(queued.assignment_id) is None


def test_cancel_blocked_becomes_abandoned(tmp_path):
    store = _store(tmp_path)
    blocked = _blocked(store)
    result = cancel_assignment(store, blocked.assignment_id)
    assert result["status"] == AssignmentStatus.ABANDONED.value


def test_cancel_refuses_to_orphan_children_without_force(tmp_path):
    store = _store(tmp_path)
    parent = _assigned(store)
    _assignment(store, agent="lin", parent_assignment_id=parent.assignment_id)

    with pytest.raises(AssignmentActionError):
        cancel_assignment(store, parent.assignment_id)

    result = cancel_assignment(store, parent.assignment_id, force=True)
    assert result["status"] == AssignmentStatus.ABANDONED.value
    assert len(result["orphaned_children"]) == 1


def test_cancel_releases_dependents(tmp_path):
    store = _store(tmp_path)
    dependency = _assignment(store)
    dependent = _assignment(store, agent="lin", dependency_ids=[dependency.assignment_id])

    cancel_assignment(store, dependency.assignment_id, force=True)

    refreshed = store.find_assignment(dependent.assignment_id)
    assert dependency.assignment_id not in refreshed.dependency_ids


def test_reissue_resets_blocked_to_assigned(tmp_path):
    store = _store(tmp_path)
    blocked = _blocked(store, awaiting_human=True)
    assert blocked.consecutive_failures == 1

    result = reissue_assignment(store, blocked.assignment_id, by="op")

    refreshed = store.find_assignment(blocked.assignment_id)
    assert result["status"] == AssignmentStatus.ASSIGNED.value
    assert refreshed.status == AssignmentStatus.ASSIGNED
    assert refreshed.consecutive_failures == 0
    assert refreshed.awaiting_human is False
    assert refreshed.blockers == []


def test_reissue_rejects_non_blocked(tmp_path):
    store = _store(tmp_path)
    queued = _assignment(store)
    with pytest.raises(AssignmentActionError):
        reissue_assignment(store, queued.assignment_id)


def test_assignment_relations_finds_children_and_dependents(tmp_path):
    store = _store(tmp_path)
    target = _assignment(store)
    child = _assignment(store, agent="lin", parent_assignment_id=target.assignment_id)
    dependent = _assignment(store, agent="lin", dependency_ids=[target.assignment_id])

    relations = assignment_relations(store, target.assignment_id)
    child_ids = {a.assignment_id for a in relations["children"]}
    dependent_ids = {a.assignment_id for a in relations["dependents"]}
    assert child.assignment_id in child_ids
    assert dependent.assignment_id in dependent_ids


# --- hung-task recovery (hybrid by severity) -------------------------------------


def _hung_config() -> OrchestrationConfig:
    # hung_task_seconds=-1 makes any in-flight task with no future checkpoint "hung".
    return OrchestrationConfig(hung_task_seconds=-1, auto_recover_enabled=True)


def test_recover_transient_hung_routes_to_ladder(tmp_path):
    store = _store(tmp_path)
    hung = _assigned(store)  # no parent, children, or dependents

    result = recover_hung_tasks(store, _hung_config())

    assert result["actions"][0]["classification"] == "transient"
    refreshed = store.find_assignment(hung.assignment_id)
    assert refreshed.status == AssignmentStatus.BLOCKED
    assert refreshed.awaiting_human is False  # ladder will retry it


def test_recover_structural_hung_escalates(tmp_path):
    store = _store(tmp_path)
    parent = _assigned(store)
    _assignment(store, agent="lin", parent_assignment_id=parent.assignment_id)

    result = recover_hung_tasks(store, _hung_config())

    parent_action = next(
        a for a in result["actions"] if a["assignment_id"] == parent.assignment_id
    )
    assert parent_action["classification"] == "structural"
    refreshed = store.find_assignment(parent.assignment_id)
    assert refreshed.status == AssignmentStatus.BLOCKED
    assert refreshed.awaiting_human is True  # parked for the operator, not killed


def test_recover_disabled_is_noop(tmp_path):
    store = _store(tmp_path)
    hung = _assigned(store)
    result = recover_hung_tasks(
        store, OrchestrationConfig(hung_task_seconds=-1, auto_recover_enabled=False)
    )
    assert result["enabled"] is False
    assert store.find_assignment(hung.assignment_id).status == AssignmentStatus.ASSIGNED


# --- occupancy release (the "all agents stuck" fix) ------------------------------


def test_awaiting_human_blocked_task_frees_agent(tmp_path):
    store = _store(tmp_path)
    _blocked(store, agent="ada", awaiting_human=True)
    queued = _assignment(store, agent="ada", text="fresh work")

    result = deterministic_cycle(store.assignments(), agents=store.agents())

    assigned_ids = {a.assignment_id for a in result.assigned}
    assert queued.assignment_id in assigned_ids


def test_active_ladder_blocked_task_still_occupies_agent(tmp_path):
    store = _store(tmp_path)
    _blocked(store, agent="ada", awaiting_human=False)
    queued = _assignment(store, agent="ada", text="fresh work")

    result = deterministic_cycle(store.assignments(), agents=store.agents())

    assigned_ids = {a.assignment_id for a in result.assigned}
    assert queued.assignment_id not in assigned_ids


# --- operator escalation (outbound + de-dupe) ------------------------------------


def test_notify_operator_sends_telegram_for_awaiting_human(tmp_path, monkeypatch):
    store = _store(tmp_path)
    blocked = _blocked(store, awaiting_human=True)
    sent: list = []

    def fake_send(bot_token, *, chat_id, text, http_post=None):
        sent.append((bot_token, chat_id, text))
        return ConnectorResult("telegram", "sent")

    monkeypatch.setattr("brigade.connectors.send_telegram_message", fake_send)
    config = OrchestrationConfig(
        telegram_bot_token="botto", operator_telegram_chat_id="42"
    )

    result = _notify_operator_escalations(store, config)

    assert len(result["notified"]) == 1
    assert sent and sent[0][1] == "42"
    assert blocked.assignment_id in sent[0][2]


def test_notify_operator_dedupes_on_prior_record(tmp_path, monkeypatch):
    store = _store(tmp_path)
    blocked = _blocked(store, awaiting_human=True)
    store.add_orchestrator_reasoning(
        {"note": f"operator-notify:v2:{blocked.assignment_id}"}
    )
    sent: list = []
    monkeypatch.setattr(
        "brigade.connectors.send_telegram_message",
        lambda *a, **k: sent.append(1) or ConnectorResult("telegram", "sent"),
    )

    result = _notify_operator_escalations(store, OrchestrationConfig())

    assert result["notified"] == []
    assert sent == []


def test_notify_operator_posts_to_orchestrator_chat_without_telegram(tmp_path):
    store = _store(tmp_path)
    blocked = _blocked(store, awaiting_human=True)

    result = _notify_operator_escalations(store, OrchestrationConfig())

    # No Telegram configured: the orchestrator chat is still notified, so the
    # escalation is visible in the surface the operator actually watches.
    delivery = result["notified"][0]["delivery"]
    assert delivery["status"] == "sent"
    assert delivery["channel"] == "chat"
    by_channel = {entry["channel"]: entry for entry in delivery["channels"]}
    assert by_channel["chat"]["status"] == "sent"
    assert by_channel["telegram"]["status"] == "skipped"
    chat = store.messages("orchestrator")
    assert len(chat) == 1
    assert chat[0].sender == "orchestrator"
    assert blocked.assignment_id in chat[0].content
    assert chat[0].metadata["kind"] == "operator_notification"


def test_notify_operator_sends_chat_and_telegram_when_configured(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _blocked(store, awaiting_human=True)
    monkeypatch.setattr(
        "brigade.connectors.send_telegram_message",
        lambda *a, **k: ConnectorResult("telegram", "sent"),
    )
    config = OrchestrationConfig(
        telegram_bot_token="botto", operator_telegram_chat_id="42"
    )

    result = _notify_operator_escalations(store, config)

    delivery = result["notified"][0]["delivery"]
    assert delivery["channel"] == "chat+telegram"
    assert delivery["status"] == "sent"
    assert len(store.messages("orchestrator")) == 1


# --- JSON extraction robustness (model-output tolerance) -------------------------


def test_extract_json_handles_bare_object():
    assert extract_json_object('{"a": 1}') == '{"a": 1}'


def test_extract_json_strips_markdown_fence():
    assert extract_json_object('```json\n{"status": "complete"}\n```') == (
        '{"status": "complete"}'
    )


def test_extract_json_finds_object_in_prose():
    text = 'Sure! Result: {"status": "complete", "summary": "ok"} Done.'
    assert extract_json_object(text) == '{"status": "complete", "summary": "ok"}'


def test_extract_json_ignores_braces_inside_strings():
    text = '{"summary": "use {curly} braces"}'
    assert extract_json_object(text) == text


def test_parse_agent_response_accepts_fenced_json():
    parsed = parse_agent_response('```json\n{"status":"complete","summary":"did it"}\n```')
    assert parsed.status == "complete"
    assert parsed.summary == "did it"


def test_parse_agent_response_accepts_prose_wrapped_json():
    parsed = parse_agent_response('Here you go: {"status":"complete","summary":"done"}')
    assert parsed.status == "complete"


# --- workspace file tools: missing path == empty scratch space, not a blocker ----


def _tool_context(tmp_path) -> ToolContext:
    store = JsonStateStore(tmp_path / "state.json")
    agent = Agent("ada", "ADA", "workspace-ada")
    store.add_agent(agent)
    assignment = _assignment(store)
    return ToolContext(agent=agent, assignment=assignment, store=store)


def test_list_files_missing_dir_is_empty_not_error(tmp_path):
    ctx = _tool_context(tmp_path)
    result = default_tool_registry().execute("list_files", ctx, {"path": "memory"})
    assert result.ok is True
    assert result.output == "[]"
    assert result.metadata["exists"] is False


def test_read_file_missing_is_empty_not_error(tmp_path):
    ctx = _tool_context(tmp_path)
    result = default_tool_registry().execute(
        "read_file", ctx, {"path": "memory/2026-01-01-MEMORY.md"}
    )
    assert result.ok is True
    assert result.output == ""
    assert result.metadata["exists"] is False


def test_write_file_creates_missing_parent_dirs(tmp_path):
    ctx = _tool_context(tmp_path)
    result = default_tool_registry().execute(
        "write_file", ctx, {"path": "rest/2026-01-01-REST.md", "content": "ok"}
    )
    assert result.ok is True
    assert (ctx.workspace / "rest" / "2026-01-01-REST.md").read_text() == "ok"


def test_rest_protocol_tells_agent_to_create_missing_paths():
    text = rest_assignment_text("2026-01-01").lower()
    assert "never block on a missing workspace path" in text
    assert "write_file" in text


def test_ollama_provider_declares_tools_and_translates_tool_calls(monkeypatch):
    captured: dict = {}

    def fake_urlopen(request, timeout=0):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResp(
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "write_file",
                                "arguments": {"path": "notes.md", "content": "hi"},
                            }
                        }
                    ],
                },
                "prompt_eval_count": 5,
                "eval_count": 7,
            }
        )

    monkeypatch.setattr("brigade.providers.urllib.request.urlopen", fake_urlopen)
    tools = [
        {
            "type": "function",
            "function": {"name": "write_file", "description": "", "parameters": {}},
        }
    ]
    response = OllamaProvider(model="gpt-oss:20b").complete("hello", tools=tools)

    assert captured["body"]["tools"] == tools
    parsed = json.loads(response.text)
    assert parsed["status"] == "tool_call"
    assert parsed["tool"] == "write_file"
    assert parsed["arguments"] == {"path": "notes.md", "content": "hi"}


def test_ollama_provider_omits_tools_key_when_not_passed(monkeypatch):
    captured: dict = {}

    def fake_urlopen(request, timeout=0):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResp({"message": {"content": "plain answer"}})

    monkeypatch.setattr("brigade.providers.urllib.request.urlopen", fake_urlopen)
    response = OllamaProvider(model="qwen2.5-coder:7b").complete("hello")

    assert "tools" not in captured["body"]
    assert response.text == "plain answer"


def test_ollama_provider_requests_expanded_context(monkeypatch):
    captured: dict = {}

    def fake_urlopen(request, timeout=0):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResp({"message": {"content": "ok"}})

    monkeypatch.setattr("brigade.providers.urllib.request.urlopen", fake_urlopen)
    OllamaProvider(model="qwen2.5-coder:7b").complete("hello")

    assert captured["body"]["options"]["num_ctx"] == 16384


def test_ollama_provider_context_size_env_override(monkeypatch):
    captured: dict = {}

    def fake_urlopen(request, timeout=0):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResp({"message": {"content": "ok"}})

    monkeypatch.setattr("brigade.providers.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("BRIGADE_OLLAMA_NUM_CTX", "8192")
    OllamaProvider(model="qwen2.5-coder:7b").complete("hello")

    assert captured["body"]["options"]["num_ctx"] == 8192


def test_starvation_alert_reaches_orchestrator_chat(tmp_path):
    from brigade.orchestrator import run_full_cycle
    from brigade.schemas import Mission

    store = _store(tmp_path)
    store.set_mission(Mission("Run the prototype", [], []))
    # ada pinned by a blocked (non-awaiting-human) assignment, with queued
    # work behind it: the starved-dispatch shape of the Jul 4-6 deadlock.
    _blocked(store, awaiting_human=False)
    queued = Assignment(
        assignment="Follow-up work stuck behind the blocked task",
        assigned_to="ada",
        created_by="human",
        source="direct_command",
    )
    store.add_assignment(queued)

    config = OrchestrationConfig(
        proactive_mode="off",
        blocker_resolution_enabled=False,
        dispatch_starvation_alert_cycles=1,
        # A rest assignment created inside the UTC rest window would count as
        # cycle work and reset the starvation streak.
        rest_enabled=False,
    )
    result = run_full_cycle(store, None, config)

    starvation = result.sub_results["starvation"]
    assert starvation["starved"] is True
    assert starvation["alert"] is not None
    assert starvation["delivery"]["channels"][0]["channel"] == "chat"
    assert starvation["delivery"]["channels"][0]["status"] == "sent"
    chat = store.messages("orchestrator")
    assert any(
        "dispatch starvation" in message.content
        and message.metadata.get("kind") == "operator_notification"
        for message in chat
    )
    assert any("dispatch starvation" in alert for alert in store.alerts())


def test_notify_operator_retries_when_no_channel_delivered(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _blocked(store, awaiting_human=True)

    # Both channels down: chat write raises, telegram unconfigured.
    def broken_add_message(message):
        raise RuntimeError("chat store unavailable")

    monkeypatch.setattr(store, "add_message", broken_add_message)
    first = _notify_operator_escalations(store, OrchestrationConfig())
    assert first["notified"][0]["delivery"]["status"] == "failed"
    # The failed delivery must not record the idempotency event.
    assert first["events"] == []

    # Channel recovers: the very next cycle delivers instead of staying
    # silently "notified" forever (the v1 behavior that dropped escalations).
    monkeypatch.undo()
    second = _notify_operator_escalations(store, OrchestrationConfig())
    assert second["notified"][0]["delivery"]["status"] == "sent"
    assert len(store.messages("orchestrator")) == 1


# --- JSON extraction from chatty model output -------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '{"status": "complete", "summary": "rested"} Let me know if you need anything!',
        '{"status": "complete", "summary": "rested"}\nHope that helps.',
        'Here is my report: {"status": "complete", "summary": "rested"} thanks',
        '```json\n{"status": "complete", "summary": "rested"}\n```',
    ],
)
def test_extract_json_object_trims_surrounding_prose(text):
    assert json.loads(extract_json_object(text)) == {
        "status": "complete",
        "summary": "rested",
    }


def test_parse_agent_response_accepts_json_with_trailing_prose():
    parsed = parse_agent_response(
        '{"status": "complete", "summary": "rested"} Let me know if you need anything!'
    )
    assert parsed.status == "complete"
    assert parsed.summary == "rested"


def test_extract_json_object_without_object_returns_input():
    assert extract_json_object("no structured output here") == "no structured output here"


# --- phantom-model demotion on 404 -------------------------------------------------


def test_is_model_not_found_error_matches_404_model_errors():
    from brigade.providers import is_model_not_found_error

    assert is_model_not_found_error(
        "openai-codex model request failed: Error code: 404 - {'error': "
        "{'message': 'Model not found gpt-5.6-luna', 'type': 'invalid_request_error', "
        "'param': 'model', 'code': None}}"
    )
    assert is_model_not_found_error("code: 'model_not_found'")
    assert not is_model_not_found_error("Error code: 429 - rate limited")
    assert not is_model_not_found_error("Error code: 404 - page missing")


def test_demote_unavailable_model_flips_and_persists(tmp_path):
    from brigade.providers import demote_unavailable_model

    store = _store(tmp_path)
    store.set_model_inventory(
        {
            "providers": {
                "openai-codex": {
                    "provider": "openai-codex",
                    "status": "ok",
                    "models": [
                        {"provider": "openai-codex", "model": "gpt-5.6-luna", "available": True},
                        {"provider": "openai-codex", "model": "gpt-5.5", "available": True},
                    ],
                }
            },
            "updated_at": "2026-07-13T00:00:00+00:00",
        }
    )
    assert demote_unavailable_model(store, "openai-codex", "gpt-5.6-luna") is True
    models = store.model_inventory()["providers"]["openai-codex"]["models"]
    by_name = {item["model"]: item for item in models}
    assert by_name["gpt-5.6-luna"]["available"] is False
    assert "not callable" in by_name["gpt-5.6-luna"]["detail"]
    assert by_name["gpt-5.5"]["available"] is True
    # unknown model / provider are no-ops
    assert demote_unavailable_model(store, "openai-codex", "nope") is False
    assert demote_unavailable_model(store, "gemini", "gpt-5.5") is False


def test_locked_complete_demotes_model_on_404(tmp_path):
    from brigade.runner import _locked_complete_with_retries

    store = _store(tmp_path)
    store.set_model_inventory(
        {
            "providers": {
                "openai-codex": {
                    "provider": "openai-codex",
                    "status": "ok",
                    "models": [
                        {"provider": "openai-codex", "model": "gpt-5.6-luna", "available": True}
                    ],
                }
            }
        }
    )

    class NotFoundProvider:
        provider_name = "openai-codex"
        model = "gpt-5.6-luna"
        route_type = "cloud"

        def complete(self, prompt):
            raise RuntimeError(
                "openai-codex model request failed: Error code: 404 - "
                "{'error': {'message': 'Model not found gpt-5.6-luna', 'param': 'model'}}"
            )

    with pytest.raises(RuntimeError):
        _locked_complete_with_retries(store, "ada", NotFoundProvider(), "hello")
    models = store.model_inventory()["providers"]["openai-codex"]["models"]
    assert models[0]["available"] is False
