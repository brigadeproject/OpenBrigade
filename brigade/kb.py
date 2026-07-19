"""Unified knowledge-base identifiers and link extraction.

Every inspectable knowledge item gets a kb_id URI of the form ``kind:rest``,
where ``rest`` may itself contain colons or slashes (goal statements, memory
file paths). The kinds in use:

- ``doc:<document_id>``
- ``chunk:<chunk_id>``
- ``episode:<episode_id>``
- ``prov:<record_id>``
- ``agent:<agent_id>``
- ``memory:<agent_id>/<filename>``
- ``task:<assignment_id>``, ``goal:<statement>``, ``team:<team_id>``,
  ``decision:<decision_id>`` for provenance-referenced entities.

``provenance_edges`` is the single definition of how a provenance record maps
to graph relationships; the Neo4j mirror and the /api/knowledge/graph endpoint
both derive their edges from it.
"""

from __future__ import annotations

from typing import Any

KB_KINDS = frozenset(
    {
        "doc",
        "chunk",
        "episode",
        "prov",
        "agent",
        "memory",
        "task",
        "goal",
        "team",
        "decision",
    }
)

_NODE_TYPE_KINDS = {
    "document": "doc",
    "chunk": "chunk",
    "episode": "episode",
    "task": "task",
    "decision": "decision",
    "team": "team",
    "agent": "agent",
    "goal": "goal",
}


def make_kb_id(kind: str, *parts: str) -> str:
    if kind not in KB_KINDS:
        raise ValueError(f"unknown kb_id kind: {kind}")
    rest = "/".join(str(part) for part in parts)
    if not rest:
        raise ValueError("kb_id requires at least one identifier part")
    return f"{kind}:{rest}"


def parse_kb_id(kb_id: str) -> tuple[str, str]:
    kind, sep, rest = str(kb_id).partition(":")
    if not sep or not rest or kind not in KB_KINDS:
        raise ValueError(f"invalid kb_id: {kb_id!r}")
    return kind, rest


def kind_for_node_type(node_type: str) -> str:
    return _NODE_TYPE_KINDS.get(str(node_type), "prov")


def provenance_edges(record: dict[str, Any]) -> list[dict[str, str]]:
    """Deterministic relationships implied by one provenance record.

    Mirrors Neo4jProvenanceStore._upsert_relationships: the returned edges are
    exactly the typed relationships the Neo4j store materializes, expressed as
    kb_id endpoints. The generic ProvenanceRecord-DESCRIBES->node backbone is
    included so record provenance stays visible without Neo4j.
    """
    node_type = str(record.get("node_type") or "")
    node_id = str(record.get("node_id") or "")
    record_id = str(record.get("record_id") or "")
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    if not node_id:
        return []

    edges: list[dict[str, str]] = []
    node_kb_id = make_kb_id(kind_for_node_type(node_type), node_id)
    if record_id:
        edges.append(
            {
                "source": make_kb_id("prov", record_id),
                "rel": "DESCRIBES",
                "target": node_kb_id,
            }
        )

    if node_type == "chunk" and metadata.get("document_id"):
        edges.append(
            {
                "source": make_kb_id("doc", str(metadata["document_id"])),
                "rel": "HAS_CHUNK",
                "target": make_kb_id("chunk", node_id),
            }
        )
    elif node_type == "task":
        if metadata.get("assigned_to"):
            edges.append(
                {
                    "source": node_kb_id,
                    "rel": "ASSIGNED_TO",
                    "target": make_kb_id("agent", str(metadata["assigned_to"])),
                }
            )
        if metadata.get("goal_statement"):
            edges.append(
                {
                    "source": node_kb_id,
                    "rel": "SUPPORTS_GOAL",
                    "target": make_kb_id("goal", str(metadata["goal_statement"])),
                }
            )
    elif node_type == "decision":
        for assignment_id in metadata.get("assignment_ids") or []:
            edges.append(
                {
                    "source": node_kb_id,
                    "rel": "CREATED_ASSIGNMENT",
                    "target": make_kb_id("task", str(assignment_id)),
                }
            )
    elif node_type == "team":
        for member_id in metadata.get("members") or []:
            edges.append(
                {
                    "source": node_kb_id,
                    "rel": "HAS_MEMBER",
                    "target": make_kb_id("agent", str(member_id)),
                }
            )
        if metadata.get("crew_chief_id"):
            edges.append(
                {
                    "source": node_kb_id,
                    "rel": "LED_BY",
                    "target": make_kb_id("agent", str(metadata["crew_chief_id"])),
                }
            )
        if metadata.get("parent_team_id"):
            edges.append(
                {
                    "source": node_kb_id,
                    "rel": "CHILD_OF",
                    "target": make_kb_id("team", str(metadata["parent_team_id"])),
                }
            )
    return edges
