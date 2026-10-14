"""试运行（#772 F / #781）：草稿走真实 harness，范围来自身份，不产生版本。

这一层的主张有三条，每条一个反例：

1. **草稿只能收窄工具集合。** 枚举外的工具名 → 400；`allowed_tools` 里的名字原样进
   ``DiagnosticToolExecutor``，由它 fail-closed 拦住 —— 试运行不自己实现白名单。
2. **范围不来自请求。** 请求体里多一个 ``tenant_id`` / ``site_ids`` 是 400（模型
   ``extra="forbid"``），而不是安静忽略；订单越权与不存在同为 404。
3. **不产生版本、不改路由。** 试运行只写一行 ``kind='trial'`` 的作业与运行目录证据。

这里不跑真实模型：``run_trial`` 执行会打到远端 provider，那种用例在 CI 里是噪声，
而**本地能证明的部分是接缝**——草稿的解析与拒绝、路由的鉴权与 429、作业行的
形状与审计字段、以及"跑完落终态"。真实执行的护栏一致性由
``tests/test_diagnostic_tools.py`` 覆盖（同一条执行路径）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aiops_diagnostics.agent_contracts import ALL_TOOL_NAMES
from aiops_diagnostics.dify_trial import MAX_DRAFT_PROMPT_CHARS, TrialDraft, TrialRefused
from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

TRIAL_KEY = "dify-trial-key-0123456789"
KNOWLEDGE_KEY = "dify-knowledge-key-0123456789"
PUBLISH_KEY = "dify-publish-key-0123456789"
TOOLS_KEY = "dify-tools-key-0123456789"
CONSOLE_KEY = "dify-console-key-0123456789"
DEBUG_TENANT = "T-DEBUG"
ORDER = "O-123456"


class _Runtime:
    """Records what the route asked for; the run itself is stubbed out."""

    def __init__(self, store: GatewayStore) -> None:
        self.store = store
        self.started: list[dict[str, Any]] = []
        self.busy = False

    def trial_run_context(self) -> ScopeContext:
        subject = SubjectRecord(b_user_id="aiops-admin", tenant_id=DEBUG_TENANT)
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id=DEBUG_TENANT,
            data_scope=DataScope(type="all"),
            roles=frozenset({"ROLE_AGENT_ADMIN"}),
            permissions=frozenset({"aiops:agents:manage"}),
        )

    def start_trial(self, initiator: str, order_no: str, draft: Any, language: str):
        self.started.append(
            {
                "initiator": initiator,
                "order_no": order_no,
                "tools": draft.allowed_tools,
                "prompt": draft.prompt,
                "language": language,
            }
        )
        if self.busy:
            return None
        context = self.trial_run_context()
        row = self.store.create_trial_job(
            context.scope_fingerprint,
            order_no=order_no,
            draft_fingerprint=draft.fingerprint(),
            initiator=initiator,
            tenant_id=context.effective_tenant_id,
        )
        self.store.update_standard_diagnosis(
            row["diagnosis_id"],
            status="completed",
            result={
                "status": "diagnosed",
                "report": {"summary": "试运行报告"},
                "evidence_ids": ["ev-1"],
                "blocked_tools": ["redis_sync"],
                "failed_sources": [],
                "language": language,
            },
        )
        return self.store.get_standard_diagnosis(row["diagnosis_id"], context.scope_fingerprint)

    def get_trial(self, context: ScopeContext, trial_id: str) -> dict[str, Any] | None:
        return self.store.get_standard_diagnosis(trial_id, context.scope_fingerprint)

    def shutdown(self) -> None:  # pragma: no cover - not exercised
        pass


class _Directory:
    def roles_for_c_user(self, c_user_id: str, tenant_id: str):
        return (PlatformRoleRecord("B-1", "C-1", "T-1", "admin"),)

    def roles_for_b_user(self, b_user_id: str, tenant_id: str):
        return ()


class _Orders:
    """Authorizes only ``O-1``; everything else looks like 'not found'."""

    def can_access(self, context: ScopeContext, order_no: str) -> bool:
        return order_no == ORDER


def _client(tmp_path: Path, **overrides: Any) -> tuple[TestClient, _Runtime]:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
        dify_trial_api_key=overrides.pop("dify_trial_api_key", TRIAL_KEY),
        dify_trial_debug_tenant=overrides.pop("dify_trial_debug_tenant", DEBUG_TENANT),
        **overrides,
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    store = GatewayStore(settings.database_file)
    runtime = _Runtime(store)
    app = create_gateway_app(
        settings=settings,
        store=store,
        runtime=runtime,  # type: ignore[arg-type]
        caller_resolver=None,  # type: ignore[arg-type]
        order_authorizer=_Orders(),
        platform_resolver=PlatformIdentityResolver(_Directory()),
        faq_catalog=FAQCatalog.bundled(),
    )
    return TestClient(app), runtime


def _post(client: TestClient, **body: Any) -> Any:
    payload = {"order_no": ORDER, "prompt": "请诊断这张订单。", "allowed_tools": ["order_snapshot"]}
    payload.update(body)
    return client.post("/v1/dify/trial", json=payload, headers={"Authorization": f"Bearer {TRIAL_KEY}"})


# --------------------------------------------------------------------------- #
# 凭据                                                                        #
# --------------------------------------------------------------------------- #


def test_the_trial_route_needs_its_own_key_and_is_absent_when_unconfigured(tmp_path: Path) -> None:
    """401 / 403 / 未配置 404；**别的** Dify 钥匙一律不通用。"""
    client, _ = _client(
        tmp_path,
        dify_knowledge_api_key=KNOWLEDGE_KEY,
        dify_knowledge_bindings="kb-1:T-1:KB-A",
        dify_publish_api_key=PUBLISH_KEY,
        dify_tools_api_key=TOOLS_KEY,
        dify_console_base_url="http://dify.invalid:10008",
        dify_console_api_key=CONSOLE_KEY,
    )
    assert client.post("/v1/dify/trial", json={"order_no": ORDER, "prompt": "x"}).status_code == 401
    assert _post(client, prompt="").status_code in (400, 422)
    for other in (KNOWLEDGE_KEY, PUBLISH_KEY, TOOLS_KEY, CONSOLE_KEY):
        assert (
            client.post(
                "/v1/dify/trial",
                json={"order_no": ORDER, "prompt": "x"},
                headers={"Authorization": f"Bearer {other}"},
            ).status_code
            == 403
        ), "试运行的钥匙必须独立 —— 它触发真实执行，不能与任何别的面同值"

    unconfigured, _ = _client(tmp_path, dify_trial_api_key="", dify_trial_debug_tenant="")
    assert _post(unconfigured).status_code == 404


def test_the_trial_key_must_differ_from_every_other_dify_key(tmp_path: Path) -> None:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
        dify_tools_api_key=TOOLS_KEY,
        dify_trial_api_key=TOOLS_KEY,
        dify_trial_debug_tenant=DEBUG_TENANT,
    )
    with pytest.raises(ValueError, match="AIOPS_GATEWAY_DIFY_TRIAL_API_KEY must differ"):
        settings.validate()


def test_the_trial_pair_must_be_configured_together(tmp_path: Path) -> None:
    """只给钥匙不给调试租户 ⇒ 启动错误 —— 否则它会退化成"按请求给的租户跑"。"""
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
        dify_trial_api_key=TRIAL_KEY,
    )
    with pytest.raises(ValueError, match="AIOPS_GATEWAY_DIFY_TRIAL_DEBUG_TENANT is required"):
        settings.validate()


# --------------------------------------------------------------------------- #
# 草稿                                                                        #
# --------------------------------------------------------------------------- #


def test_a_draft_can_only_narrow_the_tool_set() -> None:
    """枚举外的工具名 ⇒ 拒绝，不按字面搬运。"""
    draft = TrialDraft.from_payload({"prompt": "p", "allowed_tools": ["order_snapshot", "redis_sync"]})
    assert draft.allowed_tools == ("order_snapshot", "redis_sync")
    with pytest.raises(TrialRefused, match="未登记的工具"):
        TrialDraft.from_payload({"prompt": "p", "allowed_tools": ["order_snapshot", "drop_everything"]})


def test_a_draft_rejects_a_scope_field_rather_than_ignoring_it() -> None:
    """请求体里的租户/站点/用户字段 ⇒ **拒**，因为忽略它会让人以为它起了作用。"""
    with pytest.raises(TrialRefused, match="tenant_id"):
        TrialDraft.from_payload({"prompt": "p", "tenant_id": "T-OTHER"})
    with pytest.raises(TrialRefused, match="site_ids"):
        TrialDraft.from_payload({"prompt": "p", "allowed_tools": [], "site_ids": ["S-1"]})


def test_a_draft_needs_a_prompt_and_is_length_bounded() -> None:
    with pytest.raises(TrialRefused, match="必须带 prompt"):
        TrialDraft.from_payload({"prompt": "   "})
    with pytest.raises(TrialRefused, match="超过上限"):
        TrialDraft.from_payload({"prompt": "x" * (MAX_DRAFT_PROMPT_CHARS + 1)})


def test_the_draft_fingerprint_distinguishes_prompt_and_tools() -> None:
    """审计记的是哈希：同一草稿同哈希，换提示词或换工具都变。"""
    a = TrialDraft.from_payload({"prompt": "p", "allowed_tools": ["order_snapshot"]})
    b = TrialDraft.from_payload({"prompt": "p", "allowed_tools": ["order_snapshot"]})
    c = TrialDraft.from_payload({"prompt": "p", "allowed_tools": ["order_snapshot", "redis_sync"]})
    d = TrialDraft.from_payload({"prompt": "q", "allowed_tools": ["order_snapshot"]})
    assert a.fingerprint() == b.fingerprint()
    assert len({a.fingerprint(), c.fingerprint(), d.fingerprint()}) == 3


# --------------------------------------------------------------------------- #
# 路由：范围与拒绝                                                            #
# --------------------------------------------------------------------------- #


def test_the_request_body_cannot_carry_any_scope_field(tmp_path: Path) -> None:
    """多一个 ``tenant_id`` / ``site_ids`` 就是 400 —— 范围只由服务端配置决定。"""
    client, runtime = _client(tmp_path)
    for extra in ({"tenant_id": "T-OTHER"}, {"site_ids": ["S-1"]}, {"user_id": "U-1"}):
        response = client.post(
            "/v1/dify/trial",
            json={"order_no": ORDER, "prompt": "p", **extra},
            headers={"Authorization": f"Bearer {TRIAL_KEY}"},
        )
        assert response.status_code == 422, response.text
    assert runtime.started == []


def test_an_order_outside_the_debug_scope_is_the_same_404_as_not_found(tmp_path: Path) -> None:
    """越权与不存在同一形状（与生产一致）—— 订单号不能用来探测他人业务。"""
    client, runtime = _client(tmp_path)
    response = _post(client, order_no="O-999999")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ORDER_NOT_FOUND"
    assert runtime.started == [], "被拒的订单不应进入执行"


def test_a_busy_trial_is_429(tmp_path: Path) -> None:
    """并发超限 ⇒ 429（retryable），不是排队或静默丢弃。"""
    client, runtime = _client(tmp_path)
    runtime.busy = True
    response = _post(client)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "TRIAL_BUSY"


def test_a_trial_uses_the_draft_tools_and_reports_the_blocked_ones(tmp_path: Path) -> None:
    """跑起来的草稿用**草稿的白名单**；结果带回被挡工具与证据编号。"""
    client, runtime = _client(tmp_path)
    response = _post(client, allowed_tools=["order_snapshot"])
    assert response.status_code == 202, response.text
    trial_id = response.json()["trial_id"]
    assert trial_id.startswith("tr_")
    assert runtime.started[0]["tools"] == ("order_snapshot",)

    read = client.get(f"/v1/dify/trial/{trial_id}", headers={"Authorization": f"Bearer {TRIAL_KEY}"})
    assert read.status_code == 200
    body = read.json()
    assert body["status"] == "completed"
    assert body["result"]["evidence_ids"] == ["ev-1"]
    assert body["result"]["blocked_tools"] == ["redis_sync"]
    # 结果的 status 原样保留：超出预算时是 blocked/inconclusive，不是"失败"。
    assert body["result"]["status"] == "diagnosed"


def test_an_unknown_trial_id_is_not_found(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    assert (
        client.get("/v1/dify/trial/tr_nope", headers={"Authorization": f"Bearer {TRIAL_KEY}"}).status_code
        == 404
    )


# --------------------------------------------------------------------------- #
# 作业行：审计与"不产生版本"                                                   #
# --------------------------------------------------------------------------- #


def test_the_trial_row_records_the_initiator_and_the_draft_fingerprint(tmp_path: Path) -> None:
    """审计：发起人、订单号、草稿摘要哈希、结果状态 —— 都不含明文提示词。"""
    from aiops_diagnostics.gateway_store import GatewayStore as Store

    client, _ = _client(tmp_path)
    response = client.post(
        "/v1/dify/trial",
        json={"order_no": ORDER, "prompt": "机密提示词", "allowed_tools": ["known_runbook"]},
        headers={"Authorization": f"Bearer {TRIAL_KEY}", "X-AIOps-Initiator": "B-42"},
    )
    trial_id = response.json()["trial_id"]
    row = Store(tmp_path / "gateway.db").get_standard_diagnosis(trial_id, _fingerprint_of(tmp_path))
    assert row is not None
    assert row["kind"] == "trial"
    assert row["trial_initiator"] == "B-42"
    assert row["order_no"] == ORDER
    # 哈希而不是提示词本身：审计要回答"是不是同一份草稿"，不需要抄一遍运营的文字。
    assert row["trial_draft_fingerprint"]
    assert "机密提示词" not in str(row["trial_draft_fingerprint"])
    assert "机密提示词" not in str(row.get("question") or "")


def test_a_trial_writes_no_agent_version(tmp_path: Path) -> None:
    """试运行**不产生智能体版本、不写注册表** —— 跑一次不等于发布。

    两条都查**真表**，而不是"没报错就算没写"：这是本票明确承诺的副作用边界。
    """
    import sqlite3

    from aiops_diagnostics.dify_app_registry import DifyAppRegistry

    client, _ = _client(tmp_path)
    _post(client, allowed_tools=list(ALL_TOOL_NAMES))
    with sqlite3.connect(tmp_path / "gateway.db") as connection:
        versions = connection.execute("SELECT COUNT(*) FROM agent_versions").fetchone()[0]
    assert versions == 0, "试运行产生了一个智能体版本 —— 跑一次不等于发布"
    # 注册表里没有任何绑定；试运行不该凭空写一条。
    assert DifyAppRegistry(tmp_path / "gateway.db").all() == ()


def _fingerprint_of(tmp_path: Path) -> str:
    """The debug tenant's scope fingerprint, read the same way the route builds it."""
    subject = SubjectRecord(b_user_id="aiops-admin", tenant_id=DEBUG_TENANT)
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id=DEBUG_TENANT,
        data_scope=DataScope(type="all"),
        roles=frozenset({"ROLE_AGENT_ADMIN"}),
        permissions=frozenset({"aiops:agents:manage"}),
    ).scope_fingerprint


