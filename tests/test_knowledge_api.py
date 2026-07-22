"""Unified knowledge-base read API (/api/knowledge/*)."""

from __future__ import annotations

import pytest

from brigade.config import Settings
from brigade.knowledge import ingest_text, store_ingest_result
from brigade.schemas import Agent
from brigade.state import JsonStateStore
from brigade.workspace import ensure_agent_workspace


def _app(tmp_path, *, require_auth: bool = False):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from brigade.web import create_app

    store = JsonStateStore(tmp_path / "state.json")
    agent = Agent("ada", "ADA", "workspace-ada")
    store.add_agent(agent)
    workspace = ensure_agent_workspace(agent, tmp_path)
    (workspace / "CHAT_MEMORY.md").write_text("- remembers the deploy runbook\n")
    memory_dir = workspace / "memory"
    memory_dir.mkdir(exist_ok=True)
    (memory_dir / "20260719-MEMORY.md").write_text("- daily note\n")
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        require_auth=require_auth,
    )
    return create_app(settings, store), store


def _ingest(store) -> dict[str, object]:
    result = ingest_text(
        title="Deploy Runbook",
        source="unit-test",
        document_type="note",
        content="# Deploy Runbook\n\n" + ("release procedure step " * 300),
        content_path="virtual://runbook",
    )
    return store_ingest_result(store, result)


def test_overview_reports_counts_and_memory(tmp_path):
    from fastapi.testclient import TestClient

    app, store = _app(tmp_path)
    _ingest(store)

    payload = TestClient(app).get("/api/knowledge/overview").json()

    assert payload["postgres"]["documents"] == 1
    assert payload["postgres"]["chunks"] >= 2
    assert payload["postgres"]["episodes"] == 1
    assert payload["postgres"]["provenance_records"] >= 3
    assert payload["qdrant"]["configured"] is False
    agents = payload["memory"]["agents"]
    assert agents[0]["agent_id"] == "ada"
    filenames = {entry["filename"] for entry in agents[0]["files"]}
    assert "CHAT_MEMORY.md" in filenames
    assert "memory/20260719-MEMORY.md" in filenames


def test_document_list_and_detail(tmp_path):
    from fastapi.testclient import TestClient

    app, store = _app(tmp_path)
    document = _ingest(store)
    client = TestClient(app)

    listing = client.get("/api/knowledge/documents").json()
    assert listing["total"] == 1
    assert listing["documents"][0]["kb_id"] == f"doc:{document['document_id']}"

    detail = client.get(f"/api/knowledge/documents/{document['document_id']}").json()
    assert detail["document"]["title"] == "Deploy Runbook"
    assert len(detail["chunks"]) >= 2
    assert detail["episode"]["document_id"] == document["document_id"]
    assert detail["vectors"]["configured"] is False

    assert client.get("/api/knowledge/documents/nope").status_code == 404


def test_graph_contains_document_chunks_and_memory_nodes(tmp_path):
    from fastapi.testclient import TestClient

    app, store = _app(tmp_path)
    document = _ingest(store)

    payload = TestClient(app).get("/api/knowledge/graph").json()

    node_ids = {node["id"] for node in payload["nodes"]}
    doc_kb = f"doc:{document['document_id']}"
    assert doc_kb in node_ids
    assert "agent:ada" in node_ids
    assert "memory:ada/CHAT_MEMORY.md" in node_ids
    rels = {(edge["rel"], edge["origin"]) for edge in payload["edges"]}
    assert ("HAS_CHUNK", "provenance") in rels
    assert ("DERIVED_FROM", "provenance") in rels
    assert ("HAS_MEMORY", "memory") in rels
    assert payload["truncated"] is False


def test_graph_ego_mode_for_one_document(tmp_path):
    from fastapi.testclient import TestClient

    app, store = _app(tmp_path)
    document = _ingest(store)

    payload = (
        TestClient(app)
        .get(f"/api/knowledge/graph?document_id={document['document_id']}")
        .json()
    )

    kinds = {node["kind"] for node in payload["nodes"]}
    assert kinds == {"document", "chunk", "episode"}
    assert all(
        edge["rel"] in {"HAS_CHUNK", "DERIVED_FROM"} for edge in payload["edges"]
    )


