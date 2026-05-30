from __future__ import annotations

import urllib.error

from brigade.datastores import (
    DEFAULT_OLLAMA_EMBEDDING_VECTOR_SIZE,
    HASH_FALLBACK_VECTOR_SIZE,
    Neo4jProvenanceStore,
    OllamaEmbeddingClient,
    QdrantEpisodeStore,
    _basic_auth,
)


def test_qdrant_episode_vectors_are_text_derived() -> None:
    store = QdrantEpisodeStore("http://qdrant")
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    def fake_request(method: str, path: str, payload: dict[str, object] | None = None):
        calls.append((method, path, payload))
        if method == "GET":
            raise RuntimeError("missing collection")
        return {}

    store._request = fake_request

    result = store.upsert_episode(
        {
            "episode_id": "episode-1",
            "summary": "Useful revenue planning note",
            "learned_facts": ["Operators prefer explicit plans"],
        }
    )

    assert result.ok
    collection_payload = calls[1][2]
    assert collection_payload["vectors"]["size"] == HASH_FALLBACK_VECTOR_SIZE
    point_payload = calls[2][2]
    vector = point_payload["points"][0]["vector"]
    assert len(vector) == HASH_FALLBACK_VECTOR_SIZE
    assert any(value != 0.0 for value in vector)


def test_qdrant_episode_vectors_use_configured_ollama_embeddings() -> None:
    store = QdrantEpisodeStore(
        "http://qdrant",
        collection="brigade_episodes_nomic_embed_text",
        embedding_base_url="http://ollama:11435",
        embedding_model="nomic-embed-text:latest",
        embedding_vector_size=DEFAULT_OLLAMA_EMBEDDING_VECTOR_SIZE,
    )
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    def fake_request(method: str, path: str, payload: dict[str, object] | None = None):
        calls.append((method, path, payload))
        if method == "GET":
            raise RuntimeError("missing collection")
        return {}

    store._request = fake_request
    store._embedding_client = type(
        "EmbeddingClient",
        (),
        {"model": "nomic-embed-text:latest", "embed": lambda self, text: [0.5] * 768},
    )()

    result = store.upsert_episode({"episode_id": "episode-1", "summary": "Useful note"})

    assert result.ok
    collection_payload = calls[1][2]
    assert collection_payload["vectors"]["size"] == 768
    point_payload = calls[2][2]
    assert point_payload["points"][0]["vector"] == [0.5] * 768


def test_qdrant_search_episodes_uses_query_vector() -> None:
    store = QdrantEpisodeStore("http://qdrant", collection="brigade_episodes")
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    def fake_request(method: str, path: str, payload: dict[str, object] | None = None):
        calls.append((method, path, payload))
        if method == "GET":
            return {"result": {"config": {"params": {"vectors": {"size": 64}}}}}
        return {
            "result": [
                {
                    "score": 0.9,
                    "payload": {"episode_id": "episode-1", "summary": "Revenue note"},
                }
            ]
        }

    store._request = fake_request

    results = store.search_episodes("revenue note", limit=1)

    assert results[0]["payload"]["episode_id"] == "episode-1"
    assert calls[-1][1] == "/collections/brigade_episodes/points/search"
    assert len(calls[-1][2]["vector"]) == HASH_FALLBACK_VECTOR_SIZE


def test_qdrant_collection_size_mismatch_is_actionable() -> None:
    store = QdrantEpisodeStore(
        "http://qdrant",
        collection="old_collection",
        embedding_base_url="http://ollama:11435",
        embedding_model="nomic-embed-text:latest",
    )

    def fake_request(method: str, path: str, payload: dict[str, object] | None = None):
        assert method == "GET"
        return {"result": {"config": {"params": {"vectors": {"size": 64}}}}}

    store._request = fake_request
    store._embedding_client = type(
        "EmbeddingClient",
        (),
        {"model": "nomic-embed-text:latest", "embed": lambda self, text: [0.5] * 768},
    )()

    result = store.upsert_episode({"episode_id": "episode-1", "summary": "Useful note"})

    assert result.ok is False
    assert "BRIGADE_QDRANT_COLLECTION" in result.detail


def test_ollama_embedding_client_parses_embed_response(monkeypatch) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return b'{"embeddings":[[1,2,3]]}'

    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: FakeResponse())

    vector = OllamaEmbeddingClient("http://ollama:11435", "nomic-embed-text:latest").embed("hello")

    assert vector == [1.0, 2.0, 3.0]


def test_neo4j_auth_accepts_compose_and_basic_forms() -> None:
    assert _basic_auth("neo4j/secret") == _basic_auth("neo4j:secret")
    assert _basic_auth("none") is None
    assert _basic_auth(None) is None


def test_neo4j_inspect_reports_actionable_auth_failures(monkeypatch) -> None:
    def raise_unauthorized(*args, **kwargs):
        raise urllib.error.HTTPError(
            url="http://neo4j/db/neo4j/tx/commit",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=None,
        )

    monkeypatch.setattr("urllib.request.urlopen", raise_unauthorized)

    payload = Neo4jProvenanceStore("http://neo4j", "neo4j/secret").inspect()

    assert payload["ok"] is False
    assert "BRIGADE_NEO4J_AUTH" in payload["reason"]
    assert "brigade_neo4j_data" in payload["reason"]


def test_neo4j_provenance_links_document_chunks_tasks_and_teams() -> None:
    store = Neo4jProvenanceStore("http://neo4j", None)
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_cypher(statement: str, parameters: dict[str, object]):
        calls.append((statement, parameters))
        return []

    store._cypher = fake_cypher

    chunk = store.upsert_provenance(
        {
            "record_id": "record-chunk",
            "node_type": "chunk",
            "node_id": "chunk-1",
            "metadata": {"document_id": "doc-1", "chunk_index": 0},
            "created_at": "2026-05-27T00:00:00Z",
        }
    )
    task = store.upsert_provenance(
        {
            "record_id": "record-task",
            "node_type": "task",
            "node_id": "assignment-1",
            "metadata": {
                "assigned_to": "sage",
                "goal_statement": "ship v0.9",
                "status": "assigned",
            },
            "created_at": "2026-05-27T00:00:00Z",
        }
    )
    team = store.upsert_provenance(
        {
            "record_id": "record-team",
            "node_type": "team",
            "node_id": "ops",
            "metadata": {"members": ["sage"], "crew_chief_id": "chief"},
            "created_at": "2026-05-27T00:00:00Z",
        }
    )

    statements = "\n".join(statement for statement, _ in calls)
    assert chunk.ok and task.ok and team.ok
    assert "HAS_CHUNK" in statements
    assert "ASSIGNED_TO" in statements
    assert "SUPPORTS_GOAL" in statements
    assert "HAS_MEMBER" in statements
    assert "LED_BY" in statements
