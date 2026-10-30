"""启动时的纳管必须对**已在册**的应用是 no-op。

2026-10-30 41 的 CD 自检红在这里：`register_self_service` 在启动时把 `dify_app_registry`
里的每个受管应用再登记一次（actor=`managed-adoption`），而 `register` 当时把 `created_by`
算进了"是不是同一条登记"的比较 —— 于是**每一次启动都抛 `SETUP_APP_CONFLICT`**，
主机自动回滚到上一版。

这条用例不测"纳管能成功"，它测的是**第二次登记是空操作**：登记的身份是
`(应用, 租户, 工作区, agent 名, profile, 端, 场景)`，不是"谁先登记的"。
"""

from types import SimpleNamespace

import pytest

from aiops_diagnostics.dify.self_service_store import SelfServiceStore, SetupConflict

APP = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def store(tmp_path):
    return SelfServiceStore(tmp_path / "db")


def register(db, **overrides):
    body = {
        "app_id": APP,
        "tenant_id": "TENANT-A",
        "workspace_id": "workspace-a",
        "agent_name": "operator-" + APP,
        "profile_id": "default",
        "actor": "creator",
        "entry": "operator",
        "environment": "env-a",
    }
    body.update(overrides)
    return db.register(**body)


def test_registering_an_existing_app_with_another_actor_is_a_no_op(tmp_path):
    db = store(tmp_path)
    register(db)
    # 启动时的纳管用的就是另一个 actor
    register(db, actor="managed-adoption")
    row = db.app(APP) or {}
    assert row.get("created_by") == "creator"  # 第一个登记者的记录不被改写
    assert row.get("entry") == "operator" and row.get("environment") == "env-a"


def test_registering_the_same_app_for_another_tenant_is_still_refused(tmp_path):
    db = store(tmp_path)
    register(db)
    with pytest.raises(SetupConflict, match="SETUP_APP_CONFLICT"):
        register(db, tenant_id="TENANT-B", actor="managed-adoption")


def test_registering_the_same_app_for_another_entry_is_still_refused(tmp_path):
    db = store(tmp_path)
    register(db)
    with pytest.raises(SetupConflict, match="SETUP_APP_CONFLICT"):
        register(db, entry="consumer", profile_id="", actor="managed-adoption")


def test_targets_are_updated_only_for_the_same_registration(tmp_path):
    db = store(tmp_path)
    db.register(
        app_id=APP,
        tenant_id="TENANT-A",
        workspace_id="workspace-a",
        agent_name="consumer-" + APP,
        profile_id="",
        actor="creator",
        entry="consumer",
        environment="env-a",
        targets={"knowledge_base_ids": ["kb-1"]},
    )
    db.register(
        app_id=APP,
        tenant_id="TENANT-A",
        workspace_id="workspace-a",
        agent_name="consumer-" + APP,
        profile_id="",
        actor="managed-adoption",
        entry="consumer",
        environment="env-a",
        targets={"knowledge_base_ids": ["kb-2"]},
    )
    row = db.app(APP) or {}
    assert '"kb-2"' in str(row.get("targets_json"))
    assert row.get("created_by") == "creator"


def test_startup_adoption_keeps_the_entry_environment_and_targets(tmp_path, monkeypatch):
    """启动纳管一条**客户端**登记时，端/场景/环境/目标必须一起落库。

    只写应用与 agent 名会让这条登记落成 `environment=''`、`targets_json='{}'`：
    目录里有它，而按 `(租户, 环境)` 找客户端运行目标时找不到 —— 表现为"现役客户端应用
    在册，但发布时说它不受管"。管家行不按环境查目标，所以这个缺口只在客户端出现。
    """
    from aiops_diagnostics.consumer.consumer_publication import managed_target
    from aiops_diagnostics.dify import self_service_api
    from aiops_diagnostics.dify.dify_app_registry import DifyAppBinding
    from aiops_diagnostics.dify.self_service import SelfService
    from aiops_diagnostics.dify.self_service_store import SelfServiceStore

    app = "bbbbbbbb-1111-2222-3333-444444444444"
    db = SelfServiceStore(tmp_path / "db")
    context = SimpleNamespace(
        settings=SimpleNamespace(
            database_file=tmp_path / "db",
            operator_profiles_file="profiles.json",
            execution_environment="env-a",
            environment="verify",
            dify_console_base_url="http://dify.invalid",
            dify_console_api_key="console",
        ),
        runtime=SimpleNamespace(
            dify_app_registry=SimpleNamespace(
                all=lambda: (DifyAppBinding("TENANT-A", "consumer", app, "consumer-" + app),)
            )
        ),
    )

    class Service:
        def __init__(self, _context):
            self.store = db

        def managed(self, _app_id):
            return {
                "app_id": app,
                "tenant_id": "TENANT-A",
                "workspace_id": "workspace-a",
                "agent_name": "consumer-" + app,
                "profile_id": "",
                "entry": "consumer",
                "scenario": "consumer_service",
                "environment": "env-a",
                "targets_json": '{"knowledge_base_ids": ["kb-own"], "live_order_status": true}',
            }

    class App:
        """够用的 FastAPI 替身：`register` 只用到三个装饰器与 `exception_handler`。"""

        def _decorator(self, *_args, **_kwargs):
            return lambda fn: fn

        get = post = _decorator
        exception_handler = _decorator

    monkeypatch.setattr(self_service_api, "SelfService", Service)
    self_service_api.register(App(), context)
    row = db.app(app) or {}
    assert row.get("entry") == "consumer" and row.get("environment") == "env-a"
    assert '"kb-own"' in str(row.get("targets_json"))
    # 受管目标按 (租户, 环境) 查得到 —— 这正是纳管之后要成立的那条事实
    target = managed_target(
        SimpleNamespace(database_file=tmp_path / "db", execution_environment="env-a"), "TENANT-A"
    )
    assert target and target["app_id"] == app and target["knowledge_base_ids"] == ["kb-own"]
    assert DifyAppBinding("TENANT-A", "consumer", app, "consumer-" + app).business_entry == "consumer"
    assert SelfService is not None
