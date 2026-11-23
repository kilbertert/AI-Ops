"""正式应用停用试跑沿原ConsumerTrial与工具回调，不要求目标拥有Console管理凭据。"""

# ruff: noqa: F811 - 原合成真实Gateway夹具复用。
import json
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from execution.test_app_runtime_node_entry import _Resolver

from aiops_diagnostics.common.private_files import write_private_text
from aiops_diagnostics.dify.dify_chatflow import ChatflowReply
from aiops_diagnostics.gateway import environment_registry
from aiops_diagnostics.gateway import environment_release_trial as trial
from aiops_diagnostics.gateway.environment_business import load_case as REAL_LOAD_CASE
from aiops_diagnostics.knowledge import delegated_knowledge_client
from aiops_diagnostics.knowledge.knowledge_retrieval import (
    KnowledgeSearchResult,
    RetrievalStatus,
    RetrievedChunk,
)
from aiops_diagnostics.publication import environment_release as release
from publication.test_environment_release import dual, operator, prepared  # noqa: F401


@pytest.fixture
def target(prepared, dual, monkeypatch):
    ctx, doc = prepared
    doc.update(case_ref="synthetic-case", installed={}, expires_at=time.time() + 900)
    spec = {
        "id": doc["id"],
        "environment": doc["environment"],
        "tenant_id": doc["tenant_id"],
        "case_ref": doc["case_ref"],
        "installed": {},
        "capabilities": ["consumer", "offline", "order"],
        "operator_app": doc["apps"]["operator"]["app_id"],
        "consumer": {"workspace_id": doc["workspace_id"], "app_id": doc["apps"]["consumer"]["app_id"]},
    }
    case = {
        "consumer_session": "normal-synthetic-session",
        "cases": {
            "consumer": {
                "order_no": "ORDER-B-2",
                "reference_id": "ref-real",
                "question": "查询ORDER-B-2，如何启动充电",
            }
        },
    }
    ctx.runtime._order_authorizer.ownership["ORDER-B-2"] = "USER-B"
    caller = [_Resolver().resolve("token-b", required_scope="aiops:orders:read")]
    monkeypatch.setattr(trial, "consumer_identity", lambda *a: caller[0])
    monkeypatch.setattr(trial.business, "load_case", lambda *a: (case, "b" * 64))
    knowledge = SimpleNamespace(
        list_knowledge_bases=lambda: [{"knowledge_base_id": "kb-own", "audience": "customer"}],
        search=lambda *a, **kw: KnowledgeSearchResult(
            RetrievalStatus.FOUND, (RetrievedChunk("ref-real", "按设备提示启动。", "启动帮助", 1.0, ()),), 1
        ),
    )
    monkeypatch.setattr(delegated_knowledge_client, "for_application", lambda *a: knowledge)
    monkeypatch.setattr(
        "aiops_diagnostics.data.charging_status.read_charging_status",
        lambda *a, **kw: {"order_no": "ORDER-B-2", "state": "ended", "source": "cpms.chargingInfo"},
    )
    release.operation(ctx, doc, "prepare")
    write_private_text(
        ctx.settings.data_home / "environment-publications" / (doc["id"] + ".release.json"), json.dumps(doc)
    )
    requests = []

    class Dify:
        def __init__(self, *a, **kw):
            pass

        def run(self, **kw):
            requests.append(kw)
            ids = [str(uuid4()) for _ in range(4)]
            kw["on_started"](*ids)
            for resource in ("status", "knowledge"):
                response = dual.f["client"].post(
                    "/v1/internal/consumer-data/agent",
                    headers={"Authorization": "Bearer " + kw["inputs"]["data_grant"]},
                    json={
                        "resource": resource,
                        "order_no": "ORDER-B-2" if resource == "status" else "",
                        "knowledge_base_ids": ["kb-own"],
                        "query": "启动充电",
                    },
                )
                assert response.status_code == 200, response.text
            return ChatflowReply(*ids, "按设备提示启动。", {})

        def stop(self, **kw):
            pass

    monkeypatch.setattr("aiops_diagnostics.consumer.consumer_trial.DifyChatflowClient", Dify)
    monkeypatch.setattr(
        "aiops_diagnostics.consumer.consumer_trial.DifyConsoleAdmin",
        lambda *a: pytest.fail("target must not access Console admin"),
    )
    return ctx, doc, spec, case, caller, requests


