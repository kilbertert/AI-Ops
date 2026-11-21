"""D39：原管理HTTP续接安装结果；只读对账、过期/撤权、桥和互斥。"""

import json

import pytest
from dify.test_dify_ensure_workspace import upstream as workspace_upstream  # noqa: F401

from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import environment_plans as plans
from aiops_diagnostics.gateway import environment_tasks as tasks
from aiops_diagnostics.gateway import gateway_db
from gateway.test_candidate_knowledge_connection import connected  # noqa: F401
from gateway.test_environment_consumer import consumer_app  # noqa: F401
from gateway.test_environment_installation import installation_state, internal, submit  # noqa: F401
from gateway.test_environment_knowledge import knowledge  # noqa: F401
from gateway.test_environment_runtime import wiring  # noqa: F401
from gateway.test_environment_tasks import ADMIN, prepared  # noqa: F401


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