# --------------------------------------------------------------------------- #
# 接缝：草稿真的到达 run_agent_diagnosis（与 CLI 同一个入口）                    #
# --------------------------------------------------------------------------- #


def test_the_draft_reaches_the_same_run_entry_the_cli_uses(tmp_path: Path, monkeypatch) -> None:
    """**本票的核心接缝**：试运行不是第二个执行器 —— 草稿经同一个 ``run_agent_diagnosis``
    进去，methodology 与 allowed_tools 就是草稿值，其余（fixture=None、scope、budget）
    由那条公用路径决定。

    这里只替换**模型会话**那一层（与 ``test_operator_diagnosis_entry`` 同一手法），
    驱动的是**真** ``GatewayRuntime._execute_trial``，所以断言的是真的接线而不是替身。
    """
    from aiops_diagnostics.agent_contracts import (
        AgentDiagnosis,
        Confidence,
        DiagnosisStatus,
    )
    from aiops_diagnostics.config import Settings
    from aiops_diagnostics.gateway_runtime import GatewayRuntime

    recorded: list[dict] = []

    def diagnose(workspace, request, selected_settings, fixture, **kwargs):
        recorded.append({"fixture": fixture, "order_no": request.order_no, **kwargs})
        return AgentDiagnosis(
            incident_id=workspace.load_manifest().incident_id,
            order_no=request.order_no,
            tenant_id=request.tenant_id,
            status=DiagnosisStatus.DIAGNOSED,
            summary="已完成诊断",
            root_cause="测试根因",
            confidence=Confidence.HIGH,
            evidence_ids=[],
        )

    settings = Settings()
    settings.agent.run_root = str(tmp_path / "runs")
    gateway_settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
        dify_trial_api_key=TRIAL_KEY,
        dify_trial_debug_tenant=DEBUG_TENANT,
    )
    gateway_settings.server_config_file.write_text("# test\n", encoding="utf-8")
    store = GatewayStore(gateway_settings.database_file)
    monkeypatch.setattr("aiops_diagnostics.gateway_runtime.Settings.from_config", lambda *_: settings)
    monkeypatch.setattr("aiops_diagnostics.dify_trial.run_agent_diagnosis", diagnose)
    runtime = GatewayRuntime(store, gateway_settings, settings)
    # 刻意**不**替换 trial_run_context：这个测试要证的就是真运行时的范围构造
    # （它曾经用 admin_context 的 self 范围，连一张订单都解析不出来 —— 见生产代码注释）。
    app = create_gateway_app(
        settings=gateway_settings,
        store=store,
        runtime=runtime,
        caller_resolver=None,  # type: ignore[arg-type]
        order_authorizer=_Orders(),
        platform_resolver=PlatformIdentityResolver(_Directory()),
        faq_catalog=FAQCatalog.bundled(),
    )
    client = TestClient(app)
    response = client.post(
        "/v1/dify/trial",
        json={"order_no": ORDER, "prompt": "草稿方法论", "allowed_tools": ["order_snapshot", "redis_sync"]},
        headers={"Authorization": f"Bearer {TRIAL_KEY}"},
    )
    assert response.status_code == 202, response.text
    trial_id = response.json()["trial_id"]

    for _ in range(50):
        read = client.get(f"/v1/dify/trial/{trial_id}", headers={"Authorization": f"Bearer {TRIAL_KEY}"})
        if read.json()["status"] in {"completed", "failed"}:
            break
        import time as _time

        _time.sleep(0.05)
    assert recorded, read.json()
    call = recorded[0]
    assert call["methodology"] == "草稿方法论"
    assert tuple(call["allowed_tools"]) == ("order_snapshot", "redis_sync")
    # 真数据源：试运行**不是** fixture 回放，这正是它与「预览」的区别。
    assert call["fixture"] is None
    assert call["order_no"] == ORDER
    # 范围来自配置的调试租户，不是请求。
    assert call["scope"].tenant_id == DEBUG_TENANT
    runtime.shutdown()
