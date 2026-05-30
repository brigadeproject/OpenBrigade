from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

HASH_FALLBACK_VECTOR_SIZE = 64
DEFAULT_OLLAMA_EMBEDDING_VECTOR_SIZE = 768


@dataclass(frozen=True)
class ExternalWriteResult:
    backend: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


class OllamaEmbeddingClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout_seconds: int = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds

    def embed(self, text: str) -> list[float]:
        payload = {"model": self.model, "input": text}
        try:
            data = self._post_json("/api/embed", payload)
            embeddings = data.get("embeddings")
            if isinstance(embeddings, list) and embeddings:
                return _coerce_vector(embeddings[0])
        except RuntimeError:
            pass
        data = self._post_json("/api/embeddings", {"model": self.model, "prompt": text})
        return _coerce_vector(data.get("embedding"))

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise RuntimeError(f"ollama embedding request failed: {exc}") from exc
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"ollama embedding response was not JSON: {exc.msg}") from exc
        if not isinstance(decoded, dict):
            raise RuntimeError("ollama embedding response was not an object")
        return decoded


class QdrantEpisodeStore:
    def __init__(
        self,
        url: str | None,
        collection: str = "brigade_episodes",
        *,
        embedding_base_url: str | None = None,
        embedding_model: str | None = None,
        embedding_vector_size: int | None = None,
    ) -> None:
        self.url = url.rstrip("/") if url else None
        self.collection = collection
        self.embedding_vector_size = embedding_vector_size
        self._embedding_client = (
            OllamaEmbeddingClient(embedding_base_url, embedding_model)
            if embedding_base_url and embedding_model
            else None
        )

    def available(self) -> bool:
        return bool(self.url)

    def upsert_episode(self, episode: dict[str, Any]) -> ExternalWriteResult:
        if not self.url:
            return ExternalWriteResult("qdrant", False, "not configured")
        try:
            vector = self._episode_vector(episode)
            self._ensure_collection(len(vector))
            point_id = str(episode["episode_id"])
            self._request(
                "PUT",
                f"/collections/{self.collection}/points?wait=true",
                {
                    "points": [
                        {
                            "id": point_id,
                            "vector": vector,
                            "payload": episode,
                        }
                    ]
                },
            )
        except (KeyError, RuntimeError) as exc:
            return ExternalWriteResult("qdrant", False, str(exc))
        return ExternalWriteResult("qdrant", True, f"upserted episode {episode['episode_id']}")

    def health(self) -> ExternalWriteResult:
        if not self.url:
            return ExternalWriteResult("qdrant", False, "not configured")
        try:
            self._request("GET", "/collections")
        except RuntimeError as exc:
            return ExternalWriteResult("qdrant", False, str(exc))
        return ExternalWriteResult("qdrant", True, "reachable")

    def inspect(self, limit: int = 10) -> dict[str, Any]:
        if not self.url:
            return {"backend": "qdrant", "ok": False, "reason": "not configured"}
        try:
            self._ensure_collection(self._configured_vector_size())
            result = self._request(
                "POST",
                f"/collections/{self.collection}/points/scroll",
                {"limit": limit, "with_payload": True, "with_vector": False},
            )
        except RuntimeError as exc:
            return {"backend": "qdrant", "ok": False, "reason": str(exc)}
        points = ((result or {}).get("result") or {}).get("points") or []
        source_kinds: dict[str, int] = {}
        for point in points:
            payload = point.get("payload") or {}
            source_kind = str(payload.get("source_kind") or payload.get("source") or "unknown")
            source_kinds[source_kind] = source_kinds.get(source_kind, 0) + 1
        return {
            "backend": "qdrant",
            "ok": True,
            "collection": self.collection,
            "embedding_model": self.embedding_model,
            "sample_count": len(points),
            "source_kinds": source_kinds,
            "points": points,
        }

    def search_episodes(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        if not self.url:
            return []
        vector = self._query_vector(query)
        self._ensure_collection(len(vector))
        result = self._request(
            "POST",
            f"/collections/{self.collection}/points/search",
            {"vector": vector, "limit": limit, "with_payload": True, "with_vector": False},
        )
        points = (result or {}).get("result") or []
        if not isinstance(points, list):
            return []
        return [
            {
                "score": point.get("score"),
                "payload": point.get("payload") or {},
            }
            for point in points
            if isinstance(point, dict)
        ]

    @property
    def embedding_model(self) -> str:
        if self._embedding_client is None:
            return "hash-fallback"
        return self._embedding_client.model

    def _episode_vector(self, episode: dict[str, Any]) -> list[float]:
        text = _episode_embedding_text(episode)
        if self._embedding_client is None:
            return _text_vector(text)
        vector = self._embedding_client.embed(text)
        if not vector:
            raise RuntimeError("ollama embedding response was empty")
        return vector

    def _query_vector(self, query: str) -> list[float]:
        if self._embedding_client is None:
            return _text_vector(query)
        vector = self._embedding_client.embed(query)
        if not vector:
            raise RuntimeError("ollama embedding response was empty")
        return vector

    def _configured_vector_size(self) -> int:
        if self.embedding_vector_size:
            return self.embedding_vector_size
        if self._embedding_client is not None:
            return DEFAULT_OLLAMA_EMBEDDING_VECTOR_SIZE
        return HASH_FALLBACK_VECTOR_SIZE

    def _ensure_collection(self, vector_size: int | None = None) -> None:
        vector_size = vector_size or self._configured_vector_size()
        try:
            result = self._request("GET", f"/collections/{self.collection}")
        except RuntimeError:
            self._request(
                "PUT",
                f"/collections/{self.collection}",
                {"vectors": {"size": vector_size, "distance": "Cosine"}},
            )
            return
        existing_size = _qdrant_vector_size(result)
        if existing_size is not None and existing_size != vector_size:
            raise RuntimeError(
                f"qdrant collection {self.collection} has vector size {existing_size}, "
                f"but {self.embedding_model} produces {vector_size}; set "
                "BRIGADE_QDRANT_COLLECTION to a new collection or recreate the old one"
            )

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                body = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise RuntimeError(f"qdrant request failed: {exc}") from exc
        return json.loads(body) if body else None


def _episode_embedding_text(episode: dict[str, Any]) -> str:
    parts = [
        str(episode.get("summary") or ""),
        str(episode.get("request") or ""),
        str(episode.get("response") or ""),
    ]
    learned = episode.get("learned_facts") or []
    if isinstance(learned, list):
        parts.extend(str(item) for item in learned)
    open_threads = episode.get("open_threads") or []
    if isinstance(open_threads, list):
        parts.extend(str(item) for item in open_threads)
    return "\n".join(part for part in parts if part.strip())


def _text_vector(text: str) -> list[float]:
    vector = [0.0] * HASH_FALLBACK_VECTOR_SIZE
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    if not tokens:
        vector[0] = 1.0
        return vector
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % HASH_FALLBACK_VECTOR_SIZE
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[bucket] += sign
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [round(value / norm, 6) for value in vector]


def _coerce_vector(value: Any) -> list[float]:
    if not isinstance(value, list):
        raise RuntimeError("ollama embedding response did not include a vector")
    vector: list[float] = []
    for item in value:
        try:
            vector.append(float(item))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("ollama embedding vector contained a non-numeric value") from exc
    return vector


def _qdrant_vector_size(collection_response: Any) -> int | None:
    if not isinstance(collection_response, dict):
        return None
    result = collection_response.get("result")
    if not isinstance(result, dict):
        return None
    config = result.get("config")
    if not isinstance(config, dict):
        return None
    params = config.get("params")
    if not isinstance(params, dict):
        return None
    vectors = params.get("vectors")
    if isinstance(vectors, dict):
        size = vectors.get("size")
        if isinstance(size, int):
            return size
        default = vectors.get("default")
        if isinstance(default, dict) and isinstance(default.get("size"), int):
            return default["size"]
    return None


class Neo4jProvenanceStore:
    def __init__(self, http_url: str | None, auth: str | None) -> None:
        self.http_url = http_url.rstrip("/") if http_url else None
        self.auth = auth

    def available(self) -> bool:
        return bool(self.http_url)

    def upsert_provenance(self, record: dict[str, Any]) -> ExternalWriteResult:
        if not self.http_url:
            return ExternalWriteResult("neo4j", False, "not configured")
        try:
            self._cypher(
                """
                merge (p:ProvenanceRecord {record_id: $record_id})
                set p.node_id = $node_id,
                    p.node_type = $node_type,
                    p.created_at = $created_at,
                    p.record = $record
                """,
                {
                    "record_id": str(record["record_id"]),
                    "node_id": record.get("node_id"),
                    "node_type": record.get("node_type"),
                    "created_at": record.get("created_at"),
                    "record": json.dumps(record, sort_keys=True),
                },
            )
            self._upsert_relationships(record)
        except (KeyError, RuntimeError) as exc:
            return ExternalWriteResult("neo4j", False, str(exc))
        return ExternalWriteResult("neo4j", True, f"upserted provenance {record['record_id']}")

    def health(self) -> ExternalWriteResult:
        if not self.http_url:
            return ExternalWriteResult("neo4j", False, "not configured")
        try:
            self._cypher("return 1 as ok", {})
        except RuntimeError as exc:
            return ExternalWriteResult("neo4j", False, str(exc))
        return ExternalWriteResult("neo4j", True, "reachable")

    def inspect(self, limit: int = 10) -> dict[str, Any]:
        if not self.http_url:
            return {"backend": "neo4j", "ok": False, "reason": "not configured"}
        try:
            results = self._cypher(
                """
                match (p:ProvenanceRecord)
                return p.record_id as record_id, p.node_type as node_type, p.node_id as node_id
                order by p.created_at desc
                limit $limit
                """,
                {"limit": limit},
            )
        except RuntimeError as exc:
            return {"backend": "neo4j", "ok": False, "reason": str(exc)}
        rows = []
        if results:
            columns = results[0].get("columns") or []
            for row in results[0].get("data") or []:
                values = row.get("row") or []
                rows.append(dict(zip(columns, values, strict=False)))
        relationship_count = self._count_relationships()
        relationships = self._sample_relationships(limit)
        return {
            "backend": "neo4j",
            "ok": True,
            "sample_count": len(rows),
            "relationship_count": relationship_count,
            "records": rows,
            "relationships": relationships,
        }

    def _upsert_relationships(self, record: dict[str, Any]) -> None:
        node_type = str(record.get("node_type") or "")
        node_id = str(record.get("node_id") or "")
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        self._cypher(
            """
            merge (n:ProvenanceNode {node_id: $node_id, node_type: $node_type})
            set n.metadata = $metadata,
                n.updated_at = $created_at
            with n
            match (p:ProvenanceRecord {record_id: $record_id})
            merge (p)-[:DESCRIBES]->(n)
            """,
            {
                "record_id": str(record.get("record_id")),
                "node_id": node_id,
                "node_type": node_type,
                "metadata": json.dumps(metadata, sort_keys=True),
                "created_at": record.get("created_at"),
            },
        )
        if node_type == "chunk" and metadata.get("document_id"):
            self._link_document_chunk(
                document_id=str(metadata["document_id"]),
                chunk_id=node_id,
                chunk_index=metadata.get("chunk_index"),
            )
        elif node_type == "document":
            self._cypher(
                "merge (:Document {document_id: $document_id})",
                {"document_id": node_id},
            )
        elif node_type == "task":
            self._link_task_record(node_id, metadata)
        elif node_type == "decision":
            self._link_decision_record(node_id, metadata)
        elif node_type == "team":
            self._link_team_record(node_id, metadata)

    def _link_document_chunk(
        self,
        *,
        document_id: str,
        chunk_id: str,
        chunk_index: object | None,
    ) -> None:
        self._cypher(
            """
            merge (d:Document {document_id: $document_id})
            merge (c:Chunk {chunk_id: $chunk_id})
            set c.chunk_index = $chunk_index
            merge (d)-[:HAS_CHUNK]->(c)
            """,
            {
                "document_id": document_id,
                "chunk_id": chunk_id,
                "chunk_index": chunk_index,
            },
        )

    def _link_task_record(self, assignment_id: str, metadata: dict[str, Any]) -> None:
        self._cypher(
            """
            merge (t:Task {assignment_id: $assignment_id})
            set t.status = $status,
                t.assignment = $assignment,
                t.updated_at = $updated_at
            """,
            {
                "assignment_id": assignment_id,
                "status": metadata.get("status"),
                "assignment": metadata.get("assignment"),
                "updated_at": metadata.get("updated_at"),
            },
        )
        agent_id = metadata.get("assigned_to")
        if agent_id:
            self._cypher(
                """
                merge (t:Task {assignment_id: $assignment_id})
                merge (a:Agent {agent_id: $agent_id})
                merge (t)-[:ASSIGNED_TO]->(a)
                """,
                {"assignment_id": assignment_id, "agent_id": str(agent_id)},
            )
        goal_statement = metadata.get("goal_statement")
        if goal_statement:
            self._cypher(
                """
                merge (t:Task {assignment_id: $assignment_id})
                merge (g:Goal {statement: $goal_statement})
                merge (t)-[:SUPPORTS_GOAL]->(g)
                """,
                {
                    "assignment_id": assignment_id,
                    "goal_statement": str(goal_statement),
                },
            )

    def _link_decision_record(self, decision_id: str, metadata: dict[str, Any]) -> None:
        self._cypher(
            "merge (:Decision {decision_id: $decision_id})",
            {"decision_id": decision_id},
        )
        for assignment_id in metadata.get("assignment_ids") or []:
            self._cypher(
                """
                merge (d:Decision {decision_id: $decision_id})
                merge (t:Task {assignment_id: $assignment_id})
                merge (d)-[:CREATED_ASSIGNMENT]->(t)
                """,
                {
                    "decision_id": decision_id,
                    "assignment_id": str(assignment_id),
                },
            )

    def _link_team_record(self, team_id: str, metadata: dict[str, Any]) -> None:
        self._cypher(
            "merge (:Team {team_id: $team_id})",
            {"team_id": team_id},
        )
        for member_id in metadata.get("members") or []:
            self._cypher(
                """
                merge (t:Team {team_id: $team_id})
                merge (a:Agent {agent_id: $agent_id})
                merge (t)-[:HAS_MEMBER]->(a)
                """,
                {"team_id": team_id, "agent_id": str(member_id)},
            )
        if metadata.get("crew_chief_id"):
            self._cypher(
                """
                merge (t:Team {team_id: $team_id})
                merge (a:Agent {agent_id: $agent_id})
                merge (t)-[:LED_BY]->(a)
                """,
                {"team_id": team_id, "agent_id": str(metadata["crew_chief_id"])},
            )
        if metadata.get("parent_team_id"):
            self._cypher(
                """
                merge (child:Team {team_id: $team_id})
                merge (parent:Team {team_id: $parent_team_id})
                merge (child)-[:CHILD_OF]->(parent)
                """,
                {"team_id": team_id, "parent_team_id": str(metadata["parent_team_id"])},
            )

    def _count_relationships(self) -> int:
        results = self._cypher("match ()-[r]->() return count(r) as count", {})
        if not results:
            return 0
        rows = results[0].get("data") or []
        if not rows:
            return 0
        return int((rows[0].get("row") or [0])[0] or 0)

    def _sample_relationships(self, limit: int) -> list[dict[str, Any]]:
        results = self._cypher(
            """
            match (a)-[r]->(b)
            return labels(a) as from_labels,
                   coalesce(a.record_id, a.node_id, a.document_id, a.chunk_id,
                            a.assignment_id, a.agent_id, a.team_id, a.decision_id,
                            a.statement) as from_id,
                   type(r) as relationship,
                   labels(b) as to_labels,
                   coalesce(b.record_id, b.node_id, b.document_id, b.chunk_id,
                            b.assignment_id, b.agent_id, b.team_id, b.decision_id,
                            b.statement) as to_id
            limit $limit
            """,
            {"limit": limit},
        )
        if not results:
            return []
        columns = results[0].get("columns") or []
        rows = []
        for row in results[0].get("data") or []:
            rows.append(dict(zip(columns, row.get("row") or [], strict=False)))
        return rows

    def _cypher(self, statement: str, parameters: dict[str, Any]) -> Any:
        payload = {"statements": [{"statement": statement, "parameters": parameters}]}
        headers = {"Content-Type": "application/json"}
        authorization = _basic_auth(self.auth)
        if authorization:
            headers["Authorization"] = f"Basic {authorization}"
        request = urllib.request.Request(
            f"{self.http_url}/db/neo4j/tx/commit",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = f"neo4j request failed: HTTP Error {exc.code}: {exc.reason}"
            if exc.code in {401, 403}:
                detail += (
                    "; check BRIGADE_NEO4J_AUTH and recreate brigade_neo4j_data if the "
                    "password changed after the volume was initialized"
                )
            raise RuntimeError(detail) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"neo4j request failed: {exc}") from exc
        errors = body.get("errors") or []
        if errors:
            raise RuntimeError(f"neo4j returned errors: {errors}")
        return body.get("results")


def _basic_auth(value: str | None) -> str | None:
    if not value or value.strip().lower() == "none":
        return None
    normalized = value.strip()
    if "/" in normalized:
        username, password = normalized.split("/", 1)
    elif ":" in normalized:
        username, password = normalized.split(":", 1)
    else:
        raise RuntimeError(
            "invalid BRIGADE_NEO4J_AUTH; expected 'username/password', 'username:password', "
            "or 'none'"
        )
    if not username or not password:
        raise RuntimeError("invalid BRIGADE_NEO4J_AUTH; username and password are required")
    return base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
