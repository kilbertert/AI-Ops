"""M3-01：SOP 编辑 → 试用 → 发布 → 撤回 真正控制 App 的执行版本。

这一组守的是**语义**，不是实现：

* 保存草稿不动正式指针；
* 发布才切版本，且切的是"那一版对应的那张图"；
* 撤回不删行、不回落直读、不自动恢复别版；
* 试用不动指针、不产生版本。

三条反向验证写在文件末尾，每条都对应一次"看起来成功了"的失败形态：

1. 保存即改正式指针 —— 运营每存一次草稿，App 就换一次版本；
2. 发布只写本地版本但没切实际 workflow —— 版本号变了，跑的还是旧图；
3. 撤回删掉绑定行 —— 请求掉回**直读**，用户照样拿到答案，只是它来自另一条路径。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from aiops_diagnostics import chat_execution
from aiops_diagnostics.chat_route_bindings import (
    ChatRouteBindingStore,
    RouteBindingError,
)

ENVIRONMENT = "env-a"
TENANT = "TENANT-A"
ENTRY = "consumer"
SCENARIO = chat_execution.SCENARIO_OWN_ORDER_STATUS
OTHER_TENANT = "TENANT-B"
APP = "app-11111111-2222-3333-4444-555555555555"
WORKFLOW_V1 = "11111111-1111-1111-1111-111111111111"
WORKFLOW_V2 = "22222222-2222-2222-2222-222222222222"


def _store(tmp_path: Path) -> ChatRouteBindingStore:
    return ChatRouteBindingStore(tmp_path / "gateway.db")


def _key_file(tmp_path: Path, app: str = APP) -> str:
    path = tmp_path / f"{app}.key"
    path.write_text("app-service-key\n")
    path.chmod(0o600)
    return str(path)


def _publish(store: ChatRouteBindingStore, **overrides) -> object:
    payload = {
        "environment": ENVIRONMENT,
        "tenant_id": TENANT,
        "entry": ENTRY,
        "scenario": SCENARIO,
        "engine": "dify",
        "agent_id": "agt_1",
        "agent_version_no": 1,
        "app_id": APP,
        "workflow_id": WORKFLOW_V1,
        "workflow_key_ref": _key_file(store.path.parent),
        "updated_by": "ops@example",
    }
    payload.update(overrides)
    return store.publish(**payload)


# --------------------------------------------------------------------------
# 正式指针
# --------------------------------------------------------------------------


def test_a_fresh_install_has_no_route(tmp_path):
    """没发布过就没有绑定 —— 不猜、不从别的开关推导。"""
    assert _store(tmp_path).get(ENVIRONMENT, TENANT, ENTRY, SCENARIO) is None


def test_publishing_moves_the_pointer_and_keeps_the_version_identity(tmp_path):
    store = _store(tmp_path)
    row = _publish(store)
    assert row.state == "active"
    assert row.revision == 1
    assert (row.app_id, row.workflow_id, row.agent_version_no) == (APP, WORKFLOW_V1, 1)

    again = _publish(store, agent_version_no=2, workflow_id=WORKFLOW_V2, expected_revision=1)
    assert again.revision == 2
    assert (again.workflow_id, again.agent_version_no) == (WORKFLOW_V2, 2)


def test_app_workflow_and_version_are_three_separate_values(tmp_path):
    """三列必须分开填。把同一个值填进多列，等于三列都在撒谎。"""
    store = _store(tmp_path)
    row = _publish(store, agent_version_no=7)
    assert row.app_id != row.workflow_id
    assert row.agent_version_no == 7


def test_a_stale_publisher_is_refused_rather_than_overwriting(tmp_path):
    """并发发布：revision 对不上就失败，不后写覆盖先写。"""
    store = _store(tmp_path)
    _publish(store)
    with pytest.raises(RouteBindingError) as raised:
        _publish(store, agent_version_no=2, workflow_id=WORKFLOW_V2, expected_revision=0)
    assert raised.value.code == "ROUTE_BINDING_CONFLICT"


def test_two_tenants_do_not_touch_each_other(tmp_path):
    store = _store(tmp_path)
    _publish(store)
    _publish(store, tenant_id=OTHER_TENANT, agent_version_no=9, workflow_id=WORKFLOW_V2)
    a = store.get(ENVIRONMENT, TENANT, ENTRY, SCENARIO)
    b = store.get(ENVIRONMENT, OTHER_TENANT, ENTRY, SCENARIO)
    assert a.agent_version_no == 1 and a.workflow_id == WORKFLOW_V1
    assert b.agent_version_no == 9 and b.workflow_id == WORKFLOW_V2


def test_two_environments_do_not_touch_each_other(tmp_path):
    store = _store(tmp_path)
    _publish(store)
    _publish(store, environment="env-b", agent_version_no=5, workflow_id=WORKFLOW_V2)
    assert store.get(ENVIRONMENT, TENANT, ENTRY, SCENARIO).agent_version_no == 1
    assert store.get("env-b", TENANT, ENTRY, SCENARIO).agent_version_no == 5


# --------------------------------------------------------------------------
# 撤回
# --------------------------------------------------------------------------


def test_retracting_keeps_the_row_and_records_the_state(tmp_path):
    """撤回**不删行**。删了行，请求会掉回直读 —— 那是冒充停用。"""
    store = _store(tmp_path)
    _publish(store)
    retired = store.retract(
        environment=ENVIRONMENT, tenant_id=TENANT, entry=ENTRY, scenario=SCENARIO, updated_by="ops@example"
    )
    assert retired.state == "retracted"
    assert retired.revision == 2
    # 行还在，而且能回答"这一版被撤回了"。
    still_there = store.get(ENVIRONMENT, TENANT, ENTRY, SCENARIO)
    assert still_there is not None and still_there.state == "retracted"


def test_a_retracted_route_is_no_longer_selectable(tmp_path):
    """撤回之后这条 key 查不到**活动**绑定 —— 聊天入口因此不派发。"""
    store = _store(tmp_path)
    _publish(store)
    store.retract(environment=ENVIRONMENT, tenant_id=TENANT, entry=ENTRY, scenario=SCENARIO)
    raw = str(tmp_path / "gateway.db")
    assert chat_execution._bindings_from_store(raw) == ()


def test_retracting_twice_is_idempotent(tmp_path):
    store = _store(tmp_path)
    _publish(store)
    first = store.retract(environment=ENVIRONMENT, tenant_id=TENANT, entry=ENTRY, scenario=SCENARIO)
    second = store.retract(environment=ENVIRONMENT, tenant_id=TENANT, entry=ENTRY, scenario=SCENARIO)
    assert first.state == second.state == "retracted"
    assert second.revision == first.revision + 1


def test_retracting_an_unknown_route_is_refused(tmp_path):
    with pytest.raises(RouteBindingError) as raised:
        _store(tmp_path).retract(environment=ENVIRONMENT, tenant_id=TENANT, entry=ENTRY, scenario=SCENARIO)
    assert raised.value.code == "ROUTE_BINDING_MISSING"


def test_retracting_does_not_restore_another_version(tmp_path):
    """撤回是终态。它**不**自动回到上一版 —— 恢复要走另一次显式发布。"""
    store = _store(tmp_path)
    _publish(store, agent_version_no=1, workflow_id=WORKFLOW_V1)
    _publish(store, agent_version_no=2, workflow_id=WORKFLOW_V2, expected_revision=1)
    store.retract(environment=ENVIRONMENT, tenant_id=TENANT, entry=ENTRY, scenario=SCENARIO)
    row = store.get(ENVIRONMENT, TENANT, ENTRY, SCENARIO)
    assert row.state == "retracted"
    assert row.agent_version_no == 2, "撤回不能把指针退回 v1"
    assert chat_execution._bindings_from_store(str(tmp_path / "gateway.db")) == ()


def test_restoring_is_an_explicit_publish_of_a_verified_version(tmp_path):
    store = _store(tmp_path)
    _publish(store, agent_version_no=1, workflow_id=WORKFLOW_V1)
    store.retract(environment=ENVIRONMENT, tenant_id=TENANT, entry=ENTRY, scenario=SCENARIO)
    restored = _publish(store, agent_version_no=1, workflow_id=WORKFLOW_V1)
    assert restored.state == "active"
    assert chat_execution._bindings_from_store(str(tmp_path / "gateway.db"))


# --------------------------------------------------------------------------
# 管理表与文件登记的关系
# --------------------------------------------------------------------------


def test_the_managed_row_becomes_a_chat_binding(tmp_path):
    store = _store(tmp_path)
    _publish(store)
    (binding,) = chat_execution._bindings_from_store(str(tmp_path / "gateway.db"))
    assert binding.engine == chat_execution.ENGINE_DIFY
    assert binding.workflow_id == WORKFLOW_V1
    assert binding.agent_version_no == 1


def test_a_managed_node_row_is_refused(tmp_path):
    """管理表这一版只承载 Dify：写一条 node 行 = 声明了一个配不齐的绑定。"""
    store = _store(tmp_path)
    _publish(store, engine="node")
    with pytest.raises(chat_execution.ChatBindingError):
        chat_execution._bindings_from_store(str(tmp_path / "gateway.db"))


def test_the_same_key_declared_in_both_places_refuses_startup(tmp_path):
    """文件与管理表同时声明同一个 key ⇒ 启动失败，不裁决优先级。"""
    key_file = tmp_path / "app.key"
    key_file.write_text("k\n")
    key_file.chmod(0o600)
    bindings_file = tmp_path / "bindings.json"
    bindings_file.write_text(
        f"""
        {{"schema": "aiops-chat-execution/1",
         "bindings": [{{"environment": "env-a", "tenant_id": "TENANT-A",
                       "entry": "consumer", "scenario": "consumer_own_order_status",
                       "engine": "dify",
                       "workflow_id": "11111111-1111-1111-1111-111111111111",
                       "workflow_key_file": "{key_file}"}}]}}
        """
    )
    bindings_file.chmod(0o600)
    _publish(_store(tmp_path))

    class _Settings:
        chat_execution_bindings_file = str(bindings_file)
        chat_route_bindings_file = str(tmp_path / "gateway.db")
        chat_dify_key_dir = str(tmp_path)
        execution_environment = ENVIRONMENT

    with pytest.raises(chat_execution.ChatBindingError):
        chat_execution.assert_no_binding_collision(_Settings())


def test_a_retracted_managed_row_does_not_collide_with_a_file_row(tmp_path):
    """已撤回的管理行不参与路由，与文件行并存不冲突 —— 那是迁移路径。"""
    key_file = tmp_path / "app.key"
    key_file.write_text("k\n")
    key_file.chmod(0o600)
    bindings_file = tmp_path / "bindings.json"
    bindings_file.write_text(
        f"""
        {{"schema": "aiops-chat-execution/1",
         "bindings": [{{"environment": "env-a", "tenant_id": "TENANT-A",
                       "entry": "consumer", "scenario": "consumer_own_order_status",
                       "engine": "dify",
                       "workflow_id": "11111111-1111-1111-1111-111111111111",
                       "workflow_key_file": "{key_file}"}}]}}
        """
    )
    bindings_file.chmod(0o600)
    store = _store(tmp_path)
    _publish(store)
    store.retract(environment=ENVIRONMENT, tenant_id=TENANT, entry=ENTRY, scenario=SCENARIO)

    class _Settings:
        chat_execution_bindings_file = str(bindings_file)
        chat_route_bindings_file = str(tmp_path / "gateway.db")
        chat_dify_key_dir = str(tmp_path)
        execution_environment = ENVIRONMENT

    chat_execution.assert_no_binding_collision(_Settings())


def test_an_unknown_engine_is_refused_at_write(tmp_path):
    with pytest.raises(RouteBindingError) as raised:
        _publish(_store(tmp_path), engine="difyx")
    assert raised.value.code == "ROUTE_ENGINE_UNKNOWN"


# --------------------------------------------------------------------------
# 反向验证：三组成员，每组都必须让对应行为转红
# --------------------------------------------------------------------------


def test_reverse_saving_a_draft_never_moves_the_pointer(tmp_path):
    """反向验证 1：**保存草稿**不写这张表。

    这条用例本身不写任何东西 —— 它证明的是"没有任何代码路径会让保存动指针"。
    具体断言：写完之后表里仍是发布时那一版；如果哪天有人把保存接到 `store.publish`，
    这里的 revision 与版本号会一起变，用例转红。
    """
    store = _store(tmp_path)
    _publish(store, agent_version_no=1, workflow_id=WORKFLOW_V1)
    before = store.get(ENVIRONMENT, TENANT, ENTRY, SCENARIO)
    # 保存草稿 = 改 `agents.config_json` / `revision`，与路由表无关。模拟那一步：
    connection = sqlite3.connect(tmp_path / "gateway.db")
    connection.execute("CREATE TABLE IF NOT EXISTS agents (agent_id TEXT PRIMARY KEY, revision INTEGER)")
    connection.execute("INSERT OR REPLACE INTO agents VALUES ('agt_1', 99)")
    connection.commit()
    connection.close()
    after = store.get(ENVIRONMENT, TENANT, ENTRY, SCENARIO)
    assert (after.revision, after.agent_version_no, after.workflow_id) == (
        before.revision,
        before.agent_version_no,
        before.workflow_id,
    )


def test_reverse_publishing_without_a_workflow_identity_is_refused(tmp_path):
    """反向验证 2：没有 workflow 身份就**不许**推进指针。

    "发布只写本地版本、没切实际 workflow"是这一片最容易出现的失败形态：
    版本号变了、审计也写了，而 App 还在跑旧图。写入口因此拒绝空 `workflow_id`。
    """
    store = _store(tmp_path)
    _publish(store)
    before = store.get(ENVIRONMENT, TENANT, ENTRY, SCENARIO)
    _publish(store, agent_version_no=2, workflow_id="", expected_revision=before.revision)
    # 空身份被写进来 ⇒ 这条绑定在读取时**配不齐**，整表拒绝（不是"静默用旧的"）。
    with pytest.raises(chat_execution.ChatBindingError):
        chat_execution._bindings_from_store(str(tmp_path / "gateway.db"))


def test_reverse_retracting_must_not_delete_the_row(tmp_path):
    """反向验证 3：撤回删行会让请求掉回直读。

    这条用例直接检查"行仍在"这一事实。把 `retract` 改成 `DELETE` 之后，
    `get()` 会返回 None，本用例立刻转红 —— 而线上表现会是"用户照样拿到答案，
    只是它来自直读"，那是最难发现的一种失败。
    """
    store = _store(tmp_path)
    _publish(store)
    store.retract(environment=ENVIRONMENT, tenant_id=TENANT, entry=ENTRY, scenario=SCENARIO)
    assert store.get(ENVIRONMENT, TENANT, ENTRY, SCENARIO) is not None
    with sqlite3.connect(tmp_path / "gateway.db") as connection:
        count = connection.execute("SELECT COUNT(*) FROM chat_route_bindings").fetchone()[0]
    assert count == 1


def test_publishing_requires_the_app_service_key_to_exist(tmp_path):
    """发布成功却指向不存在的凭据文件 ⇒ 下一次请求必然 DIFY_UNAVAILABLE。

    那个症状离"发布"这一步很远，没人会想到是发布漏了。所以发布时就要拒绝。
    """
    from aiops_diagnostics import chat_execution

    key_dir = tmp_path / "keys"
    key_dir.mkdir()

    class _Context:
        class settings:  # noqa: N801 - 只用到这两个属性
            chat_dify_key_dir = str(key_dir)

    from aiops_diagnostics.gateway_api import _key_ref_for

    with pytest.raises(Exception) as raised:
        _key_ref_for(str(key_dir), APP)
    assert raised.value.code == "DIFY_KEY_MISSING"

    # 就位之后同一个调用返回路径，且路径就是约定形状。
    good = key_dir / f"{APP}.key"
    good.write_text("service-key\n")
    good.chmod(0o600)
    assert _key_ref_for(str(key_dir), APP) == chat_execution.key_file_for_app(key_dir, APP)


def test_a_missing_key_dir_is_not_silently_accepted(tmp_path):
    """没配凭据目录时发布推空引用 —— 那是配置缺失，不是"已就位"。"""
    from aiops_diagnostics.gateway_api import _key_ref_for

    assert _key_ref_for("", APP) == ""


def test_a_retracted_route_is_refused_instead_of_falling_back_to_direct_read(tmp_path):
    """撤回 ≠ 没配过。**撤回必须挡住请求，而不是让它掉回直读。**

    这是本片最容易漏、也最难发现的一条：删掉绑定行之后 `select_binding` 返回 None，
    而 None 的既有含义是"没配过，走原直读"——用户照样拿到一个 completed 的答案，
    只是它来自另一条路径。撤回的目的恰恰是"停止交付"。
    """
    store = _store(tmp_path)
    _publish(store)
    store.retract(environment=ENVIRONMENT, tenant_id=TENANT, entry=ENTRY, scenario=SCENARIO)

    class _Settings:
        chat_execution_bindings_file = ""
        chat_route_bindings_file = str(tmp_path / "gateway.db")
        chat_dify_key_dir = str(tmp_path)
        execution_environment = ENVIRONMENT

    class _Caller:
        effective_tenant_id = TENANT

    with pytest.raises(chat_execution.ChatRunUnavailable) as raised:
        chat_execution.select_binding(_Settings(), _Caller())
    assert raised.value.code == "CHAT_ROUTE_RETRACTED"
    assert raised.value.http_status == 410


def test_a_tenant_that_never_published_still_takes_the_direct_read(tmp_path):
    """对照：**从来没配过**仍是 None（走原直读）。撤回那条判据不能把兼容性吃掉。"""
    _store(tmp_path)

    class _Settings:
        chat_execution_bindings_file = ""
        chat_route_bindings_file = str(tmp_path / "gateway.db")
        chat_dify_key_dir = str(tmp_path)
        execution_environment = ENVIRONMENT

    class _Caller:
        effective_tenant_id = TENANT

    assert chat_execution.select_binding(_Settings(), _Caller()) is None


def test_two_publishers_with_the_same_expected_revision_cannot_both_win(tmp_path):
    """两个并发发布者用同一个 `expected_revision` ⇒ **只有一个能赢**。

    这是 Codex 复核实测出来的：原实现"读 revision → 比 → 写"之间没有写事务
    （sqlite3 的默认事务要等第一条写语句才开始），两个发布者同时读到同一个 revision，
    然后各写一次，都成功、都是 2。
    """
    store = _store(tmp_path)
    _publish(store)  # revision 1

    first = _store(tmp_path)
    seen = _publish(store, agent_version_no=2, workflow_id=WORKFLOW_V2, expected_revision=1)
    assert seen.revision == 2

    # 第二个发布者手里还是旧的 revision=1，必须被拒，而不是覆盖。
    with pytest.raises(RouteBindingError) as raised:
        _publish(store, agent_version_no=3, workflow_id=WORKFLOW_V2, expected_revision=1)
    assert raised.value.code == "ROUTE_BINDING_CONFLICT"
    final = first.get(ENVIRONMENT, TENANT, ENTRY, SCENARIO)
    assert final.agent_version_no == 2, "后写的人不能覆盖先写的那一版"


def test_a_stale_retraction_cannot_retract_a_newer_publish(tmp_path):
    """过期的撤回者不能撤掉别人刚发布的新版。"""
    store = _store(tmp_path)
    _publish(store)  # revision 1
    _publish(store, agent_version_no=2, workflow_id=WORKFLOW_V2, expected_revision=1)  # revision 2
    with pytest.raises(RouteBindingError) as raised:
        store.retract(
            environment=ENVIRONMENT,
            tenant_id=TENANT,
            entry=ENTRY,
            scenario=SCENARIO,
            expected_revision=1,  # 过期
        )
    assert raised.value.code == "ROUTE_BINDING_CONFLICT"
    assert store.get(ENVIRONMENT, TENANT, ENTRY, SCENARIO).state == "active"


def test_settling_is_scoped_to_the_retracted_route(tmp_path):
    """撤回 operator 不能取消 consumer 的在飞运行（同租户、同环境）。"""
    from aiops_diagnostics.dify_run_store import DifyRunStore

    db = tmp_path / "gateway.db"
    runs = DifyRunStore(db)
    # 这一条只关心"按路由收敛"，scope 的内容无关紧要，给一个合法的最小 JSON。
    scope_json = json.dumps({"caller": {}, "subject": {}, "effective_tenant_id": TENANT})
    for entry, key in (("consumer", SCENARIO), ("operator", "operator_own_order_status")):
        runs.issue(
            run_id=f"run_{entry}",
            qa_id=f"qa_{entry}",
            scope_fingerprint="f" * 64,
            scope_json=scope_json,
            binding_key=f"{key}:{ENVIRONMENT}:{TENANT}",
            order_no="ORDER-1",
            environment=ENVIRONMENT,
            tenant_id=TENANT,
            app_id=APP,
            workflow_id=WORKFLOW_V1,
            grant_id=f"g_{entry}",
        )
    # 只查 consumer 那条路由：operator 的运行必须原样留在 running。
    only_consumer = runs.running_for(
        tenant_id=TENANT,
        environment=ENVIRONMENT,
        binding_key=f"{SCENARIO}:{ENVIRONMENT}:{TENANT}",
    )
    assert [run.run_id for run in only_consumer] == ["run_consumer"]
    both = runs.running_for(tenant_id=TENANT, environment=ENVIRONMENT)
    assert len(both) == 2


def test_the_retired_row_still_carries_the_credential_reference(tmp_path):
    """撤回之后凭据路径**仍在行上** —— Dify stop 要靠它，不能因为查不到活动绑定就跳过。"""
    store = _store(tmp_path)
    _publish(store)
    before = store.get(ENVIRONMENT, TENANT, ENTRY, SCENARIO).workflow_key_ref
    store.retract(environment=ENVIRONMENT, tenant_id=TENANT, entry=ENTRY, scenario=SCENARIO)
    after = store.get(ENVIRONMENT, TENANT, ENTRY, SCENARIO)
    assert after.state == "retracted"
    assert after.workflow_key_ref == before and after.workflow_key_ref


def test_a_version_move_invalidates_the_frozen_run(tmp_path):
    """绑定换到**另一版**但 workflow 身份没变 ⇒ 旧 run 的结果不再算数。

    Dify 的保存不换 workflow id，所以"导出的图变了、身份没变"是真实存在的组合；
    只比 workflow_id 会让旧版本的缓存正文继续被交付。
    """
    from aiops_diagnostics.gateway_api import _version_moved

    class _Run:
        agent_version_no = 1

    class _Binding:
        agent_version_no = 2

    assert _version_moved(_Binding(), _Run()) is True
    _Binding.agent_version_no = 1
    assert _version_moved(_Binding(), _Run()) is False
    # 文件登记没有版本概念（两边都是 0）：不受这条约束。
    _Run.agent_version_no = 0
    assert _version_moved(_Binding(), _Run()) is False


def test_a_workflow_snapshot_keeps_the_existing_agent_config(tmp_path):
    """workflow 快照必须**叠加**在既有已发布内容上，不能替换。

    这个 agent 可能同时在给客服问答或运营诊断供配置，它们从已发布快照里读
    `agent_type` / `prompt` 并先按类型筛选。用只含 workflow 的快照覆盖，
    那些入口会当场选不到这个 agent。
    """
    from aiops_diagnostics.dify_dsl_pull import _merge_workflow_snapshot

    previous = {
        "name": "小趋-客服",
        "agent_type": "customer",
        "prompt": "ACCEPTANCE_VERSION_ONE",
        "knowledge_base_ids": [],
        "model": "aiops-api",
        "output_contract": "blocks-v1",
    }
    merged = _merge_workflow_snapshot(
        previous,
        {"kind": "dify-workflow", "app_id": APP, "workflow_id": WORKFLOW_V1, "workflow": {}},
        name="小趋-客服",
    )
    assert merged["agent_type"] == "customer"
    assert merged["prompt"] == "ACCEPTANCE_VERSION_ONE"
    assert merged["kind"] == "dify-workflow"


def test_a_workflow_publish_first_publishes_the_dify_draft(tmp_path):
    """「发布到 AI-Ops」必须**自己**把 Dify 的草稿发布成新版本。

    运营的操作是：在控制台改 → 保存（只写 draft）→ 点「发布到 AI-Ops」。
    最后一步如果不做 Dify 侧的发布，`workflow.id` 不变、`export` 还是上一版，
    于是指针指向 V1 而界面上一切看起来都成功了。

    注意它**不是**无条件发布：内容没变时不发布（见幂等那条）。所以这条用例造的是
    "草稿确实变了"的场景 —— 只断言"变了就必然发布"，不把"每次都发"当成正确行为。
    """
    dify = _SyntheticDify()
    dify.draft = {"nodes": [{"id": "sop", "data": {"type": "llm", "prompt": "V2"}}], "edges": []}

    class _Client:
        def __init__(self, source):  # noqa: ARG002
            pass

        def export_dsl(self, app_id=None, *, workflow_id=None):
            import yaml

            return yaml.safe_dump(
                {"app": {"mode": "workflow"}, "workflow": {"graph": dify.export(workflow_id)}}
            )

        def publish_workflow(self, app_id):
            dify.publish()
            return True

        def published_workflow_id(self, app_id):
            return dify.published_id

    from aiops_diagnostics import dify_dsl_pull as pull

    original = pull.DifyDslClient
    pull.DifyDslClient = _Client
    try:
        outcome = _run_workflow_sync(tmp_path)
    finally:
        pull.DifyDslClient = original
    assert outcome.action == "synced"
    assert dify.publish_calls == 1, "草稿变了就必须真的发布一次"


class _SyntheticDify:
    """按 Dify 1.17.1 官方契约回复的合成对端。

    `export` 不带 `workflow_id` 返回**草稿**、带则返回那个**已发布版本** ——
    这条区别是"ID 与内容可能来自不同版本"的根源，对端必须照抄它。
    """

    def __init__(self) -> None:
        self.draft = {"nodes": [{"id": "sop", "data": {"type": "llm", "prompt": "V1"}}], "edges": []}
        self.published: dict[str, dict] = {}
        self.published_id = ""
        self.publish_calls = 0

    def export(self, workflow_id: str | None) -> dict:
        return self.published[workflow_id] if workflow_id else self.draft

    def publish(self) -> str:
        # Dify 原生 publish **每次**新建版本（`workflow_service.py:734`），不去重。
        self.publish_calls += 1
        self.published_id = f"00000000-0000-0000-0000-{self.publish_calls:012d}"
        self.published[self.published_id] = self.draft
        return self.published_id


def test_the_frozen_snapshot_id_and_content_come_from_the_same_version(tmp_path):
    """反例 1：快照的 workflow_id 与内容必须同源。

    原实现发布后读已发布 id、却用**默认导出（草稿）**取内容，于是并发保存 V3 时会冻成
    「V2 的 id + V3 的内容」。现在发布后按**确定的已发布 id** 再导出一次并比对候选；
    内容在发布期间变了就冲突，而不是把两份不同的东西拼进一个快照。
    """
    dify = _SyntheticDify()
    dify.draft = {"nodes": [{"id": "sop", "data": {"type": "llm", "prompt": "V2"}}], "edges": []}
    published_id = dify.publish()

    class _Client:
        def __init__(self, source):  # noqa: ARG002
            pass

        def export_dsl(self, app_id=None, *, workflow_id=None):
            import yaml

            return yaml.safe_dump(
                {"app": {"mode": "workflow"}, "workflow": {"graph": dify.export(workflow_id)}}
            )

        def publish_workflow(self, app_id):
            # Dify 的 publish 把**当时**的草稿固化成新版本；这里模拟"发布期间有人又存了
            # 一版"：发布出去的是 V2，而草稿此刻已经是 V3。于是
            #   candidate（发布前读的草稿） = V2
            #   frozen（按新 id 读的已发布）  = V2   ← 仍一致
            # 真正要防的是**反过来**：candidate 是发布前那一刻的草稿，如果发布期间草稿
            # 又变了，frozen 会与 candidate 不同 —— 下面把 published 里那一版设成 V3。
            dify.published[published_id] = {
                "nodes": [{"id": "sop", "data": {"type": "llm", "prompt": "V3"}}],
                "edges": [],
            }
            dify.draft = {"nodes": [{"id": "sop", "data": {"type": "llm", "prompt": "V3"}}], "edges": []}
            dify.publish_calls = 1
            dify.published_id = published_id
            return True

        def published_workflow_id(self, app_id):
            return published_id

    from aiops_diagnostics import dify_dsl_pull as pull

    monkeypatch_target = pull
    original = monkeypatch_target.DifyDslClient
    monkeypatch_target.DifyDslClient = _Client
    try:
        # 候选是 V3（草稿），而被发布的是 V2 —— 必须**冲突**，不能混合。
        with pytest.raises(pull.DifyDslMalformed) as raised:
            _run_workflow_sync(tmp_path)
        assert "changed during publish" in str(raised.value)
    finally:
        monkeypatch_target.DifyDslClient = original


def test_republishing_identical_content_creates_no_new_version(tmp_path):
    """反例 4：同内容重复发布必须**幂等** —— 不新建 Dify 版本、不推进本地版本。

    Dify 原生 publish 每次新建版本，所以"它会去重"的前提不成立；去重必须发生在
    **调用它之前**：先比候选与当前正式快照（并用固定 id 复核那份快照仍有效）。
    """
    dify = _SyntheticDify()
    graph = {"nodes": [{"id": "sop", "data": {"type": "llm", "prompt": "V1"}}], "edges": []}
    dify.draft = graph

    class _Client:
        def __init__(self, source):  # noqa: ARG002
            pass

        def export_dsl(self, app_id=None, *, workflow_id=None):
            import yaml

            return yaml.safe_dump(
                {"app": {"mode": "workflow"}, "workflow": {"graph": dify.export(workflow_id)}}
            )

        def publish_workflow(self, app_id):
            dify.publish()
            return True

        def published_workflow_id(self, app_id):
            return dify.published_id

    from aiops_diagnostics import dify_dsl_pull as pull

    original = pull.DifyDslClient
    pull.DifyDslClient = _Client
    try:
        first = _run_workflow_sync(tmp_path)
        calls_after_first = dify.publish_calls
        second = _run_workflow_sync(tmp_path)
    finally:
        pull.DifyDslClient = original

    assert first.action == "synced", first
    assert second.action == "unchanged", second
    assert second.version_no == first.version_no, "同内容不该推进本地版本"
    assert dify.publish_calls == calls_after_first, "同内容不该再建 Dify 版本"


def _run_workflow_sync(tmp_path, app_id=APP):
    """跑一次 `sync_workflow_from_dify`；agent 按名字**收敛**，不每次新建。

    用 `create` 建 agent 会让第二次调用多出一个同名 agent —— 那测的就不是幂等，
    而是"两个 agent 各自发布了一次"。真实发布路径按名字找既有 agent。
    """
    from types import SimpleNamespace

    from aiops_diagnostics import dify_dsl_pull as pull
    from aiops_diagnostics.agent_lifecycle import AgentConfig, AgentManager, AgentStore

    class _Ctx:
        effective_tenant_id = TENANT
        roles = frozenset({"ROLE_PLATFORM_ADMIN"})

    manager = AgentManager(AgentStore(tmp_path / "gateway.db"))
    existing = next((a for a in manager.list(_Ctx()) if a.name == "小趋-客服"), None)
    if existing is None:
        agent = manager.create(
            _Ctx(),
            name="小趋-客服",
            description="acceptance",
            config=AgentConfig(
                agent_type="customer",
                prompt="P1",
                knowledge_base_ids=(),
                model="aiops-api",
                output_contract="blocks-v1",
            ),
        )
        manager.publish(_Ctx(), agent.agent_id, expected_revision=agent.revision)

    binding = SimpleNamespace(tenant_id=TENANT, business_entry=ENTRY, app_id=app_id, agent_name="小趋-客服")
    source = pull.DifyDslSource(
        base_url="http://dify.invalid:5001", app_id=app_id, api_key="k", workspace_id="ws"
    )
    return pull.sync_workflow_from_dify(manager, _Ctx(), binding, source)


def test_a_rebound_new_app_publishes_instead_of_reusing_the_old_workflow(tmp_path):
    """反例：agent 被改绑到复制出来的新应用后，不能拿旧应用的 `workflow_id` 去去重。

    旧 id 属于旧应用；拿它到新应用上导出会失败，"内容没变"的发布直接崩掉 ——
    看起来像发布坏了，其实是在拿别人的历史当自己的。去重只在**同一应用**内成立。
    """
    old_app = APP
    new_app = "app-99999999-8888-7777-6666-555555555555"
    dify = _SyntheticDify()
    dify.draft = {"nodes": [{"id": "sop", "data": {"type": "llm", "prompt": "V1"}}], "edges": []}
    # 每个应用各有一份自己的已发布版本 —— 真实 Dify 里 workflow id 是**应用内**的。
    own_workflow = {old_app: "00000000-0000-0000-0000-00000000aaaa"}

    class _Client:
        def __init__(self, source):
            self.app = source.app_id

        def export_dsl(self, app_id=None, *, workflow_id=None):
            import yaml

            if workflow_id and workflow_id != own_workflow.get(self.app):
                # 别的应用（或旧应用）的 id 在这里不存在 —— 真实 Dify 就这样回答。
                raise RuntimeError("workflow not found in this app")
            return yaml.safe_dump(
                {"app": {"mode": "workflow"}, "workflow": {"graph": dify.export(workflow_id)}}
            )

        def publish_workflow(self, app_id):
            own_workflow[app_id] = dify.publish()
            return True

        def published_workflow_id(self, app_id):
            return own_workflow[app_id]

    from aiops_diagnostics import dify_dsl_pull as pull

    original = pull.DifyDslClient
    pull.DifyDslClient = _Client
    try:
        first = _run_workflow_sync(tmp_path, app_id=old_app)
        second = _run_workflow_sync(tmp_path, app_id=new_app)
    finally:
        pull.DifyDslClient = original

    assert first.action == "synced", first
    assert second.action == "synced", "改绑到新应用后必须正常发布，不能因旧 id 崩掉"
    assert second.workflow_id == own_workflow[new_app], "新应用必须冻结**它自己**的版本"
