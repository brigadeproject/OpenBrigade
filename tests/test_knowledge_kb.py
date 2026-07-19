from brigade.kb import make_kb_id, parse_kb_id, provenance_edges
from brigade.knowledge import ingest_text

import pytest


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
