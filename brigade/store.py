from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from brigade.config import Settings
from brigade.datastores import Neo4jProvenanceStore, QdrantEpisodeStore
from brigade.db import ensure_schema
from brigade.schemas import (
    Agent,
    AgentState,
    Assignment,
    AssignmentStatus,
    ChatMessage,
    Goal,
    Mission,
    Team,
    User,
    agent_from_dict,
    agent_state_from_dict,
    assignment_from_dict,
    chat_message_from_dict,
    goal_from_dict,
    mission_from_dict,
    team_from_dict,
    user_from_dict,
)
from brigade.state import EMPTY_STATE, JsonStateStore, _active_assignment_for_agent
from brigade.time import add_seconds_iso, parse_utc_iso, utc_now, utc_now_iso


class StateStore(Protocol):
    data_dir: Path

    def try_claim_assignment_execution(
        self,
        assignment_id: str,
        owner: str,
        *,
        agent_id: str | None = None,
    ) -> bool: ...

    def assignment_execution_claim(self, assignment_id: str) -> dict[str, Any] | None: ...

    def release_assignment_execution_claim(
        self,
        assignment_id: str,
        *,
        owner: str | None = None,
    ) -> bool: ...

    def acquire_local_inference_lock(
        self,
        holder: str,
        *,
        lock_ttl_seconds: int,
    ) -> None: ...

    def release_local_inference_lock(
        self,
        holder: str,
        *,
        cooldown_seconds: int,
    ) -> None: ...

    def add_assignment(self, assignment: Assignment) -> Assignment: ...

    def assignments(self) -> list[Assignment]: ...

    def active_assignment_for_agent(self, agent_id: str) -> Assignment | None: ...

    def find_assignment(self, assignment_id: str) -> Assignment | None: ...

    def find_assignment_by_idempotency_key(self, idempotency_key: str) -> Assignment | None: ...

    def replace_assignments(self, assignments: list[Assignment]) -> None: ...

    def update_assignment(self, assignment: Assignment) -> None: ...

    def archive_assignment(self, assignment: Assignment, executive_summary: str) -> None: ...

    def assignment_history(self) -> list[dict[str, Any]]: ...

    def runtime_overrides(self) -> dict[str, Any]: ...

    def set_runtime_overrides(self, overrides: dict[str, Any]) -> dict[str, Any]: ...

    def set_model_inventory(self, inventory: dict[str, Any]) -> dict[str, Any]: ...

    def model_inventory(self) -> dict[str, Any]: ...

    def set_mission(self, mission: Mission) -> None: ...

    def mission(self) -> Mission | None: ...

    def add_user(self, user: User) -> None: ...

    def users(self) -> list[User]: ...

    def add_agent(self, agent: Agent) -> None: ...

    def agents(self) -> list[Agent]: ...

    def delete_agent(self, agent_id: str) -> None: ...

    def upsert_team(self, team: Team) -> None: ...

    def teams(self) -> list[Team]: ...

    def upsert_agent_state(self, agent_state: AgentState) -> None: ...

    def agent_states(self) -> dict[str, AgentState]: ...

    def add_goal(self, agent_id: str, goal: Goal) -> None: ...

    def ensure_goal(self, agent_id: str, goal: Goal) -> bool: ...

    def dedupe_goals(self, agent_id: str | None = None) -> bool: ...

    def goals(self, agent_id: str | None = None) -> dict[str, list[Goal]]: ...

    def goal_records(self, agent_id: str | None = None) -> list[dict[str, Any]]: ...

    def add_alert(self, message: str) -> None: ...

    def alerts(self) -> list[str]: ...

    def clear_alerts(self) -> int: ...

    def add_knowledge_document(self, document: dict[str, Any]) -> None: ...

    def knowledge_documents(self) -> list[dict[str, Any]]: ...

    def add_knowledge_chunk(self, chunk: dict[str, Any]) -> None: ...

    def knowledge_chunks(self, document_id: str | None = None) -> list[dict[str, Any]]: ...

    def add_message(self, message: ChatMessage) -> None: ...

    def messages(self, channel: str | None = None) -> list[ChatMessage]: ...

    def add_orchestrator_reasoning(self, record: dict[str, Any]) -> None: ...

    def orchestrator_reasoning(self) -> list[dict[str, Any]]: ...

    def add_proposal(self, proposal: dict[str, Any]) -> dict[str, Any]: ...

    def proposals(
        self,
        kind: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def find_proposal(self, proposal_id: str) -> dict[str, Any] | None: ...

    def update_proposal(self, proposal: dict[str, Any]) -> None: ...

    def add_recurrence(self, recurrence: dict[str, Any]) -> dict[str, Any]: ...

    def recurrences(self, enabled: bool | None = None) -> list[dict[str, Any]]: ...

    def update_recurrence(self, recurrence: dict[str, Any]) -> None: ...

    def add_orchestrator_policy(self, policy: dict[str, Any]) -> dict[str, Any]: ...

    def orchestrator_policies(
        self,
        *,
        active_only: bool = True,
        rule_kind: str | None = None,
        assignment_kind: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def find_orchestrator_policy(self, policy_id: str) -> dict[str, Any] | None: ...

    def update_orchestrator_policy(self, policy: dict[str, Any]) -> None: ...

    def add_usage_record(self, record: dict[str, Any]) -> None: ...

    def usage_records(self) -> list[dict[str, Any]]: ...

    def upsert_cloud_job(self, job: dict[str, Any]) -> None: ...

    def cloud_jobs(self, status: str | None = None) -> list[dict[str, Any]]: ...

    def set_financial_report(self, report: dict[str, Any]) -> None: ...

    def latest_financial_report(self) -> dict[str, Any] | None: ...

    def set_local_inference(self, record: dict[str, Any]) -> None: ...

    def local_inference(self) -> dict[str, Any]: ...

    def add_transcript(self, transcript: dict[str, Any]) -> None: ...

    def transcripts(self) -> list[dict[str, Any]]: ...

    def add_episode(self, episode: dict[str, Any]) -> None: ...

    def episodes(self) -> list[dict[str, Any]]: ...

    def search_episodes(self, query: str, limit: int = 3) -> list[dict[str, Any]]: ...

    def add_provenance_record(self, record: dict[str, Any]) -> None: ...

    def provenance_records(self) -> list[dict[str, Any]]: ...

    def external_datastore_status(self) -> dict[str, dict[str, object]]: ...

    def add_connector_audit_event(self, record: dict[str, Any]) -> None: ...

    def connector_audit_events(
        self,
        provider: str | None = None,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]: ...

    def upsert_external_identity(self, record: dict[str, Any]) -> None: ...

    def external_identity(self, provider: str, external_user_id: str) -> dict[str, Any] | None: ...

    def external_identities(
        self,
        provider: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]: ...


class RedisRuntimeClient:
    PENDING_ASSIGNMENTS_KEY = "brigade:runtime:assignments:pending"
    PENDING_ASSIGNMENT_RECORDS_KEY = "brigade:runtime:assignments:records"
    ASSIGNMENT_CLAIM_PREFIX = "brigade:runtime:claims:assignment:"
    ALERT_QUEUE_KEY = "brigade:runtime:alerts"
    LOCAL_INFERENCE_KEY = "brigade:runtime:local_inference"
    LOCAL_INFERENCE_LOCK_KEY = "brigade:runtime:locks:local_inference"
    RUNTIME_CONFIG_KEY = "brigade:runtime:config"

    def __init__(self, url: str | None) -> None:
        self.url = url

    def available(self) -> bool:
        return bool(self.url)

    def health(self) -> dict[str, object]:
        if not self.url:
            return {"backend": "redis", "ok": False, "reason": "not configured"}
        try:
            self._execute(lambda client: client.ping())
        except RuntimeError as exc:
            return {"backend": "redis", "ok": False, "reason": str(exc)}
        return {"backend": "redis", "ok": True, "url": self.url}

    def get_json(self, key: str) -> dict[str, Any] | None:
        payload = self._execute(lambda client: client.get(key))
        if not payload:
            return None
        return json.loads(payload)

    def set_json(self, key: str, payload: dict[str, Any]) -> None:
        self._execute(lambda client: client.set(key, json.dumps(payload, sort_keys=True)))

    def runtime_overrides(self) -> dict[str, Any]:
        return self.get_json(self.RUNTIME_CONFIG_KEY) or {}

    def set_runtime_overrides(self, overrides: dict[str, Any]) -> None:
        self.set_json(self.RUNTIME_CONFIG_KEY, dict(overrides))

    def enqueue_pending_assignment(self, assignment: Assignment) -> None:
        if assignment.status != AssignmentStatus.QUEUED:
            self.remove_pending_assignment(assignment.assignment_id)
            return

        def operation(client) -> None:
            client.lrem(self.PENDING_ASSIGNMENTS_KEY, 0, assignment.assignment_id)
            client.rpush(self.PENDING_ASSIGNMENTS_KEY, assignment.assignment_id)
            client.hset(
                self.PENDING_ASSIGNMENT_RECORDS_KEY,
                assignment.assignment_id,
                json.dumps(assignment.to_dict(), sort_keys=True),
            )

        self._execute(operation)

    def remove_pending_assignment(self, assignment_id: str) -> None:
        def operation(client) -> None:
            client.lrem(self.PENDING_ASSIGNMENTS_KEY, 0, assignment_id)
            client.hdel(self.PENDING_ASSIGNMENT_RECORDS_KEY, assignment_id)

        self._execute(operation)

    def acknowledge_assignment(self, assignment_id: str) -> None:
        self.remove_pending_assignment(assignment_id)

    def fail_assignment(self, assignment_id: str, reason: str) -> None:
        self.remove_pending_assignment(assignment_id)
        self.enqueue_alert(f"assignment {assignment_id} failed runtime queue handling: {reason}")

    def pending_assignment_ids(self) -> list[str]:
        return list(
            self._execute(lambda client: client.lrange(self.PENDING_ASSIGNMENTS_KEY, 0, -1))
        )

    def reconcile_pending_assignments(self, assignments: list[Assignment]) -> None:
        desired_ids: list[str] = []
        desired_records: dict[str, str] = {}
        for assignment in assignments:
            if assignment.status != AssignmentStatus.QUEUED:
                continue
            desired_ids.append(assignment.assignment_id)
            desired_records[assignment.assignment_id] = json.dumps(
                assignment.to_dict(), sort_keys=True
            )

        def operation(client) -> None:
            # No-op when Redis already matches the desired state, so idle
            # reconcile ticks stop churning the AOF with delete+rebuild writes.
            current_ids = list(client.lrange(self.PENDING_ASSIGNMENTS_KEY, 0, -1))
            current_records = dict(client.hgetall(self.PENDING_ASSIGNMENT_RECORDS_KEY))
            if current_ids == desired_ids and current_records == desired_records:
                return

            client.delete(self.PENDING_ASSIGNMENTS_KEY)
            client.delete(self.PENDING_ASSIGNMENT_RECORDS_KEY)
            for assignment_id in desired_ids:
                client.rpush(self.PENDING_ASSIGNMENTS_KEY, assignment_id)
                client.hset(
                    self.PENDING_ASSIGNMENT_RECORDS_KEY,
                    assignment_id,
                    desired_records[assignment_id],
                )

        self._execute(operation)

    def recover_pending_assignments(self, assignments: list[Assignment]) -> None:
        self.reconcile_pending_assignments(assignments)

    def claim_assignment_execution(self, claim: dict[str, Any], lease_seconds: int) -> bool:
        key = self._assignment_claim_key(str(claim["assignment_id"]))
        payload = json.dumps(claim, sort_keys=True)
        return bool(
            self._execute(lambda client: client.set(key, payload, nx=True, ex=lease_seconds))
        )

    def assignment_execution_claim(self, assignment_id: str) -> dict[str, Any] | None:
        payload = self._execute(
            lambda client: client.get(self._assignment_claim_key(assignment_id))
        )
        return json.loads(payload) if payload else None

    def release_assignment_execution_claim(
        self,
        assignment_id: str,
        *,
        owner: str | None = None,
    ) -> bool:
        key = self._assignment_claim_key(assignment_id)

        def operation(client) -> bool:
            payload = client.get(key)
            if not payload:
                return False
            claim = json.loads(payload)
            if owner is not None and claim.get("owner") != owner:
                return False
            client.delete(key)
            return True

        return bool(self._execute(operation))

    def enqueue_alert(self, message: str) -> None:
        record = {"message": message, "created_at": utc_now_iso()}
        self._execute(lambda client: client.rpush(self.ALERT_QUEUE_KEY, json.dumps(record)))

    def clear_alerts(self) -> None:
        self._execute(lambda client: client.delete(self.ALERT_QUEUE_KEY))

    def acquire_local_inference_lock(self, holder: str, *, lock_ttl_seconds: int) -> None:
        previous = self.get_json(self.LOCAL_INFERENCE_KEY) or {}
        next_available = previous.get("next_available")
        if next_available and parse_utc_iso(str(next_available)) > utc_now():
            raise RuntimeError(f"local inference unavailable until {next_available}")
        locked_at = utc_now_iso()
        record = {
            "status": "busy",
            "holder": holder,
            "last_completed": previous.get("last_completed"),
            "next_available": next_available,
            "locked_at": locked_at,
            "lock_expires_at": add_seconds_iso(locked_at, lock_ttl_seconds),
        }
        acquired = self._execute(
            lambda client: client.set(
                self.LOCAL_INFERENCE_LOCK_KEY,
                json.dumps(record, sort_keys=True),
                nx=True,
                ex=lock_ttl_seconds,
            )
        )
        if not acquired:
            active = self.get_json(self.LOCAL_INFERENCE_LOCK_KEY) or previous
            active_holder = active.get("holder") or "another local inference job"
            raise RuntimeError(f"local inference already held by {active_holder}")
        self.set_json(self.LOCAL_INFERENCE_KEY, record)

    def release_local_inference_lock(self, holder: str, *, cooldown_seconds: int) -> dict[str, Any]:
        current = self.get_json(self.LOCAL_INFERENCE_KEY) or {}
        lock = self.get_json(self.LOCAL_INFERENCE_LOCK_KEY) or current
        if lock.get("holder") not in {None, holder}:
            return current
        completed_at = utc_now_iso()
        record = {
            "status": "idle",
            "holder": None,
            "last_completed": completed_at,
            "next_available": add_seconds_iso(completed_at, cooldown_seconds),
        }
        self._execute(lambda client: client.delete(self.LOCAL_INFERENCE_LOCK_KEY))
        self.set_json(self.LOCAL_INFERENCE_KEY, record)
        return record

    def connector_rate_limit_allow(self, key: str, *, limit: int, window_seconds: int) -> bool:
        redis_key = f"brigade:runtime:connector-rate:{key}"

        def operation(client) -> bool:
            count = int(client.incr(redis_key))
            if count == 1:
                client.expire(redis_key, window_seconds)
            return count <= limit

        return bool(self._execute(operation))

    def inspect(self, limit: int = 10) -> dict[str, Any]:
        health = self.health()
        if not health.get("ok"):
            return health

        def operation(client) -> dict[str, Any]:
            claim_keys = sorted(client.scan_iter(f"{self.ASSIGNMENT_CLAIM_PREFIX}*"))
            claims = []
            for key in claim_keys[:limit]:
                payload = client.get(key)
                if not payload:
                    continue
                claim = json.loads(payload)
                claim["ttl_seconds"] = client.ttl(key)
                claims.append(claim)
            pending_ids = client.lrange(self.PENDING_ASSIGNMENTS_KEY, 0, max(limit - 1, 0))
            alert_records = []
            for payload in client.lrange(self.ALERT_QUEUE_KEY, 0, max(limit - 1, 0)):
                try:
                    alert_records.append(json.loads(payload))
                except json.JSONDecodeError:
                    alert_records.append({"message": payload})
            return {
                "backend": "redis",
                "ok": True,
                "pending_count": client.llen(self.PENDING_ASSIGNMENTS_KEY),
                "pending_assignment_ids": pending_ids,
                "active_claim_count": len(claim_keys),
                "active_claims": claims,
                "alert_queue_count": client.llen(self.ALERT_QUEUE_KEY),
                "alerts": alert_records,
                "local_inference": self.get_json(self.LOCAL_INFERENCE_KEY),
                "local_inference_lock_ttl_seconds": client.ttl(self.LOCAL_INFERENCE_LOCK_KEY),
            }

        return self._execute(operation)

    def _assignment_claim_key(self, assignment_id: str) -> str:
        return f"{self.ASSIGNMENT_CLAIM_PREFIX}{assignment_id}"

    def _execute(self, operation):
        client = self._client()
        if client is None:
            return None
        try:
            return operation(client)
        except Exception as exc:
            raise RuntimeError(f"redis runtime request failed: {exc}") from exc

    def _client(self):
        if not self.url:
            return None
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError(
                "redis is required to use the live runtime store"
            ) from exc
        return redis.Redis.from_url(self.url, decode_responses=True)


class PostgresStateStore:
    def __init__(
        self,
        dsn: str,
        data_dir: Path,
        redis_url: str | None = None,
        qdrant_url: str | None = None,
        qdrant_collection: str = "brigade_episodes",
        ollama_embedding_base_url: str | None = None,
        ollama_embedding_model: str | None = None,
        ollama_embedding_vector_size: int | None = None,
        neo4j_http_url: str | None = None,
        neo4j_auth: str | None = None,
    ) -> None:
        self.dsn = dsn
        self.data_dir = data_dir
        self._redis = RedisRuntimeClient(redis_url)
        self._qdrant = QdrantEpisodeStore(
            qdrant_url,
            collection=qdrant_collection,
            embedding_base_url=ollama_embedding_base_url,
            embedding_model=ollama_embedding_model,
            embedding_vector_size=ollama_embedding_vector_size,
        )
        self._neo4j = Neo4jProvenanceStore(neo4j_http_url, neo4j_auth)

    def is_empty(self) -> bool:
        return self._scalar("select count(*) from brigade_agents") == 0

    def try_claim_assignment_execution(
        self,
        assignment_id: str,
        owner: str,
        *,
        agent_id: str | None = None,
    ) -> bool:
        claimed_at = utc_now_iso()
        claim = {
            "assignment_id": assignment_id,
            "owner": owner,
            "agent_id": agent_id,
            "claimed_at": claimed_at,
            "lease_token": str(uuid4()),
            "lease_seconds": 3600,
            "expires_at": add_seconds_iso(claimed_at, 3600),
        }
        if self._redis.available():
            if not self._redis.claim_assignment_execution(claim, lease_seconds=3600):
                return False
            try:
                if not self._try_claim_assignment_execution_postgres(claim):
                    self._redis.release_assignment_execution_claim(assignment_id, owner=owner)
                    return False
            except Exception:
                self._redis.release_assignment_execution_claim(assignment_id, owner=owner)
                raise
            return True
        return self._try_claim_assignment_execution_postgres(claim)

    def _try_claim_assignment_execution_postgres(self, claim: dict[str, Any]) -> bool:
        assignment_id = str(claim["assignment_id"])
        with self._connect_transactional() as conn:
            with conn.cursor() as cursor:
                record = self._locked_local_inference_record(cursor)
                claims = _execution_claims(record)
                if assignment_id in claims:
                    return False
                claims[assignment_id] = claim
                record[_ASSIGNMENT_EXECUTION_CLAIMS_KEY] = claims
                cursor.execute(
                    """
                    update brigade_local_inference_state
                    set updated_at = %s, record = %s::jsonb
                    where id = 'default'
                    """,
                    (utc_now_iso(), json.dumps(record, sort_keys=True)),
                )
        return True

    def assignment_execution_claim(self, assignment_id: str) -> dict[str, Any] | None:
        if self._redis.available():
            claim = self._redis.assignment_execution_claim(assignment_id)
            if claim is not None:
                return claim
        return self._assignment_execution_claim_postgres(assignment_id)

    def _assignment_execution_claim_postgres(self, assignment_id: str) -> dict[str, Any] | None:
        record = self._record_or_none(
            "select record from brigade_local_inference_state where id = 'default'"
        )
        if not record:
            return None
        claim = _execution_claims(record).get(assignment_id)
        return dict(claim) if isinstance(claim, dict) else None

    def release_assignment_execution_claim(
        self,
        assignment_id: str,
        *,
        owner: str | None = None,
    ) -> bool:
        redis_released = False
        if self._redis.available():
            try:
                redis_released = self._redis.release_assignment_execution_claim(
                    assignment_id,
                    owner=owner,
                )
            except RuntimeError:
                redis_released = False
        postgres_released = self._release_assignment_execution_claim_postgres(
            assignment_id,
            owner=owner,
        )
        return redis_released or postgres_released

    def _release_assignment_execution_claim_postgres(
        self,
        assignment_id: str,
        *,
        owner: str | None = None,
    ) -> bool:
        with self._connect_transactional() as conn:
            with conn.cursor() as cursor:
                record = self._locked_local_inference_record(cursor)
                claims = _execution_claims(record)
                claim = claims.get(assignment_id)
                if not isinstance(claim, dict):
                    return False
                if owner is not None and claim.get("owner") != owner:
                    return False
                claims.pop(assignment_id, None)
                record[_ASSIGNMENT_EXECUTION_CLAIMS_KEY] = claims
                cursor.execute(
                    """
                    update brigade_local_inference_state
                    set updated_at = %s, record = %s::jsonb
                    where id = 'default'
                    """,
                    (utc_now_iso(), json.dumps(record, sort_keys=True)),
                )
        return True

    def add_assignment(self, assignment: Assignment) -> Assignment:
        existing = self.find_assignment_by_idempotency_key(assignment.idempotency_key or "")
        if existing is not None:
            return existing
        self._upsert_assignment(assignment)
        self._sync_assignment_runtime(assignment)
        self._record_assignment_provenance(assignment)
        return assignment

    def assignments(self) -> list[Assignment]:
        assignments = [
            assignment_from_dict(record)
            for record in self._records(
                "select record from brigade_assignments order by created_at, id"
            )
        ]
        self._sync_assignments_runtime(assignments)
        return assignments

    def active_assignment_for_agent(self, agent_id: str) -> Assignment | None:
        return _active_assignment_for_agent(self.assignments(), agent_id)

    def find_assignment(self, assignment_id: str) -> Assignment | None:
        record = self._record_or_none(
            "select record from brigade_assignments where id = %s",
            (assignment_id,),
        )
        return assignment_from_dict(record) if record else None

    def find_assignment_by_idempotency_key(self, idempotency_key: str) -> Assignment | None:
        if not idempotency_key:
            return None
        active = self._record_or_none(
            """
            select record
            from brigade_assignments
            where idempotency_key = %s
            order by created_at desc, id desc
            limit 1
            """,
            (idempotency_key,),
        )
        if active is not None:
            return assignment_from_dict(active)
        archived = self._record_or_none(
            """
            select record
            from brigade_assignment_history
            where record->>'idempotency_key' = %s
            order by archived_at desc, id desc
            limit 1
            """,
            (idempotency_key,),
        )
        return assignment_from_dict(archived) if archived else None

    def replace_assignments(self, assignments: list[Assignment]) -> None:
        keep_ids = [item.assignment_id for item in assignments]
        with self._connect() as conn:
            with conn.cursor() as cursor:
                if keep_ids:
                    cursor.execute(
                        "delete from brigade_assignments where id <> all(%s)",
                        (keep_ids,),
                    )
                else:
                    cursor.execute("delete from brigade_assignments")
                for assignment in assignments:
                    self._upsert_assignment(assignment, cursor=cursor)
        self._sync_assignments_runtime(assignments)
        for assignment in assignments:
            self._record_assignment_provenance(assignment)

    def update_assignment(self, assignment: Assignment) -> None:
        self._upsert_assignment(assignment)
        self._sync_assignment_runtime(assignment)
        self._record_assignment_provenance(assignment)

    def archive_assignment(self, assignment: Assignment, executive_summary: str) -> None:
        detached_children = False
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    update brigade_assignments
                    set parent_assignment_id = null,
                        updated_at = %s,
                        record = jsonb_set(
                          record,
                          '{parent_assignment_id}',
                          'null'::jsonb,
                          true
                        )
                    where parent_assignment_id = %s
                    """,
                    (utc_now_iso(), assignment.assignment_id),
                )
                detached_children = getattr(cursor, "rowcount", 0) > 0
                cursor.execute(
                    "delete from brigade_assignments where id = %s",
                    (assignment.assignment_id,),
                )
                cursor.execute(
                    """
                    insert into brigade_assignment_history (
                      id, assignment_id, archived_at, final_status,
                      executive_summary, failure_info, record
                    )
                    values (%s, %s, %s, %s, %s, %s, %s::jsonb)
                    on conflict (id) do update set
                      assignment_id = excluded.assignment_id,
                      archived_at = excluded.archived_at,
                      final_status = excluded.final_status,
                      executive_summary = excluded.executive_summary,
                      failure_info = excluded.failure_info,
                      record = excluded.record
                    """,
                    (
                        str(uuid4()),
                        assignment.assignment_id,
                        assignment.updated_at,
                        assignment.status.value,
                        executive_summary,
                        assignment.last_error,
                        json.dumps(assignment.to_dict(), sort_keys=True),
                    ),
                )
        self._remove_assignment_runtime(assignment.assignment_id)
        if detached_children:
            self._sync_assignments_runtime(self.assignments())
        self._record_assignment_provenance(assignment)

    def assignment_history(self) -> list[dict[str, Any]]:
        return list(
            self._rows_json(
                """
                select json_build_object(
                  'assignment_id', assignment_id,
                  'archived_at', archived_at,
                  'final_status', final_status,
                  'executive_summary', executive_summary,
                  'failure_info', failure_info,
                  'record', record
                )
                from brigade_assignment_history
                order by archived_at, id
                """
            )
        )

    def set_mission(self, mission: Mission) -> None:
        record = mission.to_dict()
        self._execute(
            """
            insert into brigade_missions (
              id, statement, success_criteria, explicitly_not, set_at, last_reviewed, record
            )
            values (%s, %s, %s::jsonb, %s::jsonb, %s, %s, %s::jsonb)
            on conflict (id) do update set
              statement = excluded.statement,
              success_criteria = excluded.success_criteria,
              explicitly_not = excluded.explicitly_not,
              set_at = excluded.set_at,
              last_reviewed = excluded.last_reviewed,
              record = excluded.record
            """,
            (
                "current",
                mission.statement,
                json.dumps(mission.success_criteria),
                json.dumps(mission.explicitly_not),
                mission.set_at,
                mission.last_reviewed,
                json.dumps(record, sort_keys=True),
            ),
        )

    def mission(self) -> Mission | None:
        record = self._record_or_none("select record from brigade_missions where id = 'current'")
        return mission_from_dict(record) if record else None

    def add_user(self, user: User) -> None:
        record = user.to_dict()
        self._execute(
            """
            insert into brigade_users (username, role, created_at, record)
            values (%s, %s, %s, %s::jsonb)
            on conflict (username) do update set
              role = excluded.role,
              created_at = excluded.created_at,
              record = excluded.record
            """,
            (
                user.username,
                user.role.value,
                user.created_at,
                json.dumps(record, sort_keys=True),
            ),
        )

    def users(self) -> list[User]:
        return [
            user_from_dict(record)
            for record in self._records(
                "select record from brigade_users order by created_at, username"
            )
        ]

    def add_agent(self, agent: Agent) -> None:
        record = agent.to_dict()
        self._execute(
            """
            insert into brigade_agents (
              id, display_name, workspace_path, role, status, specialties, created_at, record
            )
            values (%s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb)
            on conflict (id) do update set
              display_name = excluded.display_name,
              workspace_path = excluded.workspace_path,
              role = excluded.role,
              specialties = excluded.specialties,
              record = excluded.record
            """,
            (
                agent.agent_id,
                agent.display_name,
                agent.workspace_path,
                agent.role,
                "idle",
                json.dumps(agent.specialties),
                agent.created_at,
                json.dumps(record, sort_keys=True),
            ),
        )

    def agents(self) -> list[Agent]:
        return [
            agent_from_dict(record)
            for record in self._records("select record from brigade_agents order by created_at, id")
        ]

    def delete_agent(self, agent_id: str) -> None:
        self._execute("delete from brigade_agents where id = %s", (agent_id,))

    def upsert_team(self, team: Team) -> None:
        record = team.to_dict()
        self._execute(
            """
            insert into brigade_teams (
              id, display_name, description, parent_team_id, crew_chief_id,
              members, created_at, updated_at, record
            )
            values (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb)
            on conflict (id) do update set
              display_name = excluded.display_name,
              description = excluded.description,
              parent_team_id = excluded.parent_team_id,
              crew_chief_id = excluded.crew_chief_id,
              members = excluded.members,
              updated_at = excluded.updated_at,
              record = excluded.record
            """,
            (
                team.team_id,
                team.display_name,
                team.description,
                team.parent_team_id,
                team.crew_chief_id,
                json.dumps(team.members, sort_keys=True),
                team.created_at,
                team.updated_at,
                json.dumps(record, sort_keys=True),
            ),
        )
        self._record_team_provenance(team)

    def teams(self) -> list[Team]:
        return [
            team_from_dict(record)
            for record in self._records("select record from brigade_teams order by created_at, id")
        ]

    def upsert_agent_state(self, agent_state: AgentState) -> None:
        record = agent_state.to_dict()
        self._execute(
            """
            insert into brigade_agent_states (agent_id, updated_at, record)
            values (%s, %s, %s::jsonb)
            on conflict (agent_id) do update set
              updated_at = excluded.updated_at,
              record = excluded.record
            """,
            (
                agent_state.agent,
                utc_now_iso(),
                json.dumps(record, sort_keys=True),
            ),
        )

    def agent_states(self) -> dict[str, AgentState]:
        return {
            state.agent: state
            for state in (
                agent_state_from_dict(record)
                for record in self._records(
                    "select record from brigade_agent_states order by updated_at, agent_id"
                )
            )
        }

    def add_goal(self, agent_id: str, goal: Goal) -> None:
        self._insert_goal(agent_id, goal)

    def ensure_goal(self, agent_id: str, goal: Goal) -> bool:
        target = _goal_identity(goal.to_dict())
        for existing in self.goals(agent_id).get(agent_id, []):
            if _goal_identity(existing.to_dict()) == target:
                return False
        self._insert_goal(agent_id, goal)
        return True

    def dedupe_goals(self, agent_id: str | None = None) -> bool:
        changed = False
        goal_map = self.goals(agent_id)
        target_ids = [agent_id] if agent_id is not None else list(goal_map.keys())
        for current_agent in target_ids:
            existing = goal_map.get(current_agent, [])
            seen: set[tuple[object, ...]] = set()
            deduped: list[Goal] = []
            agent_changed = False
            for goal in existing:
                identity = _goal_identity(goal.to_dict())
                if identity in seen:
                    changed = True
                    agent_changed = True
                    continue
                seen.add(identity)
                deduped.append(goal)
            if agent_changed:
                self._execute("delete from brigade_goals where agent_id = %s", (current_agent,))
                for goal in deduped:
                    self._insert_goal(current_agent, goal)
        return changed

    def goals(self, agent_id: str | None = None) -> dict[str, list[Goal]]:
        sql = "select agent_id, record from brigade_goals"
        params: tuple[object, ...] = ()
        if agent_id is not None:
            sql += " where agent_id = %s"
            params = (agent_id,)
        sql += " order by set_at, id"
        rows = self._query(sql, params)
        goals: dict[str, list[Goal]] = {}
        for current_agent_id, record in rows:
            goals.setdefault(current_agent_id, []).append(goal_from_dict(_decode_json(record)))
        if agent_id is not None:
            goals.setdefault(agent_id, [])
        return goals

    def goal_records(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        sql = "select id, agent_id, record from brigade_goals"
        params: tuple[object, ...] = ()
        if agent_id is not None:
            sql += " where agent_id = %s"
            params = (agent_id,)
        sql += " order by set_at, id"
        return [
            {
                "goal_id": str(goal_id),
                "agent_id": str(current_agent_id),
                "goal": goal_from_dict(_decode_json(record)),
            }
            for goal_id, current_agent_id, record in self._query(sql, params)
        ]

    def add_alert(self, message: str) -> None:
        if self._redis.available():
            try:
                self._redis.enqueue_alert(message)
            except RuntimeError:
                pass
        self._add_alert_postgres(message)

    def _add_alert_postgres(self, message: str) -> None:
        self._execute(
            "insert into brigade_alerts (id, message, created_at) values (%s, %s, %s)",
            (str(uuid4()), message, utc_now_iso()),
        )

    def alerts(self) -> list[str]:
        return [
            row[0]
            for row in self._query(
                "select message from brigade_alerts order by created_at, id"
            )
        ]

    def clear_alerts(self) -> int:
        count = int(self._query("select count(*) from brigade_alerts")[0][0])
        self._execute("delete from brigade_alerts")
        if self._redis.available():
            try:
                self._redis.clear_alerts()
            except RuntimeError:
                pass
        return count

    def add_knowledge_document(self, document: dict[str, Any]) -> None:
        self._execute(
            """
            insert into brigade_knowledge_documents (
              id, title, source, document_type, content_path, ingested_at, metadata
            )
            values (%s, %s, %s, %s, %s, %s, %s::jsonb)
            on conflict (id) do update set
              title = excluded.title,
              source = excluded.source,
              document_type = excluded.document_type,
              content_path = excluded.content_path,
              ingested_at = excluded.ingested_at,
              metadata = excluded.metadata
            """,
            (
                document["document_id"],
                document["title"],
                document["source"],
                document["document_type"],
                document["content_path"],
                document["ingested_at"],
                json.dumps(document.get("metadata", {}), sort_keys=True),
            ),
        )

    def knowledge_documents(self) -> list[dict[str, Any]]:
        rows = self._query(
            """
            select id, title, source, document_type, content_path, ingested_at, metadata
            from brigade_knowledge_documents
            order by ingested_at, id
            """
        )
        documents: list[dict[str, Any]] = []
        for row in rows:
            documents.append(
                {
                    "document_id": row[0],
                    "title": row[1],
                    "source": row[2],
                    "document_type": row[3],
                    "content_path": row[4],
                    "ingested_at": _as_iso(row[5]),
                    "metadata": _decode_json(row[6]),
                }
            )
        return documents

    def add_knowledge_chunk(self, chunk: dict[str, Any]) -> None:
        self._execute(
            """
            insert into brigade_knowledge_chunks (
              id, document_id, chunk_index, text, source, content_path, created_at, record
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            on conflict (id) do update set
              document_id = excluded.document_id,
              chunk_index = excluded.chunk_index,
              text = excluded.text,
              source = excluded.source,
              content_path = excluded.content_path,
              created_at = excluded.created_at,
              record = excluded.record
            """,
            (
                chunk["chunk_id"],
                chunk["document_id"],
                chunk["chunk_index"],
                chunk["text"],
                chunk.get("source"),
                chunk.get("content_path"),
                chunk["created_at"],
                json.dumps(chunk, sort_keys=True),
            ),
        )

    def knowledge_chunks(self, document_id: str | None = None) -> list[dict[str, Any]]:
        sql = "select record from brigade_knowledge_chunks"
        params: tuple[object, ...] = ()
        if document_id is not None:
            sql += " where document_id = %s"
            params = (document_id,)
        sql += " order by created_at, chunk_index, id"
        return list(self._records(sql, params))

    def add_message(self, message: ChatMessage) -> None:
        self._execute(
            """
            insert into brigade_chat_messages (
              id, channel, sender, recipient, content, created_at, metadata
            )
            values (%s, %s, %s, %s, %s, %s, %s::jsonb)
            on conflict (id) do update set
              channel = excluded.channel,
              sender = excluded.sender,
              recipient = excluded.recipient,
              content = excluded.content,
              created_at = excluded.created_at,
              metadata = excluded.metadata
            """,
            (
                message.message_id,
                message.channel,
                message.sender,
                message.recipient,
                message.content,
                message.created_at,
                json.dumps(message.metadata, sort_keys=True),
            ),
        )

    def messages(self, channel: str | None = None) -> list[ChatMessage]:
        sql = """
            select id, channel, sender, recipient, content, created_at, metadata
            from brigade_chat_messages
        """
        params: tuple[object, ...] = ()
        if channel is not None:
            sql += " where channel = %s"
            params = (channel,)
        sql += " order by created_at, id"
        messages: list[ChatMessage] = []
        for row in self._query(sql, params):
            messages.append(
                chat_message_from_dict(
                    {
                        "message_id": row[0],
                        "channel": row[1],
                        "sender": row[2],
                        "recipient": row[3],
                        "content": row[4],
                        "created_at": _as_iso(row[5]),
                        "metadata": _decode_json(row[6]),
                    }
                )
            )
        return messages

    def add_orchestrator_reasoning(self, record: dict[str, Any]) -> None:
        self._execute(
            """
            insert into brigade_orchestrator_reasoning (
              id, cycle_at, mission_id, reasoning, decisions
            )
            values (%s, %s, %s, %s::jsonb, %s::jsonb)
            on conflict (id) do update set
              cycle_at = excluded.cycle_at,
              mission_id = excluded.mission_id,
              reasoning = excluded.reasoning,
              decisions = excluded.decisions
            """,
            (
                record["reasoning_id"],
                record.get("ended_at") or record.get("started_at") or utc_now_iso(),
                "current" if record.get("mission_statement") else None,
                json.dumps(record, sort_keys=True),
                json.dumps(record.get("assigned", []), sort_keys=True),
            ),
        )
        self._record_decision_provenance(record)

    def orchestrator_reasoning(self) -> list[dict[str, Any]]:
        return list(
            self._records(
                "select reasoning from brigade_orchestrator_reasoning order by cycle_at, id"
            )
        )

    def add_proposal(self, proposal: dict[str, Any]) -> dict[str, Any]:
        idempotency_key = proposal.get("idempotency_key")
        if idempotency_key:
            existing = self._record_or_none(
                """
                select record
                from brigade_proposals
                where idempotency_key = %s
                order by created_at desc, id desc
                limit 1
                """,
                (idempotency_key,),
            )
            if existing is not None:
                return existing
        self._upsert_proposal(proposal)
        return proposal

    def proposals(
        self,
        kind: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "select record from brigade_proposals"
        clauses: list[str] = []
        params: list[object] = []
        if kind is not None:
            clauses.append("kind = %s")
            params.append(kind)
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        if clauses:
            sql += " where " + " and ".join(clauses)
        sql += " order by created_at, id"
        return list(self._records(sql, tuple(params)))

    def find_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        return self._record_or_none(
            "select record from brigade_proposals where id = %s",
            (proposal_id,),
        )

    def update_proposal(self, proposal: dict[str, Any]) -> None:
        self._upsert_proposal(proposal)

    def _upsert_proposal(self, proposal: dict[str, Any]) -> None:
        self._execute(
            """
            insert into brigade_proposals (
              id, kind, status, agent_id, team_id, created_at, updated_at,
              idempotency_key, record
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            on conflict (id) do update set
              kind = excluded.kind,
              status = excluded.status,
              agent_id = excluded.agent_id,
              team_id = excluded.team_id,
              updated_at = excluded.updated_at,
              idempotency_key = excluded.idempotency_key,
              record = excluded.record
            """,
            (
                proposal["proposal_id"],
                proposal["kind"],
                proposal["status"],
                proposal.get("agent_id"),
                proposal.get("team_id"),
                proposal["created_at"],
                proposal.get("updated_at") or proposal["created_at"],
                proposal.get("idempotency_key"),
                json.dumps(proposal, sort_keys=True),
            ),
        )

    def add_recurrence(self, recurrence: dict[str, Any]) -> dict[str, Any]:
        self._upsert_recurrence(recurrence)
        return recurrence

    def recurrences(self, enabled: bool | None = None) -> list[dict[str, Any]]:
        sql = "select record from brigade_recurrences"
        params: tuple[object, ...] = ()
        if enabled is not None:
            sql += " where enabled = %s"
            params = (enabled,)
        sql += " order by next_due_at, id"
        return list(self._records(sql, params))

    def update_recurrence(self, recurrence: dict[str, Any]) -> None:
        self._upsert_recurrence(recurrence)

    def _upsert_recurrence(self, recurrence: dict[str, Any]) -> None:
        self._execute(
            """
            insert into brigade_recurrences (
              id, enabled, interval_seconds, next_due_at, created_at, updated_at, record
            )
            values (%s, %s, %s, %s, %s, %s, %s::jsonb)
            on conflict (id) do update set
              enabled = excluded.enabled,
              interval_seconds = excluded.interval_seconds,
              next_due_at = excluded.next_due_at,
              updated_at = excluded.updated_at,
              record = excluded.record
            """,
            (
                recurrence["recurrence_id"],
                bool(recurrence.get("enabled", True)),
                int(recurrence["interval_seconds"]),
                recurrence["next_due_at"],
                recurrence["created_at"],
                recurrence.get("updated_at") or recurrence["created_at"],
                json.dumps(recurrence, sort_keys=True),
            ),
        )

    def add_orchestrator_policy(self, policy: dict[str, Any]) -> dict[str, Any]:
        self._upsert_orchestrator_policy(policy)
        return policy

    def orchestrator_policies(
        self,
        *,
        active_only: bool = True,
        rule_kind: str | None = None,
        assignment_kind: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "select record from brigade_orchestrator_policies"
        clauses: list[str] = []
        params: list[object] = []
        if active_only:
            clauses.append("active = %s")
            params.append(True)
        if rule_kind is not None:
            clauses.append("rule_kind = %s")
            params.append(rule_kind)
        if assignment_kind is not None:
            clauses.append("assignment_kind = %s")
            params.append(assignment_kind)
        if clauses:
            sql += " where " + " and ".join(clauses)
        sql += " order by created_at, id"
        return list(self._records(sql, tuple(params)))

    def find_orchestrator_policy(self, policy_id: str) -> dict[str, Any] | None:
        return self._record_or_none(
            "select record from brigade_orchestrator_policies where id = %s",
            (policy_id,),
        )

    def update_orchestrator_policy(self, policy: dict[str, Any]) -> None:
        self._upsert_orchestrator_policy(policy)

    def _upsert_orchestrator_policy(self, policy: dict[str, Any]) -> None:
        self._execute(
            """
            insert into brigade_orchestrator_policies (
              id, rule_kind, assignment_kind, active, created_at, updated_at, record
            )
            values (%s, %s, %s, %s, %s, %s, %s::jsonb)
            on conflict (id) do update set
              rule_kind = excluded.rule_kind,
              assignment_kind = excluded.assignment_kind,
              active = excluded.active,
              updated_at = excluded.updated_at,
              record = excluded.record
            """,
            (
                policy["policy_id"],
                policy.get("rule_kind", "freeform"),
                policy.get("assignment_kind"),
                bool(policy.get("active", True)),
                policy["created_at"],
                policy.get("updated_at") or policy["created_at"],
                json.dumps(policy, sort_keys=True),
            ),
        )

    def add_usage_record(self, record: dict[str, Any]) -> None:
        self._execute(
            """
            insert into brigade_usage_records (id, assignment_id, agent_id, recorded_at, record)
            values (%s, %s, %s, %s, %s::jsonb)
            on conflict (id) do update set
              assignment_id = excluded.assignment_id,
              agent_id = excluded.agent_id,
              recorded_at = excluded.recorded_at,
              record = excluded.record
            """,
            (
                record["usage_id"],
                record.get("assignment_id"),
                record.get("agent_id"),
                record["recorded_at"],
                json.dumps(record, sort_keys=True),
            ),
        )

    def usage_records(self) -> list[dict[str, Any]]:
        return list(
            self._records("select record from brigade_usage_records order by recorded_at, id")
        )

    def set_model_inventory(self, inventory: dict[str, Any]) -> dict[str, Any]:
        providers = inventory.get("providers")
        if not isinstance(providers, dict):
            providers = {}
        updated_at = str(inventory.get("updated_at") or utc_now_iso())
        for provider, record in providers.items():
            if not isinstance(record, dict):
                continue
            probed_at = str(record.get("probed_at") or updated_at)
            status = str(record.get("status") or "unknown")
            self._execute(
                """
                insert into brigade_model_inventory (provider, probed_at, status, record)
                values (%s, %s, %s, %s::jsonb)
                on conflict (provider) do update set
                  probed_at = excluded.probed_at,
                  status = excluded.status,
                  record = excluded.record
                """,
                (str(provider), probed_at, status, json.dumps(record, sort_keys=True)),
            )
        return self.model_inventory()

    def model_inventory(self) -> dict[str, Any]:
        providers = {
            str(record.get("provider")): record
            for record in self._records(
                "select record from brigade_model_inventory order by provider"
            )
            if record.get("provider")
        }
        updated_at = None
        if providers:
            updated_at = max(str(item.get("probed_at") or "") for item in providers.values())
        return {"providers": providers, "updated_at": updated_at}

    def upsert_cloud_job(self, job: dict[str, Any]) -> None:
        self._execute(
            """
            insert into brigade_cloud_jobs (id, assignment_id, agent_id, status, updated_at, record)
            values (%s, %s, %s, %s, %s, %s::jsonb)
            on conflict (id) do update set
              assignment_id = excluded.assignment_id,
              agent_id = excluded.agent_id,
              status = excluded.status,
              updated_at = excluded.updated_at,
              record = excluded.record
            """,
            (
                job["job_id"],
                job.get("assignment_id"),
                job.get("agent_id"),
                job.get("status"),
                job.get("updated_at") or utc_now_iso(),
                json.dumps(job, sort_keys=True),
            ),
        )

    def cloud_jobs(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = "select record from brigade_cloud_jobs"
        params: tuple[object, ...] = ()
        if status is not None:
            sql += " where status = %s"
            params = (status,)
        sql += " order by updated_at, id"
        return list(self._records(sql, params))

    def set_financial_report(self, report: dict[str, Any]) -> None:
        self._execute(
            """
            insert into brigade_financial_reports (id, generated_at, record)
            values (%s, %s, %s::jsonb)
            on conflict (id) do update set
              generated_at = excluded.generated_at,
              record = excluded.record
            """,
            (
                report["report_id"],
                report["generated_at"],
                json.dumps(report, sort_keys=True),
            ),
        )

    def latest_financial_report(self) -> dict[str, Any] | None:
        return self._record_or_none(
            """
            select record
            from brigade_financial_reports
            order by generated_at desc, id desc
            limit 1
            """
        )

    def set_local_inference(self, record: dict[str, Any]) -> None:
        if self._redis.available():
            self._redis.set_json(RedisRuntimeClient.LOCAL_INFERENCE_KEY, record)
        self._set_local_inference_postgres(record)

    def _set_local_inference_postgres(self, record: dict[str, Any]) -> None:
        self._execute(
            """
            insert into brigade_local_inference_state (id, updated_at, record)
            values ('default', %s, %s::jsonb)
            on conflict (id) do update set
              updated_at = excluded.updated_at,
              record = excluded.record
            """,
            (utc_now_iso(), json.dumps(record, sort_keys=True)),
        )

    def local_inference(self) -> dict[str, Any]:
        if self._redis.available():
            redis_record = self._redis.get_json(RedisRuntimeClient.LOCAL_INFERENCE_KEY)
            if redis_record is not None:
                return redis_record
        return self._record_or_none(
            "select record from brigade_local_inference_state where id = 'default'"
        ) or dict(EMPTY_STATE["local_inference"])

    def acquire_local_inference_lock(
        self,
        holder: str,
        *,
        lock_ttl_seconds: int,
    ) -> None:
        if self._redis.available():
            self._redis.acquire_local_inference_lock(
                holder,
                lock_ttl_seconds=lock_ttl_seconds,
            )
            record = self._redis.get_json(RedisRuntimeClient.LOCAL_INFERENCE_KEY)
            if record is not None:
                self._set_local_inference_postgres(record)
            return

        with self._connect_transactional() as conn:
            with conn.cursor() as cursor:
                record = self._locked_local_inference_record(cursor)
                next_available = record.get("next_available")
                if next_available and parse_utc_iso(str(next_available)) > utc_now():
                    raise RuntimeError(f"local inference unavailable until {next_available}")
                lock_expires_at = record.get("lock_expires_at")
                if (
                    record.get("status") == "busy"
                    and record.get("holder") != holder
                    and lock_expires_at
                    and parse_utc_iso(str(lock_expires_at)) > utc_now()
                ):
                    raise RuntimeError(
                        f"local inference already held by {record.get('holder')}"
                    )
                locked_at = utc_now_iso()
                record.update(
                    {
                        "status": "busy",
                        "holder": holder,
                        "locked_at": locked_at,
                        "lock_expires_at": add_seconds_iso(locked_at, lock_ttl_seconds),
                    }
                )
                cursor.execute(
                    """
                    update brigade_local_inference_state
                    set updated_at = %s, record = %s::jsonb
                    where id = 'default'
                    """,
                    (utc_now_iso(), json.dumps(record, sort_keys=True)),
                )

    def release_local_inference_lock(
        self,
        holder: str,
        *,
        cooldown_seconds: int,
    ) -> None:
        if self._redis.available():
            record = self._redis.release_local_inference_lock(
                holder,
                cooldown_seconds=cooldown_seconds,
            )
            self._set_local_inference_postgres(record)
            return

        with self._connect_transactional() as conn:
            with conn.cursor() as cursor:
                record = self._locked_local_inference_record(cursor)
                if record.get("holder") != holder:
                    return
                completed_at = utc_now_iso()
                record = {
                    "status": "idle",
                    "holder": None,
                    "last_completed": completed_at,
                    "next_available": add_seconds_iso(completed_at, cooldown_seconds),
                }
                cursor.execute(
                    """
                    update brigade_local_inference_state
                    set updated_at = %s, record = %s::jsonb
                    where id = 'default'
                    """,
                    (utc_now_iso(), json.dumps(record, sort_keys=True)),
                )

    def add_transcript(self, transcript: dict[str, Any]) -> None:
        self._execute(
            """
            insert into brigade_transcripts_runtime (
              id, assignment_id, agent_id, created_at, record
            )
            values (%s, %s, %s, %s, %s::jsonb)
            on conflict (id) do update set
              assignment_id = excluded.assignment_id,
              agent_id = excluded.agent_id,
              created_at = excluded.created_at,
              record = excluded.record
            """,
            (
                transcript["transcript_id"],
                transcript.get("assignment_id"),
                transcript.get("agent_id"),
                transcript["created_at"],
                json.dumps(transcript, sort_keys=True),
            ),
        )

    def transcripts(self) -> list[dict[str, Any]]:
        return list(
            self._records(
                "select record from brigade_transcripts_runtime order by created_at, id"
            )
        )

    def add_episode(self, episode: dict[str, Any]) -> None:
        self._execute(
            """
            insert into brigade_episodes (id, agent_id, created_at, record)
            values (%s, %s, %s, %s::jsonb)
            on conflict (id) do update set
              agent_id = excluded.agent_id,
              created_at = excluded.created_at,
              record = excluded.record
            """,
            (
                episode["episode_id"],
                episode.get("agent_id"),
                episode["created_at"],
                json.dumps(episode, sort_keys=True),
            ),
        )
        result = self._qdrant.upsert_episode(episode)
        if self._qdrant.available() and not result.ok:
            self.add_alert(
                f"qdrant episode write failed for {episode['episode_id']}: {result.detail}"
            )

    def episodes(self) -> list[dict[str, Any]]:
        return list(self._records("select record from brigade_episodes order by created_at, id"))

    def search_episodes(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        try:
            return self._qdrant.search_episodes(query, limit=limit)
        except RuntimeError as exc:
            self._add_alert_postgres(f"qdrant episode search failed: {exc}")
            return []

    def add_provenance_record(self, record: dict[str, Any]) -> None:
        self._execute(
            """
            insert into brigade_provenance_records (id, node_id, node_type, created_at, record)
            values (%s, %s, %s, %s, %s::jsonb)
            on conflict (id) do update set
              node_id = excluded.node_id,
              node_type = excluded.node_type,
              created_at = excluded.created_at,
              record = excluded.record
            """,
            (
                record["record_id"],
                record.get("node_id"),
                record.get("node_type"),
                record["created_at"],
                json.dumps(record, sort_keys=True),
            ),
        )
        result = self._neo4j.upsert_provenance(record)
        if self._neo4j.available() and not result.ok:
            self.add_alert(
                f"neo4j provenance write failed for {record['record_id']}: {result.detail}"
            )

    def provenance_records(self) -> list[dict[str, Any]]:
        return list(
            self._records(
                "select record from brigade_provenance_records order by created_at, id"
            )
        )

    def external_datastore_status(self) -> dict[str, dict[str, object]]:
        return {
            "qdrant": self._qdrant.health().to_dict(),
            "neo4j": self._neo4j.health().to_dict(),
        }

    def add_connector_audit_event(self, record: dict[str, Any]) -> None:
        self._execute(
            """
            insert into brigade_connector_audit_events (
              id, provider, direction, status, external_user_id, conversation_id,
              external_message_id, agent_id, reason, redacted_metadata, created_at, record
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb)
            on conflict (id) do update set
              provider = excluded.provider,
              direction = excluded.direction,
              status = excluded.status,
              external_user_id = excluded.external_user_id,
              conversation_id = excluded.conversation_id,
              external_message_id = excluded.external_message_id,
              agent_id = excluded.agent_id,
              reason = excluded.reason,
              redacted_metadata = excluded.redacted_metadata,
              created_at = excluded.created_at,
              record = excluded.record
            """,
            (
                record["event_id"],
                record["provider"],
                record["direction"],
                record["status"],
                record.get("external_user_id"),
                record.get("conversation_id"),
                record.get("external_message_id"),
                record.get("agent_id"),
                record.get("reason"),
                json.dumps(record.get("redacted_metadata", {}), sort_keys=True),
                record["created_at"],
                json.dumps(record, sort_keys=True),
            ),
        )

    def connector_audit_events(
        self,
        provider: str | None = None,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        sql = "select record from brigade_connector_audit_events"
        params: tuple[object, ...] = ()
        if provider is not None:
            sql += " where provider = %s"
            params = (provider,)
        sql += " order by created_at, id"
        if limit is not None:
            sql += " limit %s"
            params = (*params, limit)
        return list(self._records(sql, params))

    def upsert_external_identity(self, record: dict[str, Any]) -> None:
        self._execute(
            """
            insert into brigade_external_identities (
              provider, external_user_id, username, status, reason, redacted_metadata,
              created_at, updated_at, decided_at, decided_by, record
            )
            values (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s::jsonb)
            on conflict (provider, external_user_id) do update set
              username = excluded.username,
              status = excluded.status,
              reason = excluded.reason,
              redacted_metadata = excluded.redacted_metadata,
              updated_at = excluded.updated_at,
              decided_at = excluded.decided_at,
              decided_by = excluded.decided_by,
              record = excluded.record
            """,
            (
                record["provider"],
                record["external_user_id"],
                record.get("username"),
                record["status"],
                record.get("reason"),
                json.dumps(record.get("redacted_metadata", {}), sort_keys=True),
                record["created_at"],
                record["updated_at"],
                record.get("decided_at"),
                record.get("decided_by"),
                json.dumps(record, sort_keys=True),
            ),
        )

    def external_identity(self, provider: str, external_user_id: str) -> dict[str, Any] | None:
        return self._record_or_none(
            """
            select record
            from brigade_external_identities
            where provider = %s and external_user_id = %s
            """,
            (provider, external_user_id),
        )

    def external_identities(
        self,
        provider: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "select record from brigade_external_identities"
        clauses: list[str] = []
        params: tuple[object, ...] = ()
        if provider is not None:
            clauses.append("provider = %s")
            params = (*params, provider)
        if status is not None:
            clauses.append("status = %s")
            params = (*params, status)
        if clauses:
            sql += " where " + " and ".join(clauses)
        sql += " order by updated_at, provider, external_user_id"
        return list(self._records(sql, params))

    def runtime_overrides(self) -> dict[str, Any]:
        if not self._redis.available():
            return {}
        try:
            return self._redis.runtime_overrides()
        except RuntimeError:
            return {}

    def set_runtime_overrides(self, overrides: dict[str, Any]) -> dict[str, Any]:
        if not self._redis.available():
            raise RuntimeError(
                "redis runtime store is required to persist runtime config overrides"
            )
        self._redis.set_runtime_overrides(overrides)
        return dict(overrides)

    def _sync_assignment_runtime(self, assignment: Assignment) -> None:
        if not self._redis.available():
            return
        try:
            self._redis.enqueue_pending_assignment(assignment)
        except RuntimeError as exc:
            self._add_alert_postgres(
                f"redis assignment queue sync failed for {assignment.assignment_id}: {exc}"
            )

    def _sync_assignments_runtime(self, assignments: list[Assignment]) -> None:
        if not self._redis.available():
            return
        try:
            self._redis.reconcile_pending_assignments(assignments)
        except RuntimeError as exc:
            self._add_alert_postgres(f"redis assignment queue reconcile failed: {exc}")

    def _remove_assignment_runtime(self, assignment_id: str) -> None:
        if not self._redis.available():
            return
        try:
            self._redis.remove_pending_assignment(assignment_id)
            self._redis.release_assignment_execution_claim(assignment_id)
        except RuntimeError as exc:
            self._add_alert_postgres(
                f"redis assignment runtime cleanup failed for {assignment_id}: {exc}"
            )

    def _record_assignment_provenance(self, assignment: Assignment) -> None:
        self.add_provenance_record(
            {
                "record_id": f"task:{assignment.assignment_id}",
                "node_type": "task",
                "node_id": assignment.assignment_id,
                "source_refs": [],
                "metadata": {
                    "assignment_id": assignment.assignment_id,
                    "assignment": assignment.assignment,
                    "assigned_to": assignment.assigned_to,
                    "status": assignment.status.value,
                    "goal_statement": assignment.goal_statement,
                    "updated_at": assignment.updated_at,
                },
                "created_at": assignment.updated_at,
            }
        )

    def _record_team_provenance(self, team: Team) -> None:
        self.add_provenance_record(
            {
                "record_id": f"team:{team.team_id}",
                "node_type": "team",
                "node_id": team.team_id,
                "source_refs": [],
                "metadata": {
                    "team_id": team.team_id,
                    "members": team.members,
                    "crew_chief_id": team.crew_chief_id,
                    "parent_team_id": team.parent_team_id,
                    "escalation_team_id": team.escalation_team_id,
                },
                "created_at": team.updated_at,
            }
        )

    def _record_decision_provenance(self, reasoning: dict[str, Any]) -> None:
        assignment_ids = [str(item) for item in reasoning.get("assigned", [])]
        if not assignment_ids:
            return
        node_id = str(reasoning.get("reasoning_id") or reasoning.get("cycle_id") or uuid4())
        self.add_provenance_record(
            {
                "record_id": f"decision:{node_id}",
                "node_type": "decision",
                "node_id": node_id,
                "source_refs": [],
                "metadata": {
                    "assignment_ids": assignment_ids,
                    "cycle_id": reasoning.get("cycle_id"),
                    "decision_summary": reasoning.get("decision_summary"),
                },
                "created_at": (
                    reasoning.get("ended_at") or reasoning.get("started_at") or utc_now_iso()
                ),
            }
        )

    def _insert_goal(self, agent_id: str, goal: Goal) -> None:
        record = goal.to_dict()
        self._execute(
            """
            insert into brigade_goals (
              id, agent_id, statement, success_criteria, explicitly_not,
              set_by, human_confirmed, set_at, engagement_mode, record
            )
            values (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                str(uuid4()),
                agent_id,
                goal.statement,
                json.dumps(goal.success_criteria),
                json.dumps(goal.explicitly_not),
                goal.set_by,
                goal.human_confirmed,
                goal.set_at,
                goal.engagement_mode,
                json.dumps(record, sort_keys=True),
            ),
        )

    def _upsert_assignment(self, assignment: Assignment, cursor=None) -> None:
        record = assignment.to_dict()
        params = (
            assignment.assignment_id,
            assignment.created_at,
            assignment.updated_at,
            assignment.created_by,
            assignment.assigned_to,
            assignment.source,
            assignment.assignment,
            assignment.work_mode.value,
            assignment.status.value,
            assignment.priority.value,
            assignment.kind.value,
            assignment.estimated_cycles,
            assignment.cycle_count,
            assignment.checkpoint_at,
            assignment.parent_assignment_id,
            json.dumps(assignment.result_artifact_ids),
            assignment.transcript_path,
            assignment.state_row_written_to,
            assignment.progress_summary,
            json.dumps(assignment.blockers),
            assignment.consecutive_failures,
            assignment.last_error,
            assignment.awaiting_human,
            assignment.last_run_provider,
            assignment.last_run_model,
            assignment.last_run_at,
            json.dumps(assignment.dependency_ids),
            assignment.goal_statement,
            assignment.assignment_rationale,
            assignment.created_by_user_id,
            assignment.created_by_role,
            assignment.idempotency_key,
            json.dumps(record, sort_keys=True),
        )
        sql = """
            insert into brigade_assignments (
              id, created_at, updated_at, created_by, assigned_to, source, assignment,
              work_mode, status, priority, kind, estimated_cycles, cycle_count, checkpoint_at,
              parent_assignment_id, result_artifact_ids, transcript_path, state_row_written_to,
              progress_summary, blockers, consecutive_failures, last_error, awaiting_human,
              last_run_provider, last_run_model, last_run_at, dependency_ids, goal_statement,
              assignment_rationale, created_by_user_id, created_by_role, idempotency_key, record
            )
            values (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s,
              %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s::jsonb
            )
            on conflict (id) do update set
              created_at = excluded.created_at,
              updated_at = excluded.updated_at,
              created_by = excluded.created_by,
              assigned_to = excluded.assigned_to,
              source = excluded.source,
              assignment = excluded.assignment,
              work_mode = excluded.work_mode,
              status = excluded.status,
              priority = excluded.priority,
              kind = excluded.kind,
              estimated_cycles = excluded.estimated_cycles,
              cycle_count = excluded.cycle_count,
              checkpoint_at = excluded.checkpoint_at,
              parent_assignment_id = excluded.parent_assignment_id,
              result_artifact_ids = excluded.result_artifact_ids,
              transcript_path = excluded.transcript_path,
              state_row_written_to = excluded.state_row_written_to,
              progress_summary = excluded.progress_summary,
              blockers = excluded.blockers,
              consecutive_failures = excluded.consecutive_failures,
              last_error = excluded.last_error,
              awaiting_human = excluded.awaiting_human,
              last_run_provider = excluded.last_run_provider,
              last_run_model = excluded.last_run_model,
              last_run_at = excluded.last_run_at,
              dependency_ids = excluded.dependency_ids,
              goal_statement = excluded.goal_statement,
              assignment_rationale = excluded.assignment_rationale,
              created_by_user_id = excluded.created_by_user_id,
              created_by_role = excluded.created_by_role,
              idempotency_key = excluded.idempotency_key,
              record = excluded.record
        """
        if cursor is None:
            self._execute(sql, params)
            return
        cursor.execute(sql, params)

    def _records(self, sql: str, params: tuple[object, ...] = ()) -> Iterable[dict[str, Any]]:
        return (_decode_json(row[0]) for row in self._query(sql, params))

    def _record_or_none(self, sql: str, params: tuple[object, ...] = ()) -> dict[str, Any] | None:
        row = self._query_one(sql, params)
        if row is None:
            return None
        return _decode_json(row[0])

    def _rows_json(self, sql: str, params: tuple[object, ...] = ()) -> Iterable[dict[str, Any]]:
        return (_decode_json(row[0]) for row in self._query(sql, params))

    def _scalar(self, sql: str, params: tuple[object, ...] = ()) -> int:
        row = self._query_one(sql, params)
        return int(row[0]) if row else 0

    def _execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)

    def _query(self, sql: str, params: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                return list(cursor.fetchall())

    def _query_one(self, sql: str, params: tuple[object, ...] = ()) -> tuple[Any, ...] | None:
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                return cursor.fetchone()

    def _locked_local_inference_record(self, cursor) -> dict[str, Any]:
        cursor.execute(
            """
            insert into brigade_local_inference_state (id, updated_at, record)
            values ('default', %s, %s::jsonb)
            on conflict (id) do nothing
            """,
            (
                utc_now_iso(),
                json.dumps(dict(EMPTY_STATE["local_inference"]), sort_keys=True),
            ),
        )
        cursor.execute(
            "select record from brigade_local_inference_state where id = 'default' for update"
        )
        row = cursor.fetchone()
        return _decode_json(row[0]) if row else dict(EMPTY_STATE["local_inference"])

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "psycopg is required to use the Postgres runtime store"
            ) from exc
        return psycopg.connect(self.dsn, autocommit=True)

    def _connect_transactional(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "psycopg is required to use the Postgres runtime store"
            ) from exc
        return psycopg.connect(self.dsn, autocommit=False)


def open_state_store(settings: Settings) -> StateStore:
    legacy_path = settings.data_dir / "state.json"
    if settings.postgres_dsn:
        ensure_schema(settings.postgres_dsn)
        return PostgresStateStore(
            settings.postgres_dsn,
            settings.data_dir,
            redis_url=settings.redis_url,
            qdrant_url=settings.qdrant_url,
            qdrant_collection=settings.qdrant_collection,
            ollama_embedding_base_url=settings.ollama_embedding_base_url,
            ollama_embedding_model=settings.ollama_embedding_model,
            ollama_embedding_vector_size=settings.ollama_embedding_vector_size,
            neo4j_http_url=settings.neo4j_http_url,
            neo4j_auth=settings.neo4j_auth,
        )
    if settings.allow_json_store:
        return JsonStateStore(legacy_path)
    raise RuntimeError(
        "Postgres is required for OpenBrigade runtime state. "
        "Set BRIGADE_POSTGRES_DSN or run through docker compose. "
        "Set BRIGADE_ALLOW_JSON_STORE=1 only for isolated unit tests."
    )


def _import_legacy_state(path: Path, store: StateStore) -> None:
    legacy = JsonStateStore(path)
    mission = legacy.mission()
    if mission:
        store.set_mission(mission)
    for user in legacy.users():
        store.add_user(user)
    for agent in legacy.agents():
        store.add_agent(agent)
    for team in legacy.teams():
        store.upsert_team(team)
    for current_agent_id, goals in legacy.goals().items():
        for goal in goals:
            store.ensure_goal(current_agent_id, goal)
    for assignment in legacy.assignments():
        store.add_assignment(assignment)
    for item in legacy.assignment_history():
        record = assignment_from_dict(item["record"])
        store.archive_assignment(record, item.get("executive_summary") or "")
    for state in legacy.agent_states().values():
        store.upsert_agent_state(state)
    for alert in legacy.alerts():
        store.add_alert(alert)
    for document in legacy.knowledge_documents():
        store.add_knowledge_document(document)
    for chunk in legacy.knowledge_chunks():
        store.add_knowledge_chunk(chunk)
    for message in legacy.messages():
        store.add_message(message)
    for reasoning in legacy.orchestrator_reasoning():
        store.add_orchestrator_reasoning(reasoning)
    for usage in legacy.usage_records():
        store.add_usage_record(usage)
    inventory = legacy.model_inventory()
    if inventory:
        store.set_model_inventory(inventory)
    for job in legacy.cloud_jobs():
        store.upsert_cloud_job(job)
    report = legacy.latest_financial_report()
    if report:
        store.set_financial_report(report)
    store.set_local_inference(legacy.local_inference())
    for transcript in legacy.transcripts():
        store.add_transcript(transcript)
    for episode in legacy.episodes():
        store.add_episode(episode)
    for record in legacy.provenance_records():
        store.add_provenance_record(record)
    for record in legacy.connector_audit_events():
        store.add_connector_audit_event(record)
    for record in legacy.external_identities():
        store.upsert_external_identity(record)


def _decode_json(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return json.loads(value)
    return dict(value)


def _as_iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _goal_identity(goal: dict[str, Any]) -> tuple[object, ...]:
    return (
        goal.get("statement"),
        tuple(goal.get("success_criteria", [])),
        tuple(goal.get("explicitly_not", [])),
        goal.get("set_by"),
        bool(goal.get("human_confirmed")),
    )


_ASSIGNMENT_EXECUTION_CLAIMS_KEY = "assignment_execution_claims"


def _execution_claims(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = record.get(_ASSIGNMENT_EXECUTION_CLAIMS_KEY)
    if not isinstance(raw, dict):
        return {}
    return {
        assignment_id: dict(claim)
        for assignment_id, claim in raw.items()
        if isinstance(assignment_id, str) and isinstance(claim, dict)
    }


def _json_try_claim_assignment_execution(
    self: JsonStateStore,
    assignment_id: str,
    owner: str,
    *,
    agent_id: str | None = None,
) -> bool:
    state = self.load()
    claims = state.setdefault(_ASSIGNMENT_EXECUTION_CLAIMS_KEY, {})
    if assignment_id in claims:
        return False
    claims[assignment_id] = {
        "assignment_id": assignment_id,
        "owner": owner,
        "agent_id": agent_id,
        "claimed_at": utc_now_iso(),
    }
    self.save(state)
    return True


def _json_assignment_execution_claim(
    self: JsonStateStore, assignment_id: str
) -> dict[str, Any] | None:
    claim = self.load().get(_ASSIGNMENT_EXECUTION_CLAIMS_KEY, {}).get(assignment_id)
    return dict(claim) if isinstance(claim, dict) else None


def _json_release_assignment_execution_claim(
    self: JsonStateStore,
    assignment_id: str,
    *,
    owner: str | None = None,
) -> bool:
    state = self.load()
    claims = state.setdefault(_ASSIGNMENT_EXECUTION_CLAIMS_KEY, {})
    claim = claims.get(assignment_id)
    if not isinstance(claim, dict):
        return False
    if owner is not None and claim.get("owner") != owner:
        return False
    claims.pop(assignment_id, None)
    self.save(state)
    return True


def _json_acquire_local_inference_lock(
    self: JsonStateStore,
    holder: str,
    *,
    lock_ttl_seconds: int,
) -> None:
    state = self.load()
    record = dict(state.get("local_inference") or EMPTY_STATE["local_inference"])
    next_available = record.get("next_available")
    if next_available and parse_utc_iso(str(next_available)) > utc_now():
        raise RuntimeError(f"local inference unavailable until {next_available}")
    lock_expires_at = record.get("lock_expires_at")
    if (
        record.get("status") == "busy"
        and record.get("holder") != holder
        and lock_expires_at
        and parse_utc_iso(str(lock_expires_at)) > utc_now()
    ):
        raise RuntimeError(f"local inference already held by {record.get('holder')}")
    locked_at = utc_now_iso()
    record.update(
        {
            "status": "busy",
            "holder": holder,
            "locked_at": locked_at,
            "lock_expires_at": add_seconds_iso(locked_at, lock_ttl_seconds),
        }
    )
    state["local_inference"] = record
    self.save(state)


def _json_release_local_inference_lock(
    self: JsonStateStore,
    holder: str,
    *,
    cooldown_seconds: int,
) -> None:
    state = self.load()
    record = dict(state.get("local_inference") or EMPTY_STATE["local_inference"])
    if record.get("holder") != holder:
        return
    completed_at = utc_now_iso()
    state["local_inference"] = {
        "status": "idle",
        "holder": None,
        "last_completed": completed_at,
        "next_available": add_seconds_iso(completed_at, cooldown_seconds),
    }
    self.save(state)


JsonStateStore.try_claim_assignment_execution = _json_try_claim_assignment_execution
JsonStateStore.assignment_execution_claim = _json_assignment_execution_claim
JsonStateStore.release_assignment_execution_claim = _json_release_assignment_execution_claim
JsonStateStore.acquire_local_inference_lock = _json_acquire_local_inference_lock
JsonStateStore.release_local_inference_lock = _json_release_local_inference_lock
