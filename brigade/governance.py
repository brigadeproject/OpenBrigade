from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from brigade.schemas import Agent, build_proposal
from brigade.time import utc_now_iso
from brigade.workspace import agent_workspace_files

POLICY_CHANGE_PROPOSAL_KIND = "policy_change"
POLICY_PROJECTION_VERSION = 1
GOVERNING_ROOT_FILES = frozenset(
    {
        "AGENTS.md",
        "USER.md",
        "IDENTITY.md",
        "MEMORY.md",
        "TOOLS.md",
        "SOUL.md",
        "SKILLS.md",
    }
)
DAILY_MEMORY_RE = re.compile(r"^\d{8}-memory\.md$", re.IGNORECASE)


class PolicyProjectionStaleError(RuntimeError):
    pass


def normalize_workspace_relative_path(raw_path: str | Path) -> str:
    path = Path(str(raw_path))
    if path.is_absolute():
        raise ValueError("policy paths must be relative to the agent workspace")
    parts = [part for part in path.parts if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise ValueError("policy paths cannot contain '..'")
    return Path(*parts).as_posix() if parts else "."


def is_daily_memory_path(relative_path: str | Path) -> bool:
    try:
        normalized = normalize_workspace_relative_path(relative_path)
    except ValueError:
        return False
    path = Path(normalized)
    return len(path.parts) == 2 and path.parts[0] == "memory" and bool(
        DAILY_MEMORY_RE.match(path.name)
    )


def is_governing_workspace_path(relative_path: str | Path) -> bool:
    try:
        normalized = normalize_workspace_relative_path(relative_path)
    except ValueError:
        return False
    if is_daily_memory_path(normalized):
        return False
    path = Path(normalized)
    if len(path.parts) == 1 and path.name in GOVERNING_ROOT_FILES:
        return True
    return len(path.parts) >= 2 and path.parts[0] == "skills" and path.name == "SKILL.md"


def governing_file_kind(relative_path: str | Path) -> str:
    normalized = normalize_workspace_relative_path(relative_path)
    path = Path(normalized)
    if len(path.parts) == 1:
        return path.stem.lower()
    if path.parts[0] == "skills" and path.name == "SKILL.md":
        return "skill_procedure"
    return "workspace_policy"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def workspace_governing_paths(agent: Agent, workspace: Path) -> list[str]:
    paths = list(agent_workspace_files(agent))
    skills_dir = workspace / "skills"
    if skills_dir.exists():
        paths.extend(
            path.relative_to(workspace).as_posix()
            for path in sorted(skills_dir.glob("**/SKILL.md"))
            if path.is_file()
        )
    return sorted(dict.fromkeys(paths))


def build_policy_projection(
    agent: Agent,
    workspace: Path,
    relative_path: str | Path,
    *,
    actor: str,
    source: str,
) -> dict[str, Any]:
    normalized = normalize_workspace_relative_path(relative_path)
    path = workspace / normalized
    text = path.read_text(encoding="utf-8") if path.exists() and path.is_file() else ""
    now = utc_now_iso()
    return {
        "projection_id": f"{agent.agent_id}:{normalized}",
        "agent_id": agent.agent_id,
        "workspace_path": agent.workspace_path,
        "path": normalized,
        "file_kind": governing_file_kind(normalized),
        "content_hash": content_hash(text),
        "parsed_version": POLICY_PROJECTION_VERSION,
        "status": "active" if text else "missing",
        "updated_at": now,
        "projected_by": actor,
        "projection_source": source,
    }


def upsert_policy_projection(
    store: Any,
    agent: Agent,
    relative_path: str | Path,
    *,
    actor: str,
    source: str,
) -> dict[str, Any]:
    workspace = store.data_dir / agent.workspace_path
    projection = build_policy_projection(
        agent,
        workspace,
        relative_path,
        actor=actor,
        source=source,
    )
    store.upsert_policy_projection(projection)
    return projection


def reconcile_policy_projection_after_trusted_write(
    store: Any,
    agent: Agent,
    relative_path: str | Path,
    *,
    actor: str,
    source: str,
) -> dict[str, Any]:
    """Reconcile a governed file changed by an authorized runtime workflow.

    Operator acceptance remains an explicit ``agent:write`` action. This path is
    for trusted mutations that are already authorized by their owning workflow,
    such as rest-cycle curation or an explicit owner memory request.
    """
    normalized = normalize_workspace_relative_path(relative_path)
    if not is_governing_workspace_path(normalized):
        raise ValueError(f"{normalized} is not a governed workspace policy file")
    workspace = store.data_dir / agent.workspace_path
    current = build_policy_projection(
        agent,
        workspace,
        normalized,
        actor=actor,
        source=source,
    )
    existing = store.policy_projection(agent.agent_id, normalized)
    changed = existing is None or any(
        existing.get(key) != current.get(key)
        for key in ("content_hash", "parsed_version", "status")
    )
    if not changed:
        return existing

    store.upsert_policy_projection(current)
    store.add_provenance_record(
        {
            "record_id": (
                "policy_projection_reconciled:"
                f"{agent.agent_id}:{normalized}:{uuid4()}"
            ),
            "node_id": agent.agent_id,
            "node_type": "agent_policy_projection",
            "event": "policy_projection_reconciled",
            "agent_id": agent.agent_id,
            "path": normalized,
            "actor": actor,
            "source": source,
            "old_content_hash": existing.get("content_hash") if existing else None,
            "new_content_hash": current["content_hash"],
            "created_at": utc_now_iso(),
        }
    )
    return current


def ensure_policy_projections_current(
    store: Any,
    agent: Agent,
    *,
    actor: str = "runner",
    bootstrap_missing: bool = True,
) -> list[dict[str, Any]]:
    workspace = store.data_dir / agent.workspace_path
    stale: list[str] = []
    projections: list[dict[str, Any]] = []
    for relative_path in workspace_governing_paths(agent, workspace):
        current = build_policy_projection(
            agent,
            workspace,
            relative_path,
            actor=actor,
            source="validation",
        )
        existing = store.policy_projection(agent.agent_id, relative_path)
        if existing is None and bootstrap_missing:
            store.upsert_policy_projection(current)
            projections.append(current)
            continue
        if existing is None:
            stale.append(f"{relative_path}: missing projection")
            continue
        if (
            existing.get("content_hash") != current.get("content_hash")
            or existing.get("parsed_version") != current.get("parsed_version")
            or existing.get("status") != current.get("status")
        ):
            stale.append(f"{relative_path}: projection hash/version/status is stale")
    if stale:
        raise PolicyProjectionStaleError("; ".join(stale))
    return projections


def policy_projection_diff(store: Any, agent: Agent) -> list[dict[str, object]]:
    """Compare current governed files with their accepted projections."""
    workspace = store.data_dir / agent.workspace_path
    changes: list[dict[str, object]] = []
    for relative_path in workspace_governing_paths(agent, workspace):
        current = build_policy_projection(
            agent, workspace, relative_path, actor="policy_diff", source="validation"
        )
        existing = store.policy_projection(agent.agent_id, relative_path)
        changed = existing is None or any(
            existing.get(key) != current.get(key)
            for key in ("content_hash", "parsed_version", "status")
        )
        if changed:
            changes.append(
                {
                    "path": relative_path,
                    "old_content_hash": existing.get("content_hash") if existing else None,
                    "new_content_hash": current["content_hash"],
                    "old_status": existing.get("status") if existing else None,
                    "new_status": current["status"],
                }
            )
    return changes


def accept_policy_projections(
    store: Any,
    agent: Agent,
    *,
    paths: list[str],
    actor: str,
    source: str,
) -> list[dict[str, object]]:
    """Accept explicit current files as policy baselines without rewriting them."""
    if not paths:
        raise ValueError("at least one policy path must be selected")
    workspace = store.data_dir / agent.workspace_path
    accepted: list[dict[str, object]] = []
    for raw_path in dict.fromkeys(paths):
        relative_path = normalize_workspace_relative_path(raw_path)
        if not is_governing_workspace_path(relative_path):
            raise ValueError(f"{relative_path} is not a governed workspace policy file")
        path = workspace / relative_path
        if not path.is_file():
            raise ValueError(f"{relative_path} does not exist")
        existing = store.policy_projection(agent.agent_id, relative_path)
        projection = upsert_policy_projection(
            store, agent, relative_path, actor=actor, source=source
        )
        store.add_provenance_record(
            {
                "record_id": (
                    "policy_projection_accepted:"
                    f"{agent.agent_id}:{relative_path}:{uuid4()}"
                ),
                "node_id": agent.agent_id,
                "node_type": "agent_policy_projection",
                "event": "policy_projection_accepted",
                "agent_id": agent.agent_id,
                "path": relative_path,
                "actor": actor,
                "source": source,
                "old_content_hash": existing.get("content_hash") if existing else None,
                "new_content_hash": projection["content_hash"],
                "created_at": utc_now_iso(),
            }
        )
        accepted.append(projection)
    return accepted


def build_policy_change_proposal(
    store: Any,
    agent: Agent,
    *,
    relative_path: str,
    content: str,
    append: bool,
    purpose: str,
    source_tool: str,
) -> dict[str, Any]:
    normalized = normalize_workspace_relative_path(relative_path)
    if not is_governing_workspace_path(normalized):
        raise ValueError(f"{normalized} is not a governed workspace policy file")
    workspace = store.data_dir / agent.workspace_path
    path = workspace / normalized
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    next_content = (existing + content) if append else content
    old_hash = content_hash(existing) if path.exists() else None
    new_hash = content_hash(next_content)
    idempotency_key = f"policy-change:v1:{agent.agent_id}:{normalized}:{new_hash}"
    proposal = build_proposal(
        kind=POLICY_CHANGE_PROPOSAL_KIND,
        title=f"Change {normalized}",
        agent_id=agent.agent_id,
        team_id=agent.team_id,
        details={
            "path": normalized,
            "mode": "append" if append else "write",
            "purpose": purpose,
            "source_tool": source_tool,
            "proposed_content": content,
            "expected_old_hash": old_hash,
            "proposed_content_hash": new_hash,
            "file_kind": governing_file_kind(normalized),
        },
        idempotency_key=idempotency_key,
    )
    return store.add_proposal(proposal)


def apply_policy_change_proposal(store: Any, proposal: dict[str, Any]) -> dict[str, Any]:
    details = proposal.get("details") or {}
    agent_id = str(proposal.get("agent_id") or details.get("agent_id") or "").strip()
    agent = next((item for item in store.agents() if item.agent_id == agent_id), None)
    if agent is None:
        raise ValueError(f"policy change proposal has no known agent: {agent_id}")
    relative_path = normalize_workspace_relative_path(str(details.get("path") or ""))
    if not is_governing_workspace_path(relative_path):
        raise ValueError(f"{relative_path} is not a governed workspace policy file")
    content = str(details.get("proposed_content") or "")
    append = str(details.get("mode") or "write") == "append"
    workspace = store.data_dir / agent.workspace_path
    path = workspace / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    old_hash = content_hash(existing) if path.exists() else None
    expected_old_hash = details.get("expected_old_hash")
    if expected_old_hash is not None and expected_old_hash != old_hash:
        raise PolicyProjectionStaleError(
            f"{relative_path}: current file hash does not match proposal baseline"
        )
    next_content = (existing + content) if append else content
    tmp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    tmp_path.write_text(next_content, encoding="utf-8")
    os.replace(tmp_path, path)
    projection = upsert_policy_projection(
        store,
        agent,
        relative_path,
        actor=str(proposal.get("decided_by") or "governance"),
        source=f"proposal:{proposal.get('proposal_id')}",
    )
    audit = {
        "record_id": str(uuid4()),
        "node_type": "policy_file",
        "node_id": f"{agent.agent_id}:{relative_path}",
        "source_refs": [str(path)],
        "metadata": {
            "event": "policy_file_updated",
            "proposal_id": proposal.get("proposal_id"),
            "agent_id": agent.agent_id,
            "path": relative_path,
            "mode": "append" if append else "write",
            "old_hash": old_hash,
            "new_hash": projection["content_hash"],
            "decided_by": proposal.get("decided_by"),
        },
        "created_at": utc_now_iso(),
    }
    store.add_provenance_record(audit)
    return {
        "path": relative_path,
        "content_hash": projection["content_hash"],
        "projection_id": projection["projection_id"],
    }
