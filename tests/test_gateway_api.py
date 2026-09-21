from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aiops_diagnostics.agent_contracts import IncidentManifest, ToolName
from aiops_diagnostics.agent_workspace import AgentWorkspace
from aiops_diagnostics.config import Settings
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_runtime import GatewayRuntime
from aiops_diagnostics.gateway_store import GatewayDevice, GatewayStore
from aiops_diagnostics.journal import EvidenceJournal
from aiops_diagnostics.models import DiagnosticRequest, Intent
from aiops_diagnostics.order_visibility import DeviceTenantError, resolve_device_tenant


class _FakeRuntime:
    def __init__(self, store: GatewayStore) -> None:
        self.store = store
        self.counter = 0
        self.entry_guard_calls = 0

    def start_run(self, device, *, problem, order_no, tenant_id, key_slot, provider, fixture_name):
        # The real runtime refuses a mismatching tenant through the shared entry
        # guard (#331); the fake applies the same single definition so what is
        # under test here is the HTTP layer's mapping, not a second rule. The run
        # is stored under the *resolved* tenant, exactly as the real runtime
        # stores the effective one.
        self.entry_guard_calls += 1
        effective_tenant = resolve_device_tenant(device.tenant_id, tenant_id)
        self.counter += 1
        run_id = f"run-fake-{self.counter}"
        run = self.store.create_run(
            run_id=run_id,
            workspace_id=device.workspace_id,
            incident_id=f"incident-{self.counter}",
            problem=problem,
            order_no=order_no or "ORDER-UNKNOWN",
            tenant_id=effective_tenant,
            key_slot=key_slot or "primary",
            provider=provider,
            fixture_name=fixture_name,
            created_by_device=device.device_id,
        )
        self.store.append_event(run_id, {"type": "diagnosis_started"})
        self.store.update_run(
            run_id,
            status="diagnosed",
            confidence="medium",
            summary="fake diagnosis",
            result={"status": "diagnosed"},
        )
        return run

    def shutdown(self) -> None:
        pass

    def list_evidence(self, run_id: str) -> list[dict]:
        return []


def test_gateway_enrollment_and_cross_device_run_sync(tmp_path: Path) -> None:
    database = tmp_path / "gateway.db"
    config = tmp_path / "production.env"
    config.write_text("# test config\n", encoding="utf-8")
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=database,
        server_config_file=config,
    )
    store = GatewayStore(database)
    runtime = _FakeRuntime(store)
    app = create_gateway_app(settings=settings, store=store, runtime=runtime)

    with TestClient(app) as client:
        assert client.get("/health").json()["business_mutations"] == "disabled"
        code = store.issue_enrollment(workspace_id="ops", tenant_id="tenant-a")
        first = client.post(
            "/v1/enroll",
            json={"code": code, "device_name": "windows", "platform": "win32"},
        )
        assert first.status_code == 201
        second_code = store.issue_enrollment(workspace_id="ops", tenant_id="tenant-a")
        second = client.post(
            "/v1/enroll",
            json={"code": second_code, "device_name": "linux", "platform": "linux"},
        )
        headers_a = {"Authorization": f"Bearer {first.json()['token']}"}
        headers_b = {"Authorization": f"Bearer {second.json()['token']}"}

        created = client.post(
            "/v1/runs",
            headers=headers_a,
            json={"problem": "amount mismatch", "order_no": "ORDER-1"},
        )
        assert created.status_code == 202
        run_id = created.json()["run_id"]

        listed = client.get("/v1/runs", headers=headers_b)
        assert listed.status_code == 200
        assert listed.json()["runs"][0]["run_id"] == run_id
        events = client.get(f"/v1/runs/{run_id}/events", headers=headers_b)
        assert events.status_code == 200
        assert events.json()["events"][0]["type"] == "diagnosis_started"
        with client.stream("GET", f"/v1/runs/{run_id}/events/stream", headers=headers_b) as stream:
            body = "".join(stream.iter_text())
        assert "diagnosis_started" in body
        assert "stream_end" in body

        unauthorized = client.get("/v1/runs", headers={"Authorization": "Bearer invalid"})
        assert unauthorized.status_code == 401


