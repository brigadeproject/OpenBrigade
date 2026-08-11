"""Source-quality classification and stable evidence citations."""
# ruff: noqa: E501, I001
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


SOCIAL_HOSTS = {"x.com", "twitter.com", "facebook.com", "instagram.com", "tiktok.com", "linkedin.com"}
PRIMARY_LEGAL_HOSTS = {
    "supremecourt.gov",
    "congress.gov",
    "govinfo.gov",
    "ecfr.gov",
    "uscode.house.gov",
    "regulations.gov",
}
ACADEMIC_INDEX_HOSTS = {"pubmed.ncbi.nlm.nih.gov", "semanticscholar.org", "doi.org"}
SECONDARY_LEGAL_HOSTS = {"courtlistener.com", "law.justia.com", "findlaw.com", "casetext.com"}


def classify_source(
    url: str, *, publication_status: str | None = None
) -> tuple[str, str | None, str | None]:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    normalized_status = (publication_status or "").strip().lower().replace("-", "_")
    if normalized_status == "peer_reviewed":
        return "academic_peer_reviewed", None, "peer-review status supplied by the source metadata"
    if host in SOCIAL_HOSTS:
        return "discovery_only", None, None
    if host == "law.cornell.edu":
        return "secondary_legal", "US", "Legal Information Institute interpretation"
    if host in SECONDARY_LEGAL_HOSTS or any(host.endswith(f".{item}") for item in SECONDARY_LEGAL_HOSTS):
        return "secondary_legal", "US", f"Secondary legal publisher: {host}"
    if host == "arxiv.org":
        return "academic_preprint", None, "arXiv preprint; not peer-review status"
    if host in ACADEMIC_INDEX_HOSTS:
        return "academic_index", None, "publication status must be verified from the retrieved work"
    if host == "supremecourt.gov" or host.endswith(".uscourts.gov"):
        return "court_material", "US", host
    if host in PRIMARY_LEGAL_HOSTS:
        return "primary_legal", "US", None
    if host.endswith(".gov"):
        return "official_government", "US", None
    if host.endswith(".edu"):
        return "academic", None, "peer-review status must be verified from the retrieved work"
    return "general_web", None, None


def source_selection_hint(intent: str, jurisdiction: str | None) -> str:
    if intent != "legal":
        return "Use source tier and freshness metadata; retrieve a result before treating it as evidence."
    if not jurisdiction:
        return "Jurisdiction is unspecified; state that assumption and prefer primary official material."
    return f"Prefer primary legal and official sources applicable to {jurisdiction}; use secondary sources only for orientation."


@dataclass(frozen=True)
class Citation:
    source_id: str
    source_url: str
    final_url: str
    accessed_at: str
    content_hash: str
    title: str = ""
    retrieval_method: str = ""
    source_tier: str = ""
    authority: str = ""
    char_start: int | None = None
    char_end: int | None = None
    page_start: int | None = None
    page_end: int | None = None

    def to_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


def citation_from_document(document: dict[str, object], chunk: dict[str, object] | None = None) -> Citation:
    metadata = dict(document.get("metadata") or {})
    source_map = dict(metadata.get("source_map") or {})
    return Citation(
        source_id=str(document.get("document_id") or ""),
        source_url=str(source_map.get("source_url") or metadata.get("source_url") or document.get("source") or ""),
        final_url=str(source_map.get("final_url") or metadata.get("http_final_url") or document.get("source") or ""),
        accessed_at=str(source_map.get("accessed_at") or metadata.get("fetched_at") or document.get("ingested_at") or ""),
        content_hash=str(source_map.get("content_hash") or metadata.get("content_hash") or ""),
        title=str(source_map.get("title") or document.get("title") or ""),
        retrieval_method=str(source_map.get("retrieval_tool") or ""),
        source_tier=str(source_map.get("source_tier") or metadata.get("source_tier") or ""),
        authority=str(source_map.get("authority") or metadata.get("authority") or ""),
        char_start=int(chunk["char_start"]) if chunk and chunk.get("char_start") is not None else None,
        char_end=int(chunk["char_end"]) if chunk and chunk.get("char_end") is not None else None,
        page_start=int(chunk["page_start"]) if chunk and chunk.get("page_start") is not None else None,
        page_end=int(chunk["page_end"]) if chunk and chunk.get("page_end") is not None else None,
    )


def validate_citations(citations: list[dict[str, object]], supplied_source_ids: set[str]) -> list[str]:
    errors: list[str] = []
    for index, citation in enumerate(citations):
        source_id = str(citation.get("source_id") or "")
        if not source_id or source_id not in supplied_source_ids:
            errors.append(f"citation {index} references an unsupplied source")
        if not citation.get("source_url") or not citation.get("final_url"):
            errors.append(f"citation {index} is missing a preserved URL")
        start, end = citation.get("char_start"), citation.get("char_end")
        if (start is None) != (end is None) or (start is not None and int(start) > int(end)):
            errors.append(f"citation {index} has an invalid character locator")
    return errors


def render_citation(citation: dict[str, object]) -> str:
    locator = ""
    if citation.get("page_start") is not None:
        locator = f" p. {citation['page_start']}"
    elif citation.get("char_start") is not None:
        locator = f" chars {citation['char_start']}-{citation['char_end']}"
    classification = str(citation.get("source_tier") or "").replace("_", " ")
    qualifier = f" ({classification})" if classification else ""
    return f"[{citation.get('title') or citation.get('source_id')}]({citation.get('final_url')}){qualifier} — accessed {citation.get('accessed_at')}{locator}"
