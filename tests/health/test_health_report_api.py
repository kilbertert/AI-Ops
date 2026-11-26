from __future__ import annotations

import contextlib
import time
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from aiops_diagnostics.common.config import Settings
from aiops_diagnostics.data.sources import COMM_STREAM_CODES, COMM_SUMMARY_CODES
from aiops_diagnostics.gateway.gateway_api import create_gateway_app
from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway.gateway_store import GatewayStore
from aiops_diagnostics.health.health_job_runner import execute_health_report
from aiops_diagnostics.health.health_metrics import RULE_VERSION
from aiops_diagnostics.identity.capability import CAPABILITY_ASSERTED
from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord


class _Resolver:
    def resolve(
        self,
        token: str,
        *,
        required_scope: str,
        third_session: str | None = None,
        platform_entry: str | None = None,
        source_key: str | None = None,
    ) -> ScopeContext:
        subject = SubjectRecord(b_user_id="B-1", c_user_id="C-1", tenant_id="T-1")
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id="T-1",
            data_scope=DataScope(type="self"),
            roles=frozenset(),
            permissions=frozenset({required_scope}),
            capability_source=CAPABILITY_ASSERTED,
        )


class _Orders:
    def can_access(self, context: ScopeContext, order_no: str) -> bool:
        return order_no == "O-1"


class _Runtime:
    def __init__(self, store: GatewayStore) -> None:
        self.store = store

    def start_health_report(self, context: ScopeContext, order_no: str, *, language: str = "zh"):
        job, _ = self.store.create_or_reuse_health_job(context.scope_fingerprint, order_no, "health-v1")
        return job

    def get_health_report(self, context: ScopeContext, job_id: str):
        return self.store.get_health_job(job_id, context.scope_fingerprint)

    def shutdown(self) -> None:
        pass


def _client(tmp_path: Path) -> tuple[TestClient, GatewayStore]:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    store = GatewayStore(settings.database_file)
    app = create_gateway_app(
        settings=settings,
        store=store,
        runtime=_Runtime(store),  # type: ignore[arg-type]
        caller_resolver=_Resolver(),
        order_authorizer=_Orders(),
    )
    return TestClient(app), store


