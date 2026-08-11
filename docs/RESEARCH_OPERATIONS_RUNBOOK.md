# Governed Research Operations Runbook

This runbook covers bounded MCP, public search, browser-worker, retained-source, and
citation-validation capabilities. It never requires an operator to paste a credential into the
CLI or web UI. MCP credential references remain in the configured secret store; browser profiles
remain worker-owned and are never returned by the research-status API.

## Safe operator workflow

Inspect the redacted state first:

```bash
python3 -m brigade research status
python3 -m brigade research audit --limit 100
```

Run one bounded dependency check when a component is degraded:

```bash
python3 -m brigade research test mcp
python3 -m brigade research test search
python3 -m brigade research test browser
```

The Telemetry page's **Governed Research** panel provides the same checks. A failed check creates
one deduplicated incident with a correlation ID; repeating the same failure increases its count.
An operator can acknowledge it in the panel. A succeeding check resolves it and preserves its
history. Detailed audit records require the existing `task:write` permission; status remains
read-only and does not disclose profile identity or credential references.

## Policy changes and rollback

Research component changes are staged before activation. This prevents a malformed request from
silently changing a live capability.

```bash
python3 -m brigade research disable browser
# copy proposal_id from the output after review
python3 -m brigade research apply PROPOSAL_ID
python3 -m brigade research rollback
```

The UI follows the same stage → apply flow. The active policy, actor, timestamps, previous policy,
and rollback point are recorded without secret values. The switches cover MCP tools, public search
and direct public fetches, and browser-worker actions. Disabling a component returns a clear tool
observation while unrelated built-in tools continue to work.

## Incident response

- MCP authentication or protocol failure: check `brigade research test mcp`, then inspect the
  filtered audit record. Repair or rotate the referenced secret through the approved secret-store
  workflow, never by placing a value in MCP configuration. Re-test before staging enablement.
- Search degradation or source-quality regression: run `brigade research test search`; verify the
  configured backend and allowed engine IDs in `brigade research status`. Search snippets remain
  discovery pointers, so retrieve and retain the original material before relying on it.
- Browser saturation or policy block: run `brigade research test browser`; inspect the audit for
  queue/limit/policy outcomes. Do not weaken public-network protections to recover a blocked URL.
  For an authenticated profile, revoke it through the worker's profile-clear path and renew its
  explicit ownership policy before reuse.
- Citation-validator failure: the response is returned as insufficient evidence rather than an
  uncited legal conclusion. Use the correlation ID in research audit, retrieve the primary or
  authoritative material, then retry the research task. Cornell LII and similar sources remain
  secondary interpretation and should be cited as such.
- Unexpected usage volume: inspect filtered research audit records by acting principal and tool;
  stage-disable the affected component if necessary, then roll back only after the source of the
  volume is understood.

## Recovery validation

After remediation, run the matching bounded check, confirm the incident is resolved in the
Telemetry panel, and run:

```bash
./ops/brigade-live.sh health --json
```

For app changes, rebuild the application profile using the documented Compose workflow before
declaring recovery complete.
