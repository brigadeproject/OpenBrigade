"""Citation-bearing answer policy and stable rendering for retrieved evidence."""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from brigade.evidence import (
    Citation,
    citation_from_document,
    classify_source,
    render_citation,
    validate_citations,
)
from brigade.time import utc_now_iso

_CITATION_MARKER = re.compile(r"\[\[cite:([A-Za-z0-9_.:-]+)\]\]")
_CLAIM_MARKER = re.compile(r"\[\[(inference|operator_assertion)\]\]", re.I)
_CITATION_REQUEST = re.compile(
    r"\b(?:cite|citation|references?)\b|\bsource\s+links?\b|"
    r"\b(?:provide|with)\s+sources?\b",
    re.I,
)
_RETRIEVED_TOOLS = {
    "web_fetch",
    "browser_extract",
    "browser_open",
    "browser_click",
    "search_knowledge",
}
_PERSISTABLE_RETRIEVAL_TOOLS = {"web_fetch", "browser_extract"}


@dataclass(frozen=True)
class CitationContext:
    required: bool
    citations: tuple[Citation, ...]

    @property
    def ids(self) -> set[str]:
        return {citation.source_id for citation in self.citations}


def citation_required(message: str, observations: list[dict[str, Any]]) -> bool:
    """Citation policy is opt-in by task intent; ordinary prose remains ordinary."""
    del observations  # Retrieval alone must never silently change task intent.
    return bool(_CITATION_REQUEST.search(message))


