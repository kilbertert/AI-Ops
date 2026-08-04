import hashlib
import sqlite3
from pathlib import Path

import pytest

from aiops_diagnostics.gateway_store import (
    AuthenticationError,
    EnrollmentError,
    GatewayStore,
    RunNotFoundError,
)


def test_enrollment_is_one_time_and_server_stores_only_hashes(tmp_path: Path) -> None:
    store = GatewayStore(tmp_path / "gateway" / "gateway.db")
    code = store.issue_enrollment(workspace_id="ops", tenant_id="tenant-a")

    enrolled = store.redeem_enrollment(code, device_name="windows-laptop", platform="win32")
    device = store.authenticate_device(enrolled.token)

    assert device.workspace_id == "ops"
    assert device.tenant_id == "tenant-a"
    with pytest.raises(EnrollmentError):
        store.redeem_enrollment(code, device_name="second", platform="linux")

    connection = sqlite3.connect(store.path)
    try:
        code_hash = connection.execute("SELECT code_hash FROM enrollment_codes").fetchone()[0]
        token_hash = connection.execute("SELECT token_hash FROM devices").fetchone()[0]
    finally:
        connection.close()
    assert code not in store.path.read_bytes().decode("latin-1")
    assert enrolled.token not in store.path.read_bytes().decode("latin-1")
    assert code_hash == hashlib.sha256(code.encode()).hexdigest()
    assert token_hash == hashlib.sha256(enrolled.token.encode()).hexdigest()


def test_revoked_device_cannot_authenticate(tmp_path: Path) -> None:
    store = GatewayStore(tmp_path / "gateway.db")
    code = store.issue_enrollment(workspace_id="ops")
    enrolled = store.redeem_enrollment(code, device_name="host", platform="linux")

    assert store.revoke_device(enrolled.device.device_id) is True
    with pytest.raises(AuthenticationError):
        store.authenticate_device(enrolled.token)


def test_runs_and_events_are_isolated_by_workspace(tmp_path: Path) -> None:
    store = GatewayStore(tmp_path / "gateway.db")
    first = _enroll(store, "workspace-a", "device-a")
    second = _enroll(store, "workspace-b", "device-b")
    store.create_run(
        run_id="run-1",
        workspace_id=first.device.workspace_id,
        incident_id="incident-1",
        problem="amount mismatch",
        order_no="ORDER-1",
        tenant_id=None,
        key_slot="primary",
        fixture_name=None,
        created_by_device=first.device.device_id,
    )
    event = store.append_event("run-1", {"type": "diagnosis_started"})
    store.update_run(
        "run-1",
        status="diagnosed",
        confidence="medium",
        summary="diagnosed",
        result={"status": "diagnosed"},
    )

    assert event["sequence"] == 1
    assert store.get_run("run-1", first.device.workspace_id)["status"] == "diagnosed"
    assert store.list_events("run-1", first.device.workspace_id)[0]["type"] == "diagnosis_started"
    with pytest.raises(RunNotFoundError):
        store.get_run("run-1", second.device.workspace_id)
    with pytest.raises(RunNotFoundError):
        store.list_events("run-1", second.device.workspace_id)


def test_run_problem_is_redacted_before_gateway_persistence(tmp_path: Path) -> None:
    store = GatewayStore(tmp_path / "gateway.db")
    enrolled = _enroll(store, "workspace-a", "device-a")
    store.create_run(
        run_id="run-redacted",
        workspace_id=enrolled.device.workspace_id,
        incident_id="incident-redacted",
        problem="订单 ORDER-1 手机号 13812345678，token=secret-value",
        order_no="ORDER-1",
        tenant_id=None,
        key_slot="primary",
        fixture_name=None,
        created_by_device=enrolled.device.device_id,
    )
    stored = store.get_run("run-redacted", enrolled.device.workspace_id)
    assert stored["problem"] == "订单 ORDER-1 手机号=REDACTED，token=REDACTED"


def _enroll(store: GatewayStore, workspace_id: str, device_name: str):
    code = store.issue_enrollment(workspace_id=workspace_id)
    return store.redeem_enrollment(code, device_name=device_name, platform="test")
