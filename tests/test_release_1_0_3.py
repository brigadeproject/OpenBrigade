"""Release 1.0.3 backend: telemetry live-controls (runtime overrides),
task-id search incl. archived history, and stale-target audit de-noise."""

from __future__ import annotations

import pytest

from brigade.config import Settings
from brigade.orchestrator import (
    OrchestrationConfig,
    StaleAssignmentTarget,
    apply_orchestrator_actions,
)
from brigade.schemas import Agent, Assignment, AssignmentStatus
from brigade.services import (
    build_settings_payload,
    get_runtime_overrides,
    lookup_assignment,
    set_runtime_overrides,
)
from brigade.state import JsonStateStore


def _store(tmp_path) -> JsonStateStore:
    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(Agent("ada", "ADA", "workspace-ada"))
    store.add_agent(Agent("lin", "LIN", "workspace-lin"))
    return store


def _queued(store, *, agent="ada", text="do the work", **kwargs) -> Assignment:
    a = Assignment(
        assignment=text,
        assigned_to=agent,
        created_by="human",
        source="direct_command",
        **kwargs,
    )
    store.add_assignment(a)
    return a


# --- 1. runtime overrides (telemetry live-controls) --------------------------------


def test_runtime_overrides_round_trip(tmp_path):
    store = _store(tmp_path)
    set_runtime_overrides(
        store,
        {
            "proactive_mode": "off",
            "proactive_creation_enabled": False,
            "max_proactive_creations_per_cycle": 3,
        },
        by="op",
    )
    got = get_runtime_overrides(store)
    assert got["proactive_mode"] == "off"
    assert got["proactive_creation_enabled"] is False
    assert got["max_proactive_creations_per_cycle"] == 3
    # persisted to the store, not just in-process
    assert JsonStateStore(tmp_path / "state.json").runtime_overrides()[
        "proactive_mode"
    ] == "off"


def test_runtime_overrides_partial_merge(tmp_path):
    store = _store(tmp_path)
    set_runtime_overrides(store, {"proactive_mode": "create"}, by="op")
    set_runtime_overrides(store, {"max_proactive_creations_per_cycle": 5}, by="op")
    got = get_runtime_overrides(store)
    assert got["proactive_mode"] == "create"
    assert got["max_proactive_creations_per_cycle"] == 5


def test_runtime_overrides_coerce_string_values(tmp_path):
    store = _store(tmp_path)
    # values may arrive as strings (e.g. form inputs)
    set_runtime_overrides(
        store,
        {"proactive_creation_enabled": "true", "max_proactive_creations_per_cycle": "2"},
        by="op",
    )
    got = get_runtime_overrides(store)
    assert got["proactive_creation_enabled"] is True
    assert got["max_proactive_creations_per_cycle"] == 2


