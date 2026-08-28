# Staff Meeting Implementation Gate

This is the repository-local delivery record for the Staff Meeting workflow described in
`results/OpenBrigade_Staff_Meeting_Development_Specification.md` and clarified in
`results/codex-answers.txt`.

## Approved policy baseline

- [x] Executive and Crew Chief may convene; ordinary agents may only request Chief review.
- [x] Five seats minimum/default, twelve maximum; catalog roles only.
- [x] Quorum is 70 percent of approved seats rounded up, with three distinct responding agents.
- [x] Consensus requires two-thirds of substantive votes, three substantive role-seat ballots,
  and three distinct substantive voters.
- [x] A failed support threshold is `MOTION_NOT_ADOPTED`; two-thirds opposition is recorded
  separately in the arithmetic.
- [x] One agent may hold multiple roles but at most one veto-capable role; role-family
  concentration is warned and duplicated identities are disclosed.
- [x] One targeted follow-up discussion is allowed, with up to three rounds and no nesting.
- [x] Discussion after a revealed ballot opens a new secret ballot round.
- [x] Only the issuing veto reviewer, or a replacement reviewer after fresh review, may clear a
  veto. An unresolved veto terminates as `BLOCKED_BY_VETO`.
- [x] One normal packet unfreeze is allowed. Evidence-only amendment grants one new discussion;
  material amendment restarts independent review.
- [x] Meeting work is read-only. The caller may reduce but not increase harness limits.

## Initial versioned policy values

- Role catalog: `staff-meeting-roles:v1`, containing the twelve roles in the approved design and a
  versioned `role_family` plus optional `veto_domain`.
- Discussion: three full-panel rounds; one targeted discussion of three rounds.
- Elapsed time: 7,200 seconds.
- Total model tokens: 100,000.
- Tool calls: 60.
- Excerpts: deterministic lexical overlap, all veto/risk/objection/contradiction and dissenting
  review spans mandatory, then the highest-scoring spans to a 24-excerpt target.

Catalog or ceiling increases require an explicit policy/configuration change and tests. A meeting
caller cannot raise them or add ad hoc roles.

## Delivery gates

1. [x] Postgres migration and JSON development-store parity.
2. [x] Append-only packet, transition, assignment, response, excerpt, veto, ballot, synthesis, and
   report records with meeting-scoped idempotency.
3. [x] Durable conversation identity, assignment/lease/runner integration, usage accounting, and
   crash-safe wave dispatch.
4. [x] Role proposal/approval, packet freeze/amendment, independent review, synthesis,
   deliberation, bounded targeted follow-up, ballot, veto, final report, cancellation, and replay.
5. [x] Executive/Crew Chief chat tools, ordinary-agent proposal tool, and read-only meeting tool
   registry.
6. [x] Durable in-app Chief alert. Chat-originated Telegram delivery uses the existing connector
   response path; notification failure does not roll back the meeting.
7. [x] Read-only authenticated searchable Cockpit viewer with live progress.
8. [x] Qdrant episode and Neo4j provenance writes after canonical completion.
9. [x] Repository skill and architecture/prompt documentation.
10. [x] Changed-surface repository validation, Compose validation, live migration/rebuild, and
    final specification audit recorded below.

## Validation evidence

- `python3 -m pytest -q tests/test_staff_meeting.py`: 31 passed.
- Staff Meeting plus migrations, web packaging, Ops Room, orchestrator, orchestrator chat, runner,
  v1.0 tooling, and CLI: 208 passed.
- Changed Python surface: Ruff passed.
- `git diff --check`: passed.
- `web/npm run build`: passed; the container build also produced the final Vite bundle.
- `docker compose --env-file .env.example config --quiet`: passed.
- `./ops/brigade-live.sh health --json`: healthy Postgres, Redis, Qdrant, and Neo4j; migration
  `0016_staff_meetings` applied, with no pending or failed migrations.
- Live `/api/staff-meetings` smoke: HTTP 200 under the current local auth configuration.
- Production store adapter smoke: the new meeting and catalog tables are queryable.

The unfiltered full pytest command was interrupted after several minutes with no output; no clean
full-suite claim is made. Full-repository Ruff reports five unrelated pre-existing findings in
`brigade/knowledge_web.py`, `tests/test_reconcile.py`, and `tests/test_release_1_0_3.py`; the changed
surface is clean. The Docker build's existing dependency audit reports five npm advisories (two low,
three high); dependency remediation is outside this feature gate.
