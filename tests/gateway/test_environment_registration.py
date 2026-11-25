"""D39：原管理HTTP续接安装结果；只读对账、过期/撤权、桥和互斥。"""

import json
import time

import pytest
from dify.test_dify_ensure_workspace import upstream as workspace_upstream  # noqa: F401

from aiops_diagnostics.common.http_auth import build_internal_token_headers
from aiops_diagnostics.gateway import gateway_db
from aiops_diagnostics.onboarding import drafts, plans, tasks
from aiops_diagnostics.onboarding import task_ssh_runner as worker
from gateway.test_candidate_knowledge_connection import connected  # noqa: F401
from gateway.test_environment_consumer import consumer_app  # noqa: F401
from gateway.test_environment_installation import installation_state, internal, submit  # noqa: F401
from gateway.test_environment_knowledge import knowledge  # noqa: F401
from gateway.test_environment_runtime import wiring  # noqa: F401
from gateway.test_environment_tasks import ADMIN, create, prepared, receipt  # noqa: F401


@pytest.fixture
def installed(installation_state):  # noqa: F811
    state = installation_state
    identifier = submit(state).json()["id"]
    owner = "a" * 32
    assert internal(state, {"installation_claim": True, "owner": owner}).json()["claimed"]
    stage = internal(state, {"installation": "stage", "id": identifier, "owner": owner}).json()
    result = dict(
        id=identifier,
        environment="a",
        scope="candidate-installation-only",
        state="observed",
        executable=False,
        activation="not_executed",
        registration="not_executed",
        business="unverified",
        installation="installed",
        version="1.35.0",
        config_sha256="a" * 64,
        registry_sha256="b" * 64,
        artifact_sha256=stage["archives"]["application"]["sha256"],
        runtime_sha256=stage["archives"]["runtime"]["sha256"],
        source_revision=stage["archives"]["application"]["revision"],
    )
    assert (
        internal(
            state, {"installation": "complete", "id": identifier, "owner": owner, "result": result}
        ).status_code
        == 200
    )
    with gateway_db.connection(drafts._database(state[0][0]), readonly=True) as conn:
        plan_id = json.loads(tasks._row(conn, identifier)["request"])["plan"]["id"]
    return state, identifier, plan_id


def registration(fixture):
    state, identifier, plan_id = fixture
    return state[0][2].post(
        "/v1/dify/management/environment-tasks",
        json={"installation_task": identifier, "registration_plan": plan_id},
    )


def test_registration_http_reuses_task_and_reconciles_without_second_write(installed):
    state, source, plan_id = installed
    # 原安装窗口届满不要求重装；输入相同的新计划提供本轮授权窗口。
    path, ref, *_ = state[0]
    with gateway_db.connection(drafts._database(path), write=True) as conn:
        conn.execute("UPDATE environment_plans SET expires_at=0")
        body = json.loads(tasks._row(conn, source)["request"])
        body["plan"]["expires_at"] = 0
        conn.execute("UPDATE environment_tasks SET request=? WHERE id=?", (json.dumps(body), source))
    assert registration(installed).status_code == 409
    plan = plans.preflight(path, ADMIN, "a", 1, 1, ref)
    response = registration((state, source, plan["id"]))
    assert response.status_code == 200, response.text
    identifier = response.json()["id"]
    assert registration((state, source, plan["id"])).json()["id"] == identifier
    owner = "b" * 32
    assert internal(state, {"installation_claim": True, "owner": owner}).json()["id"] == identifier
    payload = {"registration": "stage", "id": identifier, "owner": owner}
    stage = internal(state, payload).json()
    assert stage["observe_only"] is False
    assert internal(state, {**payload, "registration": "authorize"}).json() == {"authorized": True}
    assert internal(state, {**payload, "registration": "authorize"}).status_code == 409
    with gateway_db.connection(drafts._database(path), write=True) as conn:
        conn.execute("UPDATE environment_tasks SET lease_until=0 WHERE id=?", (identifier,))
    owner = "c" * 32
    assert internal(state, {"installation_claim": True, "owner": owner}).json()["id"] == identifier
    assert internal(state, {**payload, "owner": owner}).json()["observe_only"] is True
    result = dict(
        id=identifier,
        environment="a",
        tenant_id="same",
        scope="inactive-runtime-registration-only",
        state="observed",
        result="unchanged",
        executable=False,
        activated=False,
        business_acceptance=False,
        source_revision=stage["expected"]["revision"],
        **{k: v for k, v in stage["expected"].items() if k != "revision"},
    )
    complete = {"registration": "complete", "id": identifier, "owner": owner, "result": result}
    assert internal(state, {**complete, "result": {**result, "tenant_id": "wrong"}}).status_code == 409
    final = internal(state, complete)
    assert final.status_code == 200 and final.json()["state"] == "observed"
    assert final.json()["result"]["business_acceptance"] is False


