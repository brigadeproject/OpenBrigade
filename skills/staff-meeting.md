# Staff Meeting

Use a Staff Meeting when a user explicitly asks for one, a consequential decision has credible
competing approaches, a large project crosses several domains, ordinary recovery has stalled, or
technical, operational, security, safety, ethical, and user concerns materially interact.

Do not convene one for factual lookup, routine reversible work, or a decision already controlled by
a higher-priority policy. A Staff Meeting never replaces required user approval.

## Invocation

- An Executive may call `convene_staff_meeting` on the user's behalf.
- A Crew Chief may call `convene_staff_meeting`, approve the deterministic roster, and must create a
  durable operator alert.
- Other assignment agents may call `request_staff_meeting`; this creates a proposal for their Crew
  Chief and does not convene a meeting.
- Never simulate the panel or its rounds inside one agent response. Invoke the orchestration tool so
  state transitions, assignments, ballots, limits, and provenance are enforced by the harness.

## Frame the packet

Provide a precise original request and at least one testable acceptance criterion. Include the
desired deliverable, scope, constraints, assumptions, and evidence references when known. Secrets
must not be included; the harness redacts sensitive values before persistence and hashing.

The chair may approve or change only catalog roles and their agent assignments during roster
approval. The default and minimum panel is five seats; the maximum is twelve. Quorum is 70 percent
of approved seats rounded up and must include at least three underlying agents. A consensus needs
two-thirds of substantive ballots, at least three substantive ballots, and at least three distinct
substantive voters.

## Conduct and tools

The harness performs independent reviews, chair synthesis with mandatory attributed dissent and
veto excerpts, up to three full-panel deliberation rounds, and a secret formal ballot. It may run
one targeted follow-up discussion with selected roles; that discussion has up to three rounds and
cannot nest. The full panel receives the revised synthesis and retains decision authority.

Meeting assignments receive only read-only inspection and research tools. They cannot mutate the
repository, Brigade state, or external systems. Resource budgets may be reduced by the caller but
not raised above harness defaults.

## Interpret the result

Read the exact vote arithmetic, quorum, duplicated-agent disclosure, acceptance-criteria findings,
dissent, veto status, assumptions, evidence, risks, resource use, and audit references. A failed
support threshold is `MOTION_NOT_ADOPTED`; it is not mislabeled consensus to oppose. An unresolved
security or safety veto ends as `BLOCKED_BY_VETO`. Only the issuing reviewer, or a replacement
reviewer who performs a fresh review, may accept mitigation and clear it.

The Cockpit Staff Meetings tab is a read-only searchable live and historical viewer. Postgres is
authoritative; completed reports are indexed as durable episodic memory and projected into
provenance only after their canonical records are written.
