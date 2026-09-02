"""Opt-in direct Crew Chief chat policy and named Telegram account lifecycle."""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import uuid4

from brigade.config import Settings
from brigade.connectors import connector_audit_record
from brigade.secrets import (
    _normalize_telegram_account_id,
    delete_telegram_bot_token,
    read_telegram_bot_token,
    telegram_bot_token_path,
    write_telegram_bot_token,
)
from brigade.store import StateStore
from brigade.time import utc_now_iso

CHIEF_DIRECT_TOOL_GROUPS = frozenset(
    {
        "workspace_read",
        "workspace_write",
        "shell",
        "workspace_tools",
        "maintenance",
    }
)
ACTIVE_TURN_STATUSES = frozenset({"queued", "running", "awaiting_permission"})


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()[:16]


def is_crew_chief(store: StateStore, agent_id: str) -> bool:
    return any(team.crew_chief_id == agent_id for team in store.teams())


def save_telegram_account(
    store: StateStore,
    settings: Settings,
    *,
    account_id: str,
    chief_agent_id: str,
    label: str | None = None,
    token: str | None = None,
    enabled: bool = False,
    bot_username: str | None = None,
    actor: str = "operator",
) -> dict[str, Any]:
    account_id = _normalize_telegram_account_id(account_id)
    chief_agent_id = chief_agent_id.strip()
    if not is_crew_chief(store, chief_agent_id):
        raise ValueError(f"agent is not an active Crew Chief: {chief_agent_id}")
    existing = store.find_telegram_account(account_id)
    current_token = read_telegram_bot_token(settings, account_id)
    if token is not None:
        current_token = token.strip()
        if not current_token:
            raise ValueError("Telegram bot token is required")
    if enabled and not current_token:
        raise ValueError("cannot enable a Telegram account without a bot token")
    if enabled and not settings.allow_json_store:
        if not settings.postgres_dsn:
            raise ValueError("named Telegram accounts require Postgres in live operation")
        if not settings.redis_url:
            raise ValueError("named Telegram accounts require Redis in live operation")
    fingerprint = token_fingerprint(current_token) if current_token else None
    for candidate in store.telegram_accounts():
        if candidate.get("account_id") == account_id:
            continue
        if fingerprint and candidate.get("token_fingerprint") == fingerprint:
            raise ValueError("Telegram bot token is already assigned to another account")
        if (
            enabled
            and candidate.get("enabled")
            and candidate.get("chief_agent_id") == chief_agent_id
        ):
            raise ValueError(f"Crew Chief already has an enabled bot: {chief_agent_id}")
    if token is not None:
        write_telegram_bot_token(settings, account_id, current_token)
    now = utc_now_iso()
    account = {
        "account_id": account_id,
        "chief_agent_id": chief_agent_id,
        "label": (label or (existing or {}).get("label") or account_id).strip(),
        "enabled": bool(enabled),
        "mode": "polling",
        "secret_ref": str(telegram_bot_token_path(settings, account_id)),
        "token_fingerprint": fingerprint,
        "token_configured": bool(current_token),
        "bot_username": bot_username or (existing or {}).get("bot_username"),
        "created_at": (existing or {}).get("created_at") or now,
        "updated_at": now,
        "updated_by": actor,
    }
    store.upsert_telegram_account(account)
    store.add_connector_audit_event(
        connector_audit_record(
            provider=f"telegram:{account_id}",
            direction="internal",
            status="account_enabled" if enabled else "account_saved",
            agent_id=chief_agent_id,
            reason=f"named Telegram account updated by {actor}",
            metadata={"account_id": account_id, "token_configured": bool(current_token)},
        )
    )
    return public_telegram_account(account)


def public_telegram_account(account: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in account.items()
        if key not in {"token_fingerprint", "secret_ref"}
    }


def remove_telegram_account(
    store: StateStore,
    settings: Settings,
    account_id: str,
    *,
    delete_secret: bool = False,
    actor: str = "operator",
) -> bool:
    account = store.find_telegram_account(_normalize_telegram_account_id(account_id))
    if account is None:
        return False
    if account.get("enabled"):
        raise ValueError("disable the Telegram account before removing it")
    removed = store.delete_telegram_account(account_id)
    if delete_secret:
        delete_telegram_bot_token(settings, account_id)
    if removed:
        store.add_connector_audit_event(
            connector_audit_record(
                provider=f"telegram:{account_id}",
                direction="internal",
                status="account_removed",
                agent_id=str(account.get("chief_agent_id") or "") or None,
                reason=f"named Telegram account removed by {actor}",
                metadata={"account_id": account_id, "secret_removed": delete_secret},
            )
        )
    return removed


def default_chief_chat_policy(settings: Settings, chief_agent_id: str) -> dict[str, Any]:
    return {
        "chief_agent_id": chief_agent_id,
        "direct_enabled": False,
        "tool_groups": [],
        "max_tool_calls": settings.chief_direct_max_tool_calls,
        "max_elapsed_seconds": settings.chief_direct_max_elapsed_seconds,
        "hard_elapsed_seconds": settings.chief_direct_hard_elapsed_seconds,
        "context_max_chars": settings.chief_direct_context_max_chars,
        "updated_at": utc_now_iso(),
    }


def save_chief_chat_policy(
    store: StateStore,
    settings: Settings,
    *,
    chief_agent_id: str,
    direct_enabled: bool,
    tool_groups: list[str] | tuple[str, ...],
    max_tool_calls: int | None = None,
    max_elapsed_seconds: int | None = None,
    actor: str = "operator",
) -> dict[str, Any]:
    if not is_crew_chief(store, chief_agent_id):
        raise ValueError(f"agent is not an active Crew Chief: {chief_agent_id}")
    groups = sorted({str(item).strip() for item in tool_groups if str(item).strip()})
    unknown = set(groups) - CHIEF_DIRECT_TOOL_GROUPS
    if unknown:
        raise ValueError("unknown Chief direct tool groups: " + ", ".join(sorted(unknown)))
    if direct_enabled and not groups:
        raise ValueError("direct Chief chat requires at least one explicit tool group")
    tool_budget = int(
        settings.chief_direct_max_tool_calls
        if max_tool_calls is None
        else max_tool_calls
    )
    elapsed = int(
        settings.chief_direct_max_elapsed_seconds
        if max_elapsed_seconds is None
        else max_elapsed_seconds
    )
    if not 1 <= tool_budget <= 1000:
        raise ValueError("max_tool_calls must be between 1 and 1000")
    if not 60 <= elapsed <= settings.chief_direct_hard_elapsed_seconds:
        raise ValueError("max_elapsed_seconds is outside the allowed range")
    policy = {
        **default_chief_chat_policy(settings, chief_agent_id),
        "direct_enabled": bool(direct_enabled),
        "tool_groups": groups,
        "max_tool_calls": tool_budget,
        "max_elapsed_seconds": elapsed,
        "updated_at": utc_now_iso(),
        "updated_by": actor,
    }
    store.upsert_chief_chat_policy(policy)
    store.add_provenance_record(
        {
            "record_id": str(uuid4()),
            "node_type": "chief_chat_policy",
            "node_id": chief_agent_id,
            "source_refs": [],
            "metadata": {
                "event": "chief_chat_policy_updated",
                "direct_enabled": bool(direct_enabled),
                "tool_groups": groups,
                "actor": actor,
            },
            "created_at": utc_now_iso(),
        }
    )
    return policy