@pytest.mark.parametrize("change", ["cancel", "expired", "draft", "revoke", "legacy"])
def test_registration_refuses_changed_authority_before_remote_write(installed, change):
    state, source, _ = installed
    if change == "legacy":
        with gateway_db.connection(drafts._database(state[0][0]), write=True) as conn:
            result = json.loads(tasks._row(conn, source)["result"])
            result["version"] = "1.34.0"
            conn.execute("UPDATE environment_tasks SET result=? WHERE id=?", (json.dumps(result), source))
        assert registration(installed).status_code == 403
        return
    response = registration(installed)
    assert response.status_code == 200, response.text
    identifier = response.json()["id"]
    path = state[0][0]
    if change == "cancel":
        state[0][2].post("/v1/dify/management/environment-tasks", json={"id": identifier, "cancel": True})
    elif change == "revoke":
        body = json.loads(path.read_text())
        body["managers"] = []
        path.write_text(json.dumps(body))
    else:
        with gateway_db.connection(drafts._database(path), write=True) as conn:
            request = json.loads(tasks._row(conn, identifier)["request"])
            request["plan"]["expires_at" if change == "expired" else "digest"] = (
                0 if change == "expired" else "f" * 64
            )
            conn.execute(
                "UPDATE environment_tasks SET request=? WHERE id=?", (json.dumps(request), identifier)
            )
    assert internal(state, {"installation_claim": True, "owner": "a" * 32}).json()["claimed"] is False


@pytest.mark.parametrize("dispatched", [False, True])
def test_cancelled_absent_registration_releases_scope_only_before_dispatch(installed, dispatched):
    state, source, _ = installed
    identifier = registration(installed).json()["id"]
    owner = "e" * 32
    internal(state, {"installation_claim": True, "owner": owner})
    payload = {"registration": "stage", "id": identifier, "owner": owner}
    stage = internal(state, payload).json()
    if dispatched:
        assert internal(state, {**payload, "registration": "authorize"}).status_code == 200
    state[0][2].post("/v1/dify/management/environment-tasks", json={"id": identifier, "cancel": True})
    result = dict(
        id=identifier,
        environment="a",
        tenant_id="same",
        scope="inactive-runtime-registration-only",
        state="reconcile",
        result="absent",
        executable=False,
        activated=False,
        business_acceptance=False,
        source_revision=stage["expected"]["revision"],
        **{k: v for k, v in stage["expected"].items() if k != "revision"},
    )
    final = internal(state, {**payload, "registration": "complete", "result": result}).json()
    assert final["state"] == ("reconcile" if dispatched else "cancelled")
    path, ref, *_ = state[0]
    with gateway_db.connection(drafts._database(path), write=True) as conn:
        conn.execute("UPDATE environment_plans SET expires_at=0")
    plan = plans.preflight(path, ADMIN, "a", 1, 1, ref)
    retry = registration((state, source, plan["id"]))
    assert retry.status_code == (403 if dispatched else 200)


@pytest.mark.parametrize("change", ["revoke", "draft"])
def test_dispatched_registration_recovers_frozen_target_after_authority_changes(installed, change):
    state = installed[0]
    identifier = registration(installed).json()["id"]
    owner = "b" * 32
    internal(state, {"installation_claim": True, "owner": owner})
    payload = {"registration": "stage", "id": identifier, "owner": owner}
    stage = internal(state, payload).json()
    assert internal(state, {**payload, "registration": "authorize"}).status_code == 200
    path = state[0][0]
    if change == "revoke":
        body = json.loads(path.read_text())
        body["managers"] = []
        path.write_text(json.dumps(body))
    else:
        with gateway_db.connection(drafts._database(path), write=True) as conn:
            conn.execute("UPDATE environment_drafts SET digest=? WHERE environment='a'", ("f" * 64,))
    with gateway_db.connection(drafts._database(path), write=True) as conn:
        conn.execute("UPDATE environment_tasks SET lease_until=0 WHERE id=?", (identifier,))
    owner = "c" * 32
    claim = internal(state, {"installation_claim": True, "owner": owner}).json()
    assert claim["claimed"] is True and claim["id"] == identifier
    observed = internal(state, {**payload, "owner": owner}).json()
    assert observed["observe_only"] is True and observed["expected"] == stage["expected"]
    assert internal(state, {**payload, "owner": owner, "registration": "authorize"}).status_code in {403, 409}
    result = dict(
        id=identifier,
        environment="a",
        tenant_id="same",
        scope="inactive-runtime-registration-only",
        state="observed",
        result="unchanged",
        executable=False,
        activated=False,
        business_acceptance=False,
        source_revision=stage["expected"]["revision"],
        **{k: v for k, v in stage["expected"].items() if k != "revision"},
    )
    final = internal(state, {**payload, "owner": owner, "registration": "complete", "result": result})
    assert final.status_code == 200 and final.json()["state"] == "observed"
    if change == "revoke":
        assert (
            state[0][2].post("/v1/dify/management/environment-tasks", json={"id": identifier}).status_code
            == 403
        )


