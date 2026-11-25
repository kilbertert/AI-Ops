"""D51：一键租户开通串联编排、同摘要计划续签、漂移/撤权阻断与安全取消（ADR-0336）。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import httpx
import pytest
from dify.test_dify_ensure_workspace import upstream as workspace_upstream  # noqa: F401

from aiops_diagnostics.dify import dify_console_publish as bridge
from aiops_diagnostics.dify import environment_candidate_preparation as prep
from aiops_diagnostics.dify import environment_installation as installation
from aiops_diagnostics.dify import environment_onboarding as onboarding
from aiops_diagnostics.dify import environment_runtime as runtime
from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import environment_plans as plans
from aiops_diagnostics.gateway import environment_registry as registry
from aiops_diagnostics.gateway import environment_tasks as tasks
from aiops_diagnostics.gateway import gateway_db
from gateway.test_candidate_knowledge_connection import connected  # noqa: F401
from gateway.test_environment_candidate_preparation import (
    advance,
    candidate_chain,  # noqa: F401
    finish_child,
    request_of,
    rows,
)
from gateway.test_environment_consumer import consumer_app  # noqa: F401
from gateway.test_environment_knowledge import knowledge  # noqa: F401
from gateway.test_environment_tasks import ADMIN, create, prepared, receipt  # noqa: F401


def _wire_installation_config(state, tmp_path: Path) -> None:
    """为测试环境准备受控的私有安装目标与节点配置文件。"""
    settings = state[0][3]
    inst_dir = tmp_path / "install-refs"
    inst_dir.mkdir(mode=0o700, exist_ok=True)
    cfg = inst_dir / "agent-runs.json"
    manifest = inst_dir / "manifest.json"
    node = inst_dir / "node.json"
    inst = inst_dir / "environment-installation.json"
    cfg.write_text(json.dumps({"environment": "a", "nodes": []}), encoding="utf-8")
    manifest.write_text(json.dumps({"schema": "operator-exec-config/1"}), encoding="utf-8")
    node.write_text(
        json.dumps(
            {
                "environment": "a",
                "config_file": str(cfg),
                "manifest_file": str(manifest),
                "proxy_base_url": "https://127.0.0.1:8788",
            }
        ),
        encoding="utf-8",
    )
    inst.write_text(
        json.dumps(
            {
                "schema": "aiops-environment-installation/1",
                "targets": [{"id": "host-a", "node_file": str(node)}],
            }
        ),
        encoding="utf-8",
    )
    for f in (cfg, manifest, node, inst):
        f.chmod(0o600)
    settings.environment_installation_file = str(inst)


def _complete_site_preflight_with_runtime(state, site_id: str) -> None:
    owner = "d" * 32
    path, reference, *_ = state[0]
    claimed = tasks.worker(path, reference, {"id": site_id, "owner": owner})
    obs = {**receipt("a"), "runtime_sha256": "absent"}
    if claimed["claimed"]:
        done = tasks.worker(path, reference, {"id": site_id, "owner": owner, "result": obs})
        assert done["state"] == "observed", done
    else:
        db = drafts._database(path)
        with gateway_db.connection(db, write=True) as conn:
            conn.execute(
                "UPDATE environment_tasks SET state='observed',result=?,updated_at=? WHERE id=?",
                (json.dumps(obs), time.time(), site_id),
            )


def _complete_prepare_chain(state, prep_id: str, *, advance_onboard: bool = True) -> None:
    for action in prep.STEPS:
        assert advance(state).status_code == 200
        child_id = request_of(state, prep_id)["children"][action]
        if action == "site-preflight":
            _complete_site_preflight_with_runtime(state, child_id)
        else:
            finished = finish_child(state, action, child_id)
            assert finished["state"] == "observed", (action, finished)
    if advance_onboard:
        assert advance(state).status_code == 200
    else:
        prep.advance(state[1], {"advance_candidates": True})
    assert next(r for r in rows(state) if r["id"] == prep_id)["state"] == "observed"


def _mark_observed(state, task_id: str, result: dict, *, dispatched: bool = True) -> None:
    db = drafts._database(state[0][0])
    with gateway_db.connection(db, write=True) as conn:
        row = tasks._row(conn, task_id)
        req = json.loads(row["request"])
        req["attempted"] = True
        if dispatched:
            req["dispatched"] = True
        conn.execute(
            "UPDATE environment_tasks SET request=?,state='observed',result=?,"
            "owner='',lease_until=0,updated_at=? WHERE id=?",
            (json.dumps(req), json.dumps(result), time.time(), task_id),
        )


def test_one_click_onboarding_chains_all_four_stages_across_plan_renewal(
    candidate_chain,  # noqa: F811
    tmp_path: Path,
) -> None:
    state = candidate_chain
    _wire_installation_config(state, tmp_path)
    plan_id = state[2]["candidate_plan"]
    payload = {
        "onboard_plan": plan_id,
        "tenant_id": "same",
        "source_app_id": "source-app",
        "source_version": 7,
        "case_ref": "approved-case",
        "capabilities": ["consumer", "order", "offline"],
    }

    first = state[0][2].post("/v1/dify/management/environment-tasks", json=payload)
    assert first.status_code == 200, first.text
    onboard_id = first.json()["id"]
    assert first.json()["action"] == onboarding.ACTION
    assert first.json()["result"]["activation"] == "not_executed"
    assert first.json()["result"]["ready_to_publish"] is False

    # 同输入重复提交返回同一任务编号
    second = state[0][2].post("/v1/dify/management/environment-tasks", json=payload)
    assert second.status_code == 200 and second.json()["id"] == onboard_id

    # worker 不能直接领取父协调任务
    with pytest.raises(tasks.TaskRefused, match="ENVIRONMENT_TASK_WORKER_INVALID"):
        tasks.worker(state[0][0], state[0][1], {"id": onboard_id, "owner": "a" * 32})

    # 1) 完成 candidate-prepare 全部子步骤（暂不推进一键开通父任务，以便精准注入计划到期）
    prep_id = request_of(state, onboard_id)["children"]["candidate-prepare"]
    _complete_prepare_chain(state, prep_id, advance_onboard=False)

    # 模拟 15 分钟原计划窗到期（含子任务 result.expires_at 全部过期，但输入未漂移）
    # → advance 自动续签同摘要计划并拉起 candidate-install，且真实 runtime._applications() 允许续签导出！
    db = drafts._database(state[0][0])
    prep_children = request_of(state, prep_id)["children"]
    with gateway_db.connection(db, write=True) as conn:
        conn.execute("UPDATE environment_plans SET expires_at=? WHERE id=?", (time.time() - 5, plan_id))
        for expired_id in (prep_id, prep_children["operator-prepare"], prep_children["consumer-prepare"]):
            erow = tasks._row(conn, expired_id)
            ereq = json.loads(erow["request"])
            eres = json.loads(erow["result"])
            ereq["plan"]["expires_at"] = time.time() - 5
            if "expires_at" in eres:
                eres["expires_at"] = time.time() - 5
            if expired_id == prep_children["operator-prepare"]:
                ereq["resources"] = {
                    "expected_app_id": eres["app_id"],
                    "attempted": True,
                }
            conn.execute(
                "UPDATE environment_tasks SET request=?,result=? WHERE id=?",
                (json.dumps(ereq), json.dumps(eres), expired_id),
            )
        prep_req = json.loads(tasks._row(conn, prep_id)["request"])

    assert advance(state).status_code == 200
    onboard_req = request_of(state, onboard_id)
    install_id = onboard_req["children"]["candidate-install"]
    assert onboard_req["plan"]["id"] != plan_id
    assert onboard_req["plan"]["digest"] == prep_req["plan"]["digest"]
    # 实测 runtime._applications(..., allow_renewal=True) 在续签 plan_2 下验证过期 plan_1 双端准备任务仍通过！
    renewed_plan, _, exported_apps = runtime._applications(
        state[0][3],
        {
            "identity": list(ADMIN),
            "plan_id": onboard_req["plan"]["id"],
            "tenant_id": "same",
            "operator_task": prep_children["operator-prepare"],
            "consumer_task": prep_children["consumer-prepare"],
        },
        allow_renewal=True,
    )
    assert renewed_plan["id"] == onboard_req["plan"]["id"]
    assert set(exported_apps) == {"operator", "consumer"}
    # 已完成的前置阶段在阶段进度卡中仍显示 observed，不误报 expired
    mid_status = state[0][2].post("/v1/dify/management/environment-tasks", json={"id": onboard_id}).json()
    assert mid_status["result"]["stages"][0]["state"] == "observed"

    # 2) 模拟 candidate-install 完成并再次到期 → advance 自动续签并拉起 candidate-register！
    install_plan_id = request_of(state, install_id)["plan"]["id"]
    _mark_observed(
        state,
        install_id,
        {
            "scope": "candidate-installation-only",
            "expires_at": time.time() + 600,
            "installation": "installed",
            "version": "1.40.4",
            "source_revision": "a" * 40,
            "artifact_sha256": "b" * 64,
            "config_sha256": "c" * 64,
            "registry_sha256": "d" * 64,
            "runtime_sha256": "e" * 64,
        },
        dispatched=False,  # 证明已 observed 的无派发/免重装场景也不会被墙钟误杀
    )
    with gateway_db.connection(db, write=True) as conn:
        conn.execute(
            "UPDATE environment_plans SET expires_at=? WHERE id=?", (time.time() - 5, install_plan_id)
        )

    assert advance(state).status_code == 200
    register_id = request_of(state, onboard_id)["children"]["candidate-register"]
    assert request_of(state, register_id)["plan"]["id"] != install_plan_id

    # 3) 完成 candidate-register → advance 自动拉起 candidate-validate！
    _mark_observed(
        state,
        register_id,
        {
            "scope": "inactive-runtime-registration-only",
            "result": "registered",
            "expires_at": time.time() + 600,
            "activated": False,
            "business_acceptance": False,
        },
    )
    assert advance(state).status_code == 200
    validate_id = request_of(state, onboard_id)["children"]["candidate-validate"]

    # 4) candidate-validate 全三项通过 → 父任务 observed + ready_to_publish=True，绝不自动 publish！
    _mark_observed(
        state,
        validate_id,
        {
            "scope": "installed-business-validation-only",
            "expires_at": time.time() + 600,
            "activated": False,
            "business_accuracy": "unconfirmed",
            "checks": [
                {"capability": "consumer", "state": "passed"},
                {"capability": "offline", "state": "passed"},
                {"capability": "order", "state": "passed"},
            ],
        },
    )
    assert advance(state).status_code == 200
    final = state[0][2].post("/v1/dify/management/environment-tasks", json={"id": onboard_id}).json()
    assert final["state"] == "observed"
    assert final["result"]["ready_to_publish"] is True
    assert final["result"]["activation"] == "not_executed"
    assert final["result"]["business_accuracy"] == "unconfirmed"
    assert final["result"]["validation_task_id"] == validate_id
    assert [s["action"] for s in final["result"]["stages"]] == list(onboarding.ALL_STAGES)
    assert not any(json.loads(r["request"]).get("action") == "environment-publish" for r in rows(state))


def test_one_click_onboarding_three_stage_without_case_stops_after_register(
    candidate_chain,  # noqa: F811
    tmp_path: Path,
) -> None:
    state = candidate_chain
    _wire_installation_config(state, tmp_path)
    payload = {
        "onboard_plan": state[2]["candidate_plan"],
        "tenant_id": "same",
        "source_app_id": "source-app",
        "source_version": 7,
    }
    onboard_id = state[0][2].post("/v1/dify/management/environment-tasks", json=payload).json()["id"]
    prep_id = request_of(state, onboard_id)["children"]["candidate-prepare"]
    _complete_prepare_chain(state, prep_id)
    assert advance(state).status_code == 200
    install_id = request_of(state, onboard_id)["children"]["candidate-install"]
    _mark_observed(
        state,
        install_id,
        {
            "scope": "candidate-installation-only",
            "expires_at": time.time() + 600,
            "installation": "installed",
            "version": "1.40.4",
            "source_revision": "a" * 40,
            "artifact_sha256": "b" * 64,
            "config_sha256": "c" * 64,
            "registry_sha256": "d" * 64,
            "runtime_sha256": "e" * 64,
        },
    )
    assert advance(state).status_code == 200
    register_id = request_of(state, onboard_id)["children"]["candidate-register"]
    _mark_observed(
        state,
        register_id,
        {
            "scope": "inactive-runtime-registration-only",
            "result": "registered",
            "expires_at": time.time() + 600,
            "activated": False,
            "business_acceptance": False,
        },
    )
    assert advance(state).status_code == 200
    final = state[0][2].post("/v1/dify/management/environment-tasks", json={"id": onboard_id}).json()
    assert final["state"] == "observed"
    assert final["result"]["ready_to_publish"] is False
    assert final["result"]["registration_task_id"] == register_id
    assert "candidate-validate" not in request_of(state, onboard_id)["children"]


def test_one_click_onboarding_refuses_missing_install_config_drift_revocation_and_cancels_safely(
    candidate_chain,  # noqa: F811
    tmp_path: Path,
) -> None:
    state = candidate_chain
    base_payload = {
        "onboard_plan": state[2]["candidate_plan"],
        "tenant_id": "same",
        "source_app_id": "source-app",
        "source_version": 7,
    }
    # 1) 缺安装配置：快败且不创建任何 candidate-prepare 行
    before_count = len(rows(state))
    refused = state[0][2].post("/v1/dify/management/environment-tasks", json=base_payload)
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "ENVIRONMENT_INSTALLATION_CONFIG_REQUIRED"
    assert len(rows(state)) == before_count

    # 2) 配置齐全后提交，模拟在 prepare 后发生草稿漂移：绝不创建 candidate-install！
    _wire_installation_config(state, tmp_path)
    onboard_id = state[0][2].post("/v1/dify/management/environment-tasks", json=base_payload).json()["id"]
    prep_id = request_of(state, onboard_id)["children"]["candidate-prepare"]
    _complete_prepare_chain(state, prep_id, advance_onboard=False)

    db = drafts._database(state[0][0])
    with gateway_db.connection(db, write=True) as conn:
        conn.execute("UPDATE environment_drafts SET revision=revision+1 WHERE environment='a'")
    assert advance(state).status_code == 200
    after_drift = state[0][2].post("/v1/dify/management/environment-tasks", json={"id": onboard_id}).json()
    assert after_drift["state"] in ("blocked", "expired")
    assert after_drift["error_code"] == "ENVIRONMENT_TASK_PLAN_EXPIRED"
    assert "candidate-install" not in request_of(state, onboard_id)["children"]

    # 恢复草稿修订，用冷初装分支测试安全级联取消
    with gateway_db.connection(db, write=True) as conn:
        conn.execute("UPDATE environment_drafts SET revision=revision-1 WHERE environment='a'")
    cold_payload = {**base_payload, "initial_installation": True}
    cold_onboard = state[0][2].post("/v1/dify/management/environment-tasks", json=cold_payload).json()
    assert cold_onboard["action"] == onboarding.ACTION
    cold_prep_id = request_of(state, cold_onboard["id"])["children"]["candidate-prepare"]
    cold_install_id = request_of(state, cold_prep_id)["children"]["site-install"]

    cancelled = (
        state[0][2]
        .post("/v1/dify/management/environment-tasks", json={"id": cold_onboard["id"], "cancel": True})
        .json()
    )
    assert cancelled["state"] == "cancelled"
    assert next(r for r in rows(state) if r["id"] == cold_prep_id)["state"] == "cancelled"
    assert next(r for r in rows(state) if r["id"] == cold_install_id)["state"] == "cancelled"
    # 之前已经 observed 的 prep_id 绝不能被触碰
    assert next(r for r in rows(state) if r["id"] == prep_id)["state"] == "observed"

    # 撤权时不能提交，且在飞一键开通推进时保留精确错误码 ENVIRONMENT_MANAGEMENT_FORBIDDEN
    reg_path = state[0][0]
    body = registry.load(reg_path)
    registry.apply(
        reg_path, body["environments"], expected_revision=body["revision"], actor="op", managers=[]
    )
    assert state[0][2].post("/v1/dify/management/environment-tasks", json=base_payload).status_code == 403


def test_one_click_onboarding_cancel_ownership_inflight_reconcile_and_renewal_reuse(
    candidate_chain,  # noqa: F811
    tmp_path: Path,
) -> None:
    state = candidate_chain
    _wire_installation_config(state, tmp_path)
    plan_id = state[2]["candidate_plan"]
    db = drafts._database(state[0][0])

    # 1) 共享手动 candidate-prepare 归属隔离：取消一键开通绝不误杀先建的手动准备任务
    manual_prep = (
        state[0][2]
        .post(
            "/v1/dify/management/environment-tasks",
            json={
                "candidate_plan": plan_id,
                "tenant_id": "same",
                "source_app_id": "source-app",
                "source_version": 7,
            },
        )
        .json()
    )
    onboard_shared = (
        state[0][2]
        .post(
            "/v1/dify/management/environment-tasks",
            json={
                "onboard_plan": plan_id,
                "tenant_id": "same",
                "source_app_id": "source-app",
                "source_version": 7,
            },
        )
        .json()
    )
    assert request_of(state, onboard_shared["id"])["children"]["candidate-prepare"] == manual_prep["id"]
    cancelled_shared = (
        state[0][2]
        .post("/v1/dify/management/environment-tasks", json={"id": onboard_shared["id"], "cancel": True})
        .json()
    )
    assert cancelled_shared["state"] == "cancelled"
    assert next(r for r in rows(state) if r["id"] == manual_prep["id"])["state"] == "waiting"

    # 完成手动 candidate-prepare，再验证：取消后并发 spawn 零孤儿、在飞子任务取消进入 reconcile 并在终态后收敛
    _complete_prepare_chain(state, manual_prep["id"], advance_onboard=False)
    # 取消后调用 installation.submit(..., onboard_parent=cancelled_id) 返回 None 且不落库
    before_rows = len(rows(state))
    assert (
        installation.submit(
            state[1], ADMIN, manual_prep["id"], plan_id=plan_id, onboard_parent=onboard_shared["id"]
        )
        is None
    )
    assert len(rows(state)) == before_rows

    # 新开 4 阶段一键开通，推进到 candidate-install 并把子任务置为 executing（远端在飞）
    onboard_4 = (
        state[0][2]
        .post(
            "/v1/dify/management/environment-tasks",
            json={
                "onboard_plan": plan_id,
                "tenant_id": "same",
                "source_app_id": "source-app",
                "source_version": 7,
                "case_ref": "approved-case",
                "capabilities": ["consumer", "order", "offline"],
            },
        )
        .json()
    )
    assert advance(state).status_code == 200
    inst_id = request_of(state, onboard_4["id"])["children"]["candidate-install"]
    with gateway_db.connection(db, write=True) as conn:
        conn.execute(
            "UPDATE environment_tasks SET state='executing',owner=?,lease_until=? WHERE id=?",
            ("a" * 32, time.time() + 300, inst_id),
        )

    # 在飞取消：父保持 checking(reconcile) 并阻塞新一键开通；子终态后 advance 收敛为 cancelled
    cancelling = (
        state[0][2]
        .post("/v1/dify/management/environment-tasks", json={"id": onboard_4["id"], "cancel": True})
        .json()
    )
    assert cancelling["state"] == "reconcile"
    assert cancelling["cancel_requested"] is True
    busy = state[0][2].post(
        "/v1/dify/management/environment-tasks",
        json={
            "onboard_plan": plan_id,
            "tenant_id": "same",
            "source_app_id": "source-app",
            "source_version": 7,
            "initial_installation": True,
        },
    )
    assert busy.status_code == 403
    assert busy.json()["error"]["code"] == "ENVIRONMENT_TASK_BUSY"

    # 子任务完成对账并终态为 cancelled 后，advance 收敛父任务为 cancelled
    with gateway_db.connection(db, write=True) as conn:
        conn.execute(
            "UPDATE environment_tasks SET state='cancelled',owner='',lease_until=0 WHERE id=?",
            (inst_id,),
        )
    assert advance(state).status_code == 200
    settled = state[0][2].post("/v1/dify/management/environment-tasks", json={"id": onboard_4["id"]}).json()
    assert settled["state"] == "cancelled"
    assert settled["result"]["stages"][-1]["state"] == "cancelled"

    # 2) 已取消的 candidate-install(plan_1) 不会卡死续签 plan_2 的新安装尝试
    orig_plan = request_of(state, manual_prep["id"])["plan"]
    with gateway_db.connection(db, write=True) as conn:
        conn.execute("UPDATE environment_plans SET expires_at=? WHERE id=?", (time.time() - 5, plan_id))
    fresh_plan = plans.preflight(
        state[0][0],
        ADMIN,
        "a",
        orig_plan["draft_revision"],
        orig_plan["registry_revision"],
        state[0][1],
        min_remaining_seconds=300,
    )
    assert fresh_plan["id"] != plan_id
    retried_inst = installation.submit(state[1], ADMIN, manual_prep["id"], plan_id=fresh_plan["id"])
    assert retried_inst is not None
    assert retried_inst["id"] != inst_id
    assert retried_inst["state"] == "queued"

    # 3) preflight(min_remaining_seconds) 边界校验
    for bad_remaining in (-1, 900, float("inf")):
        with pytest.raises(ValueError, match="min_remaining_seconds"):
            plans.preflight(
                state[0][0],
                ADMIN,
                "a",
                orig_plan["draft_revision"],
                orig_plan["registry_revision"],
                state[0][1],
                min_remaining_seconds=bad_remaining,
            )

    # 4) 撤权时在飞一键开通记录精确错误码 ENVIRONMENT_MANAGEMENT_FORBIDDEN
    onboard_rev = (
        state[0][2]
        .post(
            "/v1/dify/management/environment-tasks",
            json={
                "onboard_plan": fresh_plan["id"],
                "tenant_id": "same",
                "source_app_id": "source-app",
                "source_version": 7,
            },
        )
        .json()
    )
    reg_path = state[0][0]
    body = registry.load(reg_path)
    registry.apply(
        reg_path, body["environments"], expected_revision=body["revision"], actor="op", managers=[]
    )
    assert advance(state).status_code == 200
    with gateway_db.connection(db, readonly=True) as conn:
        rev_row = tasks._row(conn, onboard_rev["id"])
    assert rev_row["state"] == "blocked"
    assert rev_row["error"] == "ENVIRONMENT_MANAGEMENT_FORBIDDEN"


def test_console_bridge_validates_onboard_plan_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    forwarded: list[dict] = []

    def fake_request(url, *, method="GET", headers=None, body=None, timeout=15):
        if url.endswith("/console/api/workspaces/current/summary"):
            return 200, {"role": "admin"}
        forwarded.append(body)
        return 200, {"id": "task_" + "1" * 32, "action": "tenant-onboard", "state": "queued"}

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
        url = f"http://127.0.0.1:{server.server_port}/__aiops/manage"
        headers = {
            "Origin": "https://console.example.test",
            "Referer": "https://console.example.test/aiops/environments",
            "Sec-Fetch-Site": "same-origin",
            "Content-Type": "application/json",
            "Cookie": "access_token=tok; csrf_token=csrf1",
            "X-CSRF-Token": "csrf1",
        }
        valid = {
            "action": "environment-task",
            "onboard_plan": "plan_" + "a" * 32,
            "tenant_id": "same",
            "source_app_id": "11111111-1111-1111-1111-111111111111",
            "source_version": 7,
            "case_ref": "case-01",
            "capabilities": ["consumer", "order", "offline"],
        }
        with httpx.Client() as client:
            assert client.post(url, headers=headers, json=valid).status_code == 200
            count = len(forwarded)
            for bad in (
                {**valid, "initial_installation": False},
                {k: v for k, v in valid.items() if k != "capabilities"},
                {**valid, "capabilities": ["consumer", "consumer"]},
                {**valid, "extra_field": "bad"},
                {"action": "environment-drafts", "onboard_plan": "plan_" + "a" * 32},
            ):
                assert client.post(url, headers=headers, json=bad).status_code == 400, bad
            assert len(forwarded) == count
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
