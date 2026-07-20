from __future__ import annotations

import io

import brigade.tools as tools
from brigade.state import JsonStateStore
from brigade.tools import ToolContext, _web_fetch


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self) -> str:
        return "https://example.com/final"


class _FakeOpener:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def open(self, request, timeout=0):
        return _FakeResponse(self._body)


def _context(tmp_path) -> ToolContext:
    store = JsonStateStore(tmp_path / "state.json")
    store.data_dir = tmp_path / ".brigade"
    return ToolContext(agent=None, assignment=None, store=store)


def _patch_network(monkeypatch, body: bytes) -> None:
    monkeypatch.setattr(
        tools.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(
        tools.urllib.request, "build_opener", lambda *handlers: _FakeOpener(body)
    )


def test_web_fetch_save_creates_knowledge_records(tmp_path, monkeypatch):
    context = _context(tmp_path)
    _patch_network(monkeypatch, b"# Release notes\n\n" + b"useful text " * 200)

    result = _web_fetch(
        context, {"url": "https://example.com/notes", "save_to_knowledge": True}
    )

    assert result.ok
    assert result.metadata["knowledge_save"] == "saved"
    document_id = result.metadata["saved_document_id"]
    documents = context.store.knowledge_documents()
    assert len(documents) == 1
    document = documents[0]
    assert document["document_id"] == document_id
    assert document["document_type"] == "web"
    assert document["metadata"]["source_url"] == "https://example.com/notes"
    assert document["metadata"]["http_final_url"] == "https://example.com/final"
    assert document["metadata"]["content_hash"]
    chunks = context.store.knowledge_chunks()
    assert chunks and all(chunk["document_id"] == document_id for chunk in chunks)
    episodes = context.store.episodes()
    assert any(episode.get("document_id") == document_id for episode in episodes)
    provenance = context.store.provenance_records()
    assert any(
        record["node_type"] == "document" and record["node_id"] == document_id
        for record in provenance
    )


def test_web_fetch_save_dedupes_by_url_and_hash(tmp_path, monkeypatch):
    context = _context(tmp_path)
    _patch_network(monkeypatch, b"stable page body " * 100)
    arguments = {"url": "https://example.com/notes", "save_to_knowledge": True}

    first = _web_fetch(context, arguments)
    second = _web_fetch(context, arguments)

    assert first.metadata["knowledge_save"] == "saved"
    assert second.metadata["knowledge_save"] == "skipped-duplicate"
    assert second.metadata["saved_document_id"] == first.metadata["saved_document_id"]
    assert len(context.store.knowledge_documents()) == 1


def test_web_fetch_without_flag_saves_nothing(tmp_path, monkeypatch):
    context = _context(tmp_path)
    _patch_network(monkeypatch, b"ordinary page body " * 100)

    result = _web_fetch(context, {"url": "https://example.com/notes"})

    assert result.ok
    assert "knowledge_save" not in (result.metadata or {})
    assert context.store.knowledge_documents() == []


def test_web_fetch_autosave_runtime_override(tmp_path, monkeypatch):
    context = _context(tmp_path)
    monkeypatch.setattr(
        context.store, "runtime_overrides", lambda: {"web_fetch_autosave": True}
    )
    _patch_network(monkeypatch, b"long page body " * 100)

    result = _web_fetch(context, {"url": "https://example.com/notes"})

    assert result.metadata["knowledge_save"] == "saved"
    assert len(context.store.knowledge_documents()) == 1


def test_web_fetch_autosave_skips_short_pages(tmp_path, monkeypatch):
    context = _context(tmp_path)
    monkeypatch.setattr(
        context.store, "runtime_overrides", lambda: {"web_fetch_autosave": True}
    )
    _patch_network(monkeypatch, b"tiny")

    result = _web_fetch(context, {"url": "https://example.com/notes"})

    assert result.ok
    assert "knowledge_save" not in (result.metadata or {})
    assert context.store.knowledge_documents() == []


def test_web_fetch_refetch_supersedes_old_version(tmp_path, monkeypatch):
    context = _context(tmp_path)
    _patch_network(monkeypatch, b"version one body " * 100)
    arguments = {"url": "https://example.com/notes", "save_to_knowledge": True}

    first = _web_fetch(context, arguments)
    _patch_network(monkeypatch, b"version two body " * 100)
    second = _web_fetch(context, arguments)

    old_id = first.metadata["saved_document_id"]
    new_id = second.metadata["saved_document_id"]
    assert second.metadata["knowledge_save"] == "saved"
    assert second.metadata["superseded_documents"] == [old_id]
    documents = {
        doc["document_id"]: doc for doc in context.store.knowledge_documents()
    }
    assert documents[old_id]["metadata"]["superseded_by"] == new_id
    assert documents[old_id]["metadata"]["superseded_at"]
    assert "superseded_by" not in (documents[new_id]["metadata"] or {})
    # Old chunks are retired; only the new version is retrievable.
    assert context.store.knowledge_chunks(old_id) == []
    assert context.store.knowledge_chunks(new_id)
    hits = context.store.search_chunks("version body", limit=10)
    assert hits and all(
        row["payload"]["document_id"] == new_id for row in hits
    )


def test_web_fetch_third_version_supersedes_second_only(tmp_path, monkeypatch):
    context = _context(tmp_path)
    arguments = {"url": "https://example.com/notes", "save_to_knowledge": True}
    for body in (b"one " * 200, b"two " * 200, b"three " * 200):
        _patch_network(monkeypatch, body)
        result = _web_fetch(context, arguments)
    final_id = result.metadata["saved_document_id"]

    live = [
        doc
        for doc in context.store.knowledge_documents()
        if not (doc["metadata"] or {}).get("superseded_by")
    ]
    assert [doc["document_id"] for doc in live] == [final_id]
    for doc in context.store.knowledge_documents():
        if doc["document_id"] != final_id:
            assert context.store.knowledge_chunks(doc["document_id"]) == []
