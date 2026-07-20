"""Unified knowledge-base read API.

Serves every knowledge/memory surface through one namespace: Postgres
documents/chunks/episodes/provenance (source of truth), Qdrant vector search
and neighbors, and per-agent workspace memory files exposed as live virtual
nodes. Everything degrades to Postgres-only when the external stores are down.
"""

from __future__ import annotations

import re
from typing import Any

from brigade.config import Settings
from brigade.kb import make_kb_id, parse_kb_id, provenance_edges
from brigade.knowledge import active_knowledge_chunks, web_chunk_expired, web_knowledge_max_age_days
from brigade.store import StateStore

DEFAULT_GRAPH_NODE_LIMIT = 300
MAX_GRAPH_NODE_LIMIT = 1000
MEMORY_FILE_CONTENT_CAP = 16_384
_DAILY_MEMORY_PATTERN = re.compile(r"^memory/\d{8}-MEMORY\.md$")
_MEMORY_ROOT_FILES = ("MEMORY.md", "CHAT_MEMORY.md")

_KIND_LABELS = {
    "doc": "document",
    "chunk": "chunk",
    "episode": "episode",
    "prov": "provenance",
    "agent": "agent",
    "memory": "memory",
    "task": "task",
    "goal": "goal",
    "team": "team",
    "decision": "decision",
}


def _clip(text: object, limit: int = 80) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _memory_filename_ok(filename: str) -> bool:
    return filename in _MEMORY_ROOT_FILES or bool(_DAILY_MEMORY_PATTERN.match(filename))


def _agent_memory_files(settings: Settings, agent: Any) -> list[dict[str, object]]:
    workspace = settings.data_dir / agent.workspace_path
    files: list[dict[str, object]] = []
    candidates = [workspace / name for name in _MEMORY_ROOT_FILES]
    memory_dir = workspace / "memory"
    if memory_dir.is_dir():
        candidates.extend(sorted(memory_dir.glob("*-MEMORY.md")))
    for path in candidates:
        if not path.is_file():
            continue
        relative = path.relative_to(workspace).as_posix()
        if not _memory_filename_ok(relative):
            continue
        stat = path.stat()
        files.append(
            {
                "filename": relative,
                "kb_id": make_kb_id("memory", agent.agent_id, relative),
                "size_bytes": stat.st_size,
                "modified_at": stat.st_mtime,
            }
        )
    return files


def _document_flags(store: StateStore, document: dict[str, Any]) -> dict[str, bool]:
    metadata = document.get("metadata") or {}
    stale = False
    max_age_days = web_knowledge_max_age_days(store)
    if max_age_days > 0 and document.get("document_type") == "web":
        stale = web_chunk_expired(
            {
                "document_type": "web",
                "created_at": metadata.get("fetched_at") or document.get("ingested_at"),
            },
            max_age_days,
        )
    return {"superseded": bool(metadata.get("superseded_by")), "stale": stale}


def _document_related(
    store: StateStore, document_id: str
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, list[dict[str, Any]]]:
    chunks = store.knowledge_chunks(document_id)
    episode = next(
        (
            item
            for item in store.episodes()
            if item.get("document_id") == document_id
            or item.get("source_id") == document_id
        ),
        None,
    )
    provenance = [
        record
        for record in store.provenance_records()
        if record.get("node_id") == document_id
        or (record.get("metadata") or {}).get("document_id") == document_id
    ]
    return chunks, episode, provenance


