"""D40：真实任务HTTP与模型运行证据；不能把completed或任意passed当验收。"""

import json
import time
from types import SimpleNamespace

import pytest
from dify.test_dify_ensure_workspace import upstream as workspace_upstream  # noqa: F401

from aiops_diagnostics.gateway import environment_business as business
from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import environment_tasks as tasks
from aiops_diagnostics.gateway import environment_validation_worker as worker
from aiops_diagnostics.gateway import gateway_db
from gateway.test_candidate_knowledge_connection import connected  # noqa: F401
from gateway.test_environment_consumer import consumer_app  # noqa: F401
from gateway.test_environment_installation import installation_state, internal  # noqa: F401
from gateway.test_environment_knowledge import knowledge  # noqa: F401
from gateway.test_environment_registration import installed, registration  # noqa: F401
from gateway.test_environment_runtime import wiring  # noqa: F401
from gateway.test_environment_tasks import ADMIN, prepared  # noqa: F401


@pytest.fixture
def registered(installed):  # noqa: F811
    state, original, plan = installed
    with gateway_db.connection(drafts._database(state[0][0]), write=True) as c:
        row = tasks._row(c, original)
        result = json.loads(row["result"])
        result["version"] = "1.36.0"
        c.execute("UPDATE environment_tasks SET result=? WHERE id=?", (json.dumps(result), original))
    identifier = registration(installed).json()["id"]
    owner = "a" * 32
    internal(state, {"installation_claim": True, "owner": owner})
    stage = internal(state, {"registration": "stage", "id": identifier, "owner": owner}).json()
    result = dict(
        id=identifier,
        environment="a",
        tenant_id="same",
        scope="inactive-runtime-registration-only",
        state="observed",
        result="registered",
        executable=False,
        activated=False,
        business_acceptance=False,
        source_revision=stage["expected"]["revision"],
        **{k: v for k, v in stage["expected"].items() if k != "revision"},
    )
    assert (
        internal(
            state, {"registration": "complete", "id": identifier, "owner": owner, "result": result}
        ).status_code
        == 200
    )
    return state, identifier, plan


def submit(f, capabilities=None, **extra):
    return f[0][0][2].post(
        "/v1/dify/management/environment-tasks",
        json=dict(
            registration_task=f[1],
            validation_plan=f[2],
            capabilities=capabilities or ["consumer", "order", "offline"],
            case_ref="approved-case",
        )
        | extra,
    )


def proof(capability, **extra):
    result = dict(
        capability=capability,
        state="passed",
        case_sha256="c" * 64,
        observed_at=time.time(),
        business_accuracy="unconfirmed",
        evidence=dict(
            trial_id="qa-proof",
            run_id="run-proof",
            tools={
                "consumer": ["knowledge", "status"],
                "order": ["cpms_charging_samples", "cpms_order_detail", "cpms_order_process_stats"],
                "offline": ["cpms_device_offline"],
            }[capability],
            objects=["order-a", "source-a"] if capability == "consumer" else ["order-a"],
            scope_fingerprint="f" * 64,
            **(
                {"workflow_id": "workflow-a", "app_id": "app-a"}
                if capability == "consumer"
                else {"node": "node-a", "gateway_version": "1.36.0"}
            ),
        ),
    )
    return {**result, **extra}


def test_page_task_runs_all_selected_capabilities_and_keeps_activation_closed(
    registered, tmp_path, monkeypatch
):
    state = registered[0]
    response = submit(registered)
    assert response.status_code == 200, response.text
    identifier = response.json()["id"]
    assert submit(registered).json()["id"] == identifier
    owner = "d" * 32
    claim = internal(state, {"installation_claim": True, "owner": owner})
    assert claim.json()["action"] == "candidate-validate"
    calls = []

    def remote(root, target, env, payload, control=False):
        calls.append(payload["action"])
        if payload["action"] == "business-step":
            if payload["capability"] != "consumer":
                return proof(payload["capability"])
            return dict(
                candidate=payload["spec"]["consumer"],
                token="ect_synthetic",
                question="read own order-a and knowledge",
                expected_reference="source-a",
                scope_fingerprint="f" * 64,
                case_sha256="c" * 64,
            )
        if payload["action"] == "consumer-start":
            return {"trial_id": "qa-consumer"}
        if payload["action"] == "consumer-validation-evidence":
            return {"status": "completed", **proof("consumer")}
        if payload["action"] == "consumer-cancel":
            return {}
        raise AssertionError(payload)

    monkeypatch.setattr(worker, "local_trial", remote)

    def request(payload):
        response = internal(state, payload)
        assert response.status_code == 200, response.text
        return response.json()

    result = worker.execute(tmp_path, request, identifier, owner)
    assert result["state"] == "observed" and result["result"]["activated"] is False
    assert {c["capability"] for c in result["result"]["checks"]} == {"consumer", "order", "offline"}
    assert calls.count("business-step") == 3 and calls.count("consumer-start") == 1
    assert "ect_synthetic" not in json.dumps(result)