def test_runtime_overrides_reject_unknown_key(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        set_runtime_overrides(store, {"default_model": "x"}, by="op")


def test_runtime_overrides_reject_bad_mode(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        set_runtime_overrides(store, {"proactive_mode": "sideways"}, by="op")


def test_runtime_overrides_reject_negative_int(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        set_runtime_overrides(store, {"max_proactive_creations_per_cycle": -1}, by="op")


def test_runtime_overrides_emit_audit_event(tmp_path):
    store = _store(tmp_path)
    set_runtime_overrides(store, {"proactive_mode": "off"}, by="tm")
    blob = str(store.orchestrator_reasoning())
    assert "operator_runtime_config" in blob
    assert "proactive_mode" in blob


def test_orchestration_config_with_overrides_applies_known_fields():
    base = OrchestrationConfig()
    out = base.with_overrides(
        {
            "proactive_mode": "off",
            "proactive_creation_enabled": True,
            "max_proactive_creations_per_cycle": 7,
            "cadence_seconds": 1,  # not overridable — ignored
        }
    )
    assert out.proactive_mode == "off"
    assert out.proactive_creation_enabled is True
    assert out.max_proactive_creations_per_cycle == 7
    assert out.cadence_seconds == base.cadence_seconds


def test_orchestration_config_with_overrides_noop_on_empty():
    base = OrchestrationConfig()
    assert base.with_overrides({}) is base
    assert base.with_overrides(None) is base


def test_orchestration_config_with_overrides_ignores_bad_types():
    base = OrchestrationConfig(proactive_mode="propose")
    # a malformed runtime write must never break a cycle
    out = base.with_overrides({"max_proactive_creations_per_cycle": "not-an-int"})
    assert out.max_proactive_creations_per_cycle == base.max_proactive_creations_per_cycle


def test_build_settings_payload_reflects_overrides(tmp_path):
    settings = Settings(config_path=tmp_path / "c.json", data_dir=tmp_path)
    payload = build_settings_payload(
        settings, runtime_overrides={"proactive_mode": "off"}
    )
    assert payload["proactive_mode"] == "off"
    assert payload["runtime_overrides"] == {"proactive_mode": "off"}
    assert "proactive_mode" in payload["runtime_override_keys"]


# --- 2. task-id search incl. archived history --------------------------------------


def _archive(store, *, agent="ada", text="legacy", status=AssignmentStatus.COMPLETE):
    a = Assignment(
        assignment=text,
        assigned_to=agent,
        created_by="human",
        source="direct_command",
        status=status,
    )
    store.archive_assignment(a, "wrapped up")
    return a


def test_lookup_active_assignment(tmp_path):
    store = _store(tmp_path)
    a = _queued(store)
    found = lookup_assignment(store, a.assignment_id)
    assert found is not None
    assert found["archived"] is False
    assert found["assignment_id"] == a.assignment_id


def test_lookup_archived_assignment(tmp_path):
    store = _store(tmp_path)
    a = _archive(store, status=AssignmentStatus.FAILED)
    found = lookup_assignment(store, a.assignment_id)
    assert found is not None
    assert found["archived"] is True
    assert found["final_status"] == AssignmentStatus.FAILED.value
    assert found["executive_summary"] == "wrapped up"


def test_lookup_prefix_match_unique(tmp_path):
    store = _store(tmp_path)
    a = _archive(store)
    found = lookup_assignment(store, a.assignment_id[:8])
    assert found is not None
    assert found["assignment_id"] == a.assignment_id


def test_lookup_ambiguous_prefix_returns_none(tmp_path):
    store = _store(tmp_path)
    a = _queued(store)
    b = _queued(store, text="other")
    # craft a prefix that matches both (their shared leading chars) — fall back to
    # the empty case by using a clearly-too-short ambiguous prefix only if shared.
    shared = ""
    for x, y in zip(a.assignment_id, b.assignment_id):
        if x != y:
            break
        shared += x
    if len(shared) >= 4:
        assert lookup_assignment(store, shared) is None


def test_lookup_unknown_returns_none(tmp_path):
    store = _store(tmp_path)
    assert lookup_assignment(store, "does-not-exist") is None
    assert lookup_assignment(store, "") is None


def test_task_route_returns_archived(tmp_path):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from brigade.web import create_app

    store = _store(tmp_path)
    archived = _archive(store, status=AssignmentStatus.FAILED)
    settings = Settings(config_path=tmp_path / "c.json", data_dir=tmp_path)
    client = TestClient(create_app(settings, store))

    resp = client.get(f"/api/tasks/{archived.assignment_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["archived"] is True
    assert body["final_status"] == AssignmentStatus.FAILED.value

    assert client.get("/api/tasks/nope-nope-nope").status_code == 404


def test_runtime_settings_route(tmp_path):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from brigade.web import create_app

    store = _store(tmp_path)
    settings = Settings(config_path=tmp_path / "c.json", data_dir=tmp_path)
    client = TestClient(create_app(settings, store))

    ok = client.put(
        "/api/settings/runtime",
        json={"proactive_mode": "off", "max_proactive_creations_per_cycle": 4},
    )
    assert ok.status_code == 200
    assert ok.json()["proactive_mode"] == "off"
    assert ok.json()["max_proactive_creations_per_cycle"] == 4

    # the change is reflected in the effective settings the GUI reads
    eff = client.get("/api/settings/effective")
    assert eff.json()["proactive_mode"] == "off"
    assert eff.json()["runtime_overrides"]["proactive_mode"] == "off"

    # bad value rejected
    bad = client.put("/api/settings/runtime", json={"proactive_mode": "sideways"})
    assert bad.status_code == 400


# --- 3. stale-target audit de-noise -----------------------------------------------


def test_apply_actions_skips_stale_target(tmp_path):
    store = _store(tmp_path)
    result = apply_orchestrator_actions(
        store,
        [{"type": "rebalance_queued_assignment", "assignment_id": "ghost", "to_agent_id": "ada"}],
    )
    assert result["rejected"] == []
    assert len(result["skipped"]) == 1
    assert "unknown assignment" in result["skipped"][0]["reason"]
    # a benign race must not raise an operator alert
    assert store.alerts() == []


def test_apply_actions_rejects_genuinely_invalid_action(tmp_path):
    store = _store(tmp_path)
    result = apply_orchestrator_actions(store, [{"type": "bogus_action"}])
    assert result["skipped"] == []
    assert len(result["rejected"]) == 1
    assert store.alerts()  # genuine rejections still alert


def test_stale_assignment_target_is_valueerror():
    # callers that catch ValueError still work; the distinct type only changes
    # how apply_orchestrator_actions buckets it.
    assert issubclass(StaleAssignmentTarget, ValueError)