@pytest.mark.parametrize("cancelled", [False, True])
def test_dispatched_absent_registration_exits_only_with_remote_intent_evidence(installed, cancelled):
    """已派发任务的 absent 出口必须凭 remote_intent 证据：无证据维持核查。

    首派死于登记意图落盘前（传输/锁瞬断）时，dispatched 已翻转而远端零
    副作用；observe-only 不能重放登记、spec 过期也堵死再派发——worker
    探测意图文件缺席后回报 remote_intent=absent，任务安全终态化。
    """
    state, source, _ = installed
    identifier = registration(installed).json()["id"]
    owner = "e" * 32
    internal(state, {"installation_claim": True, "owner": owner})
    payload = {"registration": "stage", "id": identifier, "owner": owner}
    stage = internal(state, payload).json()
    assert internal(state, {**payload, "registration": "authorize"}).status_code == 200
    if cancelled:
        state[0][2].post("/v1/dify/management/environment-tasks", json={"id": identifier, "cancel": True})
    result = dict(
        id=identifier,
        environment="a",
        tenant_id="same",
        scope="inactive-runtime-registration-only",
        state="reconcile",
        result="absent",
        executable=False,
        activated=False,
        business_acceptance=False,
        source_revision=stage["expected"]["revision"],
        **{k: v for k, v in stage["expected"].items() if k != "revision"},
    )

    def complete_with(body, status=200):
        # complete 后任务回到 checking；checking 按 60 秒节奏让位，故先推过节奏再重领。
        with gateway_db.connection(drafts._database(state[0][0]), write=True) as conn:
            checking = tasks._row(conn, identifier)["state"] == "checking"
            if checking:
                conn.execute(
                    "UPDATE environment_tasks SET updated_at=? WHERE id=?",
                    (time.time() - 61, identifier),
                )
        current_owner = owner
        if checking:
            claimed = internal(state, {"installation_claim": True, "owner": owner}).json()
            assert claimed["claimed"] and claimed["id"] == identifier, claimed
        response = internal(
            state, {**payload, "owner": current_owner, "registration": "complete", "result": body}
        )
        assert response.status_code == status, response.text
        return response

    # 没有远端意图证据的 absent 仍不释放已派发互斥。
    assert complete_with(result).json()["state"] == "reconcile"
    # remote_intent 只允许缺席值、且只能与 absent 结果同现。
    complete_with({**result, "remote_intent": "present"}, status=409)
    complete_with({**result, "result": "unconfirmed", "remote_intent": "absent"}, status=409)
    # 探针证明登记意图缺席 = 登记步从未越过首写：无取消判 blocked，有取消判 cancelled。
    final = complete_with({**result, "remote_intent": "absent"}).json()
    assert final["state"] == ("cancelled" if cancelled else "blocked")
    # 终态化释放互斥：新计划可重交同一登记。
    path, ref, *_ = state[0]
    with gateway_db.connection(drafts._database(path), write=True) as conn:
        conn.execute("UPDATE environment_plans SET expires_at=0")
    plan = plans.preflight(path, ADMIN, "a", 1, 1, ref)
    assert registration((state, source, plan["id"])).status_code == 200


def _unknown_registration(installed, *, cancelled=False):  # noqa: F811
    """把登记任务推进到 checking 的未知结果形，返回 (state, identifier, stage)。"""
    state, _, _ = installed
    path = state[0][0]
    identifier = registration(installed).json()["id"]
    owner = "e" * 32
    internal(state, {"installation_claim": True, "owner": owner})
    payload = {"registration": "stage", "id": identifier, "owner": owner}
    stage = internal(state, payload).json()
    assert internal(state, {**payload, "registration": "authorize"}).status_code == 200
    result = dict(
        id=identifier,
        environment="a",
        tenant_id="same",
        scope="inactive-runtime-registration-only",
        state="reconcile",
        result="unconfirmed",
        executable=False,
        activated=False,
        business_acceptance=False,
        source_revision=stage["expected"]["revision"],
        **{k: v for k, v in stage["expected"].items() if k != "revision"},
    )
    assert internal(state, {**payload, "registration": "complete", "result": result}).status_code == 200
    if cancelled:
        state[0][2].post("/v1/dify/management/environment-tasks", json={"id": identifier, "cancel": True})
    with gateway_db.connection(drafts._database(path), write=True) as conn:
        row = tasks._row(conn, identifier)
        assert row["state"] == "checking" and row["cancel"] == (1 if cancelled else 0)
        conn.execute("UPDATE environment_tasks SET lease_until=0 WHERE id=?", (identifier,))
    return state, identifier, stage


