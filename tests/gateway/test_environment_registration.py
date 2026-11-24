"""D39：原管理HTTP续接安装结果；只读对账、过期/撤权、桥和互斥。"""

import json
import time

import pytest
from dify.test_dify_ensure_workspace import upstream as workspace_upstream  # noqa: F401

from aiops_diagnostics.common.http_auth import build_internal_token_headers
from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import environment_plans as plans
from aiops_diagnostics.gateway import environment_task_worker as worker
from aiops_diagnostics.gateway import environment_tasks as tasks
from aiops_diagnostics.gateway import gateway_db
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


def _worker_round(state, tmp_path, monkeypatch):
    """一次真实 worker.run（installation_claim 优先，未领到才落现场预检）。

    远端登记一轮复现真实回执：stage 后 complete 仍报 unconfirmed，于是该行在
    下一轮又回到 checking 并刷新 updated_at——这正是毒丸每周期抢到名额的机制。
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
    monkeypatch.setattr(worker, "execute", lambda root, claim: receipt(claim["environment"]))
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


def test_unknown_registration_keeps_environment_mutex_against_new_conflicts(installed):  # noqa: F811
    """让位不得解除保护：未知登记的 checking 行仍拒绝同环境新的冲突安装。"""
    from aiops_diagnostics.dify import environment_installation as installation

    state, _, _ = _unknown_registration(installed)
    with gateway_db.connection(drafts._database(state[0][0]), write=True) as conn:
        parent = state[1]
        parent_request = json.loads(tasks._row(conn, parent)["request"])
        # 同一 BUSY 断言面（submit_cold / submit / validation 共用该查询形状）。
        with pytest.raises(tasks.TaskRefused, match="ENVIRONMENT_TASK_BUSY"):
            installation.submit_cold(conn, "a", ["control", "platform", "admin"], parent, parent_request)


def test_cancelled_dispatched_registration_reads_back_without_authorizing(installed, tmp_path, monkeypatch):  # noqa: F811
    """已取消且已派发的登记只走冻结目标 observe-only，绝不再次 authorize 写权限。

    只要 dispatched 已翻转，远端就可能已越过首写：读回只能用 observe-only，
    结论仍由意图探针给（有证据才闭环，网络不确定维持待核）。这条路径此前被
    怀疑是 absent 读不回的真因，这里以真实 worker.run 钉住它的形状。
    """
    state, identifier, _ = _unknown_registration(installed, cancelled=True)
    # 让 60 秒节奏到期，checking 登记才重新可领（让位不解除保护，见上一测试）。
    base = time.time()
    monkeypatch.setattr(time, "time", lambda: base + 61)
    _, sent = _worker_round(state, tmp_path, monkeypatch)
    # stage 的 observe_only=True 是「已派发」的既有分界；worker 因此跳过 authorize。
    assert not any(payload.get("registration") == "authorize" for payload in sent)
    assert any(payload.get("registration") == "stage" for payload in sent)
    with gateway_db.connection(drafts._database(state[0][0]), readonly=True) as conn:
        assert tasks._row(conn, identifier)["state"] == "checking"


@pytest.mark.parametrize("returncode,expected", [(7, True), (0, False), (1, False), (9, False), (255, False)])
def test_registration_intent_absent_only_remote_answer_disarms(tmp_path, monkeypatch, returncode, expected):
    """登记意图探针只信远端脚本的 7=缺席；传输失败与锁占一律按在场收敛。"""
    from types import SimpleNamespace

    from aiops_diagnostics.gateway import environment_task_worker as worker

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

    from aiops_diagnostics.gateway import environment_task_worker as worker

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

    from aiops_diagnostics.gateway import environment_task_worker as worker

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
    assert completed and ("remote_intent" in completed[0]) is intent_absent
