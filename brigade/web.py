from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from brigade import __version__
from brigade.auth import AuthResult, issue_token, verify_token
from brigade.chat_commands import (
    command_help,
    effective_model_route,
    model_command_reply,
    parse_chat_command,
    persist_model_route,
    record_command_exchange,
)
from brigade.chief_chat import (
    CHIEF_CHAT_KIND_PREFIX,
    FRONT_DESK_PERSONA,
    UnknownPersonaError,
    available_personas,
    resolve_persona,
    run_chief_chat_turn,
    run_connector_chief_chat,
)
from brigade.chief_direct_runtime import direct_turn_control, queue_direct_chief_turn
from brigade.config import Settings, load_settings
from brigade.connectors import (
    ConnectorRateLimiter,
    ExternalIdentityAlreadyDecidedError,
    HttpPost,
    RedisConnectorRateLimiter,
    UnknownExternalIdentityError,
    connector_audit_record,
    decide_external_identity,
    google_chat_reply_sender,
    parse_allowlist,
    parse_google_chat_event,
    parse_telegram_update,
    process_live_connector_message,
    telegram_reply_sender,
    telegram_typing_indicator,
)
from brigade.direct_chief import (
    public_telegram_account,
    remove_telegram_account,
    save_chief_chat_policy,
    save_telegram_account,
)
from brigade.executive import (
    EXECUTIVE_CHAT_KIND_PREFIX,
    UnknownExecutiveError,
    available_executive_personas,
    resolve_executive_persona,
    run_connector_executive_chat,
    run_executive_chat_turn,
)
from brigade.governance import accept_policy_projections, ensure_policy_projections_current
from brigade.health import check_configured_datastores, check_embedding_surface
from brigade.knowledge_web import register_knowledge_routes
from brigade.markdown import render_markdown_html
from brigade.providers import (
    available_model_options,
    probe_model_inventory,
    provider_from_settings,
)
from brigade.rbac import ROLE_PERMISSIONS, can
from brigade.research_control import (
    acknowledge_research_alert,
    apply_research_policy,
    reconcile_research_alert,
    research_audit,
    research_policy,
    research_status,
    rollback_research_policy,
    stage_research_policy,
    test_research_component,
)
from brigade.schemas import (
    AGENT_ROLE_EXECUTIVE,
    AGENT_ROLES,
    Agent,
    Assignment,
    ChatMessage,
    Conversation,
    Goal,
    Mission,
    Priority,
    Role,
    Team,
    User,
    WorkMode,
)
from brigade.secrets import read_telegram_bot_token
from brigade.services import (
    OPS_ROOM_ROOMS,
    AssignmentActionError,
    ProposalAlreadyDecidedError,
    UnknownProposalError,
    _classify_chat_confirmation,
    _pending_chat_proposal,
    _record_operator_event,
    attach_operator_guidance,
    build_chat_payload,
    build_cockpit_payload,
    build_hierarchy_payload,
    build_ops_room_payload,
    build_orchestration_payload,
    build_settings_payload,
    cancel_assignment,
    create_scheduled_task,
    decide_proposal,
    delegate_from_crew_chief,
    get_runtime_overrides,
    lookup_assignment,
    reissue_assignment,
    reissue_assignment_as_new,
    send_orchestrator_chat,
    send_user_chat,
    set_config_value,
    set_runtime_overrides,
    update_assignment_fields,
)
from brigade.staff_meeting import meeting_public_view
from brigade.store import RedisRuntimeClient, StateStore, open_state_store
from brigade.time import utc_now_iso
from brigade.tui import build_dashboard_payload
from brigade.workspace import (
    agent_workspace_files,
    ensure_agent_workspace,
    validate_agent_workspace,
)