def _worker_round(state, tmp_path, monkeypatch, execute=None):
    """一次真实 worker.run（installation_claim 优先，未领到才落现场预检）。

    远端登记一轮复现真实回执：stage 后 complete 仍报 unconfirmed，于是该行在
    下一轮又回到 checking 并刷新 updated_at——这正是毒丸每周期抢到名额的机制。
    ``execute`` 可替换现场预检的交付替身（默认基于 receipt 的 observed 回执）。
    返回 (worker.run 结果, 本轮发往中央的载荷列表)。
    """
    client, settings = state[0][2], state[0][3]
    sent = []

    def central(root, target, payload):
        sent.append(dict(payload))
        headers = build_internal_token_headers(settings.environment_task_key, 300, int(time.time()))
        response = client.post("/v1/internal/environment-tasks", json=payload, headers=headers)
        assert response.status_code == 200, (payload, response.text)
        return response.json()

    def installation_run(root, request, identifier, owner, **kwargs):
        staged = request({"registration": "stage", "id": identifier, "owner": owner})
        assert staged["observe_only"] is True  # 已派发：只读回读，不重写
        request(
            {
                "registration": "complete",
                "id": identifier,
                "owner": owner,
                "result": dict(
                    id=identifier,
                    environment=staged["environment"],
                    tenant_id=staged["tenant_id"],
                    scope="inactive-runtime-registration-only",
                    state="reconcile",
                    result="unconfirmed",
                    executable=False,
                    activated=False,
                    business_acceptance=False,
                    source_revision=staged["expected"]["revision"],
                    **{k: v for k, v in staged["expected"].items() if k != "revision"},
                ),
            }
        )
        return {"id": identifier}

    monkeypatch.setattr(worker, "central_call", central)
    monkeypatch.setattr(worker.subprocess, "check_output", lambda *a, **kw: "")
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **kw: None)
    monkeypatch.setattr(worker, "installation_run", installation_run)
    monkeypatch.setattr(worker, "execute", execute or (lambda root, claim: receipt(claim["environment"])))
    return worker.run(tmp_path, "", "", central_target="41"), sent


def test_unknown_registration_yields_slot_so_queued_preflight_runs(installed, tmp_path, monkeypatch):  # noqa: F811
    """checking 登记按节奏让位；同环境排队的只读预检在有限轮次内被真实领到。

    事故形：登记回报 reconcile/unconfirmed → 落 checking；worker 每周期先
    installation_claim 并立即 return，同一条每轮抢到名额（每轮 stage+complete
    还刷新 updated_at），于是与它无关的独立环境现场预检（env b，queued、
    lease=0、完全可领）无人处理——单个 worker 是全局的，一个毒丸就饿死整条
    队列。让位与保护必须同时成立：节奏到期后 checking 行重新可领，互斥仍在。
    """
    state, identifier, _ = _unknown_registration(installed)
    queued, _ = create(state[0], "b")
    served = []
    base = time.time()
    for round_ in range(6):
        # 30 秒唤醒一次（timer 实际 20s；60 秒节奏跨两轮才到期）。
        monkeypatch.setattr(time, "time", lambda offset=round_ * 30: base + offset)
        outcome, _ = _worker_round(state, tmp_path, monkeypatch)
        if outcome is not None and outcome.get("id") == queued["id"]:
            served.append(outcome)
            break
    assert served, "queued site-preflight was never dispatched by the real worker"
    with gateway_db.connection(drafts._database(state[0][0]), readonly=True) as conn:
        row = tasks._row(conn, identifier)
    # 让位不等于终态化：未知登记仍待核对，未被伪造成成功、取消或过期。
    assert row["state"] == "checking" and row["cancel"] == 0


def _second_dispatched_registration(state, stage):
    """在 env b 手工载入另一条已派发 checking 登记（复现跨环境两条毒丸）。

    只复制一条已派发登记在当前账本里的形状：dispatched 让 current() 走冻结
    对账分支，从不因窗口/取消被拒——正是「长期可领」的来源。
    """
    identifier = "task_" + "9" * 32
    path = state[0][0]
    environment = "b"
    with gateway_db.connection(drafts._database(path), write=True) as conn:
        request = {
            "identity": ["control", "platform", "admin"],
            "plan": {
                "id": "plan_" + "9" * 32,
                "environment": environment,
                "digest": "9" * 64,
                "expires_at": time.time() + 900,
            },
            "target": {
                "id": "host-" + environment,
                "host": "managed-" + environment,
                "user": "aiops",
                "port": 18951,
            },
            "installer": {"revision": "a" * 40, "sha256": "a" * 64},
            "action": "candidate-register",
            "installation_task": "task_" + "8" * 32,
            "installed": {
                "source_revision": "a" * 40,
                **{
                    k: "b" * 64
                    for k in ("artifact_sha256", "config_sha256", "registry_sha256", "runtime_sha256")
                },
            },
            "tenant_id": "same",
            "attempted": True,
            "dispatched": True,
        }
        result = {
            "id": identifier,
            "environment": environment,
            "tenant_id": "same",
            "scope": "inactive-runtime-registration-only",
            "state": "reconcile",
            "result": "unconfirmed",
            "executable": False,
            "activated": False,
            "business_acceptance": False,
            "source_revision": stage["expected"]["revision"],
            "expires_at": time.time() + 900,
        }
        conn.execute(
            "INSERT OR IGNORE INTO environment_tasks"
            "(id,environment,request,state,result,owner,lease_until,cancel,updated_at) "
            "VALUES(?,?,?,'checking',?,'',0,0,?)",
            (identifier, environment, json.dumps(request), json.dumps(result), time.time() - 10_000),
        )
    return identifier


