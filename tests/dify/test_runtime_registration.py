"""D32：工作区/双端同事务登记；判据来自准备与准入分离和既有复合归属。"""

import sqlite3

import pytest

from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces
from aiops_diagnostics.dify.self_service_store import SelfServiceStore, SetupConflict
from aiops_diagnostics.gateway import gateway_db


@pytest.fixture
def candidate(tmp_path):
    store = SelfServiceStore(tmp_path / "gateway.db")
    registration = dict(
        environment="target",
        tenant_id="same",
        workspace_id="workspace-target",
        operator_app_id="operator-target",
        consumer_app_id="consumer-target",
        enabled=False,
    )
    consumer = dict(knowledge_base_ids=["kb-target"], live_order_status=True, live_billing_record=True)
    return store, registration, consumer


def register(candidate, verify=lambda: None):
    return candidate[0].register_runtime(candidate[1], candidate[2], verify=verify)


def rows(candidate):
    with gateway_db.connection(candidate[0].path, readonly=True) as db:
        return {
            t: [dict(r) for r in db.execute("SELECT * FROM " + t)]
            for t in (
                "dify_workspaces",
                "operator_apps",
                "chat_route_bindings",
                "agent_versions",
                "dify_app_registry",
            )
        }


def test_same_resources_register_once_and_preserve_original_formal_state(candidate):
    assert register(candidate) is True
    before = rows(candidate)
    assert register(candidate) is False and rows(candidate) == before
    workspace = DifyWorkspaces(candidate[0].path)
    assert workspace.resolve("same", "target") == "workspace-target"
    assert workspace.row("same", "target")["setup_state"] == "created"
    apps = candidate[0].apps()
    assert {a["entry"] for a in apps} == {"operator", "consumer"}
    assert {a["environment"] for a in apps} == {"target"}
    assert before["chat_route_bindings"] == before["agent_versions"] == before["dify_app_registry"] == []


@pytest.mark.parametrize("conflict", ["workspace-owner", "app-owner", "partial", "target-flags", "scenario"])
def test_existing_conflict_never_creates_or_overwrites_partial_registration(candidate, conflict):
    store, registration, _ = candidate
    if conflict == "workspace-owner":
        workspace = DifyWorkspaces(store.path)
        workspace.claim("same", "other", owner="fixture", lease_seconds=60)
        workspace.mark_created("same", "other", registration["workspace_id"])
    elif conflict == "app-owner":
        store.register(
            app_id=registration["consumer_app_id"],
            tenant_id="other",
            workspace_id="other-workspace",
            agent_name="other",
            profile_id="",
            actor="fixture",
            entry="consumer",
            environment="other",
        )
    else:
        register(candidate)
        with store.connection() as db:
            if conflict == "partial":
                db.execute("DELETE FROM operator_apps WHERE entry='consumer'")
            elif conflict == "target-flags":
                db.execute("UPDATE operator_apps SET targets_json='{}' WHERE entry='consumer'")
            else:
                db.execute("UPDATE operator_apps SET scenario='other' WHERE entry='operator'")
    before = rows(candidate)
    with pytest.raises(SetupConflict):
        register(candidate)
    assert rows(candidate) == before


@pytest.mark.parametrize("failure", ["second-app", "inputs-changed", "constraint"])
def test_mid_transaction_failure_keeps_latest_evidence_and_no_partial_resources(
    candidate, monkeypatch, failure
):
    store = candidate[0]
    with store.connection() as db:
        db.execute("CREATE TABLE evidence(value TEXT)")
        db.execute("INSERT INTO evidence VALUES('before')")
    original = store.register
    calls = []

    def interrupted(**kw):
        calls.append(kw["entry"])
        original(**kw)
        if failure == "second-app" and kw["entry"] == "consumer":
            raise KeyboardInterrupt()
        if failure == "constraint":
            kw["connection"].execute("INSERT INTO operator_apps(app_id) VALUES(NULL)")

    monkeypatch.setattr(store, "register", interrupted)
    checks = []

    def verify():
        checks.append(1)
        if failure == "inputs-changed" and len(checks) == 2:
            raise ValueError("input drift")

    with pytest.raises((KeyboardInterrupt, ValueError, sqlite3.IntegrityError)):
        register(candidate, verify)
    assert rows(candidate)["operator_apps"] == rows(candidate)["dify_workspaces"] == []
    with gateway_db.connection(store.path, readonly=True) as db:
        assert db.execute("SELECT value FROM evidence").fetchall()[0][0] == "before"


def test_consumed_candidate_reregisters_with_reminted_app(candidate):
    store, registration, _ = candidate
    assert register(candidate) is True
    # 发布消费了原候选（应用已提升不可降级），准备对账重铸出新的 app_id；
    # 重新登记应原位替换同租户同端的旧行，而不是按缺登记拒绝。
    registration["consumer_app_id"] = "consumer-reminted"
    assert register(candidate) is True
    apps = {r["entry"]: r for r in rows(candidate)["operator_apps"]}
    assert set(apps) == {"operator", "consumer"}
    assert apps["consumer"]["app_id"] == "consumer-reminted"
    assert apps["consumer"]["agent_name"] == "consumer-consumer-reminted"
    assert apps["consumer"]["revision"] == 1
    assert apps["operator"]["app_id"] == "operator-target"
    assert all(r["app_id"] != "consumer-target" for r in apps.values())
    assert register(candidate) is False