def test_node_inspector_dispatches_by_kind(tmp_path):
    from fastapi.testclient import TestClient

    app, store = _app(tmp_path)
    document = _ingest(store)
    client = TestClient(app)
    chunk = store.knowledge_chunks()[0]
    episode = store.episodes()[0]

    doc_payload = client.get(f"/api/knowledge/node/doc:{document['document_id']}").json()
    assert doc_payload["kind"] == "document"

    chunk_payload = client.get(f"/api/knowledge/node/chunk:{chunk['chunk_id']}").json()
    assert chunk_payload["kind"] == "chunk"
    assert chunk_payload["document"]["title"] == "Deploy Runbook"

    episode_payload = client.get(
        f"/api/knowledge/node/episode:{episode['episode_id']}"
    ).json()
    assert episode_payload["kind"] == "episode"

    memory_payload = client.get("/api/knowledge/node/memory:ada/CHAT_MEMORY.md").json()
    assert memory_payload["kind"] == "memory"
    assert "deploy runbook" in memory_payload["content"]

    agent_payload = client.get("/api/knowledge/node/agent:ada").json()
    assert agent_payload["kind"] == "agent"
    assert agent_payload["memory_files"]

    assert client.get("/api/knowledge/node/doc:nope").status_code == 404
    assert client.get("/api/knowledge/node/bogus").status_code == 400
    assert (
        client.get("/api/knowledge/node/memory:ada/../secrets.md").status_code == 400
    )


def test_search_degrades_to_keyword_mode(tmp_path):
    from fastapi.testclient import TestClient

    app, store = _app(tmp_path)
    _ingest(store)

    payload = TestClient(app).get("/api/knowledge/search?q=release procedure").json()

    assert payload["mode"] == "keyword"
    assert payload["chunks"]
    assert payload["chunks"][0]["payload"]["kb_id"].startswith("chunk:")


def test_neighbors_graceful_without_qdrant(tmp_path):
    from fastapi.testclient import TestClient

    app, store = _app(tmp_path)
    _ingest(store)
    chunk = store.knowledge_chunks()[0]

    payload = (
        TestClient(app)
        .get(f"/api/knowledge/neighbors?kb_id=chunk:{chunk['chunk_id']}")
        .json()
    )

    assert payload["edges"] == []
    assert "not configured" in payload["reason"]


def test_requires_auth_when_enabled(tmp_path):
    from fastapi.testclient import TestClient

    app, _ = _app(tmp_path, require_auth=True)

    assert TestClient(app).get("/api/knowledge/overview").status_code == 401


def test_add_document_from_pasted_text(tmp_path):
    from fastapi.testclient import TestClient

    app, store = _app(tmp_path)

    response = TestClient(app).post(
        "/api/knowledge/documents",
        json={
            "title": "Manual Note",
            "source": "operator",
            "type": "note",
            "content": "# Manual Note\n\n" + ("added by hand " * 60),
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["kb_id"].startswith("doc:")
    documents = store.knowledge_documents()
    assert any(doc["title"] == "Manual Note" for doc in documents)
    # Chunks embed at write time (no backfill needed).
    assert store.knowledge_chunks()


def test_add_document_from_html_file(tmp_path):
    import base64

    from fastapi.testclient import TestClient

    app, store = _app(tmp_path)
    html = b"<html><body><h1>Uploaded</h1><p>clean body text here</p></body></html>"

    response = TestClient(app).post(
        "/api/knowledge/documents",
        json={
            "title": "Uploaded Page",
            "type": "web",
            "filename": "page.html",
            "file_b64": base64.b64encode(html).decode("ascii"),
        },
    )

    assert response.status_code == 201
    chunk_text = " ".join(str(c.get("text") or "") for c in store.knowledge_chunks())
    assert "clean body text here" in chunk_text
    assert "<h1>" not in chunk_text  # markup stripped


def test_add_document_requires_title(tmp_path):
    from fastapi.testclient import TestClient

    app, _ = _app(tmp_path)

    response = TestClient(app).post(
        "/api/knowledge/documents", json={"content": "no title provided"}
    )
    assert response.status_code == 400


def test_observer_cannot_write_knowledge():
    from brigade.rbac import can
    from brigade.schemas import Role, User

    assert not can(User("obs", Role.OBSERVER), "knowledge:write")
    assert can(User("op", Role.OPERATOR), "knowledge:write")
