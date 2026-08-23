from __future__ import annotations

from brigade.citations import (
    CitationContext,
    citation_context,
    citation_required,
    citation_retrieval_arguments,
    classify_rendered_claims,
    enforce_citation_answer,
    validate_and_render_answer,
)
from brigade.evidence import Citation
from brigade.markdown import render_markdown_html


def _context() -> CitationContext:
    return CitationContext(
        required=True,
        citations=(
            Citation(
                source_id="doc-1",
                source_url="https://example.com/original",
                final_url="https://example.com/final",
                accessed_at="2026-08-11T00:00:00Z",
                content_hash="abc",
                title="Evidence",
                retrieval_method="web_fetch",
            ),
        ),
    )


def test_plain_prose_is_not_a_citation_bearing_request():
    assert not citation_required("Write a friendly project update", [])
    assert (
        citation_retrieval_arguments("Write a friendly project update", [], "web_fetch", {})
        == {}
    )


def test_incidental_legal_or_source_language_is_not_citation_bearing():
    for message in (
        "Draft a legal-risk plan without citations.",
        "Explain the source of this deployment error.",
        "Search for a law and summarize the options.",
    ):
        assert not citation_required(message, [])


def test_citation_bearing_retrieval_retains_the_original_source():
    assert citation_retrieval_arguments(
        "Provide sources for this answer", [], "web_fetch", {"url": "https://example.com"}
    ) == {"url": "https://example.com", "save_to_knowledge": True}
    assert citation_retrieval_arguments(
        "Provide sources for this answer", [], "web_search", {"query": "statute"}
    ) == {"query": "statute"}


def test_citation_bearing_answers_reject_missing_or_invented_sources():
    _, _, missing = validate_and_render_answer("A legal answer", _context())
    _, _, invented = validate_and_render_answer("A claim [[cite:invented]]", _context())

    assert missing == ["citation-bearing answer has no citation markers"]
    assert "unsupplied" in invented[-1]


def test_failed_citation_format_returns_labelled_draft_and_retrieved_sources():
    class Store:
        def knowledge_documents(self):
            return []

    result, context, citations, errors, repaired = enforce_citation_answer(
        Store(),
        "Provide sources for this answer",
        [
            {
                "tool": "web_fetch",
                "metadata": {
                    "source_url": "https://example.com/source",
                    "http_final_url": "https://example.com/final",
                },
            }
        ],
        "An uncited answer",
    )

    assert context.required and context.citations
    assert citations == []
    assert errors == ["citation-bearing answer has no citation markers"]
    assert repaired is False
    assert "Citation validation warning" in result
    assert "An uncited answer" in result
    assert "Retrieved sources" in result


def test_citation_bearing_answer_renders_stable_footnote():
    rendered, citations, errors = validate_and_render_answer(
        "A supported claim [[cite:doc-1]]", _context()
    )

    assert not errors
    assert citations[0]["source_id"] == "doc-1"
    assert "A supported claim [^1]" in rendered
    assert "[^1]: [Evidence](https://example.com/final)" in rendered


def test_knowledge_excerpt_citation_preserves_pdf_page_and_character_locator():
    class Store:
        def knowledge_documents(self):
            return [
                {
                    "document_id": "pdf-1",
                    "title": "Opinion",
                    "metadata": {
                        "source_map": {
                            "source_url": "https://court.example/opinion.pdf",
                            "final_url": "https://court.example/opinion.pdf",
                            "accessed_at": "2026-08-11T00:00:00Z",
                            "content_hash": "hash",
                            "source_tier": "court_material",
                        }
                    },
                }
            ]

    context = citation_context(
        Store(),
        "Cite this legal source",
        [
            {
                "tool": "search_knowledge",
                "metadata": {
                    "document_ids": ["pdf-1"],
                    "citation_chunks": [
                        {
                            "document_id": "pdf-1",
                            "char_start": 10,
                            "char_end": 50,
                            "page_start": 3,
                            "page_end": 3,
                        }
                    ],
                },
            }
        ],
    )

    citation = context.citations[0]
    assert (citation.char_start, citation.char_end, citation.page_start) == (10, 50, 3)


def test_citation_rendering_classifies_source_facts_inference_and_operator_assertions():
    rendered, citations, errors = validate_and_render_answer(
        "A sourced fact [[cite:doc-1]]\n\n"
        "[[inference]] This is a model inference.\n\n"
        "[[operator_assertion]] The operator supplied this.",
        _context(),
    )

    assert not errors
    assert classify_rendered_claims(rendered, citations) == [
        {"kind": "source_fact", "citation_ids": ["doc-1"]},
        {"kind": "model_inference", "citation_ids": []},
        {"kind": "operator_assertion", "citation_ids": []},
    ]


def test_multi_source_html_and_browser_citations_render_in_the_web_markdown_surface():
    context = CitationContext(
        required=True,
        citations=(
            Citation(
                source_id="html-1",
                source_url="https://example.com/original",
                final_url="https://example.com/html",
                accessed_at="2026-08-11T00:00:00Z",
                content_hash="html-hash",
                title="HTML source",
                retrieval_method="web_fetch",
            ),
            Citation(
                source_id="browser-1",
                source_url="https://example.org/original",
                final_url="https://example.org/rendered",
                accessed_at="2026-08-11T00:00:00Z",
                content_hash="browser-hash",
                title="Browser source",
                retrieval_method="browser_extract",
            ),
        ),
    )
    rendered, citations, errors = validate_and_render_answer(
        "HTML fact [[cite:html-1]] and browser fact [[cite:browser-1]].", context
    )

    assert not errors
    assert [item["source_id"] for item in citations] == ["html-1", "browser-1"]
    html = render_markdown_html(rendered)
    assert 'href="https://example.com/html"' in html
    assert 'href="https://example.org/rendered"' in html
