from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from brigade import __version__
from brigade.auth import AuthResult, issue_token, verify_token
from brigade.config import Settings, load_settings
from brigade.connectors import (
    ConnectorRateLimiter,
    HttpPost,
    RedisConnectorRateLimiter,
    connector_audit_record,
    google_chat_reply_sender,
    parse_allowlist,
    parse_google_chat_event,
    parse_telegram_update,
    process_live_connector_message,
    telegram_reply_sender,
)
from brigade.health import check_configured_datastores
from brigade.markdown import render_markdown_html
from brigade.providers import available_model_options, provider_from_settings
from brigade.rbac import ROLE_PERMISSIONS, can
from brigade.schemas import (
    Assignment,
    ChatMessage,
    Goal,
    Mission,
    Priority,
    Role,
    Team,
    User,
    WorkMode,
)
from brigade.services import (
    OPS_ROOM_ROOMS,
    build_chat_payload,
    build_cockpit_payload,
    build_hierarchy_payload,
    build_ops_room_payload,
    build_orchestration_payload,
    build_settings_payload,
    send_orchestrator_chat,
    send_user_chat,
    set_config_value,
)
from brigade.store import RedisRuntimeClient, StateStore, open_state_store
from brigade.time import utc_now_iso
from brigade.tui import build_dashboard_payload


def create_app(
    settings: Settings | None = None,
    store: StateStore | None = None,
    *,
    connector_rate_limiter: ConnectorRateLimiter | None = None,
    telegram_http_post: HttpPost | None = None,
):
    try:
        from fastapi import Depends, FastAPI, Header, HTTPException
        from fastapi.responses import HTMLResponse, StreamingResponse
        from fastapi.staticfiles import StaticFiles
        from starlette.datastructures import MutableHeaders
    except ImportError as exc:  # pragma: no cover - exercised by CLI smoke without web extra
        raise RuntimeError(
            "install the web extra to run the gateway: pip install -e '.[web]'"
        ) from exc

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

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {"ok": True, "service": "brigade_web"}

    @app.post("/api/connectors/telegram/webhook")
    async def telegram_webhook(
        payload: dict[str, Any],
        x_telegram_secret: str | None = Header(
            default=None,
            alias="X-Telegram-Bot-Api-Secret-Token",
        ),
        content_length: int | None = Header(default=None, alias="Content-Length"),
    ) -> dict[str, object]:
        if not settings.telegram_webhook_enabled:
            return {"ok": False, "status": "disabled", "provider": "telegram"}
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
        result = process_live_connector_message(
            store,
            incoming,
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
        )
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
            started_at=started_at,
            uptime_seconds=int(time.monotonic() - started_monotonic),
        )

    @app.get("/api/orchestration")
    async def orchestration(current: AuthResult = auth_dependency) -> dict[str, object]:
        require("status:read", current)
        return build_orchestration_payload(store)

    @app.get("/api/models")
    async def models(current: AuthResult = auth_dependency) -> dict[str, object]:
        require("status:read", current)
        return available_model_options(settings)

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
            "settings": {**build_settings_payload(settings), "api_version": __version__},
            "models": available_model_options(settings),
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
        team = Team(
            team_id=existing.team_id,
            display_name=str(payload.get("display_name") or existing.display_name),
            description=payload.get("description", existing.description),
            parent_team_id=payload.get("parent_team_id", existing.parent_team_id),
            crew_chief_id=payload.get("crew_chief_id", existing.crew_chief_id),
            members=list(payload.get("members", existing.members)),
            delegation_policy=str(payload.get("delegation_policy") or existing.delegation_policy),
            escalation_team_id=payload.get("escalation_team_id", existing.escalation_team_id),
            created_at=existing.created_at,
        )
        store.upsert_team(team)
        return team.to_dict()

    @app.get("/api/tasks")
    async def tasks(current: AuthResult = auth_dependency) -> list[dict[str, object]]:
        require("task:read", current)
        return [assignment.to_dict() for assignment in store.assignments()]

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
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        require("task:read", current)
        assignment = store.find_assignment(assignment_id)
        if assignment is None:
            raise HTTPException(status_code=404, detail="unknown assignment")
        return assignment.to_dict()

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

    @app.get("/api/settings/effective")
    async def settings_effective(current: AuthResult = auth_dependency) -> dict[str, object]:
        require("status:read", current)
        return {**build_settings_payload(settings), "api_version": __version__}

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

    static_root = _static_root()
    if static_root.exists():
        app.mount("/assets", StaticFiles(directory=static_root / "assets"), name="assets")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        index_path = static_root / "index.html"
        if index_path.exists():
            return index_path.read_text(encoding="utf-8")
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
