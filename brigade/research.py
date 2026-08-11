from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from brigade.time import utc_now_iso

# This is the engine identifier exposed by the bundled SearXNG deployment.
# Other deployments must set BRIGADE_SEARCH_ALLOWED_ENGINES after a live probe.
DEFAULT_SEARCH_ENGINES = ("google cse",)


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    engine: str = ""
    rank: int = 0
    published_at: str | None = None
    publication_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "engine": self.engine,
            "rank": self.rank,
            "published_at": self.published_at,
            "publication_status": self.publication_status,
        }


def search_backend() -> str:
    return os.environ.get("BRIGADE_SEARCH_BACKEND", "searxng").strip().lower() or "searxng"


def searxng_url() -> str:
    return os.environ.get("BRIGADE_SEARXNG_URL", "http://brigade_searxng:8080").rstrip("/")


def allowed_search_engines() -> tuple[str, ...]:
    """The intentionally small engine set used for bounded public research."""
    configured = os.environ.get("BRIGADE_SEARCH_ALLOWED_ENGINES", "").strip()
    engines = configured.split(",") if configured else list(DEFAULT_SEARCH_ENGINES)
    return tuple(engine.strip().lower() for engine in engines if engine.strip())


def browser_worker_url() -> str | None:
    raw = os.environ.get("BRIGADE_BROWSER_WORKER_URL", "http://brigade_browser:8765")
    return raw.rstrip("/") if raw.strip() else None


def source_map(
    *,
    source_url: str,
    final_url: str | None = None,
    title: str = "",
    retrieval_tool: str,
    content_type: str = "",
    content_hash: str = "",
    byte_size: int | None = None,
    quote: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "source_url": source_url,
        "final_url": final_url or source_url,
        "title": title,
        "accessed_at": utc_now_iso(),
        "retrieval_tool": retrieval_tool,
        "content_type": content_type,
        "content_hash": content_hash,
    }
    if byte_size is not None:
        data["byte_size"] = byte_size
    if quote:
        data["quote"] = quote[:500]
    if extra:
        data.update(extra)
    return data


def searxng_search(query: str, *, limit: int) -> tuple[list[SearchResult], str]:
    engines = allowed_search_engines()
    params: dict[str, object] = {
        "q": query,
        "format": "json",
        "language": "en-US",
        "safesearch": 1,
    }
    if engines:
        params["engines"] = ",".join(engines)
    url = searxng_url() + "/search?" + urllib.parse.urlencode(
        params
    )
    request = urllib.request.Request(url, headers={"User-Agent": "OpenBrigade/1.3"})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read(1_000_000).decode("utf-8", errors="replace"))
        final_url = response.geturl() if hasattr(response, "geturl") else url
    results: list[SearchResult] = []
    for item in payload.get("results") or []:
        target = str(item.get("url") or "").strip()
        if not target.startswith(("http://", "https://")):
            continue
        results.append(
            SearchResult(
                title=str(item.get("title") or target).strip(),
                url=target,
                snippet=str(item.get("content") or item.get("snippet") or "").strip(),
                engine=str(item.get("engine") or ""),
                rank=len(results) + 1,
                published_at=str(
                    item.get("publishedDate")
                    or item.get("published_date")
                    or item.get("date")
                    or ""
                )
                or None,
                publication_status=str(item.get("publication_status") or "") or None,
            )
        )
        if len(results) >= limit:
            break
    return results, final_url


def search_with_retry(
    query: str,
    *,
    limit: int,
    attempts: int = 2,
    search: Callable[[str], tuple[list[SearchResult], str]] | None = None,
) -> tuple[list[SearchResult], str]:
    """Bound retries for a degraded SearXNG dependency without masking failure."""
    last_error: Exception | None = None
    for attempt in range(max(1, min(attempts, 3))):
        try:
            return (search or (lambda value: searxng_search(value, limit=limit)))(query)
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.1 * (attempt + 1))
    raise RuntimeError("SearXNG search unavailable") from last_error


def record_search_health(
    data_dir: Path,
    *,
    backend: str,
    state: str,
    reason: str | None = None,
) -> None:
    """Persist one non-secret backend state for later operator telemetry."""
    path = Path(data_dir) / "research_search_health.json"
    try:
        records = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        records = {}
    records[backend] = {
        "state": state,
        "reason": (reason or "")[:240] or None,
        "updated_at": utc_now_iso(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def call_browser_worker(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    base = browser_worker_url()
    if not base:
        return {"ok": False, "error": "browser worker is not configured"}
    request = urllib.request.Request(
        f"{base}/{action.lstrip('/')}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "OpenBrigade/1.3"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.loads(response.read(2_000_000).decode("utf-8", errors="replace"))
    except urllib.error.URLError as exc:
        return {"ok": False, "error": str(exc)}
