"""Durable long-poll Telegram ingress for the orchestrator service.

Telegram permits either a webhook or ``getUpdates`` polling for a bot token.
This worker uses polling so a local OpenBrigade deployment needs no public HTTP
listener.  It deliberately reuses the connector processing pipeline, keeping
approval, audit, Executive routing, and outbound replies identical to webhook
ingress.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Protocol

from brigade.chief_chat import parse_control_command, resolve_persona, run_connector_chief_chat
from brigade.chief_direct_runtime import direct_turn_control, queue_direct_chief_turn
from brigade.config import Settings
from brigade.connectors import (
    ConnectorChatReply,
    ConnectorResult,
    HttpPost,
    IncomingConnectorMessage,
    RedisConnectorRateLimiter,
    connector_audit_record,
    parse_allowlist,
    parse_telegram_update,
    process_live_connector_message,
    telegram_reply_sender,
    telegram_typing_indicator,
)
from brigade.executive import run_connector_executive_chat
from brigade.providers import ModelProvider, provider_from_settings
from brigade.schemas import Role
from brigade.secrets import read_telegram_bot_token
from brigade.store import RedisRuntimeClient, StateStore

LOGGER = logging.getLogger(__name__)
TELEGRAM_POLLING_CURSOR_KEY = "brigade:runtime:connector:telegram:polling"
TELEGRAM_ALLOWED_UPDATES = ("message", "edited_message")
TELEGRAM_POLLING_RETRY_SECONDS = 2
TELEGRAM_POLLING_MAX_RETRY_SECONDS = 30
TELEGRAM_POLLING_ERROR_AUDIT_INTERVAL_SECONDS = 60


class TelegramPollingCursor(Protocol):
    def get_json(self, key: str) -> dict[str, Any] | None: ...

    def set_json(self, key: str, payload: dict[str, Any]) -> None: ...


MessageProcessor = Callable[..., ConnectorResult]
ProviderFactory = Callable[[Settings], ModelProvider]


def _telegram_api_post(url: str, payload: bytes, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Telegram API request failed: {exc.reason}") from exc
    if not isinstance(body, dict):
        raise RuntimeError("Telegram API returned an invalid response")
    return body


class TelegramPollingWorker:
    """One supervised long-poll consumer for one configured Telegram bot."""

    def __init__(
        self,
        settings: Settings,
        store: StateStore,
        *,
        stop_event: threading.Event | None = None,
        http_post: HttpPost | None = None,
        cursor: TelegramPollingCursor | None = None,
        process_message: MessageProcessor = process_live_connector_message,
        provider_factory: ProviderFactory = provider_from_settings,
        account: dict[str, Any] | None = None,
        bot_token: str | None = None,
    ):
        self.settings = settings
        self.store = store
        self.stop_event = stop_event or threading.Event()
        self.http_post = http_post or _telegram_api_post
        self.cursor = cursor or RedisRuntimeClient(settings.redis_url)
        self.process_message = process_message
        self.provider_factory = provider_factory
        self.account = dict(account or {})
        self.bot_token = bot_token
        self._thread: threading.Thread | None = None
        self._webhook_cleanup_attempted = False
        self._command_menu_attempted = False
        self._last_error_reason: str | None = None
        self._last_error_audit_at = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self.run,
            name=f"brigade-telegram-polling:{self._account_id() or 'default'}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 45) -> None:
        self.stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def run(self) -> None:
        self._require_valid_configuration()
        self._audit("polling_started", reason="Telegram long polling started")
        retry_seconds = TELEGRAM_POLLING_RETRY_SECONDS
        while not self.stop_event.is_set():
            try:
                self.poll_once()
                retry_seconds = TELEGRAM_POLLING_RETRY_SECONDS
            except Exception as exc:  # keep ingress available across transient API failures
                reason = _safe_error(exc)
                if self._should_audit_error(reason):
                    LOGGER.warning("telegram_polling_error", extra={"reason": reason})
                    self._audit("polling_error", reason=reason)
                self.stop_event.wait(retry_seconds)
                retry_seconds = min(retry_seconds * 2, TELEGRAM_POLLING_MAX_RETRY_SECONDS)
        self._audit("polling_stopped", reason="Telegram long polling stopped")

    def poll_once(self) -> int:
        """Fetch and process one Telegram update batch. Returns its size."""
        self._require_valid_configuration()
        if not self._webhook_cleanup_attempted:
            self._clear_existing_webhook()
            self._webhook_cleanup_attempted = True
        if not self._command_menu_attempted:
            self._configure_command_menu()
            self._command_menu_attempted = True
        cursor = self._load_cursor()
        payload: dict[str, Any] = {
            "timeout": self.settings.telegram_polling_timeout_seconds,
            "limit": 100,
            "allowed_updates": list(TELEGRAM_ALLOWED_UPDATES),
        }
        if cursor is not None:
            payload["offset"] = cursor + 1
        result = self._call_api("getUpdates", payload)
        if not isinstance(result, list):
            raise RuntimeError("Telegram getUpdates returned a non-list result")
        for update in result:
            if self.stop_event.is_set():
                break
            if not isinstance(update, dict):
                continue
            update_id = _update_id(update)
            self._process_update(update)
            if update_id is not None:
                self._save_cursor(update_id)
        return len(result)

    def _process_update(self, update: dict[str, Any]) -> None:
        incoming = parse_telegram_update(update, account_id=self._account_id())
        if incoming is None:
            return
        self.process_message(
            self.store,
            incoming,
            default_agent=self.settings.telegram_default_agent,
            model_provider=self.provider_factory(self.settings),
            outbound_sender=telegram_reply_sender(self._token(), http_post=self.http_post),
            chat_turn=self._chat_turn(),
            working_indicator=lambda: telegram_typing_indicator(
                self._token(),
                chat_id=incoming.reply_target,
                http_post=self.http_post,
            ),
            allowlist=parse_allowlist(self.settings.telegram_allowlist),
            rate_limiter=RedisConnectorRateLimiter(
                RedisRuntimeClient(self.settings.redis_url),
                limit=self.settings.connector_rate_limit_count,
                window_seconds=self.settings.connector_rate_limit_window_seconds,
            ),
            max_inbound_chars=self.settings.connector_max_inbound_chars,
            max_outbound_chars=self.settings.connector_max_outbound_chars,
        )

    def _chat_turn(
        self,
    ) -> Callable[[StateStore, IncomingConnectorMessage, str], ConnectorChatReply] | None:
        if self.account:
            chief_agent_id = str(self.account.get("chief_agent_id") or "").strip()

            def named_chief_turn(
                store: StateStore,
                incoming: IncomingConnectorMessage,
                username: str,
            ):
                command = parse_control_command(incoming.text)
                if command is not None and command.verb not in {
                    "status",
                    "cancel",
                    "confirm",
                }:
                    return run_connector_chief_chat(
                        store,
                        incoming,
                        username,
                        provider=self.provider_factory(self.settings),
                        default_persona=chief_agent_id,
                        max_iterations=self.settings.chief_chat_max_iterations,
                        history_window=self.settings.chief_chat_history_window,
                        enable_web_fetch=self.settings.chief_chat_web_fetch_enabled,
                        fixed_persona=chief_agent_id,
                        settings=self.settings,
                        direct_enabled=True,
                        provider_factory=self.provider_factory,
                    )
                policy = store.chief_chat_policy(chief_agent_id)
                owner = next(
                    (
                        item
                        for item in store.users()
                        if item.username == username and item.role == Role.OWNER
                    ),
                    None,
                )
                if policy and policy.get("direct_enabled") and owner is not None:
                    persona = resolve_persona(store, chief_agent_id)
                    conversation = store.resolve_active_conversation(
                        username,
                        persona.persona_id,
                        chief_agent_id=persona.chief_agent_id,
                        team_id=persona.team_id,
                        title=persona.display_name,
                    )
                    control = direct_turn_control(
                        store,
                        thread=conversation,
                        operator_username=username,
                        command=(
                            f"/{command.verb}"
                            if command is not None
                            and command.verb in {"status", "cancel", "confirm"}
                            else incoming.text
                        ),
                    )
                    if control is not None:
                        return ConnectorChatReply(
                            str(control.get("summary") or control.get("status") or "updated"),
                            chief_agent_id,
                        )
                    turn = queue_direct_chief_turn(
                        store,
                        thread=conversation,
                        chief_agent_id=chief_agent_id,
                        operator_username=username,
                        content=incoming.text,
                        idempotency_key=(
                            f"{incoming.provider}:{incoming.external_user_id}:"
                            f"{incoming.external_message_id}"
                        ),
                        source_provider=incoming.provider,
                        source_account_id=self._account_id(),
                        source_chat_id=incoming.reply_target,
                    )
                    return ConnectorChatReply(
                        f"Started direct turn {str(turn['turn_id'])[:8]}. "
                        "Use /status or /cancel.",
                        chief_agent_id,
                    )
                return run_connector_chief_chat(
                    store,
                    incoming,
                    username,
                    provider=self.provider_factory(self.settings),
                    default_persona=chief_agent_id,
                    max_iterations=self.settings.chief_chat_max_iterations,
                    history_window=self.settings.chief_chat_history_window,
                    enable_web_fetch=self.settings.chief_chat_web_fetch_enabled,
                    fixed_persona=chief_agent_id,
                    settings=self.settings,
                    provider_factory=self.provider_factory,
                )

            return named_chief_turn
        if self.settings.connector_executive_chat_enabled and self.settings.executive_enabled:

            def executive_turn(
                store: StateStore,
                incoming: IncomingConnectorMessage,
                username: str,
            ):
                return run_connector_executive_chat(
                    store,
                    incoming,
                    username,
                    provider=self.provider_factory(self.settings),
                    max_iterations=self.settings.executive_max_iterations,
                    enable_web_fetch=self.settings.executive_web_fetch_enabled,
                    settings=self.settings,
                    provider_factory=self.provider_factory,
                )

            return executive_turn
        if self.settings.connector_chief_chat_enabled and self.settings.chief_chat_enabled:

            def chief_turn(store: StateStore, incoming: IncomingConnectorMessage, username: str):
                return run_connector_chief_chat(
                    store,
                    incoming,
                    username,
                    provider=self.provider_factory(self.settings),
                    default_persona=self.settings.chief_chat_default_persona,
                    max_iterations=self.settings.chief_chat_max_iterations,
                    history_window=self.settings.chief_chat_history_window,
                    enable_web_fetch=self.settings.chief_chat_web_fetch_enabled,
                    settings=self.settings,
                    provider_factory=self.provider_factory,
                )

            return chief_turn
        return None

    def _clear_existing_webhook(self) -> None:
        try:
            self._call_api("deleteWebhook", {"drop_pending_updates": False})
        except Exception as exc:
            # Match Telegram's recommended transition behaviour: still try
            # getUpdates, which will expose a conflicting active webhook.
            reason = _safe_error(exc)
            LOGGER.warning("telegram_webhook_cleanup_failed", extra={"reason": reason})
            self._audit("polling_webhook_cleanup_failed", reason=reason)

    def _configure_command_menu(self) -> None:
        commands = [
            {"command": "help", "description": "Show available chat commands"},
            {"command": "who", "description": "Show the current chat persona"},
            {"command": "model", "description": "Show or change this chat's model"},
            {"command": "new", "description": "Start a fresh conversation"},
            {"command": "clear", "description": "Start a fresh conversation"},
            {"command": "status", "description": "Show chat or active-work status"},
        ]
        if self.account:
            commands.extend(
                [
                    {"command": "cancel", "description": "Cancel active direct work"},
                    {"command": "confirm", "description": "Confirm pending permission"},
                ]
            )
        elif not self.settings.connector_executive_chat_enabled:
            commands.extend(
                [
                    {"command": "chief", "description": "Switch to a Crew Chief"},
                    {"command": "frontdesk", "description": "Switch to the front desk"},
                ]
            )
        try:
            self._call_api("setMyCommands", {"commands": commands})
        except Exception as exc:
            reason = _safe_error(exc)
            LOGGER.warning("telegram_command_menu_failed", extra={"reason": reason})
            self._audit("polling_command_menu_failed", reason=reason)

    def _call_api(self, method: str, payload: dict[str, Any]) -> Any:
        response = self.http_post(
            f"https://api.telegram.org/bot{self._token()}/{method}",
            json.dumps(payload).encode("utf-8"),
            {"Content-Type": "application/json"},
        )
        if response.get("ok") is not True:
            raise RuntimeError(str(response.get("description") or f"Telegram {method} failed"))
        return response.get("result")

    def _load_cursor(self) -> int | None:
        saved = self.cursor.get_json(self._cursor_key()) or {}
        if saved.get("token_fingerprint") != _token_fingerprint(self._token()):
            return None
        value = saved.get("last_update_id")
        return value if isinstance(value, int) and value >= 0 else None

    def _save_cursor(self, update_id: int) -> None:
        self.cursor.set_json(
            self._cursor_key(),
            {
                "token_fingerprint": _token_fingerprint(self._token()),
                "last_update_id": update_id,
                "updated_at": time.time(),
            },
        )

    def _audit(self, status: str, *, reason: str) -> None:
        self.store.add_connector_audit_event(
            connector_audit_record(
                provider="telegram",
                direction="internal",
                status=status,
                reason=reason,
                metadata={"ingress": "polling", "account_id": self._account_id()},
            )
        )

    def _should_audit_error(self, reason: str) -> bool:
        now = time.monotonic()
        if (
            reason == self._last_error_reason
            and now - self._last_error_audit_at < TELEGRAM_POLLING_ERROR_AUDIT_INTERVAL_SECONDS
        ):
            return False
        self._last_error_reason = reason
        self._last_error_audit_at = now
        return True

    def _require_valid_configuration(self) -> None:
        if not self.account:
            if not self.settings.telegram_polling_enabled:
                raise RuntimeError("Telegram polling is not enabled")
            if self.settings.telegram_webhook_enabled:
                raise RuntimeError("Telegram webhook and polling cannot both be enabled")
        elif not self.account.get("enabled"):
            raise RuntimeError("named Telegram account is not enabled")
        if self.settings.telegram_polling_timeout_seconds < 1:
            raise RuntimeError("Telegram polling timeout must be at least one second")
        if not self._token():
            raise RuntimeError("Telegram polling bot token is not configured")
        if not self.settings.redis_url:
            raise RuntimeError("Telegram polling requires Redis for its durable update cursor")

    def _token(self) -> str:
        return (self.bot_token or self.settings.telegram_bot_token or "").strip()

    def _account_id(self) -> str | None:
        value = str(self.account.get("account_id") or "").strip()
        return value or None

    def _cursor_key(self) -> str:
        account_id = self._account_id()
        return (
            f"{TELEGRAM_POLLING_CURSOR_KEY}:{account_id}"
            if account_id
            else TELEGRAM_POLLING_CURSOR_KEY
        )


class TelegramPollingSupervisor:
    """Reconciles opt-in named Telegram workers with durable account records."""

    def __init__(
        self,
        settings: Settings,
        store: StateStore,
        *,
        worker_factory=TelegramPollingWorker,
    ) -> None:
        self.settings = settings
        self.store = store
        self.worker_factory = worker_factory
        self.stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._workers: dict[str, TelegramPollingWorker] = {}
        self._missing_token_alerted: set[str] = set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self.run,
            name="brigade-telegram-account-supervisor",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 45) -> None:
        self.stop_event.set()
        for worker in list(self._workers.values()):
            worker.stop(timeout=timeout)
        if self._thread:
            self._thread.join(timeout=timeout)

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.reconcile_once()
            except Exception as exc:
                LOGGER.warning(
                    "telegram_account_reconcile_failed",
                    extra={"reason": _safe_error(exc)},
                )
            self.stop_event.wait(max(1, self.settings.telegram_account_refresh_seconds))

    def reconcile_once(self) -> None:
        desired = {
            str(item["account_id"]): item for item in self.store.telegram_accounts(enabled=True)
        }
        if desired and not self.settings.postgres_dsn:
            raise RuntimeError("named Telegram accounts require Postgres in live operation")
        if desired and not self.settings.redis_url:
            raise RuntimeError("named Telegram accounts require Redis in live operation")
        self._missing_token_alerted.intersection_update(desired)
        for account_id in set(self._workers) - set(desired):
            self._workers.pop(account_id).stop()
            self._missing_token_alerted.discard(account_id)
        for account_id, account in desired.items():
            token = read_telegram_bot_token(self.settings, account_id)
            if not token:
                if account_id not in self._missing_token_alerted:
                    self.store.add_alert(
                        f"telegram:{account_id} enabled without a configured bot token"
                    )
                    self._missing_token_alerted.add(account_id)
                continue
            self._missing_token_alerted.discard(account_id)
            existing = self._workers.get(account_id)
            if existing and existing._thread and existing._thread.is_alive():
                same_binding = (
                    existing.account.get("chief_agent_id") == account.get("chief_agent_id")
                    and existing.account.get("token_fingerprint")
                    == account.get("token_fingerprint")
                )
                if same_binding:
                    continue
                existing.stop()
            worker = self.worker_factory(
                self.settings,
                self.store,
                account=account,
                bot_token=token,
            )
            self._workers[account_id] = worker
            worker.start()


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _update_id(update: dict[str, Any]) -> int | None:
    value = update.get("update_id")
    return value if isinstance(value, int) and value >= 0 else None


def _safe_error(exc: Exception) -> str:
    text = str(exc).replace("https://api.telegram.org", "Telegram API")
    return text[:240] or exc.__class__.__name__
