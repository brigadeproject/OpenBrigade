from brigade.chat_commands import (
    effective_model_route,
    model_command_reply,
    parse_chat_command,
    record_command_exchange,
)
from brigade.config import Settings
from brigade.schemas import Conversation
from brigade.state import JsonStateStore


def test_chat_command_parser_supports_aliases_and_telegram_bot_suffix():
    assert parse_chat_command("/commands").verb == "help"
    assert parse_chat_command("/clear").verb == "new"
    command = parse_chat_command("/model@brigade_bot openai/gpt-x")
    assert command.verb == "model"
    assert command.argument == "openai/gpt-x"
    assert parse_chat_command("ordinary message") is None


def test_model_command_lists_sets_and_resets_durable_thread_route(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        default_provider="openai",
        default_model="gpt-x",
        openai_api_key="test-key",
    )
    thread = Conversation(operator_username="owner", persona="chief:sage")
    store.upsert_conversation(thread)

    listing = model_command_reply(
        store,
        settings,
        thread,
        "",
        fallback_provider="openai",
        fallback_model="gpt-x",
    )
    assert "Current model: openai / gpt-x" in listing
    assert "2. openai-codex / gpt-5.3-codex-spark" in listing

    changed = model_command_reply(
        store,
        settings,
        thread,
        "2",
        fallback_provider="openai",
        fallback_model="gpt-x",
    )
    assert changed == (
        "Model changed to openai-codex / gpt-5.3-codex-spark for this chat."
    )
    restored = store.find_conversation(thread.thread_id)
    assert restored is not None
    assert effective_model_route(
        restored, fallback_provider="openai", fallback_model="gpt-x"
    )[:2] == ("openai-codex", "gpt-5.3-codex-spark")

    reset = model_command_reply(
        store,
        settings,
        restored,
        "default",
        fallback_provider="openai",
        fallback_model="gpt-x",
    )
    assert "openai / gpt-x" in reset
    restored = store.find_conversation(thread.thread_id)
    assert restored is not None
    assert restored.model_provider is None
    assert restored.model_name is None


def test_command_exchange_is_visible_in_canonical_thread(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    thread = Conversation(operator_username="owner", persona="chief:sage")
    store.upsert_conversation(thread)

    record_command_exchange(
        store,
        thread,
        operator="owner",
        assistant="sage",
        command="/status",
        reply="Chat status: active",
    )

    messages = store.messages(thread.channel)
    assert [item.content for item in messages] == ["/status", "Chat status: active"]
    assert [item.metadata["kind"] for item in messages] == [
        "chat_command_request",
        "chat_command_response",
    ]
