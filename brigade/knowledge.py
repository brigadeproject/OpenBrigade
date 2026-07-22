from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from datetime import timedelta

from brigade.ingestion import chunk_text
from brigade.time import parse_utc_iso, utc_now, utc_now_iso

SUPPORTED_TEXT_EXTENSIONS = {".md", ".txt"}
# Formats the ingest pipeline can turn into plain text. Text formats need no
# extra dependency; .pdf/.html use the optional ``ingest`` extra (pypdf +
# trafilatura), with a stdlib fallback for HTML so a bare stack still works.
SUPPORTED_INGEST_EXTENSIONS = SUPPORTED_TEXT_EXTENSIONS | {".pdf", ".html", ".htm"}


def _pdf_to_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - depends on the ingest extra
        raise ValueError(
            "PDF ingestion requires the 'ingest' extra (pip install '.[ingest]')"
        ) from exc
    import io

    reader = PdfReader(io.BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(part.strip() for part in pages if part.strip())


def _strip_html_tags(html: str) -> str:
    """Zero-dependency HTML→text fallback used when trafilatura is absent."""
    from html.parser import HTMLParser

    class _Extractor(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self._skip = 0
            self.parts: list[str] = []

        def handle_starttag(self, tag: str, attrs: object) -> None:
            if tag in {"script", "style"}:
                self._skip += 1

        def handle_endtag(self, tag: str) -> None:
            if tag in {"script", "style"} and self._skip:
                self._skip -= 1

        def handle_data(self, data: str) -> None:
            if not self._skip and data.strip():
                self.parts.append(data.strip())

    parser = _Extractor()
    parser.feed(html)
    return "\n".join(parser.parts)


def html_to_text(html: str) -> str:
    """Extract readable text from HTML.

    Prefers trafilatura (the ``ingest`` extra) for clean article text; falls
    back to a stdlib tag-stripper so web content is never stored as raw markup.
    """
    try:
        import trafilatura

        extracted = trafilatura.extract(html)
        if extracted and extracted.strip():
            return extracted.strip()
    except Exception:  # pragma: no cover - trafilatura optional / parse issues
        pass
    return _strip_html_tags(html)


def extract_document_text(filename: str, data: bytes) -> str:
    """Turn a supported file's bytes into plain text for ingestion.

    Raises ValueError for unsupported extensions or a missing extractor dep.
    """
    suffix = Path(filename).suffix.lower()
    if suffix in SUPPORTED_TEXT_EXTENSIONS:
        return data.decode("utf-8", errors="replace")
    if suffix == ".pdf":
        return _pdf_to_text(data)
    if suffix in {".html", ".htm"}:
        return html_to_text(data.decode("utf-8", errors="replace"))
    raise ValueError(f"unsupported content type for ingestion: {suffix or '<none>'}")


@dataclass(frozen=True)
class KnowledgeDocument:
    title: str
    source: str
    document_type: str
    content_path: str
    document_id: str = field(default_factory=lambda: str(uuid4()))
    ingested_at: str = field(default_factory=utc_now_iso)
    metadata: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


def metadata_for_text(title: str, source: str, content: str) -> dict[str, object]:
    chunks = chunk_text(content)
    return {
        "title": title,
        "source": source,
        "chunk_count": len(chunks),
        "character_count": len(content),
    }


IngestResult = tuple[
    KnowledgeDocument, list[dict[str, object]], dict[str, object], list[dict[str, object]]
]


def ingest_text(
    title: str,
    source: str,
    document_type: str,
    content: str,
    *,
    content_path: str,
    extra_metadata: dict[str, object] | None = None,
) -> IngestResult:
    """Build the document/chunks/episode/provenance records for a text body.

    The document_id is stamped into every derived record (and mirrored as a
    ``kb_id`` on chunks and the episode) so all stores share one universal ID.
    """
    metadata = metadata_for_text(title, source, content)
    if extra_metadata:
        metadata.update(extra_metadata)

    document = KnowledgeDocument(
        title=title,
        source=source,
        document_type=document_type,
        content_path=content_path,
        metadata=metadata,
    )
    kb_doc_id = f"doc:{document.document_id}"
    chunks = []
    for chunk in chunk_text(content):
        chunk_id = str(uuid4())
        chunks.append(
            {
                "chunk_id": chunk_id,
                "kb_id": f"chunk:{chunk_id}",
                "document_id": document.document_id,
                "document_type": document_type,
                "chunk_index": chunk.index,
                "text": chunk.text,
                "source": source,
                "content_path": content_path,
                "created_at": utc_now_iso(),
            }
        )
    summary = _extract_summary(content, title)
    episode_id = str(uuid4())
    episode = {
        "episode_id": episode_id,
        "kb_id": f"episode:{episode_id}",
        "agent_id": "knowledge",
        "source_kind": "knowledge_document",
        "source_id": document.document_id,
        "document_id": document.document_id,
        "summary": summary,
        "learned_facts": [summary],
        "open_threads": [],
        "source_refs": [content_path],
        "created_at": utc_now_iso(),
    }
    provenance = [
        {
            "record_id": str(uuid4()),
            "node_type": "document",
            "node_id": document.document_id,
            "source_refs": [content_path],
            "metadata": {
                "title": title,
                "document_type": document_type,
                "chunk_count": len(chunks),
                "document_id": document.document_id,
                "kb_id": kb_doc_id,
            },
            "created_at": utc_now_iso(),
        }
    ]
    for chunk in chunks:
        provenance.append(
            {
                "record_id": str(uuid4()),
                "node_type": "chunk",
                "node_id": chunk["chunk_id"],
                "source_refs": [content_path],
                "metadata": {
                    "document_id": document.document_id,
                    "chunk_index": chunk["chunk_index"],
                },
                "created_at": utc_now_iso(),
            }
        )
    return document, chunks, episode, provenance


def ingest_local_document(
    title: str,
    source: str,
    document_type: str,
    content_path: str,
) -> IngestResult:
    path = Path(content_path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_INGEST_EXTENSIONS:
        raise ValueError(f"unsupported content type for ingestion: {suffix or '<none>'}")
    content = extract_document_text(path.name, path.read_bytes())
    return ingest_text(
        title=title,
        source=source,
        document_type=document_type,
        content=content,
        content_path=str(path),
        extra_metadata={"content_type": suffix.removeprefix(".")},
    )


def store_ingest_result(store: object, result: IngestResult) -> dict[str, object]:
    """Persist an ingest result to every configured store; returns the document dict.

    ``store`` is any StateStore; typed as object to avoid an import cycle.
    """
    document, chunks, episode, provenance = result
    store.add_knowledge_document(document.to_dict())
    for chunk in chunks:
        store.add_knowledge_chunk(chunk)
    store.add_episode(episode)
    for record in provenance:
        store.add_provenance_record(record)
    return document.to_dict()


def web_knowledge_max_age_days(store: object) -> int:
    """Operator TTL for web-fetched knowledge, in days. 0 disables expiry."""
    try:
        raw = (store.runtime_overrides() or {}).get("web_knowledge_max_age_days")
        value = int(raw) if raw is not None else 0
    except (TypeError, ValueError, RuntimeError):
        value = 0
    return max(0, value)


def web_chunk_expired(chunk: dict[str, object], max_age_days: int) -> bool:
    """True when a chunk comes from a web document older than the TTL.

    Non-web chunks (including pre-1.2 chunks without a document_type) never
    expire; a missing/unparseable created_at is treated as fresh so a bad
    timestamp cannot silently hide knowledge.
    """
    if max_age_days <= 0:
        return False
    if str(chunk.get("document_type") or "") != "web":
        return False
    created_at = str(chunk.get("created_at") or "")
    if not created_at:
        return False
    try:
        age = utc_now() - parse_utc_iso(created_at)
    except ValueError:
        return False
    return age > timedelta(days=max_age_days)


def active_knowledge_chunks(
    store: object, document_id: str | None = None
) -> list[dict[str, object]]:
    """Knowledge chunks minus expired web content — the retrieval-side view."""
    max_age_days = web_knowledge_max_age_days(store)
    chunks = store.knowledge_chunks(document_id)
    if max_age_days <= 0:
        return chunks
    return [chunk for chunk in chunks if not web_chunk_expired(chunk, max_age_days)]


def filter_expired_web_rows(
    store: object, rows: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Drop {score, payload} search rows whose payload is an expired web chunk."""
    max_age_days = web_knowledge_max_age_days(store)
    if max_age_days <= 0:
        return rows
    return [
        row
        for row in rows
        if not web_chunk_expired(row.get("payload") or {}, max_age_days)
    ]


def _extract_summary(content: str, fallback: str) -> str:
    for line in content.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:240]
    return fallback[:240]
