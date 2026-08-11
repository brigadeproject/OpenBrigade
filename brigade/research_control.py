"""Redacted research dependency status and bounded operator health checks."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from brigade.mcp_client import (
    configured_servers,
    discover_tools,
    record_server_health,
    redact_failure_reason,
    server_health,
)
from brigade.research import (
    allowed_search_engines,
    browser_worker_url,
    search_backend,
    searxng_search,
)
from brigade.time import utc_now_iso

RESEARCH_POLICY_KEYS = {
    "research_mcp_enabled": True,
    "research_search_enabled": True,
    "research_browser_enabled": True,
}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def research_status(data_dir: Path) -> dict[str, Any]:
    """Return public-safe state: no credentials, URLs, profile names, or audit detail."""
    try:
        servers = configured_servers(data_dir)
        health_rows = server_health(data_dir, servers)
        mcp: dict[str, Any] = {
            "state": "configured" if servers else "not_configured",
            "configured_servers": len(servers),
            "healthy_servers": sum(
                1 for row in health_rows if row["health"].get("state") == "healthy"
            ),
            "servers": [
                {
                    "id": row["id"],
                    "enabled": row["enabled"],
                    "transport": row["transport"],
                    "health": row["health"].get("state", "unknown"),
                    "last_failure": row["health"].get("last_failure_reason"),
                    "limits": row["limits"],
                }
                for row in health_rows
            ],
        }
    except (ValueError, json.JSONDecodeError) as exc:
        mcp = {"state": "invalid_configuration", "reason": str(exc)[:240], "servers": []}
    search_health = _read_json(data_dir / "research_search_health.json").get("searxng") or {}
    browser_health = _read_json(data_dir / "browser_health.json").get("browser") or {}
    return {
        "updated_at": utc_now_iso(),
        "mcp": mcp,
        "search": {
            "state": search_health.get("state", "unknown"),
            "last_failure": search_health.get("reason"),
            "updated_at": search_health.get("updated_at"),
            "backend": search_backend(),
            "allowed_engines": list(allowed_search_engines()),
            "policy": "public retrieval only; snippets are discovery pointers",
        },
        "browser": {
            "state": browser_health.get("state", "unknown"),
            "last_failure": browser_health.get("reason"),
            "updated_at": browser_health.get("updated_at"),
            "policy": "isolated public sessions; named profiles require explicit authorization",
        },
        "policy": research_policy(data_dir),
        "alerts": research_alerts(data_dir),
    }


def research_policy(data_dir: Path) -> dict[str, Any]:
    """Read only non-secret, staged research enablement policy."""
    state = _read_json(_policy_path(data_dir))
    active = _normalized_policy(state.get("active"))
    last_good = _normalized_policy(state.get("last_known_good"))
    proposals = [
        _public_proposal(item)
        for item in state.get("proposals") or []
        if isinstance(item, dict) and item.get("status") == "pending"
    ]
    return {
        "active": active,
        "last_known_good": last_good,
        "pending_proposals": proposals[-10:],
        "updated_at": state.get("updated_at"),
    }


def stage_research_policy(
    data_dir: Path, values: dict[str, Any], *, actor: str
) -> dict[str, Any]:
    """Validate and stage a non-secret policy change without activating it."""
    changes = _validate_policy_values(values)
    path = _policy_path(data_dir)
    state = _read_json(path)
    active = _normalized_policy(state.get("active"))
    proposal = {
        "proposal_id": str(uuid4()),
        "status": "pending",
        "actor": actor,
        "created_at": utc_now_iso(),
        "previous": active,
        "proposed": {**active, **changes},
    }
    proposals = [item for item in state.get("proposals") or [] if isinstance(item, dict)]
    proposals.append(proposal)
    state.update({"active": active, "proposals": proposals[-50:], "updated_at": utc_now_iso()})
    _write_json(path, state)
    return _public_proposal(proposal)


def apply_research_policy(
    data_dir: Path,
    proposal_id: str,
    *,
    apply_values: Callable[[dict[str, bool]], None],
) -> dict[str, Any]:
    """Activate a staged change only after the runtime layer accepts it."""
    path = _policy_path(data_dir)
    state = _read_json(path)
    proposals = [item for item in state.get("proposals") or [] if isinstance(item, dict)]
    proposal = next(
        (
            item
            for item in proposals
            if item.get("proposal_id") == proposal_id and item.get("status") == "pending"
        ),
        None,
    )
    if proposal is None:
        raise ValueError("unknown or non-pending research policy proposal")
    previous = _normalized_policy(state.get("active"))
    proposed = _normalized_policy(proposal.get("proposed"))
    apply_values(proposed)
    proposal.update({"status": "applied", "applied_at": utc_now_iso()})
    state.update(
        {
            "active": proposed,
            "last_known_good": previous,
            "proposals": proposals,
            "updated_at": utc_now_iso(),
        }
    )
    _write_json(path, state)
    return {"proposal": _public_proposal(proposal), "active": proposed, "rollback_point": previous}


def rollback_research_policy(
    data_dir: Path, *, apply_values: Callable[[dict[str, bool]], None], actor: str
) -> dict[str, Any]:
    """Restore the last successfully applied non-secret policy."""
    path = _policy_path(data_dir)
    state = _read_json(path)
    current = _normalized_policy(state.get("active"))
    target = _normalized_policy(state.get("last_known_good"))
    if target == current:
        raise ValueError("no earlier research policy is available for rollback")
    apply_values(target)
    event = {
        "proposal_id": str(uuid4()),
        "status": "rolled_back",
        "actor": actor,
        "created_at": utc_now_iso(),
        "previous": current,
        "proposed": target,
    }
    proposals = [item for item in state.get("proposals") or [] if isinstance(item, dict)]
    proposals.append(event)
    state.update(
        {
            "active": target,
            "last_known_good": current,
            "proposals": proposals[-50:],
            "updated_at": utc_now_iso(),
        }
    )
    _write_json(path, state)
    return {"event": _public_proposal(event), "active": target, "rollback_point": current}


def research_audit(store: Any, *, limit: int = 100) -> dict[str, Any]:
    """Return a bounded, safe external-research audit projection for operators."""
    records: list[dict[str, Any]] = []
    for item in store.provenance_records():
        if item.get("node_type") not in {"mcp_server", "web_search", "browser", "research_control"}:
            continue
        records.append(
            {
                "at": item.get("created_at"),
                "correlation_id": item.get("record_id"),
                "principal": item.get("principal"),
                "tool": item.get("tool_name") or item.get("tool"),
                "policy_outcome": item.get("policy_outcome"),
                "source_url": item.get("source_url"),
                "final_url": item.get("final_url"),
                "document_id": item.get("document_id"),
                "failure_reason": item.get("failure_reason"),
            }
        )
    audit_path = store.data_dir / "browser_audit.jsonl"
    if audit_path.exists():
        try:
            lines = audit_path.read_text(encoding="utf-8").splitlines()[-max(1, min(limit, 500)) :]
        except OSError:
            lines = []
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            records.append(
                {
                    "at": item.get("at"),
                    "correlation_id": item.get("event_id"),
                    "principal": item.get("principal"),
                    "tool": item.get("event"),
                    "policy_outcome": item.get("policy_outcome"),
                    "source_url": item.get("url"),
                    "final_url": item.get("final_url"),
                    "document_id": None,
                    "failure_reason": item.get("reason"),
                }
            )
    records.sort(key=lambda item: str(item.get("at") or ""))
    return {"events": records[-max(1, min(limit, 500)) :]}


def reconcile_research_alert(
    data_dir: Path,
    *,
    rule: str,
    failed: bool,
    message: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Deduplicate one research incident and retain its recovery history."""
    path = data_dir / "research_alerts.json"
    state = _read_json(path)
    records = [item for item in state.get("records") or [] if isinstance(item, dict)]
    active = next(
        (
            item
            for item in reversed(records)
            if item.get("rule") == rule and item.get("status") != "resolved"
        ),
        None,
    )
    now = utc_now_iso()
    if failed:
        if active is not None:
            active["count"] = int(active.get("count") or 1) + 1
            active["last_seen"] = now
            active["correlation_id"] = correlation_id
            _write_json(path, {"records": records})
            return {**_public_alert(active), "new": False}
        record = {
            "alert_id": str(uuid4()),
            "rule": rule,
            "status": "active",
            "message": message[:240],
            "count": 1,
            "first_seen": now,
            "last_seen": now,
            "correlation_id": correlation_id,
        }
        records.append(record)
        _write_json(path, {"records": records[-200:]})
        return {**_public_alert(record), "new": True}
    if active is None:
        return {"rule": rule, "status": "clear", "new": False}
    active.update({"status": "resolved", "resolved_at": now, "resolution": "healthy check"})
    _write_json(path, {"records": records})
    return {**_public_alert(active), "new": False}