def test_cancelled_validation_does_not_start_following_business(registered):
    state = registered[0]
    identifier = submit(registered).json()["id"]
    owner = "b" * 32
    internal(state, {"installation_claim": True, "owner": owner})
    state[0][2].post("/v1/dify/management/environment-tasks", json={"id": identifier, "cancel": True})
    r = internal(state, {"validation": "step", "id": identifier, "owner": owner, "capability": "order"})
    assert r.status_code == 409


def test_forged_passed_and_unselected_capability_are_rejected(registered):
    state = registered[0]
    identifier = submit(registered, ["order"]).json()["id"]
    owner = "b" * 32
    internal(state, {"installation_claim": True, "owner": owner})
    payload = dict(validation="step", id=identifier, owner=owner, capability="order")
    assert internal(state, payload).json()["execute"] is True
    assert (
        internal(state, {**payload, "result": {"capability": "order", "state": "passed"}}).status_code == 409
    )
    assert (
        internal(state, {**payload, "result": proof("order", evidence={"token": "secret"})}).status_code
        == 409
    )
    assert internal(state, {**payload, "capability": "consumer"}).status_code == 409


def test_worker_resume_does_not_repeat_unknown_consumer_start(tmp_path, monkeypatch):
    calls = []

    def request(payload):
        calls.append(payload)
        if payload["validation"] == "stage":
            return {
                "spec": {"capabilities": ["consumer"]},
                "steps": {"consumer": {"state": "started"}},
                "target": {},
            }
        if payload["validation"] == "complete":
            return {"state": "blocked"}
        return {"recorded": True}

    monkeypatch.setattr(worker, "local_trial", lambda *a, **kw: pytest.fail("unknown start must not replay"))
    assert worker.execute(tmp_path, request, "task_" + "a" * 32, "b" * 32)["state"] == "blocked"
    assert calls[1]["result"]["state"] == "blocked"


@pytest.mark.parametrize("order_status", [2, 3, 5])
def test_operator_evidence_requires_actual_object_process_and_node(monkeypatch, order_status):
    from aiops_diagnostics.execution.agent_runs import scope_to_json
    from gateway.test_environment_trial import scope

    run = SimpleNamespace(
        run_id="run-a",
        environment="a",
        scope_json=scope_to_json(scope()),
        publication_app_id="operator-a",
        exec_config_confirmed=True,
        order_no="order-a",
        snapshot={
            "evidence": {
                "cpms_order_detail": {"data": {"orderNo": "order-a", "status": order_status}},
                "cpms_charging_samples": {"data": {"records": [{"orderNo": "order-a", "voltage": 220}]}},
                "cpms_order_process_stats": {"data": {"sample_count": 1, "metrics": [{"count": 1}]}},
            }
        },
        scope_fingerprint="f" * 64,
        actual_node_id="node-a",
        gateway_version="1.36.0",
    )
    context = SimpleNamespace(agent_run_store=SimpleNamespace(by_qa=lambda _: run))
    spec = {"environment": "a", "tenant_id": "same", "operator_app": "operator-a"}
    assert (
        business.operator_evidence(context, "order", "qa-a", {"order_no": "order-a"}, spec)["run_id"]
        == "run-a"
    )
    run.snapshot["evidence"].pop("cpms_order_process_stats")
    with pytest.raises(ValueError, match="PROCESS"):
        business.operator_evidence(context, "order", "qa-a", {"order_no": "order-a"}, spec)
    run.snapshot["evidence"]["cpms_order_process_stats"] = {
        "data": {"sample_count": 1, "metrics": [{"count": 1}]}
    }
    run.order_no = "other"
    with pytest.raises(ValueError, match="ORDER"):
        business.operator_evidence(context, "order", "qa-a", {"order_no": "order-a"}, spec)


