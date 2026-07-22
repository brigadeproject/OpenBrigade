from brigade.kb import make_kb_id, parse_kb_id, provenance_edges
from brigade.knowledge import extract_document_text, html_to_text, ingest_text

import pytest


def test_html_to_text_strips_markup() -> None:
    html = (
        "<html><head><style>.x{color:red}</style>"
        "<script>var a=1;</script></head><body>"
        "<h1>Deploy Runbook</h1><p>Step one: build the image.</p></body></html>"
    )
    text = html_to_text(html)
    assert "Deploy Runbook" in text
    assert "Step one: build the image." in text
    assert "<h1>" not in text
    assert "var a=1" not in text  # script contents dropped


def test_extract_document_text_by_extension() -> None:
    assert extract_document_text("note.txt", b"plain text body") == "plain text body"
    md = extract_document_text("readme.md", b"# Title\nbody")
    assert md.startswith("# Title")
    html = extract_document_text("page.html", b"<p>hello world</p>")
    assert "hello world" in html
    with pytest.raises(ValueError):
        extract_document_text("archive.zip", b"PK\x03\x04")


def test_kb_id_round_trip() -> None:
    kb_id = make_kb_id("memory", "sage", "CHAT_MEMORY.md")
    assert kb_id == "memory:sage/CHAT_MEMORY.md"
    assert parse_kb_id(kb_id) == ("memory", "sage/CHAT_MEMORY.md")
    assert parse_kb_id("goal:ship v0.9: fast") == ("goal", "ship v0.9: fast")


def test_kb_id_rejects_unknown_kind_and_empty() -> None:
    with pytest.raises(ValueError):
        make_kb_id("mystery", "x")
    with pytest.raises(ValueError):
        parse_kb_id("doc")
    with pytest.raises(ValueError):
        parse_kb_id("mystery:x")


def test_provenance_edges_chunk() -> None:
    edges = provenance_edges(
        {
            "record_id": "rec-1",
            "node_type": "chunk",
            "node_id": "chunk-1",
            "metadata": {"document_id": "doc-1", "chunk_index": 0},
        }
    )
    assert {"source": "prov:rec-1", "rel": "DESCRIBES", "target": "chunk:chunk-1"} in edges
    assert {"source": "doc:doc-1", "rel": "HAS_CHUNK", "target": "chunk:chunk-1"} in edges


def test_provenance_edges_task() -> None:
    edges = provenance_edges(
        {
            "record_id": "rec-2",
            "node_type": "task",
            "node_id": "assignment-1",
            "metadata": {"assigned_to": "sage", "goal_statement": "ship v0.9"},
        }
    )
    rels = {(edge["source"], edge["rel"], edge["target"]) for edge in edges}
    assert ("task:assignment-1", "ASSIGNED_TO", "agent:sage") in rels
    assert ("task:assignment-1", "SUPPORTS_GOAL", "goal:ship v0.9") in rels


def test_provenance_edges_decision_and_team() -> None:
    decision = provenance_edges(
        {
            "record_id": "rec-3",
            "node_type": "decision",
            "node_id": "decision-1",
            "metadata": {"assignment_ids": ["a-1", "a-2"]},
        }
    )
    rels = {(edge["rel"], edge["target"]) for edge in decision}
    assert ("CREATED_ASSIGNMENT", "task:a-1") in rels
    assert ("CREATED_ASSIGNMENT", "task:a-2") in rels

    team = provenance_edges(
        {
            "record_id": "rec-4",
            "node_type": "team",
            "node_id": "ops",
            "metadata": {
                "members": ["sage"],
                "crew_chief_id": "chief",
                "parent_team_id": "root",
            },
        }
    )
    rels = {(edge["rel"], edge["target"]) for edge in team}
    assert ("HAS_MEMBER", "agent:sage") in rels
    assert ("LED_BY", "agent:chief") in rels
    assert ("CHILD_OF", "team:root") in rels


def test_ingest_text_propagates_document_id_everywhere() -> None:
    document, chunks, episode, provenance = ingest_text(
        title="Test Doc",
        source="unit-test",
        document_type="note",
        content="# Test Doc\n\n" + ("body text " * 400),
        content_path="virtual://test",
        extra_metadata={"content_type": "md"},
    )
    doc_id = document.document_id
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk["document_id"] == doc_id
        assert chunk["kb_id"] == f"chunk:{chunk['chunk_id']}"
    assert episode["document_id"] == doc_id
    assert episode["source_id"] == doc_id
    assert episode["kb_id"] == f"episode:{episode['episode_id']}"
    doc_records = [r for r in provenance if r["node_type"] == "document"]
    assert doc_records and doc_records[0]["metadata"]["document_id"] == doc_id
    assert doc_records[0]["metadata"]["kb_id"] == f"doc:{doc_id}"
    chunk_records = [r for r in provenance if r["node_type"] == "chunk"]
    assert len(chunk_records) == len(chunks)
    for record in chunk_records:
        assert record["metadata"]["document_id"] == doc_id
    assert document.metadata["content_type"] == "md"


def test_web_chunk_expiry_rules() -> None:
    from brigade.knowledge import web_chunk_expired

    old = {"document_type": "web", "created_at": "2026-01-01T00:00:00+00:00"}
    fresh = {"document_type": "web", "created_at": "2026-07-19T00:00:00+00:00"}
    local_doc = {"document_type": "note", "created_at": "2020-01-01T00:00:00+00:00"}
    untyped = {"created_at": "2020-01-01T00:00:00+00:00"}

    assert web_chunk_expired(old, 30) is True
    assert web_chunk_expired(fresh, 30) is False
    assert web_chunk_expired(old, 0) is False  # TTL disabled
    assert web_chunk_expired(local_doc, 30) is False  # non-web never expires
    assert web_chunk_expired(untyped, 30) is False  # pre-1.2 chunks stay
    assert web_chunk_expired({"document_type": "web", "created_at": "bogus"}, 30) is False


def test_active_knowledge_chunks_filters_expired_web(tmp_path) -> None:
    from brigade.knowledge import active_knowledge_chunks
    from brigade.state import JsonStateStore

    store = JsonStateStore(tmp_path / "state.json")
    store.add_knowledge_chunk(
        {
            "chunk_id": "old-web",
            "document_id": "doc-web",
            "document_type": "web",
            "chunk_index": 0,
            "text": "outdated web text",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )
    store.add_knowledge_chunk(
        {
            "chunk_id": "local",
            "document_id": "doc-local",
            "document_type": "note",
            "chunk_index": 0,
            "text": "durable local text",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )

    # TTL off: everything visible.
    assert {c["chunk_id"] for c in active_knowledge_chunks(store)} == {"old-web", "local"}

    store.set_runtime_overrides({"web_knowledge_max_age_days": 30})
    assert {c["chunk_id"] for c in active_knowledge_chunks(store)} == {"local"}
    assert all(
        row["payload"]["chunk_id"] != "old-web"
        for row in store.search_chunks("text", limit=10)
    )
