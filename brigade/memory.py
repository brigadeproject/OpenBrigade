from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from brigade.time import utc_now, utc_now_iso

MAX_MEMORY_BYTES = 2048


def append_daily_memory(workspace: Path, date_key: str, note: str) -> Path:
    memory_dir = workspace / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    path = memory_dir / f"{date_key}-MEMORY.md"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"- {note.strip()}\n")
    return path


def curate_memory(workspace: Path, promoted_notes: list[str]) -> Path:
    memory = workspace / "MEMORY.md"
    existing = memory.read_text(encoding="utf-8") if memory.exists() else "# Memory\n\n"
    additions = "".join(f"- {note.strip()}\n" for note in promoted_notes if note.strip())
    content = existing.rstrip() + "\n" + additions
    if len(content.encode("utf-8")) > MAX_MEMORY_BYTES:
        header = "# Memory\n\n"
        lines = [line for line in content.splitlines() if line.startswith("- ")]
        kept: list[str] = []
        size = len(header.encode("utf-8"))
        for line in reversed(lines):
            line_size = len((line + "\n").encode("utf-8"))
            if size + line_size > MAX_MEMORY_BYTES:
                break
            kept.append(line)
            size += line_size
        content = header + "\n".join(reversed(kept)) + "\n"
    memory.write_text(content, encoding="utf-8")
    return memory


def curate_workspace_memory(workspace: Path) -> Path:
    notes: list[str] = []
    for path in sorted((workspace / "memory").glob("*-MEMORY.md")):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                notes.append(stripped[2:])
    return curate_memory(workspace, notes[-50:])


def archive_stale_daily_memories(
    workspace: Path,
    agent_id: str,
    retention_days: int = 7,
) -> list[dict[str, object]]:
    archived: list[dict[str, object]] = []
    memory_dir = workspace / "memory"
    if not memory_dir.exists():
        return archived

    today = utc_now().date()
    for path in sorted(memory_dir.glob("*-MEMORY.md")):
        try:
            date_key = path.name.split("-", 1)[0]
            recorded_on = datetime.strptime(date_key, "%Y%m%d").date()
        except ValueError:
            continue
        if (today - recorded_on).days <= retention_days:
            continue
        notes = [
            line.strip()[2:]
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("- ")
        ]
        if not notes:
            path.unlink()
            continue
        archived.append(
            {
                "episode_id": str(uuid4()),
                "agent_id": agent_id,
                "source_kind": "daily_memory",
                "source_id": path.name,
                "summary": notes[-1],
                "learned_facts": notes[:10],
                "open_threads": [],
                "source_refs": [str(path)],
                "created_at": utc_now_iso(),
            }
        )
        path.unlink()
    return archived
