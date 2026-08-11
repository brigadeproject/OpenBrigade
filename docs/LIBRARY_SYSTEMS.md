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

`web_search` performs a bounded public web search and returns titles, snippets, engines, ranks, and
source URLs. The app profile defaults to an internal SearXNG service (`BRIGADE_SEARCH_BACKEND` and
`BRIGADE_SEARXNG_URL`), with DuckDuckGo HTML as a fallback for local/manual runs. With
`save_to_knowledge: true`, it stores the result list as `document_type=web_search` with
`source_url`, `query`, `result_urls`, and `source_map` metadata so later answers can preserve where
discovered links came from.

The permitted SearXNG engines are an explicit deployment policy in
`BRIGADE_SEARCH_ALLOWED_ENGINES` (the bundled deployment's verified default: `google cse`). Test
engine availability in the target deployment before changing that list; engine identifiers are
SearXNG deployment-specific. Search records include the
engine, rank, result timestamp, available publication date/status, and source tier. Legal searches
prefer primary legal, court, and official-government material; Cornell Legal Information Institute
results are deliberately labelled `secondary_legal` interpretation, never primary authority.

`web_fetch` accepts `save_to_knowledge: true` to keep the fetched text or PDF as a knowledge
document (`document_type=web`, metadata: `source_url`, `http_final_url`, `fetched_at`,
`content_type`, `content_hash`, `source_map`). PDF extraction uses the optional `ingest` extra
(`pypdf`) and fetches up to 100 MB for PDF-to-text ingestion; without the extractor, PDF fetches fail
clearly instead of storing unreadable bytes. Operators can enable autosave for every successful
fetch over 500 chars via the `web_fetch_autosave` runtime override (GUI Telemetry tab). Saves dedupe
on `(source_url, content_hash)`; extracted text lands in `<data_dir>/knowledge/web/<hash>.txt`. A
failed save never fails the fetch.

Retained research sources default to `<data_dir>/knowledge`. Set the absolute
`BRIGADE_RESEARCH_STORAGE_PATH` to an operator-mounted local disk or NAS path to retain original
responses there instead. Every fetch stores the original file (`.pdf`, `.html`, or `.bin`) and a
separate parsed-text file for Postgres/Qdrant/Neo4j ingestion. PDF chunks retain page ranges when
the extractor can provide them. Search snippets are discovery-only and are not persisted as evidence;
saved search records retain source pointers, engine/rank, and source classification instead.

For an app-profile NAS mount, mount the same path into `brigade_orchestrator` and `brigade_web` and
set the container-side path, for example `BRIGADE_RESEARCH_STORAGE_PATH=/research`. A Compose
override can bind `/mnt/openbrigade-research:/research`; do not point the setting at a host path that
is not mounted inside those containers. The default `/data/knowledge` remains suitable for local
Docker-volume storage.

The versioned offline evaluation set is `brigade/fixtures/search_evaluation_v1.json`; run it with
`python3 -m brigade.search_evaluation --fixture brigade/fixtures/search_evaluation_v1.json`. It covers
US federal statutory, regulatory, court-material, academic-preprint, and general-research queries
and checks recall, precision, authority, freshness, and duplicate-rate thresholds. An explicit live
retrieval report (which does not change labels or claim a pass) is available with `--live-report
/tmp/openbrigade-search-evaluation.json`.

`browser_open`, `browser_click`, `browser_extract`, and `browser_screenshot` route through the
isolated Playwright worker (`BRIGADE_BROWSER_WORKER_URL`). The worker enforces public HTTP(S) DNS
validation for every request and redirect; rejects local files, non-HTTP(S) protocols, private,
loopback, link-local, multicast, and reserved targets; blocks downloads, uploads, permission prompts,
popups, and non-GET/HEAD requests; and bounds session lifetime, operations, request count, response
headers, rendered text, screenshots, concurrency, and queueing. Public sessions are disposable.

Named profiles are disabled unless `BRIGADE_BROWSER_PROFILE_POLICY` supplies explicit principals,
a TTL, and `encrypted_volume` or `external_provider` storage. The worker independently checks that
principal and offers `clear-profile` for revocation. Browser audit records are append-only at
`/data/browser_audit.jsonl`; they retain redacted URLs, profile/principal, policy outcome, and reason,
but not query parameters, page content, cookies, or credentials. A 429 means saturation; workers may
be restarted safely because public sessions are disposable. `browser_extract` can save rendered page
text as `document_type=web` with a `browser_extract` source map and optional retained HTML.

All ingested text chunks now carry `char_start` and `char_end`, mirrored into provenance metadata, so
a later answer can cite a document source map plus the specific chunk offsets that informed it.

## Citation-bearing answers

Citation enforcement is intentionally narrow: normal research and operational chat may remain plain
prose. A request for legal research or explicit citations becomes citation-bearing. Its `web_fetch`
and `browser_extract` calls retain the source automatically, and source facts must use a supplied
`[[cite:SOURCE_ID]]` marker. OpenBrigade validates the ID, URL, document identity/hash, and available
chunk/PDF locator, retries the final answer once, then returns an insufficient-evidence response rather
than silently removing a citation. The stored chat or assignment transcript records machine-readable
citations and claim classes: `source_fact`, `model_inference`, and `operator_assertion`.

Rendered footnotes link to the retrieval-time source URL and show access time plus a page or character
locator. Cornell Legal Information Institute content is labelled `secondary_legal` interpretation in
the footnote; it is not represented as primary authority. A refetch supersedes retrieval rather than
overwriting it, so the original retained file and content hash remain inspectable.

Freshness is enforced two ways so dated pages cannot linger in retrieval:

- **Supersede on refetch** — saving a URL whose content changed marks earlier versions
  `superseded_by`/`superseded_at` and deletes their chunks from Postgres and Qdrant. The document
  row, provenance, and derived episode are kept for audit (an episode summarizes what was learned
  at the time and stays true after the page changes). Only the newest version is ever retrievable.
- **TTL** — the `web_knowledge_max_age_days` runtime override (0 = disabled) excludes web chunks
  older than the window from every retrieval path: runner knowledge snippets, orchestrator
  targeting, vector search, and the KB API keyword scan. Expired documents remain visible in the
  Knowledge Base tab with a `stale` badge; local/non-web documents never expire.

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