def test_multiple_checking_registrations_across_environments_still_serve_preflight(
    installed, tmp_path, monkeypatch
):  # noqa: F811
    """两条跨环境 checking 毒丸（都好领）不得合计永久占满 worker。

    这是 Devin 指出的残留：installation_claim 一领到就 return；A、B 各一条
    checking，每条都在冷却期外时，任意一条都能占住名额，queued 预检永远轮不到。
    现场预检是有限、只读、排队即领的队列：先给它名额即可保证轮次内必被处理，
    而两条 checking 仍是 checking、互斥仍在。
    """
    state, first, stage = _unknown_registration(installed)
    second = _second_dispatched_registration(state, stage)
    queued, _ = create(state[0], "b")
    served = None
    base = time.time()
    for round_ in range(4):
        monkeypatch.setattr(time, "time", lambda offset=round_ * 20: base + offset)
        outcome, _ = _worker_round(state, tmp_path, monkeypatch)
        if outcome is not None and outcome.get("id") == queued["id"]:
            served = outcome
            break
    assert served is not None, "queued site-preflight was never dispatched past two checking rows"
    with gateway_db.connection(drafts._database(state[0][0]), readonly=True) as conn:
        for identifier in (first, second):
            row = tasks._row(conn, identifier)
            assert row["state"] == "checking" and row["cancel"] == 0


def test_turn_alternates_so_perpetual_reconcile_never_starves_installation(installed, tmp_path, monkeypatch):  # noqa: F811
    """两条认领面严格交替：现场 reconcile 长期不还名额也不得饿死安装面。

    只做「现场优先」并不够：若现场面本身长期是 reconcile（远端一直待核对、跳过
    计划窗），它会对称地饿死安装登记。轮转游标让每次唤醒固定先领哪一侧，两侧都能
    在有限轮次内被服务。这里放多条永远不被排空的现场 reconcile 行，断言安装面
    仍能在有限轮次内领到。
    """
    state, registration_task, stage = _unknown_registration(installed)
    path = state[0][0]
    # 克隆一条真实现场预检请求，改造成 3 条 env b 的 reconcile 行（计划窗内、
    # reconciling 跳过窗口检查，故长期可领）。让交付替身对这些行抛传输错误，
    # 使它们每次都被判 reconcile（不排空），从而模拟「现场面永远还不了名额」。
    base_site, _ = create(state[0], "b")
    with gateway_db.connection(drafts._database(path), write=True) as conn:
        real = json.loads(tasks._row(conn, base_site["id"])["request"])
        conn.execute("DELETE FROM environment_tasks WHERE id=?", (base_site["id"],))
        for index in range(3):
            connector = "task_" + str(7 + index) * 32
            request = json.loads(json.dumps(real))
            conn.execute(
                "INSERT OR IGNORE INTO environment_tasks"
                "(id,environment,request,state,result,owner,lease_until,cancel,updated_at) "
                "VALUES(?,?,?,'reconcile',?,'',0,0,?)",
                (
                    connector,
                    "b",
                    json.dumps(request),
                    json.dumps({"expires_at": request["plan"]["expires_at"]}),
                    time.time() - 1000,
                ),
            )

    def execute(root, claim):
        # env b 的现场预检永远「未知」——每次都落回 reconcile，永不被排空。
        if claim["environment"] == "b":
            raise OSError("transport loss")
        return receipt(claim["environment"])

    base = time.time()
    served_installation = False
    for round_ in range(6):
        monkeypatch.setattr(time, "time", lambda offset=round_ * 20: base + offset)
        outcome, _ = _worker_round(state, tmp_path, monkeypatch, execute=execute)
        if outcome is not None and outcome.get("id") == registration_task:
            served_installation = True
            break
    assert served_installation, "installation registration was starved by perpetual site reconcile rows"
    # 毒丸登记仍是 checking：交替只调顺序，不解除任何保护。
    with gateway_db.connection(drafts._database(path), readonly=True) as conn:
        assert tasks._row(conn, registration_task)["state"] == "checking"


def test_unknown_registration_keeps_environment_mutex_against_new_conflicts(installed):  # noqa: F811
    """让位不得解除保护：未知登记的 checking 行仍拒绝同环境新的冲突安装。"""
    from aiops_diagnostics.onboarding import context as onboarding_context

    state, _, _ = _unknown_registration(installed)
    with gateway_db.connection(drafts._database(state[0][0]), write=True) as conn:
        parent = state[1]
        parent_request = json.loads(tasks._row(conn, parent)["request"])
        # 同一 BUSY 断言面（submit_cold / submit / validation 共用该查询形状）。
        with pytest.raises(tasks.TaskRefused, match="ENVIRONMENT_TASK_BUSY"):
            onboarding_context.submit_cold(
                conn, "a", ["control", "platform", "admin"], parent, parent_request
            )


