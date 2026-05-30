from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from brigade.store import StateStore
from brigade.time import utc_now_iso


def build_financial_report(store: StateStore) -> dict[str, object]:
    usage_records = store.usage_records()
    local_records = [record for record in usage_records if record.get("route_type") == "local"]
    cloud_records = [record for record in usage_records if record.get("route_type") == "cloud"]
    simulated_records = [
        record for record in usage_records if record.get("route_type") == "simulated"
    ]
    active_cloud_jobs = [
        job for job in store.cloud_jobs() if job.get("status") not in {"complete", "failed"}
    ]

    local_input = sum(int(record.get("input_tokens", 0)) for record in local_records)
    local_output = sum(int(record.get("output_tokens", 0)) for record in local_records)
    cloud_input = sum(int(record.get("input_tokens", 0)) for record in cloud_records)
    cloud_output = sum(int(record.get("output_tokens", 0)) for record in cloud_records)
    simulated_input = sum(int(record.get("input_tokens", 0)) for record in simulated_records)
    simulated_output = sum(int(record.get("output_tokens", 0)) for record in simulated_records)
    total_cost = round(
        sum(float(record.get("estimated_cost_usd", 0.0)) for record in usage_records),
        6,
    )
    block_cloud_dispatch = bool(active_cloud_jobs)
    routing_recommendation = "prefer_local" if block_cloud_dispatch else "cloud_ok_if_justified"

    return {
        "report_id": str(uuid4()),
        "generated_at": utc_now_iso(),
        "local_tokens": {
            "input": local_input,
            "output": local_output,
        },
        "cloud_tokens": {
            "input": cloud_input,
            "output": cloud_output,
        },
        "simulated_tokens": {
            "input": simulated_input,
            "output": simulated_output,
        },
        "total_estimated_cost_usd": total_cost,
        "cloud_jobs_in_flight": len(active_cloud_jobs),
        "block_cloud_dispatch": block_cloud_dispatch,
        "routing_recommendation": routing_recommendation,
        "source_confidence": "high" if usage_records else "low",
    }


def build_model_routing_decision(
    store: StateStore,
    *,
    task_type: str,
    risk: str = "normal",
    prefer: str = "auto",
    local_model: str = "llama3.1",
    cloud_model: str = "gpt-4.1-mini",
) -> dict[str, object]:
    report = build_financial_report(store)
    usage_records = store.usage_records()
    provider_counts: dict[str, int] = {}
    provider_costs: dict[str, float] = {}
    for record in usage_records:
        provider = str(record.get("provider") or "unknown")
        provider_counts[provider] = provider_counts.get(provider, 0) + 1
        provider_costs[provider] = provider_costs.get(provider, 0.0) + float(
            record.get("estimated_cost_usd", 0.0)
        )

    block_cloud = bool(report["block_cloud_dispatch"])
    high_risk = risk in {"high", "critical"}
    if prefer == "cloud" and block_cloud:
        recommended_provider = "ollama"
        recommended_model = local_model
        rationale = "Cloud is preferred, but another cloud job is already in flight."
    elif prefer == "cloud" or (prefer == "auto" and high_risk and not block_cloud):
        recommended_provider = "litellm"
        recommended_model = cloud_model
        rationale = "Cloud is allowed for higher-risk or explicitly cloud-preferred work."
    elif prefer == "fake":
        recommended_provider = "fake"
        recommended_model = "llama3.1"
        rationale = "Simulated routing requested for deterministic prototype testing."
    else:
        recommended_provider = "ollama"
        recommended_model = local_model
        rationale = "Local routing keeps cost bounded for normal prototype work."

    return {
        "decision_id": str(uuid4()),
        "generated_at": utc_now_iso(),
        "task_type": task_type,
        "risk": risk,
        "prefer": prefer,
        "recommended_provider": recommended_provider,
        "recommended_model": recommended_model,
        "route_type": _route_type_for_provider(recommended_provider),
        "rationale": rationale,
        "financial_report": report,
        "provider_counts": provider_counts,
        "provider_estimated_cost_usd": {
            key: round(value, 6) for key, value in provider_costs.items()
        },
        "source_confidence": report["source_confidence"],
    }


def persist_financial_report(store: StateStore, data_dir: Path) -> dict[str, object]:
    report = build_financial_report(store)
    store.set_financial_report(report)

    reports_dir = data_dir / "reports" / "financial"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / "latest-financial-report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report


def _route_type_for_provider(provider: str) -> str:
    if provider == "ollama":
        return "local"
    if provider == "litellm":
        return "cloud"
    return "simulated"
