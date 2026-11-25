"""受控执行环境迁移工件（ADR-0333）：冻结清单、消费者新版本、管家会话绑定与逐项 CAS 恢复。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from aiops_diagnostics.admin_cli import admin_app
from aiops_diagnostics.consumer.knowledge_access import KnowledgeGrants
from aiops_diagnostics.conversation.conversation_store import ConversationStore
from aiops_diagnostics.dify.self_service_store import SelfServiceStore
from aiops_diagnostics.execution import chat_execution
from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore
from aiops_diagnostics.gateway import gateway_schema
from aiops_diagnostics.gateway.gateway_api import StandardAPIError, _operator_conversation
from aiops_diagnostics.onboarding import execution_adoption as adoption
from aiops_diagnostics.publication.agent_lifecycle import AgentConfig, AgentStore


def _seed_legacy_gateway_db(tmp_path: Path) -> dict:
    db_dir = tmp_path / "state"
    db_dir.mkdir(mode=0o700)
    db = db_dir / "gateway.db"
    gateway_schema.ensure(db)
    db.chmod(0o600)

    agents = AgentStore(db)
    routes = ChatRouteBindingStore(db)
    self_service = SelfServiceStore(db)
    grants = KnowledgeGrants(db)
    conversations = ConversationStore(db)

    consumer_app = "394b5399-7ad9-428e-b2b0-cc7f05720a81"
    operator_app = "137cc417-f068-43c2-9d4a-43ebd254ea01"
    retired_operator_app = "a57602c1-6a8d-57cd-8ea0-6cd7fd9031bf"
    orphan_app = "349a637c-8f86-41b9-b932-e9818fb39a97"
    tenant_a = "1942105476598861824"
    tenant_b = "1941847786056323072"

    key_file = db_dir / "consumer.key"
    key_file.write_text("app-test-key-0123456789\n", encoding="utf-8")
    key_file.chmod(0o600)

    node_config = db_dir / "agent-runs.json"

    def write_node_config(env_name: str) -> None:
        node_config.write_text(
            json.dumps(
                {
                    "environment": env_name,
                    "snail_base_url": "https://snail.example.test",
                    "snail_app_id": "app-1",
                    "snail_app_token": "tok-1",
                    "snail_agent_id": 1,
                    "snail_open_id": "open-1",
                    "expected_node_id": "node-41",
                    "nodes": [
                        {
                            "node_id": "node-41",
                            "environment": env_name,
                            "token_sha256": "a" * 64,
                            "active": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        node_config.chmod(0o600)

    write_node_config("production-41")

    # 1) consumer_service Agent（快照冻结 production-41）
    c_cfg = AgentConfig(
        agent_type="consumer",
        prompt="consumer prompt",
        knowledge_base_ids=("kb-active-1",),
        model="gpt",
        output_contract="markdown",
    )
    c_agent = agents.create(tenant_a, "ulink-consumer", "desc", c_cfg, created_by="seed")
    c_snapshot = {
        "app_id": consumer_app,
        "workflow_id": "aa550684-5a55-4794-b487-ed2814cce544",
        "model": {"provider": "openai", "name": "gpt"},
        "workflow": {"nodes": ["a"]},
        "consumer_service": {
            "callback_version": 5,
            "photo_enabled": True,
            "live_order_status": True,
            "live_billing_record": True,
            "knowledge_base_ids": ["kb-active-1"],
            "content": {
                "schema": "aiops-consumer-documents/1",
                "tenant_id": tenant_a,
                "environment": "production-41",
                "state": "published",
            },
        },
    }
    c_ver = agents.publish(c_agent.agent_id, tenant_a, c_agent.revision, c_snapshot, "seed")
    routes.publish(
        environment="production-41",
        tenant_id=tenant_a,
        entry="consumer",
        scenario=chat_execution.SCENARIO_CONSUMER_SERVICE,
        engine="dify",
        agent_id=c_agent.agent_id,
        agent_version_no=c_ver.version_no,
        app_id=consumer_app,
        workflow_id="aa550684-5a55-4794-b487-ed2814cce544",
        workflow_key_ref=str(key_file),
        updated_by="seed",
    )

    # 2) operator node Agent（含真实 operator-agent/1 清单）
    o_cfg = AgentConfig(
        agent_type="operator",
        prompt="operator prompt",
        knowledge_base_ids=(),
        model="gpt",
        output_contract="markdown",
    )
    o_agent = agents.create(tenant_a, "ulink-operator", "desc", o_cfg, created_by="seed")
    node_manifest = json.dumps(
        {
            "schema": "operator-agent/1",
            "name": "ulink-operator",
            "history_policy": "gateway-conversation/1",
            "model": {
                "model_key": "m1",
                "provider_key": "p1",
                "adapter_key": "a1",
                "endpoint": "https://model.example.test",
            },
            "model_options": {},
            "system_messages": ["You are operator assistant."],
            "tools": [
                {
                    "name": "cpms_order_detail",
                    "description": "read order",
                    "input_schema": '{"type":"object"}',
                }
            ],
        }
    )
    o_ver = agents.publish(
        o_agent.agent_id,
        tenant_a,
        o_agent.revision,
        {"app_id": operator_app, "node_manifest": node_manifest},
        "seed",
    )
    routes.publish(
        environment="production-41",
        tenant_id=tenant_a,
        entry="operator",
        scenario="operator_order_diagnosis",
        engine="node",
        agent_id=o_agent.agent_id,
        agent_version_no=o_ver.version_no,
        app_id=operator_app,
        node_config_file=str(node_config),
        updated_by="seed",
    )

    # 3) operator_apps：含 production-41 与空串孤儿行
    self_service.register(
        app_id=operator_app,
        tenant_id=tenant_a,
        workspace_id="ws-a",
        agent_name="ulink-operator",
        profile_id="operator-41",
        actor="seed",
        entry="operator",
        environment="production-41",
    )
    self_service.register(
        app_id=consumer_app,
        tenant_id=tenant_a,
        workspace_id="ws-a",
        agent_name="ulink-consumer",
        profile_id="",
        actor="seed",
        entry="consumer",
        scenario=chat_execution.SCENARIO_CONSUMER_SERVICE,
        environment="production-41",
    )
    with sqlite3.connect(db) as con:
        con.execute(
            """
            INSERT INTO operator_apps
                (app_id, tenant_id, workspace_id, agent_name, profile_id, created_by,
                 revision, entry, scenario, environment, targets_json)
            VALUES (?, ?, 'ws-b', 'orphan-op', 'operator-41', 'seed', 1, 'operator',
                    'operator_order_diagnosis', '', '{}')
            """,
            (orphan_app, tenant_b),
        )

    # 4) consumer_knowledge_grants：1 条有效 + 1 条已撤销
    grants.grant(tenant_a, "production-41", "kb-active-1", "admin")
    grants.grant(tenant_a, "production-41", "kb-revoked-1", "admin")
    assert grants.revoke(tenant_a, "production-41", "kb-revoked-1") is True

    # 5) 管家会话：1 条现役 formal（必迁）、1 条 preview（不动）、1 条历史退役 app（不动）
    formal_conv = conversations.create(
        scope_fingerprint="fp-ops-a",
        business_entry="operator",
        agent_version_key=f"{o_agent.agent_id}#v{o_ver.version_no}",
        operator_binding=f"operator_order_diagnosis:production-41:{tenant_a}:{operator_app}:formal",
    )
    preview_conv = conversations.create(
        scope_fingerprint="fp-ops-a",
        business_entry="operator",
        agent_version_key="agt_1111111111111111#v1",
        operator_binding=f"operator-trial:{operator_app}:{operator_app}:preview",
    )
    retired_conv = conversations.create(
        scope_fingerprint="fp-ops-b",
        business_entry="operator",
        agent_version_key="agt_2222222222222222#v1",
        operator_binding=f"operator_order_diagnosis:production-41:{tenant_a}:{retired_operator_app}:formal",
    )

    return {
        "db": db,
        "write_node_config": write_node_config,
        "tenant_a": tenant_a,
        "tenant_b": tenant_b,
        "consumer_agent_id": c_agent.agent_id,
        "operator_agent_id": o_agent.agent_id,
        "consumer_app": consumer_app,
        "operator_app": operator_app,
        "orphan_app": orphan_app,
        "formal_conv": formal_conv["conversation_id"],
        "preview_conv": preview_conv["conversation_id"],
        "retired_conv": retired_conv["conversation_id"],
    }


def test_adopt_dry_run_apply_runtime_load_followup_and_cas_restore(tmp_path):
    """ADR-0333：默认只读预演→单事务迁移→真实 _bindings 与老管家会话追问通过→无漂移 CAS 恢复。"""
    seed = _seed_legacy_gateway_db(tmp_path)
    db = seed["db"]
    cfg = tmp_path / "state" / "node-targets.json"
    cfg.write_text(json.dumps({"targets": [{"environment": "production-41"}]}), encoding="utf-8")
    cfg.chmod(0o600)

    # 1) 默认只读预演不写库，输出冻结清单且含孤儿 app 与跳过会话统计。
    before_bytes = db.read_bytes()
    dry = adoption.adopt(db, "production-41", "41", config_files=(cfg,))
    assert dry["mode"] == "dry_run" and dry["changed"] is False
    assert len(dry["routes"]) == 2
    assert dry["consumer_versions"][0]["from_version_no"] == 1
    assert dry["consumer_versions"][0]["to_version_no"] == 2
    assert dry["grants"] == {"total": 2, "active": 1, "revoked": 1}
    assert dry["operator_conversations_count"] == 1
    assert dry["skipped_operator_conversations"] == {"preview": 1, "retired_or_other_app": 1}
    assert [a["app_id"] for a in dry["orphan_operator_apps"]] == [seed["orphan_app"]]
    assert dry["config_refs"] == [{"name": "state/node-targets.json", "source_refs": 1, "target_refs": 0}]
    assert db.read_bytes() == before_bytes

    # 2) 切配套 node 配置至 41 并带独占私有备份执行单事务迁移 + 真实路由加载校验。
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    backup = backup_dir / "adopt-41.json"
    seed["write_node_config"]("41")
    result = adoption.adopt(db, "production-41", "41", actor="ops-1", backup=backup, verify_bindings=True)
    assert result["mode"] == "migrated" and result["changed"] is True
    assert backup.stat().st_mode & 0o077 == 0

    # 3) 真实 _bindings_from_store 只加载新环境活跃路由，旧 production-41 行保留为 retracted 供 by_key 溯源。
    bindings = chat_execution._bindings_from_store(str(db))
    by_key = {b.key: b for b in bindings}
    assert set(by_key) == {
        f"consumer_service:41:{seed['tenant_a']}",
        f"operator_order_diagnosis:41:{seed['tenant_a']}",
    }
    route_store = ChatRouteBindingStore(db)
    old_c_row = route_store.by_key(f"consumer_service:production-41:{seed['tenant_a']}")
    assert old_c_row is not None and old_c_row.state == "retracted" and old_c_row.revision == 2
    new_consumer = by_key[f"consumer_service:41:{seed['tenant_a']}"]
    assert new_consumer.agent_version_no == 2
    assert new_consumer.content_snapshot["environment"] == "41"
    old_snap = AgentStore(db).version(seed["consumer_agent_id"], seed["tenant_a"], 1).snapshot
    assert old_snap["consumer_service"]["content"]["environment"] == "production-41"

    # 4) grants 保留撤销态，不复活已撤销库；孤儿 operator_apps 保持空串。
    grants = KnowledgeGrants(db)
    assert grants.granted(seed["tenant_a"], "41") == {"kb-active-1"}
    assert grants.generation(seed["tenant_a"], "41", "kb-active-1") == grants.generation(
        seed["tenant_a"], "production-41", "kb-active-1"
    )
    assert grants.generation(seed["tenant_a"], "41", "kb-revoked-1") is None
    assert SelfServiceStore(db).app(seed["orphan_app"])["environment"] == ""
    assert SelfServiceStore(db).app(seed["operator_app"])["environment"] == "41"

    # 5) 现役管家正式会话可被新 '41' 绑定追问；预览、历史退役 app 与错租户/错 app 被拒 404。
    conv_store = ConversationStore(db)
    new_op_binding = by_key[f"operator_order_diagnosis:41:{seed['tenant_a']}"]
    caller = SimpleNamespace(scope_fingerprint="fp-ops-a")
    ctx = SimpleNamespace(conversation_store=conv_store)
    formal = conv_store.get(seed["formal_conv"], "fp-ops-a")
    assert (
        _operator_conversation(ctx, caller, new_op_binding, formal, trial=False)["conversation_id"]
        == seed["formal_conv"]
    )
    preview = conv_store.get(seed["preview_conv"], "fp-ops-a")
    assert preview["operator_binding"].startswith("operator-trial:")
    retired = conv_store.get(seed["retired_conv"], "fp-ops-b")
    assert ":production-41:" in retired["operator_binding"]
    with pytest.raises(StandardAPIError) as exc_info:
        _operator_conversation(ctx, caller, new_op_binding, retired, trial=False)
    assert exc_info.value.status_code == 404

    # 6) 恢复配套 node 配置至 production-41 并执行无漂移 CAS 恢复 + 真实路由加载校验。
    seed["write_node_config"]("production-41")
    restored = adoption.adopt(
        db, "production-41", "41", actor="ops-1", backup=backup, restore=True, verify_bindings=True
    )
    assert restored["mode"] == "restored" and restored["changed"] is True
    assert (
        route_store.get("41", seed["tenant_a"], "consumer", chat_execution.SCENARIO_CONSUMER_SERVICE).state
        == "retracted"
    )
    assert (
        route_store.get("41", seed["tenant_a"], "consumer", chat_execution.SCENARIO_CONSUMER_SERVICE).revision
        == 2
    )
    restored_old = route_store.get(
        "production-41", seed["tenant_a"], "consumer", chat_execution.SCENARIO_CONSUMER_SERVICE
    )
    assert restored_old.active is True and restored_old.revision == 3
    assert SelfServiceStore(db).app(seed["operator_app"])["environment"] == "production-41"
    assert conv_store.get(seed["formal_conv"], "fp-ops-a")["operator_binding"] == (
        f"operator_order_diagnosis:production-41:{seed['tenant_a']}:{seed['operator_app']}:formal"
    )
    assert grants.granted(seed["tenant_a"], "41") == set()
    assert grants.granted(seed["tenant_a"], "production-41") == {"kb-active-1"}
    assert AgentStore(db).get(seed["consumer_agent_id"], seed["tenant_a"]).published_version == 1
    assert AgentStore(db).version(seed["consumer_agent_id"], seed["tenant_a"], 2).version_no == 2


@pytest.mark.parametrize("hazard", ["inflight_run", "generating_conv", "target_route", "existing_backup"])
def test_adopt_refuses_inflight_conflicts_and_existing_backup(tmp_path, hazard):
    seed = _seed_legacy_gateway_db(tmp_path)
    db = seed["db"]
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    backup = backup_dir / "adopt-41.json"

    if hazard == "inflight_run":
        with sqlite3.connect(db) as con:
            con.execute(
                """
                INSERT INTO agent_runs
                    (run_id, scope_fingerprint, scope_json, order_no, operation, environment,
                     app_id, agent_id, expected_node_id, state, gateway_version, created_at, expires_at)
                VALUES ('run_1', 'fp', '{}', 'ORD1', 'diag', 'production-41', 'app', 'agt', 'n1',
                        'running', '1.0', '2026-11-25T00:00:00Z', '2099-01-01T00:00:00Z')
                """
            )
    elif hazard == "generating_conv":
        with sqlite3.connect(db) as con:
            con.execute(
                "UPDATE conversations SET generating_since = '2026-11-25T00:00:00Z'"
                " WHERE conversation_id = ?",
                (seed["formal_conv"],),
            )
    elif hazard == "target_route":
        ChatRouteBindingStore(db).publish(
            environment="41",
            tenant_id=seed["tenant_a"],
            entry="operator",
            scenario="operator_order_diagnosis",
            engine="node",
            agent_id=seed["operator_agent_id"],
            agent_version_no=1,
            app_id=seed["operator_app"],
            updated_by="other",
        )
    elif hazard == "existing_backup":
        backup.write_text("existing\n", encoding="utf-8")
        backup.chmod(0o600)

    with pytest.raises(adoption.AdoptionError):
        adoption.adopt(db, "production-41", "41", backup=backup)


@pytest.mark.parametrize("drift", ["grant_revoked", "conversation_turn", "route_published"])
def test_restore_refuses_post_cutover_drift_without_overwriting(tmp_path, drift):
    seed = _seed_legacy_gateway_db(tmp_path)
    db = seed["db"]
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    backup = backup_dir / "adopt-41.json"
    adoption.adopt(db, "production-41", "41", actor="ops-1", backup=backup)

    if drift == "grant_revoked":
        assert KnowledgeGrants(db).revoke(seed["tenant_a"], "41", "kb-active-1") is True
    elif drift == "conversation_turn":
        with sqlite3.connect(db) as con:
            con.execute(
                "UPDATE conversations SET last_turn_no = last_turn_no + 1 WHERE conversation_id = ?",
                (seed["formal_conv"],),
            )
    elif drift == "route_published":
        ChatRouteBindingStore(db).retract(
            environment="41",
            tenant_id=seed["tenant_a"],
            entry="operator",
            scenario="operator_order_diagnosis",
            expected_revision=1,
            updated_by="ops-2",
        )

    with pytest.raises(adoption.AdoptionError, match="ADOPTION_RESTORE_DRIFT"):
        adoption.adopt(db, "production-41", "41", actor="ops-1", backup=backup, restore=True)
    # 拒绝后保持现役状态不变，不复活已撤销授权、不覆盖新会话轮次。
    assert SelfServiceStore(db).app(seed["operator_app"])["environment"] == "41"
    if drift == "grant_revoked":
        assert KnowledgeGrants(db).granted(seed["tenant_a"], "41") == set()


def test_admin_cli_environment_adopt_execution_dry_run_and_refusal(tmp_path):
    seed = _seed_legacy_gateway_db(tmp_path)
    runner = CliRunner()
    ok = runner.invoke(
        admin_app,
        [
            "environment-adopt-execution",
            "--database",
            str(seed["db"]),
            "--from",
            "production-41",
            "--to",
            "41",
        ],
    )
    assert ok.exit_code == 0, ok.output
    body = json.loads(ok.output)
    assert body["mode"] == "dry_run" and body["changed"] is False
    assert "workflow_key_ref" not in ok.output

    bad = runner.invoke(
        admin_app,
        [
            "environment-adopt-execution",
            "--database",
            str(seed["db"]),
            "--from",
            "41",
            "--to",
            "41",
        ],
    )
    assert bad.exit_code == 1
    assert json.loads(bad.output)["reason"] == "ADOPTION_ENVIRONMENT_SAME"


def test_verify_bindings_failure_is_atomic_before_backup_and_allows_retry(tmp_path):
    """回归 F1/F2/F6：verify_bindings 失败必须在提交与落盘备份之前整体回滚，且修复后可原路径重试。"""
    seed = _seed_legacy_gateway_db(tmp_path)
    db = seed["db"]
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    backup = backup_dir / "adopt-41.json"

    # agent-runs.json 仍指向 production-41：预演与迁移的 --verify-bindings 均应提交前拒绝。
    with pytest.raises(adoption.AdoptionError, match="ADOPTION_BINDING_VERIFICATION_FAILED"):
        adoption.adopt(db, "production-41", "41", verify_bindings=True)

    runner = CliRunner()
    failed = runner.invoke(
        admin_app,
        [
            "environment-adopt-execution",
            "--database",
            str(db),
            "--from",
            "production-41",
            "--to",
            "41",
            "--backup",
            str(backup),
            "--verify-bindings",
        ],
    )
    assert failed.exit_code == 1
    assert json.loads(failed.output)["reason"] == "ADOPTION_BINDING_VERIFICATION_FAILED"
    assert not backup.exists()

    route_store = ChatRouteBindingStore(db)
    old_row = route_store.get("production-41", seed["tenant_a"], "operator", "operator_order_diagnosis")
    assert old_row is not None and old_row.active is True and old_row.revision == 1
    assert route_store.get("41", seed["tenant_a"], "operator", "operator_order_diagnosis") is None

    # 失败后只读预演仍可用；修好 agent-runs.json 后用同一 backup 路径重试即可成功。
    dry = adoption.adopt(db, "production-41", "41")
    assert dry["mode"] == "dry_run" and len(dry["routes"]) == 2
    seed["write_node_config"]("41")
    ok = adoption.adopt(db, "production-41", "41", actor="ops-1", backup=backup, verify_bindings=True)
    assert ok["mode"] == "migrated" and backup.exists()

    # 恢复时若未先切回 agent-runs.json，同样在提交前拒绝且不修改现役 41 状态。
    with pytest.raises(adoption.AdoptionError, match="ADOPTION_BINDING_VERIFICATION_FAILED"):
        adoption.adopt(
            db, "production-41", "41", actor="ops-1", backup=backup, restore=True, verify_bindings=True
        )
    assert SelfServiceStore(db).app(seed["operator_app"])["environment"] == "41"
    cur_41 = route_store.get("41", seed["tenant_a"], "operator", "operator_order_diagnosis")
    assert cur_41 is not None and cur_41.active is True and cur_41.revision == 1


def test_underscore_environments_use_exact_prefix_not_like_wildcard(tmp_path):
    """回归 F3：含下划线的环境名不得触发 SQLite LIKE 通配符误伤或误报漂移。"""
    seed = _seed_legacy_gateway_db(tmp_path)
    db = seed["db"]
    convs = ConversationStore(db)
    # 构造 prod_41 源环境迁移到 prod_42，并同时注入 prodX41 / prodX42 干扰会话。
    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE chat_route_bindings SET environment = 'prod_41' WHERE environment = 'production-41'"
        )
        con.execute("UPDATE operator_apps SET environment = 'prod_41' WHERE environment = 'production-41'")
        con.execute(
            "UPDATE consumer_knowledge_grants SET environment = 'prod_41' WHERE environment = 'production-41'"
        )
        con.execute(
            "UPDATE conversations"
            " SET operator_binding = replace(operator_binding, ':production-41:', ':prod_41:')"
        )
        row = con.execute(
            "SELECT snapshot_json FROM agent_versions WHERE agent_id = ? AND version_no = 1",
            (seed["consumer_agent_id"],),
        ).fetchone()
        snap = json.loads(row[0])
        snap["consumer_service"]["content"]["environment"] = "prod_41"
        con.execute(
            "UPDATE agent_versions SET snapshot_json = ? WHERE agent_id = ? AND version_no = 1",
            (json.dumps(snap), seed["consumer_agent_id"]),
        )
    convs.create(
        scope_fingerprint="fp-other-1",
        business_entry="operator",
        agent_version_key="agt_3333333333333333#v1",
        operator_binding=f"operator_order_diagnosis:prodX41:{seed['tenant_a']}:other-app:formal",
    )
    convs.create(
        scope_fingerprint="fp-other-2",
        business_entry="operator",
        agent_version_key="agt_4444444444444444#v1",
        operator_binding=f"operator_order_diagnosis:prodX42:{seed['tenant_a']}:other-app:formal",
    )

    dry = adoption.adopt(db, "prod_41", "prod_42")
    assert dry["operator_conversations_count"] == 1
    assert dry["skipped_operator_conversations"] == {"preview": 1, "retired_or_other_app": 1}

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    backup = backup_dir / "adopt-underscore.json"
    seed["write_node_config"]("prod_42")
    migrated = adoption.adopt(db, "prod_41", "prod_42", actor="ops-1", backup=backup, verify_bindings=True)
    assert migrated["mode"] == "migrated"

    seed["write_node_config"]("prod_41")
    restored = adoption.adopt(
        db, "prod_41", "prod_42", actor="ops-1", backup=backup, restore=True, verify_bindings=True
    )
    assert restored["mode"] == "restored"


def test_planner_rejects_loader_invalid_snapshot_duplicate_agent_and_world_writable_dir(tmp_path):
    """回归 F4/F5 及父目录权限：快照缺项、同 Agent 双路由、0777 库父目录均在预检即拒且不落盘备份。"""
    seed = _seed_legacy_gateway_db(tmp_path)
    db = seed["db"]
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)

    # 1) F4：删除 knowledge_base_ids，默认预演与不带 --verify-bindings 的迁移都必须拒绝。
    with sqlite3.connect(db) as con:
        orig_json = con.execute(
            "SELECT snapshot_json FROM agent_versions WHERE agent_id = ? AND version_no = 1",
            (seed["consumer_agent_id"],),
        ).fetchone()[0]
        broken = json.loads(orig_json)
        del broken["consumer_service"]["knowledge_base_ids"]
        con.execute(
            "UPDATE agent_versions SET snapshot_json = ? WHERE agent_id = ? AND version_no = 1",
            (json.dumps(broken), seed["consumer_agent_id"]),
        )
    backup_f4 = backup_dir / "f4.json"
    with pytest.raises(adoption.AdoptionError, match="ADOPTION_CONSUMER_SNAPSHOT_INVALID"):
        adoption.adopt(db, "production-41", "41")
    with pytest.raises(adoption.AdoptionError, match="ADOPTION_CONSUMER_SNAPSHOT_INVALID"):
        adoption.adopt(db, "production-41", "41", backup=backup_f4)
    assert not backup_f4.exists()

    # 恢复有效快照后测试 F5：同一 consumer agent_id 被两条活跃路由引用。
    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE agent_versions SET snapshot_json = ? WHERE agent_id = ? AND version_no = 1",
            (orig_json, seed["consumer_agent_id"]),
        )
        con.execute(
            """
            INSERT INTO chat_route_bindings
                (environment, tenant_id, entry, scenario, engine, agent_id, agent_version_no,
                 app_id, workflow_id, workflow_key_ref, state, revision, updated_by, updated_at,
                 node_config_file)
            SELECT environment, tenant_id, 'alt-entry', scenario, engine, agent_id, agent_version_no,
                   app_id, workflow_id, workflow_key_ref, 'active', 1, 'seed', updated_at, node_config_file
              FROM chat_route_bindings
             WHERE environment = 'production-41' AND scenario = ?
            """,
            (chat_execution.SCENARIO_CONSUMER_SERVICE,),
        )
    backup_f5 = backup_dir / "f5.json"
    with pytest.raises(adoption.AdoptionError, match="ADOPTION_DUPLICATE_CONSUMER_AGENT"):
        adoption.adopt(db, "production-41", "41")
    with pytest.raises(adoption.AdoptionError, match="ADOPTION_DUPLICATE_CONSUMER_AGENT"):
        adoption.adopt(db, "production-41", "41", backup=backup_f5)
    assert not backup_f5.exists()

    # 3) 数据库父目录为 0777 时拒绝。
    db.parent.chmod(0o777)
    try:
        with pytest.raises(adoption.AdoptionError, match="ADOPTION_PATH_UNSAFE"):
            adoption.adopt(db, "production-41", "41")
    finally:
        db.parent.chmod(0o700)


def test_backup_fsync_failure_cleans_orphan_and_restore_rejects_tampered_manifest(tmp_path, monkeypatch):
    """回归 NEW-1..NEW-4：目录 fsync 失败须清理孤儿备份且可重试；缺段/篡改清单恢复须稳定拒绝零回溯。"""
    seed = _seed_legacy_gateway_db(tmp_path)
    db = seed["db"]
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    backup = backup_dir / "adopt-41.json"
    seed["write_node_config"]("41")

    # 1) NEW-1：模拟目录 fsync（第 2 次 os.fsync）失败，确认 DB 回滚且不留孤儿备份，原路径可重试。
    real_fsync = adoption.os.fsync
    calls = {"n": 0}

    def flaky_fsync(fd: int) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated dir fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(adoption.os, "fsync", flaky_fsync)
    with pytest.raises(OSError, match="simulated dir fsync failure"):
        adoption.adopt(db, "production-41", "41", actor="ops-1", backup=backup, verify_bindings=True)
    monkeypatch.setattr(adoption.os, "fsync", real_fsync)
    assert not backup.exists()
    assert (
        ChatRouteBindingStore(db).get("41", seed["tenant_a"], "operator", "operator_order_diagnosis") is None
    )

    ok = adoption.adopt(db, "production-41", "41", actor="ops-1", backup=backup, verify_bindings=True)
    assert ok["mode"] == "migrated" and backup.exists()

    # 2) NEW-2 / NEW-3 / NEW-4：篡改备份清单各段均由 CLI 稳定返回 ADOPTION_BACKUP_INVALID。
    orig = json.loads(backup.read_text(encoding="utf-8"))
    seed["write_node_config"]("production-41")
    runner = CliRunner()
    bad_from_binding = (
        f"operator_order_diagnosis:production-37:{seed['tenant_a']}:{seed['operator_app']}:formal"
    )
    tampered_cases = [
        {**orig, "agents": []},  # NEW-2: 删除 agents 段
        {**orig, "agents": [{**orig["agents"][0], "from_version_no": 424242}]},  # NEW-2: 伪造版本
        {
            **orig,
            "operator_conversations": [
                {
                    **orig["operator_conversations"][0],
                    "from_binding": bad_from_binding,
                }
            ],
        },  # NEW-3: 篡改 from_binding
        {k: v for k, v in orig.items() if k != "config_refs"},  # NEW-4: 缺失顶层段
        {**orig, "routes": "not-a-list"},  # NEW-4: 错误类型
        {**orig, "grants": [{**orig["grants"][0], "granted_at": "bad"}]},  # NEW-4: 非数值时间戳
    ]
    for idx, payload in enumerate(tampered_cases):
        bad_file = backup_dir / f"tampered-{idx}.json"
        bad_file.write_text(json.dumps(payload), encoding="utf-8")
        bad_file.chmod(0o600)
        res = runner.invoke(
            admin_app,
            [
                "environment-adopt-execution",
                "--database",
                str(db),
                "--from",
                "production-41",
                "--to",
                "41",
                "--backup",
                str(bad_file),
                "--restore",
                "--verify-bindings",
            ],
        )
        assert res.exit_code == 1, res.output
        assert "Traceback" not in res.output
        assert json.loads(res.output)["reason"] == "ADOPTION_BACKUP_INVALID"

    # 全部拒绝后，库中现役 41 状态一毫未动。
    assert SelfServiceStore(db).app(seed["operator_app"])["environment"] == "41"
    assert AgentStore(db).get(seed["consumer_agent_id"], seed["tenant_a"]).published_version == 2


def test_expired_unclaimed_run_does_not_block_and_unexpired_issued_blocks(tmp_path):
    """回归 Devin #3：已过期未领取 agent_runs 不阻塞预演/迁移，未过期 issued 行仍拒绝。"""
    seed = _seed_legacy_gateway_db(tmp_path)
    db = seed["db"]
    with sqlite3.connect(db) as con:
        con.execute(
            """
            INSERT INTO agent_runs
                (run_id, scope_fingerprint, scope_json, order_no, operation, environment,
                 app_id, agent_id, expected_node_id, state, gateway_version, created_at, expires_at)
            VALUES ('run_expired', 'fp', '{}', 'ORD1', 'diag', 'production-41', 'app', 'agt', 'n1',
                    'issued', '1.0', '2020-01-01T00:00:00Z', '2020-01-01T00:05:00Z')
            """
        )
    before = db.read_bytes()
    dry = adoption.adopt(db, "production-41", "41")
    assert dry["mode"] == "dry_run" and db.read_bytes() == before

    with sqlite3.connect(db) as con:
        con.execute(
            """
            INSERT INTO agent_runs
                (run_id, scope_fingerprint, scope_json, order_no, operation, environment,
                 app_id, agent_id, expected_node_id, state, gateway_version, created_at, expires_at)
            VALUES ('run_live', 'fp', '{}', 'ORD2', 'diag', 'production-41', 'app', 'agt', 'n1',
                    'issued', '1.0', '2026-11-25T00:00:00Z', '2099-01-01T00:00:00Z')
            """
        )
    with pytest.raises(adoption.AdoptionError, match="ADOPTION_INFLIGHT_WORK"):
        adoption.adopt(db, "production-41", "41")