@pytest.mark.parametrize("observe_only", [True, False])
def test_installation_run_authorizes_only_when_stage_is_not_observe_only(tmp_path, monkeypatch, observe_only):
    """真实 installation_run：stage 报 observe_only 时绝不发 authorize，否则必发。

    Devin 指出旧取消测试把 installation_run 整个替换掉，断言「没发 authorize」
    实际只测了替身自己。这里驱动真实函数：它按 stage 返回的 observe_only 决定
    是否发 authorize——已派发（observe_only=True）只读回读，未派发才授权写入。
    """
    import hashlib
    import subprocess

    from aiops_diagnostics.onboarding import task_ssh_runner as worker

    identifier = "task_" + "e" * 32
    owner = "a" * 32
    installer_bytes = b"fixed installer"
    staged = {
        "id": identifier,
        "environment": "a",
        "tenant_id": "same",
        "expires_at": time.time() + 900,
        "observe_only": observe_only,
        "target": {"host": "managed-a", "user": "aiops", "port": 2222},
        "installer": {"revision": "f" * 40, "sha256": hashlib.sha256(installer_bytes).hexdigest()},
        "expected": {
            "revision": "f" * 40,
            "artifact_sha256": "a" * 64,
            "config_sha256": "b" * 64,
            "registry_sha256": "c" * 64,
            "runtime_sha256": "d" * 64,
        },
    }
    (tmp_path / ".dev-host.toml").write_text('[[targets]]\nname="124"\nhost="managed-a"\n')
    calls = []

    def run(args, **kwargs):
        if args[0] == "bash":
            __import__("pathlib").Path(args[-1]).write_bytes(installer_bytes)
        return subprocess.CompletedProcess(args, 0)

    def output(args, **kwargs):
        raise subprocess.CalledProcessError(1, args)

    def request(payload):
        calls.append(payload.get("registration") or "complete")
        if payload["registration"] == "stage":
            return staged
        if payload["registration"] == "authorize":
            return {"authorized": True}
        return payload["result"]

    monkeypatch.setattr(worker.subprocess, "run", run)
    monkeypatch.setattr(worker.subprocess, "check_output", output)
    monkeypatch.setattr(worker, "registration_intent_absent", lambda *a, **kw: False)
    worker.installation_run(tmp_path, request, identifier, owner, registration=True)
    # stage 必发；authorize 只随 observe_only=False 发出。
    assert "stage" in calls
    assert ("authorize" in calls) is (not observe_only)


@pytest.mark.parametrize("returncode,expected", [(7, True), (0, False), (1, False), (9, False), (255, False)])
def test_registration_intent_absent_only_remote_answer_disarms(tmp_path, monkeypatch, returncode, expected):
    """登记意图探针只信远端脚本的 7=缺席；传输失败与锁占一律按在场收敛。"""
    from types import SimpleNamespace

    from aiops_diagnostics.onboarding import task_ssh_runner as worker

    sent = {}

    def run(*a, **kw):
        sent.update(kw)
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(worker, "subprocess", SimpleNamespace(run=run))
    identifier = "task_" + "0" * 32
    assert worker.registration_intent_absent(tmp_path, "95", "95", identifier) is expected
    # 先持安装锁再读意图文件：锁占期间远端 task.py 可能在写意图，不能当缺席。
    assert "/run/lock/aiops-access-95.lock" in sent["input"]
    assert "/opt/aiops-access-95/registration-tasks/" + identifier + ".json" in sent["input"]
    assert sent["input"].index("flock") < sent["input"].index("sys.exit(0 if")


@pytest.mark.parametrize("observe_only", [True, False])
def test_worker_registration_remote_failure_reports_absent_only_on_intent_probe(
    tmp_path, monkeypatch, observe_only
):
    """登记远端失败时 worker 先探意图：缺席才回报 absent+remote_intent 终态化。"""
    import hashlib
    import subprocess
    import time

    from aiops_diagnostics.onboarding import task_ssh_runner as worker

    identifier = "task_" + "c" * 32
    owner = "a" * 32
    installer_bytes = b"fixed installer"
    staged = {
        "id": identifier,
        "environment": "a",
        "tenant_id": "same",
        "expires_at": time.time() + 900,
        "observe_only": observe_only,
        "target": {"host": "managed-a", "user": "aiops", "port": 2222},
        "installer": {
            "revision": "f" * 40,
            "sha256": hashlib.sha256(installer_bytes).hexdigest(),
        },
        "expected": {
            "revision": "f" * 40,
            "artifact_sha256": "a" * 64,
            "config_sha256": "b" * 64,
            "registry_sha256": "c" * 64,
            "runtime_sha256": "d" * 64,
        },
    }
    (tmp_path / ".dev-host.toml").write_text('[[targets]]\nname="124"\nhost="managed-a"\n')
    completed = []

    def run(args, **kwargs):
        if args[0] == "bash":
            __import__("pathlib").Path(args[-1]).write_bytes(installer_bytes)
        return subprocess.CompletedProcess(args, 0)

    def output(args, **kwargs):
        raise subprocess.CalledProcessError(1, args)

    def request(payload):
        if payload["registration"] == "stage":
            return staged
        if payload["registration"] == "authorize":
            return {"authorized": True}
        completed.append(payload["result"])
        return payload["result"]

    monkeypatch.setattr(worker.subprocess, "run", run)
    monkeypatch.setattr(worker.subprocess, "check_output", output)
    monkeypatch.setattr(worker, "registration_intent_absent", lambda *a, **kw: True)
    result = worker.installation_run(tmp_path, request, identifier, owner, registration=True)
    assert result["result"] == "absent" and result["remote_intent"] == "absent"
    assert completed and completed[0]["remote_intent"] == "absent"

    completed.clear()
    monkeypatch.setattr(worker, "registration_intent_absent", lambda *a, **kw: False)
    result = worker.installation_run(tmp_path, request, identifier, owner, registration=True)
    assert result["result"] == "unconfirmed" and "remote_intent" not in result


