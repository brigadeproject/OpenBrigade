from __future__ import annotations

import pytest

from brigade.governance import (
    accept_policy_projections,
    ensure_policy_projections_current,
    policy_projection_diff,
)
from brigade.schemas import Agent
from brigade.state import JsonStateStore
from brigade.workspace import ensure_agent_workspace


def test_policy_projection_diff_and_explicit_accept(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agent = Agent("ada", "ADA", "workspace-ada")
    store.add_agent(agent)
    workspace = ensure_agent_workspace(agent, tmp_path)
    ensure_policy_projections_current(store, agent)
    identity = workspace / "IDENTITY.md"
    identity.write_text(
        identity.read_text(encoding="utf-8") + "\nApproved update\n",
        encoding="utf-8",
    )

    diff = policy_projection_diff(store, agent)
    assert [item["path"] for item in diff] == ["IDENTITY.md"]
    accepted = accept_policy_projections(
        store,
        agent,
        paths=["IDENTITY.md"],
        actor="operator",
        source="operator:test",
    )

    assert accepted[0]["path"] == "IDENTITY.md"
    assert policy_projection_diff(store, agent) == []
    audit = store.provenance_records()[-1]
    assert audit["event"] == "policy_projection_accepted"
    assert audit["actor"] == "operator"


def test_policy_projection_accept_rejects_non_governing_or_missing_files(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agent = Agent("ada", "ADA", "workspace-ada")
    store.add_agent(agent)
    ensure_agent_workspace(agent, tmp_path)

    with pytest.raises(ValueError, match="not a governed"):
        accept_policy_projections(store, agent, paths=["HEARTBEAT.md"], actor="op", source="test")
    with pytest.raises(ValueError, match="does not exist"):
        accept_policy_projections(store, agent, paths=["SKILLS.md"], actor="op", source="test")
