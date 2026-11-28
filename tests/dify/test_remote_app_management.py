"""单控制面跨环境 Dify 工作区、远端受管发布与退役旧开通顶栏测试（ADR-0372）。"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiops_diagnostics.dify import remote_app_management, self_service_identity
from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces
from aiops_diagnostics.dify.self_service import SelfService, draft_fingerprint
from aiops_diagnostics.dify.self_service_store import SelfServiceStore, SetupConflict
from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore
from aiops_diagnostics.gateway import gateway_schema
from aiops_diagnostics.onboarding import registry as env_registry
from aiops_diagnostics.publication.agent_lifecycle import AgentConfig, AgentStore

ROOT = Path(__file__).resolve().parents[2]
AGENT_PATH = ROOT / "src/aiops_diagnostics/onboarding/host_release_agent.py"


def _load_host_agent():
    spec = importlib.util.spec_from_file_location("host_release_agent_test_mod", AGENT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed_remote_host_dirs(tmp_path: Path, env: str = "jiejia"):
    etc_root = tmp_path / "etc"
    var_root = tmp_path / "var"
    runtime_dir = etc_root / f"aiops-access-{env}" / "runtime"
    gw_dir = var_root / f"aiops-access-{env}" / "gateway"
    runtime_dir.mkdir(parents=True)
    gw_dir.mkdir(parents=True)
    db_path = gw_dir / "gateway.db"
    gateway_schema.ensure(db_path)

    op_app_id = "71a466cc-7298-58ef-bad2-7f6031239e8e"
    con_app_id = "47e64e25-1588-5f4f-b350-97e37963fcc1"
    ws_id = "c8c0a12f-7e0c-4564-8f40-cb1bc212bca0"
    tenant_id = "1990990768688599040"
    kb_id = "fe180a36e97b11f1a026f1f706dc4043"

    manifest_template = {
        "schema": "aiops-node-agent/2",
        "history_policy": "recent-12",
        "model": {
            "provider_key": "deepseek",
            "adapter_key": "openai",
            "model_key": "deepseek-v4-1-flash-261017",
            "endpoint": "https://api.example/v1",
        },
        "model_options": {"temperature": 0.1, "max_tokens": 4096},
        "system_messages": ["旧管家提示词"],
        "tools": [],
    }
    (runtime_dir / "operator-manifest.json").write_text(
        json.dumps(manifest_template, ensure_ascii=False), encoding="utf-8"
    )

    store = SelfServiceStore(db_path)
    store.register(
        app_id=op_app_id,
        tenant_id=tenant_id,
        workspace_id=ws_id,
        agent_name=f"operator-{env}",
        profile_id=f"operator-{env}",
        actor="wizard-worker",
        entry="operator",
        environment=env,
    )
    store.register(
        app_id=con_app_id,
        tenant_id=tenant_id,
        workspace_id=ws_id,
        agent_name=f"consumer-{con_app_id}",
        profile_id="",
        actor="wizard-worker",
        entry="consumer",
        environment=env,
        targets={"knowledge_base_ids": [kb_id], "live_order_status": True, "live_billing_record": True},
    )

    agents = AgentStore(db_path)
    op_agt = agents.create(
        tenant_id,
        f"operator-{env}",
        "",
        AgentConfig(
            agent_type="operations",
            prompt="旧管家提示词",
            knowledge_base_ids=(),
            model="deepseek-v4-1-flash-261017",
            output_contract="diagnosis-v1",
        ),
        "wizard-worker",
    )
    op_v1 = agents.publish(
        op_agt.agent_id,
        tenant_id,
        1,
        {
            "app_id": op_app_id,
            "workspace_id": ws_id,
            "workflow_id": "",
            "node_manifest": json.dumps(manifest_template),
        },
        "wizard-worker",
    )
    routes = ChatRouteBindingStore(db_path)
    routes.publish(
        environment=env,
        tenant_id=tenant_id,
        entry="operator",
        scenario="operator_order_diagnosis",
        engine="node",
        agent_id=op_agt.agent_id,
        agent_version_no=op_v1.version_no,
        app_id=op_app_id,
        node_config_file=f"/etc/aiops-access-{env}/runtime/agent-runs.json",
        expected_revision=0,
        updated_by="wizard-worker",
    )

    con_agt = agents.create(
        tenant_id,
        f"consumer-{con_app_id}",
        "",
        AgentConfig(
            agent_type="customer",
            prompt="",
            knowledge_base_ids=(kb_id,),
            model="deepseek-v4-1-flash-261017",
            output_contract="blocks-v1",
        ),
        "wizard-worker",
    )
    con_v1 = agents.publish(
        con_agt.agent_id,
        tenant_id,
        1,
        {"app_id": con_app_id, "workspace_id": ws_id, "workflow_id": "wf-v1", "consumer_service": {}},
        "wizard-worker",
    )
    routes.publish(
        environment=env,
        tenant_id=tenant_id,
        entry="consumer",
        scenario="consumer_service",
        engine="dify",
        agent_id=con_agt.agent_id,
        agent_version_no=con_v1.version_no,
        app_id=con_app_id,
        workflow_id="wf-v1",
        workflow_key_ref=f"/var/lib/aiops-access-{env}/keys/{con_app_id}.key",
        expected_revision=0,
        updated_by="wizard-worker",
    )

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO consumer_knowledge_grants "
            "(tenant_id, environment, knowledge_base_id, granted_by, granted_at, revoked_at) "
            "VALUES (?, ?, ?, 'wizard-worker', 1.0, NULL)",
            (tenant_id, env, kb_id),
        )

    return {
        "etc_root": etc_root,
        "var_root": var_root,
        "db_path": db_path,
        "env": env,
        "tenant_id": tenant_id,
        "workspace_id": ws_id,
        "op_app_id": op_app_id,
        "con_app_id": con_app_id,
        "kb_id": kb_id,
    }


def test_host_release_agent_app_lifecycle_commands(tmp_path: Path, monkeypatch) -> None:
    """客户机 host_release_agent 5 条 SQLite 子命令：读态、乐观锁发布、历史版本、撤回/恢复与知识授权。"""
    agent = _load_host_agent()
    seeded = _seed_remote_host_dirs(tmp_path)
    monkeypatch.setattr(agent, "ETC_BASE", seeded["etc_root"])
    monkeypatch.setattr(agent, "VAR_BASE", seeded["var_root"])

    st = agent.cmd_app_state(
        {
            "environment": seeded["env"],
            "tenant_id": seeded["tenant_id"],
            "app_id": seeded["op_app_id"],
            "entry": "operator",
        }
    )
    assert st["ok"] is True
    assert st["route"]["agent_version_no"] == 1
    assert st["route"]["revision"] == 1
    assert st["granted_knowledge_base_ids"] == [seeded["kb_id"]]

    # 乐观锁冲突拒绝
    conflict = agent.cmd_app_publish(
        {
            "environment": seeded["env"],
            "tenant_id": seeded["tenant_id"],
            "app_id": seeded["op_app_id"],
            "workspace_id": seeded["workspace_id"],
            "entry": "operator",
            "agent_name": "operator-jiejia",
            "expected_revision": 99,
            "actor": "admin-1",
            "snapshot": {"node_manifest": json.dumps({"system_messages": ["新提示词"], "tools": []})},
        }
    )
    assert conflict["ok"] is False
    assert conflict["conflict"] == "SETUP_ROUTE_CHANGED"

    # 正常发布 V2
    pub = agent.cmd_app_publish(
        {
            "environment": seeded["env"],
            "tenant_id": seeded["tenant_id"],
            "app_id": seeded["op_app_id"],
            "workspace_id": seeded["workspace_id"],
            "entry": "operator",
            "agent_name": "operator-jiejia",
            "expected_revision": 1,
            "actor": "admin-1",
            "snapshot": {
                "node_manifest": json.dumps({"system_messages": ["定制后的捷佳管家提示词"], "tools": []})
            },
        }
    )
    assert pub["ok"] is True
    assert pub["version_no"] == 2
    assert pub["revision"] == 2

    vers = agent.cmd_app_versions(
        {
            "environment": seeded["env"],
            "tenant_id": seeded["tenant_id"],
            "app_id": seeded["op_app_id"],
            "entry": "operator",
        }
    )
    assert vers["versions"] == [2, 1]

    # 撤回 + 恢复 V1
    retracted = agent.cmd_app_change_version(
        {
            "environment": seeded["env"],
            "tenant_id": seeded["tenant_id"],
            "app_id": seeded["op_app_id"],
            "workspace_id": seeded["workspace_id"],
            "entry": "operator",
            "expected_revision": 2,
            "version_no": None,
            "actor": "admin-1",
        }
    )
    assert retracted["ok"] is True
    assert retracted["state"] == "retracted"
    assert retracted["revision"] == 3

    restored = agent.cmd_app_change_version(
        {
            "environment": seeded["env"],
            "tenant_id": seeded["tenant_id"],
            "app_id": seeded["op_app_id"],
            "workspace_id": seeded["workspace_id"],
            "entry": "operator",
            "expected_revision": 3,
            "version_no": 1,
            "actor": "admin-1",
        }
    )
    assert restored["ok"] is True
    assert restored["state"] == "active"
    assert restored["version_no"] == 1
    assert restored["revision"] == 4

    # 知识库授权撤销与恢复
    revoked = agent.cmd_app_knowledge_grant(
        {
            "environment": seeded["env"],
            "tenant_id": seeded["tenant_id"],
            "knowledge_base_id": seeded["kb_id"],
            "allowed": False,
            "actor": "admin-1",
        }
    )
    assert revoked == {"ok": True, "id": seeded["kb_id"], "allowed": False}
    st_after = agent.cmd_app_state(
        {
            "environment": seeded["env"],
            "tenant_id": seeded["tenant_id"],
            "app_id": seeded["con_app_id"],
            "entry": "consumer",
        }
    )
    assert st_after["granted_knowledge_base_ids"] == []


def test_cross_env_workspace_and_remote_publish_isolation(tmp_path: Path, monkeypatch) -> None:
    """仅控制面显式 managers 可跨环境进入 jiejia 工作区并发布；普通租户账号严格拒绝。"""
    agent = _load_host_agent()
    seeded = _seed_remote_host_dirs(tmp_path)
    monkeypatch.setattr(agent, "ETC_BASE", seeded["etc_root"])
    monkeypatch.setattr(agent, "VAR_BASE", seeded["var_root"])

    ctrl_db = tmp_path / "ctrl" / "gateway.db"
    ctrl_db.parent.mkdir(parents=True)
    gateway_schema.ensure(ctrl_db)

    reg_file = tmp_path / "ctrl" / "registry.json"
    env_registry.apply(
        reg_file,
        [
            {
                "id": "41",
                "name": "41生产环境",
                "enabled": True,
                "gateway_url": "https://api.mall.qushiyun.com",
                "cpms_base_url": "https://api.mall.qushiyun.com/charging-pile",
                "company_gateway_url": "https://api.mall.qushiyun.com",
                "config_ref": "/etc/aiops-41/production.env",
                "tenants": [{"id": "t-41", "name": "Ulink", "enabled": True}],
            },
            {
                "id": "jiejia",
                "name": "捷佳四轮国内充电",
                "enabled": True,
                "gateway_url": "https://api.juxichongdian.cn",
                "cpms_base_url": "https://api.juxichongdian.cn/charging-pile",
                "company_gateway_url": "https://api.juxichongdian.cn",
                "config_ref": "/var/lib/aiops-41/onboarding-assets/jiejia.env",
                "tenants": [{"id": seeded["tenant_id"], "name": "捷佳", "enabled": True}],
            },
        ],
        expected_revision=0,
        actor="test-init",
        managers=[{"environment": "41", "tenant_id": "-1", "user_id": "mgr-1", "environments": ["*"]}],
    )

    spaces = DifyWorkspaces(ctrl_db)
    spaces.claim(seeded["tenant_id"], "jiejia", owner="test", lease_seconds=60)
    spaces.mark_created(seeded["tenant_id"], "jiejia", seeded["workspace_id"])

    ctrl_store = SelfServiceStore(ctrl_db)
    ctrl_store.register(
        app_id=seeded["op_app_id"],
        tenant_id=seeded["tenant_id"],
        workspace_id=seeded["workspace_id"],
        agent_name="operator-jiejia",
        profile_id="operator-jiejia",
        actor="wizard-worker",
        entry="operator",
        environment="jiejia",
    )

    audit_calls: list[dict] = []
    settings = SimpleNamespace(
        database_file=ctrl_db,
        environment="41",
        execution_environment="41",
        environment_registry_file="",
        environment_management_registry_file=str(reg_file),
        dify_console_base_url="http://dify.local",
        dify_console_api_key="test-key",
        chat_dify_key_dir=str(tmp_path / "ctrl-keys"),
        operator_profiles_file="",
        environment_manifest_file="",
    )

    def fake_runner(env: str, subcmd: str, payload: dict):
        assert env == "jiejia"
        return agent._COMMANDS[subcmd](payload)

    context = SimpleNamespace(
        settings=settings,
        remote_app_runner=fake_runner,
        store=SimpleNamespace(record_publish_audit=lambda **kw: audit_calls.append(kw)),
    )
    svc = SelfService(context)

    # 非 manager 严禁跨环境访问工作区或应用
    normal_caller = self_service_identity.ManagerIdentity("user-2", "t-41", False, "tok")
    with pytest.raises(self_service_identity.SetupRefused, match="ENVIRONMENT_MANAGEMENT_FORBIDDEN"):
        svc.workspace(normal_caller, seeded["workspace_id"])
    with pytest.raises(self_service_identity.SetupRefused, match="ENVIRONMENT_MANAGEMENT_FORBIDDEN"):
        svc.authorize_app(normal_caller, seeded["op_app_id"])

    # 显式 manager 可跨环境切换工作区并读取实时状态
    mgr_caller = self_service_identity.ManagerIdentity("mgr-1", "-1", True, "tok")
    assert svc.workspace_scope(mgr_caller, seeded["workspace_id"]) == (seeded["tenant_id"], "jiejia")
    assert svc.workspace(mgr_caller, seeded["workspace_id"]) == seeded["tenant_id"]

    st = svc.status(mgr_caller, seeded["op_app_id"])
    assert st["managed"] is True
    assert st["remote_managed"] is True
    assert st["environment"] == "jiejia"
    assert st["environment_name"] == "捷佳四轮国内充电"
    assert st["tenant_name"] == "捷佳"
    assert st["version_no"] == 1
    assert st["route_revision"] == 1
    assert st["published_prompt"] == "旧管家提示词"

    # 模拟 Dify 草稿更新后跨环境发布到 jiejia 客户机
    new_mc = {"pre_prompt": "捷佳定制管家 SOP v2", "agent_mode": {"tools": []}}
    svc.console = SimpleNamespace(
        _call=lambda ws, path, **_kw: {"model_config": new_mc} if path.endswith(seeded["op_app_id"]) else {}
    )
    monkeypatch.setattr(
        remote_app_management.node_publication,
        "candidate",
        lambda _source, _target, *, operator_preview=None: json.dumps(
            {
                "schema": "aiops-node-agent/2",
                "history_policy": "recent-12",
                "model": {
                    "provider_key": "deepseek",
                    "adapter_key": "openai",
                    "model_key": "deepseek-v4-1-flash-261017",
                    "endpoint": "https://api.example/v1",
                },
                "model_options": {"temperature": 0.1, "max_tokens": 4096},
                "system_messages": [operator_preview["pre_prompt"]],
                "tools": [],
            },
            ensure_ascii=False,
        ),
    )

    pub = svc.publish(mgr_caller, seeded["op_app_id"], revision=1, expected_draft=draft_fingerprint(new_mc))
    assert pub["version_no"] == 2
    assert pub["revision"] == 2
    assert len(audit_calls) == 1
    assert audit_calls[0]["action"] == "published"

    # 发布后立即回读，确认客户机 gateway.db 已热更新至 V2
    st2 = svc.status(mgr_caller, seeded["op_app_id"])
    assert st2["version_no"] == 2
    assert st2["route_revision"] == 2
    assert st2["published_prompt"] == "捷佳定制管家 SOP v2"
    assert svc.versions(mgr_caller, seeded["op_app_id"]) == [2, 1]

    # 乐观锁过期 revision 拒绝
    with pytest.raises(SetupConflict, match="SETUP_ROUTE_CHANGED"):
        svc.publish(mgr_caller, seeded["op_app_id"], revision=1, expected_draft=draft_fingerprint(new_mc))


def test_legacy_tenant_setup_retired_and_single_env_login_hides_selector() -> None:
    """确认旧「复制并开通客户 AI 服务」已从补丁与桥彻底退役，且单环境登录页隐藏下拉框。"""
    p04 = (ROOT / "deploy/dify-36/web-patches/1.17.1/04-self-service.patch").read_text(encoding="utf-8")
    p06 = (ROOT / "deploy/dify-36/web-patches/1.17.1/06-workflow-and-home-entry.patch").read_text(
        encoding="utf-8"
    )
    mgmt = (ROOT / "deploy/dify-36/web-patches/1.17.1/aiops-management.tsx").read_text(encoding="utf-8")
    bridge = (ROOT / "services/dify-console-bridge/dify_console_bridge/self_service_bridge.py").read_text(
        encoding="utf-8"
    )
    login_html = (ROOT / "ops/dify-login-proxy/dify-login-page.html").read_text(encoding="utf-8")

    assert "TenantSetup" not in p04
    assert "TenantSetup" not in p06
    assert "复制并开通客户 AI 服务" not in mgmt
    assert "TenantSetup" not in mgmt
    assert '"catalogue"' not in bridge
    assert '"/operations"' not in bridge
    assert "data.environments.length <= 1" in login_html


def test_node_all_targets_ignores_remote_environment_operator_apps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """控制面启动加载本机节点目标时，必须忽略远端客户环境的 operator_apps 行。"""
    from aiops_diagnostics.dify import self_service_config
    from aiops_diagnostics.dify.self_service_store import SelfServiceStore
    from aiops_diagnostics.execution import node_publication

    db_file = tmp_path / "control.db"
    store = SelfServiceStore(db_file)
    store.register(
        app_id="137cc417-f068-43c2-9d4a-43ebd254ea01",
        tenant_id="1942105476598861824",
        workspace_id="c3f871e1-ffd0-4a10-a84a-b8696f7acd25",
        agent_name="ops-methodology-700",
        profile_id="operator-41",
        actor="test",
        entry="operator",
        environment="41",
    )
    store.register(
        app_id="71a466cc-7298-58ef-bad2-7f6031239e8e",
        tenant_id="1990990768688599040",
        workspace_id="c8c0a12f-7e0c-4564-8f40-cb1bc212bca0",
        agent_name="operator-jiejia",
        profile_id="operator-jiejia",
        actor="test",
        entry="operator",
        environment="jiejia",
    )

    monkeypatch.setattr(
        self_service_config,
        "profiles",
        lambda _s: {
            "operator-41": {
                "target": {
                    "environment": "41",
                    "entry": "operator",
                    "config_file": str(tmp_path / "node.json"),
                    "manifest_file": str(tmp_path / "manifest.json"),
                }
            }
        },
    )
    monkeypatch.setattr(node_publication, "validate_target", lambda _row: None)

    settings = SimpleNamespace(
        database_file=db_file,
        chat_node_targets_file="",
        operator_profiles_file=str(tmp_path / "profiles.json"),
        execution_environment="41",
    )
    loaded = node_publication.all_targets(settings)
    assert [t.app_id for t in loaded] == ["137cc417-f068-43c2-9d4a-43ebd254ea01"]
