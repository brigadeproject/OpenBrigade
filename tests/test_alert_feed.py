"""Alert hygiene: timestamps, de-duplication, and TTL aging in the GUI feed."""

from __future__ import annotations

import json
from datetime import timedelta

from brigade.services import build_alert_feed
from brigade.state import JsonStateStore
from brigade.time import utc_now


def _store(tmp_path) -> JsonStateStore:
    return JsonStateStore(tmp_path / "state.json")


def test_add_alert_records_timestamp(tmp_path):
    store = _store(tmp_path)
    store.add_alert("codex credential failed")
    records = store.alert_records()
    assert records[0]["message"] == "codex credential failed"
    assert records[0]["created_at"]
    # string API stays intact for prompts/TUI
    assert store.alerts() == ["codex credential failed"]


def test_legacy_string_alerts_still_read(tmp_path):
    store = _store(tmp_path)
    state = store.load()
    state["alerts"] = ["old style alert"]
    store.save(state)
    assert store.alerts() == ["old style alert"]
    assert store.alert_records()[0] == {"message": "old style alert", "created_at": None}
    # undated alerts are kept by the feed rather than aged out
    feed = build_alert_feed(store)
    assert feed[0]["message"] == "old style alert"


def test_feed_dedupes_repeats_with_counts(tmp_path):
    store = _store(tmp_path)
    for _ in range(3):
        store.add_alert("dispatch starvation")
    store.add_alert("something else")
    feed = build_alert_feed(store)
    by_message = {item["message"]: item for item in feed}
    assert by_message["dispatch starvation"]["count"] == 3
    assert by_message["something else"]["count"] == 1
    assert by_message["dispatch starvation"]["last_seen"] >= by_message[
        "dispatch starvation"
    ]["first_seen"]


def test_feed_normalizes_datetime_created_at(tmp_path, monkeypatch):
    # The Postgres store returns datetime objects for created_at where the
    # JSON store has ISO strings; the feed must stay json.dumps-safe because
    # the ops-room SSE stream serializes it without FastAPI's encoder.
    store = _store(tmp_path)
    store.add_alert("mixed timestamp types")
    records = [
        {"message": "mixed timestamp types", "created_at": utc_now()},
        {"message": "mixed timestamp types", "created_at": utc_now().isoformat()},
    ]
    monkeypatch.setattr(store, "alert_records", lambda: records)
    feed = build_alert_feed(store)
    assert feed[0]["count"] == 2
    json.dumps(feed)  # raises TypeError if a datetime leaks through


def test_feed_ages_out_stale_alerts(tmp_path):
    store = _store(tmp_path)
    stale = (utc_now() - timedelta(hours=72)).isoformat()
    fresh = utc_now().isoformat()
    state = store.load()
    state["alerts"] = [
        {"message": "ancient 401 wall", "created_at": stale},
        {"message": "current issue", "created_at": fresh},
    ]
    store.save(state)
    feed = build_alert_feed(store, ttl_hours=48)
    assert [item["message"] for item in feed] == ["current issue"]
    # a repeat within the TTL keeps the whole group alive
    state["alerts"].append({"message": "ancient 401 wall", "created_at": fresh})
    store.save(state)
    feed = build_alert_feed(store, ttl_hours=48)
    assert {item["message"] for item in feed} == {"ancient 401 wall", "current issue"}
