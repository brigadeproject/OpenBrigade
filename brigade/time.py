from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def parse_utc_iso(value: str) -> datetime:
    # Python 3.10 fromisoformat rejects the Z suffix; models commonly emit it.
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def add_seconds_iso(value: str | None, seconds: int) -> str:
    base = parse_utc_iso(value) if value else utc_now()
    return (base + timedelta(seconds=seconds)).isoformat()