def _timeline_run(order_status, detail_extra=None, timeline=None, stats=None):
    from aiops_diagnostics.execution.agent_runs import scope_to_json
    from gateway.test_environment_trial import scope

    detail = {"orderNo": "order-a", "status": order_status, **(detail_extra or {})}
    evidence = {
        "cpms_order_detail": {"data": detail},
        "cpms_order_process_stats": {"data": stats or {"sample_count": 1, "metrics": [{"count": 1}]}},
    }
    if timeline is not None:
        evidence["cpms_charging_timeline"] = {"data": timeline}
    return SimpleNamespace(
        run_id="run-a",
        environment="a",
        scope_json=scope_to_json(scope()),
        publication_app_id="operator-a",
        exec_config_confirmed=True,
        order_no="order-a",
        snapshot={"evidence": evidence},
        scope_fingerprint="f" * 64,
        actual_node_id="node-a",
        gateway_version="1.36.0",
    )


@pytest.mark.parametrize("order_status", [2, 3, 5])
def test_operator_evidence_accepts_authorized_timeline_series(monkeypatch, order_status):
    """监测表结构性空行：验收认同一授权单的时序证据——设备与窗口逐字等于详情。"""
    detail = {
        "childDeviceCode": "DEV-1",
        "deviceCode": "DEV-0",
        "createdTime": "2026-11-20 17:13:29",
        "stopTime": "2026-11-21 03:02:22",
    }
    timeline = {
        "records": [{"txSerialNo": "17", "outputVoltage": "220"}],
        "device": "DEV-1",
        "window_start": "2026-11-20 17:13:29",
        "window_end": "2026-11-21 03:02:22",
    }
    run = _timeline_run(order_status, detail, timeline)
    context = SimpleNamespace(agent_run_store=SimpleNamespace(by_qa=lambda _: run))
    spec = {"environment": "a", "tenant_id": "same", "operator_app": "operator-a"}
    result = business.operator_evidence(context, "order", "qa-a", {"order_no": "order-a"}, spec)
    assert result["tools"] == [
        "cpms_charging_timeline",
        "cpms_order_detail",
        "cpms_order_process_stats",
    ]
    # 公开回执同样认时序序列：记录与发布两道 validate_result 不得再按监测表拒。
    business.validate_result(
        {
            "capability": "order",
            "state": "passed",
            "evidence": result,
            "case_sha256": "a" * 64,
            "observed_at": time.time(),
            "business_accuracy": "unconfirmed",
        },
        "order",
    )

    run.snapshot["evidence"]["cpms_charging_timeline"]["data"]["device"] = "DEV-9"
    with pytest.raises(ValueError, match="PROCESS"):
        business.operator_evidence(context, "order", "qa-a", {"order_no": "order-a"}, spec)
    run.snapshot["evidence"]["cpms_charging_timeline"]["data"]["device"] = "DEV-1"
    run.snapshot["evidence"]["cpms_charging_timeline"]["data"]["window_end"] = "2026-11-21 04:02:22"
    with pytest.raises(ValueError, match="PROCESS"):
        business.operator_evidence(context, "order", "qa-a", {"order_no": "order-a"}, spec)


def test_operator_evidence_timeline_path_rejects_count_and_metric_gaps(monkeypatch):
    """有时序行不等于通过：样本数与统计消费行数不一致、统计无有效指标都拒。"""
    detail = {
        "childDeviceCode": "DEV-1",
        "createdTime": "2026-11-20 17:13:29",
        "stopTime": "2026-11-21 03:02:22",
    }
    timeline = {
        "records": [{"txSerialNo": "17", "outputVoltage": "220"}],
        "device": "DEV-1",
        "window_start": "2026-11-20 17:13:29",
        "window_end": "2026-11-21 03:02:22",
    }
    spec = {"environment": "a", "tenant_id": "same", "operator_app": "operator-a"}

    run = _timeline_run(3, detail, timeline, stats={"sample_count": 2, "metrics": [{"count": 1}]})
    context = SimpleNamespace(agent_run_store=SimpleNamespace(by_qa=lambda _: run))
    with pytest.raises(ValueError, match="PROCESS"):
        business.operator_evidence(context, "order", "qa-a", {"order_no": "order-a"}, spec)

    # 多流水未合并时统计 metrics 为空：拒，不把「窗口有行」当通过。
    run = _timeline_run(3, detail, timeline, stats={"sample_count": 1, "metrics": []})
    context = SimpleNamespace(agent_run_store=SimpleNamespace(by_qa=lambda _: run))
    with pytest.raises(ValueError, match="PROCESS"):
        business.operator_evidence(context, "order", "qa-a", {"order_no": "order-a"}, spec)

    # 两源均空（监测表无行、时序未交付）照旧拒。
    run = _timeline_run(3, detail, None, stats={"sample_count": 0, "metrics": []})
    context = SimpleNamespace(agent_run_store=SimpleNamespace(by_qa=lambda _: run))
    with pytest.raises(ValueError, match="PROCESS"):
        business.operator_evidence(context, "order", "qa-a", {"order_no": "order-a"}, spec)