def create_app(
    settings: Settings | None = None,
    store: StateStore | None = None,
    *,
    connector_rate_limiter: ConnectorRateLimiter | None = None,
    telegram_http_post: HttpPost | None = None,
):
    try:
        from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
        from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
        from fastapi.staticfiles import StaticFiles
        from starlette.datastructures import MutableHeaders
    except ImportError as exc:  # pragma: no cover - exercised by CLI smoke without web extra
        raise RuntimeError(
            "install the web extra to run the gateway: pip install -e '.[web]'"
        ) from exc

    # FastAPI resolves route-handler annotations via get_type_hints against this
    # module's globals, but the web extra is imported lazily inside this
    # function. Expose BackgroundTasks at module scope so the annotation on the
    # Telegram webhook resolves instead of being treated as a query parameter.
    globals().setdefault("BackgroundTasks", BackgroundTasks)

    settings = settings or load_settings()
    store = store or open_state_store(settings)
    app = FastAPI(title="OpenBrigade Gateway", version=__version__)
    started_at = utc_now_iso()
    started_monotonic = time.monotonic()

    class SecurityHeadersMiddleware:
        def __init__(self, inner_app):
            self.inner_app = inner_app

        async def __call__(self, scope, receive, send):
            async def send_with_headers(message):
                if message["type"] == "http.response.start":
                    headers = MutableHeaders(scope=message)
                    headers.setdefault(
                        "Content-Security-Policy",
                        "default-src 'self'; script-src 'self'; "
                        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                        "connect-src 'self'",
                    )
                    headers.setdefault("X-Content-Type-Options", "nosniff")
                    headers.setdefault("X-Frame-Options", "DENY")
                    headers.setdefault("Referrer-Policy", "no-referrer")
                await send(message)

            await self.inner_app(scope, receive, send_with_headers)

    app.add_middleware(SecurityHeadersMiddleware)

    async def actor(authorization: str | None = Header(default=None)) -> AuthResult:
        if authorization:
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() != "bearer" or "\n" in token or "\r" in token:
                raise HTTPException(status_code=401, detail="invalid authorization header")
            result = verify_token(settings, token)
            if not result.ok:
                raise HTTPException(status_code=401, detail=result.reason or "invalid token")
            return result
        if settings.require_auth:
            raise HTTPException(status_code=401, detail="authorization required")
        users = store.users()
        if len(users) == 1:
            return AuthResult(ok=True, method="implicit-single-user", user=users[0])
        owners = [user for user in users if user.role == Role.OWNER]
        if len(owners) == 1:
            return AuthResult(ok=True, method="implicit-owner", user=owners[0])
        return AuthResult(ok=True, method="bootstrap", user=None)

    auth_dependency = Depends(actor)

    def require(permission: str, current: AuthResult) -> User | None:
        if not store.users() and not settings.require_auth:
            return current.user
        if not current.ok:
            raise HTTPException(status_code=401, detail=current.reason or "auth failed")
        if current.user is None:
            raise HTTPException(status_code=403, detail="no authenticated actor")
        if not can(current.user, permission):
            raise HTTPException(status_code=403, detail=f"missing permission: {permission}")
        return current.user

    def _connector_chat_turn(max_iterations: int):
        """Build a ConnectorChatTurn bound to this app's settings, or None when
        connector chief chat is disabled (the default single-shot path)."""
        if not (settings.connector_chief_chat_enabled and settings.chief_chat_enabled):
            return None

        def _turn(turn_store, incoming, username):
            return run_connector_chief_chat(
                turn_store,
                incoming,
                username,
                provider=provider_from_settings(settings),
                default_persona=settings.chief_chat_default_persona,
                max_iterations=max_iterations,
                history_window=settings.chief_chat_history_window,
                enable_web_fetch=settings.chief_chat_web_fetch_enabled,
                settings=settings,
                provider_factory=provider_from_settings,
            )

        return _turn

    def _connector_executive_turn(max_iterations: int):
        if not (settings.connector_executive_chat_enabled and settings.executive_enabled):
            return None

        def _turn(turn_store, incoming, username):
            return run_connector_executive_chat(
                turn_store,
                incoming,
                username,
                provider=provider_from_settings(settings),
                max_iterations=max_iterations,
                enable_web_fetch=settings.executive_web_fetch_enabled,
                settings=settings,
                provider_factory=provider_from_settings,
            )

        return _turn

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {"ok": True, "service": "brigade_web"}

    @app.post("/api/connectors/telegram/webhook")
    async def telegram_webhook(
        payload: dict[str, Any],
        background_tasks: BackgroundTasks,
        x_telegram_secret: str | None = Header(
            default=None,
            alias="X-Telegram-Bot-Api-Secret-Token",
        ),
        content_length: int | None = Header(default=None, alias="Content-Length"),
    ) -> dict[str, object]:
        if not settings.telegram_webhook_enabled:
            return {"ok": False, "status": "disabled", "provider": "telegram"}
        if settings.telegram_polling_enabled:
            raise HTTPException(
                status_code=409,
                detail="telegram polling is enabled; webhook ingress is disabled",
            )
        _require_live_connector_store(settings)
        if not settings.telegram_webhook_secret:
            raise HTTPException(status_code=503, detail="telegram webhook secret is not configured")
        if x_telegram_secret != settings.telegram_webhook_secret:
            raise HTTPException(status_code=401, detail="invalid telegram webhook secret")
        incoming = parse_telegram_update(payload)
        if incoming is None:
            return {"ok": True, "status": "ignored", "provider": "telegram"}
        if content_length and content_length > settings.connector_max_body_bytes:
            store.add_connector_audit_event(
                connector_audit_record(
                    provider="telegram",
                    direction="inbound",
                    status="rejected",
                    external_user_id=incoming.external_user_id,
                    conversation_id=incoming.conversation_id,
                    external_message_id=incoming.external_message_id,
                    reason="body too large",
                    metadata={"content_length": content_length},
                )
            )
            raise HTTPException(status_code=413, detail="telegram webhook body too large")
        if not settings.telegram_bot_token:
            raise HTTPException(status_code=503, detail="telegram bot token is not configured")
        connector_kwargs = dict(
            default_agent=settings.telegram_default_agent,
            model_provider=provider_from_settings(settings),
            outbound_sender=telegram_reply_sender(
                settings.telegram_bot_token,
                http_post=telegram_http_post,
            ),
            allowlist=parse_allowlist(settings.telegram_allowlist),
            rate_limiter=connector_rate_limiter or _connector_rate_limiter(settings),
            max_inbound_chars=settings.connector_max_inbound_chars,
            max_outbound_chars=settings.connector_max_outbound_chars,
            working_indicator=lambda: telegram_typing_indicator(
                settings.telegram_bot_token,
                chat_id=incoming.reply_target,
                http_post=telegram_http_post,
            ),
        )
        chat_turn = _connector_executive_turn(
            settings.executive_max_iterations
        ) or _connector_chat_turn(settings.chief_chat_max_iterations)

        if chat_turn is not None:
            # The chief-chat loop can run several model calls; do it out of band
            # so the webhook returns 200 fast and telegram_reply_sender posts the
            # reply when the turn finishes.
            background_tasks.add_task(
                process_live_connector_message,
                store,
                incoming,
                chat_turn=chat_turn,
                **connector_kwargs,
            )
            return {"ok": True, "status": "accepted", "provider": "telegram"}
        result = process_live_connector_message(store, incoming, **connector_kwargs)
        if result.status == "rate_limited":
            raise HTTPException(status_code=429, detail=result.reason or "rate limit exceeded")
        if result.status == "rejected":
            raise HTTPException(status_code=403, detail=result.reason or "rejected")
        return {"ok": result.status in {"complete", "pending_approval"}, **result.to_dict()}

    @app.post("/api/connectors/google-chat/webhook")
    async def google_chat_webhook(
        payload: dict[str, Any],
        token: str | None = None,
        content_length: int | None = Header(default=None, alias="Content-Length"),
    ) -> dict[str, object]:
        if not settings.google_chat_webhook_enabled:
            return {"ok": False, "status": "disabled", "provider": "google_chat"}
        _require_live_connector_store(settings)
        if not settings.google_chat_secret:
            raise HTTPException(status_code=503, detail="google chat secret is not configured")
        if token != settings.google_chat_secret:
            raise HTTPException(status_code=401, detail="invalid google chat token")
        incoming = parse_google_chat_event(payload)
        if incoming is None:
            return {"ok": True, "status": "ignored", "provider": "google_chat"}
        if content_length and content_length > settings.connector_max_body_bytes:
            store.add_connector_audit_event(
                connector_audit_record(
                    provider="google_chat",
                    direction="inbound",
                    status="rejected",
                    external_user_id=incoming.external_user_id,
                    conversation_id=incoming.conversation_id,
                    external_message_id=incoming.external_message_id,
                    reason="body too large",
                    metadata={"content_length": content_length},
                )
            )
            raise HTTPException(status_code=413, detail="google chat webhook body too large")
        # Google Chat needs a synchronous response body, so the chief-chat loop
        # runs inline with a tighter iteration cap to stay under webhook timeouts.
        result = process_live_connector_message(
            store,
            incoming,
            default_agent=settings.google_chat_default_agent,
            model_provider=provider_from_settings(settings),
            outbound_sender=google_chat_reply_sender(),
            allowlist=parse_allowlist(settings.google_chat_allowlist),
            rate_limiter=connector_rate_limiter or _connector_rate_limiter(settings),
            max_inbound_chars=settings.connector_max_inbound_chars,
            max_outbound_chars=settings.connector_max_outbound_chars,
            chat_turn=(
                _connector_executive_turn(settings.executive_max_iterations)
                or _connector_chat_turn(settings.chief_chat_connector_max_iterations)
            ),
        )
        if result.status == "rate_limited":
            raise HTTPException(status_code=429, detail=result.reason or "rate limit exceeded")
        if result.status == "rejected":
            raise HTTPException(status_code=403, detail=result.reason or "rejected")
        if result.response_body is not None:
            return result.response_body
        return {"ok": result.status in {"complete", "pending_approval"}, **result.to_dict()}

    @app.get("/api/auth/me")
    async def auth_me(current: AuthResult = auth_dependency) -> dict[str, object]:
        permissions = (
            sorted(ROLE_PERMISSIONS[current.user.role])
            if current.user is not None
            else []
        )
        return {
            "ok": current.ok,
            "method": current.method,
            "user": current.user.to_dict() if current.user else None,
            "permissions": permissions,
            "token": {
                "issued_at": current.claims.get("iat") if current.claims else None,
                "expires_at": current.claims.get("exp") if current.claims else None,
            },
        }

    @app.post("/api/auth/token")
    async def auth_token(
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, str]:
        require("auth:write", current)
        username = str(payload.get("username") or "").strip()
        role = payload.get("role")
        if not username:
            raise HTTPException(status_code=400, detail="username is required")
        user = next((item for item in store.users() if item.username == username), None)
        if user is None:
            if role is None:
                raise HTTPException(status_code=404, detail="unknown user")
            user = User(username=username, role=Role(str(role)))
            store.add_user(user)
        return {"token": issue_token(settings, user, int(payload.get("ttl_seconds") or 3600))}

    @app.get("/api/status")
    async def status(current: AuthResult = auth_dependency) -> dict[str, object]:
        require("status:read", current)
        return {
            "mission": store.mission().to_dict() if store.mission() else None,
            "agents": [agent.to_dict() for agent in store.agents()],
            "teams": [team.to_dict() for team in store.teams()],
            "assignments": [assignment.to_dict() for assignment in store.assignments()],
            "alerts": store.alerts(),
        }

    @app.get("/api/dashboard")
    async def dashboard(current: AuthResult = auth_dependency) -> dict[str, object]:
        require("status:read", current)
        return build_dashboard_payload(store)

    @app.get("/api/cockpit")
    async def cockpit(current: AuthResult = auth_dependency) -> dict[str, object]:
        require("status:read", current)
        return build_cockpit_payload(
            store,
            settings,
            datastore_checks=check_configured_datastores(settings),
            embedding_check=check_embedding_surface(settings),
            started_at=started_at,
            uptime_seconds=int(time.monotonic() - started_monotonic),
        )

    @app.get("/api/orchestration")
    async def orchestration(current: AuthResult = auth_dependency) -> dict[str, object]:
        require("status:read", current)
        return build_orchestration_payload(store)

    @app.get("/api/proposals")
    async def proposals(
        kind: str | None = None,
        status: str | None = None,
        limit: int | None = None,
        current: AuthResult = auth_dependency,
    ) -> list[dict[str, object]]:
        require("proposal:read", current)
        records = store.proposals(kind=kind, status=status)
        if limit is not None and limit > 0:
            records = records[-limit:]
        return records

    @app.post("/api/proposals/{proposal_id}/decision")
    async def decide_proposal_route(
        proposal_id: str,
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("proposal:write", current)
        decision = str(payload.get("decision") or "").strip()
        try:
            return decide_proposal(
                store,
                proposal_id=proposal_id,
                decision=decision,
                decided_by=user.username if user else "web",
                reason=payload.get("reason"),
            )
        except UnknownProposalError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ProposalAlreadyDecidedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/connectors/approvals")
    async def connector_approvals(
        provider: str | None = None,
        status: str | None = None,
        current: AuthResult = auth_dependency,
    ) -> list[dict[str, object]]:
        require("admin", current)
        return store.external_identities(provider=provider, status=status)

    @app.post("/api/connectors/approvals/decision")
    async def decide_connector_approval(
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("admin", current)
        provider = str(payload.get("provider") or "").strip()
        external_user_id = str(payload.get("external_user_id") or "").strip()
        decision = str(payload.get("decision") or "").strip()
        reason = payload.get("reason")
        if not provider or not external_user_id:
            raise HTTPException(
                status_code=400,
                detail="provider and external_user_id are required",
            )
        try:
            return decide_external_identity(
                store,
                provider=provider,
                external_user_id=external_user_id,
                decision=decision,
                decided_by=user.username if user else "web",
                username=str(payload.get("username") or "").strip() or None,
                reason=reason,
            )
        except UnknownExternalIdentityError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ExternalIdentityAlreadyDecidedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/connectors/telegram/accounts")
    async def telegram_accounts(
        current: AuthResult = auth_dependency,
    ) -> list[dict[str, object]]:
        require("admin", current)
        return [public_telegram_account(item) for item in store.telegram_accounts()]

    @app.post("/api/connectors/telegram/accounts")
    async def create_telegram_account(
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("admin", current)
        try:
            return save_telegram_account(
                store,
                settings,
                account_id=str(payload.get("account_id") or ""),
                chief_agent_id=str(payload.get("chief_agent_id") or ""),
                label=payload.get("label"),
                token=str(payload["token"]) if payload.get("token") else None,
                enabled=bool(payload.get("enabled", False)),
                actor=user.username if user else "web",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.patch("/api/connectors/telegram/accounts/{account_id}")
    async def update_telegram_account(
        account_id: str,
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("admin", current)
        existing = store.find_telegram_account(account_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="unknown Telegram account")
        try:
            return save_telegram_account(
                store,
                settings,
                account_id=account_id,
                chief_agent_id=str(
                    payload.get("chief_agent_id") or existing.get("chief_agent_id") or ""
                ),
                label=payload.get("label") or existing.get("label"),
                token=str(payload["token"]) if payload.get("token") else None,
                enabled=bool(payload.get("enabled", existing.get("enabled", False))),
                bot_username=str(existing.get("bot_username") or "") or None,
                actor=user.username if user else "web",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/connectors/telegram/accounts/{account_id}/test")
    async def test_telegram_account(
        account_id: str,
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("admin", current)
        account = store.find_telegram_account(account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="unknown Telegram account")
        token = read_telegram_bot_token(settings, account_id)
        if not token:
            raise HTTPException(status_code=409, detail="Telegram bot token is not configured")
        from brigade.telegram_polling import _telegram_api_post

        post = telegram_http_post or _telegram_api_post
        try:
            response = post(
                f"https://api.telegram.org/bot{token}/getMe",
                b"{}",
                {"Content-Type": "application/json"},
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if response.get("ok") is not True:
            raise HTTPException(
                status_code=502,
                detail=str(response.get("description") or "Telegram getMe failed"),
            )
        bot = response.get("result") if isinstance(response.get("result"), dict) else {}
        updated = save_telegram_account(
            store,
            settings,
            account_id=account_id,
            chief_agent_id=str(account["chief_agent_id"]),
            label=str(account.get("label") or account_id),
            enabled=bool(account.get("enabled")),
            bot_username=str(bot.get("username") or "") or None,
            actor=user.username if user else "web",
        )
        return {"ok": True, "account": updated, "bot_username": bot.get("username")}

    @app.delete("/api/connectors/telegram/accounts/{account_id}")
    async def delete_telegram_account_route(
        account_id: str,
        delete_secret: bool = False,
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("admin", current)
        try:
            removed = remove_telegram_account(
                store,
                settings,
                account_id,
                delete_secret=delete_secret,
                actor=user.username if user else "web",
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not removed:
            raise HTTPException(status_code=404, detail="unknown Telegram account")
        return {"removed": True, "account_id": account_id, "secret_removed": delete_secret}

    @app.get("/api/chief-chat/policies")
    async def chief_chat_policies(
        current: AuthResult = auth_dependency,
    ) -> list[dict[str, object]]:
        require("admin", current)
        return store.chief_chat_policies()

    @app.put("/api/chief-chat/policies/{chief_agent_id}")
    async def update_chief_chat_policy(
        chief_agent_id: str,
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("admin", current)
        try:
            return save_chief_chat_policy(
                store,
                settings,
                chief_agent_id=chief_agent_id,
                direct_enabled=bool(payload.get("direct_enabled", False)),
                tool_groups=[str(item) for item in payload.get("tool_groups") or []],
                max_tool_calls=(
                    int(payload["max_tool_calls"])
                    if payload.get("max_tool_calls") is not None
                    else None
                ),
                max_elapsed_seconds=(
                    int(payload["max_elapsed_seconds"])
                    if payload.get("max_elapsed_seconds") is not None
                    else None
                ),
                actor=user.username if user else "web",
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/models")
    async def models(current: AuthResult = auth_dependency) -> dict[str, object]:
        require("status:read", current)
        return available_model_options(settings, store.model_inventory())

    @app.post("/api/models/refresh")
    def refresh_models(current: AuthResult = auth_dependency) -> dict[str, object]:
        # sync endpoint: the provider probes do blocking network I/O, so let
        # FastAPI run this in its threadpool instead of stalling the loop
        require("admin", current)
        inventory = probe_model_inventory(settings)
        store.set_model_inventory(inventory)
        return available_model_options(settings, store.model_inventory())

    @app.put("/api/models/default")
    async def set_default_model(
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        nonlocal settings
        require("admin", current)
        provider = str(payload.get("provider") or "").strip()
        model = str(payload.get("model") or "").strip()
        if not provider or not model:
            raise HTTPException(status_code=400, detail="provider and model are required")
        set_config_value(settings.config_path, "default_provider", provider)
        set_config_value(settings.config_path, "default_model", model)
        settings = load_settings(settings.config_path)
        return {
            "settings": {
                **build_settings_payload(
                    settings, runtime_overrides=get_runtime_overrides(store)
                ),
                "api_version": __version__,
            },
            "models": available_model_options(settings, store.model_inventory()),
        }

    @app.get("/api/ops-room")
    async def ops_room(current: AuthResult = auth_dependency) -> dict[str, object]:
        require("status:read", current)
        return build_ops_room_payload(store)

    @app.get("/api/ops-room/events")
    async def ops_room_events(current: AuthResult = auth_dependency):
        require("status:read", current)

        async def events():
            while True:
                payload = build_ops_room_payload(store)
                yield f"event: snapshot\ndata: {json.dumps(payload, sort_keys=True)}\n\n"
                await asyncio.sleep(2.0)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/staff-meetings")
    async def staff_meetings(
        status: str | None = None,
        team_id: str | None = None,
        owner_username: str | None = None,
        query: str | None = None,
        limit: int = 100,
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        require("status:read", current)
        meetings = store.staff_meetings(
            status=status or None,
            team_id=team_id or None,
            owner_username=owner_username or None,
        )
        if query and query.strip():
            needle = query.strip().lower()
            meetings = [
                item
                for item in meetings
                if needle
                in " ".join(
                    [
                        str(item.get("meeting_id") or ""),
                        str(item.get("original_request") or ""),
                        str(item.get("chair_agent_id") or ""),
                        str(item.get("team_id") or ""),
                        str(item.get("owner_username") or ""),
                        str(item.get("status") or ""),
                        str(item.get("created_at") or ""),
                        str(item.get("completed_at") or ""),
                        str(item.get("termination_reason") or ""),
                        str(item.get("veto_status") or ""),
                        str(item.get("vote_result", {}).get("outcome") or ""),
                        " ".join(
                            str(seat.get("role_key") or "")
                            for seat in item.get("approved_roster", [])
                        ),
                    ]
                ).lower()
            ]
        selected = meetings[: max(1, min(int(limit), 500))]
        return {
            "meetings": [meeting_public_view(store, item) for item in selected],
            "count": len(meetings),
        }

    @app.get("/api/staff-meetings/{meeting_id}")
    async def staff_meeting_detail(
        meeting_id: str,
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        require("status:read", current)
        meeting = store.find_staff_meeting(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail="unknown Staff Meeting")
        return meeting_public_view(store, meeting, include_records=True)

    @app.get("/api/staff-meetings/{meeting_id}/events")
    async def staff_meeting_events(
        meeting_id: str,
        current: AuthResult = auth_dependency,
    ):
        require("status:read", current)
        if store.find_staff_meeting(meeting_id) is None:
            raise HTTPException(status_code=404, detail="unknown Staff Meeting")

        async def events():
            while True:
                meeting = store.find_staff_meeting(meeting_id)
                if meeting is None:
                    yield "event: deleted\ndata: {}\n\n"
                    return
                payload = meeting_public_view(store, meeting, include_records=True)
                yield f"event: snapshot\ndata: {json.dumps(payload, sort_keys=True)}\n\n"
                if meeting.get("status") in {
                    "COMPLETED",
                    "COMPLETED_WITH_DISSENT",
                    "COMPLETED_NO_CONSENSUS",
                    "MOTION_NOT_ADOPTED",
                    "BLOCKED_BY_VETO",
                    "FAILED_NO_QUORUM",
                    "CANCELLED",
                    "RESOURCE_LIMIT_REACHED",
                }:
                    return
                await asyncio.sleep(2.0)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.put("/api/mission")
    async def set_mission(
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        require("mission:write", current)
        statement = str(payload.get("statement") or "").strip()
        if not statement:
            raise HTTPException(status_code=400, detail="statement is required")
        mission = Mission(
            statement=statement,
            success_criteria=_string_list(payload.get("success_criteria")),
            explicitly_not=_string_list(payload.get("explicitly_not")),
        )
        store.set_mission(mission)
        return mission.to_dict()

    @app.post("/api/goals")
    async def add_goal(
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("goal:write", current)
        agent_id = str(payload.get("agent_id") or "").strip()
        if not agent_id:
            raise HTTPException(status_code=400, detail="agent_id is required")
        if next((agent for agent in store.agents() if agent.agent_id == agent_id), None) is None:
            raise HTTPException(status_code=404, detail="unknown agent")
        statement = str(payload.get("statement") or "").strip()
        if not statement:
            raise HTTPException(status_code=400, detail="statement is required")
        goal = Goal(
            statement=statement,
            success_criteria=_string_list(payload.get("success_criteria")),
            explicitly_not=_string_list(payload.get("explicitly_not")),
            set_by=str(payload.get("set_by") or (user.username if user else "web")),
            human_confirmed=bool(payload.get("human_confirmed", True)),
        )
        store.add_goal(agent_id, goal)
        return {"agent_id": agent_id, "goal": goal.to_dict()}

    @app.get("/api/agents")
    async def agents(current: AuthResult = auth_dependency) -> list[dict[str, object]]:
        require("status:read", current)
        return [agent.to_dict() for agent in store.agents()]

    @app.post("/api/agents")
    async def create_agent(
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        # Mirrors the CLI `agent onboard` flow so a brigade can be built from the
        # browser: create the agent record, seed its on-disk workspace, and (when a
        # team is named) join it, optionally as Crew Chief.
        require("agent:write", current)
        agent_id = str(payload.get("agent_id") or "").strip()
        if not agent_id:
            raise HTTPException(status_code=400, detail="agent_id is required")
        display_name = str(payload.get("display_name") or agent_id).strip()
        team_id = str(payload.get("team_id") or "").strip() or None
        make_chief = bool(payload.get("crew_chief"))
        if make_chief and not team_id:
            raise HTTPException(status_code=400, detail="crew_chief requires team_id")
        workspace = str(payload.get("workspace_path") or "").strip() or f"workspace-{agent_id}"
        role = str(payload.get("role") or ("crew_chief" if make_chief else "line_worker")).strip()
        if role not in AGENT_ROLES:
            raise HTTPException(
                status_code=400,
                detail="role must be one of: " + ", ".join(sorted(AGENT_ROLES)),
            )
        owner_username = str(payload.get("owner_username") or "").strip() or None
        if role == AGENT_ROLE_EXECUTIVE:
            if team_id or make_chief:
                raise HTTPException(
                    status_code=400,
                    detail="executive agents are user-owned and cannot join teams",
                )
            owner = next(
                (
                    item
                    for item in store.users()
                    if item.username == owner_username and item.role == Role.OWNER
                ),
                None,
            )
            if owner is None:
                raise HTTPException(
                    status_code=400,
                    detail="executive agents require owner_username for an existing owner user",
                )
        team: Team | None = None
        if team_id:
            team = next((item for item in store.teams() if item.team_id == team_id), None)
            if team is None:
                if not bool(payload.get("create_team")):
                    raise HTTPException(
                        status_code=400,
                        detail=f"unknown team: {team_id}; set create_team to create it",
                    )
                team = Team(team_id=team_id, display_name=team_id)
                store.upsert_team(team)
        agent = Agent(
            agent_id=agent_id,
            display_name=display_name,
            workspace_path=workspace,
            role=role,
            team_id=team_id,
            model_provider=str(payload.get("model_provider") or settings.default_provider),
            model_name=str(payload.get("model_name") or settings.default_model),
            specialties=_string_list(payload.get("specialties")),
            owner_username=owner_username,
        )
        store.add_agent(agent)
        ensure_agent_workspace(agent, settings.data_dir)
        ensure_policy_projections_current(store, agent, actor="web_agent_onboard")
        if team is not None:
            members = list(dict.fromkeys([*team.members, agent_id]))
            team = Team(
                team_id=team.team_id,
                display_name=team.display_name,
                description=team.description,
                parent_team_id=team.parent_team_id,
                crew_chief_id=agent_id if make_chief else team.crew_chief_id,
                members=members,
                delegation_policy=team.delegation_policy,
                escalation_team_id=team.escalation_team_id,
                created_at=team.created_at,
            )
            store.upsert_team(team)
        diagnostics = validate_agent_workspace(agent, settings.data_dir)
        return {
            "agent": agent.to_dict(),
            "team": team.to_dict() if team else None,
            "workspace": str(settings.data_dir / agent.workspace_path),
            "diagnostics": [item.to_dict() for item in diagnostics],
            "valid": not any(item.severity == "error" for item in diagnostics),
        }

    @app.delete("/api/agents/{agent_id}")
    async def delete_agent(
        agent_id: str,
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        require("agent:write", current)
        if next((agent for agent in store.agents() if agent.agent_id == agent_id), None) is None:
            raise HTTPException(status_code=404, detail="unknown agent")
        # Scrub the agent from any team membership / Crew Chief slot before deleting.
        for team in store.teams():
            if agent_id in team.members or team.crew_chief_id == agent_id:
                chief_id = None if team.crew_chief_id == agent_id else team.crew_chief_id
                store.upsert_team(
                    Team(
                        team_id=team.team_id,
                        display_name=team.display_name,
                        description=team.description,
                        parent_team_id=team.parent_team_id,
                        crew_chief_id=chief_id,
                        members=[member for member in team.members if member != agent_id],
                        delegation_policy=team.delegation_policy,
                        escalation_team_id=team.escalation_team_id,
                        created_at=team.created_at,
                    )
                )
        store.delete_agent(agent_id)
        return {"status": "deleted", "agent_id": agent_id}

    @app.patch("/api/agents/{agent_id}")
    async def update_agent(
        agent_id: str,
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        require("agent:write", current)
        agent = next(
            (item for item in store.agents() if item.agent_id == agent_id), None
        )
        if agent is None:
            raise HTTPException(status_code=404, detail="unknown agent")
        updates: dict[str, Any] = {}
        if payload.get("model_provider") is not None:
            updates["model_provider"] = str(payload["model_provider"])
        if payload.get("model_name") is not None:
            updates["model_name"] = str(payload["model_name"])
        if payload.get("role") is not None:
            role = str(payload["role"]).strip()
            if role not in AGENT_ROLES:
                raise HTTPException(
                    status_code=400,
                    detail="role must be one of: " + ", ".join(sorted(AGENT_ROLES)),
                )
            updates["role"] = role
        if payload.get("owner_username") is not None:
            owner_username = str(payload["owner_username"]).strip() or None
            if owner_username is not None and next(
                (
                    item
                    for item in store.users()
                    if item.username == owner_username and item.role == Role.OWNER
                ),
                None,
            ) is None:
                raise HTTPException(status_code=400, detail="unknown owner_username")
            updates["owner_username"] = owner_username
        if payload.get("specialties") is not None:
            updates["specialties"] = _string_list(payload["specialties"])
        if not updates:
            raise HTTPException(status_code=400, detail="no updatable fields provided")
        updated = replace(agent, **updates)
        if updated.role == AGENT_ROLE_EXECUTIVE and not updated.owner_username:
            raise HTTPException(
                status_code=400, detail="executive agents require owner_username"
            )
        store.add_agent(updated)
        ensure_agent_workspace(updated, settings.data_dir)
        ensure_policy_projections_current(store, updated, actor="web_agent_update")
        return updated.to_dict()

    MAX_WORKSPACE_FILE_BYTES = 64 * 1024

    def _workspace_file_path(agent_id: str, filename: str) -> Path:
        agent = next(
            (item for item in store.agents() if item.agent_id == agent_id), None
        )
        if agent is None:
            raise HTTPException(status_code=404, detail="unknown agent")
        # Whitelist membership is the traversal guard: only bare manifest
        # filenames are ever joined to the workspace path.
        allowed = agent_workspace_files(agent)
        if filename not in allowed:
            raise HTTPException(
                status_code=400,
                detail=(
                    "filename must be one of: " + ", ".join(allowed)
                ),
            )
        return settings.data_dir / agent.workspace_path / filename

    @app.get("/api/agents/{agent_id}/files/{filename}")
    async def read_agent_file(
        agent_id: str,
        filename: str,
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        require("status:read", current)
        path = _workspace_file_path(agent_id, filename)
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            raise HTTPException(status_code=404, detail="file not found") from None
        return {"agent_id": agent_id, "filename": filename, "content": content}

    @app.put("/api/agents/{agent_id}/files/{filename}")
    async def write_agent_file(
        agent_id: str,
        filename: str,
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        require("agent:write", current)
        path = _workspace_file_path(agent_id, filename)
        content = payload.get("content")
        if not isinstance(content, str):
            raise HTTPException(status_code=400, detail="content must be a string")
        if len(content.encode("utf-8")) > MAX_WORKSPACE_FILE_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"content exceeds {MAX_WORKSPACE_FILE_BYTES} bytes",
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        agent = next(item for item in store.agents() if item.agent_id == agent_id)
        accepted = accept_policy_projections(
            store,
            agent,
            paths=[filename],
            actor=current.user.username if current.user else "web",
            source="operator:web-file-update",
        )
        return {
            "status": "saved",
            "agent_id": agent_id,
            "filename": filename,
            "content_hash": accepted[0]["content_hash"],
        }

    @app.get("/api/teams")
    async def teams(current: AuthResult = auth_dependency) -> dict[str, object]:
        require("team:read", current)
        return build_hierarchy_payload(store)

    @app.post("/api/teams")
    async def create_team(
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        require("team:write", current)
        team = Team(
            team_id=str(payload["team_id"]),
            display_name=str(payload.get("display_name") or payload["team_id"]),
            description=payload.get("description"),
            parent_team_id=payload.get("parent_team_id"),
            delegation_policy=str(payload.get("delegation_policy") or "chief_only"),
            escalation_team_id=payload.get("escalation_team_id"),
        )
        store.upsert_team(team)
        return team.to_dict()

    @app.patch("/api/teams/{team_id}")
    async def update_team(
        team_id: str,
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        require("team:write", current)
        existing = next((team for team in store.teams() if team.team_id == team_id), None)
        if existing is None:
            raise HTTPException(status_code=404, detail="unknown team")
        members = list(payload.get("members", existing.members))
        team = Team(
            team_id=existing.team_id,
            display_name=str(payload.get("display_name") or existing.display_name),
            description=payload.get("description", existing.description),
            parent_team_id=payload.get("parent_team_id", existing.parent_team_id),
            crew_chief_id=payload.get("crew_chief_id", existing.crew_chief_id),
            members=members,
            delegation_policy=str(payload.get("delegation_policy") or existing.delegation_policy),
            escalation_team_id=payload.get("escalation_team_id", existing.escalation_team_id),
            created_at=existing.created_at,
        )
        store.upsert_team(team)
        # Keep each agent's denormalized team_id consistent with membership changes.
        added = set(members) - set(existing.members)
        removed = set(existing.members) - set(members)
        if added or removed:
            agents_by_id = {agent.agent_id: agent for agent in store.agents()}
            for agent_id in added:
                agent = agents_by_id.get(agent_id)
                if agent is not None and agent.team_id != team_id:
                    store.add_agent(_agent_with_team_id(agent, team_id))
            for agent_id in removed:
                agent = agents_by_id.get(agent_id)
                if agent is not None and agent.team_id == team_id:
                    store.add_agent(_agent_with_team_id(agent, None))
        return team.to_dict()

    @app.post("/api/teams/{team_id}/delegate")
    async def delegate_team_work(
        team_id: str,
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("team:write", current)
        chief_agent_id = str(payload.get("chief_agent_id") or "").strip()
        target_agent_id = str(payload.get("target_agent_id") or "").strip()
        assignment_text = str(payload.get("assignment") or "").strip()
        if not chief_agent_id or not target_agent_id or not assignment_text:
            raise HTTPException(
                status_code=400,
                detail="chief_agent_id, target_agent_id, and assignment are required",
            )
        try:
            return delegate_from_crew_chief(
                store,
                team_id=team_id,
                chief_agent_id=chief_agent_id,
                target_agent_id=target_agent_id,
                assignment_text=assignment_text,
                goal_statement=payload.get("goal_statement"),
                rationale=payload.get("rationale"),
                priority=Priority(str(payload.get("priority") or "normal")),
                current_user=user,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/tasks")
    async def tasks(current: AuthResult = auth_dependency) -> list[dict[str, object]]:
        require("task:read", current)
        return [assignment.to_dict() for assignment in store.assignments()]

    @app.get("/api/schedules")
    async def schedules(current: AuthResult = auth_dependency) -> list[dict[str, object]]:
        user = require("task:read", current)
        visible: list[dict[str, object]] = []
        for recurrence in store.recurrences():
            template = recurrence.get("template") or {}
            if template.get("target_kind") != "executive":
                visible.append(recurrence)
                continue
            if user is not None and template.get("owner_username") == user.username:
                visible.append(recurrence)
        return visible

    @app.post("/api/schedules")
    async def create_schedule(
        payload: dict[str, Any], current: AuthResult = auth_dependency
    ) -> dict[str, object]:
        user = require("task:write", current)
        try:
            return create_scheduled_task(
                store,
                agent_id=str(payload.get("agent_id") or ""),
                assignment=str(payload.get("assignment") or ""),
                owner_username=user.username if user else None,
                cron=payload.get("cron"),
                interval_seconds=payload.get("interval_seconds"),
                next_due_at=payload.get("next_due_at"),
                priority=str(payload.get("priority") or "normal"),
                label=payload.get("label"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/schedules/{recurrence_id}/enabled")
    async def set_schedule_enabled(
        recurrence_id: str,
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("task:write", current)
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=400, detail="enabled must be true or false")
        recurrence = next(
            (item for item in store.recurrences() if item.get("recurrence_id") == recurrence_id),
            None,
        )
        if recurrence is None:
            raise HTTPException(status_code=404, detail="unknown schedule")
        template = recurrence.get("template") or {}
        if template.get("target_kind") == "executive" and (
            user is None or template.get("owner_username") != user.username
        ):
            raise HTTPException(status_code=403, detail="not your Executive schedule")
        recurrence["enabled"] = enabled
        recurrence["updated_at"] = utc_now_iso()
        store.update_recurrence(recurrence)
        return recurrence

    @app.delete("/api/alerts")
    async def clear_alerts(current: AuthResult = auth_dependency) -> dict[str, object]:
        require("orchestrator:write", current)
        return {"status": "cleared", "count": store.clear_alerts()}

    @app.post("/api/tasks")
    async def create_task(
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("task:write", current)
        room_id = str(payload.get("room_id") or "").strip().lower() or None
        work_room_ids = {room["id"] for room in OPS_ROOM_ROOMS if room.get("kind") == "work"}
        if room_id and room_id not in work_room_ids:
            raise HTTPException(status_code=400, detail="unknown task room")
        agent_id = str(payload["agent_id"])
        if agent_id not in {agent.agent_id for agent in store.agents()}:
            raise HTTPException(status_code=400, detail=f"unknown agent: {agent_id}")
        assignment = Assignment(
            assignment=str(payload["assignment"]),
            assigned_to=agent_id,
            created_by=user.username if user else "web",
            source="web_gateway",
            priority=Priority(str(payload.get("priority") or "normal")),
            work_mode=WorkMode(str(payload.get("work_mode") or WorkMode.HEARTBEAT.value)),
            goal_statement=payload.get("goal_statement"),
            assignment_rationale=payload.get("rationale"),
            created_by_user_id=user.username if user else None,
            created_by_role=user.role.value if user else None,
            idempotency_key=payload.get("idempotency_key"),
            room_id=room_id,
        )
        persisted = store.add_assignment(assignment)
        return persisted.to_dict()

    @app.get("/api/tasks/{assignment_id}")
    async def task(
        assignment_id: str,
        include_history: bool = True,
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        require("task:read", current)
        if include_history:
            found = lookup_assignment(store, assignment_id)
            if found is None:
                raise HTTPException(status_code=404, detail="unknown assignment")
            return found
        assignment = store.find_assignment(assignment_id)
        if assignment is None:
            raise HTTPException(status_code=404, detail="unknown assignment")
        return assignment.to_dict()

    @app.delete("/api/tasks/{assignment_id}")
    async def cancel_task(
        assignment_id: str,
        force: bool = False,
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("task:write", current)
        if store.find_assignment(assignment_id) is None:
            raise HTTPException(status_code=404, detail="unknown assignment")
        try:
            return cancel_assignment(
                store,
                assignment_id,
                by=user.username if user else "web",
                force=force,
            )
        except AssignmentActionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/tasks/{assignment_id}/reissue")
    async def reissue_task(
        assignment_id: str,
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("task:write", current)
        if store.find_assignment(assignment_id) is None:
            raise HTTPException(status_code=404, detail="unknown assignment")
        try:
            return reissue_assignment(store, assignment_id, by=user.username if user else "web")
        except AssignmentActionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.patch("/api/tasks/{assignment_id}")
    async def edit_task(
        assignment_id: str,
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("task:write", current)
        if store.find_assignment(assignment_id) is None:
            raise HTTPException(status_code=404, detail="unknown assignment")
        try:
            return update_assignment_fields(
                store,
                assignment_id,
                assignment_text=payload.get("assignment"),
                priority=payload.get("priority"),
                assigned_to=payload.get("assigned_to") or payload.get("agent_id"),
                by=user.username if user else "web",
            )
        except AssignmentActionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/tasks/{assignment_id}/reissue-as-new")
    async def reissue_task_as_new(
        assignment_id: str,
        payload: dict[str, Any] | None = None,
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("task:write", current)
        if store.find_assignment(assignment_id) is None:
            raise HTTPException(status_code=404, detail="unknown assignment")
        try:
            return reissue_assignment_as_new(
                store,
                assignment_id,
                by=user.username if user else "web",
                note=(payload or {}).get("note"),
            )
        except AssignmentActionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/tasks/{assignment_id}/guidance")
    async def attach_task_guidance(
        assignment_id: str,
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("chat:write", current)
        message = str(payload.get("message") or "").strip()
        if not message:
            raise HTTPException(status_code=422, detail="message is required")
        if store.find_assignment(assignment_id) is None:
            raise HTTPException(status_code=404, detail="unknown assignment")
        try:
            return attach_operator_guidance(
                store,
                assignment_id,
                operator=user.username if user else "web",
                message=message,
            )
        except AssignmentActionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/chat/channels")
    async def chat_channels(current: AuthResult = auth_dependency) -> list[dict[str, object]]:
        require("chat:read", current)
        return build_chat_payload(store)["channels"]

    @app.get("/api/chat/messages")
    async def chat_messages(
        channel: str | None = None,
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        require("chat:read", current)
        return build_chat_payload(store, channel=channel)

    @app.post("/api/chat/messages")
    async def chat_send(
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("chat:write", current)
        message = ChatMessage(
            channel=str(payload["channel"]),
            sender=str(payload.get("sender") or (user.username if user else "web")),
            recipient=str(payload["recipient"]),
            content=str(payload["content"]),
            metadata={
                "kind": "web_chat_message",
                "idempotency_key": payload.get("idempotency_key"),
            },
        )
        store.add_message(message)
        return message.to_dict()

    @app.post("/api/chat/ask-agent")
    async def chat_ask_agent(
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("chat:write", current)
        provider = _provider_from_payload(payload, settings)
        return send_user_chat(
            store,
            current,
            user=user,
            agent_id=str(payload["agent_id"]),
            content=str(payload["content"]),
            provider=provider,
            channel=payload.get("channel"),
            idempotency_key=payload.get("idempotency_key") or f"web:{uuid4()}",
            resume_escalations=bool(payload.get("resume_escalations")),
            guidance_assignment_id=payload.get("guidance_assignment_id"),
        )

    @app.post("/api/chat/ask-orchestrator")
    async def chat_ask_orchestrator(
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("chat:write", current)
        provider = _provider_from_payload(payload, settings)
        return send_orchestrator_chat(
            store,
            current,
            user=user,
            content=str(payload["content"]),
            provider=provider,
            channel=str(payload.get("channel") or "orchestrator"),
            idempotency_key=payload.get("idempotency_key") or f"web-orchestrator:{uuid4()}",
        )

    @app.post("/api/chat/ask-orchestrator-markdown")
    async def chat_ask_orchestrator_markdown(
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("chat:write", current)
        channel = str(payload.get("channel") or "orchestrator")
        provider = _provider_from_payload(payload, settings)
        result = send_orchestrator_chat(
            store,
            current,
            user=user,
            content=str(payload["content"]),
            provider=provider,
            channel=channel,
            idempotency_key=payload.get("idempotency_key") or f"web-orchestrator-md:{uuid4()}",
        )
        response_message_id = result.get("response_message_id")
        if not isinstance(response_message_id, str):
            return {**result, "response_markdown": "", "response_html": ""}
        response_message = next(
            (item for item in store.messages(channel) if item.message_id == response_message_id),
            None,
        )
        response_markdown = response_message.content if response_message else ""
        return {
            **result,
            "response_markdown": response_markdown,
            "response_html": render_markdown_html(response_markdown),
        }

    def _operator_username(user: User | None) -> str:
        return user.username if user else "operator"

    def _thread_or_404(thread_id: str):
        conversation = store.find_conversation(thread_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail=f"unknown thread: {thread_id}")
        return conversation

    def _thread_payload(conversation: Conversation) -> dict[str, object]:
        fallback_provider = settings.default_provider
        fallback_model = settings.default_model
        try:
            if conversation.persona.startswith("executive:"):
                persona = resolve_executive_persona(
                    store, conversation.operator_username, conversation.persona
                )
                agent_id = persona.agent_id
            else:
                persona = resolve_persona(store, conversation.persona)
                agent_id = persona.chief_agent_id
            agent = next(
                (item for item in store.agents() if item.agent_id == agent_id), None
            )
            if agent is not None:
                fallback_provider = agent.model_provider
                fallback_model = agent.model_name
        except (UnknownPersonaError, UnknownExecutiveError):
            pass
        provider_name, model_name, base_url = effective_model_route(
            conversation,
            fallback_provider=fallback_provider,
            fallback_model=fallback_model,
        )
        return {
            **conversation.to_dict(),
            "channel": conversation.channel,
            "effective_model_provider": provider_name,
            "effective_model_name": model_name,
            "effective_model_base_url": base_url,
        }

    @app.get("/api/chat/threads")
    async def chat_threads(current: AuthResult = auth_dependency) -> dict[str, object]:
        user = require("chat:read", current)
        username = _operator_username(user)
        return {
            "threads": [_thread_payload(item) for item in store.conversations(username)],
            "personas": [
                *[item.to_dict() for item in available_executive_personas(store, username)],
                *[item.to_dict() for item in available_personas(store)],
            ],
        }

    @app.post("/api/chat/threads")
    async def chat_thread_open(
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("chat:write", current)
        username = _operator_username(user)
        requested_persona = str(payload.get("persona") or "")
        try:
            if requested_persona.startswith("executive"):
                persona = resolve_executive_persona(store, username, requested_persona)
                conversation = store.resolve_active_conversation(
                    username,
                    persona.persona_id,
                    title=payload.get("title") or persona.display_name,
                )
                return _thread_payload(conversation)
            persona = resolve_persona(
                store,
                payload.get("persona"),
                default=settings.chief_chat_default_persona,
            )
        except (UnknownPersonaError, UnknownExecutiveError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        conversation = store.resolve_active_conversation(
            username,
            persona.persona_id,
            chief_agent_id=persona.chief_agent_id,
            team_id=persona.team_id,
            title=payload.get("title") or persona.display_name,
        )
        return _thread_payload(conversation)

    @app.get("/api/chat/threads/{thread_id}/messages")
    async def chat_thread_messages(
        thread_id: str,
        limit: int = 100,
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("chat:read", current)
        conversation = _thread_or_404(thread_id)
        if conversation.operator_username != _operator_username(user):
            raise HTTPException(status_code=403, detail="not your thread")
        return {
            "thread": _thread_payload(conversation),
            "messages": [
                message.to_dict()
                for message in store.recent_messages(conversation.channel, limit=limit)
            ],
        }

    @app.post("/api/chat/threads/{thread_id}/model")
    async def chat_thread_model(
        thread_id: str,
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("chat:write", current)
        conversation = _thread_or_404(thread_id)
        if conversation.operator_username != _operator_username(user):
            raise HTTPException(status_code=403, detail="not your thread")
        provider_name = str(payload.get("provider") or "").strip()
        model_name = str(payload.get("model") or "").strip()
        if not provider_name or not model_name:
            raise HTTPException(status_code=422, detail="provider and model are required")
        options = available_model_options(settings, store.model_inventory()).get("options", [])
        selected = next(
            (
                item
                for item in options
                if item.get("provider") == provider_name
                and item.get("model") == model_name
                and item.get("available")
                and item.get("configured", True)
            ),
            None,
        )
        if selected is None:
            raise HTTPException(status_code=400, detail="model route is not available")
        persist_model_route(
            store,
            conversation,
            provider=provider_name,
            model=model_name,
            base_url=str(selected["base_url"]) if selected.get("base_url") else None,
        )
        return _thread_payload(conversation)

    @app.post("/api/chat/threads/{thread_id}/messages")
    async def chat_thread_send(
        thread_id: str,
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("chat:write", current)
        conversation = _thread_or_404(thread_id)
        username = _operator_username(user)
        if conversation.operator_username != username:
            raise HTTPException(status_code=403, detail="not your thread")
        content = str(payload.get("content") or "").strip()
        if not content:
            raise HTTPException(status_code=400, detail="content is required")
        if conversation.persona.startswith("executive:"):
            if not settings.executive_enabled:
                raise HTTPException(status_code=503, detail="executive chat is disabled")
            try:
                executive_persona = resolve_executive_persona(
                    store, username, conversation.persona
                )
            except UnknownExecutiveError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            executive_agent = next(
                item for item in store.agents() if item.agent_id == executive_persona.agent_id
            )
            command = parse_chat_command(content)
            if command and command.verb in {"help", "who", "status", "model", "new"}:
                if command.verb == "help":
                    summary = command_help(fixed_persona=True)
                elif command.verb == "who":
                    summary = f"You're talking to {executive_persona.display_name}."
                elif command.verb == "status":
                    provider_name, model_name, _ = effective_model_route(
                        conversation,
                        fallback_provider=executive_agent.model_provider,
                        fallback_model=executive_agent.model_name,
                    )
                    summary = (
                        f"Chat status: active\nPersona: {executive_persona.display_name}\n"
                        f"Model: {provider_name} / {model_name}"
                    )
                elif command.verb == "model":
                    summary = model_command_reply(
                        store,
                        settings,
                        conversation,
                        command.argument,
                        fallback_provider=executive_agent.model_provider,
                        fallback_model=executive_agent.model_name,
                    )
                else:
                    conversation.status = "archived"
                    store.upsert_conversation(conversation)
                    fresh = store.resolve_active_conversation(
                        username,
                        executive_persona.persona_id,
                        title=executive_persona.display_name,
                    )
                    summary = f"Started a fresh conversation with {executive_persona.display_name}."
                    request_id, response_id = record_command_exchange(
                        store,
                        fresh,
                        operator=username,
                        assistant=executive_persona.agent_id,
                        command=content,
                        reply=summary,
                    )
                    return {
                        "status": "complete",
                        "summary": summary,
                        "thread_id": fresh.thread_id,
                        "conversation_id": fresh.channel,
                        "agent_id": executive_persona.agent_id,
                        "request_message_id": request_id,
                        "response_message_id": response_id,
                    }
                request_id, response_id = record_command_exchange(
                    store,
                    conversation,
                    operator=username,
                    assistant=executive_persona.agent_id,
                    command=content,
                    reply=summary,
                )
                return {
                    "status": "complete",
                    "summary": summary,
                    "conversation_id": conversation.channel,
                    "agent_id": executive_persona.agent_id,
                    "request_message_id": request_id,
                    "response_message_id": response_id,
                }
            pending = _pending_chat_proposal(
                store,
                conversation.channel,
                kind_prefix=EXECUTIVE_CHAT_KIND_PREFIX,
            )
            if pending is not None and _classify_chat_confirmation(content) == "confirm":
                require("task:write", current)
            provider = _thread_provider(
                payload,
                settings,
                store,
                conversation,
                fallback_provider=executive_agent.model_provider,
                fallback_model=executive_agent.model_name,
            )
            return run_executive_chat_turn(
                store,
                thread=conversation,
                persona=executive_persona,
                operator=username,
                content=content,
                provider=provider,
                max_iterations=settings.executive_max_iterations,
                enable_web_fetch=settings.executive_web_fetch_enabled,
                idempotency_key=payload.get("idempotency_key") or f"web-executive:{uuid4()}",
            )
        if not settings.chief_chat_enabled:
            raise HTTPException(status_code=503, detail="chief chat is disabled")
        try:
            persona = resolve_persona(store, conversation.persona)
        except UnknownPersonaError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        pending = _pending_chat_proposal(
            store, conversation.channel, kind_prefix=CHIEF_CHAT_KIND_PREFIX
        )
        chief_agent = next(
            (
                item
                for item in store.agents()
                if item.agent_id == persona.chief_agent_id
            ),
            None,
        )
        fallback_provider = (
            chief_agent.model_provider if chief_agent else settings.default_provider
        )
        fallback_model = chief_agent.model_name if chief_agent else settings.default_model
        command = parse_chat_command(content)
        direct_enabled = bool(
            persona.chief_agent_id
            and user is not None
            and user.role == Role.OWNER
            and (store.chief_chat_policy(persona.chief_agent_id) or {}).get("direct_enabled")
        )
        basic_command = bool(
            command
            and (
                command.verb in {"help", "who", "model", "new"}
                or (
                    not direct_enabled
                    and pending is None
                    and command.verb in {"status", "cancel", "confirm"}
                )
            )
        )
        if command and basic_command:
            if command.verb == "help":
                summary = command_help(
                    fixed_persona=True, direct_enabled=direct_enabled
                )
            elif command.verb == "who":
                summary = f"You're talking to {persona.display_name}."
            elif command.verb == "model":
                summary = model_command_reply(
                    store,
                    settings,
                    conversation,
                    command.argument,
                    fallback_provider=fallback_provider,
                    fallback_model=fallback_model,
                )
            elif command.verb == "status":
                provider_name, model_name, _ = effective_model_route(
                    conversation,
                    fallback_provider=fallback_provider,
                    fallback_model=fallback_model,
                )
                summary = (
                    f"Chat status: active\nPersona: {persona.display_name}\n"
                    f"Model: {provider_name} / {model_name}"
                )
            elif command.verb in {"cancel", "confirm"}:
                summary = "There is no active direct Chief turn."
            else:
                conversation.status = "archived"
                store.upsert_conversation(conversation)
                fresh = store.resolve_active_conversation(
                    username,
                    persona.persona_id,
                    chief_agent_id=persona.chief_agent_id,
                    team_id=persona.team_id,
                    title=persona.display_name,
                )
                summary = f"Started a fresh conversation with {persona.display_name}."
                request_id, response_id = record_command_exchange(
                    store,
                    fresh,
                    operator=username,
                    assistant=persona.chief_agent_id or FRONT_DESK_PERSONA,
                    command=content,
                    reply=summary,
                )
                return {
                    "status": "complete",
                    "summary": summary,
                    "thread_id": fresh.thread_id,
                    "conversation_id": fresh.channel,
                    "agent_id": persona.chief_agent_id or FRONT_DESK_PERSONA,
                    "request_message_id": request_id,
                    "response_message_id": response_id,
                }
            request_id, response_id = record_command_exchange(
                store,
                conversation,
                operator=username,
                assistant=persona.chief_agent_id or FRONT_DESK_PERSONA,
                command=content,
                reply=summary,
            )
            return {
                "status": "complete",
                "summary": summary,
                "conversation_id": conversation.channel,
                "agent_id": persona.chief_agent_id or FRONT_DESK_PERSONA,
                "request_message_id": request_id,
                "response_message_id": response_id,
            }
        if pending is not None and _classify_chat_confirmation(content) == "confirm":
            require("task:write", current)
        if (
            pending is None
            and persona.chief_agent_id
            and user is not None
            and user.role == Role.OWNER
            and direct_enabled
        ):
            control = direct_turn_control(
                store,
                thread=conversation,
                operator_username=username,
                command=content,
            )
            if control is not None:
                return control
            try:
                turn = queue_direct_chief_turn(
                    store,
                    thread=conversation,
                    chief_agent_id=persona.chief_agent_id,
                    operator_username=username,
                    content=content,
                    idempotency_key=(
                        payload.get("idempotency_key") or f"web-chief-direct:{uuid4()}"
                    ),
                )
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return JSONResponse(
                status_code=202,
                content={
                    "status": "queued",
                    "turn_id": turn["turn_id"],
                    "conversation_id": conversation.channel,
                    "agent_id": persona.chief_agent_id,
                },
            )
        provider = _thread_provider(
            payload,
            settings,
            store,
            conversation,
            fallback_provider=fallback_provider,
            fallback_model=fallback_model,
        )
        return run_chief_chat_turn(
            store,
            thread=conversation,
            persona=persona,
            operator=username,
            content=content,
            provider=provider,
            max_iterations=settings.chief_chat_max_iterations,
            history_window=settings.chief_chat_history_window,
            enable_web_fetch=settings.chief_chat_web_fetch_enabled,
            idempotency_key=payload.get("idempotency_key") or f"web-chief:{uuid4()}",
        )

    @app.post("/api/chat/threads/{thread_id}/persona")
    async def chat_thread_switch_persona(
        thread_id: str,
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        # A thread keeps one persona for life: switching resolves (or creates)
        # the caller's active thread for the requested persona instead.
        user = require("chat:write", current)
        _thread_or_404(thread_id)
        requested_persona = str(payload.get("persona") or "")
        try:
            if requested_persona.startswith("executive"):
                executive_persona = resolve_executive_persona(
                    store, _operator_username(user), requested_persona
                )
                conversation = store.resolve_active_conversation(
                    _operator_username(user),
                    executive_persona.persona_id,
                    title=executive_persona.display_name,
                )
                return _thread_payload(conversation)
            persona = resolve_persona(store, requested_persona)
        except (UnknownPersonaError, UnknownExecutiveError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        conversation = store.resolve_active_conversation(
            _operator_username(user),
            persona.persona_id,
            chief_agent_id=persona.chief_agent_id,
            team_id=persona.team_id,
            title=persona.display_name,
        )
        return _thread_payload(conversation)

    @app.get("/api/chat/turns/{turn_id}")
    async def chief_direct_turn_status(
        turn_id: str,
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("chat:read", current)
        turn = store.find_chief_interactive_turn(turn_id)
        if turn is None:
            raise HTTPException(status_code=404, detail="unknown direct Chief turn")
        if turn.get("operator_username") != _operator_username(user):
            raise HTTPException(status_code=403, detail="not your turn")
        return turn

    @app.post("/api/chat/turns/{turn_id}/cancel")
    async def cancel_chief_direct_turn(
        turn_id: str,
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("chat:write", current)
        turn = store.find_chief_interactive_turn(turn_id)
        if turn is None:
            raise HTTPException(status_code=404, detail="unknown direct Chief turn")
        if turn.get("operator_username") != _operator_username(user):
            raise HTTPException(status_code=403, detail="not your turn")
        thread = _thread_or_404(str(turn["thread_id"]))
        result = direct_turn_control(
            store,
            thread=thread,
            operator_username=_operator_username(user),
            command="/cancel",
        )
        return result or {"status": str(turn.get("status") or "unknown")}

    @app.post("/api/chat/turns/{turn_id}/confirm")
    async def confirm_chief_direct_turn_permission(
        turn_id: str,
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("admin", current)
        turn = store.find_chief_interactive_turn(turn_id)
        if turn is None:
            raise HTTPException(status_code=404, detail="unknown direct Chief turn")
        if turn.get("operator_username") != _operator_username(user):
            raise HTTPException(status_code=403, detail="not your turn")
        thread = _thread_or_404(str(turn["thread_id"]))
        result = direct_turn_control(
            store,
            thread=thread,
            operator_username=_operator_username(user),
            command="confirm",
        )
        if result is None or result.get("status") != "resumed":
            raise HTTPException(status_code=409, detail="turn is not awaiting permission")
        return result

    @app.get("/api/settings/effective")
    async def settings_effective(current: AuthResult = auth_dependency) -> dict[str, object]:
        require("status:read", current)
        return {
            **build_settings_payload(
                settings, runtime_overrides=get_runtime_overrides(store)
            ),
            "api_version": __version__,
        }

    @app.get("/api/research/status")
    async def research_component_status(current: AuthResult = auth_dependency) -> dict[str, object]:
        require("status:read", current)
        return research_status(settings.data_dir)

    @app.post("/api/research/test/{component}")
    async def test_research_component_route(
        component: str,
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("task:write", current)
        try:
            result = test_research_component(settings.data_dir, component)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        correlation_id = f"research-check:{uuid4()}"
        alert = reconcile_research_alert(
            settings.data_dir,
            rule=f"{component}_health",
            failed=not bool(result["ok"]),
            message=(
                f"research {component} health check failed: "
                f"{result.get('reason') or 'unknown'}"
            ),
            correlation_id=correlation_id,
        )
        if alert.get("new"):
            store.add_alert(str(alert["message"]))
        store.add_provenance_record(
            {
                "record_id": correlation_id,
                "node_id": component,
                "node_type": "research_control",
                "created_at": utc_now_iso(),
                "principal": user.username if user else "web",
                "tool": "health_check",
                "policy_outcome": "healthy" if result["ok"] else "degraded",
                "failure_reason": result.get("reason"),
                "alert_id": alert.get("alert_id"),
            }
        )
        _record_operator_event(
            store,
            action="research_component_test",
            summary=f"tested research {component}: {'ok' if result['ok'] else 'failed'}",
            assignment_id=f"research:{component}",
            by=user.username if user else "web",
            payload={"component": component, "ok": result["ok"], "correlation_id": correlation_id},
        )
        return {**result, "alert": alert, "correlation_id": correlation_id}

    @app.get("/api/research/policy")
    async def get_research_policy(current: AuthResult = auth_dependency) -> dict[str, object]:
        require("status:read", current)
        return research_policy(settings.data_dir)

    @app.get("/api/research/audit")
    async def research_audit_route(
        limit: int = 100,
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        require("task:write", current)
        return research_audit(store, limit=limit)

    @app.post("/api/research/alerts/{alert_id}/acknowledge")
    async def acknowledge_research_alert_route(
        alert_id: str,
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("task:write", current)
        try:
            alert = acknowledge_research_alert(
                settings.data_dir, alert_id, actor=user.username if user else "web"
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _record_operator_event(
            store,
            action="research_alert_acknowledged",
            summary="acknowledged research alert",
            assignment_id=f"research-alert:{alert_id}",
            by=user.username if user else "web",
            payload={"rule": alert.get("rule")},
        )
        return alert

    @app.post("/api/research/policy/proposals")
    async def stage_research_policy_route(
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("task:write", current)
        try:
            proposal = stage_research_policy(
                settings.data_dir,
                dict((payload or {}).get("values") or {}),
                actor=user.username if user else "web",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _record_operator_event(
            store,
            action="research_policy_staged",
            summary="staged research policy change",
            assignment_id=f"research-policy:{proposal['proposal_id']}",
            by=user.username if user else "web",
            payload={"proposed": proposal.get("proposed")},
        )
        return {"proposal": proposal, "policy": research_policy(settings.data_dir)}

    @app.post("/api/research/policy/proposals/{proposal_id}/apply")
    async def apply_research_policy_route(
        proposal_id: str,
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("task:write", current)
        try:
            result = apply_research_policy(
                settings.data_dir,
                proposal_id,
                apply_values=lambda values: set_runtime_overrides(
                    store, values, by=user.username if user else "web"
                ),
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _record_operator_event(
            store,
            action="research_policy_applied",
            summary="applied staged research policy",
            assignment_id=f"research-policy:{proposal_id}",
            by=user.username if user else "web",
            payload={"active": result["active"], "rollback_point": result["rollback_point"]},
        )
        return {**result, "policy": research_policy(settings.data_dir)}

    @app.post("/api/research/policy/rollback")
    async def rollback_research_policy_route(
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("task:write", current)
        try:
            result = rollback_research_policy(
                settings.data_dir,
                apply_values=lambda values: set_runtime_overrides(
                    store, values, by=user.username if user else "web"
                ),
                actor=user.username if user else "web",
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _record_operator_event(
            store,
            action="research_policy_rolled_back",
            summary="rolled back research policy",
            assignment_id="research-policy:rollback",
            by=user.username if user else "web",
            payload={"active": result["active"], "rollback_point": result["rollback_point"]},
        )
        return {**result, "policy": research_policy(settings.data_dir)}

    @app.put("/api/settings/runtime")
    async def update_runtime_settings(
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        user = require("task:write", current)
        try:
            set_runtime_overrides(
                store,
                payload or {},
                by=user.username if user else "web",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {
            **build_settings_payload(
                settings, runtime_overrides=get_runtime_overrides(store)
            ),
            "api_version": __version__,
        }

    @app.get("/api/users")
    async def users(current: AuthResult = auth_dependency) -> list[dict[str, object]]:
        require("user:read", current)
        return [user.to_dict() for user in store.users()]

    @app.post("/api/users")
    async def create_user(
        payload: dict[str, Any],
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        require("user:write", current)
        user = User(
            username=str(payload["username"]),
            role=Role(str(payload.get("role") or "observer")),
        )
        store.add_user(user)
        return user.to_dict()

    register_knowledge_routes(
        app,
        store=store,
        settings=settings,
        require=require,
        auth_dependency=auth_dependency,
    )

    static_root = _static_root()
    if static_root.exists():
        app.mount("/assets", StaticFiles(directory=static_root / "assets"), name="assets")

    @app.get("/", response_class=HTMLResponse)
    @app.get("/chat", response_class=HTMLResponse)
    async def index() -> str:
        index_path = static_root / "index.html"
        if index_path.exists():
            return index_path.read_text(encoding="utf-8")
        return _fallback_html()

    @app.get("/mobile", response_class=HTMLResponse)
    @app.get("/mobile.html", response_class=HTMLResponse)
    async def mobile() -> str:
        mobile_path = static_root / "mobile.html"
        if mobile_path.exists():
            return mobile_path.read_text(encoding="utf-8")
        return _fallback_html()

    return app


def run_web(settings: Settings, *, host: str, port: int) -> None:
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "install the web extra to run the gateway: pip install -e '.[web]'"
        ) from exc
    if not settings.require_auth and host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            "WARNING: brigade web is binding to a reachable host with authentication disabled",
            file=sys.stderr,
        )
    uvicorn.run(create_app(settings), host=host, port=port)


def _static_root(candidates: list[Path] | None = None) -> Path:
    candidates = candidates or [
        Path(__file__).resolve().parent.parent / "web" / "dist",
        Path.cwd() / "web" / "dist",
        Path("/app/web/dist"),
    ]
    for candidate in candidates:
        if (candidate / "index.html").exists():
            return candidate
    return candidates[0]


def _provider_from_payload(payload: dict[str, Any], settings: Settings):
    provider = str(payload.get("provider") or settings.default_provider)
    model = str(payload.get("model") or settings.default_model)
    base_url = payload.get("base_url")
    return provider_from_settings(
        settings,
        provider=provider,
        model=model,
        api_key=payload.get("api_key"),
        api_base=str(base_url) if base_url else None,
    )


def _thread_provider(
    payload: dict[str, Any],
    settings: Settings,
    store: StateStore,
    conversation,
    *,
    fallback_provider: str,
    fallback_model: str,
):
    requested_provider = str(payload.get("provider") or "").strip()
    requested_model = str(payload.get("model") or "").strip()
    if requested_provider and requested_model:
        persist_model_route(
            store,
            conversation,
            provider=requested_provider,
            model=requested_model,
            base_url=str(payload["base_url"]) if payload.get("base_url") else None,
        )
    provider_name, model_name, base_url = effective_model_route(
        conversation,
        fallback_provider=fallback_provider,
        fallback_model=fallback_model,
    )
    routed_payload = {
        **payload,
        "provider": provider_name,
        "model": model_name,
    }
    if base_url:
        routed_payload["base_url"] = base_url
    else:
        routed_payload.pop("base_url", None)
    return _provider_from_payload(routed_payload, settings)


def _require_live_connector_store(settings: Settings) -> None:
    try:
        from fastapi import HTTPException
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("fastapi is required for live connector routes") from exc
    if not settings.postgres_dsn:
        raise HTTPException(status_code=503, detail="live connectors require Postgres")
    if not settings.redis_url:
        raise HTTPException(status_code=503, detail="live connectors require Redis")


def _connector_rate_limiter(settings: Settings) -> RedisConnectorRateLimiter:
    return RedisConnectorRateLimiter(
        RedisRuntimeClient(settings.redis_url),
        limit=settings.connector_rate_limit_count,
        window_seconds=settings.connector_rate_limit_window_seconds,
    )


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _agent_with_team_id(agent: Agent, team_id: str | None) -> Agent:
    return Agent(
        agent_id=agent.agent_id,
        display_name=agent.display_name,
        workspace_path=agent.workspace_path,
        role=agent.role,
        team_id=team_id,
        model_provider=agent.model_provider,
        model_name=agent.model_name,
        specialties=agent.specialties,
        created_at=agent.created_at,
    )


def _fallback_html() -> str:
    return """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>OpenBrigade</title>
    <style>
      body { font-family: system-ui, sans-serif; margin: 2rem; max-width: 980px; }
      pre { background: #f4f4f4; padding: 1rem; overflow: auto; }
      button, input { font: inherit; }
    </style>
  </head>
  <body>
    <h1>OpenBrigade Gateway</h1>
    <p>React build not found. API gateway is running.</p>
    <button id="load">Load dashboard JSON</button>
    <pre id="out"></pre>
    <script>
      document.getElementById('load').onclick = async () => {
        const res = await fetch('/api/dashboard');
        document.getElementById('out').textContent = JSON.stringify(await res.json(), null, 2);
      };
    </script>
  </body>
</html>
"""
