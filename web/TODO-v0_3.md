# v0.3 Memory and Knowledge

Research notes for the `TODO.md` v0.3 bullets. Scope: explain what each item means for OpenBrigade, cite useful reference snippets, and identify the nearest reference when no exact implementation exists.

## Store raw transcripts in Postgres

OpenBrigade should persist full conversation and dispatch transcripts durably in PostgreSQL, keyed by stable IDs such as `session_id`, `assignment_id`, `agent_id`, `message_id`, role, timestamp, and source. This keeps auditability and replay separate from retrieval-time vector memory.

Useful references:

- `OpenBrigade-Concept.md:163` says PostgreSQL owns completed assignment history, audit records, outcomes, and dispatch transcripts indexed by `assignment_id`.
- `reference/agent-system-design-v1.3.md:112` repeats the same split: Redis for active runtime state, PostgreSQL for completed history, audit records, assignment outcomes, and dispatch transcripts.
- `reference/pixelagent/pixelagent/core/base.py:84` creates a memory table with `message_id`, `role`, `content`, and `timestamp`; `reference/pixelagent/pixelagent/core/base.py:158` and `reference/pixelagent/pixelagent/core/base.py:191` insert user and assistant messages separately.

Why useful: Pixelagent is not Postgres, but its table shape is the nearest concrete code example for raw message persistence. Use the schema idea, not its storage engine.

## Store structured episodic summaries in Qdrant

Qdrant should receive compact, structured episodic records produced by a summarization/extraction step, not whole chats. Records should include session/task identity, goals set/completed/abandoned, decisions and rationales, completed tasks, open threads, learned facts, participating agents, and source pointers back to Postgres rows.

Useful references:

- `reference/agent-system-design-v1.3.md:402` specifies a dedicated LLM call before anything reaches Qdrant.
- `reference/agent-system-design-v1.3.md:404` gives the target JSON shape, including `session_id`, `timestamp`, goals, decisions, tasks, open threads, learned items, and relationship notes.
- `reference/agent-system-design-v1.3.md:425` states the rule directly: raw conversation goes to PostgreSQL, while Qdrant holds structured episodic records or summaries.
- `reference/mempalace/mempalace/general_extractor.py:3` is a useful non-LLM nearest reference for classifying text into decisions, preferences, milestones, problems, and emotional context.

Why useful: the agent-system design is the source-of-truth behavior; MemPalace's extractor shows a cheap fallback or pre-filter for typed memory extraction.

## Stop raw transcript dumping into vector memory

OpenBrigade should avoid embedding unprocessed transcript logs into Qdrant. Store raw text in Postgres, then embed only curated summaries, typed episodic records, and knowledge chunks with source metadata. This reduces noisy retrieval, privacy blast radius, duplicated facts, and vector-store bloat.

Useful references:

- `reference/agent-system-design-v1.3.md:512` explicitly says to keep raw conversation archival in PostgreSQL.
- `reference/agent-system-design-v1.3.md:513` says to stop writing raw or minimally processed conversation dumps to Qdrant.
- `reference/agent-system-design-v1.3.md:514` says structured episodic records are the Qdrant-facing memory format.
- `reference/mempalace/README.md:17` is the strongest counterexample: MemPalace stores raw exchanges in ChromaDB and reports strong benchmark results from raw mode.
- `reference/pixelagent/examples/memory/openai/semantic-memory.py:19` creates a computed `timestamp: role: content` string and embeds it; this is a good example of what OpenBrigade should not copy for v0.3 transcript memory.

Why useful: these references show the design decision clearly. MemPalace and Pixelagent demonstrate raw vector memory can work, but OpenBrigade's v0.3 direction is intentionally different: raw evidence remains searchable through Postgres/full text and source links, while Qdrant gets higher-signal records.

## Add daily memory curation job

Each agent needs a daily off-peak curation job that reads `memory/YYYYMMDD-MEMORY.md`, decides what to elevate into `MEMORY.md`, compacts or cleans daily entries when useful, and leaves raw session evidence in durable storage. This job is maintenance, not a heartbeat assignment.

Useful references:

- `OpenBrigade_V0.1_Design_Summary.md:121` defines append-only daily memory files with retry behavior.
- `OpenBrigade_V0.1_Design_Summary.md:122` allows curation to compact, clean, or remove daily entries after they move to Qdrant.
- `OpenBrigade_V0.1_Design_Summary.md:282` sets daily off-peak cadence, with `2:30 AM` local time as the default.
- `OpenBrigade_V0.1_Design_Summary.md:283` says the curation trigger is a separate fire-and-forget cron and not a heartbeat task.
- `reference/self-improving-proactive-agent-1.0.0/SKILL.md:124` recommends logging corrections concisely and promoting after repetition or explicit confirmation.

Why useful: OpenBrigade docs define the job contract; self-improving references provide promotion discipline so curation does not over-promote one-off noise.

## Enforce the `MEMORY.md` 2KB soft cap in live workspaces

`MEMORY.md` should remain a small, always-loadable file containing only confirmed high-value facts, preferences, durable rules, and reusable patterns. v0.3 should detect when the file exceeds roughly 2KB and trigger a rewrite-to-fit during curation rather than blocking work immediately.

Useful references:

- `OpenBrigade_V0.1_Design_Summary.md:114` defines `MEMORY.md` as curated long-term memory with a 2KB soft cap.
- `OpenBrigade_V0.1_Design_Summary.md:286` gives the curator authority to add, remove, modify, or merge `MEMORY.md` entries.
- `OpenBrigade_V0.1_Design_Summary.md:287` says exceeding 2KB triggers an LLM rewrite-to-fit on the next cycle.
- `OpenBrigade-Concept.md:119` says the most important curated memories belong in `MEMORY.md`; `OpenBrigade-Concept.md:121` says it should never exceed 2KB.
- `reference/self-improving-1.2.16/memory-template.md:8` separates confirmed preferences, active patterns, and recent pending items.

