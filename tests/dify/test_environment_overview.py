"""环境总览与运行节点可见性测试（ADR-0335 / D50）。"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from aiops_diagnostics.dify import dify_console_publish as bridge
from aiops_diagnostics.dify import environment_management as management
from aiops_diagnostics.dify import self_service_identity as auth
from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces
from aiops_diagnostics.gateway import (
    environment_drafts as drafts,
)
from aiops_diagnostics.gateway import (
    environment_registry as registry,
)
from aiops_diagnostics.gateway import (
    environment_tasks,
    gateway_db,
    gateway_schema,
)
from aiops_diagnostics.gateway.gateway_api import create_gateway_app
from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings


def _tools_digest() -> str:
    raw = files("aiops_diagnostics.operator").joinpath("operator_tools.json").read_bytes()
    return hashlib.sha256(raw).hexdigest()


def _seed_overview_fixture(tmp_path: Path):
    reg_dir = tmp_path / "registry"
    reg_dir.mkdir(mode=0o700)
    reg_path = reg_dir / "directory.json"
    cfg_41 = tmp_path / "env-41-secret.env"
    cfg_95 = tmp_path / "env-95-secret.env"
    cfg_96 = tmp_path / "env-96-secret.env"
    for cfg, secret in ((cfg_41, "topsecret"), (cfg_95, "othersecret"), (cfg_96, "thirdsecret")):
        cfg.write_text(f"SECRET_TOKEN={secret}\n", encoding="utf-8")
        cfg.chmod(0o600)

    registry.apply(
        reg_path,
        [
            {
                "id": "41",
                "name": "41生产环境",
                "enabled": True,
                "gateway_url": "https://gw41.example.test",
                "cpms_base_url": "https://cpms41.example.test",
                "company_gateway_url": "https://co41.example.test",
                "config_ref": str(cfg_41),
                "tenants": [
                    {"id": "t41-a", "name": "租户甲", "enabled": True},
                    {"id": "t41-b", "name": "租户乙", "enabled": False},
                    {"id": "t41-retracted", "name": "撤回路由租户", "enabled": True},
                ],
            },
            {
                "id": "95",
                "name": "95停用环境",
                "enabled": False,
                "gateway_url": "https://gw95.example.test",
                "cpms_base_url": "https://cpms95.example.test",
                "company_gateway_url": "https://co95.example.test",
                "config_ref": str(cfg_95),
                "tenants": [
                    {"id": "t95-a", "name": "远端租户", "enabled": False},
                    # 环境整体 disabled 但租户 enabled=True：绝不能伪报 enabled/active！
                    {"id": "t95-b", "name": "环境停用内启用租户", "enabled": True},
                ],
            },
            {
                "id": "96",
                "name": "96启用远端环境",
                "enabled": True,
                "gateway_url": "https://gw96.example.test",
                "cpms_base_url": "https://cpms96.example.test",
                "company_gateway_url": "https://co96.example.test",
                "config_ref": str(cfg_96),
                "tenants": [
                    {"id": "t96-live", "name": "远端已开通租户", "enabled": True},
                    {"id": "t96-restored", "name": "远端已恢复租户", "enabled": True},
                ],
            },
        ],
        expected_revision=0,
        actor="operator",
        managers=[
            {"environment": "41", "tenant_id": "platform", "user_id": "root-admin", "environments": ["*"]},
            {"environment": "41", "tenant_id": "platform", "user_id": "only-41", "environments": ["41"]},
            {"environment": "41", "tenant_id": "platform", "user_id": "only-96", "environments": ["96"]},
        ],
    )

    drafts.save(
        reg_path,
        ("41", "platform", "root-admin"),
        {
            "id": "124",
            "name": "124候选环境",
            "enabled": False,
            "gateway_url": "https://gw124.example.test",
            "cpms_base_url": "https://cpms124.example.test",
            "company_gateway_url": "https://co124.example.test",
            "deployment_target": "host-124",
            "credential_ref": "cred-124",
            "timezone": "Asia/Shanghai",
            "tenants": [
                {"id": "t124-a", "name": "草稿租户", "enabled": False},
                # 试图借 platform 租户名探查内部平台哨兵工作区
                {"id": "platform", "name": "伪装平台", "enabled": False},
            ],
        },
        expected_revision=0,
        registry_revision=1,
    )

    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o700)
    gw_db = data_dir / "gateway.db"
    gateway_schema.ensure(gw_db)
    DifyWorkspaces(gw_db)
    now = time.time()
    with gateway_db.connection(gw_db, write=True) as conn:
        for env, tid, wsid, name, st in (
            ("41", "t41-a", "ws-41-a", "aiops-41-a", "ready"),
            ("124", "platform", "ws-secret-platform-sentinel", "aiops-124-platform", "ready"),
            ("95", "t95-a", "ws-95-a", "aiops-95-a", "created"),
        ):
            conn.execute(
                "INSERT INTO dify_workspaces("
                "environment,tenant_id,workspace_id,workspace_name,setup_state,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?)",
                (env, tid, wsid, name, st, now, now),
            )
        conn.execute(
            """
            INSERT INTO chat_route_bindings(
                environment,tenant_id,entry,scenario,engine,agent_id,agent_version_no,
                app_id,workflow_id,workflow_key_ref,revision,state,node_config_file,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "production-41",
                "t41-a",
                "operator",
                "operator_order_diagnosis",
                "node",
                "agt_op_41",
                36,
                "11111111-1111-1111-1111-111111111111",
                "",
                "",
                5,
                "active",
                "/private/secret/agent-runs.json",
                "2026-11-25T00:00:00Z",
            ),
        )
        conn.execute(
            """
            INSERT INTO chat_route_bindings(
                environment,tenant_id,entry,scenario,engine,agent_id,agent_version_no,
                app_id,workflow_id,workflow_key_ref,revision,state,node_config_file,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "production-41",
                "t41-a",
                "consumer",
                "consumer_service",
                "dify",
                "agt_cs_41",
                35,
                "22222222-2222-2222-2222-222222222222",
                "wf_41",
                "/private/secret/workflow.key",
                7,
                "active",
                "",
                "2026-11-25T00:00:00Z",
            ),
        )
        # 控制环境已撤回的路由：即使有历史 environment-publish 也不得伪报 active！
        conn.execute(
            """
            INSERT INTO chat_route_bindings(
                environment,tenant_id,entry,scenario,engine,agent_id,agent_version_no,
                app_id,workflow_id,workflow_key_ref,revision,state,node_config_file,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "production-41",
                "t41-retracted",
                "operator",
                "operator_order_diagnosis",
                "node",
                "agt_op_ret",
                9,
                "99999999-9999-9999-9999-999999999999",
                "",
                "",
                3,
                "retracted",
                "",
                "2026-11-25T00:00:00Z",
            ),
        )
        conn.execute(
            """
            INSERT INTO consumer_knowledge_grants(
                tenant_id,environment,knowledge_base_id,granted_by,granted_at,revoked_at
            ) VALUES(?,?,?,?,?,?)
            """,
            ("t41-a", "production-41", "kb-active", "op", now, None),
        )
        conn.execute(
            """
            INSERT INTO consumer_knowledge_grants(
                tenant_id,environment,knowledge_base_id,granted_by,granted_at,revoked_at
            ) VALUES(?,?,?,?,?,?)
            """,
            ("t41-a", "production-41", "kb-revoked", "op", now - 20, now - 5),
        )
        # 在线且工具一致节点
        conn.execute(
            "INSERT INTO execution_node_capabilities(environment,node_id,body,observed_at) VALUES(?,?,?,?)",
            (
                "production-41",
                "node-41",
                json.dumps(
                    {
                        "schemas": ["operator-agent/1", "operator-conversation/1"],
                        "tool_contract": "request-target/2",
                        "tools_sha256": _tools_digest(),
                    }
                ),
                now - 10,
            ),
        )
        # 工具摘要相同但 tool_contract 不合法（须标 tools_compatible=False 且不列工具）
        conn.execute(
            "INSERT INTO execution_node_capabilities(environment,node_id,body,observed_at) VALUES(?,?,?,?)",
            (
                "production-41",
                "node-bad-contract",
                json.dumps(
                    {
                        "schemas": None,
                        "tool_contract": "legacy-contract/0",
                        "tools_sha256": _tools_digest(),
                    }
                ),
                now - 5,
            ),
        )
        # 心跳超时且摘要不匹配节点
        conn.execute(
            "INSERT INTO execution_node_capabilities(environment,node_id,body,observed_at) VALUES(?,?,?,?)",
            (
                "production-41",
                "node-old",
                json.dumps(
                    {
                        "schemas": ["operator-agent/1"],
                        "tool_contract": "request-target/1",
                        "tools_sha256": "0" * 64,
                    }
                ),
                now - 600,
            ),
        )

    draft_db = drafts._database(reg_path)
    with gateway_db.initialize(draft_db) as conn:
        drafts.initialize_tasks(conn)

        def insert_task(
            task_id: str, env: str, req: dict, state: str, res: dict, updated_at: float, err: str = ""
        ):
            conn.execute(
                "INSERT INTO environment_tasks(id,environment,request,state,result,error,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (task_id, env, json.dumps(req), state, json.dumps(res), err, updated_at),
            )

        plan_fresh = {
            "id": "plan_" + "a" * 32,
            "expires_at": now + 600,
            "environment": "95",
            "digest": "fresh-digest",
        }
        plan_drifted = {
            "id": "plan_" + "b" * 32,
            "expires_at": now + 600,
            "environment": "95",
            "digest": "drifted-digest",
        }
        secret_target = {"id": "host-95", "host": "10.99.99.95", "user": "ssh_secret_user", "port": 22222}

        # 控制环境撤回租户存在历史 publish 回执：因 is_control=True 必须按 live 路由判，不得伪报 active
        insert_task(
            "task_" + "0" * 32,
            "41",
            {"action": "environment-publish", "tenant_id": "t41-retracted", "plan": plan_fresh},
            "observed",
            {
                "expires_at": now + 600,
                "phase": "activated",
                "activated": True,
                "directory": "synchronized",
                "versions": {"operator": {"app_id": "99999999-9999-9999-9999-999999999999", "version_no": 9}},
            },
            now - 10,
        )

        # 95 远端主机与安装观察
        insert_task(
            "task_" + "1" * 32,
            "95",
            {"action": "host-verify", "plan": plan_fresh, "target": secret_target},
            "observed",
            {
                "expires_at": now + 600,
                "evidence": {"remote_hostname": "ecs-95", "service_user_present": True},
            },
            now - 50,
        )
        insert_task(
            "task_" + "2" * 32,
            "95",
            {
                "action": "candidate-install",
                "tenant_id": "t95-a",
                "plan": plan_fresh,
                "target": secret_target,
            },
            "observed",
            {"expires_at": now + 600, "installation": "installed", "version": "1.40.4"},
            now - 40,
        )
        # 随后的 site-preflight 不含 installation 字段，不得覆盖上面已确认的 installation="installed"
        insert_task(
            "task_" + "a" * 32,
            "95",
            {"action": "site-preflight", "plan": plan_fresh, "target": secret_target},
            "observed",
            {"expires_at": now + 600, "version": "1.40.4"},
            now - 38,
        )
        insert_task(
            "task_" + "3" * 32,
            "95",
            {"action": "knowledge-prepare", "tenant_id": "t95-a", "plan": plan_fresh},
            "observed",
            {"expires_at": now + 600, "knowledge_base_id": "kb-95-a"},
            now - 35,
        )
        insert_task(
            "task_" + "4" * 32,
            "95",
            {"action": "knowledge-connect", "tenant_id": "t95-a", "plan": plan_fresh},
            "observed",
            {"expires_at": now + 600, "connection_id": "conn-95-a"},
            now - 34,
        )
        insert_task(
            "task_" + "5" * 32,
            "95",
            {"action": "operator-prepare", "tenant_id": "t95-a", "plan": plan_fresh},
            "observed",
            {"expires_at": now + 600, "app_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
            now - 33,
        )
        # blocked 的 consumer-prepare 带有预分配 ghost app_id：绝不能当 candidate 展示！
        insert_task(
            "task_" + "b" * 32,
            "95",
            {"action": "consumer-prepare", "tenant_id": "t95-a", "plan": plan_fresh},
            "blocked",
            {"expires_at": now + 600, "state": "blocked", "app_id": "deadbeef-0000-0000-0000-000000000000"},
            now - 32,
            err="DIFY_IMPORT_FAILED",
        )
        # 漂移但尚未超时的验证任务（digest="drifted-digest"）：须经 _current 检出为 expired！
        insert_task(
            "task_" + "6" * 32,
            "95",
            {"action": "candidate-validate", "tenant_id": "t95-a", "plan": plan_drifted},
            "observed",
            {
                "expires_at": now + 600,
                "checks": [
                    {"capability": "consumer", "state": "passed"},
                    {"capability": "order", "state": "passed"},
                    {"capability": "offline", "state": "passed"},
                ],
            },
            now - 30,
        )
        # 95 已停用环境内的 t95-b 虽有完整 observed publish 回执，因环境停用绝不得报 active！
        insert_task(
            "task_" + "7" * 32,
            "95",
            {"action": "environment-publish", "tenant_id": "t95-b", "plan": plan_fresh},
            "observed",
            {
                "expires_at": now + 600,
                "phase": "activated",
                "activated": True,
                "directory": "synchronized",
                "versions": {"operator": {"app_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "version_no": 3}},
            },
            now - 25,
        )
        insert_task(
            "task_" + "9" * 32,
            "95",
            {"action": "candidate-validate", "tenant_id": "t95-b", "plan": plan_fresh},
            "blocked",
            {
                "expires_at": now + 600,
                "checks": [
                    {"capability": "consumer", "state": "passed"},
                    {"capability": "order", "state": "blocked", "error_code": "ORDER_MISSING"},
                ],
            },
            now - 20,
            err="ORDER_MISSING",
        )

        # 96 启用远端环境：t96-live 真实同步开通（versions + directory=synchronized）；
        # t96-restored 随后恢复。
        plan_96 = {**plan_fresh, "environment": "96"}
        insert_task(
            "task_" + "c" * 32,
            "96",
            {"action": "environment-publish", "tenant_id": "t96-live", "plan": plan_96},
            "observed",
            {
                "expires_at": now + 600,
                "phase": "activated",
                "activated": True,
                "directory": "synchronized",
                "versions": {
                    "operator": {"app_id": "cccccccc-cccc-cccc-cccc-cccccccccccc", "version_no": 4},
                    "consumer": {"app_id": "dddddddd-dddd-dddd-dddd-dddddddddddd", "version_no": 2},
                },
            },
            now - 15,
        )
        insert_task(
            "task_" + "d" * 32,
            "96",
            {"action": "environment-publish", "tenant_id": "t96-restored", "plan": plan_96},
            "observed",
            {
                "expires_at": now + 600,
                "phase": "activated",
                "activated": True,
                "directory": "synchronized",
                "versions": {"operator": {"app_id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee", "version_no": 8}},
            },
            now - 14,
        )
        insert_task(
            "task_" + "e" * 32,
            "96",
            {"action": "environment-publish", "tenant_id": "t96-restored", "plan": plan_96},
            "cancelled",
            {"expires_at": now + 600, "phase": "restored", "activated": False},
            now - 5,
        )

    return reg_path, gw_db, cfg_41


def test_overview_honest_states_drift_detection_and_zero_secret_leakage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg_path, gw_db, cfg_41 = _seed_overview_fixture(tmp_path)
    # 模拟真实的 _current 漂移校验：fresh-digest 通过，drifted-digest 抛过期
    monkeypatch.setattr(
        environment_tasks,
        "_current",
        lambda path, req, ref, *, reconciling=False: (
            None
            if req.get("plan", {}).get("digest") == "fresh-digest"
            else (_ for _ in ()).throw(environment_tasks.TaskRefused("ENVIRONMENT_TASK_PLAN_EXPIRED"))
        ),
    )

    settings = GatewayServerSettings(
        data_home=tmp_path / "data",
        database_file=gw_db,
        environment="41",
        execution_environment="production-41",
        server_config_file=cfg_41,
        environment_management_registry_file=str(reg_path),
        dify_publish_api_key="service-secret",
        dify_console_base_url="http://127.0.0.1:1",
        dify_console_api_key="console-secret",
    )
    app = create_gateway_app(settings=settings)
    current_user = {"id": "root-admin"}
    monkeypatch.setattr(
        auth,
        "identity",
        lambda *a, **k: auth.ManagerIdentity(current_user["id"], "platform", True, "token"),
    )
    headers = {"Authorization": "Bearer service-secret", "X-AIOps-Console-Session": "live-session"}

    with TestClient(app) as client:
        assert client.get("/v1/dify/management/environment-overview").status_code == 403

        resp = client.get("/v1/dify/management/environment-overview", headers=headers)
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-store"
        raw_text = resp.text
        for forbidden in (
            "env-41-secret.env",
            "env-95-secret.env",
            "env-96-secret.env",
            "config_ref",
            "root-admin",
            "only-41",
            "only-96",
            "10.99.99.95",
            "ssh_secret_user",
            "/private/secret/",
            "service-secret",
            "ws-secret-platform-sentinel",
            "deadbeef-0000-0000-0000-000000000000",
        ):
            assert forbidden not in raw_text

        data = resp.json()
        assert data["schema"] == "aiops-environment-overview/1"
        assert [e["id"] for e in data["environments"]] == ["41", "95", "96", "124"]

        env41 = data["environments"][0]
        assert env41["is_control"] is True
        assert env41["enabled_tenant_count"] == 2  # t41-a + t41-retracted
        assert env41["active_route_count"] == 2
        nodes41 = {n["node_id"]: n for n in env41["nodes"]}
        assert nodes41["node-41"]["status"] == "online"
        assert nodes41["node-41"]["tools_compatible"] is True
        assert len(nodes41["node-41"]["available_tools"]) > 0
        # tool_contract 不合法或工具摘要不匹配均不得列可用工具
        assert nodes41["node-bad-contract"]["tools_compatible"] is False
        assert nodes41["node-bad-contract"]["available_tools"] == []
        assert nodes41["node-old"]["status"] == "stale"
        assert nodes41["node-old"]["tools_compatible"] is False
        assert nodes41["node-old"]["available_tools"] == []

        t41_a = next(t for t in env41["tenants"] if t["id"] == "t41-a")
        assert t41_a["apps"]["operator"]["version_no"] == 36
        assert t41_a["apps"]["consumer"]["version_no"] == 35
        assert t41_a["knowledge"]["active_grants"] == 1
        assert t41_a["accuracy_confirmed"] is False

        # 控制环境已撤回路由绝不从历史 publish 任务伪报 active
        t41_ret = next(t for t in env41["tenants"] if t["id"] == "t41-retracted")
        assert t41_ret["apps"]["operator"]["state"] != "active"

        # 95 停用环境：整体停用时所有租户均不得报 enabled/active，enabled_tenant_count 必须为 0
        env95 = data["environments"][1]
        assert env95["status"] == "disabled"
        assert env95["enabled_tenant_count"] == 0
        assert len(env95["nodes"]) == 1
        assert env95["nodes"][0]["source"] == "task_receipt"
        assert env95["nodes"][0]["status"] == "observed"
        assert env95["nodes"][0]["remote_hostname"] == "ecs-95"
        assert env95["nodes"][0]["installation"] == "installed"
        assert env95["nodes"][0]["version"] == "1.40.4"

        t95_a = next(t for t in env95["tenants"] if t["id"] == "t95-a")
        assert t95_a["apps"]["operator"]["state"] == "candidate"
        # blocked consumer-prepare 幽灵 app_id 必须被忽略
        assert t95_a["apps"]["consumer"]["state"] == "unprepared"
        assert t95_a["apps"]["consumer"]["app_id"] == ""
        # 漂移的 candidate-validate 必须报 expired（即便 expires_at 未到期）
        assert t95_a["capabilities"]["consumer"]["state"] == "expired"
        assert t95_a["capabilities"]["order"]["state"] == "expired"
        assert t95_a["capabilities"]["offline"]["state"] == "expired"

        t95_b = next(t for t in env95["tenants"] if t["id"] == "t95-b")
        assert t95_b["admission"] == "disabled"
        assert t95_b["effective_enabled"] is False
        assert t95_b["apps"]["operator"]["state"] != "active"
        assert t95_b["capabilities"]["consumer"]["state"] == "passed"
        assert t95_b["capabilities"]["order"]["state"] == "blocked"

        # 96 启用远端环境：真实 versions + directory=synchronized 正确报 active；已恢复租户不报 active
        env96 = data["environments"][2]
        t96_live = next(t for t in env96["tenants"] if t["id"] == "t96-live")
        assert t96_live["apps"]["operator"] == {
            "app_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
            "version_no": 4,
            "state": "active",
            "source": "task_receipt",
        }
        assert t96_live["apps"]["consumer"] == {
            "app_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
            "version_no": 2,
            "state": "active",
            "source": "task_receipt",
        }
        t96_restored = next(t for t in env96["tenants"] if t["id"] == "t96-restored")
        assert t96_restored["apps"]["operator"]["state"] != "active"

        # 124 草稿环境：draft_revision=1，且 platform 租户不能看到内部平台哨兵工作区
        env124 = data["environments"][3]
        assert env124["status"] == "draft_only"
        assert env124["draft_revision"] == 1
        t124_plat = next(t for t in env124["tenants"] if t["id"] == "platform")
        assert t124_plat["workspace"]["id"] == ""

        # 受限管理员 only-96 既不能看到 41/95/124，也不能看到控制环境内部命名 production-41
        current_user["id"] = "only-96"
        scoped_96 = client.get("/v1/dify/management/environment-overview", headers=headers)
        assert scoped_96.status_code == 200
        s96_data = scoped_96.json()
        assert [e["id"] for e in s96_data["environments"]] == ["96"]
        assert s96_data["control_environment"] == ""
        assert s96_data["control_execution_environment"] == ""
        assert s96_data["control_version"] == ""
        assert "production-41" not in scoped_96.text and "ecs-95" not in scoped_96.text

        # 未授权/已撤权立即 403
        current_user["id"] = "revoked-user"
        assert client.get("/v1/dify/management/environment-overview", headers=headers).status_code == 403


def test_overview_handles_disabled_control_env_and_corrupted_runtime_db_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg_path, gw_db, _ = _seed_overview_fixture(tmp_path)
    body = registry.load(reg_path)
    body["environments"][0]["enabled"] = False
    registry.apply(
        reg_path,
        body["environments"],
        expected_revision=1,
        actor="operator",
        managers=body["managers"],
    )
    context = SimpleNamespace(
        settings=SimpleNamespace(
            environment="41",
            execution_environment="production-41",
            database_file=gw_db,
            environment_registry_file=str(reg_path),
        ),
        operator_cpms=SimpleNamespace(
            console_identity=lambda session: {"token": "t", "user_id": "only-41", "tenant_id": "platform"}
        ),
    )
    monkeypatch.setattr(
        auth,
        "company_get",
        lambda *a: {"sysUser": {"id": "only-41", "tenantId": "platform", "type": -1}, "permissions": []},
    )
    result = management.overview(context, "live-session")
    assert [e["id"] for e in result["environments"]] == ["41"]
    assert result["environments"][0]["status"] == "disabled"
    assert result["environments"][0]["enabled_tenant_count"] == 0

    # 运行库异常时，drafts 接口仍可返回草稿且不阻断恢复编辑
    context.settings.database_file = tmp_path / "corrupt.db"
    context.settings.database_file.write_bytes(b"SQLite format 3\x00" + b"\xff" * 64)
    drafts_resp = management.drafts(context, "live-session")
    assert drafts_resp["effective"][0]["id"] == "41"
    assert drafts_resp["overview"]["environments"][0]["id"] == "41"


def test_overview_devin_review_regressions_bounds_grants_memoization_partial_val_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """覆盖 Devin Review 6 项回归：1600 租户 1MiB 预算、停用控制路由、
    远端授权、记忆化漂移校验、部分验证保留与最新版本号。"""
    reg_path, gw_db, cfg_41 = _seed_overview_fixture(tmp_path)
    now = time.time()

    # 1) 控制环境停用租户 t41-b 保留 active 路由，必须报 disabled 而非 active
    # 2) 远端环境 96 与 95 均存在同名租户授权，仅统计对应环境未撤销授权
    with gateway_db.connection(gw_db, write=True) as conn:
        conn.execute(
            """
            INSERT INTO chat_route_bindings(
                environment,tenant_id,entry,scenario,engine,agent_id,agent_version_no,
                app_id,workflow_id,workflow_key_ref,revision,state,node_config_file,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "production-41",
                "t41-b",
                "operator",
                "operator_order_diagnosis",
                "node",
                "agt_op_disabled",
                12,
                "77777777-7777-7777-7777-777777777777",
                "",
                "",
                2,
                "active",
                "",
                "2026-11-25T00:00:00Z",
            ),
        )
        conn.execute(
            "INSERT INTO consumer_knowledge_grants("
            "tenant_id,environment,knowledge_base_id,granted_by,granted_at,revoked_at"
            ") VALUES(?,?,?,?,?,?)",
            ("t96-live", "96", "kb-remote-live", "op", now, None),
        )
        conn.execute(
            "INSERT INTO consumer_knowledge_grants("
            "tenant_id,environment,knowledge_base_id,granted_by,granted_at,revoked_at"
            ") VALUES(?,?,?,?,?,?)",
            ("t96-live", "96", "kb-remote-revoked", "op", now - 10, now - 2),
        )

    # 3) 95 旧 install=1.40.2 + 新 preflight=1.40.4；
    # 96 先全三项通过再单测 offline 阻塞（须保留 consumer/order 通过）
    draft_db = drafts._database(reg_path)
    shared_plan = {
        "id": "plan_" + "f" * 32,
        "expires_at": now + 600,
        "environment": "96",
        "draft_revision": 1,
        "registry_revision": 1,
        "digest": "fresh-digest",
    }
    with gateway_db.connection(draft_db, write=True) as conn:
        conn.execute(
            "UPDATE environment_tasks SET result=?, updated_at=? WHERE id=?",
            (
                json.dumps({"expires_at": now + 600, "installation": "installed", "version": "1.40.2"}),
                now - 45,
                "task_" + "2" * 32,
            ),
        )
        conn.execute(
            "INSERT INTO environment_tasks(id,environment,request,state,result,error,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                "task_" + "f" * 32,
                "96",
                json.dumps(
                    {
                        "identity": ["41", "platform", "root-admin"],
                        "action": "candidate-validate",
                        "tenant_id": "t96-live",
                        "plan": shared_plan,
                    }
                ),
                "observed",
                json.dumps(
                    {
                        "expires_at": now + 600,
                        "checks": [
                            {"capability": "consumer", "state": "passed"},
                            {"capability": "order", "state": "passed"},
                            {"capability": "offline", "state": "passed"},
                        ],
                    }
                ),
                "",
                now - 20,
            ),
        )
        conn.execute(
            "INSERT INTO environment_tasks(id,environment,request,state,result,error,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                "task_" + "1" * 31 + "a",
                "96",
                json.dumps(
                    {
                        "identity": ["41", "platform", "root-admin"],
                        "action": "candidate-validate",
                        "tenant_id": "t96-live",
                        "plan": shared_plan,
                    }
                ),
                "blocked",
                json.dumps(
                    {
                        "expires_at": now + 600,
                        "checks": [
                            {"capability": "offline", "state": "blocked", "error_code": "OFFLINE_TIMEOUT"}
                        ],
                    }
                ),
                "OFFLINE_TIMEOUT",
                now - 10,
            ),
        )
        # 再插 4 条同计划任务检验 _current 记忆化：同冻结计划只验一次
        for i in range(4):
            conn.execute(
                "INSERT INTO environment_tasks(id,environment,request,state,result,error,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    f"task_99{i:030d}",
                    "96",
                    json.dumps(
                        {
                            "identity": ["41", "platform", "root-admin"],
                            "action": "workspace-prepare",
                            "tenant_id": "t96-live",
                            "plan": shared_plan,
                        }
                    ),
                    "observed",
                    json.dumps({"expires_at": now + 600, "workspace_id": "ws-96"}),
                    "",
                    now - 15 - i,
                ),
            )

    current_calls: list[tuple[str, str]] = []

    def counting_current(path, req, ref, *, reconciling=False):
        plan_obj = req.get("plan") or {}
        plan_id = str(plan_obj.get("id") or "")
        digest = str(plan_obj.get("digest") or "")
        current_calls.append((plan_id, digest))
        if digest != "fresh-digest":
            raise environment_tasks.TaskRefused("ENVIRONMENT_TASK_PLAN_EXPIRED")

    monkeypatch.setattr(environment_tasks, "_current", counting_current)

    context = SimpleNamespace(
        settings=SimpleNamespace(
            environment="41",
            execution_environment="production-41",
            database_file=gw_db,
            environment_management_registry_file=str(reg_path),
        ),
        operator_cpms=SimpleNamespace(
            console_identity=lambda session: {"token": "t", "user_id": "root-admin", "tenant_id": "platform"}
        ),
    )
    monkeypatch.setattr(
        auth,
        "identity",
        lambda *a, **k: auth.ManagerIdentity("root-admin", "platform", True, "token"),
    )

    ov = management.overview(context, "live-session")
    env41 = next(e for e in ov["environments"] if e["id"] == "41")
    t41_b = next(t for t in env41["tenants"] if t["id"] == "t41-b")
    assert t41_b["apps"]["operator"] == {
        "app_id": "77777777-7777-7777-7777-777777777777",
        "version_no": 12,
        "state": "disabled",
        "source": "runtime",
    }

    # 95 最新 preflight 版本 1.40.4 覆盖旧 install 的 1.40.2
    env95 = next(e for e in ov["environments"] if e["id"] == "95")
    assert env95["nodes"][0]["version"] == "1.40.4"
    assert env95["nodes"][0]["installation"] == "installed"

    # 96 远端授权计数=1，且单测 offline 后 consumer/order 仍保留 passed
    env96 = next(e for e in ov["environments"] if e["id"] == "96")
    t96_live = next(t for t in env96["tenants"] if t["id"] == "t96-live")
    assert t96_live["knowledge"]["active_grants"] == 1
    assert t96_live["capabilities"]["consumer"]["state"] == "passed"
    assert t96_live["capabilities"]["order"]["state"] == "passed"
    assert t96_live["capabilities"]["offline"]["state"] == "blocked"
    assert t96_live["capabilities"]["offline"]["error_code"] == "OFFLINE_TIMEOUT"

    # 6 条共享 shared_plan（含 identity）的任务，_current 仅校验 1 次（非 6 次重复调用）
    assert sum(1 for pid, _ in current_calls if pid == shared_plan["id"]) == 1

    # 4) 1600 租户大目录：drafts 与 overview 均严守桥 1 MiB 上限，不锁死草稿编辑！
    big_reg = tmp_path / "big" / "directory.json"
    big_reg.parent.mkdir(mode=0o700)
    registry.apply(
        big_reg,
        [
            {
                "id": "41",
                "name": "大目录环境",
                "enabled": True,
                "gateway_url": "https://big.example.test",
                "cpms_base_url": "https://big.example.test/cpms",
                "company_gateway_url": "https://big.example.test",
                "config_ref": str(cfg_41),
                "tenants": [{"id": f"t-{i}", "name": f"Company {i}", "enabled": True} for i in range(1600)],
            }
        ],
        expected_revision=0,
        actor="operator",
        managers=[
            {"environment": "41", "tenant_id": "platform", "user_id": "root-admin", "environments": ["*"]}
        ],
    )
    big_ctx = SimpleNamespace(
        settings=SimpleNamespace(
            environment="41",
            execution_environment="41",
            database_file=gw_db,
            environment_management_registry_file=str(big_reg),
        )
    )
    drafts_big = management.drafts(big_ctx, "live-session")
    assert len(drafts_big["effective"][0]["tenants"]) == 1600
    assert (
        len(json.dumps(drafts_big, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) < 1024 * 1024
    )

    ov_big = management.overview(big_ctx, "live-session")
    assert len(json.dumps(ov_big, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) < 1024 * 1024
    assert ov_big["environments"][0]["tenant_count"] == 1600
    assert ov_big["environments"][0]["tenants_truncated"] is True


def test_console_bridge_forwards_environment_overview_and_rejects_extra_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forwarded: list[tuple[str, str, dict | None]] = []

    def fake_request(url, *, method="GET", headers=None, body=None, timeout=15):
        if url.endswith("/console/api/workspaces/current/summary"):
            return 200, {"role": "admin"}
        forwarded.append((method, url, body))
        return 200, {"schema": "aiops-environment-overview/1", "environments": []}

    monkeypatch.setattr(bridge, "_request", fake_request)
    server = bridge.make_server(
        bridge.BridgeSettings(
            "127.0.0.1",
            0,
            "http://dify.local",
            "http://gw.local",
            "pub-key",
            "https://console.example.test",
            "ws-1",
        )
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        headers = {
            "Origin": "https://console.example.test",
            "Referer": "https://console.example.test/aiops/environments",
            "Sec-Fetch-Site": "same-origin",
            "Content-Type": "application/json",
            "Cookie": "access_token=tok; csrf_token=csrf1",
            "X-CSRF-Token": "csrf1",
        }
        with httpx.Client() as client:
            ok = client.post(
                f"{base}/__aiops/manage", headers=headers, json={"action": "environment-overview"}
            )
            assert ok.status_code == 200
            assert forwarded[-1] == ("GET", "http://gw.local/v1/dify/management/environment-overview", None)

            count = len(forwarded)
            assert (
                client.post(
                    f"{base}/__aiops/manage",
                    headers=headers,
                    json={"action": "environment-overview", "environment": "41"},
                ).status_code
                == 400
            )
            assert (
                client.post(
                    f"{base}/__aiops/manage",
                    headers={**headers, "Origin": "https://evil.example.test"},
                    json={"action": "environment-overview"},
                ).status_code
                == 403
            )
            assert len(forwarded) == count
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
