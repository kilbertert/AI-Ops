from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiops_diagnostics.gateway_store import GatewayStore


def test_health_job_reuses_active_and_completed_work(tmp_path: Path) -> None:
    store = GatewayStore(tmp_path / "gateway.db")

    first, created = store.create_or_reuse_health_job("scope-1", "O-1", "health-v1")
    reused, reused_created = store.create_or_reuse_health_job("scope-1", "O-1", "health-v1")

    assert created
    assert not reused_created
    assert reused["job_id"] == first["job_id"]

    store.update_health_job(first["job_id"], status="running")
    store.update_health_job(first["job_id"], status="completed", report={"summary": "ok"})
    completed, completed_created = store.create_or_reuse_health_job("scope-1", "O-1", "health-v1")
    assert not completed_created
    assert completed["report"] == {"summary": "ok"}


def test_health_job_is_isolated_by_scope_and_recovers_after_failure(tmp_path: Path) -> None:
    store = GatewayStore(tmp_path / "gateway.db")
    first, _ = store.create_or_reuse_health_job("scope-1", "O-1", "health-v1")
    store.update_health_job(first["job_id"], status="failed", error_code="SOURCE_UNAVAILABLE")

    replacement, created = store.create_or_reuse_health_job("scope-1", "O-1", "health-v1")
    other, other_created = store.create_or_reuse_health_job("scope-2", "O-1", "health-v1")

    assert created and replacement["job_id"] != first["job_id"]
    assert other_created and other["job_id"] != replacement["job_id"]
    assert store.get_health_job(replacement["job_id"], "scope-2") is None


def test_constructing_a_store_leaves_live_work_alone(tmp_path: Path) -> None:
    """A new `GatewayStore` is not a restart (#492).

    Construction happens on every short-lived CLI command (`aiops-gateway
    devices`, `issue-enrollment`, `revoke-device`). It used to converge every
    `queued`/`running` row to `expired`, so listing devices ended a diagnosis
    that was still running and the caller read a result-less `expired`. This
    test is the one the old behaviour could never have failed: it asserted the
    CONSTRUCTION was the convergence.
    """
    database = tmp_path / "gateway.db"
    store = GatewayStore(database)
    job, _ = store.create_or_reuse_health_job("scope-1", "O-1", "health-v1")
    store.update_health_job(job["job_id"], status="running")

    restarted = GatewayStore(database)

    assert restarted.get_health_job(job["job_id"], "scope-1")["status"] == "running"
    # And the job is still writable, which is what the caller of an in-flight
    # job needs: the old behaviour refused every later terminal write.
    assert restart_write_succeeds(restarted, job["job_id"]) is True


def restart_write_succeeds(store: GatewayStore, job_id: str) -> bool:
    """Whether a live job can still reach its own terminal state."""
    updated = store.update_health_job(job_id, status="failed", error_code="X")
    return updated is True


def test_the_boot_hook_converges_with_a_restart_verdict(tmp_path: Path) -> None:
    """The restart verdict belongs where the restart happened.

    `failed` with a code naming the cause, not `expired` (which means the job
    outran its deadline) and not `cancelled` (which means the user stopped it).
    `docs/validation.md` records why the three are kept apart.
    """
    from aiops_diagnostics.gateway_store import HEALTH_JOB_RESTART_ERROR_CODE

    database = tmp_path / "gateway.db"
    store = GatewayStore(database)
    job, _ = store.create_or_reuse_health_job("scope-1", "O-1", "health-v1")
    store.update_health_job(job["job_id"], status="running")

    restarted = GatewayStore(database)
    restarted.recover_interrupted_jobs()

    current = restarted.get_health_job(job["job_id"], "scope-1")
    assert current["status"] == "failed"
    assert current["error_code"] == HEALTH_JOB_RESTART_ERROR_CODE


def test_health_job_terminal_state_cannot_be_overwritten(tmp_path: Path) -> None:
    store = GatewayStore(tmp_path / "gateway.db")
    job, _ = store.create_or_reuse_health_job("scope-1", "O-1", "health-v1")
    store.update_health_job(job["job_id"], status="completed", report={"summary": "ok"})

    updated = store.update_health_job(
        job["job_id"],
        status="failed",
        error_code="LATE_FAILURE",
    )

    assert not updated
    assert store.get_health_job(job["job_id"], "scope-1")["status"] == "completed"


def test_health_job_rejects_completion_after_deadline(tmp_path: Path) -> None:
    store = GatewayStore(tmp_path / "gateway.db")
    job, _ = store.create_or_reuse_health_job("scope-1", "O-1", "health-v1")
    store.update_health_job(job["job_id"], status="running")
    expired_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with store._connection(write=True) as connection:
        connection.execute(
            "UPDATE health_report_jobs SET deadline_at = ? WHERE job_id = ?",
            (expired_at, job["job_id"]),
        )

    updated = store.update_health_job(
        job["job_id"],
        status="completed",
        report={"summary": "late"},
    )

    assert not updated
    current = store.get_health_job(job["job_id"], "scope-1")
    assert current["status"] == "expired"
    assert current["report"] is None


def test_a_restarted_report_tells_the_caller_to_retry(tmp_path: Path) -> None:
    """The restart verdict must be actionable, not just honest.

    The whole point of converging a restarted job to `failed` (rather than
    letting it hang, or calling it `expired`) is that the caller can do
    something about it. `retryable` is what the frontend renders as that action,
    and it is computed from an allowlist of codes — a new code that is not on it
    produces an error with no button, which is the outcome this ticket exists to
    avoid.
    """
    from aiops_diagnostics.gateway_api import _health_job_response
    from aiops_diagnostics.gateway_store import HEALTH_JOB_RESTART_ERROR_CODE

    body = _health_job_response(
        {
            "job_id": "job1",
            "order_no": "O-1",
            "rule_version": "v1",
            "status": "failed",
            "error_code": HEALTH_JOB_RESTART_ERROR_CODE,
            "created_at": "2026-10-01T00:00:00+00:00",
            "updated_at": "2026-10-01T00:00:00+00:00",
            "completed_at": "2026-10-01T00:00:00+00:00",
        }
    )
    assert body["error"]["retryable"] is True, body
