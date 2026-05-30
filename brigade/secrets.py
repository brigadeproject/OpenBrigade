from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from brigade.config import Settings
from brigade.time import add_seconds_iso, utc_now_iso

MODEL_AUTH_PROVIDERS = {"openai", "openai-codex", "gemini"}


def write_oauth_credential(
    settings: Settings,
    *,
    provider: str,
    access_token: str | None = None,
    refresh_token: str | None = None,
    expires_at: str | None = None,
    expires_in: int | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    scope: str | None = None,
    account: str | None = None,
    token_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    provider = _normalize_provider(provider)
    payload = dict(token_payload or {})
    access_token = access_token or payload.get("access_token")
    refresh_token = refresh_token or payload.get("refresh_token")
    client_id = client_id or payload.get("client_id")
    client_secret = client_secret or payload.get("client_secret")
    scope = scope or payload.get("scope")
    account = account or payload.get("account")
    expires_at = expires_at or payload.get("expires_at")
    if expires_at is None and expires_in is None and payload.get("expires_in") is not None:
        expires_in = int(payload["expires_in"])
    if expires_at is None and expires_in is not None:
        expires_at = add_seconds_iso(utc_now_iso(), expires_in)
    if not access_token and not refresh_token:
        raise ValueError("OAuth login requires an access token or refresh token")

    now = utc_now_iso()
    record = {
        "provider": provider,
        "method": "oauth",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": payload.get("token_type", "Bearer"),
        "expires_at": expires_at,
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": scope,
        "account": account,
        "created_at": payload.get("created_at", now),
        "updated_at": now,
    }
    path = oauth_credential_path(settings, provider)
    _ensure_secret_dir(path.parent)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _chmod_secret(path)
    return oauth_credential_status(settings, provider)


def read_oauth_credential(settings: Settings, provider: str) -> dict[str, Any] | None:
    path = oauth_credential_path(settings, _normalize_provider(provider))
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def delete_oauth_credential(settings: Settings, provider: str) -> bool:
    path = oauth_credential_path(settings, _normalize_provider(provider))
    if not path.exists():
        return False
    path.unlink()
    return True


def oauth_credential_status(settings: Settings, provider: str) -> dict[str, Any]:
    provider = _normalize_provider(provider)
    credential = read_oauth_credential(settings, provider)
    if credential is None:
        return {
            "provider": provider,
            "configured": False,
            "method": "oauth",
            "path": str(oauth_credential_path(settings, provider)),
        }
    return {
        "provider": provider,
        "configured": True,
        "method": "oauth",
        "path": str(oauth_credential_path(settings, provider)),
        "account": credential.get("account"),
        "client_id": _redacted(credential.get("client_id")),
        "client_secret": _redacted(credential.get("client_secret")),
        "access_token": _redacted(credential.get("access_token")),
        "refresh_token": _redacted(credential.get("refresh_token")),
        "scope": credential.get("scope"),
        "expires_at": credential.get("expires_at"),
        "expired": oauth_credential_expired(credential),
        "updated_at": credential.get("updated_at"),
    }


def oauth_credential_expired(credential: dict[str, Any]) -> bool:
    expires_at = credential.get("expires_at")
    if not expires_at:
        return False
    return str(expires_at) <= utc_now_iso()


def oauth_credential_path(settings: Settings, provider: str) -> Path:
    root = settings.secret_store_path or (settings.data_dir / "secrets")
    return root / "model-auth" / f"{_normalize_provider(provider)}.oauth.json"


def _normalize_provider(provider: str) -> str:
    normalized = provider.strip().lower()
    if normalized not in MODEL_AUTH_PROVIDERS:
        raise ValueError(f"unsupported model auth provider: {provider}")
    return normalized


def _ensure_secret_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _chmod_secret(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _redacted(value: str | None) -> str | None:
    if not value:
        return None
    return "***redacted***"
