from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from brigade.providers import ModelProvider
from brigade.schemas import ChatMessage, User
from brigade.store import RedisRuntimeClient, StateStore
from brigade.time import utc_now_iso

SENSITIVE_METADATA_KEYS = {
    "api_key",
    "authorization",
    "bot_token",
    "client_secret",
    "password",
    "refresh_token",
    "secret",
    "secret_token",
    "token",
}
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConnectorResult:
    provider: str
    status: str
    channel: str | None = None
    message_id: str | None = None
    reason: str | None = None
    response_body: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status,
            "channel": self.channel,
            "message_id": self.message_id,
            "reason": self.reason,
            "response_body": self.response_body,
        }


@dataclass(frozen=True)
class IncomingConnectorMessage:
    provider: str
    external_user_id: str
    conversation_id: str
    external_message_id: str | None
    text: str
    channel: str
    reply_target: str
    thread_name: str | None = None
    metadata: dict[str, Any] | None = None


class ConnectorRateLimiter(Protocol):
    def allow(self, provider: str, external_user_id: str) -> bool:
        """Return whether this provider/user may run another live turn."""


class InMemoryConnectorRateLimiter:
    def __init__(self, *, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._buckets: dict[tuple[str, str], tuple[float, int]] = {}

    def allow(self, provider: str, external_user_id: str) -> bool:
        now = time.monotonic()
        key = (provider, external_user_id)
        window_started, count = self._buckets.get(key, (now, 0))
        if now - window_started >= self.window_seconds:
            window_started, count = now, 0
        count += 1
        self._buckets[key] = (window_started, count)
        return count <= self.limit


class RedisConnectorRateLimiter:
    def __init__(
        self,
        redis: RedisRuntimeClient,
        *,
        limit: int,
        window_seconds: int,
    ) -> None:
        self.redis = redis
        self.limit = limit
        self.window_seconds = window_seconds

    def allow(self, provider: str, external_user_id: str) -> bool:
        key = f"{provider}:{external_user_id}"
        return self.redis.connector_rate_limit_allow(
            key,
            limit=self.limit,
            window_seconds=self.window_seconds,
        )


OutboundSender = Callable[[IncomingConnectorMessage, str], ConnectorResult]
HttpPost = Callable[[str, bytes, dict[str, str]], dict[str, Any]]


def handle_telegram_update(
    store: StateStore,
    payload: dict[str, Any],
    *,
    default_agent: str,
    allowlist: set[str] | None = None,
    max_message_chars: int = 4000,
) -> ConnectorResult:
    incoming = parse_telegram_update(payload)
    if incoming is None:
        return ConnectorResult("telegram", "ignored", reason="missing message")
    result = _validate_smoke_inbound(
        incoming,
        allowlist=allowlist,
        max_message_chars=max_message_chars,
    )
    if result is not None:
        return result
    msg = _inbound_chat_message(incoming, default_agent)
    store.add_message(msg)
    return ConnectorResult(
        "telegram",
        "accepted",
        channel=incoming.channel,
        message_id=msg.message_id,
    )


def handle_google_chat_event(
    store: StateStore,
    payload: dict[str, Any],
    *,
    default_agent: str,
    allowlist: set[str] | None = None,
    max_message_chars: int = 4000,
) -> ConnectorResult:
    incoming = parse_google_chat_event(payload)
    if incoming is None:
        return ConnectorResult("google_chat", "ignored", reason="missing message")
    result = _validate_smoke_inbound(
        incoming,
        allowlist=allowlist,
        max_message_chars=max_message_chars,
    )
    if result is not None:
        return result
    msg = _inbound_chat_message(incoming, default_agent)
    store.add_message(msg)
    return ConnectorResult(
        "google_chat",
        "accepted",
        channel=incoming.channel,
        message_id=msg.message_id,
    )


def process_live_connector_message(
    store: StateStore,
    incoming: IncomingConnectorMessage,
    *,
    default_agent: str,
    model_provider: ModelProvider,
    outbound_sender: OutboundSender,
    allowlist: set[str] | None = None,
    rate_limiter: ConnectorRateLimiter | None = None,
    max_inbound_chars: int = 4000,
    max_outbound_chars: int = 3500,
) -> ConnectorResult:
    validation = _validate_live_inbound(
        store,
        incoming,
        default_agent=default_agent,
        allowlist=allowlist,
        rate_limiter=rate_limiter,
        max_inbound_chars=max_inbound_chars,
    )
    if validation is not None:
        return validation

    identity = _approved_identity_for(store, incoming, allowlist=allowlist)
    username = str(identity.get("username") or incoming.external_user_id)

    inbound = _inbound_chat_message(incoming, default_agent)
    store.add_message(inbound)
    _record_audit(
        store,
        incoming,
        direction="inbound",
        status="accepted",
        agent_id=default_agent,
        metadata={
            "message_id": inbound.message_id,
            "content_length": len(incoming.text),
            **(incoming.metadata or {}),
        },
    )

    agent = next((item for item in store.agents() if item.agent_id == default_agent), None)
    if agent is None:
        reason = f"unknown target agent: {default_agent}"
        store.add_alert(f"{incoming.provider} connector rejected message: {reason}")
        _record_audit(
            store,
            incoming,
            direction="internal",
            status="rejected",
            reason=reason,
            agent_id=default_agent,
        )
        return ConnectorResult(incoming.provider, "rejected", incoming.channel, reason=reason)

    try:
        response = model_provider.complete(
            _external_chat_prompt(
                display_name=agent.display_name,
                agent_id=agent.agent_id,
                content=incoming.text,
                username=username,
                provider=incoming.provider,
                store=store,
            )
        )
    except RuntimeError as exc:
        reason = str(exc)
        store.add_alert(f"{incoming.provider} connector model call failed: {reason}")
        _record_audit(
            store,
            incoming,
            direction="outbound",
            status="blocked",
            reason=reason,
            agent_id=default_agent,
        )
        return ConnectorResult(incoming.provider, "blocked", incoming.channel, reason=reason)

    response_text = response.text.strip()
    if not response_text:
        reason = "empty model response"
        store.add_alert(f"{incoming.provider} connector blocked outbound reply: {reason}")
        _record_audit(
            store,
            incoming,
            direction="outbound",
            status="blocked",
            reason=reason,
            agent_id=default_agent,
        )
        return ConnectorResult(incoming.provider, "blocked", incoming.channel, reason=reason)
    if len(response_text) > max_outbound_chars:
        reason = f"outbound reply too large: {len(response_text)} chars"
        store.add_alert(f"{incoming.provider} connector blocked outbound reply: {reason}")
        _record_audit(
            store,
            incoming,
            direction="outbound",
            status="blocked",
            reason=reason,
            agent_id=default_agent,
            metadata={"content_length": len(response_text)},
        )
        return ConnectorResult(incoming.provider, "blocked", incoming.channel, reason=reason)

    response_message = ChatMessage(
        channel=incoming.channel,
        sender=default_agent,
        recipient=f"{incoming.provider}:{incoming.external_user_id}",
        content=response_text,
        metadata={
            "kind": "external_outbound",
            "provider": incoming.provider,
            "conversation_id": incoming.conversation_id,
            "external_user_id": incoming.external_user_id,
            "external_message_id": incoming.external_message_id,
            "provider_route": response.provider,
            "model": response.model,
            "route_type": response.route_type,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "estimated_cost_usd": response.estimated_cost_usd,
        },
    )
    store.add_message(response_message)
    store.add_usage_record(
        {
            "usage_id": str(uuid4()),
            "assignment_id": None,
            "agent_id": default_agent,
            "provider": response.provider,
            "model": response.model,
            "route_type": response.route_type,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "total_tokens": response.input_tokens + response.output_tokens,
            "estimated_cost_usd": response.estimated_cost_usd,
            "recorded_at": utc_now_iso(),
            "conversation_id": incoming.channel,
            "source": f"{incoming.provider}_connector",
        }
    )
    store.add_episode(
        {
            "episode_id": str(uuid4()),
            "agent_id": default_agent,
            "created_at": utc_now_iso(),
            "source": f"{incoming.provider}_connector",
            "conversation_id": incoming.channel,
            "summary": _summarize(response_text),
            "request": incoming.text,
            "response": response_text,
            "user": username,
        }
    )

    outbound = outbound_sender(incoming, response_text)
    LOGGER.info(
        "connector_live_message_processed",
        extra={
            "provider": incoming.provider,
            "channel": incoming.channel,
            "status": outbound.status,
            "agent_id": default_agent,
        },
    )
    _record_audit(
        store,
        incoming,
        direction="outbound",
        status=outbound.status,
        reason=outbound.reason,
        agent_id=default_agent,
        metadata={
            "message_id": response_message.message_id,
            "content_length": len(response_text),
        },
    )
    return ConnectorResult(
        incoming.provider,
        "complete" if outbound.status == "sent" else outbound.status,
        incoming.channel,
        message_id=response_message.message_id,
        reason=outbound.reason,
        response_body=outbound.response_body,
    )


def parse_telegram_update(payload: dict[str, Any]) -> IncomingConnectorMessage | None:
    message = payload.get("message") or payload.get("edited_message") or {}
    if not isinstance(message, dict) or not message:
        return None
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    external_user_id = str(sender.get("id") or chat.get("id") or "").strip()
    text = str(message.get("text") or "").strip()
    chat_id = str(chat.get("id") or external_user_id).strip()
    if not chat_id:
        return None
    return IncomingConnectorMessage(
        provider="telegram",
        external_user_id=external_user_id,
        conversation_id=chat_id,
        external_message_id=str(message.get("message_id") or payload.get("update_id") or ""),
        text=text,
        channel=f"telegram:{chat_id}",
        reply_target=chat_id,
        metadata={
            "chat_type": chat.get("type"),
            "update_id": payload.get("update_id"),
            "username": sender.get("username"),
        },
    )


def parse_google_chat_event(payload: dict[str, Any]) -> IncomingConnectorMessage | None:
    message = payload.get("message") or {}
    if not isinstance(message, dict) or not message:
        return None
    sender = payload.get("user") or message.get("sender") or {}
    space = message.get("space") or payload.get("space") or {}
    thread = message.get("thread") or {}
    external_user_id = str(sender.get("name") or sender.get("email") or "").strip()
    text = str(message.get("text") or payload.get("text") or "").strip()
    space_name = str(space.get("name") or "direct").strip()
    return IncomingConnectorMessage(
        provider="google_chat",
        external_user_id=external_user_id,
        conversation_id=space_name,
        external_message_id=str(message.get("name") or payload.get("eventTime") or ""),
        text=text,
        channel=f"google-chat:{space_name}",
        reply_target=space_name,
        thread_name=thread.get("name"),
        metadata={
            "event_type": payload.get("type"),
            "space_type": space.get("type"),
            "thread_name": thread.get("name"),
        },
    )


def telegram_reply_sender(
    bot_token: str,
    *,
    http_post: HttpPost | None = None,
) -> OutboundSender:
    def send(incoming: IncomingConnectorMessage, text: str) -> ConnectorResult:
        return send_telegram_message(
            bot_token,
            chat_id=incoming.reply_target,
            text=text,
            http_post=http_post,
        )

    return send


def send_telegram_message(
    bot_token: str,
    *,
    chat_id: str,
    text: str,
    http_post: HttpPost | None = None,
) -> ConnectorResult:
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    post = http_post or _urllib_post_json
    try:
        response = post(url, payload, {"Content-Type": "application/json"})
    except RuntimeError as exc:
        return ConnectorResult("telegram", "failed", reason=str(exc))
    if response.get("ok") is False:
        return ConnectorResult("telegram", "failed", reason=str(response.get("description")))
    message_id = None
    result = response.get("result")
    if isinstance(result, dict):
        message_id = str(result.get("message_id") or "")
    return ConnectorResult("telegram", "sent", channel=f"telegram:{chat_id}", message_id=message_id)


def google_chat_reply_sender() -> OutboundSender:
    def send(incoming: IncomingConnectorMessage, text: str) -> ConnectorResult:
        body: dict[str, Any] = {"text": text}
        if incoming.thread_name:
            body["thread"] = {"name": incoming.thread_name}
        return ConnectorResult(
            "google_chat",
            "sent",
            channel=incoming.channel,
            response_body=body,
        )

    return send


def approve_external_identity(
    store: StateStore,
    *,
    provider: str,
    external_user_id: str,
    username: str,
    decided_by: str,
    reason: str | None = None,
) -> dict[str, Any]:
    existing = store.external_identity(provider, external_user_id)
    now = utc_now_iso()
    record = {
        "provider": provider,
        "external_user_id": external_user_id,
        "username": username,
        "status": "approved",
        "reason": reason,
        "redacted_metadata": redact_metadata((existing or {}).get("redacted_metadata", {})),
        "created_at": (existing or {}).get("created_at", now),
        "updated_at": now,
        "decided_at": now,
        "decided_by": decided_by,
    }
    if next((user for user in store.users() if user.username == username), None) is None:
        store.add_user(User(username=username))
    store.upsert_external_identity(record)
    store.add_connector_audit_event(
        connector_audit_record(
            provider=provider,
            direction="internal",
            status="approved",
            external_user_id=external_user_id,
            reason=reason,
            metadata={"username": username, "decided_by": decided_by},
        )
    )
    return record


def reject_external_identity(
    store: StateStore,
    *,
    provider: str,
    external_user_id: str,
    decided_by: str,
    reason: str | None = None,
) -> dict[str, Any]:
    existing = store.external_identity(provider, external_user_id)
    now = utc_now_iso()
    record = {
        "provider": provider,
        "external_user_id": external_user_id,
        "username": (existing or {}).get("username"),
        "status": "rejected",
        "reason": reason,
        "redacted_metadata": redact_metadata((existing or {}).get("redacted_metadata", {})),
        "created_at": (existing or {}).get("created_at", now),
        "updated_at": now,
        "decided_at": now,
        "decided_by": decided_by,
    }
    store.upsert_external_identity(record)
    store.add_connector_audit_event(
        connector_audit_record(
            provider=provider,
            direction="internal",
            status="rejected",
            external_user_id=external_user_id,
            reason=reason,
            metadata={"decided_by": decided_by},
        )
    )
    return record


def connector_audit_record(
    *,
    provider: str,
    direction: str,
    status: str,
    external_user_id: str | None = None,
    conversation_id: str | None = None,
    external_message_id: str | None = None,
    agent_id: str | None = None,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "provider": provider,
        "direction": direction,
        "status": status,
        "external_user_id": external_user_id,
        "conversation_id": conversation_id,
        "external_message_id": external_message_id,
        "agent_id": agent_id,
        "reason": reason,
        "redacted_metadata": redact_metadata(metadata or {}),
        "created_at": utc_now_iso(),
    }


def redact_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(sensitive in lowered for sensitive in SENSITIVE_METADATA_KEYS):
                redacted[str(key)] = "***redacted***"
            else:
                redacted[str(key)] = redact_metadata(item)
        return redacted
    if isinstance(value, list):
        return [redact_metadata(item) for item in value]
    return value


def parse_allowlist(value: str | None) -> set[str] | None:
    if value is None:
        return None
    items = {item.strip() for item in value.split(",") if item.strip()}
    return items or set()


def _validate_smoke_inbound(
    incoming: IncomingConnectorMessage,
    *,
    allowlist: set[str] | None,
    max_message_chars: int,
) -> ConnectorResult | None:
    if not incoming.external_user_id:
        return ConnectorResult(incoming.provider, "rejected", reason="missing user id")
    if allowlist is not None and incoming.external_user_id not in allowlist:
        return ConnectorResult(incoming.provider, "rejected", reason="user not allowlisted")
    if not incoming.text:
        return ConnectorResult(incoming.provider, "ignored", reason="empty message")
    if len(incoming.text) > max_message_chars:
        return ConnectorResult(incoming.provider, "rejected", reason="message too large")
    return None


def _validate_live_inbound(
    store: StateStore,
    incoming: IncomingConnectorMessage,
    *,
    default_agent: str,
    allowlist: set[str] | None,
    rate_limiter: ConnectorRateLimiter | None,
    max_inbound_chars: int,
) -> ConnectorResult | None:
    smoke = _validate_smoke_inbound(
        incoming,
        allowlist=None,
        max_message_chars=max_inbound_chars,
    )
    if smoke is not None:
        _record_audit(
            store,
            incoming,
            direction="inbound",
            status=smoke.status,
            reason=smoke.reason,
            agent_id=default_agent,
            metadata={"content_length": len(incoming.text)},
        )
        return smoke

    if rate_limiter is not None and not rate_limiter.allow(
        incoming.provider,
        incoming.external_user_id,
    ):
        reason = "rate limit exceeded"
        LOGGER.warning(
            "connector_rate_limited",
            extra={"provider": incoming.provider, "external_user_id": incoming.external_user_id},
        )
        _record_audit(
            store,
            incoming,
            direction="inbound",
            status="rate_limited",
            reason=reason,
            agent_id=default_agent,
        )
        return ConnectorResult(
            incoming.provider,
            "rate_limited",
            incoming.channel,
            reason=reason,
        )

    identity = store.external_identity(incoming.provider, incoming.external_user_id)
    allowlisted = allowlist is not None and incoming.external_user_id in allowlist
    if allowlisted:
        return None
    if identity and identity.get("status") == "approved":
        return None
    if identity and identity.get("status") == "rejected":
        reason = identity.get("reason") or "external user rejected"
        _record_audit(
            store,
            incoming,
            direction="inbound",
            status="rejected",
            reason=reason,
            agent_id=default_agent,
        )
        return ConnectorResult(incoming.provider, "rejected", incoming.channel, reason=reason)

    _ensure_pending_identity(store, incoming)
    LOGGER.warning(
        "connector_identity_pending",
        extra={"provider": incoming.provider, "external_user_id": incoming.external_user_id},
    )
    store.add_alert(
        f"{incoming.provider} external user {incoming.external_user_id} pending approval"
    )
    _record_audit(
        store,
        incoming,
        direction="inbound",
        status="pending_approval",
        reason="external user pending approval",
        agent_id=default_agent,
        metadata=incoming.metadata,
    )
    return ConnectorResult(
        incoming.provider,
        "pending_approval",
        incoming.channel,
        reason="external user pending approval",
    )


def _approved_identity_for(
    store: StateStore,
    incoming: IncomingConnectorMessage,
    *,
    allowlist: set[str] | None,
) -> dict[str, Any]:
    identity = store.external_identity(incoming.provider, incoming.external_user_id)
    if identity and identity.get("status") == "approved":
        return identity
    if allowlist is not None and incoming.external_user_id in allowlist:
        now = utc_now_iso()
        return {
            "provider": incoming.provider,
            "external_user_id": incoming.external_user_id,
            "username": incoming.external_user_id,
            "status": "approved",
            "reason": "allowlisted",
            "redacted_metadata": redact_metadata(incoming.metadata or {}),
            "created_at": now,
            "updated_at": now,
            "decided_at": now,
            "decided_by": "allowlist",
        }
    return {}


def _ensure_pending_identity(store: StateStore, incoming: IncomingConnectorMessage) -> None:
    existing = store.external_identity(incoming.provider, incoming.external_user_id)
    if existing is not None:
        return
    now = utc_now_iso()
    store.upsert_external_identity(
        {
            "provider": incoming.provider,
            "external_user_id": incoming.external_user_id,
            "username": None,
            "status": "pending",
            "reason": "first inbound message",
            "redacted_metadata": redact_metadata(incoming.metadata or {}),
            "created_at": now,
            "updated_at": now,
            "decided_at": None,
            "decided_by": None,
        }
    )


def _record_audit(
    store: StateStore,
    incoming: IncomingConnectorMessage,
    *,
    direction: str,
    status: str,
    agent_id: str | None = None,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    store.add_connector_audit_event(
        connector_audit_record(
            provider=incoming.provider,
            direction=direction,
            status=status,
            external_user_id=incoming.external_user_id,
            conversation_id=incoming.conversation_id,
            external_message_id=incoming.external_message_id,
            agent_id=agent_id,
            reason=reason,
            metadata=metadata,
        )
    )


def _inbound_chat_message(
    incoming: IncomingConnectorMessage,
    default_agent: str,
) -> ChatMessage:
    return ChatMessage(
        channel=incoming.channel,
        sender=f"{incoming.provider}:{incoming.external_user_id}",
        recipient=default_agent,
        content=incoming.text,
        metadata={
            "kind": "external_inbound",
            "provider": incoming.provider,
            "conversation_id": incoming.conversation_id,
            "external_user_id": incoming.external_user_id,
            "external_message_id": incoming.external_message_id,
            "audit_id": str(uuid4()),
        },
    )


def _external_chat_prompt(
    *,
    display_name: str,
    agent_id: str,
    content: str,
    username: str,
    provider: str,
    store: StateStore | None = None,
) -> str:
    from brigade.prompt_floors import build_chat_status_context

    lines = [
        f"You are {display_name} ({agent_id}).",
        f"User {username} is chatting with you through {provider}.",
        "Answer directly and concisely. If you need action, state the next concrete step.",
    ]
    if store is not None:
        lines.extend(
            [
                "",
                "Live status context (ground answers about current work, "
                "priorities, and blockers in this, not memory):",
                json.dumps(
                    build_chat_status_context(store, agent_id),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ]
        )
    lines.extend(["", "Message:", content])
    return "\n".join(lines)


def _summarize(value: str) -> str:
    stripped = " ".join(value.split())
    return stripped if len(stripped) <= 240 else stripped[:237].rstrip() + "..."


def _urllib_post_json(url: str, payload: bytes, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"telegram sendMessage failed: {exc}") from exc
