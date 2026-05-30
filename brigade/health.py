from __future__ import annotations

import socket
from dataclasses import dataclass
from urllib.parse import urlparse

from brigade.config import Settings


@dataclass(frozen=True)
class HealthCheck:
    name: str
    ok: bool
    detail: str


def check_tcp(name: str, host: str, port: int, timeout: float = 1.0) -> HealthCheck:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return HealthCheck(name=name, ok=True, detail=f"{host}:{port} reachable")
    except OSError as exc:
        return HealthCheck(name=name, ok=False, detail=f"{host}:{port} unreachable: {exc}")


def check_configured_datastores(settings: Settings) -> list[HealthCheck]:
    checks: list[HealthCheck] = []
    for name, uri in (
        ("postgres", settings.postgres_dsn),
        ("redis", settings.redis_url),
        ("qdrant", settings.qdrant_url),
        ("neo4j", settings.neo4j_uri),
    ):
        if not uri:
            checks.append(HealthCheck(name=name, ok=False, detail="not configured"))
            continue
        checks.append(_check_uri(name, uri))
    return checks


def _check_uri(name: str, uri: str) -> HealthCheck:
    parsed = urlparse(uri)
    host = parsed.hostname
    port = parsed.port or _default_port(parsed.scheme)
    if not host or not port:
        return HealthCheck(name=name, ok=False, detail=f"invalid uri: {uri}")
    return check_tcp(name, host, port)


def _default_port(scheme: str) -> int | None:
    return {
        "postgresql": 5432,
        "postgres": 5432,
        "redis": 6379,
        "http": 80,
        "https": 443,
        "bolt": 7687,
    }.get(scheme)
