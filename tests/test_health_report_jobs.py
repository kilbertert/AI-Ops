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


def test_health_job_marks_incomplete_work_expired_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "gateway.db"
    store = GatewayStore(database)
    job, _ = store.create_or_reuse_health_job("scope-1", "O-1", "health-v1")
    store.update_health_job(job["job_id"], status="running")

    restarted = GatewayStore(database)

    assert restarted.get_health_job(job["job_id"], "scope-1")["status"] == "expired"


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
