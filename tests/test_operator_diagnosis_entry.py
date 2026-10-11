"""管家端诊断由已发布的 operations 版本驱动（#695，PRD #693 的 ② 号路径）。

从**运行时**这一层取证，走真实的注册表读取、真实的版本选择与真实的作业终态写入；
唯一被替换的是模型会话（``run_agent_diagnosis``）——它把「这次诊断拿到的方法论」
记下来，这正是本票要证明的那一跳。

四条判据各自对应一种会静默放行的退化：

* 登记表点名了谁 —— 提示词就该是谁的（换标记可证伪）；
* consumer 入口 —— 注入必须为空，逐字保持本票之前的行为；
* 未登记 / 名字解析不出 / 类型不符 —— 作业终态为 ``failed`` + ``DIAGNOSIS_AGENT_UNREGISTERED``，
  **不回退**烤死提示词；
* 未采用登记表的环境 —— 与 consumer 同形（空方法论），旧行为零回归。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from aiops_diagnostics.agent_contracts import AgentDiagnosis, Confidence, DiagnosisStatus
from aiops_diagnostics.agent_lifecycle import AgentConfig, AgentManager, AgentStore
from aiops_diagnostics.config import Settings
from aiops_diagnostics.dify_app_registry import DifyAppBinding, DifyAppRegistry
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_runtime import GatewayRuntime
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

TENANT = "T-1"

#: 运维配方正文里的一个**只可能来自那一版**的标记（#695 的验收判据）。
METHODOLOGY_MARKER = "OPS-METHOD-V1-先核对设备上报再下结论"


class _PublishOkResolver:
    def validate(self, tenant_id: str, knowledge_base_ids: tuple[str, ...]) -> None:
        del tenant_id, knowledge_base_ids


def _admin_context() -> ScopeContext:
    subject = SubjectRecord(b_user_id="B-1", tenant_id=TENANT)
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id=TENANT,
        data_scope=DataScope(type="self"),
        roles=frozenset({"ROLE_AGENT_ADMIN"}),
        permissions=frozenset({"aiops:agents:manage"}),
    )


def _scope() -> ScopeContext:
    subject = SubjectRecord(b_user_id="B-1", c_user_id="C-1", tenant_id=TENANT)
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id=TENANT,
        data_scope=DataScope(type="self"),
        roles=frozenset(),
        permissions=frozenset({"aiops:diagnoses:write"}),
    )


def _publish_operations(db_path: Path, *, name: str) -> None:
    """发布一份 operations 版本，提示词带本票的标记。"""
    _publish_agent(AgentStore(db_path), name=name, agent_type="operations", prompt=METHODOLOGY_MARKER)


def _bind_operator(db_path: Path, agent_name: str, *, tenant_id: str = TENANT) -> None:
    """登记一行 ``(租户, operator)``——运营动作的数据半边，不是新代码。"""
    DifyAppRegistry(db_path).put(
        DifyAppBinding(tenant_id=tenant_id, business_entry="operator", app_id="app-1", agent_name=agent_name)
    )


def _publish_agent(store: AgentStore, *, name: str, agent_type: str, prompt: str) -> str:
    manager = AgentManager(store, knowledge_resolver=_PublishOkResolver())
    admin = _admin_context()
    contract = "diagnosis-v1" if agent_type == "operations" else "blocks-v1"
    knowledge = () if agent_type == "operations" else ("kb-a",)
    agent = manager.create(
        admin,
        name=name,
        description="",
        config=AgentConfig(
            agent_type=agent_type,
            prompt=prompt,
            knowledge_base_ids=knowledge,
            model="aiops-api",
            output_contract=contract,
        ),
    )
    manager.publish(admin, agent.agent_id, expected_revision=agent.revision)
    return agent.agent_id


def _runtime(tmp_path: Path, monkeypatch) -> tuple[GatewayRuntime, GatewayStore, list[dict]]:
    """真实运行时 + 被替换的模型会话，记录每次诊断收到的方法论。"""
    gateway_settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    gateway_settings.server_config_file.write_text("# test\n", encoding="utf-8")
    settings = Settings()
    settings.agent.run_root = str(tmp_path / "runs")
    store = GatewayStore(gateway_settings.database_file)
    agent_store = AgentStore(gateway_settings.database_file)
    recorded: list[dict] = []

    def diagnose(workspace, request, selected_settings, fixture, **kwargs):
        recorded.append(kwargs)
        return AgentDiagnosis(
            incident_id=workspace.load_manifest().incident_id,
            order_no=request.order_no,
            tenant_id=request.tenant_id,
            status=DiagnosisStatus.DIAGNOSED,
            summary="已完成诊断",
            root_cause="测试根因",
            confidence=Confidence.HIGH,
            evidence_ids=[],
            hypotheses=[],
            limitations=[],
            failed_sources=[],
            next_steps=[],
        )

    monkeypatch.setattr("aiops_diagnostics.gateway_runtime.Settings.from_config", lambda *_: settings)
    monkeypatch.setattr("aiops_diagnostics.gateway_runtime.run_agent_diagnosis", diagnose)
    runtime = GatewayRuntime(store, gateway_settings, settings, agent_store=agent_store)
    return runtime, store, recorded


def _run(runtime: GatewayRuntime, store: GatewayStore, **kwargs) -> dict:
    created = runtime.start_standard_diagnosis(_scope(), "ORDER-1", "为什么跳枪", None, **kwargs)
    deadline = time.monotonic() + 5
    current = created
    while current["status"] not in {"completed", "failed", "inconclusive"}:
        assert time.monotonic() < deadline, current
        time.sleep(0.01)
        current = store.get_standard_diagnosis(created["diagnosis_id"], _scope().scope_fingerprint)
    return current


# --- 1. operator 入口：由登记表点名的已发布 operations 版本服务 -----------------


def test_the_operator_entry_runs_under_the_named_published_operations_prompt(
    tmp_path: Path, monkeypatch
) -> None:
    """换一个只在该版本提示词里出现的标记，诊断就拿它去跑。"""
    runtime, store, recorded = _runtime(tmp_path, monkeypatch)
    try:
        store_agents = AgentStore(store.path)
        _publish_agent(store_agents, name="运维甲", agent_type="operations", prompt="旧配方")
        _publish_agent(store_agents, name="运维乙", agent_type="operations", prompt=METHODOLOGY_MARKER)
        _bind_operator(store.path, "运维乙")

        job = _run(runtime, store, business_entry="operator")
    finally:
        runtime.shutdown()

    assert job["status"] == "completed", job
    assert recorded and recorded[0]["methodology"] == METHODOLOGY_MARKER


def test_the_named_agent_is_used_even_when_it_is_not_the_newest(tmp_path: Path, monkeypatch) -> None:
    """登记表是查找不是搜索：点名哪一版就是哪一版。"""
    runtime, store, recorded = _runtime(tmp_path, monkeypatch)
    try:
        agents = AgentStore(store.path)
        _publish_agent(agents, name="运维甲", agent_type="operations", prompt=METHODOLOGY_MARKER)
        _publish_agent(agents, name="运维乙", agent_type="operations", prompt="更新的配方")
        _bind_operator(store.path, "运维甲")

        job = _run(runtime, store, business_entry="operator")
    finally:
        runtime.shutdown()

    assert job["status"] == "completed", job
    assert recorded[0]["methodology"] == METHODOLOGY_MARKER


# --- 2. consumer 入口与未采用登记表的环境：零回归 -------------------------------


def test_the_consumer_entry_injects_nothing(tmp_path: Path, monkeypatch) -> None:
    """客户端同一条诊断链逐字不变：方法论为空。"""
    runtime, store, recorded = _runtime(tmp_path, monkeypatch)
    try:
        _publish_operations(store.path, name="运维乙")
        _bind_operator(store.path, "运维乙")

        job = _run(runtime, store, business_entry="consumer")
    finally:
        runtime.shutdown()

    assert job["status"] == "completed", job
    assert recorded[0]["methodology"] == ""


def test_an_unadopted_registry_keeps_the_pre_695_behavior(tmp_path: Path, monkeypatch) -> None:
    """没有登记表行的环境：即使点了 operator 也不注入，旧行为不变。"""
    runtime, store, recorded = _runtime(tmp_path, monkeypatch)
    try:
        _publish_operations(store.path, name="运维乙")

        job = _run(runtime, store, business_entry="operator")
    finally:
        runtime.shutdown()

    assert job["status"] == "completed", job
    assert recorded[0]["methodology"] == ""


def test_no_entry_at_all_injects_nothing(tmp_path: Path, monkeypatch) -> None:
    """没带入口的调用方（CLI 形状）不读登记表。"""
    runtime, store, recorded = _runtime(tmp_path, monkeypatch)
    try:
        _publish_operations(store.path, name="运维乙")
        _bind_operator(store.path, "运维乙")

        job = _run(runtime, store)
    finally:
        runtime.shutdown()

    assert job["status"] == "completed", job
    assert recorded[0]["methodology"] == ""


# --- 3. fail-closed：四种「这一行服务不了」同形 ---------------------------------


@pytest.mark.parametrize(
    ("name", "published", "bound"),
    [
        # The registry is adopted (a row exists for another pair) and this
        # tenant's operator entry is not in it.
        ("未登记租户的入口", "operations", "<other-tenant>"),
        # A row names an agent nobody publishes.
        ("点名了不存在的名字", "operations", "另一个名字"),
        # A row names an agent of the wrong type: the entry fork both ways.
        ("点名了 customer 版本", "customer", "运维乙"),
        # A row names a name that exists only as a draft.
        ("点名的版本没有发布", "draft", "运维乙"),
    ],
)
def test_the_operator_entry_fails_closed_without_a_serviceable_operations_agent(
    tmp_path: Path, monkeypatch, name: str, published: str, bound: str
) -> None:
    """四种情况都不服务，且**不**回退烤死提示词。

    四种成因的处置完全相同（同一个终态码、都没有触到模型），所以它们在这里是同一条
    判据的四个输入，而不是四条各自实现的规则。
    """
    runtime, store, recorded = _runtime(tmp_path, monkeypatch)
    agents = AgentStore(store.path)
    try:
        if published == "customer":
            _publish_agent(agents, name="运维乙", agent_type="customer", prompt="客服提示词")
        elif published == "draft":
            AgentManager(agents, knowledge_resolver=_PublishOkResolver()).create(
                _admin_context(),
                name="运维乙",
                description="",
                config=AgentConfig(
                    agent_type="operations",
                    prompt=METHODOLOGY_MARKER,
                    knowledge_base_ids=(),
                    model="aiops-api",
                    output_contract="diagnosis-v1",
                ),
            )
        else:
            _publish_agent(agents, name="运维乙", agent_type="operations", prompt=METHODOLOGY_MARKER)
        if bound == "<other-tenant>":
            # Adopt the registry on someone else's pair, so this tenant's
            # operator entry is *unmapped* rather than *unadopted* — the two
            # read the same way in a response and are opposite decisions.
            _bind_operator(store.path, "运维乙", tenant_id="T-OTHER")
        else:
            _bind_operator(store.path, bound)

        job = _run(runtime, store, business_entry="operator")
    finally:
        runtime.shutdown()

    assert job["status"] == "failed", (name, job)
    assert job["error_code"] == "DIAGNOSIS_AGENT_UNREGISTERED", (name, job)
    assert recorded == [], "fail-closed 的运行不得触到模型"