@pytest.mark.parametrize("drift", ["actor", "workspace", "name", "profile", "scenario", "revision"])
def test_replaced_app_row_must_share_registration_lineage(candidate, drift):
    store, registration, _ = candidate
    register(candidate)
    registration["consumer_app_id"] = "consumer-reminted"
    column, value = {
        "actor": ("created_by", "'other'"),
        "workspace": ("workspace_id", "'other-workspace'"),
        "name": ("agent_name", "'renamed'"),
        "profile": ("profile_id", "'other-profile'"),
        "scenario": ("scenario", "'other'"),
        "revision": ("revision", "2"),
    }[drift]
    with store.connection() as db:
        db.execute(f"UPDATE operator_apps SET {column}={value} WHERE entry='consumer'")
    before = rows(candidate)
    with pytest.raises(SetupConflict):
        register(candidate)
    assert rows(candidate) == before


def test_readonly_observe_does_not_replace_consumed_row(candidate):
    store, registration, consumer = candidate
    register(candidate)
    registration["consumer_app_id"] = "consumer-reminted"
    before = rows(candidate)
    with pytest.raises(SetupConflict):
        store.register_runtime(registration, consumer, verify=lambda: None, observe_only=True)
    assert rows(candidate) == before


def test_registration_rejects_formal_binding(candidate):
    from aiops_diagnostics.dify.dify_app_registry import DifyAppBinding, DifyAppRegistry

    DifyAppRegistry(candidate[0].path).put(DifyAppBinding("same", "operator", "operator-existing", "old"))
    before = rows(candidate)
    with pytest.raises(SetupConflict):
        register(candidate)
    assert rows(candidate) == before


def _bind(candidate, state):
    with candidate[0].connection() as db:
        db.execute(
            "INSERT INTO chat_route_bindings(environment,tenant_id,entry,scenario,engine,"
            "agent_id,agent_version_no,app_id,workflow_id,workflow_key_ref,state,revision,"
            "updated_by,updated_at,node_config_file) "
            "VALUES('target','same','operator','operator_order_diagnosis','node','agt_x',1,"
            "'operator-target','','',?,2,'fixture','2026-01-01T00:00:00Z','')",
            (state,),
        )


def test_retracted_binding_and_own_registry_row_are_own_lineage_not_conflict(candidate):
    # 发布血统留下的两行都不算占用：撤回绑定按设计留行，注册表行是发布激活写的
    # 本租户同应用绑定。已发布租户必须还能再登记/再观察。
    from aiops_diagnostics.dify.dify_app_registry import DifyAppBinding, DifyAppRegistry

    store, registration, consumer = candidate
    register(candidate)
    _bind(candidate, "retracted")
    DifyAppRegistry(store.path).put(
        DifyAppBinding("same", "operator", registration["operator_app_id"], "old")
    )
    assert register(candidate) is False
    assert store.register_runtime(registration, consumer, verify=lambda: None, observe_only=True) is False


def test_active_binding_and_foreign_registry_claim_still_conflict(candidate):
    from aiops_diagnostics.dify.dify_app_registry import DifyAppBinding, DifyAppRegistry

    store, registration, _ = candidate
    register(candidate)
    _bind(candidate, "active")
    with pytest.raises(SetupConflict):
        register(candidate)
    with store.connection() as db:
        db.execute("DELETE FROM chat_route_bindings")
    DifyAppRegistry(store.path).put(
        DifyAppBinding("other", "operator", registration["consumer_app_id"], "old")
    )
    before = rows(candidate)
    with pytest.raises(SetupConflict):
        register(candidate)
    assert rows(candidate) == before


def test_concurrent_registration_has_one_commit_and_one_unchanged_receipt(candidate):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: register(candidate), range(2)))
    assert sorted(results) == [False, True]
    assert len(rows(candidate)["operator_apps"]) == 2
    assert len(rows(candidate)["dify_workspaces"]) == 1


def test_inflight_work_refuses_registration_before_any_write(candidate):
    from aiops_diagnostics.gateway.gateway_store import GatewayStore

    store = GatewayStore(candidate[0].path)
    with store._connection(write=True) as db:
        db.execute(
            "INSERT INTO assistant_questions(qa_id,scope_fingerprint,question,status,"
            "created_at,updated_at,deadline_at) "
            "VALUES('q','s','q','running','n','n','n')"
        )
    before = rows(candidate)
    with pytest.raises(SetupConflict, match="INFLIGHT"):
        register(candidate)
    assert rows(candidate) == before


def test_direct_admin_without_managed_receipt_cannot_register(tmp_path):
    from typer.testing import CliRunner

    from aiops_diagnostics.admin_cli import admin_app

    result = CliRunner().invoke(admin_app, ["environment-runtime-register", "--sha256", "a" * 64])
    assert result.exit_code != 0


def test_readonly_reconciliation_never_repairs_missing_or_partial_resources(candidate):
    store, registration, consumer = candidate
    before = rows(candidate)
    assert store.register_runtime(registration, consumer, verify=lambda: None, observe_only=True) is None
    assert rows(candidate) == before
    register(candidate)
    before = rows(candidate)
    assert store.register_runtime(registration, consumer, verify=lambda: None, observe_only=True) is False
    assert rows(candidate) == before
    with store.connection() as db:
        db.execute("DELETE FROM operator_apps WHERE entry='consumer'")
    before = rows(candidate)
    with pytest.raises(SetupConflict):
        store.register_runtime(registration, consumer, verify=lambda: None, observe_only=True)
    assert rows(candidate) == before
