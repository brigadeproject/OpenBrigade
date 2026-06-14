"""Training-data export: the accumulated history as self-contained JSONL.

Every line parses as standalone JSON. Cycles carry their ``cycle_outcome``,
assignments their lineage and final outcomes, and transcripts inline their
content, so the export needs nothing from the live stack to be useful.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from brigade.orchestrator import CYCLE_REASONING_RECORD_VERSION
from brigade.store import StateStore
from brigade.time import parse_utc_iso, utc_now_iso

EXPORT_SCHEMA_VERSION = 1

EXPORT_FILES = (
    "cycles.jsonl",
    "assignments.jsonl",
    "transcripts.jsonl",
    "usage.jsonl",
    "episodes.jsonl",
    "proposals.jsonl",
)


def export_training_data(
    store: StateStore,
    *,
    out_dir: Path,
    since: str | None = None,
) -> dict[str, Any]:
    """Write the JSONL bundle plus ``manifest.json`` and return the manifest."""
    since_dt = parse_utc_iso(since) if since else None
    out_dir.mkdir(parents=True, exist_ok=True)

    # The reasoning list also holds event-only mini-records (proposal
    # decisions, sub-system passes); a cycle line is one with a cycle_outcome.
    cycles = [
        record
        for record in store.orchestrator_reasoning()
        if "cycle_outcome" in record and _after(record.get("started_at"), since_dt)
    ]
    assignments = _assignment_rows(store, since_dt)
    transcripts = _transcript_rows(store, since_dt)
    usage = [
        record
        for record in store.usage_records()
        if _after(record.get("recorded_at"), since_dt)
    ]
    episodes = [
        record
        for record in store.episodes()
        if _after(record.get("created_at"), since_dt)
    ]
    proposals = [
        record
        for record in store.proposals()
        if _after(record.get("created_at"), since_dt)
    ]

    rows_by_file: dict[str, list[dict[str, Any]]] = {
        "cycles.jsonl": cycles,
        "assignments.jsonl": assignments,
        "transcripts.jsonl": transcripts,
        "usage.jsonl": usage,
        "episodes.jsonl": episodes,
        "proposals.jsonl": proposals,
    }
    for filename, rows in rows_by_file.items():
        _write_jsonl(out_dir / filename, rows)

    timestamps = [
        stamp
        for rows, keys in (
            (cycles, ("started_at",)),
            (assignments, ("created_at", "updated_at")),
            (transcripts, ("created_at",)),
            (usage, ("recorded_at",)),
            (episodes, ("created_at",)),
            (proposals, ("created_at",)),
        )
        for row in rows
        for key in keys
        if isinstance(row.get(key), str) and row.get(key)
        for stamp in [row[key]]
    ]
    manifest = {
        "export_schema_version": EXPORT_SCHEMA_VERSION,
        "generated_at": utc_now_iso(),
        "since": since,
        "counts": {filename: len(rows) for filename, rows in rows_by_file.items()},
        "time_range": {
            "earliest": min(timestamps) if timestamps else None,
            "latest": max(timestamps) if timestamps else None,
        },
        "schema_versions": {
            "export": EXPORT_SCHEMA_VERSION,
            "cycle_reasoning_record": CYCLE_REASONING_RECORD_VERSION,
        },
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _assignment_rows(
    store: StateStore,
    since: datetime | None,
) -> list[dict[str, Any]]:
    """State/decision/outcome tuples: live assignments plus archived history."""
    rows: list[dict[str, Any]] = []
    for item in store.assignments():
        if not _after(item.updated_at, since):
            continue
        rows.append(
            {
                **item.to_dict(),
                "final_status": item.status.value,
                "executive_summary": item.progress_summary,
                "archived_at": None,
            }
        )
    for item in store.assignment_history():
        archived_at = item.get("archived_at")
        if not _after(archived_at if isinstance(archived_at, str) else None, since):
            continue
        record = dict(item.get("record") or {})
        record.update(
            {
                "final_status": item.get("final_status"),
                "executive_summary": item.get("executive_summary"),
                "failure_info": item.get("failure_info"),
                "archived_at": archived_at,
            }
        )
        rows.append(record)
    return rows


def _transcript_rows(
    store: StateStore,
    since: datetime | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in store.transcripts():
        if not _after(item.get("created_at"), since):
            continue
        row = dict(item)
        row["content"] = _read_transcript(item.get("path"))
        rows.append(row)
    return rows


def _read_transcript(path: Any) -> Any:
    if not isinstance(path, str) or not path:
        return None
    file = Path(path)
    if not file.exists():
        return None
    text = file.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def _after(value: Any, since: datetime | None) -> bool:
    if since is None:
        return True
    if not isinstance(value, str) or not value:
        return False
    try:
        return parse_utc_iso(value) >= since
    except ValueError:
        return False