def finish(target):
    ctx, doc, spec, _, _, _ = target
    result = trial.start(ctx, {"operation": doc["id"], "spec": spec})
    until = time.monotonic() + 5
    while time.monotonic() < until:
        observed = trial.read(
            ctx,
            {
                "operation": doc["id"],
                "trial_id": result["trial_id"],
                "expected_reference": result["expected_reference"],
            },
        )
        if observed["state"] not in {"running", "queued"}:
            return result, observed
        time.sleep(0.01)
    raise AssertionError("trial unfinished")


def test_formal_application_trial_uses_actual_callback_without_opening_admission(target):
    ctx, doc, spec, case, caller, requests = target
    started, result = finish(target)
    assert result["state"] == "passed", result
    assert len(requests) == 1 and requests[0]["key"] == "synthetic-app-key"
    assert trial.start(ctx, {"operation": doc["id"], "spec": spec}) == started
    assert len(requests) == 1
    body = environment_registry.load(ctx.settings.environment_registry_file)
    assert not next(t for t in body["environments"][0]["tenants"] if t["id"] == "TENANT-B")["enabled"]
    assert release.read_record(ctx.settings, doc["id"])["routes"] == {}
    # 已关闭数据能力仍可正常回读；真实身份撤销后不得沿用旧scope。
    caller[0] = _Resolver().resolve("token-a", required_scope="aiops:orders:read")
    with pytest.raises(ValueError, match="identity changed"):
        trial.read(
            ctx, {"operation": doc["id"], "trial_id": started["trial_id"], "expected_reference": "ref-real"}
        )


