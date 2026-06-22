"""Manual Orchestration backend: reissue-as-new lineage + task edit/reassign."""

from __future__ import annotations

import pytest

from brigade.config import Settings
from brigade.schemas import Agent, Assignment, AssignmentStatus, Priority
from brigade.services import (
    AssignmentActionError,
    reissue_assignment_as_new,
    update_assignment_fields,
)
from brigade.state import JsonStateStore


def _store(tmp_path) -> JsonStateStore:
    store = JsonStateStore(tmp_path / "state.json")
    store.add_agent(Agent("ada", "ADA", "workspace-ada"))
    store.add_agent(Agent("lin", "LIN", "workspace-lin"))
    return store


def _queued(store, *, agent="ada", **kwargs) -> Assignment:
    a = Assignment(
        assignment=kwargs.pop("text", "do the work"),
        assigned_to=agent,
        created_by="human",
        source="direct_command",
        **kwargs,
    )
    store.add_assignment(a)
    return a


# --- reissue as new (lineage; never reuse ids) -----------------------------------


def test_reissue_as_new_mints_new_id_and_supersedes_original(tmp_path):
    store = _store(tmp_path)
    original = _queued(store, text="ship it")

    result = reissue_assignment_as_new(store, original.assignment_id, by="op")

    assert result["assignment_id"] != original.assignment_id
    assert result["reissued_from_assignment_id"] == original.assignment_id
    assert result["original_status"] == AssignmentStatus.SUPERSEDED.value
    # original retired out of the active set, new one is queued with lineage + prompt
    assert store.find_assignment(original.assignment_id) is None
    new = store.find_assignment(result["assignment_id"])
    assert new is not None
    assert new.status == AssignmentStatus.QUEUED
    assert new.assignment == "ship it"
    assert new.reissued_from_assignment_id == original.assignment_id


def test_reissue_as_new_repoints_dependents(tmp_path):
    store = _store(tmp_path)
    dep = _queued(store, text="upstream")
    downstream = _queued(store, agent="lin", text="downstream",
                         dependency_ids=[dep.assignment_id])

    result = reissue_assignment_as_new(store, dep.assignment_id, by="op")

    refreshed = store.find_assignment(downstream.assignment_id)
    assert dep.assignment_id not in refreshed.dependency_ids
    assert result["assignment_id"] in refreshed.dependency_ids


def test_reissue_as_new_emits_audit_event(tmp_path):
    store = _store(tmp_path)
    original = _queued(store)
    reissue_assignment_as_new(store, original.assignment_id, by="op")
    records = store.orchestrator_reasoning()
    blob = str(records)
    assert "operator_reissue" in blob and original.assignment_id in blob


# --- edit / reassign / reprioritize ----------------------------------------------


def test_update_edits_text_and_priority(tmp_path):
    store = _store(tmp_path)
    a = _queued(store, text="old")
    result = update_assignment_fields(
        store, a.assignment_id, assignment_text="new text", priority="high", by="op"
    )
    refreshed = store.find_assignment(a.assignment_id)
    assert refreshed.assignment == "new text"
    assert refreshed.priority == Priority.HIGH
    assert "text" in result["changes"]


def test_update_reassigns_to_known_agent(tmp_path):
    store = _store(tmp_path)
    a = _queued(store, agent="ada")
    update_assignment_fields(store, a.assignment_id, assigned_to="lin", by="op")
    assert store.find_assignment(a.assignment_id).assigned_to == "lin"


def test_update_rejects_unknown_agent(tmp_path):
    store = _store(tmp_path)
    a = _queued(store)
    with pytest.raises(AssignmentActionError):
        update_assignment_fields(store, a.assignment_id, assigned_to="ghost", by="op")


def test_update_rejects_running_task(tmp_path):
    store = _store(tmp_path)
    a = _queued(store)
    a.transition_to(AssignmentStatus.ASSIGNED)
    store.update_assignment(a)
    with pytest.raises(AssignmentActionError):
        update_assignment_fields(store, a.assignment_id, priority="low", by="op")


# --- routes ----------------------------------------------------------------------


def _app(tmp_path):
    pytest.importorskip("fastapi")
    from brigade.web import create_app

    store = _store(tmp_path)
    _queued(store, text="route task")
    settings = Settings(config_path=tmp_path / "brigade.config.json", data_dir=tmp_path)
    return create_app(settings, store), store


def test_manual_routes_registered(tmp_path):
    app, _ = _app(tmp_path)
    patch_paths = {
        r.path for r in app.routes if "PATCH" in getattr(r, "methods", set())
    }
    post_paths = {
        r.path for r in app.routes if "POST" in getattr(r, "methods", set())
    }
    assert "/api/tasks/{assignment_id}" in patch_paths
    assert "/api/tasks/{assignment_id}/reissue-as-new" in post_paths


def test_patch_and_reissue_as_new_routes(tmp_path):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    app, store = _app(tmp_path)
    client = TestClient(app)
    task_id = store.assignments()[0].assignment_id

    patched = client.patch(f"/api/tasks/{task_id}", json={"priority": "high"})
    assert patched.status_code == 200

    reissued = client.post(f"/api/tasks/{task_id}/reissue-as-new", json={"note": "redo"})
    assert reissued.status_code == 200
    assert reissued.json()["reissued_from_assignment_id"] == task_id

    assert client.patch("/api/tasks/nope", json={"priority": "low"}).status_code == 404
