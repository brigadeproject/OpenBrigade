from brigade.evidence import (
    citation_from_document,
    classify_source,
    render_citation,
    validate_citations,
)


def test_source_tiers_distinguish_primary_official_secondary_and_discovery_only():
    assert classify_source("https://www.congress.gov/bill")[0] == "primary_legal"
    assert classify_source("https://www.justice.gov/example")[0] == "official_government"
    tier, jurisdiction, label = classify_source("https://www.law.cornell.edu/uscode/text")
    assert (tier, jurisdiction) == ("secondary_legal", "US")
    assert label and "interpretation" in label.lower()
    assert classify_source("https://x.com/example/status/1")[0] == "discovery_only"
    assert classify_source("https://www.courtlistener.com/opinion/1")[0] == "secondary_legal"
    assert (
        classify_source("https://journal.example/paper", publication_status="peer-reviewed")[0]
        == "academic_peer_reviewed"
    )


def test_secondary_source_citation_keeps_its_interpretive_tier():
    citation = citation_from_document(
        {
            "document_id": "lii-1",
            "title": "LII analysis",
            "metadata": {
                "source_map": {
                    "source_url": "https://www.law.cornell.edu/example",
                    "final_url": "https://www.law.cornell.edu/example",
                    "accessed_at": "2026-08-11T00:00:00Z",
                    "source_tier": "secondary_legal",
                }
            },
        }
    ).to_dict()

    assert "secondary legal" in render_citation(citation)


def test_citation_validation_rejects_unsupplied_sources_and_bad_locators():
    errors = validate_citations(
        [
            {
                "source_id": "invented",
                "source_url": "",
                "final_url": "",
                "char_start": 4,
                "char_end": 2,
            }
        ],
        {"doc-1"},
    )
    assert len(errors) == 3
