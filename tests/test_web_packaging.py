from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_web_dockerfiles_include_public_assets_before_build() -> None:
    for dockerfile in (ROOT / "Dockerfile", ROOT / "web" / "Dockerfile"):
        text = dockerfile.read_text(encoding="utf-8")
        public_copy = "COPY web/public ./public"
        assert public_copy in text
        assert text.index(public_copy) < text.index("RUN npm run build")


def test_v092_frontend_wires_cockpit_auth_and_ops_room_workflows() -> None:
    source = (ROOT / "web" / "src" / "main.tsx").read_text(encoding="utf-8")

    for expected in (
        "/api/cockpit",
        "/api/auth/me",
        "permissions",
        "Cockpit",
        "Ops Room",
        "TaskDialog",
        "OrchestratorChat",
        "ModelSelect",
        "/api/models",
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
        "cockpit-desktop.png",
        "ops-desktop.png",
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
        "cockpit-mobile.png",
    ):
        assert expected in script