def register_knowledge_routes(
    app: Any,
    *,
    store: StateStore,
    settings: Settings,
    require: Any,
    auth_dependency: Any,
) -> None:
    from fastapi import HTTPException
    from brigade.auth import AuthResult

    def _find_document(document_id: str) -> dict[str, Any]:
        document = next(
            (
                item
                for item in store.knowledge_documents()
                if item.get("document_id") == document_id
            ),
            None,
        )
        if document is None:
            raise HTTPException(status_code=404, detail="unknown document")
        return document

    def _qdrant_stats() -> dict[str, object]:
        try:
            return store.qdrant_collection_stats()
        except RuntimeError as exc:
            return {"configured": False, "reason": str(exc)}

    def _document_payload(document_id: str) -> dict[str, Any]:
        document = _find_document(document_id)
        chunks, episode, provenance = _document_related(store, document_id)
        stats = _qdrant_stats()
        return {
            "kb_id": make_kb_id("doc", document_id),
            "document": document,
            **_document_flags(store, document),
            "chunks": chunks,
            "episode": episode,
            "provenance": provenance,
            "vectors": {
                "configured": bool(stats.get("configured")),
                "chunk_count": len(chunks),
                "episode_indexed": episode is not None and bool(stats.get("configured")),
            },
        }

    @app.get("/api/knowledge/overview")
    async def knowledge_overview(
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        require("knowledge:read", current)
        documents = store.knowledge_documents()
        chunks = store.knowledge_chunks()
        episodes = store.episodes()
        provenance = store.provenance_records()
        datastores = store.external_datastore_status()
        stats = _qdrant_stats()
        chunk_points = stats.get("chunk_points")
        memory_agents = []
        for agent in store.agents():
            files = _agent_memory_files(settings, agent)
            memory_agents.append(
                {
                    "agent_id": agent.agent_id,
                    "kb_id": make_kb_id("agent", agent.agent_id),
                    "files": files,
                }
            )
        return {
            "postgres": {
                "documents": len(documents),
                "chunks": len(chunks),
                "episodes": len(episodes),
                "provenance_records": len(provenance),
            },
            "qdrant": {
                **(datastores.get("qdrant") or {}),
                **stats,
                "chunk_backfill_pending": (
                    max(0, len(chunks) - int(chunk_points))
                    if isinstance(chunk_points, int)
                    else None
                ),
            },
            "neo4j": datastores.get("neo4j") or {},
            "memory": {"agents": memory_agents},
        }

    @app.get("/api/knowledge/documents")
    async def knowledge_documents(
        limit: int = 50,
        offset: int = 0,
        type: str | None = None,
        q: str | None = None,
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        require("knowledge:read", current)
        documents = store.knowledge_documents()
        if type:
            documents = [item for item in documents if item.get("document_type") == type]
        if q:
            needle = q.lower()
            documents = [
                item
                for item in documents
                if needle in str(item.get("title") or "").lower()
                or needle in str(item.get("source") or "").lower()
            ]
        documents = list(reversed(documents))  # newest first
        total = len(documents)
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        page = documents[offset : offset + limit]
        for item in page:
            item["kb_id"] = make_kb_id("doc", str(item.get("document_id")))
            item.update(_document_flags(store, item))
        return {"total": total, "limit": limit, "offset": offset, "documents": page}

    @app.get("/api/knowledge/documents/{document_id}")
    async def knowledge_document_detail(
        document_id: str,
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        require("knowledge:read", current)
        return _document_payload(document_id)

    @app.get("/api/knowledge/episodes")
    async def knowledge_episodes(
        limit: int = 50,
        agent_id: str | None = None,
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        require("knowledge:read", current)
        episodes = store.episodes()
        if agent_id:
            episodes = [item for item in episodes if item.get("agent_id") == agent_id]
        episodes = list(reversed(episodes))
        total = len(episodes)
        page = episodes[: max(1, min(limit, 200))]
        for item in page:
            item.setdefault("kb_id", make_kb_id("episode", str(item.get("episode_id"))))
        return {"total": total, "episodes": page}

    @app.get("/api/knowledge/episodes/{episode_id}")
    async def knowledge_episode_detail(
        episode_id: str,
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        require("knowledge:read", current)
        episode = next(
            (
                item
                for item in store.episodes()
                if str(item.get("episode_id")) == episode_id
            ),
            None,
        )
        if episode is None:
            raise HTTPException(status_code=404, detail="unknown episode")
        return {"kb_id": make_kb_id("episode", episode_id), "episode": episode}

    @app.get("/api/knowledge/graph")
    async def knowledge_graph(
        limit: int = DEFAULT_GRAPH_NODE_LIMIT,
        types: str | None = None,
        document_id: str | None = None,
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        require("knowledge:read", current)
        limit = max(10, min(limit, MAX_GRAPH_NODE_LIMIT))
        wanted = (
            {part.strip() for part in types.split(",") if part.strip()}
            if types
            else None
        )
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, str]] = []
        edge_keys: set[tuple[str, str, str]] = set()
        truncated = False

        labels: dict[str, str] = {}
        documents = store.knowledge_documents()
        titles: dict[str, str] = {}
        for item in documents:
            doc_id = str(item.get("document_id"))
            titles[doc_id] = str(item.get("title") or "")
            labels[make_kb_id("doc", doc_id)] = _clip(item.get("title"), 60)
        for chunk in store.knowledge_chunks():
            title = titles.get(str(chunk.get("document_id")), "")
            labels[make_kb_id("chunk", str(chunk.get("chunk_id")))] = _clip(
                f"{title} · chunk {chunk.get('chunk_index')}", 60
            )

        def add_node(kb_id: str) -> bool:
            if kb_id in nodes:
                return True
            nonlocal truncated
            if len(nodes) >= limit:
                truncated = True
                return False
            kind, rest = parse_kb_id(kb_id)
            nodes[kb_id] = {
                "id": kb_id,
                "kind": _KIND_LABELS.get(kind, kind),
                "label": labels.get(kb_id) or _clip(rest, 60),
            }
            return True

        def add_edge(source: str, rel: str, target: str, origin: str) -> None:
            key = (source, rel, target)
            if key in edge_keys:
                return
            source_kind = parse_kb_id(source)[0]
            target_kind = parse_kb_id(target)[0]
            if wanted is not None and not (
                source_kind in wanted and target_kind in wanted
            ):
                return
            if not add_node(source) or not add_node(target):
                return
            edge_keys.add(key)
            edges.append(
                {"source": source, "rel": rel, "target": target, "origin": origin}
            )

        if document_id is not None:
            # Ego graph for one document: the document, its chunks, and the
            # derived episode.
            document = _find_document(document_id)
            doc_kb = make_kb_id("doc", document_id)
            labels[doc_kb] = _clip(document.get("title"), 60)
            add_node(doc_kb)
            chunks, episode, provenance = _document_related(store, document_id)
            for chunk in chunks:
                chunk_kb = make_kb_id("chunk", str(chunk.get("chunk_id")))
                labels[chunk_kb] = f"chunk {chunk.get('chunk_index')}"
                add_edge(doc_kb, "HAS_CHUNK", chunk_kb, "provenance")
            if episode is not None:
                episode_kb = make_kb_id("episode", str(episode.get("episode_id")))
                labels[episode_kb] = _clip(episode.get("summary"), 60)
                add_node(episode_kb)
                add_edge(episode_kb, "DERIVED_FROM", doc_kb, "provenance")
            return {
                "nodes": list(nodes.values()),
                "edges": edges,
                "truncated": truncated,
            }

        provenance = store.provenance_records()
        for record in reversed(provenance):  # newest first under the node cap
            for edge in provenance_edges(record):
                if edge["rel"] == "DESCRIBES":
                    continue  # record backbone stays in the inspector, not the graph
                add_edge(edge["source"], edge["rel"], edge["target"], "provenance")

        for episode in reversed(store.episodes()):
            source_document = episode.get("document_id") or episode.get("source_id")
            if not source_document:
                continue
            episode_kb = make_kb_id("episode", str(episode.get("episode_id")))
            labels[episode_kb] = _clip(episode.get("summary"), 60)
            add_edge(
                episode_kb,
                "DERIVED_FROM",
                make_kb_id("doc", str(source_document)),
                "provenance",
            )

        for agent in store.agents():
            agent_kb = make_kb_id("agent", agent.agent_id)
            for entry in _agent_memory_files(settings, agent):
                add_edge(agent_kb, "HAS_MEMORY", str(entry["kb_id"]), "memory")

        return {"nodes": list(nodes.values()), "edges": edges, "truncated": truncated}

    @app.get("/api/knowledge/node/{kb_id:path}")
    async def knowledge_node(
        kb_id: str,
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        require("knowledge:read", current)
        try:
            kind, rest = parse_kb_id(kb_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid kb_id") from None
        if kind == "doc":
            return {"kind": "document", **_document_payload(rest)}
        if kind == "chunk":
            chunk = next(
                (
                    item
                    for item in store.knowledge_chunks()
                    if str(item.get("chunk_id")) == rest
                ),
                None,
            )
            if chunk is None:
                raise HTTPException(status_code=404, detail="unknown chunk")
            document_id = str(chunk.get("document_id") or "")
            document = next(
                (
                    item
                    for item in store.knowledge_documents()
                    if item.get("document_id") == document_id
                ),
                None,
            )
            return {
                "kind": "chunk",
                "kb_id": kb_id,
                "chunk": chunk,
                "document": {
                    "document_id": document_id,
                    "title": (document or {}).get("title"),
                    "kb_id": make_kb_id("doc", document_id) if document_id else None,
                },
            }
        if kind == "episode":
            episode = next(
                (
                    item
                    for item in store.episodes()
                    if str(item.get("episode_id")) == rest
                ),
                None,
            )
            if episode is None:
                raise HTTPException(status_code=404, detail="unknown episode")
            return {"kind": "episode", "kb_id": kb_id, "episode": episode}
        if kind == "prov":
            record = next(
                (
                    item
                    for item in store.provenance_records()
                    if str(item.get("record_id")) == rest
                ),
                None,
            )
            if record is None:
                raise HTTPException(status_code=404, detail="unknown provenance record")
            return {"kind": "provenance", "kb_id": kb_id, "record": record}
        if kind == "memory":
            agent_id, _, filename = rest.partition("/")
            if not agent_id or not _memory_filename_ok(filename):
                raise HTTPException(status_code=400, detail="invalid memory kb_id")
            agent = next(
                (item for item in store.agents() if item.agent_id == agent_id), None
            )
            if agent is None:
                raise HTTPException(status_code=404, detail="unknown agent")
            path = settings.data_dir / agent.workspace_path / filename
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                raise HTTPException(status_code=404, detail="memory file not found") from None
            stat = path.stat()
            return {
                "kind": "memory",
                "kb_id": kb_id,
                "agent_id": agent_id,
                "filename": filename,
                "content": content[:MEMORY_FILE_CONTENT_CAP],
                "truncated": len(content) > MEMORY_FILE_CONTENT_CAP,
                "size_bytes": stat.st_size,
                "modified_at": stat.st_mtime,
            }
        if kind == "agent":
            agent = next(
                (item for item in store.agents() if item.agent_id == rest), None
            )
            if agent is None:
                raise HTTPException(status_code=404, detail="unknown agent")
            return {
                "kind": "agent",
                "kb_id": kb_id,
                "agent": agent.to_dict(),
                "memory_files": _agent_memory_files(settings, agent),
            }
        # task/goal/team/decision: surface whatever provenance describes them.
        related = [
            record
            for record in store.provenance_records()
            if str(record.get("node_id")) == rest
        ]
        return {"kind": _KIND_LABELS.get(kind, kind), "kb_id": kb_id, "records": related}

    @app.get("/api/knowledge/search")
    async def knowledge_search(
        q: str,
        limit: int = 10,
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        require("knowledge:read", current)
        limit = max(1, min(limit, 50))
        stats = _qdrant_stats()
        vector_mode = bool(stats.get("configured"))
        episodes = store.search_episodes(q, limit=limit)
        chunks = store.search_chunks(q, limit=limit)
        if vector_mode and not chunks:
            # Qdrant configured but returned nothing (down, or not backfilled):
            # fall back to a keyword scan so the KB stays searchable.
            chunks = _keyword_chunk_scan(store, q, limit)
            if chunks:
                vector_mode = False
        elif not vector_mode and not chunks:
            chunks = _keyword_chunk_scan(store, q, limit)
        for row in episodes:
            payload = row.get("payload") or {}
            if payload.get("episode_id"):
                payload.setdefault(
                    "kb_id", make_kb_id("episode", str(payload["episode_id"]))
                )
        for row in chunks:
            payload = row.get("payload") or {}
            if payload.get("chunk_id"):
                payload.setdefault("kb_id", make_kb_id("chunk", str(payload["chunk_id"])))
        return {
            "mode": "vector" if vector_mode else "keyword",
            "episodes": episodes,
            "chunks": chunks,
        }

    @app.get("/api/knowledge/neighbors")
    async def knowledge_neighbors(
        kb_id: str,
        limit: int = 8,
        current: AuthResult = auth_dependency,
    ) -> dict[str, object]:
        require("knowledge:read", current)
        try:
            kind, rest = parse_kb_id(kb_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid kb_id") from None
        limit = max(1, min(limit, 25))
        if kind == "chunk":
            rows = store.chunk_neighbors(rest, limit=limit)
        elif kind == "episode":
            rows = store.episode_neighbors(rest, limit=limit)
        else:
            return {"edges": [], "nodes": [], "reason": f"no vectors for kind {kind}"}
        edges = []
        nodes = []
        for row in rows:
            payload = row.get("payload") or {}
            target_id = payload.get("kb_id")
            if not target_id:
                point = payload.get("chunk_id") or payload.get("episode_id")
                if not point:
                    continue
                target_id = make_kb_id(kind, str(point))
            if target_id == kb_id:
                continue
            edges.append(
                {
                    "source": kb_id,
                    "target": target_id,
                    "rel": "SIMILAR_TO",
                    "score": row.get("score"),
                    "origin": "similarity",
                }
            )
            nodes.append(
                {
                    "id": target_id,
                    "kind": _KIND_LABELS.get(kind, kind),
                    "label": _clip(
                        payload.get("text") or payload.get("summary") or target_id, 60
                    ),
                }
            )
        reason = None
        if not edges:
            stats = _qdrant_stats()
            reason = (
                "no similar points found"
                if stats.get("configured")
                else "qdrant is not configured"
            )
        return {"edges": edges, "nodes": nodes, "reason": reason}


def _keyword_chunk_scan(
    store: StateStore, query: str, limit: int
) -> list[dict[str, Any]]:
    terms = [term.lower() for term in query.split() if len(term) >= 3]
    if not terms:
        return []
    matches: list[dict[str, Any]] = []
    for chunk in active_knowledge_chunks(store):
        text = str(chunk.get("text") or "").lower()
        if any(term in text for term in terms):
            matches.append({"score": None, "payload": chunk})
            if len(matches) >= limit:
                break
    return matches
