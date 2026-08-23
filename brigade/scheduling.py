"""Small, dependency-free UTC cron scheduling helpers.

Schedules deliberately use five-field cron expressions (minute, hour, day of
month, month, day of week).  The orchestrator is the clock: there is no
in-memory timer to lose on a restart.  A missed slot is skipped when the next
orchestrator cycle advances the durable ``next_due_at`` value.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

_MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}
_FIELD_LIMITS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def normalize_cron(expression: str) -> str:
    """Validate a five-field UTC cron expression and return its canonical form."""
    value = " ".join(str(expression or "").strip().lower().split())
    value = _MACROS.get(value, value)
    fields = value.split(" ")
    if len(fields) != 5:
        raise ValueError("cron must have five fields: minute hour day month weekday (UTC)")
    for field, (minimum, maximum), name in zip(
        fields, _FIELD_LIMITS, ("minute", "hour", "day", "month", "weekday"), strict=True
    ):
        _parse_field(field, minimum, maximum, name)
    return value


def next_cron_due(expression: str, after: datetime) -> datetime:
    """Return the first minute strictly after ``after`` matching the expression."""
    fields = normalize_cron(expression).split(" ")
    parsed = [
        _parse_field(field, minimum, maximum, name)
        for field, (minimum, maximum), name in zip(
            fields, _FIELD_LIMITS, ("minute", "hour", "day", "month", "weekday"), strict=True
        )
    ]
    candidate = after.astimezone(timezone.utc).replace(second=0, microsecond=0)
    candidate += timedelta(minutes=1)
    # A two-year cap makes malformed-but-valid impossible schedules observable
    # rather than turning one orchestrator cycle into an unbounded loop.
    for _ in range(1_051_201):
        if _matches(candidate, parsed, fields):
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError("cron has no matching UTC time within the next two years")


def _parse_field(field: str, minimum: int, maximum: int, name: str) -> set[int]:
    if not field:
        raise ValueError(f"cron {name} field is empty")
    values: set[int] = set()
    for segment in field.split(","):
        if not segment:
            raise ValueError(f"cron {name} field contains an empty segment")
        base, separator, step_raw = segment.partition("/")
        step = 1
        if separator:
            try:
                step = int(step_raw)
            except ValueError as exc:
                raise ValueError(f"cron {name} step must be an integer") from exc
            if step <= 0:
                raise ValueError(f"cron {name} step must be positive")
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            pieces = base.split("-", 1)
            start, end = (_field_number(part, minimum, maximum, name) for part in pieces)
            if start > end:
                raise ValueError(f"cron {name} range is descending")
        else:
            start = end = _field_number(base, minimum, maximum, name)
        values.update(range(start, end + 1, step))
    return values


def _field_number(value: str, minimum: int, maximum: int, name: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise ValueError(f"cron {name} values must be numeric") from exc
    if number < minimum or number > maximum:
        raise ValueError(f"cron {name} must be between {minimum} and {maximum}")
    # Both 0 and 7 mean Sunday, as in traditional cron.
    return 0 if name == "weekday" and number == 7 else number


def _matches(candidate: datetime, fields: list[set[int]], raw_fields: list[str]) -> bool:
    minute, hour, day, month, weekday = fields
    if candidate.minute not in minute or candidate.hour not in hour or candidate.month not in month:
        return False
    dom_match = candidate.day in day
    cron_weekday = (candidate.weekday() + 1) % 7
    dow_match = cron_weekday in weekday
    dom_wildcard = raw_fields[2] == "*"
    dow_wildcard = raw_fields[4] == "*"
    # Vixie cron's useful compatibility rule: when both DOM and DOW are
    # restricted, either field may match.  When only one is restricted it must.
    if not dom_wildcard and not dow_wildcard:
        return dom_match or dow_match
    return dom_match and dow_match