@pytest.mark.parametrize("intent_absent", [True, False])
def test_worker_registration_remote_absent_gains_evidence_only_on_intent_probe(
    tmp_path, monkeypatch, intent_absent
):
    """远端正常回报 absent 时同样要凭意图探针补证据，否则 dispatched 维持核查。

    首派死于意图落盘前后续观察可无异常地回报 store-absent：store 为空本身
    不证明登记步零副作用，意图文件同缺才放行终态。
    """
    import hashlib
    import subprocess
    import time

    from aiops_diagnostics.onboarding import task_ssh_runner as worker

    identifier = "task_" + "d" * 32
    owner = "a" * 32
    installer_bytes = b"fixed installer"
    staged = {
        "id": identifier,
        "environment": "a",
        "tenant_id": "same",
        "expires_at": time.time() + 900,
        "observe_only": True,
        "target": {"host": "managed-a", "user": "aiops", "port": 2222},
        "installer": {
            "revision": "f" * 40,
            "sha256": hashlib.sha256(installer_bytes).hexdigest(),
        },
        "expected": {
            "revision": "f" * 40,
            "artifact_sha256": "a" * 64,
            "config_sha256": "b" * 64,
            "registry_sha256": "c" * 64,
            "runtime_sha256": "d" * 64,
        },
    }
    absent_result = {
        "id": identifier,
        "environment": "a",
        "tenant_id": "same",
        "scope": "inactive-runtime-registration-only",
        "state": "reconcile",
        "result": "absent",
        "executable": False,
        "activated": False,
        "business_acceptance": False,
        "source_revision": staged["expected"]["revision"],
        **{k: v for k, v in staged["expected"].items() if k != "revision"},
    }
    (tmp_path / ".dev-host.toml").write_text('[[targets]]\nname="124"\nhost="managed-a"\n')
    completed = []

    def run(args, **kwargs):
        if args[0] == "bash":
            __import__("pathlib").Path(args[-1]).write_bytes(installer_bytes)
        return subprocess.CompletedProcess(args, 0)

    def output(args, **kwargs):
        return json.dumps(absent_result)

    def request(payload):
        if payload["registration"] == "stage":
            return staged
        completed.append(payload["result"])
        return payload["result"]

    monkeypatch.setattr(worker.subprocess, "run", run)
    monkeypatch.setattr(worker.subprocess, "check_output", output)
    monkeypatch.setattr(worker, "registration_intent_absent", lambda *a, **kw: intent_absent)
    result = worker.installation_run(tmp_path, request, identifier, owner, registration=True)
    assert result["result"] == "absent"
    assert ("remote_intent" in result) is intent_absent
    # 意图未证缺席时补核登记资产（替身 run 返回退出码 0=在场）。
    assert ("remote_assets" in result) is not intent_absent
    assert completed and ("remote_intent" in completed[0]) is intent_absent
    assert ("remote_assets" in completed[0]) is not intent_absent


@pytest.mark.parametrize("assets", ["present", "absent"])
@pytest.mark.parametrize("verdict", ["unconfirmed", "absent"])
def test_cancelled_unknown_registration_exits_on_remote_asset_evidence(installed, assets, verdict):
    """已取消的未知登记凭远端资产事实终态化。

    登记资产（工作区与双端应用）在同一事务落盘：在场=原登记已提交，
    缺席=除意图文件外零写。两方向对已取消任务都是可证有界的副作用，
    不再为无法判决的远端观察永占互斥。意图在场时正常回报的 absent
    走同一条证据出口。
    """
    state, source, _ = installed
    _, identifier, stage = _unknown_registration(installed, cancelled=True)
    owner = "e" * 32
    result = dict(
        id=identifier,
        environment="a",
        tenant_id="same",
        scope="inactive-runtime-registration-only",
        state="reconcile",
        result=verdict,
        executable=False,
        activated=False,
        business_acceptance=False,
        source_revision=stage["expected"]["revision"],
        remote_assets=assets,
        **{k: v for k, v in stage["expected"].items() if k != "revision"},
    )

    def complete(body, status=200):
        with gateway_db.connection(drafts._database(state[0][0]), write=True) as conn:
            checking = tasks._row(conn, identifier)["state"] == "checking"
            if checking:
                conn.execute(
                    "UPDATE environment_tasks SET updated_at=? WHERE id=?",
                    (time.time() - 61, identifier),
                )
        if checking:
            internal(state, {"installation_claim": True, "owner": owner})
        response = internal(
            state,
            {"registration": "complete", "id": identifier, "owner": owner, "result": body},
        )
        assert response.status_code == status, response.text
        return response

    # 无资产证据不终态化；remote_assets 只允许与不可判结果同现且取值封闭。
    without = {k: v for k, v in result.items() if k != "remote_assets"}
    assert complete(without).json()["state"] == "reconcile"
    complete({**result, "remote_assets": "unknown"}, status=409)
    complete({**result, "state": "observed", "result": "unchanged"}, status=409)
    assert complete(result).json()["state"] == "cancelled"
    # 终态化释放互斥：新计划可重交同一登记。
    path, ref, *_ = state[0]
    with gateway_db.connection(drafts._database(path), write=True) as conn:
        conn.execute("UPDATE environment_plans SET expires_at=0")
    plan = plans.preflight(path, ADMIN, "a", 1, 1, ref)
    assert registration((state, source, plan["id"])).status_code == 200


