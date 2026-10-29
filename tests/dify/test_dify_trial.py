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

from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord

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

    class _EmptyRegistry:
        def all(self) -> list[Any]:
            return []

    #: ``app_id`` 那条路要按**绑定表**查（与发布路径同一张）。本文件不测拉草稿的
    #: 成功路径（那要真连 Dify），只测"未登记 ⇒ 不进入执行器"，所以给一张空表。
    dify_app_registry = _EmptyRegistry()

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

    def trial_context_for(self, initiator: Any) -> ScopeContext:
        # #782 的桩：**故意与 trial_run_context 不同范围**（站点集合 + 主体类型都不同），
        # 因为两个 context 相等恰好是"GET 用错 context 读不回来"那个缺陷的藏身处 ——
        # 相等时它不会红，而线上按人范围一启用就 404。范围规则本身由
        # tests/dify/test_dify_trial_scope.py 覆盖，这里只保证两个指纹**可区分**。
        subject = SubjectRecord(
            b_user_id=initiator.b_user_id,
            tenant_id=initiator.tenant_id,
            user_type=initiator.user_type,
        )
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id=initiator.tenant_id,
            data_scope=DataScope(type="organ", site_ids=("S-1", "S-2")),
            roles=frozenset(),
            permissions=frozenset(),
        )

    def start_trial(self, *args, **kwargs):
        self.started.append((args, kwargs))
        raise AssertionError("unbound trial must never execute")

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


#: 本文件里绑定行的默认租户（`_Runtime` 注册表里那一条）。
_DEFAULT_TENANT = "T-1"


def _seed_default_workspace(db: Path) -> None:
    """给默认租户种一行 `ready`（#885）。既有用例只关心"试运行能不能跑"，不该全红。"""
    from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces

    workspaces = DifyWorkspaces(db)
    with workspaces._connection(write=True) as connection:  # noqa: SLF001 — 测试造的是行
        connection.execute(
            "INSERT OR REPLACE INTO dify_workspaces (tenant_id, environment, workspace_id,"
            " setup_state, created_at, updated_at) VALUES (?, ?, ?, 'ready',"
            " '2026-10-21T00:00:00+00:00', '2026-10-21T00:00:00+00:00')",
            (_DEFAULT_TENANT, "41", "ws-default"),
        )


def _client(tmp_path: Path, **overrides: Any) -> tuple[TestClient, _Runtime]:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
        dify_trial_api_key=overrides.pop("dify_trial_api_key", TRIAL_KEY),
        dify_trial_debug_tenant=overrides.pop("dify_trial_debug_tenant", DEBUG_TENANT),
        # 部署身份（#885）：试运行的草稿按 (绑定租户, 环境) 解析。
        environment=overrides.pop("environment", "41"),
        **overrides,
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    store = GatewayStore(settings.database_file)
    _seed_default_workspace(settings.database_file)
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
    payload = {"order_no": ORDER, "app_id": "11111111-2222-3333-4444-555555555555"}
    payload.update(body)
    return client.post("/v1/dify/trial", json=payload, headers={"Authorization": f"Bearer {TRIAL_KEY}"})


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


def test_an_unknown_trial_id_is_not_found(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    assert (
        client.get("/v1/dify/trial/tr_nope", headers={"Authorization": f"Bearer {TRIAL_KEY}"}).status_code
        == 404
    )


def test_a_failed_trial_reads_back_with_a_human_reason(tmp_path: Path) -> None:
    """失败的作业读回来时带**一句给人看的原因**，与诊断面同一张多语言表。

    只给 ``error_code`` 不够：控制台面板背后是运营，``TRIAL_FAILED`` 不告诉他该做什么。
    反证：去掉那一行映射，``error_message`` 会落到 ``generic``（"稍后重试"）—— 欠费这种
    需要人去充值的失败会被说成"过一会儿再试"，而它与代码缺陷长得一模一样。
    """
    from aiops_diagnostics.i18n import diagnosis_error_message

    client, runtime = _client(tmp_path)
    # 直接建一条**失败**的作业行：真 worker 的失败路径由 `gateway_runtime` 覆盖，
    # 这里只验"读回面把原因给出来"这一件事。不经提交路径是因为终态行不可改写
    # （completed → failed 会落进 claim-guard 的拒绝分支，那正是它该做的）。
    context = runtime.trial_run_context()
    row = runtime.store.create_trial_job(
        context.scope_fingerprint, order_no=ORDER, draft_fingerprint="d", initiator="t"
    )
    trial_id = str(row["diagnosis_id"])
    runtime.store.update_standard_diagnosis(
        trial_id, status="failed", error_code="TRIAL_FAILED", error_message="ProviderOutage: quota"
    )
    read = client.get(f"/v1/dify/trial/{trial_id}", headers={"Authorization": f"Bearer {TRIAL_KEY}"})
    assert read.status_code == 200
    body = read.json()
    assert body["status"] == "failed"
    assert body["error_code"] == "TRIAL_FAILED"
    assert body["error_message"] == diagnosis_error_message("zh", "TRIAL_FAILED")
    # 这句必须**不是**兜底那句：兜底是"稍后重试"，对欠费是误导。
    assert body["error_message"] != diagnosis_error_message("zh", "SOMETHING_UNMAPPED")
