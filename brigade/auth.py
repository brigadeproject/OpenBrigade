from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from brigade.config import Settings
from brigade.schemas import Role, User

DEFAULT_JWT_AUDIENCE = "openbrigade"
DEFAULT_JWT_ISSUER = "openbrigade-local"
DEFAULT_JWT_SECRET = "openbrigade-dev-secret"


@dataclass(frozen=True)
class AuthResult:
    ok: bool
    method: str
    user: User | None = None
    claims: dict[str, Any] | None = None
    reason: str | None = None


def build_user_identity_context(user: User) -> str:
    safe_username = _sanitize_identity_value(user.username)
    return (
        "Current user: "
        f"username={safe_username}, "
        f"role={user.role.value}, "
        f"user_id={safe_username}"
    )


def issue_token(
    settings: Settings,
    user: User,
    ttl_seconds: int = 3600,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": user.username,
        "username": user.username,
        "role": user.role.value,
        "iat": now,
        "exp": now + ttl_seconds,
        "jti": str(uuid4()),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    if extra_claims:
        payload.update(extra_claims)
    header = {"alg": "HS256", "typ": "JWT"}
    encoded_header = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    encoded_payload = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    signature = hmac.new(
        settings.jwt_secret.encode("utf-8"),
        signing_input,
        hashlib.sha256,
    ).digest()
    return f"{encoded_header}.{encoded_payload}.{_b64url_encode(signature)}"


def verify_token(settings: Settings, token: str) -> AuthResult:
    try:
        encoded_header, encoded_payload, encoded_signature = token.split(".")
    except ValueError:
        return AuthResult(ok=False, method="jwt", reason="token must contain three segments")

    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    expected_signature = hmac.new(
        settings.jwt_secret.encode("utf-8"),
        signing_input,
        hashlib.sha256,
    ).digest()
    try:
        provided_signature = _b64url_decode(encoded_signature)
    except ValueError as exc:
        return AuthResult(ok=False, method="jwt", reason=str(exc))
    if not hmac.compare_digest(expected_signature, provided_signature):
        return AuthResult(ok=False, method="jwt", reason="invalid signature")

    try:
        header = json.loads(_b64url_decode(encoded_header).decode("utf-8"))
        claims = json.loads(_b64url_decode(encoded_payload).decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return AuthResult(ok=False, method="jwt", reason=f"invalid token payload: {exc}")

    if header.get("alg") != "HS256":
        return AuthResult(ok=False, method="jwt", reason="unsupported jwt algorithm")
    if claims.get("iss") != settings.jwt_issuer:
        return AuthResult(ok=False, method="jwt", reason="unexpected issuer")
    if claims.get("aud") != settings.jwt_audience:
        return AuthResult(ok=False, method="jwt", reason="unexpected audience")
    now = int(time.time())
    if int(claims.get("exp", 0) or 0) < now:
        return AuthResult(ok=False, method="jwt", reason="token expired")
    username = str(claims.get("username") or claims.get("sub") or "").strip()
    role_name = str(claims.get("role") or "").strip()
    if not username or role_name not in {item.value for item in Role}:
        return AuthResult(ok=False, method="jwt", reason="missing username or role claim")

    user = User(username=username, role=Role(role_name))
    return AuthResult(ok=True, method="jwt", user=user, claims=claims)


def _sanitize_identity_value(value: str) -> str:
    return " ".join(value.strip().split())[:120]


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode((value + padding).encode("ascii"))
    except Exception as exc:  # pragma: no cover - defensive decode wrapper
        raise ValueError(f"invalid base64url segment: {exc}") from exc
