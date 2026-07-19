# OpenBrigade Memory Architecture

OpenBrigade separates raw operational records from curated memory. The intent is to preserve audit
truth while keeping retrieval concise enough for agent context.

## Memory Layers

- Raw transcripts: stored durably as transcript records and files, linked to assignment IDs.
- Chat messages: stored as channel messages with sender, recipient, content, timestamp, and audit
  metadata.
- Daily workspace notes: operator or agent-authored notes in agent workspaces.
- Curated episodes: concise summaries with source references, stored in Postgres and Qdrant.
- Knowledge chunks: document-derived chunks with provenance records.
- Provenance graph: Neo4j links among documents, chunks, assignments, goals, agents, teams, and
  decisions.

Raw transcripts are not vector memory. Qdrant receives curated episode summaries and source
metadata, not unfiltered transcript dumps.

## Creation Paths

Assignment completion creates:

- Transcript record.
- Usage record.
- Assignment history record when complete/failed/abandoned.
- Financial report update.
- Optional curated episode depending on workflow.

User chat creates:

- Request message.
- Response message.
- Usage record.
- Curated episode with request/response summary.

Knowledge ingestion creates:

- Knowledge document record.
- Knowledge chunk records.
- Qdrant episode records where applicable.
- Neo4j document/chunk provenance relationships.

Memory archive creates:

- Episodic summaries from stale daily memory notes.
- Qdrant records with agent/source/date metadata.
- Provenance records tying the episode back to its source.

## Unified Knowledge Base View (1.2)

All memory layers are inspectable through `/api/knowledge/*` and the GUI Knowledge Base tab.
Items are addressed by `kb_id` URIs (see LIBRARY_SYSTEMS.md). Per-agent memory files
(`MEMORY.md`, `CHAT_MEMORY.md`, `memory/<date>-MEMORY.md`) are exposed as live virtual nodes
(`agent:<id>` −HAS_MEMORY→ `memory:<id>/<file>`) read straight from the workspace — they are not
copied into the database, so the existing file-edit API remains the single write path. Knowledge
chunks are embedded in the `brigade_chunks` Qdrant collection alongside episode vectors, and the
graph view shows structural provenance edges plus view-time `SIMILAR_TO` edges from Qdrant
nearest-neighbor lookups.

## Retrieval and Inspection

Current operator inspection commands:

```bash
./ops/brigade-live.sh status --json
./ops/brigade-live.sh dashboard --plain --view mission
./ops/brigade-live.sh chat list --channel <channel>
./ops/brigade-live.sh knowledge list
./ops/brigade-live.sh datastore inspect --backend qdrant --limit 10
./ops/brigade-live.sh datastore inspect --backend neo4j --limit 10
```

Prompt construction should draw from mission, active goals, current assignment, selected memory
summaries, and relevant knowledge snippets. It should not push whole transcripts into routine model
context.

## Retention

Runtime state is disposable until the PR-candidate pass is complete, but state transitions should be
auditable while a stack is running. Before destructive wipe/reseed tests, create a backup. If a test
artifact becomes valuable, export it deliberately rather than preserving the entire accumulated test
store.

## Failure Behavior

Postgres write failures block memory creation. Qdrant and Neo4j write failures should produce alerts
without losing the canonical Postgres event. Recovery checks should confirm that Qdrant samples,
Neo4j samples, and Redis queue state survive non-dropping container recreation.