def test_health_report_job_create_and_poll(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    with client:
        created = client.post(
            "/v1/health-report-jobs",
            headers={"Authorization": "Bearer token"},
            json={"order_no": "O-1"},
        )
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        store.update_health_job(job_id, status="running")
        store.update_health_job(job_id, status="completed", report={"summary": "ok"})
        result = client.get(
            f"/v1/health-report-jobs/{job_id}",
            headers={"Authorization": "Bearer token"},
        )

    assert result.status_code == 200
    assert result.json()["status"] == "completed"
    assert result.json()["report"] == {"summary": "ok"}
    assert "scope_fingerprint" not in result.json()


def test_health_report_job_rejects_unauthorized_order(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    with client:
        response = client.post(
            "/v1/health-report-jobs",
            headers={"Authorization": "Bearer token"},
            json={"order_no": "O-OTHER"},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ORDER_NOT_FOUND"
    assert store.count_health_jobs() == 0


def test_health_report_job_create_meets_p95_target(tmp_path: Path) -> None:
    """Off-process POST create-call stays under the documented 500 ms P95 target.

    This is the offline smoke for the PRD #87 Performance decision. Real-environment
    measurement belongs to the #92 acceptance plan; here we only assert that the
    gateway handoff (auth + scope + order authorization + store insert) does not
    blow the budget before any real source work begins.
    """
    client, _ = _client(tmp_path)
    samples_ms: list[float] = []
    with client:
        for i in range(50):
            started = time.perf_counter()
            response = client.post(
                "/v1/health-report-jobs",
                headers={"Authorization": "Bearer token"},
                json={"order_no": f"O-{i % 5}"},  # reuse to exercise the reuse path
            )
            samples_ms.append((time.perf_counter() - started) * 1000)
            assert response.status_code in {202, 404}

    p95_index = max(0, int(len(samples_ms) * 0.95) - 1)
    p95 = sorted(samples_ms)[p95_index]
    assert p95 < 500, f"create-job P95 {p95:.1f} ms exceeds 500 ms target"


def test_health_report_v3_post_execute_and_poll_end_to_end(tmp_path: Path) -> None:
    """授权 POST -> execute_health_report -> GET 端到端链路校验 health-v3 落库结构与拆分协议查询。"""
    seen_comm_code_sets: list[tuple[str, ...]] = []

    def _ykc_raw(cmd: str, payload_hex: str) -> str:
        body = f"000000{cmd}021258440253588725760000000000002026091900004101{payload_hex}"
        return f"68{len(bytes.fromhex(body)):02x}{body}0000"

    class _FakeSources:
        def get_orders(self, order_no: str, tenant_id: str | None = None):
            return [
                {
                    "order_no": order_no,
                    "status": 1,
                    "device_code": "20260919000041",
                    "child_device_code": "20260919000041-1",
                    "created_time": "2026-11-26 05:19:03",
                    "stop_time": "2026-11-26 06:31:22",
                    "stopped_reason_code": "0",
                    "stopped_reason_content": "正常停止",
                    "start_soc": 18,
                    "end_soc": 99,
                    "electricity": 45.4,
                    "transaction_id": "2125844025358872576",
                }
            ]

        def get_gun_samples(self, device, start_time, end_time, tx_serial_no):
            return [
                {
                    "_ts": "2026-11-26T05:20:00Z",
                    "outputVoltage": 350.0,
                    "outputCurrent": 120.0,
                    "power": 42000.0,
                    "soc": 18,
                    "batteryMaxTemperature": 31,
                    "chargingElectricityQuantity": 0.0,
                },
                {
                    "_ts": "2026-11-26T06:30:00Z",
                    "outputVoltage": 370.0,
                    "outputCurrent": 60.0,
                    "power": 22200.0,
                    "soc": 99,
                    "batteryMaxTemperature": 33,
                    "chargingElectricityQuantity": 45.4,
                },
            ]

        def get_pile_comm_frames(self, device, start_time, end_time, *, codes=()):
            seen_comm_code_sets.append(tuple(codes))
            if tuple(codes) == COMM_SUMMARY_CODES:
                return [
                    {
                        "_ts": "2026-11-26T05:19:10Z",
                        "code": "15",
                        "raw": _ykc_raw("15", "010100032607970cffffffff"),
                    },
                    {
                        "_ts": "2026-11-26T05:19:12Z",
                        "code": "17",
                        "raw": _ykc_raw("17", "6e0184034e02c40e6a1a02110d1027d0072e049001"),
                    },
                    {
                        "_ts": "2026-11-26T06:31:20Z",
                        "code": "19",
                        "raw": _ykc_raw("19", "6356015b0151532300ca3800000000"),
                    },
                ]
            return [
                {
                    "_ts": "2026-11-26T05:25:00Z",
                    "code": "23",
                    "raw": _ykc_raw("23", "c40e130a029e0d0c0a57f1551600ac0d0a0a1000"),
                },
                {
                    "_ts": "2026-11-26T05:25:01Z",
                    "code": "25",
                    "raw": _ykc_raw("25", "4752004e0c00000000000000000000000001000000"),
                },
            ]

    @contextlib.contextmanager
    def _fake_scoped_sources(_settings, *, scope):
        yield _FakeSources()

    fake_runtime_mod = SimpleNamespace(
        resolve_query_scope=lambda ctx: object(),
        scoped_live_sources=_fake_scoped_sources,
    )

    class _InlineV3Runtime:
        def __init__(self, store: GatewayStore) -> None:
            self.store = store
            self.diagnostic_settings = Settings.from_env()

        def start_health_report(self, context: ScopeContext, order_no: str, *, language: str = "zh"):
            job, created = self.store.create_or_reuse_health_job(
                context.scope_fingerprint, order_no, RULE_VERSION, language=language
            )
            if created:
                execute_health_report(self, job["job_id"], context, order_no, runtime_mod=fake_runtime_mod)
            return self.store.get_health_job(job["job_id"], context.scope_fingerprint)

        def get_health_report(self, context: ScopeContext, job_id: str):
            return self.store.get_health_job(job_id, context.scope_fingerprint)

        def shutdown(self) -> None:
            pass

    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    store = GatewayStore(settings.database_file)
    app = create_gateway_app(
        settings=settings,
        store=store,
        runtime=_InlineV3Runtime(store),  # type: ignore[arg-type]
        caller_resolver=_Resolver(),
        order_authorizer=_Orders(),
    )

    with TestClient(app) as client:
        created = client.post(
            "/v1/health-report-jobs",
            headers={"Authorization": "Bearer token", "Accept-Language": "zh"},
            json={"order_no": "O-1"},
        )
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        polled = client.get(
            f"/v1/health-report-jobs/{job_id}",
            headers={"Authorization": "Bearer token"},
        )

    assert polled.status_code == 200
    body = polled.json()
    assert body["status"] == "completed"
    assert body["rule_version"] == "health-v3"
    report = body["report"]
    assert report["rule_version"] == "health-v3"
    assert report["overall_health_score"] == 89.3
    assert report["overall_health_source"] == "soh"
    assert len(report["indicators"]) == 7
    assert len(report["radar"]) == 5
    assert report["conclusion"]["level"] == "normal"
    assert report["curves"]["power"]["series"][0]["points"][0][1] == 42.0
    assert seen_comm_code_sets == [COMM_SUMMARY_CODES, COMM_STREAM_CODES]
