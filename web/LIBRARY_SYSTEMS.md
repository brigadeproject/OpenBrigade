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
