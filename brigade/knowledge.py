from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from brigade.ingestion import chunk_text
from brigade.time import utc_now_iso

SUPPORTED_TEXT_EXTENSIONS = {".md", ".txt"}


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


def ingest_local_document(
    title: str,
    source: str,
    document_type: str,
    content_path: str,
) -> tuple[KnowledgeDocument, list[dict[str, object]], dict[str, object], list[dict[str, object]]]:
    path = Path(content_path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_TEXT_EXTENSIONS:
        raise ValueError(f"unsupported content type for MVP ingestion: {suffix or '<none>'}")
    content = path.read_text(encoding="utf-8")
    metadata = metadata_for_text(title, source, content)
    metadata["content_type"] = suffix.removeprefix(".")

    document = KnowledgeDocument(
        title=title,
        source=source,
        document_type=document_type,
        content_path=str(path),
        metadata=metadata,
    )
    chunks = [
        {
            "chunk_id": str(uuid4()),
            "document_id": document.document_id,
            "chunk_index": chunk.index,
            "text": chunk.text,
            "source": source,
            "content_path": str(path),
            "created_at": utc_now_iso(),
        }
        for chunk in chunk_text(content)
    ]
    summary = _extract_summary(content, title)
    episode = {
        "episode_id": str(uuid4()),
        "agent_id": "knowledge",
        "source_kind": "knowledge_document",
        "source_id": document.document_id,
        "summary": summary,
        "learned_facts": [summary],
        "open_threads": [],
        "source_refs": [str(path)],
        "created_at": utc_now_iso(),
    }
    provenance = [
        {
            "record_id": str(uuid4()),
            "node_type": "document",
            "node_id": document.document_id,
            "source_refs": [str(path)],
            "metadata": {
                "title": title,
                "document_type": document_type,
                "chunk_count": len(chunks),
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
                "source_refs": [str(path)],
                "metadata": {
                    "document_id": document.document_id,
                    "chunk_index": chunk["chunk_index"],
                },
                "created_at": utc_now_iso(),
            }
        )
    return document, chunks, episode, provenance


def _extract_summary(content: str, fallback: str) -> str:
    for line in content.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:240]
    return fallback[:240]
