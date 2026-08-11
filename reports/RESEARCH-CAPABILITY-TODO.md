# Governed Research Capability TODO

## Purpose

This is the implementation backlog for turning the current web-research slice into a dependable,
governed capability. It covers the five areas with the most remaining uncertainty after the first
MCP, SearXNG, Playwright, PDF extraction, source-map, and chunk-offset implementation.

The work is deliberately sequential. A phase is not started until its acceptance checks pass and
the operator has reviewed the phase-close evidence. This prevents a polished operator surface from
hiding an unsafe browser worker, or citation formatting from overstating weak search results.

## Current Baseline

- MCP tool consumption supports configured stdio and HTTP servers plus tool discovery and calls.
- Public search defaults to the internal SearXNG service and can fall back to DuckDuckGo HTML.
- Browser navigation and rendered-text extraction run in a separate Playwright worker.
- Web fetches, PDF extraction, search result lists, and browser extracts can persist source maps.
- Knowledge chunks retain character offsets that can identify supporting passages.

This document does not claim that the baseline is sufficient for hostile web content, reliable legal
research, complete MCP interoperability, or mandatory citations in agent answers.

## Shared Guardrails

- Keep all default tests offline-safe. Live checks must be explicitly enabled and must not require
  production credentials.
- Preserve source URL, final URL, retrieval time, content hash, content type, and retrieval tool for
  every persisted external source. Add new provenance; do not replace those fields with display text.
- Never put browser profile credentials, MCP secrets, cookies, authorization headers, or raw tokens
  in agent context, logs, documents, alerts, screenshots, or operator APIs.
- Keep public browsing and authenticated-profile browsing as separate policies, storage locations,
  audit categories, and permission grants.
- Every new external action must have bounded time, response/body size, retry behavior, and a
  structured failure observation. A degraded dependency must not remove unrelated built-in tools.
- Update the README, the relevant runbook, and this checklist only when the behavior has been
  implemented and validated.

## Phase 1: Complete the MCP Client Contract

**Status:** approved and closed (2026-08-10)

**Concern addressed:** the current client is a useful first slice, not a complete or easily operated
MCP integration.

**Outcome:** OpenBrigade supports a clearly bounded, versioned subset of MCP tool use with reliable
session behavior, credential references, policy enforcement, and diagnosable server state.

### TODO

- [x] Write and publish the supported MCP contract: protocol revision(s), transports, tool-only scope,
  request/response size limits, timeouts, retry policy, and unsupported features. Explicitly defer
  resources, prompts, sampling, elicitation, streaming, and server-to-client requests unless added
  with their own design and tests.
- [x] Replace one-shot discovery/call behavior where necessary with a managed client lifecycle:
  `initialize`, negotiated capabilities, `notifications/initialized`, request-id correlation, clean
  shutdown, and reconnect behavior for stdio and HTTP transports.
- [x] Add strict configuration validation and per-server policy: enabled state, allowed agent/team
  principals, allowed tools, argument and result size limits, timeout, rate budget, and a stable
  server identifier. Reject unsafe or ambiguous configuration at load time.
- [x] Add credential references resolved through the existing secret store. Configuration and APIs
  must contain secret identifiers only; redact credentials from all observations and diagnostics.
- [x] Record server health transitions, discovery failures, protocol failures, and policy denials in
  the audit/alert system without exposing sensitive tool arguments or results.
- [x] Define an MCP tool-result normalization policy so structured content, text content, error
  content, and large results become bounded agent observations with a retained audit reference.

### Acceptance and Evidence

- [x] A test fixture validates initialization, discovery, tool calls, reconnect, and shutdown for
  each supported transport.
- [x] Malformed protocol messages, slow servers, oversized results, missing credentials, and denied
  tools fail locally with a clear observation and do not affect other tools or servers.
- [x] An authorized operator can inspect non-secret server health and the most recent failure reason
  through a stable service/CLI surface, even before the full GUI in Phase 5.
- [x] At least one real, non-production MCP server is exercised in an opt-in integration test.
- [x] `docs/MCP_CLIENT_POST_RC.md` documents the supported subset and no longer implies more.

**Phase-close decision:** approve the exact supported MCP subset and its credential ownership model
before allowing additional external MCP servers in regular operator workflows.

### Phase-close evidence (2026-08-10)

- Changed: `brigade/mcp_client.py`, MCP tool registration/audit handling, `brigade mcp status`,
  the MCP contract, README capability boundary, and offline/opt-in MCP tests.
