from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str


def chunk_text(text: str, max_chars: int = 1200, overlap: int = 120) -> list[Chunk]:
    clean = text.strip()
    if not clean:
        return []
    if max_chars <= overlap:
        raise ValueError("max_chars must be greater than overlap")

    chunks: list[Chunk] = []
    start = 0
    while start < len(clean):
        end = min(start + max_chars, len(clean))
        chunks.append(Chunk(index=len(chunks), text=clean[start:end]))
        if end == len(clean):
            break
        start = end - overlap
    return chunks