def citation_retrieval_arguments(
    message: str, observations: list[dict[str, Any]], tool_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Retain originals only when a tool call participates in a cited answer."""
    if (
        not citation_required(message, observations)
        or tool_name not in _PERSISTABLE_RETRIEVAL_TOOLS
    ):
        return arguments
    prepared = dict(arguments)
    prepared["save_to_knowledge"] = True
    return prepared


def citation_context(
    store: Any, message: str, observations: list[dict[str, Any]]
) -> CitationContext:
    citations: list[Citation] = []
    for observation in observations:
        if str(observation.get("tool") or "") not in _RETRIEVED_TOOLS:
            continue
        metadata = observation.get("metadata") or {}
        if not isinstance(metadata, dict):
            continue
        document_ids = [str(metadata.get("saved_document_id") or "")]
        document_ids.extend(str(item) for item in metadata.get("document_ids") or [])
        documents = [
            item
            for item in store.knowledge_documents()
            if str(item.get("document_id") or "") in set(document_ids)
        ]
        if documents:
            chunks = metadata.get("citation_chunks") or []
            for document in documents:
                document_id = str(document.get("document_id") or "")
                chunk = next(
                    (
                        item
                        for item in chunks
                        if isinstance(item, dict)
                        and str(item.get("document_id") or "") == document_id
                    ),
                    None,
                )
                citations.append(citation_from_document(document, chunk))
            continue
        final_url = str(
            metadata.get("final_url")
            or metadata.get("http_final_url")
            or metadata.get("source_url")
            or ""
        )
        if not final_url:
            continue
        source_id = f"external:{sha256(final_url.encode('utf-8')).hexdigest()[:16]}"
        source_tier, _jurisdiction, authority = classify_source(final_url)
        citations.append(
            Citation(
                source_id=source_id,
                source_url=str(metadata.get("source_url") or final_url),
                final_url=final_url,
                accessed_at=str(metadata.get("fetched_at") or utc_now_iso()),
                content_hash=str(metadata.get("content_hash") or ""),
                title=str(metadata.get("title") or final_url),
                retrieval_method=str(observation.get("tool") or ""),
                source_tier=source_tier,
                authority=authority or "",
            )
        )
    unique = {citation.source_id: citation for citation in citations}
    return CitationContext(citation_required(message, observations), tuple(unique.values()))


def citation_instructions(context: CitationContext) -> str:
    if not context.required:
        return ""
    if not context.citations:
        return (
            "This is a citation-bearing request but no retrieved evidence is available. "
            "Say that you cannot substantiate the requested claim; do not answer from memory."
        )
    sources = ", ".join(
        f"{citation.source_id} ({citation.final_url})" for citation in context.citations
    )
    return (
        "This is a citation-bearing request. Support externally derived claims with "
        "[[cite:SOURCE_ID]] markers using only these supplied sources: "
        f"{sources}. Do not invent citations. Prefix model reasoning with "
        "[[inference]] and an operator-provided assertion with [[operator_assertion]]."
    )


def validate_and_render_answer(
    text: str, context: CitationContext
) -> tuple[str, list[dict[str, object]], list[str]]:
    """Validate only citation-bearing answers, then render stable Markdown footnotes."""
    if not context.required:
        return text, [], []
    markers = _CITATION_MARKER.findall(text)
    if not context.citations:
        return text, [], ["citation-bearing answer has no retrieved evidence"]
    if not markers:
        return text, [], ["citation-bearing answer has no citation markers"]
    by_id = {citation.source_id: citation for citation in context.citations}
    cited = [by_id[source_id].to_dict() for source_id in markers if source_id in by_id]
    errors = validate_citations(cited, context.ids)
    if len(cited) != len(markers):
        errors.append("answer references an unsupplied citation")
    if errors:
        return text, cited, errors
    ordered: list[dict[str, object]] = []
    for citation in cited:
        if citation not in ordered:
            ordered.append(citation)
    numbers = {str(citation["source_id"]): index for index, citation in enumerate(ordered, start=1)}
    rendered = _CITATION_MARKER.sub(lambda match: f"[^{numbers[match.group(1)]}]", text)
    rendered = _CLAIM_MARKER.sub(
        lambda match: "**Inference:** "
        if match.group(1).lower() == "inference"
        else "**Operator assertion:** ",
        rendered,
    )
    footnotes = "\n".join(
        f"[^{index}]: {render_citation(citation)}"
        for index, citation in enumerate(ordered, start=1)
    )
    return f"{rendered.rstrip()}\n\n{footnotes}", ordered, []


def classify_rendered_claims(
    rendered: str, citations: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Expose source facts, model inference, and operator assertions separately."""
    source_by_number = {
        str(index): str(citation["source_id"])
        for index, citation in enumerate(citations, start=1)
    }
    claims: list[dict[str, object]] = []
    for paragraph in rendered.split("\n\n"):
        stripped = paragraph.strip()
        if not stripped or stripped.startswith("[^") and "]:" in stripped:
            continue
        source_numbers = re.findall(r"\[\^(\d+)\]", stripped)
        if source_numbers:
            claims.append(
                {
                    "kind": "source_fact",
                    "citation_ids": [
                        source_by_number[number]
                        for number in source_numbers
                        if number in source_by_number
                    ],
                }
            )
        elif stripped.startswith("**Inference:**"):
            claims.append({"kind": "model_inference", "citation_ids": []})
        elif stripped.startswith("**Operator assertion:**"):
            claims.append({"kind": "operator_assertion", "citation_ids": []})
    return claims


def enforce_citation_answer(
    store: Any,
    message: str,
    observations: list[dict[str, Any]],
    draft: str,
    *,
    repair: Callable[[str], str] | None = None,
) -> tuple[str, CitationContext, list[dict[str, object]], list[str], bool]:
    """Render validated citations or a clearly-labelled, non-evidentiary draft.

    Citation formatting is a model-output concern, not a reason to spend more
    agent turns.  We therefore retain the operator-visible draft when evidence
    exists, while deliberately keeping it out of the validated-citation and
    source-fact surfaces.
    """
    context = citation_context(store, message, observations)
    rendered, citations, errors = validate_and_render_answer(draft, context)
    if not errors:
        return rendered, context, citations, errors, False
    del repair
    if context.citations:
        return (
            _render_unvalidated_draft(draft, context.citations),
            context,
            [],
            errors,
            False,
        )
    return (
        "I cannot substantiate this citation-bearing request from the retrieved evidence. "
        "Please retrieve a relevant primary or otherwise authoritative source first.",
        context,
        [],
        errors,
        False,
    )


def citation_validation_status(context: CitationContext, errors: list[str]) -> str:
    """Expose a stable, operator-facing citation validation outcome."""
    if not context.required:
        return "not_required"
    if not errors:
        return "validated"
    if not context.citations:
        return "no_retrieved_evidence"
    return "unvalidated_draft"


def available_citations(context: CitationContext) -> list[dict[str, object]]:
    """Return retrieved sources without asserting that the draft cites them."""
    return [citation.to_dict() for citation in context.citations]


def _render_unvalidated_draft(draft: str, citations: tuple[Citation, ...]) -> str:
    """Keep a failed draft visible without displaying fabricated footnotes."""
    safe_draft = _CITATION_MARKER.sub("[unverified citation]", draft).strip()
    safe_draft = _CLAIM_MARKER.sub("", safe_draft).strip()
    sources = "\n".join(
        f"- {render_citation(citation.to_dict())}" for citation in citations
    )
    return (
        "> **Citation validation warning:** The following model draft could not be "
        "validated against the retrieved evidence. Do not treat its claims as source-backed.\n\n"
        "## Unvalidated draft\n\n"
        f"{safe_draft}\n\n"
        "## Retrieved sources\n\n"
        f"{sources}"
    )