Why useful: no reference has a byte-cap enforcement implementation. The nearest references are the OpenBrigade design lines plus self-improving's HOT memory templates, which show how to keep always-loaded memory sparse and curated.

## Archive daily memories after 7 days into Qdrant

Daily memory files older than 7 days should be summarized/vectorized into Qdrant, linked back to raw transcript or daily-memory provenance, and removed from the live workspace. The archive should preserve enough metadata to retrieve by agent, date, session, task, participants, source path, and archival timestamp.

Useful references:

- `OpenBrigade_V0.1_Design_Summary.md:123` says daily memory is vectorized to Qdrant and deleted from disk after 7 days.
- `OpenBrigade_V0.1_Design_Summary.md:288` repeats daily archival after 7 days as part of Memory Curation.
- `reference/self-improving-proactive-agent-1.0.0/SKILL.md:173` uses a 7-day repetition window before promoting a pattern to HOT memory.
- `reference/self-improving-1.2.16/memory-template.md:14` includes a "Recent (last 7 days)" section for pending corrections.

Why useful: OpenBrigade defines the exact archival rule. The self-improving references support the 7-day window as a useful boundary for separating recent working memory from durable memory.

## Add knowledge ingestion for Markdown and text files first

The first ingestion pass should accept `.md` and `.txt`, identify source and document type, chunk text predictably, attach metadata, store chunks in Qdrant, and create provenance-ready records for graph expansion. Start with local files because they are deterministic, testable, and do not require network, PDF parsing, or crawler policy.

Useful references:

- `OpenBrigade_V0.1_Design_Summary.md:266` defines the ingestion pipeline: identify document type/source, chunk and store in Qdrant with metadata, and extract metadata for graph nodes/edges.
- `reference/mempalace/mempalace/miner.py:22` lists readable extensions including `.txt` and `.md`.
- `reference/mempalace/mempalace/miner.py:325` chunks text while trying to split on paragraph or line boundaries.
- `reference/mempalace/mempalace/miner.py:379` stores metadata including source file, chunk index, agent, and filed timestamp.
- `reference/wiki-layer/README.md:24` recommends Markdown output so humans can inspect, edit, diff, and review it.

Why useful: MemPalace gives the best local-file ingestion example. OpenBrigade should adapt the chunking and metadata ideas but write to Qdrant and Postgres-backed ingestion records instead of ChromaDB-only drawers.

## Add PDF, web, and repository ingestion later in this phase

After Markdown/text ingestion is stable, extend ingestion to PDFs, web pages, and repositories. PDF and long web content need smaller overlapping chunks; repositories need explicit manifests, ignore handling, source snapshots, and license/attribution tracking before indexing.

Useful references:

- `OpenBrigade-Concept.md:173` requires document and repository libraries for web articles, PDFs, GitHub repos, and long texts.
- `OpenBrigade-Concept.md:180` requires document type and source identification.
- `OpenBrigade-Concept.md:181` requires Qdrant chunks with appropriate metadata, including overlap for long articles and PDFs.
- `OpenBrigade_V0.1_Design_Summary.md:255` lists web articles, PDFs, GitHub repos, and books/long texts as planned sources.
- `reference/pixelagent/examples/agentic-rag/financial-pdf-reports/table.py:16` shows a PDF chunked view using `DocumentSplitter` with a token limit.
- `reference/mempalace/mempalace/miner.py:65` implements a lightweight `.gitignore` matcher, useful for repository ingestion.

Why useful: Pixelagent is the nearest concrete PDF ingestion example; MemPalace is the nearest repository-file scanning example. No reference provides a full OpenBrigade-ready web/PDF/repo pipeline with licensing and provenance, so this should be designed after the Markdown/text path is validated.

## Create Neo4j document, task, and decision provenance nodes

Neo4j should model provenance across documents, tasks, assignments, decisions, agents, source chunks, and transcript records. Every summary, memory, and knowledge claim should be traceable to its source evidence and decision context.

Useful references:

- `OpenBrigade-Concept.md:185` says ingestion metadata should create knowledge graph nodes and links for author, title, type, category, subject, and publication date.
- `OpenBrigade-Concept.md:202` explicitly calls for a knowledge graph of tasks and decisions in Neo4j.
- `OpenBrigade_V0.1_Design_Summary.md:344` names Neo4j schema for documents, agents, tasks, and decisions as a deferred design item.
- `reference/agent-system-design-v1.3.md:427` introduces graph expansion for decision provenance.
- `reference/agent-system-design-v1.3.md:529` lists adding decision provenance to the knowledge graph as a later phase.
- `reference/mempalace/mempalace/knowledge_graph.py:61` creates entity nodes, `reference/mempalace/mempalace/knowledge_graph.py:69` creates triples with temporal validity and source fields, and `reference/mempalace/mempalace/knowledge_graph.py:121` adds relationship triples.
- `reference/wiki-layer/docs/page-schema.md:82` defines `source_refs` as supporting sources; `reference/wiki-layer/docs/page-schema.md:161` maps important claims to source references; `reference/wiki-layer/src/lib/provenance.ts:3` requires first-class source refs, confidence, unresolved questions, and explicit contradictions.

Why useful: there is no Neo4j implementation in the reference corpus. MemPalace provides the nearest graph storage pattern, though in SQLite triples, and wiki-layer provides the strongest provenance shape for human-readable synthesis. Use those to design Neo4j node and edge types before implementation.
