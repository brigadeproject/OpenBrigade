# RC Punch List (v1.0 Orchestrated Agent Harness)

Purpose: close remaining hardening and release gates with a single, executable checklist.

## 0) Scope Lock

- [x] Freeze scope to RC cleanup only (no net-new features during this pass).
- [x] Confirm target: v1.0 "Orchestrated Agent Harness" with orchestrator-floor behavior and web orchestrator chat path included.

## 1) Baseline Validation (Must Pass)

- [x] `python3 -m pytest`
- [x] `python3 -m ruff check .`
- [x] `python3 -m compileall brigade tests ops/ollama_bridge_proxy.py`
- [x] `docker compose --env-file .env.example config`
- [x] `brigade db status` (or `./ops/brigade-live.sh db status`)

Evidence to capture:
- [x] Store command outputs in `artifacts/rc-validation-baseline.txt` (or session notes).

## 2) Runtime + Recovery Validation

- [x] Run recovery smoke: `./ops/check-recovery.sh`
- [x] Run concurrency stress: `KEEP_WORK_DIR=1 PROVIDER=ollama MODEL='qwen2.5-coder:7b' ./ops/stress-concurrency.sh`
- [x] Run malformed-heartbeat smoke: `./ops/test-bad-heartbeats.sh`
- [x] Execute backup: `./ops/backup-prototype.sh`
- [x] Perform restore verification from latest backup (document exact command/path used).

Exit criteria:
- [x] No duplicate execution regressions.
- [x] No unrecovered stuck leases/claims.
- [x] Recovery and restore are reproducible from written steps.

## 3) Clean-Stack Datastore Gates

- [x] Qdrant gate: verify episode writes and source refs work on a clean stack run.
- [x] Neo4j gate: verify provenance relationships appear on a clean stack run.
- [x] Redis gate: verify queue/lease reconciliation and lock behavior remain healthy.
- [x] Postgres gate: verify migration status clean and no schema drift.

Exit criteria:
- [x] All four datastores pass in the same validation window.

## 4) Auth + Web Smoke (Auth-Enabled)

- [x] Start app profile and web stack through release path.
- [x] Run auth-enabled web smoke using token path (`BRIGADE_TOKEN` flow).
- [x] Verify orchestrator chat endpoint and markdown rendering from UI.
- [x] Verify role-aware control behavior (observer/operator/owner) for at least one denied action path.

Exit criteria:
- [x] No silent token-expiry loops.
- [x] No markdown rendering regressions in orchestrator chat page.

## 5) External Integrations Hardening (v0.9.1 Tail)

- [x] Telegram: finish setup/runbook doc, outbound reply path behavior doc, disable switch default-off verification.
- [x] Google Chat: finish setup/runbook doc, outbound reply path behavior doc, disable switch default-off verification.
- [x] OpenAI/Codex: document supported auth mode(s), invalid-credential behavior, bounded live smoke, disable switch.
- [x] Gemini: document supported auth mode(s), invalid-credential behavior, bounded live smoke, disable switch.
- [x] Enforce/verify connector rate limits + message size limits + audit records (inbound/outbound).

Exit criteria:
- [x] Every external connector/model integration is either:
  - verified in bounded smoke, or
  - explicitly disabled with documented reason and rollback path.

## 6) Release-Path Docker Checks (v0.9.3)

- [x] Build `brigade_web`.
- [x] Start full app profile.
- [x] Run `brigade health --json`.
- [x] Run `brigade db status`.
- [x] Run dashboard smoke.
- [x] Run auth-enabled web smoke.
- [x] Run recovery smoke.

Exit criteria:
- [x] Full release path passes without manual patching between steps.

## 7) Public Repo Cleanup + Reproducibility

- [x] Ensure generated artifacts are excluded (`web/node_modules/`, `web/dist/`, caches, snapshots, backups).
- [x] Confirm `package-lock.json` is present and current.
- [x] Check docs/examples/config for secrets and host-specific paths.
- [x] Verify README/PROTOTYPE commands match current CLI behavior.

Exit criteria:
- [ ] Fresh clone can follow docs and reproduce core flows. Blocked in this directory because git metadata is invalid.

## 8) Checklist/Doc Hygiene

- [x] Update stale checklist language that still describes pre-v0.7 disposable runtime assumptions.
- [ ] Normalize TODO status markers (consistent checkboxes or consistent `Implemented/Remaining` format). Current RC/v0.9.1 markers are normalized; older legacy TODO sections still contain inline completion notes.
- [x] Link this RC punch list from `TODO.md` and `RELEASE_CHECKLIST.md`.

## 9) Final RC Readiness Report

- [x] Produce `RC_READINESS.md` with:
  - `Done`
  - `Deferred`
  - `Known Risks`
  - `Rollback Notes`
  - exact validation timestamp window (UTC)

Release decision rule:
- [ ] Proceed only if no unresolved P0/P1 items remain from sections 1-7.
