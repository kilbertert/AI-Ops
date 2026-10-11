"""已发布 ``operations`` 版本的选择（#695，PRD #693 的 ② 号路径）。

断言的只有**选择语义**：谁会被选中、谁不会、查不到时返回什么。提示词注入本身由
``test_operator_diagnosis_entry.py`` 从 HTTP 面取证；这里测的是那个注入的**来源**。

四条判据各自对应一种会静默放行的退化：

* 草稿 / 停用 / 未发布 —— 选了就等于把一个没人发布的版本拿去服务真实订单；
* 名字不符 —— 注册表点名了谁就该是谁，扫描语义会把「配置错了」变成「凑合服务」；
* 类型不符 —— consumer 行选不出 operations，operator 行选不出 customer；
* 空提示词 —— 注入了空字符串，于是「已发布版本的提示词被注入」被「什么都没注入」满足。
"""

from __future__ import annotations

from pathlib import Path

from aiops_diagnostics.agent_lifecycle import AgentConfig, AgentManager, AgentStore
from aiops_diagnostics.ops_agents import select_operations_agent
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord


class _PublishOkResolver:
    def validate(self, tenant_id: str, knowledge_base_ids: tuple[str, ...]) -> None:
        del tenant_id, knowledge_base_ids


def _admin_context():
    subject = SubjectRecord(b_user_id="B-1", tenant_id="tenant-a")
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id="tenant-a",
        data_scope=DataScope(type="self"),
        roles=frozenset({"ROLE_AGENT_ADMIN"}),
        permissions=frozenset({"aiops:agents:manage"}),
    )


def _operations_config(prompt: str = "先看订单快照，再核对设备上报。") -> AgentConfig:
    return AgentConfig(
        agent_type="operations",
        prompt=prompt,
        knowledge_base_ids=(),
        model="aiops-api",
        output_contract="diagnosis-v1",
    )


def _customer_config() -> AgentConfig:
    return AgentConfig(
        agent_type="customer",
        prompt="回答必须引用已授权的业务资料。",
        knowledge_base_ids=("kb-a",),
        model="aiops-api",
        output_contract="blocks-v1",
    )


def _published_operations(tmp_path: Path, *, name: str = "运维", prompt: str | None = None):
    store = AgentStore(tmp_path / "gateway.db")
    manager = AgentManager(store, knowledge_resolver=_PublishOkResolver())
    admin = _admin_context()
    config = _operations_config() if prompt is None else _operations_config(prompt)
    agent = manager.create(admin, name=name, description="", config=config)
    manager.publish(admin, agent.agent_id, expected_revision=agent.revision)
    return store, manager, admin, agent


def test_a_draft_operations_agent_is_not_selected(tmp_path: Path) -> None:
    store = AgentStore(tmp_path / "gateway.db")
    manager = AgentManager(store, knowledge_resolver=_PublishOkResolver())
    admin = _admin_context()
    manager.create(admin, name="运维", description="", config=_operations_config())

    assert select_operations_agent(store, "tenant-a", "运维") is None


def test_the_published_named_operations_agent_is_selected(tmp_path: Path) -> None:
    store, _, _, agent = _published_operations(tmp_path)

    selection = select_operations_agent(store, "tenant-a", "运维")

    assert selection is not None
    assert selection.agent_id == agent.agent_id
    assert selection.version_no == 1
    assert selection.prompt == "先看订单快照，再核对设备上报。"


def test_a_different_name_selects_nothing(tmp_path: Path) -> None:
    """名字是注册表的回答，不是搜索的起点：不符即不服务。"""
    store, _, _, _ = _published_operations(tmp_path, name="运维")

    assert select_operations_agent(store, "tenant-a", "另一个名字") is None


def test_no_name_selects_nothing(tmp_path: Path) -> None:
    """注册表没给名字时**没有**「最新已发布」兜底——那是本票要拆掉的东西。"""
    store, _, _, _ = _published_operations(tmp_path)

    assert select_operations_agent(store, "tenant-a", None) is None
    assert select_operations_agent(store, "tenant-a", "") is None


def test_a_customer_agent_never_serves_the_operations_path(tmp_path: Path) -> None:
    """入口分叉的反方向：operator 行不得选出 customer 智能体。"""
    store = AgentStore(tmp_path / "gateway.db")
    manager = AgentManager(store, knowledge_resolver=_PublishOkResolver())
    admin = _admin_context()
    agent = manager.create(admin, name="客服", description="", config=_customer_config())
    manager.publish(admin, agent.agent_id, expected_revision=agent.revision)

    assert select_operations_agent(store, "tenant-a", "客服") is None


def test_an_empty_prompt_is_not_a_selection(tmp_path: Path) -> None:
    """空提示词注入等于什么都没注入，因此不算「已发布版本被采用」。

    发布门本身要求提示词非空（``agent_lifecycle._validate_config``），所以这条防的
    是**快照漂移**：绕过发布门写进去的版本。这里就照漂移的形状直接改快照。
    """
    import json
    import sqlite3

    store, _, _, agent = _published_operations(tmp_path)
    connection = sqlite3.connect(store.path)
    try:
        row = connection.execute(
            "SELECT snapshot_json FROM agent_versions WHERE agent_id = ? AND version_no = 1",
            (agent.agent_id,),
        ).fetchone()
        snapshot = json.loads(row[0])
        snapshot["prompt"] = "   "
        connection.execute(
            "UPDATE agent_versions SET snapshot_json = ? WHERE agent_id = ? AND version_no = 1",
            (json.dumps(snapshot, ensure_ascii=False), agent.agent_id),
        )
        connection.commit()
    finally:
        connection.close()

    assert select_operations_agent(store, "tenant-a", "运维") is None


def test_a_disabled_agent_stops_being_selected(tmp_path: Path) -> None:
    store, manager, admin, agent = _published_operations(tmp_path)
    published = manager.get(admin, agent.agent_id)
    manager.disable(admin, agent.agent_id, expected_revision=published.revision)

    assert select_operations_agent(store, "tenant-a", "运维") is None


def test_another_tenant_sees_nothing(tmp_path: Path) -> None:
    store, _, _, _ = _published_operations(tmp_path)

    assert select_operations_agent(store, "tenant-b", "运维") is None