def test_target_persists_intent_and_never_restarts_an_unknown_run(tmp_path, monkeypatch):

    spec = dict(
        id="task_" + "a" * 32,
        environment="a",
        tenant_id="same",
        case_ref="case",
        capabilities=["order"],
        installed={"runtime_sha256": "a" * 64},
        operator_app="app-a",
        consumer={"app_id": "app-c"},
    )
    case = {
        "operator_token": "synthetic-secret",
        "cases": {"order": {"question": "分析order-a", "order_no": "order-a"}},
    }
    context = SimpleNamespace(settings=SimpleNamespace(data_home=tmp_path))
    monkeypatch.setattr(business, "installation", lambda *a: None)
    monkeypatch.setattr(business, "load_case", lambda *a: (case, "c" * 64))
    from aiops_diagnostics.gateway import environment_trial as trial

    attempts = []

    def lost(*args):
        attempts.append(True)
        raise OSError("response lost")

    monkeypatch.setattr(trial, "start", lost)
    with pytest.raises(OSError):
        business.step(
            context,
            {"spec": spec, "capability": "order", "observe_only": False},
            start_trial=trial.start,
            read_trial=trial.read,
            resolve_identity=trial.identity,
        )
    with pytest.raises(business.ValidationRefused, match="START_UNCONFIRMED"):
        business.step(
            context,
            {"spec": spec, "capability": "order", "observe_only": False},
            start_trial=trial.start,
            read_trial=trial.read,
            resolve_identity=trial.identity,
        )
    assert attempts == [True]
    assert "synthetic-secret" not in next(tmp_path.rglob("order.json")).read_text()


def test_target_completed_run_is_checked_and_persisted_without_token(tmp_path, monkeypatch):
    from aiops_diagnostics.gateway import environment_trial as trial
    from gateway.test_environment_trial import scope

    spec = dict(
        id="task_" + "b" * 32,
        environment="a",
        tenant_id="same",
        case_ref="case",
        capabilities=["order"],
        installed={"runtime_sha256": "a" * 64},
        operator_app="app-a",
        consumer={"app_id": "app-c"},
    )
    case = {
        "operator_token": "synthetic-secret",
        "cases": {"order": {"question": "分析order-a", "order_no": "order-a"}},
    }
    context = SimpleNamespace(settings=SimpleNamespace(data_home=tmp_path))
    monkeypatch.setattr(business, "installation", lambda *a: None)
    monkeypatch.setattr(business, "load_case", lambda *a: (case, "c" * 64))
    calls = []
    monkeypatch.setattr(trial, "start", lambda *a: calls.append(True) or {"trial_id": "qa-a"})
    monkeypatch.setattr(trial, "read", lambda *a: {"status": "completed"})
    evidence = proof("order")["evidence"]
    evidence["scope_fingerprint"] = scope().scope_fingerprint
    monkeypatch.setattr(business, "operator_evidence", lambda *a: evidence)
    monkeypatch.setattr(trial, "identity", lambda *a: scope())
    first = business.step(
        context,
        {"spec": spec, "capability": "order", "observe_only": False},
        start_trial=trial.start,
        read_trial=trial.read,
        resolve_identity=trial.identity,
    )
    assert business.step(
        context,
        {"spec": spec, "capability": "order", "observe_only": False},
        start_trial=trial.start,
        read_trial=trial.read,
        resolve_identity=trial.identity,
    ) == first and calls == [True]
    assert first["business_accuracy"] == "unconfirmed"
    assert "synthetic-secret" not in next(tmp_path.rglob("order.json")).read_text()