def test_gateway_rejects_cross_tenant_requests(tmp_path: Path) -> None:
    config = tmp_path / "production.env"
    config.write_text("# test config\n", encoding="utf-8")
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=config,
    )
    store = GatewayStore(settings.database_file)
    runtime = _FakeRuntime(store)
    app = create_gateway_app(settings=settings, store=store, runtime=runtime)

    with TestClient(app) as client:
        code = store.issue_enrollment(workspace_id="ops", tenant_id="tenant-a")
        enrolled = client.post(
            "/v1/enroll",
            json={"code": code, "device_name": "windows", "platform": "win32"},
        )
        headers = {"Authorization": f"Bearer {enrolled.json()['token']}"}
        rejected = client.post(
            "/v1/runs",
            headers=headers,
            json={"problem": "amount mismatch", "order_no": "ORDER-1", "tenant_id": "tenant-b"},
        )
        assert rejected.status_code == 403


def test_cross_tenant_request_is_refused_once_with_one_error(tmp_path: Path) -> None:
    """A mismatching tenant yields one status code and one error (#331).

    The entry guard existed twice: this route compared the request tenant
    against the enrolled one and answered 403, then the runtime compared the
    very same two values and answered 400. It is now one definition the runtime
    calls, so the request is refused exactly once — inside the runtime, not at
    the edge, and never with a second status code behind the first.
    """
    config = tmp_path / "production.env"
    config.write_text("# test config\n", encoding="utf-8")
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=config,
    )
    store = GatewayStore(settings.database_file)
    runtime = _FakeRuntime(store)
    app = create_gateway_app(settings=settings, store=store, runtime=runtime)

    with TestClient(app) as client:
        code = store.issue_enrollment(workspace_id="ops", tenant_id="tenant-a")
        token = client.post(
            "/v1/enroll",
            json={"code": code, "device_name": "windows", "platform": "win32"},
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        rejected = client.post(
            "/v1/runs",
            headers=headers,
            json={"problem": "amount mismatch", "order_no": "ORDER-1", "tenant_id": "tenant-b"},
        )
        assert rejected.status_code == 403
        assert rejected.json()["detail"] == "requested tenant does not match the enrolled device scope"
        # Refused once, by the one definition: the runtime was entered and the
        # guard ran there, instead of the edge refusing before the runtime.
        assert runtime.entry_guard_calls == 1

        # The same tenant as the enrolled one is the run's own scope, not a
        # second tenant to match.
        accepted = client.post(
            "/v1/runs",
            headers=headers,
            json={"problem": "amount mismatch", "order_no": "ORDER-1", "tenant_id": "tenant-a"},
        )
        assert accepted.status_code == 202
        assert runtime.entry_guard_calls == 2


def test_gateway_run_tenant_id_is_judged_only_by_the_shared_entry_rule(tmp_path: Path) -> None:
    """``tenant_id`` carries no character pattern: the entry rule decides (#331).

    The transport must not hold a second tenant decision. A pattern here would
    refuse a blank tenant as an invalid request even though the shared rule
    already treats a blank one as *absent* — the enrolled tenant, not a second
    tenant to match — and would refuse a padded identifier that normalizes onto
    the enrolled tenant, changing a public request contract (202 -> 422) for
    input the rule already accepts. So this surface keeps only the length bound
    and lets ``resolve_device_tenant`` answer: the guard is reached for every
    request, and the status code is the guard's, never the validator's.
    """
    config = tmp_path / "production.env"
    config.write_text("# test config\n", encoding="utf-8")
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=config,
    )
    store = GatewayStore(settings.database_file)
    runtime = _FakeRuntime(store)
    app = create_gateway_app(settings=settings, store=store, runtime=runtime)

    with TestClient(app) as client:
        code = store.issue_enrollment(workspace_id="ops", tenant_id="tenant-a")
        token = client.post(
            "/v1/enroll",
            json={"code": code, "device_name": "windows", "platform": "win32"},
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        # (requested tenant, expected status, expected run tenant)
        cases = [
            # Blank is an absent tenant: the run takes the enrolled one.
            ("", 202, "tenant-a"),
            # Padded normalizes onto the enrolled tenant: one normalization, so
            # it is the run's own scope rather than a mismatch.
            (" tenant-a ", 202, "tenant-a"),
            # Genuinely outside the enrolled scope: refused by the guard.
            ("tenant-b", 403, None),
            # Punctuation the rule cannot reconcile with the enrolled tenant.
            ("tenant a", 403, None),
            ("tenant/a", 403, None),
        ]
        for tenant_id, status_code, run_tenant in cases:
            response = client.post(
                "/v1/runs",
                headers=headers,
                json={"problem": "amount mismatch", "order_no": "ORDER-1", "tenant_id": tenant_id},
            )
            assert response.status_code == status_code, tenant_id
            if status_code == 202:
                assert response.json()["tenant_id"] == run_tenant
            else:
                assert (
                    response.json()["detail"] == "requested tenant does not match the enrolled device scope"
                )

        # Every request reached the guard: none was refused at the edge.
        assert runtime.entry_guard_calls == len(cases)


def test_runtime_start_run_refuses_a_mismatching_tenant_with_one_error(tmp_path: Path) -> None:
    """The runtime refuses through the shared entry rule, not a copy of it.

    The guard used to live here as ``_tenant_for_device`` raising a bare
    ``ValueError``, which the HTTP layer answered with 400 while the entry
    answered 403. It is now the shared definition's domain error, so a direct
    runtime caller gets the same single refusal the HTTP entry maps: one
    condition, one exception type, one status code. The guard runs before
    anything is queued, so no diagnosis is started.
    """
    database = tmp_path / "gateway.db"
    config = tmp_path / "production.env"
    config.write_text("# test config\n", encoding="utf-8")
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=database,
        server_config_file=config,
    )
    store = GatewayStore(database)
    runtime = GatewayRuntime(store, settings, Settings())
    device = GatewayDevice(
        device_id="dev-1",
        workspace_id="ops",
        tenant_id="tenant-a",
        name="windows",
        platform="win32",
    )
    try:
        with pytest.raises(DeviceTenantError) as excinfo:
            runtime.start_run(
                device,
                problem="amount mismatch",
                order_no="ORDER-1",
                tenant_id="tenant-b",
                key_slot=None,
                provider=None,
                fixture_name=None,
            )
    finally:
        runtime.shutdown()
    assert isinstance(excinfo.value, DeviceTenantError)
    assert str(excinfo.value) == "requested tenant does not match the enrolled device scope"
    assert store.list_runs("ops") == []


