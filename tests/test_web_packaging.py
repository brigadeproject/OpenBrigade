from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_web_dockerfiles_include_public_assets_before_build() -> None:
    for dockerfile in (ROOT / "Dockerfile", ROOT / "web" / "Dockerfile"):
        text = dockerfile.read_text(encoding="utf-8")
        public_copy = "COPY web/public ./public"
        assert public_copy in text
        assert text.index(public_copy) < text.index("RUN npm run build")


def test_compose_forwards_connector_settings_to_the_web_service() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    web_service = compose[compose.index("  brigade_web:") :]
    orchestrator_service = compose[
        compose.index("  brigade_orchestrator:") : compose.index("  brigade_web:")
    ]

    for setting in (
        "BRIGADE_TELEGRAM_BOT_TOKEN",
        "BRIGADE_TELEGRAM_WEBHOOK_ENABLED",
        "BRIGADE_TELEGRAM_WEBHOOK_SECRET",
        "BRIGADE_TELEGRAM_POLLING_ENABLED",
        "BRIGADE_TELEGRAM_DEFAULT_AGENT",
        "BRIGADE_TELEGRAM_ALLOWLIST",
        "BRIGADE_CONNECTOR_RATE_LIMIT_COUNT",
        "BRIGADE_CONNECTOR_MAX_BODY_BYTES",
        "BRIGADE_CONNECTOR_CHIEF_CHAT_ENABLED",
        "BRIGADE_CONNECTOR_EXECUTIVE_CHAT_ENABLED",
    ):
        assert setting in web_service

    for setting in (
        "BRIGADE_TELEGRAM_BOT_TOKEN",
        "BRIGADE_TELEGRAM_POLLING_ENABLED",
        "BRIGADE_TELEGRAM_POLLING_TIMEOUT_SECONDS",
        "BRIGADE_OPERATOR_TELEGRAM_CHAT_ID",
    ):
        assert setting in orchestrator_service


def test_dedicated_compose_runs_only_support_services_with_srv_persistence() -> None:
    compose = (ROOT / "compose.dedicated.yml").read_text(encoding="utf-8")
    assert "brigade_web:" not in compose
    assert "brigade_orchestrator:" not in compose
    for service in (
        "brigade_postgres:",
        "brigade_redis:",
        "brigade_qdrant:",
        "brigade_neo4j:",
        "brigade_searxng:",
        "brigade_browser:",
    ):
        assert service in compose
    assert "/srv/openbrigade/services" in compose


def test_dedicated_installer_copies_regular_systemd_units() -> None:
    installer = (ROOT / "deploy" / "install-dedicated-system.sh").read_text(encoding="utf-8")
    assert "install -m 0644" in installer
    assert "ln -s" not in installer
    assert "systemctl daemon-reload" in installer


def test_v092_frontend_wires_cockpit_auth_and_ops_room_workflows() -> None:
    source = (ROOT / "web" / "src" / "main.tsx").read_text(encoding="utf-8")

    for expected in (
        "/api/cockpit",
        "/api/auth/me",
        "permissions",
        "Cockpit",
        "Ops Room",
        "Proposals",
        "Approval Workbench",
        "TaskDialog",
        "OrchestratorChat",
        "ActivityLogPanel",
        "ModelSelect",
        "/api/models",
        "/api/proposals",
        "/api/connectors/approvals",
        "/api/chat/ask-orchestrator",
        "SettingsStatus",
        "unsafe_bind_without_auth",
        "Token expired",
        "Token format unreadable",
        "readJwtMetadata",
        "PermissionNotice",
        'permission="task:write"',
        "Read-only role",
    ):
        assert expected in source


def test_v092_browser_smoke_script_captures_main_views() -> None:
    script = (ROOT / "ops" / "web-browser-smoke.sh").read_text(encoding="utf-8")

    for expected in (
        "/api/cockpit",
        "/api/models",
        "?view=cockpit",
        "?view=ops",
        "?view=proposals",
        "cockpit-desktop.png",
        "ops-desktop.png",
        "proposals-desktop.png",
        "cockpit-mobile.png",
        "identify -format",
        "web-browser-smoke-cdp.mjs",
        "BRIGADE_TOKEN",
    ):
        assert expected in script


def test_v092_authenticated_browser_smoke_uses_devtools_token_seed() -> None:
    script = (ROOT / "ops" / "web-browser-smoke-cdp.mjs").read_text(encoding="utf-8")

    for expected in (
        "remote-debugging-port",
        "localStorage.setItem",
        "brigade_token",
        "Page.captureScreenshot",
        "cockpit-desktop.png",
        "ops-desktop.png",
        "proposals-desktop.png",
        "cockpit-mobile.png",
    ):
        assert expected in script
