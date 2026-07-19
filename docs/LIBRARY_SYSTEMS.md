# OpenBrigade Library Systems

The library system turns operator-provided material into searchable records and provenance-linked
knowledge. v0.9 focuses on Markdown and plain text.

## Ingestion Inputs

Supported first-pass inputs:

- Markdown files.
- Plain text files.
- Operator-provided title, source, and type metadata.

Future v0.9.1+ inputs may include PDF, web pages, repository snapshots, and external connector
transcripts, but they should preserve the same document/chunk/provenance structure.

## Ingestion Flow

```text
source file -> knowledge document -> chunks -> provenance records -> optional Qdrant episodes
```

The operator path is:

```bash
./ops/brigade-live.sh knowledge ingest \
  --title "Reference notes" \
  --source local \
  --type note \
  --path ./notes/reference.md
```

When run through `./ops/brigade-live.sh`, host files are copied into the container before ingestion
so the live store owns the resulting records.

## Universal IDs (kb_id)

Every knowledge item carries a `kb_id` URI so the same object can be addressed across Postgres,
Qdrant, Neo4j, and the web API: `doc:<uuid>`, `chunk:<uuid>`, `episode:<uuid>`, `prov:<record_id>`,
`agent:<agent_id>`, `memory:<agent_id>/<filename>`, plus `task:/goal:/team:/decision:` for
provenance-referenced entities. `brigade/kb.py` owns the scheme and the single definition of
provenance relationships (`provenance_edges`), which both the Neo4j mirror and
`/api/knowledge/graph` consume. Ingestion stamps `document_id` (and `kb_id`) into chunks, the
derived episode, Qdrant payloads, and provenance metadata.

## Chunk Embeddings

Chunks are embedded into their own Qdrant collection (`brigade_chunks`, override with
`BRIGADE_QDRANT_CHUNK_COLLECTION`) at ingest time, using the same embedding surface as episodes.
Existing stores are indexed with:

```bash
./ops/brigade-live.sh knowledge backfill-embeddings --batch-size 32   # add --recreate to rebuild
```

The backfill skips already-indexed points; run it off-peak — the embedding Ollama instance is
shared. `/api/knowledge/overview` reports `chunk_backfill_pending` when Postgres and Qdrant drift.

## Web Fetch Persistence

`web_fetch` accepts `save_to_knowledge: true` to keep the fetched page as a knowledge document
(`document_type=web`, metadata: `source_url`, `http_final_url`, `fetched_at`, `content_hash`).
Operators can enable autosave for every successful fetch over 500 chars via the
`web_fetch_autosave` runtime override (GUI Telemetry tab). Saves dedupe on
`(source_url, content_hash)`; page bodies land in `<data_dir>/knowledge/web/<hash>.txt`. A failed
save never fails the fetch.

## Unified Read API and GUI

`/api/knowledge/*` (RBAC `knowledge:read`, read-only) serves every store through one namespace:
`overview`, `documents`, `episodes`, `graph` (provenance + episode + per-agent memory edges, with
`?document_id=` ego mode), `node/{kb_id}` (single inspector endpoint), `search` (vector with
keyword fallback and a `mode` field), and `neighbors` (Qdrant recommend → `SIMILAR_TO` similarity
edges). The Knowledge Base tab in the desktop GUI renders the graph (cytoscape), a browse/search
rail, and a per-kind inspector. When Qdrant or Neo4j are down everything degrades to
Postgres-only: search reports `mode: keyword`, neighbors return empty with a reason, and the
overview flags the store as down.

## Records

Document records include title, source, type, metadata, and chunk counts. Chunk records include
document ID, index, text, source reference, and provenance metadata. Neo4j records link documents to
chunks so operators can inspect where a later answer or decision drew context from.

## Boundaries

Library records are reference material, not agent identity. Ingested documents should not rewrite
`IDENTITY.md`, `TOOLS.md`, `SOUL.md`, or long-term memory files. If source material should influence
an agent, create an explicit assignment or goal that tells the agent how to evaluate it.

## Validation

Useful smoke checks:

```bash
./ops/brigade-live.sh knowledge list
./ops/brigade-live.sh datastore inspect --backend qdrant --limit 10
./ops/brigade-live.sh datastore inspect --backend neo4j --limit 10
```

A clean-stack sentinel pass should ingest one small document and confirm that document, chunk,
Qdrant, and Neo4j records survive non-dropping container recreation.