- Offline validation: `python3 -m pytest tests/test_mcp_client.py tests/test_web_fetch_save.py
  tests/test_config.py tests/test_cli.py -q` — 93 passed, 1 opt-in test skipped; `python3 -m ruff
  check brigade/mcp_client.py brigade/tools.py brigade/cli.py tests/test_mcp_client.py` and `git diff
  --check` passed.
- Opt-in integration: a locally installed non-production Google Workspace MCP server completed
  initialization, discovery, and `gmail_list_accounts` with isolated dummy OAuth metadata and an empty
  account set; no existing credentials were read or used.
- Live validation: rebuilt `brigade_web` and `brigade_orchestrator`; `./ops/brigade-live.sh health
  --json` returned `ok: true` with PostgreSQL, Redis, Qdrant, Neo4j, and migrations healthy.
- Known limits: tool-only MCP subset; stdio and request/response HTTP only; no resources, prompts,
  sampling, elicitation, streaming, or server-to-client requests. Configuration edits and richer
  operator controls remain Phase 5.
- Next phase: do not begin Phase 2 until the operator approves this MCP subset and credential-reference
  ownership model.

## Phase 2: Harden the Browser Worker Against Untrusted Pages

**Status:** approved and closed (2026-08-11; reopened validation incorporated)

**Concern addressed:** the worker has basic network screening, but hostile or merely heavy pages can
still consume resources or expose browser-profile data.

**Outcome:** browser activity is bounded, auditable, and isolated enough for public research; named
authenticated profiles require an explicit, narrower approval path.

### TODO

- [x] Document the browser threat model, including SSRF, DNS rebinding, redirects, downloads,
  uploads, popups, cross-origin requests, credential leakage, prompt injection in page content,
  browser exploits, and resource exhaustion.
- [x] Enforce limits in the worker, not only in the caller: per-operation wall time, page lifetime,
  request count, response/body bytes, rendered-text bytes, screenshot dimensions/bytes, concurrent
  contexts, and per-session TTL. Return a structured limit failure.
- [x] Block downloads, file uploads, permission prompts, local-file URLs, non-HTTP(S) schemes,
  private/link-local/loopback/reserved destinations, and unexpected browser-initiated protocols.
  Validate every request and redirect after DNS resolution.
- [x] Make public sessions disposable and prohibit their persistent storage. For authenticated
  profiles, add an explicit owner/principal mapping, encrypted-at-rest storage or a documented
  external secret/profile provider, revocation, TTL, and a clear-data operation.
- [x] Add an append-only audit event for browser open, navigation, click, extract, screenshot,
  blocked request, profile use, and limit failure. Store URLs and policy results; redact query
  parameters or page content where they may hold secrets.
- [x] Add worker health and saturation metrics, a bounded queue, and a restart/recovery strategy.
  Define the behavior when the worker is unavailable.

### Acceptance and Evidence

- [x] Offline tests prove policy rejection for private destinations, redirect chains, uploads,
  downloads, disallowed schemes, over-budget content, and expired sessions.
- [x] A browser-worker integration test proves public sessions cannot read a named profile and that
  named-profile authorization is enforced by the worker as well as the caller.
- [x] A load test demonstrates that hung or large pages do not starve later requests beyond the
  declared queue/timeout budget.
- [x] Audit records allow an operator to answer who used which profile, against what final URL, and
  why an operation was blocked, without storing credentials.
- [x] `docs/LIBRARY_SYSTEMS.md` and the operator runbook document the final limits and recovery
  commands.

**Phase-close decision:** public browsing can become a normal agent tool only after the worker
passes adversarial-policy tests. Authenticated browsing remains opt-in until its credential-storage
and ownership review is approved.

### Reopened validation evidence (2026-08-11)

- `tests/test_browser_worker.py` now exercises worker-enforced profile authorization and public-session
  isolation, validates each browser request/redirect against a private-space DNS result, and confirms a
  held worker slot immediately rejects later operations within the queue budget.
- `python3 -m pytest tests/test_browser_worker.py tests/test_web_fetch_save.py tests/test_config.py -q`
  passed (34 tests); focused Ruff and `git diff --check` passed.

## Phase 3: Make Search Quality Measurable and Suitable for Legal Research

**Status:** acceptance complete (2026-08-11; operator-approved progression)

**Concern addressed:** a successful SearXNG smoke proves reachability, not relevance, source quality,
jurisdictional fit, or reliable failure behavior.

**Outcome:** search is an evaluated retrieval service with explicit source-quality policy and
observable degradation, rather than an unmeasured list of links.

### TODO

- [x] Define search intents and source tiers. At minimum distinguish primary legal materials,
  official government sources, court sites, reputable secondary analysis, academic papers (both per reviewed and pre-published), arXive.org papers, and general web results.
  Record jurisdiction, court/authority, publication date, and source tier when available.