def test_gateway_evidence_endpoint_requires_run_and_auth(tmp_path: Path) -> None:
    database = tmp_path / "gateway.db"
    config = tmp_path / "production.env"
    config.write_text("# test config\n", encoding="utf-8")
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=database,
        server_config_file=config,
    )
    store = GatewayStore(database)
    app = create_gateway_app(settings=settings, store=store, runtime=_FakeRuntime(store))

    with TestClient(app) as client:
        code = store.issue_enrollment(workspace_id="ops", tenant_id="tenant-a")
        token = client.post(
            "/v1/enroll",
            json={"code": code, "device_name": "windows", "platform": "win32"},
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        run_id = client.post(
            "/v1/runs", headers=headers, json={"problem": "amount", "order_no": "O1"}
        ).json()["run_id"]

        ok = client.get(f"/v1/runs/{run_id}/evidence", headers=headers)
        assert ok.status_code == 200
        assert ok.json()["evidence"] == []

        missing = client.get("/v1/runs/run-nope/evidence", headers=headers)
        assert missing.status_code == 404

        unauth = client.get(f"/v1/runs/{run_id}/evidence")
        assert unauth.status_code == 401


def test_gateway_runtime_list_evidence_returns_metadata_without_payload(tmp_path: Path) -> None:
    database = tmp_path / "gateway.db"
    config = tmp_path / "production.env"
    config.write_text("# test config\n", encoding="utf-8")
    gateway_settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=database,
        server_config_file=config,
    )
    store = GatewayStore(database)
    runtime = GatewayRuntime(store, gateway_settings, Settings())

    request = DiagnosticRequest(order_no="O1", tenant_id="t1", problem="p", intent=Intent("general"))
    manifest = IncidentManifest.from_request(request)
    workspace = AgentWorkspace.create(Path(__file__).parents[1], tmp_path / "runs", manifest)
    journal = EvidenceJournal(workspace, manifest)
    journal.record(
        tool=ToolName.ORDER_SNAPSHOT,
        source="mysql:ch_order_info",
        status="success",
        request={"order_no": "O1", "tenant_id": "t1"},
        payload={"orders": [{"order_no": "O1", "tenant_id": "t1", "status": 2}]},
        row_count=1,
    )

    evidence = runtime.list_evidence(workspace.run_id)
    assert len(evidence) == 1
    entry = evidence[0]
    assert entry["evidence_id"] == "ev-001"
    assert entry["tool"] == "order_snapshot"
    assert entry["status"] == "success"
    assert entry["row_count"] == 1
    # Business payload (order rows) must NOT be exposed to the client.
    assert "orders" not in entry
    assert "payload" not in entry