def test_consumer_validation_checks_tools_and_reference_not_completed_alone():
    from aiops_diagnostics.dify.environment_consumer_trial import validation_evidence

    calls = [
        {"tool": "status", "result": {"status": "found", "order_ids": ["order-a"]}},
        {
            "tool": "knowledge",
            "result": {"status": "found", "sources": [{"retrieved_reference_id": "source-a"}]},
        },
    ]
    snapshot = {"blocks": [{"kind": "reference", "reference_id": "source-a"}]}
    adapter = SimpleNamespace(
        caller=SimpleNamespace(scope_fingerprint="f" * 64), scope={"order_no": "order-a", "app_id": "app-a"}
    )
    run = SimpleNamespace(run_id="run-a", scope_fingerprint="f" * 64, workflow_id="workflow-a")
    context = SimpleNamespace(
        consumer_candidate_trials=SimpleNamespace(
            read=lambda p: {"status": "completed"}, leases={"qa-a": adapter}
        ),
        dify_run_store=SimpleNamespace(
            by_qa=lambda _: run, context=lambda _: dict(calls=calls), snapshot=lambda _: snapshot
        ),
    )
    payload = dict(
        trial_id="qa-a", expected_reference="source-a", expected_scope="f" * 64, case_sha256="c" * 64
    )
    assert validation_evidence(context, payload)["state"] == "passed"
    calls.pop(0)
    with pytest.raises(ValueError, match="EVIDENCE"):
        validation_evidence(context, payload)


def test_readonly_recovery_without_remote_intent_never_starts_a_run(tmp_path, monkeypatch):
    from aiops_diagnostics.gateway import environment_trial as trial

    spec = dict(
        id="task_" + "b" * 32,
        environment="a",
        tenant_id="same",
        case_ref="case",
        capabilities=["order"],
        installed={"runtime_sha256": "a" * 64},
        operator_app="app-a",
        consumer={"app_id": "app-c"},
    )
    case = {
        "operator_token": "synthetic-secret",
        "cases": {"order": {"question": "分析order-a", "order_no": "order-a"}},
    }
    context = SimpleNamespace(settings=SimpleNamespace(data_home=tmp_path))
    monkeypatch.setattr(business, "installation", lambda *a: None)
    monkeypatch.setattr(business, "load_case", lambda *a: (case, "c" * 64))
    monkeypatch.setattr(trial, "start", lambda *a: pytest.fail("recovery must not start"))
    with pytest.raises(business.ValidationRefused, match="START_UNCONFIRMED"):
        business.step(
            context,
            {"spec": spec, "capability": "order", "observe_only": True},
            start_trial=trial.start,
            read_trial=trial.read,
            resolve_identity=trial.identity,
        )


def test_case_is_private_bound_expiring_and_missing_scope_does_not_hide_other_cases(tmp_path, monkeypatch):
    from aiops_diagnostics.common.private_files import write_private_text

    spec = dict(
        id="task_" + "a" * 32,
        environment="a",
        tenant_id="same",
        case_ref="approved",
        capabilities=["consumer", "order", "offline"],
        installed={},
        operator_app="a",
        consumer={},
    )
    body = dict(
        environment="a",
        tenant_id="same",
        expires_at=time.time() + 600,
        operator_token="synthetic-token",
        consumer_session="",
        cases={"order": {"question": "分析 order-a 的异常及过程", "order_no": "order-a"}},
    )
    file = write_private_text(tmp_path / "cases/approved.json", json.dumps(body))
    monkeypatch.setattr(business, "case_file", lambda *a: file)
    context = SimpleNamespace(settings=SimpleNamespace(environment="a", execution_environment="a"))
    assert business.load_case(context, spec, "order")[0]["tenant_id"] == "same"
    with pytest.raises(business.ValidationRefused, match="SAMPLE_REQUIRED"):
        business.load_case(context, spec, "consumer")
    body["tenant_id"] = "other"
    file.write_text(json.dumps(body))
    with pytest.raises(business.ValidationRefused):
        business.load_case(context, spec, "order")
    body["tenant_id"] = "same"
    body["expires_at"] = time.time() - 1
    file.write_text(json.dumps(body))
    with pytest.raises(business.ValidationRefused):
        business.load_case(context, spec, "order")
    with pytest.raises(ValueError):
        business.load_case(context, {**spec, "case_ref": "../untrusted"}, "order")