def test_uncancelled_unknown_registration_holds_scope_despite_asset_evidence(installed):
    """未取消的未知登记不因资产证据放行：资产在场只证「原登记已提交」，
    不证本冻结目标登记的 verdict——互斥继续保持，证据随结果存档。"""
    state, source, _ = installed
    _, identifier, stage = _unknown_registration(installed)
    owner = "e" * 32
    result = dict(
        id=identifier,
        environment="a",
        tenant_id="same",
        scope="inactive-runtime-registration-only",
        state="reconcile",
        result="unconfirmed",
        executable=False,
        activated=False,
        business_acceptance=False,
        source_revision=stage["expected"]["revision"],
        remote_assets="present",
        **{k: v for k, v in stage["expected"].items() if k != "revision"},
    )
    with gateway_db.connection(drafts._database(state[0][0]), write=True) as conn:
        conn.execute(
            "UPDATE environment_tasks SET updated_at=? WHERE id=?",
            (time.time() - 61, identifier),
        )
    internal(state, {"installation_claim": True, "owner": owner})
    response = internal(
        state, {"registration": "complete", "id": identifier, "owner": owner, "result": result}
    )
    assert response.status_code == 200 and response.json()["state"] == "reconcile"
    path, ref, *_ = state[0]
    with gateway_db.connection(drafts._database(path), write=True) as conn:
        conn.execute("UPDATE environment_plans SET expires_at=0")
    plan = plans.preflight(path, ADMIN, "a", 1, 1, ref)
    assert registration((state, source, plan["id"])).status_code == 403


@pytest.mark.parametrize("returncode,expected", [(7, False), (0, True), (1, None), (9, None), (255, None)])
def test_registration_assets_present_only_remote_answer_counts(tmp_path, monkeypatch, returncode, expected):
    """登记资产探针只信远端脚本退出码：0=在场、7=缺席，其余一律未知。"""
    from types import SimpleNamespace

    sent = {}

    def run(*a, **kw):
        sent.update(kw)
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(worker, "subprocess", SimpleNamespace(run=run))
    assert worker.registration_assets_present(tmp_path, "95", "95", "same") is expected
    # 先持安装锁再读库：锁占期间远端登记事务可能在飞，不能当缺席。
    assert "/run/lock/aiops-access-95.lock" in sent["input"]
    assert "/var/lib/aiops-access-95/gateway/gateway.db" in sent["input"]
    assert sent["input"].index("flock") < sent["input"].index("is_file")


@pytest.mark.parametrize("assets,expected", [(True, "present"), (False, "absent"), (None, None)])
def test_worker_registration_remote_failure_reports_assets_only_when_probed(
    tmp_path, monkeypatch, assets, expected
):
    """意图在场（或探不出）时补报登记资产证据；资产探针读不出则不带字段。"""
    import hashlib
    import subprocess
    import time

    identifier = "task_" + "e" * 32
    owner = "a" * 32
    installer_bytes = b"fixed installer"
    staged = {
        "id": identifier,
        "environment": "a",
        "tenant_id": "same",
        "expires_at": time.time() + 900,
        "observe_only": True,
        "target": {"host": "managed-a", "user": "aiops", "port": 2222},
        "installer": {
            "revision": "f" * 40,
            "sha256": hashlib.sha256(installer_bytes).hexdigest(),
        },
        "expected": {
            "revision": "f" * 40,
            "artifact_sha256": "a" * 64,
            "config_sha256": "b" * 64,
            "registry_sha256": "c" * 64,
            "runtime_sha256": "d" * 64,
        },
    }
    (tmp_path / ".dev-host.toml").write_text('[[targets]]\nname="124"\nhost="managed-a"\n')

    def run(args, **kwargs):
        if args[0] == "bash":
            __import__("pathlib").Path(args[-1]).write_bytes(installer_bytes)
        return subprocess.CompletedProcess(args, 0)

    def output(args, **kwargs):
        raise subprocess.CalledProcessError(1, args)

    def request(payload):
        if payload["registration"] == "stage":
            return staged
        return payload["result"]

    monkeypatch.setattr(worker.subprocess, "run", run)
    monkeypatch.setattr(worker.subprocess, "check_output", output)
    monkeypatch.setattr(worker, "registration_intent_absent", lambda *a, **kw: False)
    monkeypatch.setattr(worker, "registration_assets_present", lambda *a, **kw: assets)
    result = worker.installation_run(tmp_path, request, identifier, owner, registration=True)
    assert result["result"] == "unconfirmed"
    assert ("remote_assets" in result) is (expected is not None)
    if expected:
        assert result["remote_assets"] == expected
