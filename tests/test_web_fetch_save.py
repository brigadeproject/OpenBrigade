from __future__ import annotations

import io
from pathlib import Path

import brigade.tools as tools
from brigade.mcp_client import MCPCallResult, MCPServerConfig, MCPTool
from brigade.schemas import Agent, Team
from brigade.state import JsonStateStore
from brigade.tools import (
    ToolContext,
    _browser_extract,
    _browser_open,
    _web_fetch,
    _web_search,
    default_tool_registry,
)


class _FakeResponse(io.BytesIO):
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self) -> str:
        return "https://example.com/final"


class _FakeOpener:
    def __init__(
        self,
        body: bytes,
        final_url: str = "https://example.com/final",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._body = body
        self._final_url = final_url
        self._headers = headers or {}

    def open(self, request, timeout=0):
        response = _FakeResponse(self._body)
        response.geturl = lambda: self._final_url
        response.headers = self._headers
        return response


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


def _patch_search(monkeypatch, html: str) -> None:
    monkeypatch.setattr(
        tools.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("40.89.244.232", 443))],
    )
    monkeypatch.setattr(
        tools.urllib.request,
        "build_opener",
        lambda *handlers: _FakeOpener(
            html.encode("utf-8"), "https://duckduckgo.com/html/?q=test"
        ),
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
    assert document["metadata"]["source_map"]["retrieval_tool"] == "web_fetch"
    assert document["metadata"]["source_map"]["source_url"] == "https://example.com/notes"
    assert document["metadata"]["source_map"]["final_url"] == "https://example.com/final"
    chunks = context.store.knowledge_chunks()
    assert chunks and all(chunk["document_id"] == document_id for chunk in chunks)
    assert all("char_start" in chunk and "char_end" in chunk for chunk in chunks)
    episodes = context.store.episodes()
    assert any(episode.get("document_id") == document_id for episode in episodes)
    provenance = context.store.provenance_records()
    assert any(
        record["node_type"] == "document" and record["node_id"] == document_id
        for record in provenance
    )
    retained = document["metadata"]["retained_source_path"]
    assert Path(retained).read_bytes().startswith(b"# Release notes")


def test_web_fetch_can_retain_originals_in_configured_local_or_nas_mount(tmp_path, monkeypatch):
    context = _context(tmp_path)
    retained_root = tmp_path / "mounted-research"
    monkeypatch.setenv("BRIGADE_RESEARCH_STORAGE_PATH", str(retained_root))
    _patch_network(monkeypatch, b"retained source " * 100)

    result = _web_fetch(
        context, {"url": "https://example.com/retained", "save_to_knowledge": True}
    )

    assert result.ok
    document = context.store.knowledge_documents()[0]
    assert Path(document["metadata"]["retained_source_path"]).is_relative_to(retained_root)


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
    old_map = documents[old_id]["metadata"]["source_map"]
    new_map = documents[new_id]["metadata"]["source_map"]
    assert old_map["content_hash"] != new_map["content_hash"]
    assert Path(old_map["retained_source_path"]).read_bytes().startswith(b"version one")
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


def test_web_fetch_pdf_extracts_text_and_preserves_url_metadata(tmp_path, monkeypatch):
    context = _context(tmp_path)
    _patch_network(monkeypatch, b"%PDF fake bytes")
    monkeypatch.setattr(
        tools,
        "extract_document_text",
        lambda filename, data: "PDF statute text\nsection 1" if filename.endswith(".pdf") else "",
    )

    result = _web_fetch(
        context,
        {
            "url": "https://example.com/report.pdf",
            "save_to_knowledge": True,
            "max_chars": 200,
        },
    )

    assert result.ok
    assert result.output == "PDF statute text\nsection 1"
    assert result.metadata["content_type"] == "application/pdf"
    assert result.metadata["source_url"] == "https://example.com/report.pdf"
    document = context.store.knowledge_documents()[0]
    assert document["metadata"]["source_url"] == "https://example.com/report.pdf"
    assert document["metadata"]["http_final_url"] == "https://example.com/final"
    assert document["metadata"]["content_type"] == "application/pdf"
    assert context.store.knowledge_chunks()[0]["text"].startswith("PDF statute text")


def test_web_fetch_pdf_uses_content_type_when_url_has_no_pdf_suffix(tmp_path, monkeypatch):
    context = _context(tmp_path)
    monkeypatch.setattr(
        tools.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(
        tools.urllib.request,
        "build_opener",
        lambda *handlers: _FakeOpener(
            b"%PDF fake bytes",
            "https://example.com/download?id=123",
            {"content-type": "application/pdf"},
        ),
    )
    monkeypatch.setattr(tools, "extract_document_text", lambda filename, data: "PDF text")

    result = _web_fetch(context, {"url": "https://example.com/download?id=123"})

    assert result.ok
    assert result.output == "PDF text"
    assert result.metadata["content_type"] == "application/pdf"


def test_web_search_returns_source_urls_and_can_save_result_list(tmp_path, monkeypatch):
    context = _context(tmp_path)
    monkeypatch.setenv("BRIGADE_SEARCH_BACKEND", "duckduckgo")
    html = """
    <html><body>
      <a class="result__a" href="/l/?uddg=https%3A%2F%2Fexample.com%2Fcase">Example case</a>
      <div class="result__snippet">A useful source snippet.</div>
      <a class="result__a" href="https://example.org/brief.pdf">Brief PDF</a>
      <div class="result__snippet">PDF result.</div>
    </body></html>
    """
    _patch_search(monkeypatch, html)

    result = _web_search(
        context,
        {"query": "test legal source", "limit": 2, "save_to_knowledge": True},
    )

    assert result.ok
    assert "https://example.com/case" in result.output
    assert "https://example.org/brief.pdf" in result.output
    assert result.metadata["source_urls"] == [
        "https://example.com/case",
        "https://example.org/brief.pdf",
    ]
    assert result.metadata["knowledge_save"] == "saved"
    document = context.store.knowledge_documents()[0]
    assert document["document_type"] == "web_search"
    assert document["metadata"]["query"] == "test legal source"
    assert document["metadata"]["result_urls"] == result.metadata["source_urls"]
    assert document["metadata"]["source_map"]["retrieval_tool"] == "web_search"
    assert document["metadata"]["source_map"]["results"][0]["url"] == "https://example.com/case"


def test_web_search_uses_configured_searxng_backend(tmp_path, monkeypatch):
    context = _context(tmp_path)
    monkeypatch.setenv("BRIGADE_SEARCH_BACKEND", "searxng")
    monkeypatch.setattr(
        tools,
        "searxng_search",
        lambda query, limit: (
            [
                tools.WebSearchResult(
                    title="Court opinion",
                    url="https://example.com/opinion",
                    snippet="A sourced result",
                    engine="bing",
                    rank=1,
                )
            ],
            "http://brigade_searxng:8080/search?q=test",
        ),
    )
    monkeypatch.setattr(tools, "_private_address_reason", lambda url: None)

    result = _web_search(context, {"query": "test", "limit": 1})

    assert result.ok
    assert "https://example.com/opinion" in result.output
    assert result.metadata["backend"] == "searxng"
    assert result.metadata["results"][0]["engine"] == "bing"


def test_web_search_retains_social_as_a_discovery_pointer_without_a_saved_snippet(
    tmp_path, monkeypatch
):
    context = _context(tmp_path)
    monkeypatch.setenv("BRIGADE_SEARCH_BACKEND", "searxng")
    monkeypatch.setattr(
        tools,
        "searxng_search",
        lambda query, limit: (
            [
                tools.WebSearchResult(
                    title="Pointer", url="https://x.com/example/status/1", snippet="Do not save me"
                )
            ],
            "http://brigade_searxng:8080/search?q=test",
        ),
    )
    monkeypatch.setattr(tools, "_private_address_reason", lambda url: None)

    result = _web_search(context, {"query": "pointer", "save_to_knowledge": True})

    assert result.ok
    assert "Discovery pointer only" in result.output
    assert result.metadata["results"][0]["source_tier"] == "discovery_only"
    saved_results = context.store.knowledge_documents()[0]["metadata"]["source_map"]["results"]
    assert saved_results[0]["source_tier"] == "discovery_only"
    assert "snippet" not in saved_results[0]


def test_web_search_records_degraded_searxng_before_fallback(tmp_path, monkeypatch):
    context = _context(tmp_path)
    monkeypatch.setenv("BRIGADE_SEARCH_BACKEND", "searxng")
    def unavailable(*args, **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(tools, "search_with_retry", unavailable)
    monkeypatch.setattr(
        tools,
        "_duckduckgo_search",
        lambda query, limit: ([tools.WebSearchResult("Fallback", "https://example.com", "")], "https://duckduckgo.com"),
    )
    monkeypatch.setattr(tools, "_private_address_reason", lambda url: None)

    result = _web_search(context, {"query": "fallback"})

    assert result.ok
    assert result.metadata["mode"] == "fallback"
    health = (context.store.data_dir / "research_search_health.json").read_text(encoding="utf-8")
    assert '"state": "degraded"' in health


def test_saved_search_keeps_backend_degradation_metadata(tmp_path, monkeypatch):
    context = _context(tmp_path)
    monkeypatch.setenv("BRIGADE_SEARCH_BACKEND", "searxng")

    def unavailable(*args, **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(tools, "search_with_retry", unavailable)
    monkeypatch.setattr(
        tools,
        "_duckduckgo_search",
        lambda query, limit: ([tools.WebSearchResult("Fallback", "https://example.com", "")], "https://duckduckgo.com"),
    )
    monkeypatch.setattr(tools, "_private_address_reason", lambda url: None)

    result = _web_search(context, {"query": "fallback", "save_to_knowledge": True})

    assert result.ok
    source_map = context.store.knowledge_documents()[0]["metadata"]["source_map"]
    assert source_map["search_mode"] == "fallback"
    assert source_map["backend_error"] == "down"


def test_browser_extract_can_save_rendered_page_with_source_map(tmp_path, monkeypatch):
    context = _context(tmp_path)
    monkeypatch.setattr(tools, "_private_address_reason", lambda url: None)
    monkeypatch.setattr(
        tools,
        "call_browser_worker",
        lambda action, payload: {
            "ok": True,
            "url": "https://example.com/app",
            "title": "Rendered App",
            "text": "Rendered page text " * 80,
            "html": "<html><body>Rendered page text</body></html>",
        },
    )

    result = _browser_extract(
        context,
        {"url": "https://example.com/app", "save_to_knowledge": True},
    )

    assert result.ok
    assert result.metadata["knowledge_save"] == "saved"
    document = context.store.knowledge_documents()[0]
    assert document["document_type"] == "web"
    assert document["metadata"]["source_map"]["retrieval_tool"] == "browser_extract"
    assert document["metadata"]["source_url"] == "https://example.com/app"


def test_browser_profile_is_restricted_to_chiefs_and_executive(tmp_path, monkeypatch):
    context = _context(tmp_path)
    context = ToolContext(
        agent=Agent("worker", "Worker", "agents/worker"),
        assignment=None,
        store=context.store,
    )
    monkeypatch.setattr(tools, "_private_address_reason", lambda url: None)

    denied = _browser_open(
        context,
        {"url": "https://example.com/private", "profile": "operator"},
    )

    assert not denied.ok
    assert "restricted to Crew Chiefs" in denied.output

    context.store.upsert_team(
        Team(team_id="alpha", display_name="Alpha", crew_chief_id="sage")
    )
    chief_context = ToolContext(
        agent=Agent("sage", "Sage", "agents/sage", role="crew_chief"),
        assignment=None,
        store=context.store,
    )
    monkeypatch.setattr(
        tools,
        "call_browser_worker",
        lambda action, payload: {
            "ok": True,
            "url": payload["url"],
            "title": "Private Session",
        },
    )

    allowed = _browser_open(
        chief_context,
        {"url": "https://example.com/private", "profile": "operator"},
    )

    assert allowed.ok
    assert allowed.metadata["title"] == "Private Session"


def test_mcp_tools_register_from_configured_servers(tmp_path, monkeypatch):
    server = MCPServerConfig(name="demo", transport="http", url="http://mcp.local")
    monkeypatch.setattr(tools, "configured_servers", lambda data_dir: [server])
    monkeypatch.setattr(
        tools,
        "discover_tools",
        lambda configured: [
            MCPTool(
                server=configured,
                name="echo",
                description="Echo input",
                argument_schema={"text": {"type": "string", "description": "input text"}},
            )
        ],
    )
    monkeypatch.setattr(
        tools,
        "call_mcp_tool",
        lambda configured, tool_name, args: MCPCallResult(
            True,
            f"{configured.name}:{tool_name}:{args['text']}",
            {"server": configured.name},
        ),
    )

    registry = default_tool_registry()
    result = registry.execute("mcp__demo__echo", _context(tmp_path), {"text": "hello"})

    assert result.ok
    assert result.output == "demo:echo:hello"
