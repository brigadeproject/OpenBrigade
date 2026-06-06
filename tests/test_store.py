from __future__ import annotations

import json

import pytest

from brigade.config import Settings
from brigade.schemas import Assignment, AssignmentStatus
from brigade.state import JsonStateStore
from brigade.store import PostgresStateStore, RedisRuntimeClient, open_state_store


def test_open_state_store_requires_postgres_without_json_opt_in(tmp_path):
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        allow_json_store=False,
    )

    with pytest.raises(RuntimeError, match="Postgres is required"):
        open_state_store(settings)


def test_open_state_store_allows_json_only_when_explicit(tmp_path):
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        allow_json_store=True,
    )

    assert isinstance(open_state_store(settings), JsonStateStore)


def test_json_state_store_claims_execution_once(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")

    assert store.try_claim_assignment_execution(
        "assignment-1",
        "runner-a",
        agent_id="sage",
    )

    claim = store.assignment_execution_claim("assignment-1")
    assert claim is not None
    assert claim["assignment_id"] == "assignment-1"
    assert claim["owner"] == "runner-a"
    assert claim["agent_id"] == "sage"


def test_json_state_store_rejects_duplicate_claim(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")

    assert store.try_claim_assignment_execution("assignment-1", "runner-a")
    assert not store.try_claim_assignment_execution("assignment-1", "runner-b")

    claim = store.assignment_execution_claim("assignment-1")
    assert claim is not None
    assert claim["owner"] == "runner-a"


def test_json_state_store_releases_claim_by_owner(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.try_claim_assignment_execution("assignment-1", "runner-a")

    assert not store.release_assignment_execution_claim("assignment-1", owner="runner-b")
    assert store.assignment_execution_claim("assignment-1") is not None
    assert store.release_assignment_execution_claim("assignment-1", owner="runner-a")
    assert store.assignment_execution_claim("assignment-1") is None


def test_json_state_store_reuses_active_assignment_by_idempotency_key(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    first = Assignment(
        assignment="Same work",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
        idempotency_key="same-key",
    )
    duplicate = Assignment(
        assignment="Same work",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
        idempotency_key="same-key",
    )

    created = store.add_assignment(first)
    reused = store.add_assignment(duplicate)

    assert reused.assignment_id == created.assignment_id
    assert [item.assignment_id for item in store.assignments()] == [created.assignment_id]


def test_json_state_store_reuses_archived_assignment_by_idempotency_key(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    first = Assignment(
        assignment="Same work",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
        idempotency_key="same-key",
    )
    first.transition_to(AssignmentStatus.ASSIGNED)
    first.mark_complete("done")
    store.add_assignment(first)
    store.archive_assignment(first, executive_summary="done")
    duplicate = Assignment(
        assignment="Same work",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
        idempotency_key="same-key",
    )

    reused = store.add_assignment(duplicate)

    assert reused.assignment_id == first.assignment_id
    assert store.assignments() == []
    assert len(store.assignment_history()) == 1


def test_postgres_archive_assignment_detaches_active_children_before_delete() -> None:
    parent = Assignment(
        assignment="Parent work",
        assigned_to="chief",
        created_by="human",
        source="direct_command",
    )
    parent.transition_to(AssignmentStatus.ASSIGNED)
    parent.mark_complete("done")
    child = Assignment(
        assignment="Child work",
        assigned_to="builder",
        created_by="chief",
        source="agent_delegate",
        parent_assignment_id=parent.assignment_id,
    )
    cursor = _FakeArchiveCursor(parent.assignment_id)
    store = PostgresStateStore.__new__(PostgresStateStore)
    store._connect = lambda: _FakeArchiveConnection(cursor)
    store._remove_assignment_runtime = lambda assignment_id: None
    store._record_assignment_provenance = lambda assignment: None
    store.assignments = lambda: [child]
    synced: list[list[Assignment]] = []
    store._sync_assignments_runtime = lambda assignments: synced.append(assignments)

    store.archive_assignment(parent, executive_summary="done")

    assert cursor.detached_children is True
    assert cursor.deleted_parent is True
    assert cursor.inserted_history is True
    assert synced == [[child]]


def test_json_state_store_local_inference_lock_rejects_overlap(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")

    store.acquire_local_inference_lock("sage", lock_ttl_seconds=60)

    try:
        store.acquire_local_inference_lock("scout", lock_ttl_seconds=60)
    except RuntimeError as exc:
        assert "already held" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("expected overlapping local inference lock to fail")

    store.release_local_inference_lock("sage", cooldown_seconds=0)
    assert store.local_inference()["status"] == "idle"


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    def ping(self):
        return True

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def delete(self, key):
        self.values.pop(key, None)
        self.lists.pop(key, None)
        self.hashes.pop(key, None)

    def lrem(self, key, count, value):
        self.lists[key] = [item for item in self.lists.get(key, []) if item != value]

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    def lrange(self, key, start, end):
        items = self.lists.get(key, [])
        return items[start:] if end == -1 else items[start : end + 1]

    def llen(self, key):
        return len(self.lists.get(key, []))

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    def hdel(self, key, field):
        self.hashes.get(key, {}).pop(field, None)

    def scan_iter(self, pattern):
        prefix = pattern.rstrip("*")
        return [key for key in self.values if key.startswith(prefix)]

    def ttl(self, key):
        return 60 if key in self.values else -2


def test_redis_runtime_client_claims_queue_and_inspects():
    redis = _FakeRedis()
    client = RedisRuntimeClient("redis://unused")
    client._client = lambda: redis

    assert client.claim_assignment_execution(
        {"assignment_id": "a1", "owner": "runner", "agent_id": "sage"},
        lease_seconds=60,
    )
    assert not client.claim_assignment_execution(
        {"assignment_id": "a1", "owner": "other", "agent_id": "sage"},
        lease_seconds=60,
    )

    payload = client.inspect()

    assert payload["ok"] is True
    assert payload["active_claim_count"] == 1
    assert payload["active_claims"][0]["owner"] == "runner"


class _FakePostgresCursor:
    def __init__(self, holder: dict[str, object]) -> None:
        self._holder = holder
        self._fetchone = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, sql: str, params=()) -> None:
        normalized = " ".join(sql.split())
        if normalized.startswith("insert into brigade_local_inference_state"):
            if self._holder["record"] is None:
                self._holder["record"] = json.loads(params[1])
            self._fetchone = None
            return
        if normalized == (
            "select record from brigade_local_inference_state "
            "where id = 'default' for update"
        ):
            record = self._holder["record"]
            self._fetchone = (record,) if record is not None else None
            return
        if normalized.startswith(
            "update brigade_local_inference_state "
            "set updated_at = %s, record = %s::jsonb"
        ):
            self._holder["record"] = json.loads(params[1])
            self._fetchone = None
            return
        raise AssertionError(f"unexpected SQL: {normalized}")

    def fetchone(self):
        return self._fetchone


class _FakePostgresConnection:
    def __init__(self, holder: dict[str, object]) -> None:
        self._holder = holder

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def cursor(self):
        return _FakePostgresCursor(self._holder)


class _FakeArchiveCursor:
    def __init__(self, parent_assignment_id: str) -> None:
        self.parent_assignment_id = parent_assignment_id
        self.detached_children = False
        self.deleted_parent = False
        self.inserted_history = False
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, sql: str, params=()) -> None:
        normalized = " ".join(sql.split())
        if normalized.startswith(
            "update brigade_assignments set parent_assignment_id = null"
        ):
            assert params[1] == self.parent_assignment_id
            self.detached_children = True
            self.rowcount = 1
            return
        if normalized == "delete from brigade_assignments where id = %s":
            assert self.detached_children is True
            assert params == (self.parent_assignment_id,)
            self.deleted_parent = True
            self.rowcount = 1
            return
        if normalized.startswith("insert into brigade_assignment_history"):
            assert self.deleted_parent is True
            self.inserted_history = True
            self.rowcount = 1
            return
        raise AssertionError(f"unexpected SQL: {normalized}")


class _FakeArchiveConnection:
    def __init__(self, cursor: _FakeArchiveCursor) -> None:
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def cursor(self):
        return self._cursor


def test_postgres_state_store_rejects_duplicate_claim(monkeypatch, tmp_path):
    store = PostgresStateStore("postgresql://unused", tmp_path)
    holder: dict[str, object] = {"record": None}

    monkeypatch.setattr(store, "_connect_transactional", lambda: _FakePostgresConnection(holder))
    monkeypatch.setattr(
        store,
        "_record_or_none",
        lambda sql, params=(): holder["record"],
    )

    assert store.try_claim_assignment_execution(
        "assignment-1",
        "runner-a",
        agent_id="sage",
    )
    assert not store.try_claim_assignment_execution(
        "assignment-1",
        "runner-b",
        agent_id="sage",
    )

    claim = store.assignment_execution_claim("assignment-1")
    assert claim is not None
    assert claim["owner"] == "runner-a"
    assert claim["agent_id"] == "sage"


def test_postgres_state_store_releases_claim(monkeypatch, tmp_path):
    store = PostgresStateStore("postgresql://unused", tmp_path)
    holder: dict[str, object] = {"record": None}

    monkeypatch.setattr(store, "_connect_transactional", lambda: _FakePostgresConnection(holder))
    monkeypatch.setattr(
        store,
        "_record_or_none",
        lambda sql, params=(): holder["record"],
    )

    assert store.try_claim_assignment_execution("assignment-1", "runner-a")
    assert not store.release_assignment_execution_claim("assignment-1", owner="runner-b")
    assert store.release_assignment_execution_claim("assignment-1", owner="runner-a")
    assert store.assignment_execution_claim("assignment-1") is None
