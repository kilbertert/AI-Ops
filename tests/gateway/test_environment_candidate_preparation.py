"""D36：一次提交的持久候选准备，不替代原子任务的副作用对账。"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from dify.test_dify_ensure_workspace import upstream as workspace_upstream  # noqa: F401

from aiops_diagnostics.common.http_auth import build_internal_token_headers
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