def test_restored_older_consumer_version_migrates_and_post_cutover_old_env_restore_is_refused(tmp_path):
    """回归 Devin #1/#2：回退至旧版的现役消费者路由可迁移与恢复；迁移后禁止把路由回指旧环境快照。"""
    from aiops_diagnostics.dify.dify_app_registry import DifyAppBinding
    from aiops_diagnostics.dify.dify_dsl_pull import SyncOutcome
    from aiops_diagnostics.gateway import gateway_api

    seed = _seed_legacy_gateway_db(tmp_path)
    db = seed["db"]
    agents = AgentStore(db)
    v1_snap = agents.version(seed["consumer_agent_id"], seed["tenant_a"], 1).snapshot
    # 模拟曾发布 v2（agents.published_version=2），但线上路由回退至 v1（route.agent_version_no=1）。
    v2_snap = json.loads(json.dumps(v1_snap))
    v2_snap["workflow"] = {"nodes": ["v2"]}
    c_agent = agents.get(seed["consumer_agent_id"], seed["tenant_a"])
    draft = agents.fork_draft(c_agent.agent_id, seed["tenant_a"], c_agent.revision)
    agents.publish(c_agent.agent_id, seed["tenant_a"], draft.revision, v2_snap, "seed")
    assert agents.get(seed["consumer_agent_id"], seed["tenant_a"]).published_version == 2
    assert (
        ChatRouteBindingStore(db)
        .get("production-41", seed["tenant_a"], "consumer", chat_execution.SCENARIO_CONSUMER_SERVICE)
        .agent_version_no
        == 1
    )

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    backup = backup_dir / "adopt-restored.json"
    seed["write_node_config"]("41")
    res = adoption.adopt(db, "production-41", "41", actor="ops-1", backup=backup, verify_bindings=True)
    assert res["mode"] == "migrated"
    assert res["consumer_versions"][0]["from_version_no"] == 1
    assert res["consumer_versions"][0]["from_published_version"] == 2
    assert res["consumer_versions"][0]["to_version_no"] == 3
    v3_snap = agents.version(seed["consumer_agent_id"], seed["tenant_a"], 3).snapshot
    assert v3_snap["workflow"] == {"nodes": ["a"]}
    assert v3_snap["consumer_service"]["content"]["environment"] == "41"

    # Devin #1：迁移后尝试把 41 路由回指旧环境 v1 快照，写指针前被拒且路由表仍可加载。
    fake_ctx = SimpleNamespace(
        settings=SimpleNamespace(
            chat_route_bindings_file=str(db),
            execution_environment="41",
            chat_dify_key_dir=str(db.parent),
            dify_chat_base_url="https://dify.example.test",
            chat_execution_bindings_file="",
        ),
        agent_manager=SimpleNamespace(store=agents),
    )
    outcome = SyncOutcome(
        seed["tenant_a"],
        "consumer",
        seed["consumer_app"],
        "ulink-consumer",
        "synced",
        1,
        workflow_id="aa550684-5a55-4794-b487-ed2814cce544",
        agent_id=seed["consumer_agent_id"],
        route_scenario=chat_execution.SCENARIO_CONSUMER_SERVICE,
    )
    with pytest.raises(StandardAPIError) as exc_info:
        gateway_api._advance_route_binding(
            fake_ctx,
            outcome,
            DifyAppBinding(seed["tenant_a"], "consumer", seed["consumer_app"], "ulink-consumer"),
            initiator=("ops", "", ""),
            expected_revision=1,
        )
    assert exc_info.value.code == "CONSUMER_VERSION_ENVIRONMENT_MISMATCH"
    assert len(chat_execution._bindings_from_store(str(db))) == 2

    # Devin #2：--restore 恢复 agents.published_version 回 2，并把源路由恢复回 v1。
    seed["write_node_config"]("production-41")
    restored = adoption.adopt(
        db, "production-41", "41", actor="ops-1", backup=backup, restore=True, verify_bindings=True
    )
    assert restored["mode"] == "restored"
    assert agents.get(seed["consumer_agent_id"], seed["tenant_a"]).published_version == 2
    old_route = ChatRouteBindingStore(db).get(
        "production-41", seed["tenant_a"], "consumer", chat_execution.SCENARIO_CONSUMER_SERVICE
    )
    assert old_route is not None and old_route.active is True and old_route.agent_version_no == 1


