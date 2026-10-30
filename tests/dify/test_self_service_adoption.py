"""启动时的纳管必须对**已在册**的应用是 no-op。

2026-10-30 41 的 CD 自检红在这里：`register_self_service` 在启动时把 `dify_app_registry`
里的每个受管应用再登记一次（actor=`managed-adoption`），而 `register` 当时把 `created_by`
算进了"是不是同一条登记"的比较 —— 于是**每一次启动都抛 `SETUP_APP_CONFLICT`**，
主机自动回滚到上一版。

这条用例不测"纳管能成功"，它测的是**第二次登记是空操作**：登记的身份是
`(应用, 租户, 工作区, agent 名, profile, 端, 场景)`，不是"谁先登记的"。
"""

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