- [x] Configure and document the allowed engines and result filters for the deployment. Disable or
  down-rank engines that produce unstable, scraped, inaccessible, or misleading results for the
  stated research intent.
- [x] Create a versioned, non-sensitive evaluation set covering statutory, regulatory, case-law, and
  general research queries across the jurisdictions the brigade is expected to serve. Define recall,
  precision, authority, freshness, and duplicate-rate thresholds before tuning ranking.
- [x] Preserve query, engine, rank, result URL, final fetched URL, result timestamp, and source-tier
  classification in result/source-map metadata. Do not treat a search-result snippet as evidence.
- [x] Add retry/backoff, engine-health accounting, and a clear degraded-mode observation. Search
  callers must know whether the answer came from normal search, reduced-engine search, fallback, or
  no search result.
- [x] Add a source-selection helper that biases legal research toward primary/official material and
  asks for the jurisdiction or flags it as an assumption when it is unknown.

### Acceptance and Evidence

- [x] The offline evaluation set runs in CI with deterministic fixtures; opt-in live evaluation
  reports result quality separately from unit tests.
- [x] The agreed thresholds are met for the target jurisdictions, or the feature is described as
  general web research only and legal-research claims remain disabled.
- [x] A failed engine or fallback is visible in the tool observation, stored metadata, and operator
  telemetry.
- [x] Search results retain enough metadata for a later answer to identify the source and explain
  whether it is primary or secondary material.
- [x] Retained search data may not contain social media as a primary source. It is only retained as
  a no-snippet discovery pointer to material that must be independently retrieved.

**Phase-close decision:** approve the jurisdictions and authority threshold. Do not represent the
system as reliable legal research before this evidence exists.

### Phase-close evidence (2026-08-11)

- Scope: legal retrieval is a targeted `intent=legal` mode, not a requirement for ordinary research
  or plain-prose work. It targets US federal material in this release; general and academic research
  remain available. It does not represent legal advice or eliminate the need to check primary law.
- Source policy: `primary_legal`, `court_material`, `official_government`, `secondary_legal`,
  `academic_peer_reviewed` (only when source metadata attests it), `academic_preprint` (arXiv),
  `academic`, and discovery-only sources are recorded. Cornell LII is explicitly
  `secondary_legal` interpretation and citations retain that qualification.
- Retention: fetched originals are retained separately from parsed text under the default local
  data volume or an absolute operator-mounted NAS/local path. Search saves retain result pointers
  and metadata but never result snippets. Browser snapshots may retain their rendered HTML.
- Engine policy: the bundled deployment uses the live-verified `google cse` SearXNG identifier.
  Other deployments must probe and configure their own allowed IDs. The initial broad engine list
  was rejected after the live report returned irrelevant legal/academic results.
- Validation: `python3 -m pytest tests/test_web_fetch_save.py tests/test_config.py
  tests/test_evidence.py tests/test_browser_worker.py tests/test_research.py
  tests/test_search_evaluation.py tests/test_knowledge_kb.py -q` — 55 passed; focused Ruff,
  `git diff --check`, `docker compose --env-file .env.example config`, and deterministic evaluation
  (`recall`, `precision`, `authority`, `freshness` 1.0; duplicate rate 0.0) passed.
- Opt-in live report: the five-query internal SearXNG report returned primary/official US materials
  for statute and general research, primary/court results for case-law, and arXiv preprints for the
  academic query. It is a candidate report, not a claim that dynamic search results pass the fixed
  fixture's thresholds; publication dates were not supplied by this engine.
- Live validation: rebuilt the app services, recovered a transient Docker Compose container-name
  conflict by removing only the two stopped app service containers, and `./ops/brigade-live.sh health
  --json` returned healthy PostgreSQL, Redis, Qdrant, Neo4j, and migrations.
- Next phase: citation enforcement is required only for answer paths designated as citation-bearing
  (including legal research), never as a blanket requirement for ordinary plain-prose work.

## Phase 4: Enforce Evidence-Backed Citations in Final Answers

**Status:** approved and closed (2026-08-11; scoped to citation-bearing requests only)

**Concern addressed:** source maps and chunk offsets are stored, but the current prompt guidance does
not guarantee citations or prevent unsupported claims.

**Outcome:** an answer that relies on externally retrieved material carries verifiable citations to
the exact source and supporting passage, or it is marked as unable to substantiate the claim.

### TODO

- [x] Define a citation data model used from tool observation through final rendering: source ID,
  original and final URL, accessed time, document version/content hash, title, retrieval method,
  chunk character range, and PDF page range where extraction can supply it.