def test_verify_bindings_rejects_static_bindings_file_collision_before_commit(tmp_path):
    """回归 Devin #4：静态路由文件与目标迁移路由重名时，--verify-bindings 在提交前拒绝且不留备份。"""
    seed = _seed_legacy_gateway_db(tmp_path)
    db = seed["db"]
    seed["write_node_config"]("41")
    content_file = db.parent / "content.json"
    content_file.write_text("{}", encoding="utf-8")
    content_file.chmod(0o600)
    static_bindings = db.parent / "bindings.json"
    static_bindings.write_text(
        json.dumps(
            [
                {
                    "environment": "41",
                    "tenant_id": seed["tenant_a"],
                    "entry": "consumer",
                    "scenario": chat_execution.SCENARIO_CONSUMER_SERVICE,
                    "engine": "dify",
                    "app_id": seed["consumer_app"],
                    "agent_id": seed["consumer_agent_id"],
                    "agent_version_no": 1,
                    "workflow_id": "aa550684-5a55-4794-b487-ed2814cce544",
                    "workflow_key_file": str(db.parent / "consumer.key"),
                    "content_file": str(content_file),
                    "knowledge_base_ids": ["kb-active-1"],
                    "live_order_status": True,
                    "live_billing_record": True,
                    "callback_version": 5,
                }
            ]
        ),
        encoding="utf-8",
    )
    static_bindings.chmod(0o600)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    backup = backup_dir / "collision.json"

    with pytest.raises(adoption.AdoptionError, match="ADOPTION_BINDING_VERIFICATION_FAILED"):
        adoption.adopt(
            db,
            "production-41",
            "41",
            backup=backup,
            verify_bindings=True,
            bindings_file=static_bindings,
        )
    assert not backup.exists()
    assert ChatRouteBindingStore(db).get("41", seed["tenant_a"], "consumer", "consumer_service") is None
