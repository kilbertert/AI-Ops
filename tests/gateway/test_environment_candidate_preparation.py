"""D36：一次提交的持久候选准备，不替代原子任务的副作用对账。"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from dify.test_dify_ensure_workspace import upstream as workspace_upstream  # noqa: F401

from aiops_diagnostics.common.http_auth import build_internal_token_headers
from aiops_diagnostics.common.private_files import write_private_text
from aiops_diagnostics.dify import environment_candidate_preparation as chain
from aiops_diagnostics.dify import environment_operator
from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import environment_tasks as tasks
from aiops_diagnostics.gateway import gateway_db
from gateway.test_candidate_knowledge_connection import connected  # noqa: F401
from gateway.test_environment_consumer import consumer_app  # noqa: F401
from gateway.test_environment_knowledge import knowledge  # noqa: F401
from gateway.test_environment_tasks import ADMIN, create, prepared, receipt  # noqa: F401


@pytest.fixture
def candidate_chain(consumer_app, monkeypatch):  # noqa: F811
    fixture, _, settings, *_ = consumer_app[0]
    settings.operator_profiles_file = "synthetic"
    monkeypatch.setattr(environment_operator, "binding", lambda *_: "operator-binding")
    # The existing consumer fixture provides real synthetic HTTP KB/connection and Console.
    context = SimpleNamespace(settings=settings)
    # The reused source fixture prepared its KB before switching to the shared
    # synthetic Console endpoint; this test starts with the current binding.
    with gateway_db.connection(drafts._database(fixture[0]), write=True) as conn:
        for row in conn.execute("SELECT id,request FROM environment_tasks").fetchall():
            request = json.loads(row["request"])
            if request.get("action") == "knowledge-prepare" and request.get("resources"):
                request["resources"]["target_binding"] = chain.environment_management.knowledge_binding(
                    settings, target_only=True
                )
                request["resources"]["verified_binding"] = chain.environment_management.knowledge_binding(
                    settings
                )
                conn.execute(
                    "UPDATE environment_tasks SET request=? WHERE id=?", (json.dumps(request), row["id"])
                )
    job, plan = create(fixture)
    payload = {
        "candidate_plan": plan["id"],
        "tenant_id": "same",
        "source_app_id": "source-app",
        "source_version": 7,
    }
    return fixture, context, payload, consumer_app[2], consumer_app


def submit_http(state):
    return state[0][2].post("/v1/dify/management/environment-tasks", json=state[2])


def advance(state):
    # Registered original API context and this context share the same settings and files.
    return state[0][2].post(
        "/v1/internal/environment-tasks",
        json={"advance_candidates": True},
        headers=build_internal_token_headers(state[0][3].environment_task_key, 300, int(time.time())),
    )


def internal(state, payload):
    return state[0][2].post(
        "/v1/internal/environment-tasks",
        json=payload,
        headers=build_internal_token_headers(state[0][3].environment_task_key, 300, int(time.time())),
    )


def rows(state):
    with gateway_db.connection(drafts._database(state[0][0]), readonly=True) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM environment_tasks ORDER BY rowid")]


def request_of(state, identifier):
    return json.loads(next(row["request"] for row in rows(state) if row["id"] == identifier))


def finish_child(state, action, identifier):
    owner = "d" * 32
    path, reference, *_ = state[0]
    claimed = tasks.worker(path, reference, {"id": identifier, "owner": owner})
    if not claimed["claimed"]:
        return claimed["task"]
    if action == "site-preflight":
        return tasks.worker(path, reference, {"id": identifier, "owner": owner, "result": receipt("a")})
    if action == "operator-prepare":
        # Operator template differs from consumer; existing dedicated operator suite exercises its import.
        request = tasks.workspace_request(path, reference, identifier, owner, action=action)
        return tasks.finish_workspace(
            path,
            reference,
            identifier,
            owner,
            {
                "state": "observed",
                "scope": "candidate-operator-application-only",
                "environment": "a",
                "tenant_id": "same",
                "workspace_id": request["workspace_id"],
                "knowledge_base_id": request["knowledge_base_id"],
                "app_id": "synthetic-operator",
                "executable": False,
                "expires_at": time.time() + 900,
            },
        )
    response = state[0][2].post(
        "/v1/internal/environment-tasks",
        json={
            "id": identifier,
            "owner": owner,
            "prepare": {
                "workspace-prepare": "workspace",
                "knowledge-prepare": "knowledge",
                "knowledge-connect": "connection",
                "consumer-prepare": "consumer",
            }[action],
        },
        headers=build_internal_token_headers(state[0][3].environment_task_key, 300, int(time.time())),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_parent_is_persistent_and_existing_resource_tasks_are_reused(candidate_chain):
    state = candidate_chain
    first = submit_http(state)
    assert first.status_code == 200, first.text
    identifier = first.json()["id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        repeated = list(pool.map(lambda _: chain.submit(state[1], ADMIN, state[2], state[3]), range(8)))
    assert {r["id"] for r in repeated} == {identifier}
    assert first.json()["action"] == "candidate-prepare" and first.json()["executable"] is False
    assert (
        state[0][2].post("/v1/internal/environment-tasks", json={"advance_candidates": True}).status_code
        != 200
    )
    for action in chain.STEPS:
        assert advance(state).status_code == 200
        child = request_of(state, identifier)["children"][action]
        finished = finish_child(state, action, child)
        assert finished["state"] == "observed", (action, finished)
        # Repeated advancement (as after a process restart) never duplicates the child row.
        count = len(rows(state))
        assert advance(state).status_code == 200
        after = request_of(state, identifier)["children"]
        assert after[action] == child and len(rows(state)) <= count + 1
    parent = state[0][2].post("/v1/dify/management/environment-tasks", json={"id": identifier}).json()
    assert parent["state"] == "observed"
    assert parent["result"]["activation"] == "not_executed" and parent["result"]["business"] == "unverified"
    assert [s["action"] for s in parent["result"]["steps"]] == list(chain.STEPS)
    apps = state[4][3]
    assert len(apps) == 1 and all(
        a["enable_api"] is False and a["enable_site"] is False for a in apps.values()
    )


@pytest.mark.parametrize("failure", ["cancel", "drift", "credentials", "expired", "bad-tenant"])
def test_failed_premise_never_enqueues_successor(candidate_chain, failure):
    state = candidate_chain
    if failure == "bad-tenant":
        state[2]["tenant_id"] = "not-registered"
        count = len(rows(state))
        assert submit_http(state).status_code == 403 and len(rows(state)) == count
        return
    parent = submit_http(state).json()
    identifier = parent["id"]
    site = request_of(state, identifier)["children"]["site-preflight"]
    finish_child(state, "site-preflight", site)
    if failure == "cancel":
        state[0][2].post("/v1/dify/management/environment-tasks", json={"id": identifier, "cancel": True})
    elif failure == "credentials":
        state[0][3].dify_inner_api_key = "changed"
    else:
        with gateway_db.connection(drafts._database(state[0][0]), write=True) as conn:
            if failure == "drift":
                conn.execute("UPDATE environment_drafts SET revision=revision+1 WHERE environment=?", ("a",))
            else:
                row = request_of(state, identifier)
                row["plan"]["expires_at"] = 0
                conn.execute(
                    "UPDATE environment_tasks SET request=? WHERE id=?", (json.dumps(row), identifier)
                )
    count = len(rows(state))
    assert advance(state).status_code == 200
    assert len(rows(state)) == count
    assert state[0][2].post("/v1/dify/management/environment-tasks", json={"id": identifier}).json()[
        "state"
    ] in ("blocked", "cancelled", "expired")


def test_parent_never_takes_worker_lease_and_cancel_stops_owned_resources(candidate_chain):
    state = candidate_chain
    # This branch has no workspace task yet; other tests cover reusing one.
    with gateway_db.connection(drafts._database(state[0][0]), write=True) as conn:
        conn.execute(
            "DELETE FROM environment_tasks WHERE json_extract(request,'$.action')='workspace-prepare'"
        )
    parent = submit_http(state).json()
    identifier = parent["id"]
    site = request_of(state, identifier)["children"]["site-preflight"]
    finish_child(state, "site-preflight", site)
    advance(state)
    workspace = request_of(state, identifier)["children"]["workspace-prepare"]
    assert tasks.worker(state[0][0], state[0][1], {"id": workspace, "owner": "e" * 32})["id"] == workspace
    with pytest.raises(tasks.TaskRefused):
        tasks.worker(state[0][0], state[0][1], {"id": identifier, "owner": "f" * 32})
    state[0][2].post("/v1/dify/management/environment-tasks", json={"id": identifier, "cancel": True})
    assert next(r for r in rows(state) if r["id"] == workspace)["cancel"] == 1
    assert next(r for r in rows(state) if r["id"] == site)["cancel"] == 0


def test_legacy_worker_without_candidates_keeps_working(prepared, tmp_path, monkeypatch):  # noqa: F811
    from aiops_diagnostics.gateway import environment_task_worker as worker

    path, ref, client, settings = prepared
    context = SimpleNamespace(settings=settings)
    job, _ = create(prepared)
    monkeypatch.setattr(worker.subprocess, "check_output", lambda *a, **kw: "")
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **kw: None)
    monkeypatch.setattr(
        worker,
        "central_call",
        lambda root, target, payload: (
            chain.advance(context, payload)
            if "advance_candidates" in payload
            else {"claimed": False, "task": None}
            if payload.get("installation_claim") is True
            else tasks.worker(path, ref, payload)
        ),
    )
    monkeypatch.setattr(worker, "execute", lambda root, claim: receipt(claim["environment"]))
    assert worker.run(tmp_path, "", "", central_target="41")["id"] == job["id"]


def test_response_loss_reconciles_same_child_before_next_step(candidate_chain):
    state = candidate_chain
    parent = submit_http(state).json()["id"]
    site = request_of(state, parent)["children"]["site-preflight"]
    finish_child(state, "site-preflight", site)
    for action in ("workspace-prepare", "knowledge-prepare", "knowledge-connect", "operator-prepare"):
        advance(state)
        finish_child(state, action, request_of(state, parent)["children"][action])
    advance(state)
    child = request_of(state, parent)["children"]["consumer-prepare"]
    state[4][6][0] = True
    assert finish_child(state, "consumer-prepare", child)["state"] == "reconcile"
    count = len(rows(state))
    for _ in range(3):
        assert (
            chain.advance(SimpleNamespace(settings=state[0][3]), {"advance_candidates": True})["advanced"]
            == 1
        )
    assert len(rows(state)) == count and request_of(state, parent)["children"]["consumer-prepare"] == child
    state[4][6][0] = False
    assert finish_child(state, "consumer-prepare", child)["state"] == "observed"
    advance(state)
    final = state[0][2].post("/v1/dify/management/environment-tasks", json={"id": parent}).json()
    assert final["state"] == "observed" and len(state[4][3]) == 1


def test_initial_installation_is_explicit_and_installs_before_preflight(candidate_chain, tmp_path):
    state = candidate_chain
    data_home = tmp_path / "cold-data"
    data_home.mkdir(mode=0o700)
    state[0][3].data_home = data_home
    # 「包含初装」只认显式true；其他取值与默认提交一样按无效载荷拒绝。
    for value in (False, "yes", 1):
        state[2]["initial_installation"] = value
        assert submit_http(state).status_code != 200
    state[2]["initial_installation"] = True
    parent = submit_http(state)
    assert parent.status_code == 200, parent.text
    identifier = parent.json()["id"]
    again = submit_http(state)
    assert again.status_code == 200 and again.json()["id"] == identifier
    children = request_of(state, identifier)["children"]
    assert set(children) == {"site-install"}
    cold = children["site-install"]
    owner = "c" * 32
    # 初装属安装面租约；现场/旧worker路径不能认领或代写结果。
    with pytest.raises(tasks.TaskRefused):
        tasks.worker(state[0][0], state[0][1], {"id": cold, "owner": owner})
    claim = internal(state, {"installation_claim": True, "owner": owner})
    assert claim.status_code == 200 and claim.json()["id"] == cold
    assert claim.json()["action"] == "site-install"
    stage = internal(state, {"installation": "stage", "id": cold, "owner": owner})
    assert stage.status_code == 200, stage.text
    body = stage.json()
    assert set(body["archives"]) == {"application", "candidate"}
    assert "expected" not in body and body["observe_only"] is False
    assert body["expires_at"] > time.time() and body["python"] in ("3.11", "3.12", "3.13")
    # 初装未完成前父任务不创建现场预检，旧计划阻塞行也不可能被复用。
    assert advance(state).status_code == 200
    assert "site-preflight" not in request_of(state, identifier)["children"]
    authorize = internal(state, {"installation": "authorize", "id": cold, "owner": owner})
    assert authorize.json() == {"authorized": True}
    result = {
        "id": cold,
        "environment": "a",
        "scope": "site-cold-installation-only",
        "state": "observed",
        "installation": "installed",
        "executable": False,
        "version": "1.2.3",
        "source_revision": body["archives"]["application"]["revision"],
        "artifact_sha256": body["archives"]["application"]["sha256"],
        "config_sha256": "c" * 64,
        "registry_sha256": "d" * 64,
    }
    for malformed in (
        {**result, "installation": "not_installed"},
        {**result, "state": "observed", "installation": "installed", "artifact_sha256": "e" * 64},
        {**result, "scope": "candidate-installation-only"},
    ):
        assert (
            internal(
                state, {"installation": "complete", "id": cold, "owner": owner, "result": malformed}
            ).status_code
            != 200
        ), malformed
    done = internal(state, {"installation": "complete", "id": cold, "owner": owner, "result": result})
    assert done.status_code == 200 and done.json()["state"] == "observed", done.text
    assert advance(state).status_code == 200
    children = request_of(state, identifier)["children"]
    site = children["site-preflight"]
    assert request_of(state, site).get("after") == cold
    finished = finish_child(state, "site-preflight", site)
    assert finished["state"] == "observed", finished
    assert advance(state).status_code == 200
    workspace = request_of(state, identifier)["children"]["workspace-prepare"]
    assert finish_child(state, "workspace-prepare", workspace)["state"] == "observed"
    steps = (
        state[0][2]
        .post("/v1/dify/management/environment-tasks", json={"id": identifier})
        .json()["result"]["steps"]
    )
    assert [s["action"] for s in steps][:2] == ["site-install", "site-preflight"]
    # 基础初装结果独立成字段；父任务自身 runtime 安装字段仍保持原语义。
    parent_result = (
        state[0][2].post("/v1/dify/management/environment-tasks", json={"id": identifier}).json()["result"]
    )
    assert parent_result["initial_installation"] == "installed"
    assert parent_result["installation"] == "not_executed"


def test_cold_submission_initializes_task_store_on_fresh_database(candidate_chain):
    state = candidate_chain
    # 真实新库：草稿与计划表在场而任务表从未创建；预建任务fixture覆盖不了此缺口。
    with gateway_db.connection(drafts._database(state[0][0]), write=True) as conn:
        conn.execute("DROP TABLE environment_tasks")
    state[2]["initial_installation"] = True
    response = submit_http(state)
    assert response.status_code == 200, response.text
    children = request_of(state, response.json()["id"])["children"]
    assert set(children) == {"site-install"}


@pytest.mark.parametrize("terminal", ["cancel", "expired"])
def test_dispatched_cold_only_observes_frozen_result_without_reviving_parent(
    candidate_chain, terminal, tmp_path
):
    state = candidate_chain
    data_home = tmp_path / "cold-data"
    data_home.mkdir(mode=0o700)
    state[0][3].data_home = data_home
    state[2]["initial_installation"] = True
    parent = submit_http(state).json()["id"]
    cold = request_of(state, parent)["children"]["site-install"]
    owner = "c" * 32
    assert internal(state, {"installation_claim": True, "owner": owner}).json()["id"] == cold
    stage = internal(state, {"installation": "stage", "id": cold, "owner": owner})
    assert stage.status_code == 200 and stage.json()["observe_only"] is False
    assert internal(state, {"installation": "authorize", "id": cold, "owner": owner}).json() == {
        "authorized": True
    }
    if terminal == "cancel":
        state[0][2].post("/v1/dify/management/environment-tasks", json={"id": parent, "cancel": True})
    else:
        with gateway_db.connection(drafts._database(state[0][0]), write=True) as conn:
            request = request_of(state, parent)
            request["plan"]["expires_at"] = 0
            conn.execute("UPDATE environment_tasks SET request=? WHERE id=?", (json.dumps(request), parent))
        assert advance(state).status_code == 200
        assert next(r for r in rows(state) if r["id"] == parent)["state"] == "blocked"
    # fresh授权对取消/过期仍硬拒；已派发任务租约到期重领只能观察。
    assert internal(state, {"installation": "authorize", "id": cold, "owner": owner}).status_code != 200
    with gateway_db.connection(drafts._database(state[0][0]), write=True) as conn:
        conn.execute("UPDATE environment_tasks SET lease_until=0 WHERE id=?", (cold,))
    claim = internal(state, {"installation_claim": True, "owner": owner})
    assert claim.status_code == 200 and claim.json()["id"] == cold, claim.text
    stage = internal(state, {"installation": "stage", "id": cold, "owner": owner})
    assert stage.status_code == 200 and stage.json()["observe_only"] is True
    result = {
        "id": cold,
        "environment": "a",
        "scope": "site-cold-installation-only",
        "state": "observed",
        "installation": "installed",
        "executable": False,
        "version": "1.2.3",
        "source_revision": stage.json()["archives"]["application"]["revision"],
        "artifact_sha256": stage.json()["archives"]["application"]["sha256"],
        "config_sha256": "c" * 64,
        "registry_sha256": "d" * 64,
    }
    done = internal(state, {"installation": "complete", "id": cold, "owner": owner, "result": result})
    assert done.status_code == 200, done.text
    assert done.json()["state"] == ("cancelled" if terminal == "cancel" else "observed")
    row = next(r for r in rows(state) if r["id"] == parent)
    assert row["state"] == ("cancelled" if terminal == "cancel" else "blocked")
    # 已验证cold回执同事务回写父摘要：advance不再选中终态父，initial_installation
    # 与site-install步状态仍须读出真实安装结果；终态/children/后继不动。
    view = state[0][2].post("/v1/dify/management/environment-tasks", json={"id": parent}).json()
    assert view["result"]["initial_installation"] == "installed"
    step = next(s for s in view["result"]["steps"] if s["id"] == cold)
    assert step["action"] == "site-install" and step["state"] == done.json()["state"]
    assert "site-preflight" not in request_of(state, parent)["children"]


@pytest.mark.parametrize("mutation", ["python", "target"])
def test_dispatched_cold_observe_reclaims_with_frozen_python(candidate_chain, mutation, tmp_path):
    state = candidate_chain
    data_home = tmp_path / "cold-data"
    data_home.mkdir(mode=0o700)
    state[0][3].data_home = data_home
    state[2]["initial_installation"] = True
    parent = submit_http(state).json()["id"]
    cold = request_of(state, parent)["children"]["site-install"]
    owner = "c" * 32
    assert internal(state, {"installation_claim": True, "owner": owner}).json()["id"] == cold
    stage = internal(state, {"installation": "stage", "id": cold, "owner": owner})
    assert stage.status_code == 200 and stage.json()["python"] == "3.12", stage.text
    assert internal(state, {"installation": "authorize", "id": cold, "owner": owner}).json() == {
        "authorized": True
    }
    # 已派发后现场引用漂移/移除：观察回收只能使用提交时冻结的python。
    ref = json.loads(state[0][1].read_text())
    if mutation == "python":
        next(t for t in ref["targets"] if t["id"] == "host-a")["python"] = "3.13"
    else:
        ref["targets"] = [t for t in ref["targets"] if t["id"] != "host-a"]
    write_private_text(state[0][1], json.dumps(ref))
    with gateway_db.connection(drafts._database(state[0][0]), write=True) as conn:
        conn.execute("UPDATE environment_tasks SET lease_until=0 WHERE id=?", (cold,))
    claim = internal(state, {"installation_claim": True, "owner": owner})
    assert claim.status_code == 200 and claim.json()["id"] == cold, claim.text
    stage = internal(state, {"installation": "stage", "id": cold, "owner": owner})
    assert stage.status_code == 200 and stage.json()["observe_only"] is True, stage.text
    assert stage.json()["python"] == "3.12"
    result = {
        "id": cold,
        "environment": "a",
        "scope": "site-cold-installation-only",
        "state": "observed",
        "installation": "installed",
        "executable": False,
        "version": "1.2.3",
        "source_revision": stage.json()["archives"]["application"]["revision"],
        "artifact_sha256": stage.json()["archives"]["application"]["sha256"],
        "config_sha256": "c" * 64,
        "registry_sha256": "d" * 64,
    }
    done = internal(state, {"installation": "complete", "id": cold, "owner": owner, "result": result})
    assert done.status_code == 200 and done.json()["state"] == "observed", done.text


@pytest.mark.parametrize("mutation", ["python", "target"])
def test_fresh_cold_claim_rejects_reference_drift(candidate_chain, mutation, tmp_path):
    state = candidate_chain
    data_home = tmp_path / "cold-data"
    data_home.mkdir(mode=0o700)
    state[0][3].data_home = data_home
    state[2]["initial_installation"] = True
    parent = submit_http(state).json()["id"]
    cold = request_of(state, parent)["children"]["site-install"]
    ref = json.loads(state[0][1].read_text())
    if mutation == "python":
        next(t for t in ref["targets"] if t["id"] == "host-a")["python"] = "3.13"
    else:
        ref["targets"] = [t for t in ref["targets"] if t["id"] != "host-a"]
    write_private_text(state[0][1], json.dumps(ref))
    claim = internal(state, {"installation_claim": True, "owner": "c" * 32})
    assert claim.status_code == 200 and claim.json()["claimed"] is False, claim.text
    row = next(r for r in rows(state) if r["id"] == cold)
    assert row["state"] == "blocked" and row["error"] == "ENVIRONMENT_TASK_OBSERVATION_REQUIRED"


def test_checking_cold_paces_reclaim_and_yields_independent_site_queue(candidate_chain, tmp_path):
    state = candidate_chain
    data_home = tmp_path / "cold-data"
    data_home.mkdir(mode=0o700)
    state[0][3].data_home = data_home
    state[2]["initial_installation"] = True
    parent = submit_http(state).json()["id"]
    cold = request_of(state, parent)["children"]["site-install"]
    owner = "c" * 32
    assert internal(state, {"installation_claim": True, "owner": owner}).json()["id"] == cold
    assert internal(state, {"installation": "stage", "id": cold, "owner": owner}).status_code == 200
    assert internal(state, {"installation": "authorize", "id": cold, "owner": owner}).status_code == 200
    reconcile = {
        "id": cold,
        "environment": "a",
        "scope": "site-cold-installation-only",
        "state": "reconcile",
        "installation": "unconfirmed",
        "executable": False,
        "error_code": "ENVIRONMENT_INSTALLATION_RECOVERY_REQUIRED",
    }
    done = internal(state, {"installation": "complete", "id": cold, "owner": owner, "result": reconcile})
    assert done.status_code == 200 and done.json()["state"] == "reconcile", done.text
    # 另一环境普通现场预检排队；残迹cold冷却内不能立即重领占住安装认领面。
    other, _ = create(state[0], "b")
    claim = internal(state, {"installation_claim": True, "owner": "e" * 32})
    assert claim.status_code == 200 and claim.json()["claimed"] is False, claim.text
    # root复验输入：取消级联后cold为checking+cancel=1，两轮认领仍不得抢占。
    state[0][2].post("/v1/dify/management/environment-tasks", json={"id": parent, "cancel": True})
    assert next(r for r in rows(state) if r["id"] == cold)["cancel"] == 1
    for _ in range(2):
        claim = internal(state, {"installation_claim": True, "owner": "e" * 32})
        assert claim.status_code == 200 and claim.json()["claimed"] is False, claim.text
    seen = set()
    for _ in range(2):
        claimed = tasks.worker(state[0][0], state[0][1], {"owner": "e" * 32})
        if claimed.get("claimed"):
            seen.add(claimed["id"])
    assert other["id"] in seen
    # 非cold检查任务沿原即领合同：被选中后按其自身门禁对账，不受新节奏限频。
    with gateway_db.connection(drafts._database(state[0][0]), write=True) as conn:
        for served in seen - {other["id"]}:
            conn.execute(
                "UPDATE environment_tasks SET state='queued',owner='',lease_until=0 WHERE id=?",
                (served,),
            )
        source = conn.execute(
            "SELECT id,request FROM environment_tasks WHERE state='observed' LIMIT 1"
        ).fetchone()
        victim_request = json.loads(source["request"])
        victim_request["action"] = "candidate-validate"
        conn.execute(
            "UPDATE environment_tasks SET state='checking',request=?,owner='',lease_until=0 WHERE id=?",
            (json.dumps(victim_request), source["id"]),
        )
    claim = internal(state, {"installation_claim": True, "owner": "e" * 32})
    assert claim.status_code == 200 and claim.json()["claimed"] is False, claim.text
    assert next(r for r in rows(state) if r["id"] == source["id"])["state"] == "blocked"
    # 冷却结束仍恢复观察循环；取消任务observed回执照常收敛终态。
    with gateway_db.connection(drafts._database(state[0][0]), write=True) as conn:
        conn.execute("UPDATE environment_tasks SET updated_at=? WHERE id=?", (time.time() - 61, cold))
    again = internal(state, {"installation_claim": True, "owner": "f" * 32})
    assert again.status_code == 200 and again.json()["id"] == cold
    stage = internal(state, {"installation": "stage", "id": cold, "owner": "f" * 32})
    assert stage.status_code == 200 and stage.json()["observe_only"] is True
    result = {
        "id": cold,
        "environment": "a",
        "scope": "site-cold-installation-only",
        "state": "observed",
        "installation": "installed",
        "executable": False,
        "version": "1.2.3",
        "source_revision": stage.json()["archives"]["application"]["revision"],
        "artifact_sha256": stage.json()["archives"]["application"]["sha256"],
        "config_sha256": "c" * 64,
        "registry_sha256": "d" * 64,
    }
    done = internal(state, {"installation": "complete", "id": cold, "owner": "f" * 32, "result": result})
    assert done.status_code == 200 and done.json()["state"] == "cancelled", done.text


def test_cold_complete_accepts_absent_registry_only_as_already_installed(candidate_chain, tmp_path):
    state = candidate_chain
    data_home = tmp_path / "cold-data"
    data_home.mkdir(mode=0o700)
    state[0][3].data_home = data_home
    state[2]["initial_installation"] = True
    parent = submit_http(state).json()["id"]
    cold = request_of(state, parent)["children"]["site-install"]
    owner = "c" * 32
    assert internal(state, {"installation_claim": True, "owner": owner}).json()["id"] == cold
    stage = internal(state, {"installation": "stage", "id": cold, "owner": owner})
    assert stage.status_code == 200
    archives = stage.json()["archives"]
    assert internal(state, {"installation": "authorize", "id": cold, "owner": owner}).status_code == 200
    result = {
        "id": cold,
        "environment": "a",
        "scope": "site-cold-installation-only",
        "state": "observed",
        "installation": "already_installed",
        "executable": False,
        "version": "1.2.3",
        "source_revision": archives["application"]["revision"],
        "artifact_sha256": archives["application"]["sha256"],
        "config_sha256": "c" * 64,
        "registry_sha256": "absent",
    }
    # absent只属于旧config-only合法安装；新装形状与缺键仍须强摘要。
    missing = {k: v for k, v in result.items() if k != "registry_sha256"}
    for malformed in (missing, {**result, "installation": "installed"}):
        assert (
            internal(
                state,
                {"installation": "complete", "id": cold, "owner": owner, "result": malformed},
            ).status_code
            != 200
        ), malformed
    done = internal(state, {"installation": "complete", "id": cold, "owner": owner, "result": result})
    assert done.status_code == 200 and done.json()["state"] == "observed", done.text
    stored = json.loads(next(r["result"] for r in rows(state) if r["id"] == cold))
    assert stored["registry_sha256"] == "absent" and stored["installation"] == "already_installed"


def test_cancel_completed_parent_keeps_observed_resource_tasks(candidate_chain):
    state = candidate_chain
    parent = submit_http(state).json()["id"]
    site = request_of(state, parent)["children"]["site-preflight"]
    finish_child(state, "site-preflight", site)
    advance(state)
    workspace = request_of(state, parent)["children"]["workspace-prepare"]
    finish_child(state, "workspace-prepare", workspace)
    state[0][2].post("/v1/dify/management/environment-tasks", json={"id": parent, "cancel": True})
    child = next(row for row in rows(state) if row["id"] == workspace)
    assert child["state"] == "observed" and child["cancel"] == 0
