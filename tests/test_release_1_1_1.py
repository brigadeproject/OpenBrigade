"""Release 1.1.1: telemetry tab cleanup — embedding-surface health, a
10-iteration default tool budget, and orchestrator tunables (cadence,
stale-work, max tool iterations) editable as live runtime overrides."""

from __future__ import annotations

import pytest

from brigade.config import Settings
from brigade.health import check_embedding_surface
from brigade.orchestrator import OrchestrationConfig
from brigade.runner import MAX_AGENT_ITERATIONS, _iteration_budget
from brigade.schemas import Agent
from brigade.services import (
    build_cockpit_payload,
    build_settings_payload,
    get_runtime_overrides,
    set_runtime_overrides,
)
from brigade.state import JsonStateStore


def _store(tmp_path) -> JsonStateStore:
    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(Agent("ada", "ADA", "workspace-ada"))
    return store


def _settings(tmp_path, **kwargs) -> Settings:
    return Settings(config_path=tmp_path / "config.json", data_dir=tmp_path, **kwargs)


# --- embedding surface -------------------------------------------------------------


def test_embedding_check_unconfigured(tmp_path):
    check = check_embedding_surface(_settings(tmp_path))
    assert check.name == "embedding"
    assert not check.ok
    assert check.detail == "not configured"


def test_cockpit_payload_carries_embedding_surface(tmp_path):
    store = _store(tmp_path)
    settings = _settings(
        tmp_path,
        ollama_embedding_base_url="http://embed-host:11435",
        ollama_embedding_model="nomic-embed-text:latest",
    )
    payload = build_cockpit_payload(
        store,
        settings,
        datastore_checks=[],
        embedding_check=check_embedding_surface(settings),
        started_at="2026-07-19T00:00:00Z",
        uptime_seconds=1,
    )
    embedding = payload["embedding"]
    assert embedding["provider"] == "ollama"
    assert embedding["base_url"] == "http://embed-host:11435"
    assert embedding["model"] == "nomic-embed-text:latest"
    assert embedding["ok"] in (True, False)  # reachability depends on env
    assert embedding["detail"]


def test_cockpit_payload_embedding_defaults_when_unchecked(tmp_path):
    payload = build_cockpit_payload(
        _store(tmp_path),
        _settings(tmp_path),
        datastore_checks=[],
        started_at="2026-07-19T00:00:00Z",
        uptime_seconds=1,
    )
    assert payload["embedding"]["provider"] is None
    assert payload["embedding"]["ok"] is None


# --- tool iteration budget ---------------------------------------------------------


def test_default_iteration_budget_is_ten(tmp_path):
    assert MAX_AGENT_ITERATIONS == 10
    assert _iteration_budget(_store(tmp_path)) == 10


def test_iteration_budget_honors_runtime_override(tmp_path):
    store = _store(tmp_path)
    set_runtime_overrides(store, {"max_agent_iterations": 4}, by="op")
    assert _iteration_budget(store) == 4


def test_iteration_budget_ignores_malformed_override(tmp_path):
    store = _store(tmp_path)
    store.set_runtime_overrides({"max_agent_iterations": "not-a-number"})
    assert _iteration_budget(store) == MAX_AGENT_ITERATIONS


# --- runtime-editable orchestrator tunables ----------------------------------------


def test_new_runtime_override_keys_round_trip(tmp_path):
    store = _store(tmp_path)
    set_runtime_overrides(
        store,
        {
            "orchestrator_cadence_seconds": "120",
            "stale_work_seconds": 3600,
            "max_agent_iterations": "8",
        },
        by="op",
    )
    got = get_runtime_overrides(store)
    assert got["orchestrator_cadence_seconds"] == 120
    assert got["stale_work_seconds"] == 3600
    assert got["max_agent_iterations"] == 8


@pytest.mark.parametrize(
    ("key", "too_low"),
    [
        ("orchestrator_cadence_seconds", 10),
        ("stale_work_seconds", 60),
        ("max_agent_iterations", 0),
    ],
)
def test_runtime_override_minimums_enforced(tmp_path, key, too_low):
    with pytest.raises(ValueError):
        set_runtime_overrides(_store(tmp_path), {key: too_low}, by="op")


def test_settings_payload_layers_new_overrides(tmp_path):
    store = _store(tmp_path)
    settings = _settings(tmp_path)
    base = build_settings_payload(settings)
    assert base["orchestrator_cadence_seconds"] == 900
    assert base["stale_work_seconds"] == 86_400
    assert base["max_agent_iterations"] == 10

    set_runtime_overrides(
        store,
        {
            "orchestrator_cadence_seconds": 300,
            "stale_work_seconds": 7200,
            "max_agent_iterations": 12,
        },
        by="op",
    )
    layered = build_settings_payload(
        settings, runtime_overrides=get_runtime_overrides(store)
    )
    assert layered["orchestrator_cadence_seconds"] == 300
    assert layered["stale_work_seconds"] == 7200
    assert layered["max_agent_iterations"] == 12


def test_orchestration_config_applies_cadence_and_stale_overrides(tmp_path):
    config = OrchestrationConfig.from_settings(_settings(tmp_path))
    layered = config.with_overrides(
        {"orchestrator_cadence_seconds": 300, "stale_work_seconds": 7200}
    )
    assert layered.cadence_seconds == 300
    assert layered.stale_work_seconds == 7200
    # max_agent_iterations is consumed by the runner, not this config
    assert config.with_overrides({"max_agent_iterations": 4}) == config
