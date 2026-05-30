from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPS_DIR = ROOT / "ops"


def _read_script(name: str) -> str:
    return (OPS_DIR / name).read_text(encoding="utf-8")


def _run_help(name: str) -> str:
    result = subprocess.run(
        ["bash", str(OPS_DIR / name), "-h"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def test_ops_scripts_are_bash_syntax_valid() -> None:
    subprocess.run(
        [
            "bash",
            "-n",
            str(OPS_DIR / "brigade-live.sh"),
            str(OPS_DIR / "stress-concurrency.sh"),
            str(OPS_DIR / "test-bad-heartbeats.sh"),
            str(OPS_DIR / "check-recovery.sh"),
            str(OPS_DIR / "v07-wipe-reseed.sh"),
        ],
        cwd=ROOT,
        check=True,
    )


def test_v07_wipe_reseed_script_requires_confirmation() -> None:
    script = _read_script("v07-wipe-reseed.sh")

    assert "--confirm-wipe" in script
    assert "./ops/backup-prototype.sh" in script
    assert "./ops/recreate-stack.sh --drop-volumes" in script
    assert "./ops/brigade-live.sh db migrate" in script
    assert "./ops/brigade-live.sh init mvp --force" in script


def test_stress_concurrency_script_exposes_defaults_and_invariant_checks() -> None:
    help_text = _run_help("stress-concurrency.sh")
    script = _read_script("stress-concurrency.sh")

    assert "PRIMARY_CLEANUP_PASSES" in help_text
    assert "CLEANUP_SLEEP_SECONDS" in help_text
    assert "CLEANUP_PROVIDER" in help_text
    assert "INCLUDE_RUN_ALL" in help_text
    assert "ensure_test_agent" in script
    assert "agent onboard" in script
    assert 'status.get("agents", [])' in script
    assert '{"status": "idle"}' in script
    assert 'if [[ "$PROVIDER" == "ollama" ]]; then' in script
    assert 'CLEANUP_PROVIDER=fake' in script
    assert "PRIMARY_CLEANUP_PASSES=6" in script
    assert "PRIMARY_CLEANUP_PASSES=3" in script
    assert 'CLEANUP_SLEEP_SECONDS="${CLEANUP_SLEEP_SECONDS:-2}"' in script
    assert '"cleanup_provider"' in script
    assert '"usage_counts_after_cleanup"' in script
    assert '"history_counts_after_cleanup"' in script
    assert '"transcript_counts_after_cleanup"' in script
    assert '"job_behavior"' in script
    assert '"cleanup_policy"' in script
    assert '"invariant_failures"' in script
    assert '"invariants_ok"' in script
    assert "execution_claim_backoff" in script
    assert "local_inference_cooldown" in script
    assert "expected at least one usage record" in script
    assert "expected exactly one archived history record" in script
    assert "expected at least one transcript record" in script
    assert "duplicate_transcript_paths_after_cleanup" in script
    assert "duplicate transcript paths detected" in script
    assert "lingering test-agent assignments after cleanup" in script
    assert "raise SystemExit(1)" in script


def test_brigade_live_uploads_host_knowledge_files_into_container() -> None:
    script = _read_script("brigade-live.sh")

    assert "DOCKER_EXEC=(docker exec)" in script
    assert "DOCKER_EXEC+=(-it)" in script
    assert 'exec "${DOCKER_EXEC[@]}" "$CONTAINER" brigade "$@"' in script
    assert '${1:-}" == "knowledge"' in script
    assert '"${2:-}" == "upload"' in script
    assert '"${2:-}" == "ingest"' in script
    assert 'docker cp "$host_path" "$CONTAINER:$upload_path"' in script
    assert 'args[$((i + 1))]="$upload_path"' in script


def test_bad_heartbeat_script_exposes_defaults_and_invariant_checks() -> None:
    help_text = _run_help("test-bad-heartbeats.sh")
    script = _read_script("test-bad-heartbeats.sh")

    assert "EXPECTED_CASE_COUNT" in help_text
    assert "KEEP_WORK_DIR" in help_text
    assert "ensure_test_agent" in script
    assert "agent onboard" in script
    assert 'status.get("agents", [])' in script
    assert '{"status": "idle"}' in script
    assert "invalid_json" in script
    assert "duplicate_conflicting_block" in script
    assert "wrong_assigned_to" in script
    assert "missing_required_fields" in script
    assert "stale_assignment_id" in script
    assert "truncated_fence" in script
    assert '"alerts_delta"' in script
    assert '"side_effect_free"' in script
    assert '"invariant_failures"' in script
    assert '"invariants_ok"' in script
    assert '"run_status"' in script
    assert "expected structured blocked result for malformed heartbeat" in script
    assert "expected alert emission for malformed heartbeat" in script
    assert "malformed heartbeat created completion side effects" in script
    assert "malformed heartbeat degraded via traceback or unclear error" in script
    assert "malformed heartbeat test cleanup" in script
    assert "cleanup left active malformed heartbeat assignments" in script
    assert "raise SystemExit(1)" in script


def test_check_recovery_script_exposes_defaults_and_invariant_checks() -> None:
    help_text = _run_help("check-recovery.sh")
    script = _read_script("check-recovery.sh")

    assert "REQUIRED_STORE_BACKEND" in help_text
    assert "TIMEOUT_SECONDS" in help_text
    assert 'REQUIRED_STORE_BACKEND="${REQUIRED_STORE_BACKEND:-PostgresStateStore}"' in script
    assert '"invariant_failures"' in script
    assert '"invariants_ok"' in script
    assert "expected store backend" in script
    assert "user count changed across recreate" in script
    assert "agent count changed across recreate" in script
    assert "team count changed across recreate" in script
    assert "goal counts changed across recreate" in script
    assert "assignment history count regressed across recreate" in script
    assert "transcript count regressed across recreate" in script
    assert "active assignments changed across recreate" in script
    assert "dashboard mission view did not render after recreate" in script
    assert "dashboard alerts view did not render after recreate" in script
    assert "dashboard teams view did not render after recreate" in script
    assert "redis inspection failed after recreate" in script
    assert "redis pending assignment queue is not reconciled after recreate" in script
    assert "timed out waiting for live prototype after recreate" in script
    assert "raise SystemExit(1)" in script
