"""Deterministic and opt-in-live measurement for governed search quality."""
from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from brigade.evidence import classify_source
from brigade.research import searxng_search


def canonical_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", "")
    )


def evaluate_cases(cases: Iterable[dict[str, Any]]) -> dict[str, float | int]:
    """Score labelled fixtures without making a network request."""
    case_list = list(cases)
    expected_total = 0
    expected_found = 0
    returned_total = 0
    relevant_total = 0
    authority_total = 0
    freshness_total = 0
    fresh_total = 0
    duplicate_total = 0
    for case in case_list:
        expected = {canonical_url(url) for url in case["expected_urls"]}
        allowed_tiers = set(case["preferred_tiers"])
        seen: set[str] = set()
        found_expected: set[str] = set()
        results = case["results"]
        expected_total += len(expected)
        for row in results:
            url = canonical_url(str(row["url"]))
            returned_total += 1
            if url in seen:
                duplicate_total += 1
            seen.add(url)
            if url in expected:
                relevant_total += 1
                if url not in found_expected:
                    expected_found += 1
                    found_expected.add(url)
            if str(row.get("source_tier") or classify_source(url)[0]) in allowed_tiers:
                authority_total += 1
            fresh_after = case.get("fresh_after")
            if fresh_after:
                freshness_total += 1
                if str(row.get("publication_date") or "") >= str(fresh_after):
                    fresh_total += 1
    return {
        "queries": len(case_list),
        "recall": expected_found / expected_total if expected_total else 1.0,
        "precision": relevant_total / returned_total if returned_total else 0.0,
        "authority": authority_total / returned_total if returned_total else 0.0,
        "freshness": fresh_total / freshness_total if freshness_total else 1.0,
        "duplicate_rate": duplicate_total / returned_total if returned_total else 0.0,
    }


def meets_thresholds(metrics: dict[str, float | int], thresholds: dict[str, float]) -> bool:
    return all(
        float(metrics[name]) >= minimum
        for name, minimum in thresholds.items()
        if name != "max_duplicate_rate"
    ) and float(metrics["duplicate_rate"]) <= thresholds["max_duplicate_rate"]


def live_cases(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    """Retrieve a report only; live runs never change the labelled fixture."""
    report: list[dict[str, Any]] = []
    for case in fixture["cases"]:
        found, _ = searxng_search(case["query"], limit=8)
        results = []
        for result in found:
            tier, jurisdiction, authority = classify_source(result.url)
            results.append(
                {
                    "url": result.url,
                    "source_tier": tier,
                    "jurisdiction": jurisdiction,
                    "authority": authority,
                    "publication_date": result.published_at,
                }
            )
        report.append({"query_id": case["query_id"], "results": results})
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate OpenBrigade research search quality")
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--live-report", type=Path)
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    if args.live_report:
        args.live_report.write_text(
            json.dumps(live_cases(fixture), indent=2) + "\n", encoding="utf-8"
        )
        print(f"wrote live retrieval report to {args.live_report}")
        return 0
    metrics = evaluate_cases(fixture["cases"])
    print(json.dumps(metrics, indent=2, sort_keys=True))
    return 0 if meets_thresholds(metrics, fixture["thresholds"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
