from __future__ import annotations

import json
from pathlib import Path

from brigade.search_evaluation import evaluate_cases, meets_thresholds


def test_versioned_search_evaluation_fixture_meets_agreed_thresholds():
    fixture_path = Path(__file__).parents[1] / "brigade" / "fixtures" / "search_evaluation_v1.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    metrics = evaluate_cases(fixture["cases"])

    assert metrics["queries"] == 5
    assert meets_thresholds(metrics, fixture["thresholds"])


def test_search_evaluation_counts_duplicates_and_missing_relevant_results():
    metrics = evaluate_cases(
        [
            {
                "expected_urls": ["https://example.test/a"],
                "preferred_tiers": ["official_government"],
                "results": [
                    {"url": "https://example.test/a", "source_tier": "official_government"},
                    {"url": "https://example.test/a?duplicate=1", "source_tier": "general_web"},
                ],
            }
        ]
    )

    assert metrics["recall"] == 1.0
    assert metrics["precision"] == 1.0
    assert metrics["authority"] == 0.5
    assert metrics["duplicate_rate"] == 0.5
