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

from brigade.kb import parse_kb_id, provenance_edges

# kb_id kind -> (Neo4j label, identifying property). Drives the generic
# relationship merge so the Neo4j mirror and the KB graph API share one edge
# definition (brigade.kb.provenance_edges).
KB_NEO4J_SCHEMA: dict[str, tuple[str, str]] = {
    "doc": ("Document", "document_id"),
    "chunk": ("Chunk", "chunk_id"),
    "agent": ("Agent", "agent_id"),
    "task": ("Task", "assignment_id"),
    "goal": ("Goal", "statement"),
    "team": ("Team", "team_id"),
    "decision": ("Decision", "decision_id"),
}

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

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            data = self._post_json("/api/embed", {"model": self.model, "input": texts})
            embeddings = data.get("embeddings")
            if isinstance(embeddings, list) and len(embeddings) == len(texts):
                return [_coerce_vector(item) for item in embeddings]
        except RuntimeError:
            pass
        return [self.embed(text) for text in texts]

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


class QdrantCollectionStore:
    """Shared Qdrant machinery for one embedded collection.

    Subclasses give the collection its payload semantics (episodes, chunks).
    """

    def __init__(
        self,
        url: str | None,
        collection: str,
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

    def _upsert_point(
        self,
        point_id: str,
        text: str,
        payload: dict[str, Any],
        *,
        detail: str,
        vector: list[float] | None = None,
    ) -> ExternalWriteResult:
        if not self.url:
            return ExternalWriteResult("qdrant", False, "not configured")
        try:
            if vector is None:
                vector = self._query_vector(text)
            self._ensure_collection(len(vector))
            self._request(
                "PUT",
                f"/collections/{self.collection}/points?wait=true",
                {
                    "points": [
                        {
                            "id": point_id,
                            "vector": vector,
                            "payload": payload,
                        }
                    ]
                },
            )
        except (KeyError, RuntimeError) as exc:
            return ExternalWriteResult("qdrant", False, str(exc))
        return ExternalWriteResult("qdrant", True, detail)

    def count(self) -> int | None:
        if not self.url:
            return None
        try:
            result = self._request(
                "POST",
                f"/collections/{self.collection}/points/count",
                {"exact": True},
            )
        except RuntimeError:
            return None
        count = ((result or {}).get("result") or {}).get("count")
        return int(count) if isinstance(count, (int, float)) else None

    def existing_ids(self, page_size: int = 256) -> set[str]:
        """All point ids in the collection (paged scroll, ids only)."""
        ids: set[str] = set()
        if not self.url:
            return ids
        offset: Any = None
        while True:
            body: dict[str, Any] = {
                "limit": page_size,
                "with_payload": False,
                "with_vector": False,
            }
            if offset is not None:
                body["offset"] = offset
            result = self._request(
                "POST", f"/collections/{self.collection}/points/scroll", body
            )
            payload = (result or {}).get("result") or {}
            points = payload.get("points") or []
            ids.update(str(point.get("id")) for point in points if isinstance(point, dict))
            offset = payload.get("next_page_offset")
            if offset is None or not points:
                return ids

    def neighbors(self, point_id: str, limit: int = 8) -> list[dict[str, Any]]:
        if not self.url:
            return []
        try:
            result = self._request(
                "POST",
                f"/collections/{self.collection}/points/recommend",
                {
                    "positive": [point_id],
                    "limit": limit,
                    "with_payload": True,
                    "with_vector": False,
                },
            )
        except RuntimeError:
            return []
        return _score_payload_rows((result or {}).get("result"))

    def drop_collection(self) -> None:
        if not self.url:
            return
        self._request("DELETE", f"/collections/{self.collection}")

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

    def search(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        if not self.url:
            return []
        vector = self._query_vector(query)
        self._ensure_collection(len(vector))
        result = self._request(
            "POST",
            f"/collections/{self.collection}/points/search",
            {"vector": vector, "limit": limit, "with_payload": True, "with_vector": False},
        )
        return _score_payload_rows((result or {}).get("result"))

    @property
    def embedding_model(self) -> str:
        if self._embedding_client is None:
            return "hash-fallback"
        return self._embedding_client.model

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


class QdrantEpisodeStore(QdrantCollectionStore):
    def __init__(
        self,
        url: str | None,
        collection: str = "brigade_episodes",
        *,
        embedding_base_url: str | None = None,
        embedding_model: str | None = None,
        embedding_vector_size: int | None = None,
    ) -> None:
        super().__init__(
            url,
            collection,
            embedding_base_url=embedding_base_url,
            embedding_model=embedding_model,
            embedding_vector_size=embedding_vector_size,
        )

    def upsert_episode(self, episode: dict[str, Any]) -> ExternalWriteResult:
        try:
            point_id = str(episode["episode_id"])
        except KeyError as exc:
            return ExternalWriteResult("qdrant", False, str(exc))
        return self._upsert_point(
            point_id,
            _episode_embedding_text(episode),
            episode,
            detail=f"upserted episode {point_id}",
        )

    def search_episodes(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        return self.search(query, limit)


class QdrantChunkStore(QdrantCollectionStore):
    def __init__(
        self,
        url: str | None,
        collection: str = "brigade_chunks",
        *,
        embedding_base_url: str | None = None,
        embedding_model: str | None = None,
        embedding_vector_size: int | None = None,
    ) -> None:
        super().__init__(
            url,
            collection,
            embedding_base_url=embedding_base_url,
            embedding_model=embedding_model,
            embedding_vector_size=embedding_vector_size,
        )

    def upsert_chunk(
        self, chunk: dict[str, Any], *, vector: list[float] | None = None
    ) -> ExternalWriteResult:
        try:
            point_id = str(chunk["chunk_id"])
            text = str(chunk["text"])
        except KeyError as exc:
            return ExternalWriteResult("qdrant", False, str(exc))
        payload = {
            "chunk_id": point_id,
            "kb_id": chunk.get("kb_id") or f"chunk:{point_id}",
            "document_id": chunk.get("document_id"),
            "chunk_index": chunk.get("chunk_index"),
            "source": chunk.get("source"),
            "text": text,
            "created_at": chunk.get("created_at"),
        }
        return self._upsert_point(
            point_id,
            text,
            payload,
            detail=f"upserted chunk {point_id}",
            vector=vector,
        )

    def search_chunks(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        return self.search(query, limit)

    def upsert_chunks(
        self, chunks: list[dict[str, Any]], *, batch_size: int = 32
    ) -> list[ExternalWriteResult]:
        results: list[ExternalWriteResult] = []
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            texts = [str(chunk.get("text") or "") for chunk in batch]
            vectors: list[list[float]]
            if self._embedding_client is not None:
                try:
                    vectors = self._embedding_client.embed_batch(texts)
                except RuntimeError as exc:
                    results.extend(
                        ExternalWriteResult("qdrant", False, str(exc)) for _ in batch
                    )
                    continue
            else:
                vectors = [_text_vector(text) for text in texts]
            for chunk, vector in zip(batch, vectors):
                results.append(self.upsert_chunk(chunk, vector=vector))
        return results


def _score_payload_rows(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, list):
        return []
    return [
        {
            "score": point.get("score"),
            "payload": point.get("payload") or {},
        }
        for point in result
        if isinstance(point, dict)
    ]


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
        if node_type == "chunk":
            self._cypher(
                """
                merge (c:Chunk {chunk_id: $chunk_id})
                set c.chunk_index = $chunk_index
                """,
                {"chunk_id": node_id, "chunk_index": metadata.get("chunk_index")},
            )
        elif node_type == "document":
            self._cypher(
                "merge (:Document {document_id: $document_id})",
                {"document_id": node_id},
            )
        elif node_type == "task":
            self._cypher(
                """
                merge (t:Task {assignment_id: $assignment_id})
                set t.status = $status,
                    t.assignment = $assignment,
                    t.updated_at = $updated_at
                """,
                {
                    "assignment_id": node_id,
                    "status": metadata.get("status"),
                    "assignment": metadata.get("assignment"),
                    "updated_at": metadata.get("updated_at"),
                },
            )
        elif node_type == "decision":
            self._cypher(
                "merge (:Decision {decision_id: $decision_id})",
                {"decision_id": node_id},
            )
        elif node_type == "team":
            self._cypher(
                "merge (:Team {team_id: $team_id})",
                {"team_id": node_id},
            )
        for edge in provenance_edges(record):
            if edge["rel"] == "DESCRIBES":
                continue
            self._merge_edge(edge)

    def _merge_edge(self, edge: dict[str, str]) -> None:
        source_kind, source_id = parse_kb_id(edge["source"])
        target_kind, target_id = parse_kb_id(edge["target"])
        source_schema = KB_NEO4J_SCHEMA.get(source_kind)
        target_schema = KB_NEO4J_SCHEMA.get(target_kind)
        if source_schema is None or target_schema is None:
            return
        source_label, source_key = source_schema
        target_label, target_key = target_schema
        # Labels/keys/rel come from fixed internal maps, never user input, so
        # interpolation is safe (Cypher cannot parameterize labels/rel types).
        self._cypher(
            f"""
            merge (s:{source_label} {{{source_key}: $source_id}})
            merge (t:{target_label} {{{target_key}: $target_id}})
            merge (s)-[:{edge["rel"]}]->(t)
            """,
            {"source_id": source_id, "target_id": target_id},
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