- [x] Preserve PDF page-to-text mapping during extraction. Add page references to saved sources and
  chunks; character offsets alone are insufficient for a reader to check a long PDF.
- [x] Carry source identifiers, not only prose snippets, into agent context. Make the final-answer
  schema attach citations to claims or paragraphs using a stable machine-readable form.
- [x] Validate citation-bearing completed answers whenever web, browser, or knowledge-web sources were
  used: citations
  must refer to sources actually supplied to the turn; quoted/supporting ranges must exist; URLs must
  be present; citations cannot be fabricated. Retry once with a repair instruction, then return a
  transparent insufficient-evidence result if validation still fails.
- [x] Render citations consistently in Chief, Executive, assignment, CLI/TUI, API, and web UI
  responses. The rendered citation must link to the preserved source URL and show accessed time plus
  locator (PDF page or chunk range).
- [x] Distinguish source facts, model inference, and operator-provided assertions in the response
  model. Citations support the former; they must not falsely validate the latter two.

### Acceptance and Evidence

- [x] Unit tests reject invented source IDs, URLs, locators, citations to unsupplied documents, and
  uncited externally sourced claims.
- [x] End-to-end fixtures cover HTML, PDF, browser-rendered content, a multi-source answer, and a
  no-source answer; each renders stable citations across API and UI surfaces.
- [x] Citation links remain available after the original page changes because the source map retains
  the retrieval-time document identity and hash.
- [x] A failed repair never silently strips citations or converts an evidence-backed answer into an
  uncited answer.

**Phase-close decision:** make evidence-backed citations mandatory for web-derived responses only
after every supported answer surface uses the same renderer and validator.

### Phase-close evidence (2026-08-11)

- Scope: citation enforcement is active only for explicit citation/legal requests and legal-search
  intent. General research, operational work, and ordinary chat remain plain prose. Citation-bearing
  retrieval automatically retains `web_fetch` and `browser_extract` originals before an answer is made.
- Contract: `brigade/citations.py` carries source ID, source/final URL, access time, hash, title,
  retrieval method, source tier, character range, and PDF page range. It admits only supplied IDs,
  renders stable Markdown footnotes, and exposes `source_fact`, `model_inference`, and
  `operator_assertion` classes in chat metadata and assignment transcript records.
- Enforcement: Chief, Executive, and assignment responses get one repair prompt. Missing/invented
  citations, absent evidence, or a failed repair return a transparent insufficient-evidence answer;
  no uncited draft is stored as a citation-bearing answer. Cornell LII renders as secondary legal
  interpretation rather than primary authority.
- Surfaces: the durable chat API returns citation/claim metadata; CLI/TUI display the same Markdown;
  the web Markdown renderer preserves the retained-source links. Assignment transcript records retain
  the machine-readable citations and claim classes.
- Validation: `python3 -m pytest tests/test_citations.py tests/test_evidence.py
  tests/test_web_fetch_save.py tests/test_chief_chat.py tests/test_executive.py tests/test_runner.py -q`
  — 84 passed; focused Ruff and `git diff --check` passed. Fixtures cover HTML, PDF page/character
  locators, browser extracts, multi-source rendering, API history, no source, invented source IDs,
  missing locators, and failed repairs. `cd web && npm run build` passed; rebuilt app profile and
  `./ops/brigade-live.sh health --json` reported healthy Postgres, Redis, Qdrant, Neo4j, and migrations.
- Next phase: provide the authenticated operator control plane for this now-governed research stack.

## Phase 5: Give Operators a Complete Research Control Plane

**Status:** approved and closed (2026-08-11)

**Concern addressed:** dependency failures and policy decisions currently appear as agent
observations, but not as a coherent operator workflow with health, controls, and audit access.

**Outcome:** authorized operators can configure, test, monitor, and revoke research capabilities
without editing opaque configuration or exposing secrets.

### TODO

- [x] Add CLI and API operations to list, validate, enable, disable, and test MCP servers, search
  backends, and browser-worker connectivity. Return capability, health, policy, and redacted
  credential-reference state; never return secret values.
- [x] Add an operator UI research panel that shows current configuration, last successful check,
  active degradation, error reason, policy limits, and a one-action test for each component.
- [x] Add alert rules for MCP authentication/protocol failure, search backend/engine degradation,
  browser saturation/policy blocks, citation-validation failure, and unexpected usage volume.
  Deduplicate alerts and provide acknowledgement/resolution history.
- [x] Add a filtered audit view for external research actions, including acting principal, tool,
  policy outcome, source/final URL, retained document ID, and correlation ID. Restrict it by the
  existing RBAC/audit model.
