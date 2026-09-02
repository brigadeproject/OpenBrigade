"""Shared control commands for durable Executive and Crew Chief chats."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from brigade.config import Settings
from brigade.providers import available_model_options
from brigade.schemas import ChatMessage, Conversation, utc_now_iso
from brigade.store import StateStore


@dataclass(frozen=True)
class ChatCommand:
    verb: str
    argument: str = ""


def parse_chat_command(text: str) -> ChatCommand | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    command, _, argument = stripped[1:].partition(" ")
    verb = command.lower().split("@", 1)[0]
    aliases = {
        "commands": "help",
        "front_desk": "frontdesk",
        "front-desk": "frontdesk",
        "clear": "new",
    }
    return ChatCommand(aliases.get(verb, verb), argument.strip())


def effective_model_route(
    conversation: Conversation,
    *,
    fallback_provider: str,
    fallback_model: str,
    fallback_base_url: str | None = None,
) -> tuple[str, str, str | None]:
    return (
        conversation.model_provider or fallback_provider,
        conversation.model_name or fallback_model,
        conversation.model_base_url or fallback_base_url,
    )


def persist_model_route(
    store: StateStore,
    conversation: Conversation,
    *,
    provider: str | None,
    model: str | None,
    base_url: str | None = None,
) -> Conversation:
    conversation.model_provider = provider
    conversation.model_name = model
    conversation.model_base_url = base_url
    conversation.updated_at = utc_now_iso()
    store.upsert_conversation(conversation)
    return conversation


def model_command_reply(
    store: StateStore,
    settings: Settings,
    conversation: Conversation,
    argument: str,
    *,
    fallback_provider: str,
    fallback_model: str,
    fallback_base_url: str | None = None,
) -> str:
    inventory = available_model_options(settings, store.model_inventory())
    options = [
        item
        for item in inventory.get("options", [])
        if item.get("available") and item.get("configured", True)
    ]
    current_provider, current_model, _ = effective_model_route(
        conversation,
        fallback_provider=fallback_provider,
        fallback_model=fallback_model,
        fallback_base_url=fallback_base_url,
    )
    selection = argument.strip()
    if not selection:
        lines = [f"Current model: {current_provider} / {current_model}", "Available models:"]
        lines.extend(
            f"{index}. {item['provider']} / {item['model']}"
            for index, item in enumerate(options, start=1)
        )
        if not options:
            lines.append("No configured model routes are currently available.")
        lines.append("Use /model <number>, /model <provider>/<model>, or /model default.")
        return "\n".join(lines)

    if selection.lower() in {"default", "reset"}:
        persist_model_route(store, conversation, provider=None, model=None)
        return f"Model reset to this persona's default: {fallback_provider} / {fallback_model}."

    selected: dict[str, Any] | None = None
    if selection.isdigit():
        index = int(selection) - 1
        if 0 <= index < len(options):
            selected = options[index]
    else:
        lowered = selection.lower()
        matches = [
            item
            for item in options
            if lowered
            in {
                str(item["model"]).lower(),
                f"{item['provider']}/{item['model']}".lower(),
                f"{item['provider']}:{item['model']}".lower(),
            }
        ]
        if len(matches) == 1:
            selected = matches[0]
    if selected is None:
        return "Unknown or ambiguous model selection. Use /model to see numbered options."

    persist_model_route(
        store,
        conversation,
        provider=str(selected["provider"]),
        model=str(selected["model"]),
        base_url=str(selected["base_url"]) if selected.get("base_url") else None,
    )
    return f"Model changed to {selected['provider']} / {selected['model']} for this chat."


def command_help(*, fixed_persona: bool = False, direct_enabled: bool = False) -> str:
    commands = [
        "/help — show these commands",
        "/who — show who you are talking to",
        "/model — show or change this chat's model",
        "/new or /clear — start a fresh conversation",
        "/status — show chat or active-work status",
    ]
    if direct_enabled:
        commands.extend(
            [
                "/cancel — cancel active direct work",
                "/confirm — approve a pending direct-work permission",
            ]
        )
    if not fixed_persona:
        commands.extend(
            [
                "/chief <name> — switch to a Crew Chief",
                "/frontdesk — switch to the front desk",
            ]
        )
    return "Chat commands:\n" + "\n".join(commands)


def record_command_exchange(
    store: StateStore,
    conversation: Conversation,
    *,
    operator: str,
    assistant: str,
    command: str,
    reply: str,
) -> tuple[str, str]:
    request = ChatMessage(
        channel=conversation.channel,
        sender=operator,
        recipient=assistant,
        content=command,
        metadata={"kind": "chat_command_request"},
    )
    response = ChatMessage(
        channel=conversation.channel,
        sender=assistant,
        recipient=operator,
        content=reply,
        metadata={"kind": "chat_command_response"},
    )
    store.add_message(request)
    store.add_message(response)
    store.touch_conversation(conversation.thread_id)
    return request.message_id, response.message_id