def test_saved_success_cannot_bypass_expired_trial_authorization(tmp_path, monkeypatch):
    from aiops_diagnostics.common.private_files import write_private_text
    from aiops_diagnostics.gateway import environment_trial as trial

    spec = dict(
        id="task_" + "d" * 32,
        environment="a",
        tenant_id="same",
        case_ref="case",
        capabilities=["order"],
        installed={"runtime_sha256": "a" * 64},
        operator_app="app-a",
        consumer={"app_id": "app-c"},
    )
    case = {
        "operator_token": "synthetic-secret",
        "cases": {"order": {"question": "分析order-a", "order_no": "order-a"}},
    }
    context = SimpleNamespace(settings=SimpleNamespace(data_home=tmp_path))
    monkeypatch.setattr(business, "installation", lambda *a: None)
    monkeypatch.setattr(business, "load_case", lambda *a: (case, "c" * 64))
    write_private_text(
        tmp_path / "business-validation" / spec["id"] / "order.json",
        json.dumps({"spec": spec, "case_sha256": "c" * 64, "trial_id": "qa-old", "result": proof("order")}),
    )

    def expired(*a):
        raise trial.TrialRefused("ENVIRONMENT_TRIAL_NOT_FOUND")

    with pytest.raises(trial.TrialRefused):
        business.step(
            context,
            {"spec": spec, "capability": "order", "observe_only": True},
            start_trial=lambda *a: pytest.fail("must not restart"),
            read_trial=expired,
            resolve_identity=lambda *a: pytest.fail("readback must happen first"),
        )


def test_cancel_between_open_and_start_never_calls_consumer_model(registered, tmp_path, monkeypatch):
    state = registered[0]
    identifier = submit(registered, ["consumer"]).json()["id"]
    owner = "c" * 32
    internal(state, {"installation_claim": True, "owner": owner})
    calls = []

    def remote(root, target, env, payload, control=False):
        calls.append(payload["action"])
        assert payload["action"] == "business-step"
        assert (
            state[0][2]
            .post("/v1/dify/management/environment-tasks", json={"id": identifier, "cancel": True})
            .status_code
            == 200
        )
        return dict(
            candidate={},
            token="ect_synthetic",
            question="read",
            expected_reference="r",
            scope_fingerprint="f" * 64,
            case_sha256="c" * 64,
        )

    monkeypatch.setattr(worker, "local_trial", remote)

    def request(payload):
        response = internal(state, payload)
        if response.status_code != 200:
            raise ValueError("cancelled")
        return response.json()

    result = worker.execute(tmp_path, request, identifier, owner)
    assert result["state"] == "cancelled" and calls == ["business-step"]


def test_target_http_preserves_original_refusal_code_instead_of_500(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aiops_diagnostics.common.http_auth import build_internal_token_headers
    from aiops_diagnostics.gateway import environment_trial as trial

    context = SimpleNamespace(
        settings=SimpleNamespace(environment_task_key="synthetic", environment_registry_file="fixture"),
        runtime=SimpleNamespace(),
    )

    def refused(*args, **kwargs):
        raise trial.TrialRefused("ENVIRONMENT_TRIAL_EXPIRED")

    monkeypatch.setattr(business, "step", refused)
    app = FastAPI()
    trial.register(app, context)
    with TestClient(app, client=("127.0.0.1", 1234)) as client:
        response = client.post(
            "/v1/internal/environment-trial",
            headers=build_internal_token_headers("synthetic", 300, int(time.time())),
            json={"action": "business-step", "spec": {}, "capability": "order", "observe_only": True},
        )
    assert response.status_code == 200 and response.json() == {
        "capability": "order",
        "state": "blocked",
        "error_code": "ENVIRONMENT_TRIAL_EXPIRED",
    }


def test_same_blocked_validation_resubmits_original_receipt(registered):
    state = registered[0]
    identifier = submit(registered).json()["id"]
    owner = "b" * 32
    internal(state, {"installation_claim": True, "owner": owner})
    for capability in ("consumer", "offline", "order"):
        assert (
            internal(
                state, {"validation": "step", "id": identifier, "owner": owner, "capability": capability}
            ).status_code
            == 200
        )
        assert (
            internal(
                state,
                {
                    "validation": "step",
                    "id": identifier,
                    "owner": owner,
                    "capability": capability,
                    "result": {
                        "capability": capability,
                        "state": "blocked",
                        "error_code": "ENVIRONMENT_VALIDATION_KEY_REQUIRED",
                    },
                },
            ).status_code
            == 200
        )
    final = internal(state, {"validation": "complete", "id": identifier, "owner": owner}).json()
    assert final["state"] == "blocked"
    repeated = submit(registered)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["id"] == identifier and repeated.json()["result"] == final["result"]