- [x] Implement configuration changes as staged, auditable proposals with validation before apply,
  rollback to the last known good configuration, and no secret values in UI/API payloads.
- [x] Publish an operator runbook for incident response, dependency outage, credential rotation,
  profile revocation, source-quality regression, and citation-validator failure.

### Acceptance and Evidence

- [x] An authorized operator can diagnose each dependency state and execute a safe health check from
  CLI and UI without container-shell access.
- [x] An unauthorized user cannot view profile identity, credential references, detailed audit events,
  or modify research configuration.
- [x] Simulated failure fixtures produce one actionable alert, a correlated audit entry, and a clear
  UI state; recovery resolves rather than duplicates the alert.
- [x] A configuration change is rejected before apply when invalid, and a valid change records actor,
  time, old/new non-secret policy, and rollback point.

**Phase-close decision:** research tooling is ready for regular operator use when all prior phases
remain green and this control plane can show, constrain, and recover every external dependency.

### Phase-close evidence (2026-08-11)

- Control plane: `brigade research status|audit|test|enable|disable|apply|rollback` and the matching
  authenticated `/api/research/*` routes provide redacted status, bounded checks, filtered audit,
  staged policy, explicit apply, and last-known-good rollback. The Telemetry **Governed Research**
  panel provides status, test, staged enablement, pending apply, acknowledgement, and rollback without
  shell access.
- Policy and retention: active switches gate MCP tools, public search/direct fetches, and browser
  operations without affecting built-in tools. Policy records only booleans, actor, time, old/new
  policy, and rollback points; no URLs, credentials, profile identifiers, cookies, or tokens are
  stored in that control-plane record.
- Incident handling: MCP protocol/auth failures, SearXNG degradation/fallback, browser policy or
  capacity failures, citation validation failure, and sustained research-action volume are deduplicated
  incidents. They retain correlation IDs, acknowledgement/resolution history, and a bounded filtered
  audit projection. A healthy check resolves the prior incident instead of emitting another alert.
- Citation hardening: the assignment tool-budget wrap-up now runs the same citation validator as the
  normal completion path. It becomes transparent `awaiting_human`/blocked progress rather than
  emitting an uncited citation-bearing summary.
- Documentation: `docs/RESEARCH_OPERATIONS_RUNBOOK.md` covers dependency outage, credential rotation,
  profile revocation, quality regression, citation-validator incident response, staged recovery, and
  live validation; README links to it.
- Validation: `python3 -m pytest tests/test_research_control.py tests/test_mcp_client.py
  tests/test_research.py tests/test_web_fetch_save.py tests/test_citations.py tests/test_chief_chat.py
  tests/test_executive.py tests/test_runner.py tests/test_cli.py -q` — 160 passed, 1 opt-in skipped;
  focused Ruff and `git diff --check` passed. `cd web && npm run build` passed;
  `docker compose --env-file .env.example config` passed; rebuilt `brigade_web` and
  `brigade_orchestrator`; `./ops/brigade-live.sh health --json` returned healthy PostgreSQL, Redis,
  Qdrant, Neo4j, and migrations.
- Known limit: `python3 -m ruff check .` remains non-green because of eight pre-existing unrelated
  findings in `brigade/knowledge_web.py`, `tests/test_reconcile.py`, `tests/test_release_1_0_3.py`,
  and `tests/test_v1_0_recovery.py`; changed-surface Ruff is clean.

## Cross-Phase Validation Checklist

- [x] Focused unit tests run with no public network, API keys, browser binaries outside the worker,
  or live MCP servers.
- [x] Opt-in integration tests are documented, scrub secrets, and clean up temporary profiles and
  test configurations.
- [ ] `python3 -m ruff check .` passes for changed Python files.
- [x] Relevant backend tests and `cd web && npm run build` pass for every phase that changes those
  surfaces.
- [x] `docker compose --env-file .env.example config` passes after compose/configuration changes.
- [x] App-profile rebuild plus `./ops/brigade-live.sh health --json` passes for every deployed phase.
- [x] The phase-close document records changed files, test commands, live-check result, known limits,
  and the next approved phase.

## Dependencies and Order

1. Phase 1 establishes a reliable external-tool contract and health record.
2. Phase 2 makes browser retrieval safe enough to use as an evidence source.
3. Phase 3 establishes what sources and results are trustworthy for the intended research domain.
4. Phase 4 turns retained evidence into enforceable answer citations.
5. Phase 5 exposes the completed system safely to operators and makes degradation actionable.

Phase 5 may add narrow read-only status endpoints while earlier phases are in progress, but it must
not expose mutation controls for a component before that component's policy and audit requirements
are accepted.
