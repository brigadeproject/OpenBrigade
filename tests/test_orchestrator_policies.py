from __future__ import annotations

from brigade.orchestrator import policy_routed_chief_id
from brigade.schemas import Agent, Team
from brigade.state import JsonStateStore
from brigade.time import utc_now_iso


def _policy(**overrides) -> dict:
    base = {
        "policy_id": "p1",
        "rule_kind": "routing_rule",
        "assignment_kind": "failure_analysis",
        "target_team_id": "infra",
        "statement": "route failure analysis to infra",
        "active": True,
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    }
    base.update(overrides)
    return base


def test_add_and_list_policies_defaults_to_active_only(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.add_orchestrator_policy(_policy())
    store.add_orchestrator_policy(_policy(policy_id="p2", active=False))

    assert [item["policy_id"] for item in store.orchestrator_policies()] == ["p1"]
    assert {item["policy_id"] for item in store.orchestrator_policies(active_only=False)} == {
        "p1",
        "p2",
    }


def test_policies_filter_by_rule_kind_and_assignment_kind(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.add_orchestrator_policy(_policy())
    store.add_orchestrator_policy(
        _policy(policy_id="p2", rule_kind="freeform", assignment_kind=None, statement="be nice")
    )

    routing_only = store.orchestrator_policies(rule_kind="routing_rule")
    assert [item["policy_id"] for item in routing_only] == ["p1"]

    matching_kind = store.orchestrator_policies(assignment_kind="failure_analysis")
    assert [item["policy_id"] for item in matching_kind] == ["p1"]


def test_update_orchestrator_policy_retires_it(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.add_orchestrator_policy(_policy())

    policy = store.find_orchestrator_policy("p1")
    policy["active"] = False
    store.update_orchestrator_policy(policy)

    assert store.orchestrator_policies() == []
    assert store.find_orchestrator_policy("p1")["active"] is False


def test_policy_routed_chief_id_resolves_target_teams_chief(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(Agent("bolt", "BOLT", "workspace-bolt", role="crew_chief"))
    store.upsert_team(
        Team(team_id="infra", display_name="Infra", crew_chief_id="bolt", members=["bolt"])
    )
    store.add_orchestrator_policy(_policy())

    assert policy_routed_chief_id(store, "failure_analysis") == "bolt"
    assert policy_routed_chief_id(store, "mission") is None


def test_policy_routed_chief_id_none_without_active_policy(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    assert policy_routed_chief_id(store, "failure_analysis") is None
