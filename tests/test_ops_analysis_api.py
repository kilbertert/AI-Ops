"""运维分析面的 HTTP 契约（#697，PRD #693 的 ③ 号路径）。

从外面往里断言：客户端能拿到什么、拿不到什么、谁拿不到。作业的**计算**由
``test_ops_analysis.py`` 覆盖；这里覆盖的是**面**——入口门、id 空间、错误形状，
以及「客户端不得用这条链」这条本票明确要求的产品决定。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.ops_analysis import RULE_VERSION
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

TENANT = "T-1"
WINDOW = {"window_start": "2026-10-01T00:00:00Z", "window_end": "2026-10-02T00:00:00Z"}


class _Resolver:
    def __init__(self, permission: str = "aiops:diagnoses:write") -> None:
        self.permission = permission

    def resolve(
        self,
        token: str,
        *,
        required_scope: str,
        third_session: str | None = None,
        platform_entry: str | None = None,
        source_key: str | None = None,
    ) -> ScopeContext:
        from aiops_diagnostics.caller_auth import CallerAuthError

        if token != "token":
            raise CallerAuthError("bad token", code="caller_auth.invalid_token")
        subject = SubjectRecord(b_user_id="B-1", c_user_id="C-1", tenant_id=TENANT)
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id=TENANT,
            data_scope=DataScope(type="self"),
            roles=frozenset(),
            permissions=frozenset({required_scope}),
        )


class _Directory:
    """该会话具有管家端角色，因此 operator 入口可用。"""

    def roles_for_c_user(self, c_user_id: str, tenant_id: str):
        return (PlatformRoleRecord("B-1", c_user_id, tenant_id, "admin"),)

    def roles_for_b_user(self, b_user_id: str, tenant_id: str):
        return (PlatformRoleRecord("B-1", None, tenant_id, "admin"),)


class _Runtime:
    """记录调用并直接落一行作业——面本身的行为才是本文件的对象。"""

    def __init__(self, store: GatewayStore) -> None:
        self.store = store
        self.calls: list[dict[str, object]] = []

    def start_operations_analysis(
        self, context: ScopeContext, *, site_ids, window_start, window_end, language="zh"
    ):
        self.calls.append(
            {
                "site_ids": site_ids,
                "window_start": window_start,
                "window_end": window_end,
                "language": language,
            }
        )
        return self.store.create_analysis_job(
            context.scope_fingerprint,
            site_ids=site_ids,
            window_start=window_start,
            window_end=window_end,
            rule_version=RULE_VERSION,
            language=language,
        )

    def get_operations_analysis(self, context: ScopeContext, analysis_id: str):
        return self.store.get_standard_diagnosis(analysis_id, context.scope_fingerprint)

    def list_standard_diagnoses(self, context: ScopeContext, *, limit: int = 50):
        return self.store.list_standard_diagnoses(context.scope_fingerprint, limit=limit)

    def shutdown(self) -> None:
        return None


def _client(tmp_path: Path) -> tuple[TestClient, GatewayStore, _Runtime]:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    store = GatewayStore(settings.database_file)
    runtime = _Runtime(store)
    app = create_gateway_app(
        settings=settings,
        store=store,
        runtime=runtime,  # type: ignore[arg-type]
        caller_resolver=_Resolver(),
        platform_resolver=PlatformIdentityResolver(_Directory()),
        faq_catalog=FAQCatalog.bundled(),
    )
    return TestClient(app), store, runtime


def _headers(entry: str = "operator") -> dict[str, str]:
    return {"Authorization": "Bearer token", "X-Business-Entry": entry}


def test_the_operator_entry_creates_and_polls_an_analysis(tmp_path: Path) -> None:
    client, store, runtime = _client(tmp_path)
    with client:
        created = client.post(
            "/v1/standard/analyses",
            headers=_headers(),
            json={"site_ids": ["S-1", "S-2"], **WINDOW},
        )
        assert created.status_code == 202, created.text
        body = created.json()
        assert body["analysis_id"].startswith("an_")
        assert body["kind"] == "analysis"
        assert body["status"] == "queued"
        assert body["retry_after_ms"] == 1000
        assert body["rule_version"] == RULE_VERSION
        assert body["window_start"].startswith("2026-10-01")
        # No order identity on this face — that is the whole point of the surface.
        assert "order_no" not in body and "question" not in body and "indicator_code" not in body

        store.update_standard_diagnosis(
            body["analysis_id"],
            status="completed",
            result={"failure_rate": 0.18, "risk": "abnormal", "main_cause": "device offline"},
        )
        polled = client.get(f"/v1/standard/analyses/{body['analysis_id']}", headers=_headers())

    assert polled.status_code == 200, polled.text
    assert polled.json()["result"]["failure_rate"] == 0.18
    assert polled.json()["retry_after_ms"] is None
    assert runtime.calls[0]["site_ids"] == ("S-1", "S-2")


def test_no_site_list_means_the_callers_whole_visible_set(tmp_path: Path) -> None:
    """``site_ids=None`` 不是「全库」，是「不收窄」——收窄发生在运行时那侧。"""
    client, _, runtime = _client(tmp_path)
    with client:
        created = client.post("/v1/standard/analyses", headers=_headers(), json=WINDOW)

    assert created.status_code == 202, created.text
    assert runtime.calls[0]["site_ids"] is None


def test_the_consumer_entry_cannot_reach_the_analysis_surface(tmp_path: Path) -> None:
    """客户端**不可达**——不是「暂时没有数据」，而是一个不成立的问题。

    客户端身份的数据范围是「本人」；这张卡问的是「这一组站点的整体情况」。放它进来
    只会得到一张空卡，而空卡会被读成数据问题。所以显式拒绝。
    """
    client, _, _ = _client(tmp_path)
    with client:
        created = client.post("/v1/standard/analyses", headers=_headers("consumer"), json=WINDOW)
        polled = client.get("/v1/standard/analyses/an_" + "0" * 32, headers=_headers("consumer"))

    assert created.status_code == 403
    assert created.json()["error"]["code"] == "OPERATOR_ENTRY_REQUIRED"
    assert polled.status_code == 403


def test_an_unknown_analysis_is_not_found(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path)
    with client:
        response = client.get("/v1/standard/analyses/an_" + "0" * 32, headers=_headers())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ANALYSIS_NOT_FOUND"


def test_a_qa_id_is_not_an_analysis_id(tmp_path: Path) -> None:
    """id 空间不串：拿一个问答 id 来轮询分析面，得到的是「找不到」。"""
    client, _, _ = _client(tmp_path)
    with client:
        response = client.get("/v1/standard/analyses/qa_" + "0" * 32, headers=_headers())

    assert response.status_code == 404


def test_an_analysis_is_not_listed_as_an_order_diagnosis(tmp_path: Path) -> None:
    """同一张表、两个面：诊断列表里不得出现分析作业。"""
    client, store, _ = _client(tmp_path)
    with client:
        client.post("/v1/standard/analyses", headers=_headers(), json=WINDOW)
        listed = client.get("/v1/standard/diagnoses", headers=_headers())

    assert listed.status_code == 200
    assert listed.json()["diagnoses"] == []


def test_an_inverted_window_is_refused_before_a_job_exists(tmp_path: Path) -> None:
    client, store, _ = _client(tmp_path)
    with client:
        response = client.post(
            "/v1/standard/analyses",
            headers=_headers(),
            json={"window_start": "2026-10-02T00:00:00Z", "window_end": "2026-10-01T00:00:00Z"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ANALYSIS_UNAVAILABLE"
    assert store.count_standard_diagnoses() == 0


def test_an_explicit_empty_site_list_is_refused(tmp_path: Path) -> None:
    """显式空集合 = 请求「什么都不看」，与「不收窄」是两回事。"""
    client, store, _ = _client(tmp_path)
    with client:
        response = client.post("/v1/standard/analyses", headers=_headers(), json={"site_ids": [], **WINDOW})

    assert response.status_code == 503
    assert store.count_standard_diagnoses() == 0


@pytest.mark.parametrize("field", ["order_no", "question"])
def test_the_request_schema_forbids_order_identity(tmp_path: Path, field: str) -> None:
    """分析面不接受订单身份字段——收下它们就等于宣称这是一次诊断。"""
    client, _, _ = _client(tmp_path)
    with client:
        response = client.post("/v1/standard/analyses", headers=_headers(), json={**WINDOW, field: "O-1"})

    assert response.status_code == 422