def test_published_consumer_original_http_path_and_restore_keep_old_operator(target, dual, monkeypatch):
    import httpx
    from operations.test_operator_cpms import node, request, terminal

    from aiops_diagnostics.gateway.environment_registry import EnvironmentRegistry
    from aiops_diagnostics.operator import operator_cpms

    ctx, doc, _, _, caller, requests_seen = target
    original_resolver = ctx.caller_resolver

    class Resolver:
        def resolve(self, token, **kwargs):
            return (
                _Resolver().resolve(token, **kwargs)
                if token.startswith("token-")
                else original_resolver.resolve(token, **kwargs)
            )

    from aiops_diagnostics.identity.caller_auth import RegisteredCallerResolver

    ctx.runtime.environment_registry = EnvironmentRegistry(ctx.settings.environment_registry_file, "env-a")
    ctx.caller_resolver = RegisteredCallerResolver(Resolver(), ctx.runtime.environment_registry)
    # dual-entry模板夹具的旧客服source仅供复制，没有运行content；撤回该无运行row，旧管家仍在用。
    from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore

    routes = ChatRouteBindingStore(ctx.settings.chat_route_bindings_file)
    routes.retract(
        environment="env-a",
        tenant_id="TENANT-A",
        entry="consumer",
        scenario="consumer_service",
        expected_revision=1,
    )
    ctx.settings.consumer_chatflow_enabled = True
    ctx.settings.knowledge_delegation_file = "configured-private-file"
    ctx.settings.charging_status_base_url = dual.f["target"].cpms_base_url
    engine = __import__(
        "aiops_diagnostics.consumer.consumer_trial", fromlist=["DifyChatflowClient"]
    ).DifyChatflowClient
    monkeypatch.setattr("aiops_diagnostics.consumer.consumer_chat_service.DifyChatflowClient", engine)
    from aiops_diagnostics.gateway import gateway_api

    monkeypatch.setattr(
        gateway_api, "_authorized_charging_snapshot", lambda *a: {"order_no": "ORDER-B-2", "state": "ended"}
    )
    # 准入未开时普通消费者仍被原网关拒绝。
    url = dual.f["gateway_base"] + "/v1/assistant/questions"
    headers = {"Authorization": "Bearer token-b", "X-Business-Entry": "consumer"}
    denied = httpx.post(url, headers=headers, json={"question": "查询ORDER-B-2，启动帮助"}, timeout=5)
    assert denied.status_code in {403, 404}, denied.text
    release.operation(ctx, doc, "publish")
    directory = environment_registry.load(ctx.settings.environment_registry_file)
    directory["environments"][0]["tenants"][1]["enabled"] = True
    environment_registry.apply(
        __import__("pathlib").Path(ctx.settings.environment_registry_file),
        directory["environments"],
        expected_revision=directory["revision"],
        actor="synthetic-approved-installer",
        managers=[],
    )
    response = httpx.post(url, headers=headers, json={"question": "查询ORDER-B-2，启动帮助"}, timeout=5)
    assert response.status_code == 202, response.text
    qa = response.json()["qa_id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        read = httpx.get(url + "/" + qa, headers=headers, timeout=5)
        if read.json().get("status") not in {"queued", "running"}:
            break
        time.sleep(0.01)
    assert read.status_code == 200 and read.json()["status"] == "completed", read.text
    assert "按设备提示启动" in str(read.json()["result"])
    assert requests_seen[-1]["workflow_id"] == doc["apps"]["consumer"]["workflow_id"]
    release.operation(ctx, doc, "restore")
    assert httpx.post(url, headers=headers, json={"question": "继续"}, timeout=5).status_code == 410
    # 原TENANT-A的管家仍沿原HTTP和节点授权完成，不靠只比store声称旧租户可用。
    monkeypatch.setattr(
        operator_cpms.SnailDispatcher, "dispatch", lambda _, config, run_id: node(dual.f, run_id)
    )
    old = request(dual.f["client"])
    assert old.status_code == 202, old.text
    assert terminal(dual.f, old.json()["qa_id"]).json()["status"] == "completed"


def test_three_formal_runs_complete_from_real_tool_evidence(target, dual, monkeypatch):
    from operations.test_device_offline import END, START, event, order
    from operations.test_device_offline import run as analyze
    from operations.test_operator_agent import call_tool
    from operations.test_operator_agent import finish as finish_operator
    from operations.test_operator_cpms import ORDER, scope

    from aiops_diagnostics.common.private_files import write_private_text
    from aiops_diagnostics.dify.self_service_store import SelfServiceStore
    from aiops_diagnostics.execution import chat_execution, node_publication
    from aiops_diagnostics.gateway import environment_trial
    from aiops_diagnostics.operator import offline_tool, operator_cpms

    ctx, doc, spec, case, _, requests = target
    doc["installed"]["runtime_sha256"] = "a" * 64
    spec["installed"] = dict(doc["installed"])
    manifest = json.loads(doc["apps"]["operator"]["manifest"])
    from aiops_diagnostics.operator import operator_tools

    declared = operator_tools.tool(offline_tool.NAME)
    manifest["tools"].append(
        {"name": declared.name, "description": declared.description, "input_schema": declared.input_schema()}
    )
    doc["apps"]["operator"]["manifest"] = json.dumps(manifest)
    # 原prepare尚未正式publish，重新fixture本次冻结内容；不直接改版本/run证据。
    from aiops_diagnostics.publication.environment_release import record_path

    record_path(ctx.settings, doc["id"]).unlink()
    release.operation(ctx, doc, "prepare")
    write_private_text(
        ctx.settings.data_home / "environment-publications" / (doc["id"] + ".release.json"), json.dumps(doc)
    )
    case["operator_token"] = "normal-synthetic-operator"
    case["cases"].update(
        order={"order_no": ORDER, "question": "查询异常订单" + ORDER},
        offline={
            "device_id": "D",
            "start": START,
            "end": END,
            "question": "设备 D 在" + START + "到" + END + "的离线情况",
        },
    )
    app = doc["apps"]["operator"]["app_id"]
    row = SelfServiceStore(ctx.settings.database_file).app(app)
    expected = node_publication.managed_target(ctx.settings, row["profile_id"], app, doc["tenant_id"])
    binding = chat_execution.trial_binding(ctx.settings, app)
    targets = []
    identities = []

    def target_stub(*a):
        targets.append(time.time())
        return (expected, binding, doc["apps"]["operator"]["manifest"], row)

    def identity_stub(*a):
        identities.append(time.time())
        return scope(tenant="TENANT-B")

    monkeypatch.setattr(environment_trial, "target", target_stub)
    monkeypatch.setattr(environment_trial, "identity", identity_stub)
    dual.f["state"].update(
        tenant="TENANT-B",
        monitor_rows=[
            {
                "orderNo": ORDER,
                "tenantId": "TENANT-B",
                "siteId": "SITE-1",
                "createdTime": "2026-11-05 14:26:00",
                "power": 5,
                "current": 10,
                "voltage": 230,
            }
        ],
    )
    # SQL服务替身返回由原离线算法演算的事件；原工具授权、投影、run和最终proof不替换。
    data = analyze(
        [event("2026-11-01T01:00:00Z", "2026-11-01T02:00:00Z", tenant_id="TENANT-B", site_id="SITE-1")],
        [order(ORDER, tenant_id="TENANT-B", site_id="SITE-1")],
    )
    data["scope"] = {"site_count": 1, "device_id": "D"}
    data["quality"]["unverified_device_records"] = 0
    monkeypatch.setattr(
        offline_tool, "query", lambda *a: (data, {"devices": [["D", "SITE-1"]], "orders": []})
    )
    monkeypatch.setattr(offline_tool, "require_current", lambda *a: None)
    dispatches = []

    def node_dispatch(_, config, run_id):
        task = ctx.operator_cpms.tasks[run_id]
        if "设备 D " in task.question:
            _, head, _ = call_tool(
                dual.f, run_id, offline_tool.NAME, {"device": "D", "start": START, "end": END}
            )
        else:
            call_tool(dual.f, run_id, "cpms_order_detail", {"order_no": ORDER})
            call_tool(dual.f, run_id, "cpms_charging_samples", {"order_no": ORDER})
            _, head, _ = call_tool(dual.f, run_id, "cpms_order_process_stats")
        finish_operator(dual.f, run_id, head, "已有本轮记录，请结合现场核对。", [])
        dispatches.append(run_id)

    errors = []

    def dispatch(*args):
        try:
            return node_dispatch(*args)
        except Exception as exc:
            errors.append(str(exc))
            raise

    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
    consumer, proof = finish(target)
    assert proof["state"] == "passed"
    trials = {}
    for capability in ("order", "offline"):
        result = trial.start_operator(ctx, {"operation": doc["id"], "spec": spec, "capability": capability})
        trials[capability] = result["trial_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            read = environment_trial.read(ctx, {"app_id": app, "trial_id": result["trial_id"]})
            if read["status"] not in {"queued", "running", "pending"}:
                break
            time.sleep(0.01)
        assert read["status"] == "completed", (read, errors)
    checks = len(identities)
    verified = trial.complete(ctx, {"operation": doc["id"], "spec": spec, "trials": trials})
    assert verified["state"] == "passed" and len(verified["operator_runs"]) == 2
    assert set(verified["operator_runs"]) == set(dispatches)
    assert verified["run_id"] == ctx.dify_run_store.by_qa(consumer["trial_id"]).run_id
    # complete对每个operator能力重核现行公司身份与发布目标：同一case token再解、
    # 同一runtime sha再比对，而非沿用start时的一次授权或租约。
    assert len(identities) == checks + 2 and len(targets) == checks + 2
    # 确认时刻=最后一次全部核对完成的墙钟：不得早于任何一次operator重核，
    # 也不得沿用消费者read的确认时刻。
    stored = json.loads(
        (
            ctx.settings.data_home / "environment-publications" / (doc["id"] + ".consumer-verification.json")
        ).read_text()
    )
    assert verified["observed_at"] >= max(identities + targets) >= stored["observed_at"] - 1
    # 进程租约失活（重启或300s数据能力窗关闭）后，complete按持久锚点重核同一
    # 证据面并写回新的observed_at；run/operator_runs不变，模型不重放。
    first = verified["observed_at"]
    checks = len(identities)
    ctx.consumer_candidate_trials.leases.clear()
    ctx.operator_cpms.candidate_authorizations.clear()
    time.sleep(0.01)
    again = trial.complete(ctx, {"operation": doc["id"], "spec": spec, "trials": trials})
    assert again["state"] == "passed" and again["observed_at"] > first
    assert (again["run_id"], again["operator_runs"]) == (
        verified["run_id"],
        verified["operator_runs"],
    )
    assert len(dispatches) == 2 and len(requests) == 1 and len(identities) == checks + 2
    # 撤权的公司token在原run仍completed时也不得激活；身份/案例漂移是拒绝不是过期。
    monkeypatch.setattr(
        environment_trial,
        "identity",
        lambda *a: (_ for _ in ()).throw(environment_trial.TrialRefused("REVOKED")),
    )
    with pytest.raises(RuntimeError):
        trial.complete(ctx, {"operation": doc["id"], "spec": spec, "trials": trials})
    monkeypatch.setattr(environment_trial, "identity", lambda *a: scope(tenant="TENANT-A"))
    with pytest.raises(ValueError, match="identity changed"):
        trial.complete(ctx, {"operation": doc["id"], "spec": spec, "trials": trials})
    monkeypatch.setattr(environment_trial, "identity", identity_stub)
    file = ctx.settings.data_home / "environment-publications" / (doc["id"] + ".order.trial.json")
    intent = json.loads(file.read_text())
    intent["case_sha256"] = "0" * 64
    write_private_text(file, json.dumps(intent))
    with pytest.raises(ValueError, match="another operation"):
        trial.complete(ctx, {"operation": doc["id"], "spec": spec, "trials": trials})


def test_reconfirm_uses_persisted_anchors_after_lease_loss_without_replay(target):
    ctx, doc, _, _, caller, requests = target
    started, result = finish(target)
    assert result["state"] == "passed" and result["anchors"]["doc"]
    first = result["observed_at"]
    ctx.consumer_candidate_trials.leases.clear()
    time.sleep(0.01)
    again = trial.read(
        ctx,
        {
            "operation": doc["id"],
            "trial_id": started["trial_id"],
            "expected_reference": "ref-real",
        },
    )
    assert again["state"] == "passed" and again["observed_at"] > first
    assert again["run_id"] == result["run_id"] and again["anchors"] == result["anchors"]
    assert len(requests) == 1
    # 撤权身份与锚点缺失各自拒绝；复验不把「查不到证据」当通过。
    caller[0] = _Resolver().resolve("token-a", required_scope="aiops:orders:read")
    with pytest.raises(ValueError, match="identity changed"):
        trial.read(
            ctx,
            {
                "operation": doc["id"],
                "trial_id": started["trial_id"],
                "expected_reference": "ref-real",
            },
        )
    caller[0] = _Resolver().resolve("token-b", required_scope="aiops:orders:read")
    receipt = (
        ctx.settings.data_home / "environment-publications" / (doc["id"] + ".consumer-verification.json")
    )
    receipt.unlink()
    with pytest.raises(ValueError, match="unavailable"):
        trial.read(
            ctx,
            {
                "operation": doc["id"],
                "trial_id": started["trial_id"],
                "expected_reference": "ref-real",
            },
        )


def test_complete_returns_expired_verdict_when_release_or_case_window_closed(target, monkeypatch, tmp_path):
    ctx, doc, spec, case, _, _ = target
    finish(target)
    payload = {"operation": doc["id"], "spec": spec, "trials": {}}
    release_file = ctx.settings.data_home / "environment-publications" / (doc["id"] + ".release.json")
    original = release_file.read_text()
    doc["expires_at"] = time.time() - 1
    write_private_text(release_file, json.dumps(doc))
    assert trial.complete(ctx, payload) == {
        "operation": doc["id"],
        "state": "expired",
        "error_code": "ENVIRONMENT_PUBLICATION_VERIFICATION_EXPIRED",
    }
    # 写回原字节：doc指纹是锚点一部分，重算时间会把它变成另一份发行物。
    write_private_text(release_file, original)
    # 真实load_case按窗口判定分终态：expires_at越过只有CaseExpired入verdict；
    # 形状无效/篡改（同一公开error_code）仍是拒绝，不吞成时间过期。
    file = tmp_path / "validation-cases" / (spec["case_ref"] + ".json")
    file.parent.mkdir(mode=0o700)
    monkeypatch.setattr(trial.business, "case_file", lambda *a: file)
    monkeypatch.setattr(trial.business, "load_case", REAL_LOAD_CASE)
    body = {
        "environment": spec["environment"],
        "tenant_id": spec["tenant_id"],
        "expires_at": time.time() - 1,
        "operator_token": case.get("operator_token", "synthetic"),
        "consumer_session": case["consumer_session"],
        "cases": case["cases"],
    }
    write_private_text(file, json.dumps(body))
    assert trial.complete(ctx, payload)["state"] == "expired"
    body["expires_at"] = time.time() + 600
    del body["consumer_session"]
    write_private_text(file, json.dumps(body))
    with pytest.raises(trial.business.ValidationRefused):
        trial.complete(ctx, payload)
    body["consumer_session"] = case["consumer_session"]
    body["cases"] = {"consumer": dict(case["cases"]["consumer"], reference_id="ref-other")}
    write_private_text(file, json.dumps(body))
    with pytest.raises(ValueError):
        trial.complete(ctx, payload)


def test_legacy_live_path_without_anchors_still_reads_and_upgrades_receipt(target):
    ctx, doc, _, _, _, _ = target
    started, result = finish(target)
    assert result["state"] == "passed"
    receipt = (
        ctx.settings.data_home / "environment-publications" / (doc["id"] + ".consumer-verification.json")
    )
    legacy = json.loads(receipt.read_text())
    del legacy["anchors"]
    write_private_text(receipt, json.dumps(legacy))
    # 活租约内的旧格式回执照常沿live路径读回；通过即升级写入真实锚点。
    again = trial.read(
        ctx,
        {
            "operation": doc["id"],
            "trial_id": started["trial_id"],
            "expected_reference": "ref-real",
        },
    )
    assert again["state"] == "passed" and again["anchors"]["doc"]
    # 租约失活后无首证锚点一律拒绝：旧任务走页面恢复，不迁移伪锚点。
    ctx.consumer_candidate_trials.leases.clear()
    write_private_text(receipt, json.dumps(legacy))
    with pytest.raises(ValueError, match="unavailable"):
        trial.read(
            ctx,
            {
                "operation": doc["id"],
                "trial_id": started["trial_id"],
                "expected_reference": "ref-real",
            },
        )


def test_same_conversation_followup_after_publication_uses_original_scope(target, dual, monkeypatch):
    # 原TCP完整用例的同会话需求独立于页面状态；复用同一运行入口，不制造第二聊天实现。
    import httpx

    from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore
    from aiops_diagnostics.gateway import gateway_api
    from aiops_diagnostics.gateway.environment_registry import EnvironmentRegistry
    from aiops_diagnostics.identity.caller_auth import RegisteredCallerResolver

    ctx, doc, _, _, _, observed = target
    registry = EnvironmentRegistry(ctx.settings.environment_registry_file, "env-a")
    ctx.runtime.environment_registry = registry
    ctx.caller_resolver = RegisteredCallerResolver(_Resolver(), registry)
    routes = ChatRouteBindingStore(ctx.settings.chat_route_bindings_file)
    routes.retract(
        environment="env-a",
        tenant_id="TENANT-A",
        entry="consumer",
        scenario="consumer_service",
        expected_revision=1,
    )
    ctx.settings.consumer_chatflow_enabled = True
    ctx.settings.knowledge_delegation_file = "configured-private-file"
    engine = __import__(
        "aiops_diagnostics.consumer.consumer_trial", fromlist=["DifyChatflowClient"]
    ).DifyChatflowClient
    monkeypatch.setattr("aiops_diagnostics.consumer.consumer_chat_service.DifyChatflowClient", engine)
    monkeypatch.setattr(
        gateway_api, "_authorized_charging_snapshot", lambda *a: {"order_no": "ORDER-B-2", "state": "ended"}
    )
    release.operation(ctx, doc, "publish")
    body = environment_registry.load(ctx.settings.environment_registry_file)
    body["environments"][0]["tenants"][1]["enabled"] = True
    environment_registry.apply(
        __import__("pathlib").Path(ctx.settings.environment_registry_file),
        body["environments"],
        expected_revision=body["revision"],
        actor="fixture",
        managers=[],
    )
    url, headers = (
        dual.f["gateway_base"] + "/v1/assistant/questions",
        {"Authorization": "Bearer token-b", "X-Business-Entry": "consumer"},
    )
    conversation = None
    for question in ("查询 ORDER-B-2 以及充电方法", "刚才的订单继续解释一下"):
        response = httpx.post(
            url,
            headers=headers,
            json={"question": question, **({"conversation_id": conversation} if conversation else {})},
        )
        assert response.status_code == 202, response.text
        value = response.json()
        conversation = value["conversation_id"]
        for _ in range(100):
            final = httpx.get(url + "/" + value["qa_id"], headers=headers)
            if final.json().get("status") not in {"queued", "running"}:
                break
            time.sleep(0.01)
        assert final.json()["status"] == "completed", final.text
    assert len(observed) == 2
    history = json.loads(observed[-1]["inputs"]["conversation_context"])["history"]
    assert history and history[0]["order_ids"] == ["ORDER-B-2"]
    assert (
        httpx.post(
            url,
            headers={**headers, "Authorization": "Bearer token-a"},
            json={"question": "继续", "conversation_id": conversation},
        ).status_code
        == 404
    )


def test_installer_invokes_real_cli_subprocess_without_recovering_running_jobs(
    prepared, tmp_path, monkeypatch
):
    import os
    import subprocess
    import sys
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path

    from tooling.test_environment_publication_install import publication as installer

    from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore
    from aiops_diagnostics.knowledge.knowledge_delegation import credential, identifier

    ctx, doc = prepared
    doc["expires_at"] = time.time() + 900
    routes = ChatRouteBindingStore(ctx.settings.chat_route_bindings_file)
    routes.retract(
        environment="env-a",
        tenant_id="TENANT-A",
        entry="consumer",
        scenario="consumer_service",
        expected_revision=1,
    )
    # 模拟中央只读目录HTTP；目标子进程使用真实委派客户端，不带Console key。
    scope = ("env-a", "TENANT-B", doc["workspace_id"], doc["apps"]["consumer"]["app_id"], "kb-own", 1.0)
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append(self.path)
            assert self.path == "/v1/dify/knowledge"
            assert self.headers["Authorization"] == "Bearer " + credential("synthetic-central", scope)
            body = json.dumps(
                {"knowledge_items": [{"knowledge_id": identifier(scope), "knowledge_base_ids": ["kb-own"]}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    knowledge = tmp_path / "private-knowledge/read.json"
    write_private_text(
        knowledge,
        json.dumps(
            {
                "schema": "aiops-knowledge-delegation/1",
                "base_url": f"http://127.0.0.1:{server.server_port}",
                "capabilities": [
                    {"knowledge_id": identifier(scope), "token": credential("synthetic-central", scope)}
                ],
            }
        ),
    )
    profiles = json.loads(Path(ctx.settings.operator_profiles_file).read_text())
    profiles["profiles"][0]["model"]["name"] = "synthetic-model"
    profiles["profiles"][0]["consumer_models"] = [{**profiles["profiles"][0]["model"], "name": "aiops-api"}]
    Path(ctx.settings.operator_profiles_file).write_text(json.dumps(profiles))
    qa = ctx.store.create_assistant_question("scope-existing", "existing in-flight", tenant_id="TENANT-A")
    state, app_root = tmp_path / "installer-state", tmp_path / "installer-root"
    (state / "gateway").mkdir(mode=0o700, parents=True)
    # 保留同一真实数据库，但按原安装器形状放到state/gateway；不可手工对齐data_home。
    import sqlite3

    db_target = state / "gateway/gateway.db"
    with sqlite3.connect(ctx.settings.database_file) as source, sqlite3.connect(db_target) as destination:
        source.backup(destination)
    db_target.chmod(0o600)
    (state / "keys").mkdir(mode=0o700)
    write_private_text(state / "keys" / (doc["apps"]["consumer"]["app_id"] + ".key"), "synthetic-app-key")
    app_root.mkdir(mode=0o700)
    (app_root / "publication-tasks" / doc["id"]).mkdir(mode=0o700, parents=True)
    installed = app_root / "installed.json"
    write_private_text(installed, json.dumps({"environment": "env-a"}))
    # 真实CLI里只有root fd/固定路径的宿主所有权断言用合成边界；版本/route/授权/Gateway合同不替换。
    harness = tmp_path / "cli-host-fixture.py"
    harness.write_text("""import os,sys
from pathlib import Path as ActualPath
from types import SimpleNamespace
from aiops_diagnostics import admin_cli
from typer.main import get_command
command=get_command(admin_cli.admin_app)
original_stat=os.fstat
original_path_stat=ActualPath.stat
original_symlink=ActualPath.is_symlink
def path_stat(self,*args,**kwargs):
 if str(self)=="/opt/aiops-access-env-a/installed.json":
  return original_path_stat(ActualPath(os.environ["SYNTHETIC_INSTALLED"]),*args,**kwargs)
 return original_path_stat(self,*args,**kwargs)
ActualPath.stat=path_stat
def is_symlink(self):
 if str(self)=="/opt/aiops-access-env-a/installed.json":return False
 return original_symlink(self)
ActualPath.is_symlink=is_symlink
fds={int(sys.argv[sys.argv.index(key)+1]) for key in ("--receipt-fd","--release-fd")}
def simulated_root_fd(fd):
 value=original_stat(fd)
 if fd not in fds:return value
 return SimpleNamespace(st_uid=0,st_mode=value.st_mode,st_nlink=value.st_nlink,
                        st_dev=value.st_dev,st_ino=value.st_ino)
os.fstat=simulated_root_fd
command()
""")
    env = {key: value for key, value in os.environ.items() if not key.startswith("AIOPS_")}
    env.update(
        PYTHONPATH=str(Path.cwd() / "src"),
        SYNTHETIC_INSTALLED=str(installed),
        AIOPS_GATEWAY_ENVIRONMENT="env-a",
        AIOPS_GATEWAY_EXECUTION_ENVIRONMENT="env-a",
        AIOPS_GATEWAY_SERVER_CONFIG_FILE=str(ctx.settings.server_config_file),
        AIOPS_GATEWAY_ENVIRONMENT_REGISTRY_FILE=ctx.settings.environment_registry_file,
        AIOPS_GATEWAY_OPERATOR_PROFILES_FILE=ctx.settings.operator_profiles_file,
        AIOPS_GATEWAY_DIFY_CHAT_BASE_URL=ctx.settings.dify_chat_base_url,
        AIOPS_GATEWAY_KNOWLEDGE_DELEGATION_FILE=str(knowledge),
    )

    def command(*args, **kwargs):
        start = args.index("environment-release")
        # 原environment_args中的env -i与路径派生被实际执行；仅runuser和root fd断言为宿主替身。
        environment_start = args.index("/usr/bin/env")
        cli = args.index(str(app_root / "current/venv/bin/aiops-admin"))
        original_env = list(args[environment_start:cli])
        result = subprocess.run(
            [
                *original_env,
                *[
                    key + "=" + value
                    for key, value in env.items()
                    if key.startswith("AIOPS_") or key in {"PYTHONPATH", "SYNTHETIC_INSTALLED"}
                ],
                sys.executable,
                str(harness),
                *args[start:],
            ],
            env={"PATH": os.environ["PATH"]},
            text=True,
            stderr=subprocess.STDOUT,
            timeout=30,
            stdout=subprocess.PIPE,
            **kwargs,
        )
        assert result.returncode == 0, result.stdout
        return result.stdout

    inst = SimpleNamespace(
        state=state,
        root=app_root,
        user="synthetic-service",
        environment="env-a",
        config=Path(ctx.settings.server_config_file).parent,
        environment_args=lambda root: installer.install.Installer.environment_args(inst, root),
        account=SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid()),
        command=command,
        config_snapshot_bytes=lambda raw, path: write_private_text(path, raw.decode()),
    )
    monkeypatch.setattr(installer.install, "trusted", lambda path, **kw: None)
    monkeypatch.setattr(installer.os, "chown", lambda *a: None)
    # 旧已受理作业的存储生命周期不可被短命CLI当重启恢复掉。
    try:
        prepared = installer.invoke(inst, doc, "prepare")
        derived = subprocess.check_output(
            [
                *installer.install.Installer.environment_args(inst, state),
                "PYTHONPATH=" + str(Path.cwd() / "src"),
                sys.executable,
                "-c",
                "import json;from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings;"
                "from aiops_diagnostics.gateway.environment_release_trial import document;"
                "from types import SimpleNamespace;s=GatewayServerSettings.from_env();"
                f"d=document(SimpleNamespace(settings=s),{doc['id']!r});"
                "print(json.dumps({'id':d['id'],'data_home':str(s.data_home)}))",
            ],
            text=True,
        )
        assert json.loads(derived)["data_home"] == str(state / "gateway")
        assert json.loads(derived)["id"] == doc["id"]
        verification = state / "gateway/environment-publications" / (doc["id"] + ".verification.json")
        write_private_text(verification, json.dumps({"operation": doc["id"]}))
        assert (
            verification.parent
            == release.record_path(SimpleNamespace(data_home=state / "gateway"), doc["id"]).parent
        )
        published = installer.invoke(inst, doc, "publish")
        assert published["routes"] == {"operator": 1, "consumer": 1}
        assert installer.invoke(inst, doc, "observe")["phase"] == "published"
        restored = installer.invoke(inst, doc, "restore")
        assert restored["restored_routes"] == {"operator": 2, "consumer": 2}
        from aiops_diagnostics.gateway.gateway_store import GatewayStore

        assert (
            GatewayStore(db_target).get_assistant_question(qa["qa_id"], "scope-existing")["status"]
            == qa["status"]
        )
        assert prepared["versions"] == restored["versions"] and calls
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