def acknowledge_research_alert(data_dir: Path, alert_id: str, *, actor: str) -> dict[str, Any]:
    path = data_dir / "research_alerts.json"
    state = _read_json(path)
    records = [item for item in state.get("records") or [] if isinstance(item, dict)]
    record = next((item for item in records if item.get("alert_id") == alert_id), None)
    if record is None:
        raise ValueError("unknown research alert")
    if record.get("status") == "resolved":
        raise ValueError("resolved research alerts cannot be acknowledged")
    record.update(
        {
            "status": "acknowledged",
            "acknowledged_at": utc_now_iso(),
            "acknowledged_by": actor,
        }
    )
    _write_json(path, {"records": records})
    return _public_alert(record)


def research_alerts(data_dir: Path) -> dict[str, Any]:
    records = [
        item
        for item in _read_json(data_dir / "research_alerts.json").get("records") or []
        if isinstance(item, dict)
    ]
    active = [_public_alert(item) for item in records if item.get("status") != "resolved"]
    return {"active": active[-50:], "history": [_public_alert(item) for item in records[-100:]]}


def record_citation_validation(
    store: Any,
    *,
    errors: list[str],
    principal: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Track citation-validator degradation without persisting answer contents."""
    alert = reconcile_research_alert(
        store.data_dir,
        rule="citation_validation",
        failed=bool(errors),
        message="citation validation failed: " + "; ".join(errors[:3]),
        correlation_id=correlation_id,
    )
    if alert.get("new"):
        store.add_alert(str(alert["message"]))
    store.add_provenance_record(
        {
            "record_id": correlation_id,
            "node_id": "citation_validation",
            "node_type": "research_control",
            "created_at": utc_now_iso(),
            "principal": principal,
            "tool": "citation_validator",
            "policy_outcome": "invalid" if errors else "valid",
            "failure_reason": "; ".join(errors[:3]) or None,
            "alert_id": alert.get("alert_id"),
        }
    )
    return alert


def test_research_component(data_dir: Path, component: str) -> dict[str, Any]:
    """Run one bounded, explicit health check and persist only a redacted outcome."""
    component = component.strip().lower()
    if component == "search":
        try:
            results, _ = searxng_search("OpenBrigade research health", limit=1)
            result = {"ok": True, "component": component, "result_count": len(results)}
            _write_health(data_dir / "research_search_health.json", "searxng", "healthy")
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "component": component, "reason": str(exc)[:240]}
            _write_health(
                data_dir / "research_search_health.json",
                "searxng",
                "degraded",
                redact_failure_reason(result["reason"]),
            )
        return {**result, "checked_at": utc_now_iso()}
    if component == "browser":
        worker_url = browser_worker_url()
        if not worker_url:
            result = {
                "ok": False,
                "component": component,
                "reason": "browser worker is not configured",
            }
            _write_health(data_dir / "browser_health.json", "browser", "degraded", result["reason"])
            return {**result, "checked_at": utc_now_iso()}
        request = urllib.request.Request(f"{worker_url}/healthz", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read(32_000).decode("utf-8", errors="replace"))
            result = {"ok": bool(payload.get("ok")), "component": component}
            _write_health(
                data_dir / "browser_health.json",
                "browser",
                "healthy" if result["ok"] else "degraded",
            )
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            result = {"ok": False, "component": component, "reason": str(exc)[:240]}
            _write_health(
                data_dir / "browser_health.json",
                "browser",
                "degraded",
                redact_failure_reason(result["reason"]),
            )
        return {**result, "checked_at": utc_now_iso()}
    if component == "mcp":
        try:
            servers = [server for server in configured_servers(data_dir) if server.enabled]
            results: list[dict[str, Any]] = []
            failed = False
            for server in servers:
                try:
                    tools = discover_tools(server, data_dir=data_dir)
                    record_server_health(data_dir, server, "healthy")
                    results.append({"id": server.id, "tool_count": len(tools), "ok": True})
                except Exception as exc:  # noqa: BLE001
                    reason = redact_failure_reason(str(exc))
                    record_server_health(data_dir, server, "degraded", reason)
                    results.append({"id": server.id, "ok": False, "reason": reason})
                    failed = True
            return {
                "ok": not failed,
                "component": component,
                "servers": results,
                "checked_at": utc_now_iso(),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "component": component,
                "reason": redact_failure_reason(str(exc)),
                "checked_at": utc_now_iso(),
            }
    raise ValueError("component must be mcp, search, or browser")


def _write_health(path: Path, key: str, state: str, reason: str | None = None) -> None:
    payload = _read_json(path)
    payload[key] = {"state": state, "reason": reason, "updated_at": utc_now_iso()}
    _write_json(path, payload)


def _policy_path(data_dir: Path) -> Path:
    return data_dir / "research_policy.json"


def _normalized_policy(raw: Any) -> dict[str, bool]:
    candidate = raw if isinstance(raw, dict) else {}
    return {
        key: value if isinstance(value := candidate.get(key, default), bool) else default
        for key, default in RESEARCH_POLICY_KEYS.items()
    }


def _validate_policy_values(values: dict[str, Any]) -> dict[str, bool]:
    if not isinstance(values, dict) or not values:
        raise ValueError("research policy requires at least one enablement value")
    unknown = sorted(set(values).difference(RESEARCH_POLICY_KEYS))
    if unknown:
        raise ValueError("research policy keys are not editable: " + ", ".join(unknown))
    invalid = sorted(key for key, value in values.items() if not isinstance(value, bool))
    if invalid:
        raise ValueError("research policy values must be booleans: " + ", ".join(invalid))
    return {key: values[key] for key in values}


def _public_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    return {
        key: proposal.get(key)
        for key in (
            "proposal_id",
            "status",
            "actor",
            "created_at",
            "applied_at",
            "previous",
            "proposed",
        )
        if key in proposal
    }


def _public_alert(alert: dict[str, Any]) -> dict[str, Any]:
    return {
        key: alert.get(key)
        for key in (
            "alert_id",
            "rule",
            "status",
            "message",
            "count",
            "first_seen",
            "last_seen",
            "correlation_id",
            "acknowledged_at",
            "acknowledged_by",
            "resolved_at",
            "resolution",
        )
        if key in alert
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
