"""D43原管理任务入口；真实账本、真实已核内容与拒绝，不接受网页passed。"""

import json
import threading
import time

import pytest

from aiops_diagnostics.gateway import gateway_db
from aiops_diagnostics.onboarding import drafts, tasks
from gateway.test_environment_tasks import prepared as base_prepared  # noqa: F401
from gateway.test_environment_validation import (  # noqa: F401
    connected,
    consumer_app,
    installation_state,
    installed,
    internal,
    knowledge,
    proof,
    registered,
    wiring,
    workspace_upstream,
)
from gateway.test_environment_validation import (
    submit as validate,
)


@pytest.fixture
def prepared(base_prepared, tmp_path):  # noqa: F811
    from aiops_diagnostics.common.private_files import write_private_text

    path, old, client, settings = base_prepared
    root = tmp_path / "onboarding-assets"
    root.mkdir(mode=0o700)
    reference = write_private_text(root / "ref.json", old.read_text())
    settings.environment_onboarding_file = str(reference)
    return path, reference, client, settings


@pytest.fixture
def validated(registered):  # noqa: F811
    state = registered[0]
    identifier = validate(registered).json()["id"]
    owner = "e" * 32
    assert internal(state, {"installation_claim": True, "owner": owner}).json()["id"] == identifier
    for capability in ("consumer", "offline", "order"):
        internal(state, {"validation": "step", "id": identifier, "owner": owner, "capability": capability})
        result = internal(
            state,
            {
                "validation": "step",
                "id": identifier,
                "owner": owner,
                "capability": capability,
                "result": proof(capability),
            },
        )
        assert result.status_code == 200
    assert (
        internal(state, {"validation": "complete", "id": identifier, "owner": owner}).json()["state"]
        == "observed"
    )
    return registered, identifier


def publish(f, **extra):
    original, identifier = f
    return original[0][0][2].post(
        "/v1/dify/management/environment-tasks",
        json={
            "validation_task": identifier,
            "publication_plan": original[2],
            "confirm_readonly": True,
            "confirm_customer_knowledge": True,
            "acceptance_ref": "readonly-diagnosis",
            **extra,
        },
    )


def test_publish_task_consumes_validation_and_reuses_same_identity(validated):
    response = publish(validated)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"] == "environment-publish" and body["state"] == "queued"
    assert body["result"]["activated"] is False and body["result"]["business_accuracy"] == "unconfirmed"
    assert publish(validated).json()["id"] == body["id"]
    state = validated[0][0]
    claim = internal(state, {"installation_claim": True, "owner": "f" * 32})
    assert claim.status_code == 200 and claim.json()["action"] == "environment-publish"
    described = internal(state, {"publication": "describe", "id": body["id"], "owner": "f" * 32})
    assert described.status_code == 200, described.text
    assert described.json()["spec"]["tenant_id"] == "same"


@pytest.mark.parametrize("change", ["missing-business", "quality-claim", "expired"])
def test_incomplete_or_forged_publication_never_enters_publish(validated, change):
    original, identifier = validated
    path = original[0][0][0]
    if change == "quality-claim":
        response = publish(validated, passed=True)
    else:
        with gateway_db.connection(drafts._database(path), write=True) as conn:
            row = tasks._row(conn, identifier)
            result = json.loads(row["result"])
            if change == "missing-business":
                result["checks"][0] = {
                    "capability": "consumer",
                    "state": "blocked",
                    "error_code": "SAMPLE_REQUIRED",
                }
            else:
                result["checks"][0]["observed_at"] = time.time() - 2000
            conn.execute("UPDATE environment_tasks SET result=? WHERE id=?", (json.dumps(result), identifier))
        response = publish(validated)
    assert response.status_code in {403, 409, 422}


def test_missing_capacity_is_safe_before_central_publication(validated, monkeypatch):
    original, _ = validated
    state = original[0]
    response = publish(validated)
    assert response.status_code == 200, response.text
    identifier = response.json()["id"]
    owner = "a" * 32
    internal(state, {"installation_claim": True, "owner": owner})
    response = internal(state, {"publication": "capacity-reference", "id": identifier, "owner": owner})
    assert response.status_code in {403, 409}
    assert "CAPACITY" in response.text


@pytest.mark.parametrize("completed_stages", [0, 1, 2, 3, 4])
def test_worker_yields_phases_and_restores_same_task_without_second_models(
    validated, tmp_path, monkeypatch, completed_stages
):
    from aiops_diagnostics.onboarding import capacity as environment_capacity
    from aiops_diagnostics.onboarding import publication
    from aiops_diagnostics.onboarding import publication_ssh_runner as worker

    state = validated[0][0]
    identifier = publish(validated).json()["id"]
    calls = []
    target_config = ["original"]
    bundle = {"path": "/private/release.json", "sha256": "a" * 64}
    monkeypatch.setattr(publication, "central_bundle", lambda *a, **kw: bundle)
    monkeypatch.setattr(publication, "connect", lambda *a: {"connected": True})
    monkeypatch.setattr(publication, "activate_directory", lambda *a: {"revision": 2})
    monkeypatch.setattr(
        worker,
        "local_trial",
        lambda *a, **kw: (
            {
                "state": "passed",
                "operation": identifier,
                "operator_runs": ["o", "f"],
                "run_id": "r1",
                "observed_at": time.time(),
                "trials": a[3]["trials"],
            }
            if a[3].get("action") == "publication-trial-complete"
            else {}
        ),
    )
    monkeypatch.setattr(
        environment_capacity,
        "run",
        lambda *a: (
            calls.append("capacity")
            or {"state": "passed", "observed_at": time.time(), "runs": ["c", "o", "f"]}
        ),
    )
    monkeypatch.setattr(
        worker,
        "verify",
        lambda *a: (
            calls.append("models")
            or {
                "state": "passed",
                "operation": identifier,
                "operator_runs": ["o", "f"],
                "observed_at": time.time(),
                "trials": {"order": "t1", "offline": "t2"},
            }
        ),
    )

    def target(*args, **kwargs):
        action = args[3]
        calls.append(action)
        target_config[0] = "original" if action == "restore" else "formal"
        phase = {"stage": "staged", "activate": "activated", "restore": "restored"}[action]
        return {
            "state": "observed",
            "phase": phase,
            "activated": phase == "activated",
            "installed": {"private-path": "/private/target"},
        }

    monkeypatch.setattr(worker, "target_task", target)
    original = publication.dispatch

    def dispatch(context, payload):
        if payload.get("publication") == "capacity-reference":
            return {"shared": []}
        try:
            return original(context, payload)
        except Exception as exc:
            raise AssertionError(f"{payload['publication']}: {type(exc).__name__}: {exc}") from exc

    monkeypatch.setattr(publication, "dispatch", dispatch)

    def request(payload):
        response = internal(state, payload)
        assert response.status_code == 200, response.text
        return response.json()

    for index in range(completed_stages):
        owner = f"{index + 1:032x}"
        assert request({"installation_claim": True, "owner": owner})["id"] == identifier
        result = worker.execute(tmp_path, request, identifier, owner)
    if completed_stages == 4:
        assert result["state"] == "observed", result
        assert result["result"]["activated"] and result["result"]["directory"] == "synchronized"
        assert calls == ["capacity", "stage", "models", "activate"]
        assert "/private/" not in json.dumps(result)
    elif completed_stages:
        assert result["state"] == "queued"
    prior_calls = list(calls)
    response = state[0][2].post(
        "/v1/dify/management/environment-tasks",
        json={"restore_publication": identifier}
        if completed_stages == 4
        else {"id": identifier, "cancel": True},
    )
    assert response.status_code == 200, response.text
    owner = "a" * 32
    claimed = request({"installation_claim": True, "owner": owner})
    if completed_stages == 0:
        assert response.json()["state"] == "cancelled" and not claimed["claimed"]
        assert calls == [] and target_config[0] == "original"
        return
    assert claimed["claimed"] is True and claimed["id"] == identifier, response.text
    restored = worker.execute(tmp_path, request, identifier, owner)
    assert restored["state"] == "cancelled" and restored["result"]["phase"] == "restored"
    assert target_config[0] == "original"
    assert calls == prior_calls + (["restore"] if completed_stages >= 2 else [])


def _drive_to_stale_verify(identifier, state, tmp_path, monkeypatch, observed_at):
    from aiops_diagnostics.onboarding import capacity as environment_capacity
    from aiops_diagnostics.onboarding import publication
    from aiops_diagnostics.onboarding import publication_ssh_runner as worker

    monkeypatch.setattr(
        publication,
        "central_bundle",
        lambda *a, **kw: {"path": "/private/release.json", "sha256": "a" * 64},
    )
    monkeypatch.setattr(publication, "connect", lambda *a: {"connected": True})
    monkeypatch.setattr(publication, "activate_directory", lambda *a: {"revision": 2})
    monkeypatch.setattr(
        environment_capacity,
        "run",
        lambda *a: {"state": "passed", "observed_at": time.time(), "runs": ["c", "o", "f"]},
    )
    monkeypatch.setattr(
        worker,
        "verify",
        lambda *a: {
            "state": "passed",
            "operation": identifier,
            "operator_runs": ["o", "f"],
            "observed_at": observed_at,
            "trials": {"order": "t1", "offline": "t2"},
        },
    )

    def target(*args, **kwargs):
        phase = {"stage": "staged", "activate": "activated", "restore": "restored"}[args[3]]
        return {
            "state": "observed",
            "phase": phase,
            "activated": phase == "activated",
            "installed": {"private-path": "/private/target"},
        }

    monkeypatch.setattr(worker, "target_task", target)
    monkeypatch.setattr(worker, "local_trial", lambda *a, **kw: {})
    original = publication.dispatch
    monkeypatch.setattr(
        publication,
        "dispatch",
        lambda context, payload: (
            {"shared": []}
            if payload.get("publication") == "capacity-reference"
            else original(context, payload)
        ),
    )

    def request(payload):
        response = internal(state, payload)
        assert response.status_code == 200, response.text
        return response.json()

    for index in range(3):
        owner = f"{index + 1:032x}"
        request({"installation_claim": True, "owner": owner})
        worker.execute(tmp_path, request, identifier, owner)
    return worker, request


def test_worker_reconfirms_stale_proof_on_target_before_activate(validated, tmp_path, monkeypatch):
    # 每次activate前都在同operation上重核取回新确认时刻的proof：不只在proof
    # 逾300s时——发行物到期、activate失败重领都由目标verdict给终态；不重放模型。
    state = validated[0][0]
    identifier = publish(validated).json()["id"]
    worker, request = _drive_to_stale_verify(identifier, state, tmp_path, monkeypatch, time.time() - 400)
    refreshes, activated = [], []

    def trial(root, target, environment, payload, **kwargs):
        if payload.get("action") == "publication-trial-complete":
            refreshes.append(payload)
            return {
                "state": "passed",
                "operation": identifier,
                "operator_runs": ["o", "f"],
                "run_id": "r1",
                "scope_fingerprint": "fp",
                "observed_at": time.time(),
                "trials": payload["trials"],
            }
        return {}

    monkeypatch.setattr(worker, "local_trial", trial)
    monkeypatch.setattr(
        worker,
        "target_task",
        lambda *a, **kw: (
            activated.append(kw.get("verification"))
            or {
                "state": "observed",
                "phase": "activated",
                "activated": True,
                "installed": {"private-path": "/private/target"},
            }
        ),
    )
    owner = "d" * 32
    request({"installation_claim": True, "owner": owner})
    result = worker.execute(tmp_path, request, identifier, owner)
    assert result["result"]["activated"] is True, result
    assert [p["action"] for p in refreshes] == ["publication-trial-complete"]
    assert refreshes[0]["trials"] == {"order": "t1", "offline": "t2"}
    assert activated and 0 <= time.time() - activated[0]["observed_at"] < 300


def test_worker_reconfirms_fresh_proof_before_every_activate(validated, tmp_path, monkeypatch):
    # proof刚签发也要先复核再activate：新鲜度只是复验结果不是跳过理由。
    state = validated[0][0]
    identifier = publish(validated).json()["id"]
    worker, request = _drive_to_stale_verify(identifier, state, tmp_path, monkeypatch, time.time())
    refreshes = []

    def trial(root, target, environment, payload, **kwargs):
        if payload.get("action") == "publication-trial-complete":
            refreshes.append(payload)
            return {
                "state": "passed",
                "operation": identifier,
                "operator_runs": ["o", "f"],
                "run_id": "r1",
                "scope_fingerprint": "fp",
                "observed_at": time.time(),
                "trials": payload["trials"],
            }
        return {}

    monkeypatch.setattr(worker, "local_trial", trial)
    owner = "d" * 32
    request({"installation_claim": True, "owner": owner})
    result = worker.execute(tmp_path, request, identifier, owner)
    assert result["result"]["activated"] is True, result
    assert [p["action"] for p in refreshes] == ["publication-trial-complete"]


def test_worker_lands_expired_blocked_and_restores_via_page(validated, tmp_path, monkeypatch):
    # 复验判终态（发行物/案例期限已尽）：任务落blocked而非无限重领过期activate；
    # 且先于source配置写就终局，原页面恢复入口照旧走取消→恢复，不要求新鲜证据。
    state = validated[0][0]
    identifier = publish(validated).json()["id"]
    worker, request = _drive_to_stale_verify(identifier, state, tmp_path, monkeypatch, time.time() - 400)
    monkeypatch.setattr(
        worker,
        "local_trial",
        lambda *a, **kw: (
            {
                "state": "expired",
                "error_code": "ENVIRONMENT_PUBLICATION_VERIFICATION_EXPIRED",
            }
            if a[3].get("action") == "publication-trial-complete"
            else {}
        ),
    )
    calls = []

    def spy(payload):
        calls.append(payload.get("publication"))
        return request(payload)

    owner = "d" * 32
    request({"installation_claim": True, "owner": owner})
    result = worker.execute(tmp_path, spy, identifier, owner)
    assert result["state"] == "blocked", result
    assert result["result"]["error_code"] == "ENVIRONMENT_PUBLICATION_VERIFICATION_EXPIRED"
    assert "source-configuration" not in calls
    assert request({"installation_claim": True, "owner": "e" * 32}).get("claimed") is not True
    response = state[0][2].post(
        "/v1/dify/management/environment-tasks", json={"restore_publication": identifier}
    )
    assert response.status_code == 200, response.text
    owner = "f" * 32
    assert request({"installation_claim": True, "owner": owner})["claimed"] is True
    restored = worker.execute(tmp_path, request, identifier, owner)
    assert restored["result"]["phase"] == "restored", restored


def test_worker_resumes_activated_target_and_finishes_directory_without_recheck(
    validated, tmp_path, monkeypatch
):
    # activate真实返回activated并已记账、directory同步前中断：重领时目标准入
    # 已放开，closed-admission复核必永拒——observe到activated即按真实回执
    # 完成directory；不重核proof、不重放模型、不看发行物/案例期限。
    state = validated[0][0]
    identifier = publish(validated).json()["id"]
    worker, request = _drive_to_stale_verify(identifier, state, tmp_path, monkeypatch, time.time() - 400)
    refreshes = []
    answers = iter(
        [
            {
                "state": "passed",
                "operation": identifier,
                "operator_runs": ["o", "f"],
                "run_id": "r1",
                "scope_fingerprint": "fp",
                "observed_at": time.time(),
                "trials": {"order": "t1", "offline": "t2"},
            },
            {"state": "expired", "error_code": "ENVIRONMENT_PUBLICATION_VERIFICATION_EXPIRED"},
        ]
    )

    def trial(root, target, environment, payload, **kwargs):
        if payload.get("action") == "publication-trial-complete":
            refreshes.append(payload)
            return next(answers)
        return {}

    monkeypatch.setattr(worker, "local_trial", trial)

    def target(*args, **kwargs):
        if kwargs.get("observe_only"):
            return {
                "state": "observed",
                "phase": "activated",
                "activated": True,
                "installed": {"private-path": "/private/target"},
                "routes": {"operator": 1, "consumer": 1},
            }
        return {
            "state": "observed",
            "phase": "activated",
            "activated": True,
            "installed": {"private-path": "/private/target"},
            "routes": {"operator": 1, "consumer": 1},
        }

    monkeypatch.setattr(worker, "target_task", target)
    interrupted = []

    def request_or_fail(payload):
        if payload.get("publication") == "directory" and not interrupted:
            interrupted.append(payload)
            raise RuntimeError("directory interrupted")
        return request(payload)

    owner = "d" * 32
    request({"installation_claim": True, "owner": owner})
    first = worker.execute(tmp_path, request_or_fail, identifier, owner)
    assert first["state"] == "reconcile", first
    # 第二段：此时发行物/案例期限已尽（complete只会回expired）——若走到复核
    # 必落blocked；正确路径是observe到已activated直接完成directory同步。
    owner = "e" * 32
    request({"installation_claim": True, "owner": owner})
    result = worker.execute(tmp_path, request, identifier, owner)
    assert result["state"] == "observed", result
    assert result["result"]["activated"] and result["result"]["directory"] == "synchronized"
    assert interrupted and len(refreshes) == 1


def test_worker_rechecks_staged_target_after_interrupted_activate_intent(validated, tmp_path, monkeypatch):
    # activate intent已授权但远端从未越过准入（仍staged）：重领照常走精确复核；
    # expired → 先于source配置写落blocked。
    state = validated[0][0]
    identifier = publish(validated).json()["id"]
    worker, request = _drive_to_stale_verify(identifier, state, tmp_path, monkeypatch, time.time() - 400)
    refreshes = []
    answers = iter(
        [
            {
                "state": "passed",
                "operation": identifier,
                "operator_runs": ["o", "f"],
                "run_id": "r1",
                "scope_fingerprint": "fp",
                "observed_at": time.time(),
                "trials": {"order": "t1", "offline": "t2"},
            },
            {"state": "expired", "error_code": "ENVIRONMENT_PUBLICATION_VERIFICATION_EXPIRED"},
        ]
    )

    def trial(root, target, environment, payload, **kwargs):
        if payload.get("action") == "publication-trial-complete":
            refreshes.append(payload)
            return next(answers)
        return {}

    monkeypatch.setattr(worker, "local_trial", trial)
    attempts = []

    def target(*args, **kwargs):
        if kwargs.get("observe_only"):
            return {
                "state": "reconcile",
                "phase": "staged",
                "activated": False,
                "installed": {"private-path": "/private/target"},
            }
        attempts.append(args[3])
        raise RuntimeError("activate transport lost")

    monkeypatch.setattr(worker, "target_task", target)
    calls = []

    def spy(payload):
        calls.append(payload.get("publication"))
        return request(payload)

    owner = "d" * 32
    request({"installation_claim": True, "owner": owner})
    first = worker.execute(tmp_path, spy, identifier, owner)
    assert first["state"] == "reconcile" and attempts == ["activate"]
    owner = "e" * 32
    request({"installation_claim": True, "owner": owner})
    calls.clear()
    result = worker.execute(tmp_path, spy, identifier, owner)
    assert result["state"] == "blocked", result
    assert result["result"]["error_code"] == "ENVIRONMENT_PUBLICATION_VERIFICATION_EXPIRED"
    assert attempts == ["activate"] and "source-configuration" not in calls
    assert len(refreshes) == 2


def test_central_publication_freezes_real_template_and_reconciles_unique_key(validated, monkeypatch):
    import copy
    from types import SimpleNamespace

    import yaml

    from aiops_diagnostics.consumer.knowledge_access import KnowledgeGrants
    from aiops_diagnostics.dify import dify_console_admin, dify_dsl_pull
    from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces
    from aiops_diagnostics.knowledge import knowledge_delegation
    from aiops_diagnostics.onboarding import consumer_prep as environment_consumer
    from aiops_diagnostics.onboarding import publication

    original, _ = validated
    state = original[0]
    settings = state[2]
    settings.chat_dify_key_dir = str(settings.data_home / "keys")
    response = publish(validated)
    identifier = response.json()["id"]
    owner = "a" * 32
    internal(state, {"installation_claim": True, "owner": owner})
    staged = internal(state, {"publication": "stage", "id": identifier, "owner": owner}).json()
    request = staged["request"]
    with gateway_db.connection(drafts._database(state[0][0]), readonly=True) as conn:
        child = json.loads(tasks._row(conn, request["children"]["consumer-prepare"])["request"])
    context = SimpleNamespace(settings=settings)
    spec = publication.describe(context, request, identifier)["spec"]
    document = environment_consumer.document(child["consumer_source"], spec["consumer"]["knowledge_base_id"])
    exported = copy.deepcopy(document)
    for variable in exported["workflow"].get("environment_variables", []):
        variable["selector"] = ["env", variable["name"]]
    published = []
    keys = []
    enabled = [False]

    class Console:
        def __init__(self, *a):
            pass

        def _call(self, workspace, path, *, method="GET", body=None, **kwargs):
            assert workspace == spec["consumer"]["workspace_id"]
            if path.endswith("/api-keys"):
                if method == "POST":
                    keys.append({"token": "synthetic-only-app-key", "id": "key-one"})
                    return keys[0]
                return {"data": keys}
            if path.endswith("/api-enable"):
                enabled[0] = body["enable_api"]
                return {}
            return {
                "id": spec["consumer"]["app_id"],
                "workflow": {"id": "fixed-workflow"} if published else None,
                "enable_api": enabled[0],
                "enable_site": False,
            }

    class Dsl:
        current = [exported]

        def __init__(self, source):
            pass

        def export_dsl(self, *, workflow_id=None):
            # Dify导出会补回变量自身的selector；草稿详情省略它，比对只许忽略这一项。
            return yaml.safe_dump(self.current[0])

        def publish_workflow(self):
            published.append(True)
            return True

        def published_workflow_id(self):
            return "fixed-workflow"

    monkeypatch.setattr(dify_console_admin, "DifyConsoleAdmin", Console)
    monkeypatch.setattr(dify_dsl_pull, "DifyDslClient", Dsl)
    request["steps"]["capacity"] = {"state": "passed", "observed_at": time.time(), "runs": ["c", "o", "f"]}
    # 计划窗不得传进发行物：发行期限按签发时刻给链尾余量（ADR-0315），
    # 远端 stage/verify/activate 与 trial 加载都复核它——继承计划窗会把它
    # 冻成一个已经到期的值，链尾所有远端闸门必拒。
    request["plan"]["expires_at"] = 0
    readiness = {
        "operator_manifest": "frozen",
        "operator_prompt": "sop",
        "gateway_url": "https://a.example.test",
        "catalogue_token": "ckc_synthetic",
        "dify_chat_base_url": "https://dify.example.test/v1",
    }
    # 比对仍要拒绝被改动的导出：归一化只豁免已知表示差异，不是放宽判据。
    for tamper in (
        lambda d: d["workflow"]["environment_variables"][0].update(value="tampered"),
        lambda d: d["workflow"]["environment_variables"][0].update(selector=["env", "other"]),
    ):
        drifted = copy.deepcopy(exported)
        tamper(drifted)
        Dsl.current[0] = drifted
        with pytest.raises(ValueError, match="differs"):
            publication.central_bundle(context, request, spec, readiness, identifier)
    Dsl.current[0] = exported
    assert not published and not keys
    result = publication.central_bundle(context, request, spec, readiness, identifier)
    assert len(keys) == len(published) == 1 and enabled[0]
    assert (
        publication.central_bundle(context, request, spec, readiness, identifier, observe_only=True) == result
    )
    assert len(keys) == len(published) == 1
    from pathlib import Path

    private = json.loads(Path(result["path"]).read_text())
    assert 3000 < private["expires_at"] - time.time() <= 3600
    assert private["apps"]["consumer"]["key"] == "synthetic-only-app-key"
    frozen = private["apps"]["consumer"]["workflow"]["environment_variables"]
    assert frozen and all(v.get("selector") == ["env", v["name"]] for v in frozen)
    assert "api_key" not in private
    assert DifyWorkspaces(settings.database_file).resolve("same", "a") == spec["consumer"]["workspace_id"]
    knowledge_delegation.active(
        settings, knowledge_delegation.parse_id(private["knowledge"]["capabilities"][0]["knowledge_id"])
    )
    KnowledgeGrants(settings.database_file).revoke("same", "a", spec["consumer"]["knowledge_base_id"])
    with pytest.raises(ValueError):
        publication.central_bundle(context, request, spec, readiness, identifier, observe_only=True)
    assert len(keys) == len(published) == 1


def test_cancel_before_target_dispatch_refuses_even_existing_step_and_never_restarts(validated):
    state = validated[0][0]
    identifier = publish(validated).json()["id"]
    owner = "b" * 32
    internal(state, {"installation_claim": True, "owner": owner})
    base = {"publication": "intent", "id": identifier, "owner": owner, "step": "target"}
    assert internal(state, base).json()["execute"] is True
    response = state[0][2].post(
        "/v1/dify/management/environment-tasks", json={"id": identifier, "cancel": True}
    )
    assert response.status_code == 200
    assert internal(state, base).status_code in {403, 409}


def test_capacity_requires_overlapping_real_run_windows_before_publish(monkeypatch, tmp_path):
    from aiops_diagnostics.onboarding import capacity

    (tmp_path / ".dev-host.toml").write_text('[[targets]]\nname="target"\nhost="managed"\n')
    staged = {"request": {"target": {"host": "managed"}}}
    spec = {"environment": "a", "installed": {"source_revision": "a" * 40}}
    reference = {"shared": [{"host": "managed", "service": "m2live03-node", "kind": "docker"}]}
    sample = {
        "memory_bytes": 200_000_000,
        "peak_bytes": 200_000_000,
        "limit_bytes": 1_000_000_000,
        "available_bytes": 4_000_000_000,
    }
    monkeypatch.setattr(capacity, "sample", lambda root, hosts: [dict(sample) for _ in hosts])
    overlap = {
        "environment": "a",
        "revision": "a" * 40,
        "sampled_at": time.time(),
        "services": [{"pid": 1, "memory_bytes": 200_000_000}, {"pid": 2, "memory_bytes": 150_000_000}],
    }
    monkeypatch.setattr(capacity.subprocess, "check_output", lambda *a, **kw: json.dumps(overlap))
    concurrent = [True]

    def run(root, staged, spec, cap, consumer_running=None):
        if cap != "consumer":
            # 真实链同序：短腿等慢预热腿的 run 落库后才发令；consumer 自身立即就绪。
            consumer_running.wait(timeout=5)
        else:
            consumer_running.set()
        time.sleep(0.02)
        start = {"consumer": 1, "offline": 2, "order": 3}[cap]
        return {
            "state": "passed",
            "evidence": {"run_id": cap},
            "window": {"started_at": start, "completed_at": 4 if concurrent[0] else start + 0.5},
        }

    monkeypatch.setattr(capacity, "business_run", run)
    result = capacity.run(tmp_path, staged, spec, reference)
    assert result["state"] == "passed" and result["overlap"] == {"started_at": 3, "completed_at": 4}
    concurrent[0] = False
    with pytest.raises(ValueError, match="concurrent"):
        capacity.run(tmp_path, staged, spec, reference)


def _capacity_fixture(monkeypatch, tmp_path):
    from aiops_diagnostics.onboarding import capacity

    (tmp_path / ".dev-host.toml").write_text('[[targets]]\nname="target"\nhost="managed"\n')
    staged = {"request": {"target": {"host": "managed"}}}
    spec = {
        "environment": "a",
        "installed": {"source_revision": "a" * 40},
        "operator_app": "op",
        "consumer": {"app_id": "cons"},
    }
    reference = {"shared": [{"host": "managed", "service": "m2live03-node", "kind": "docker"}]}
    sample = {
        "memory_bytes": 200_000_000,
        "peak_bytes": 200_000_000,
        "limit_bytes": 1_000_000_000,
        "available_bytes": 4_000_000_000,
    }
    monkeypatch.setattr(capacity, "sample", lambda root, hosts: [dict(sample) for _ in hosts])
    overlap = {
        "environment": "a",
        "revision": "a" * 40,
        "sampled_at": time.time(),
        "services": [{"pid": 1, "memory_bytes": 200_000_000}, {"pid": 2, "memory_bytes": 150_000_000}],
    }
    monkeypatch.setattr(capacity.subprocess, "check_output", lambda *a, **kw: json.dumps(overlap))
    return capacity, staged, spec, reference


def test_capacity_short_legs_gated_on_the_measured_window(monkeypatch, tmp_path):
    """run 级：短腿的 business-step 必须晚于 consumer 的 run 落库（探针为真）。"""
    capacity, staged, spec, reference = _capacity_fixture(monkeypatch, tmp_path)
    order = []
    run_exists = threading.Event()

    def trial(root, target, environment, payload, *, control=False):
        action = payload["action"]
        if action == "consumer-run-started":
            order.append("probe")
            return {"run_started": run_exists.is_set()}
        if action == "business-step":
            cap = payload["capability"]
            order.append("step:" + cap)
            if cap != "consumer":
                assert run_exists.is_set(), "short leg fired before the consumer window existed"
            return (
                {
                    "candidate": {},
                    "token": "t",
                    "question": "q",
                    "expected_reference": "ref",
                    "scope_fingerprint": "fp",
                    "case_sha256": "0" * 64,
                }
                if cap == "consumer"
                else {"state": "passed", "evidence": {"trial_id": "qa-" + cap, "run_id": "run-" + cap}}
            )
        if action == "consumer-start":
            order.append("consumer-start")
            threading.Timer(0.2, run_exists.set).start()
            return {"trial_id": "tr"}
        if action == "consumer-validation-evidence":
            return {
                "status": "completed",
                "state": "passed",
                "run_id": "run-c",
                "evidence": {"run_id": "run-c"},
            }
        return {"started_at": 10, "completed_at": 20}

    monkeypatch.setattr(capacity, "local_trial", trial)
    result = capacity.run(tmp_path, staged, spec, reference)
    assert result["state"] == "passed"
    assert order.index("consumer-start") < order.index("step:offline")
    assert order.index("consumer-start") < order.index("step:order")


def test_capacity_short_legs_released_when_consumer_leg_fails(monkeypatch, tmp_path):
    """run 级：consumer 腿在窗口前失败，短腿必须被采样循环放行而不是空等。"""
    capacity, staged, spec, reference = _capacity_fixture(monkeypatch, tmp_path)
    released = []

    def trial(root, target, environment, payload, *, control=False):
        action = payload["action"]
        if action == "consumer-run-started":
            return {"run_started": False}
        if action == "business-step" and payload.get("capability") == "consumer":
            raise ValueError("candidate target refused or unavailable")
        if action == "business-step":
            released.append(payload["capability"])
            return {"state": "passed", "evidence": {"trial_id": "qa"}}
        return {"started_at": 10, "completed_at": 20}

    monkeypatch.setattr(capacity, "local_trial", trial)
    # 收紧等待上限：若采样循环不再放行失败腿，测试会以超时红掉而不是挂死。
    monkeypatch.setattr(capacity, "consumer_wait_seconds", lambda: 2)
    with pytest.raises(ValueError, match="refused"):
        capacity.run(tmp_path, staged, spec, reference)
    # 缴获失败后两条短腿都被放行（而非卡死到 150s 超时）。
    assert sorted(released) == ["offline", "order"]


def test_second_tenant_directory_sync_adds_only_approved_tenant_without_reentrant_lock(validated):
    from types import SimpleNamespace

    from aiops_diagnostics.onboarding import publication, registry

    state = validated[0][0]
    path, _, _, settings = state[0]
    body = registry.load(path)
    with gateway_db.connection(drafts._database(path), readonly=True) as connection:
        draft = json.loads(
            connection.execute("SELECT body FROM environment_drafts WHERE environment='a'").fetchone()[0]
        )
    existing = {
        k: v for k, v in draft.items() if k not in {"timezone", "deployment_target", "credential_ref"}
    }
    existing.update(
        config_ref="/etc/aiops-access-a/production.env",
        enabled=True,
        tenants=[{"id": "old", "name": "existing", "enabled": True}],
    )
    registry.apply(
        path, [existing], expected_revision=body["revision"], actor="fixture", managers=body["managers"]
    )
    result = publication.activate_directory(
        SimpleNamespace(settings=settings),
        {"plan": {"environment": "a"}, "tenant_id": "same", "identity": ("control", "platform", "admin")},
        {"phase": "activated", "activated": True},
    )
    current = registry.load(path)
    assert result["revision"] == current["revision"] == body["revision"] + 2
    assert current["environments"][0]["tenants"] == [
        {"id": "old", "name": "existing", "enabled": True},
        {"id": "same", "name": "同号", "enabled": True},
    ]


@pytest.mark.parametrize("artifact", [None, "intent.json", "release.json", "older-gateway", "markers-null"])
def test_cancel_with_unknown_central_publication_remains_reconcilable_without_replay(
    validated, tmp_path, monkeypatch, artifact
):
    from aiops_diagnostics.onboarding import publication_ssh_runner as worker

    state = validated[0][0]
    identifier = publish(validated).json()["id"]
    owner = "d" * 32
    internal(state, {"installation_claim": True, "owner": owner})
    assert internal(
        state, {"publication": "intent", "id": identifier, "owner": owner, "step": "central"}
    ).json()["execute"]
    internal(state, {"publication": "yield", "id": identifier, "owner": owner})
    cancelled = state[0][2].post(
        "/v1/dify/management/environment-tasks", json={"id": identifier, "cancel": True}
    )
    assert cancelled.json()["state"] == "reconcile"
    remote = []
    monkeypatch.setattr(worker, "target_task", lambda *a, **kw: remote.append("target"))
    monkeypatch.setattr(worker, "local_trial", lambda *a, **kw: remote.append("model"))

    if artifact and artifact not in ("older-gateway", "markers-null"):
        from pathlib import Path

        directory = Path(state[2].data_home) / "publication-bundles" / identifier
        directory.mkdir(parents=True)
        (directory / artifact).write_text("{}")

    def request(payload):
        response = internal(state, payload)
        assert response.status_code == 200, response.text
        out = response.json()
        # 旧网关不回 markers 键 / 标记 stat 失败回 null：判据缺失时恢复结论不得放行。
        if payload.get("publication") == "describe":
            if artifact == "older-gateway":
                out.pop("markers", None)
            elif artifact == "markers-null":
                out["markers"] = None
        return out

    assert request({"installation_claim": True, "owner": owner})["id"] == identifier
    observed = worker.execute(tmp_path, request, identifier, owner)
    if artifact:
        assert observed["state"] == "reconcile" and not observed["result"]["activated"]
        assert observed["result"].get("phase") != "restored" and remote == []
    else:
        # 中央步从未落盘工件：无任何共享副作用，恢复结论与未启动相同。
        assert observed["state"] == "cancelled" and observed["result"]["phase"] == "restored"
        assert remote == []


@pytest.mark.parametrize("remote_staged", [False, True])
def test_cancel_with_only_target_intent_restores_by_remote_staging_marker(
    validated, tmp_path, monkeypatch, remote_staged
):
    from aiops_diagnostics.onboarding import publication_ssh_runner as worker

    state = validated[0][0]
    identifier = publish(validated).json()["id"]
    owner = "d" * 32
    internal(state, {"installation_claim": True, "owner": owner})
    assert internal(
        state, {"publication": "intent", "id": identifier, "owner": owner, "step": "central"}
    ).json()["execute"]
    # 中央步已落账 bundle；目标步只消费 intent 即崩溃——账本无法区分远端是否已 staged。
    internal(
        state,
        {
            "publication": "record",
            "id": identifier,
            "owner": owner,
            "step": "central",
            "result": {"state": "prepared", "bundle": {"path": "/x/release.json", "sha256": "0" * 64}},
        },
    )
    assert internal(
        state, {"publication": "intent", "id": identifier, "owner": owner, "step": "target"}
    ).json()["execute"]
    internal(state, {"publication": "yield", "id": identifier, "owner": owner})
    cancelled = state[0][2].post(
        "/v1/dify/management/environment-tasks", json={"id": identifier, "cancel": True}
    )
    assert cancelled.json()["state"] == "reconcile"
    remote = []
    monkeypatch.setattr(
        worker, "target_task", lambda *a, **kw: remote.append("target") or {"phase": "restored"}
    )
    monkeypatch.setattr(worker, "target_staged", lambda *a, **kw: remote_staged)

    def request(payload):
        response = internal(state, payload)
        assert response.status_code == 200, response.text
        return response.json()

    assert request({"installation_claim": True, "owner": owner})["id"] == identifier
    observed = worker.execute(tmp_path, request, identifier, owner)
    if remote_staged:
        # 远端 publication-tasks 在场：可能有未知目标侧副作用，必须走受管恢复；
        # 替身 recover 回报 restored 后，complete 应同案终态化为 cancelled/restored。
        assert remote == ["target"]
        assert observed["state"] == "cancelled" and observed["result"]["phase"] == "restored"
    else:
        # 远端从未留下 staged 目录：目标侧零副作用，恢复结论与未启动相同。
        assert observed["state"] == "cancelled" and observed["result"]["phase"] == "restored"
        assert remote == []


@pytest.mark.parametrize("corrupt_key", ["central", "target"])
def test_cancel_with_malformed_step_shape_holds_reconcile(validated, tmp_path, corrupt_key):
    # 持久账本出现代码没写过的形状（steps.* 非 dict）时，读取必须收敛到
    # 核查 hold 而不是把 worker 崩成同形的无限重领。
    from aiops_diagnostics.onboarding import publication_ssh_runner as worker

    state = validated[0][0]
    identifier = publish(validated).json()["id"]
    owner = "d" * 32
    internal(state, {"installation_claim": True, "owner": owner})
    internal(state, {"publication": "intent", "id": identifier, "owner": owner, "step": "central"})
    internal(
        state,
        {
            "publication": "record",
            "id": identifier,
            "owner": owner,
            "step": "central",
            "result": {"state": "prepared", "bundle": {"path": "/x/release.json", "sha256": "0" * 64}},
        },
    )
    internal(state, {"publication": "intent", "id": identifier, "owner": owner, "step": "target"})
    internal(state, {"publication": "yield", "id": identifier, "owner": owner})
    cancelled = state[0][2].post(
        "/v1/dify/management/environment-tasks", json={"id": identifier, "cancel": True}
    )
    assert cancelled.json()["state"] == "reconcile"

    def request(payload):
        response = internal(state, payload)
        assert response.status_code == 200, response.text
        body = response.json()
        if payload.get("publication") == "stage":
            body["request"]["steps"][corrupt_key] = ["not-a-dict"]
        return body

    assert request({"installation_claim": True, "owner": owner})["id"] == identifier
    observed = worker.execute(tmp_path, request, identifier, owner)
    assert observed["state"] == "reconcile"
    assert observed["result"]["error_code"] == "ENVIRONMENT_PUBLICATION_RECONCILIATION_REQUIRED"


@pytest.mark.parametrize("returncode,expected", [(7, False), (0, True), (1, True), (9, True), (255, True)])
def test_target_staged_only_remote_absence_answer_disarms(tmp_path, monkeypatch, returncode, expected):
    # 远端脚本以 7 回答「缺席」；dev-host 自身失败（1/255 等）与环境锁被占（9，
    # 远端 task.py 可能在飞）都必须按在场处理，否则传输故障或竞态会被当成
    # 零副作用证据放行恢复。
    from types import SimpleNamespace

    from aiops_diagnostics.onboarding import publication_ssh_runner as worker

    (tmp_path / ".dev-host.toml").write_text('[[targets]]\nname = "95"\nhost = "qy-gw-120"\n')
    staged = {
        "request": {"target": {"host": "qy-gw-120"}, "plan": {"environment": "95"}},
    }
    sent = {}

    def run(*a, **kw):
        sent.update(kw)
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(worker, "subprocess", SimpleNamespace(run=run))
    assert worker.target_staged(tmp_path, staged, "task_" + "0" * 32) is expected
    # 探测路径与锁都按安装器 environment 推导，不走宿主注册的 paths.access；
    # 且必须先持锁再读标记——is_dir 断言的下标要在 flock 之后。
    assert "/run/lock/aiops-access-95.lock" in sent["input"]
    assert "/opt/aiops-access-95/publication-tasks/" in sent["input"]
    assert sent["input"].index("flock") < sent["input"].index("sys.exit(0 if")


def test_target_staged_rejects_dash_prefixed_host_name(tmp_path):
    # REFERENCE_PATTERN 放行前导 '-'；与 central_call 同口径的防御性拒绝，
    # 必须先于探测返回保守的在场侧（不发任何 subprocess）。
    from aiops_diagnostics.onboarding import publication_ssh_runner as worker

    (tmp_path / ".dev-host.toml").write_text('[[targets]]\nname = "-rf"\nhost = "qy-gw-120"\n')
    staged = {
        "request": {"target": {"host": "qy-gw-120"}, "plan": {"environment": "95"}},
    }
    assert worker.target_staged(tmp_path, staged, "task_" + "0" * 32) is True


@pytest.mark.parametrize("cancelled", [False, True])
def test_attempted_publication_outlives_plan_window_but_cancel_still_refuses(validated, cancelled):
    """attempted 发布的存续由账本与取消/漂移闸治理，不再受计划墙钟约束。

    真实链路（readiness/capacity/central/target/verify/activate）超过计划 900s
    窗时，在飞发布的每个前向 fresh 调用都被冻结计划墙钟拒死——任务停在
    checking 可认领却永远走不出下一步，等同登记任务的 dispatched 无出口。
    attempted（首个 intent 已消费）后窗只约束启动：取消行仍硬拒。
    """
    state = validated[0][0]
    identifier = publish(validated).json()["id"]
    owner = "a" * 32
    assert internal(state, {"installation_claim": True, "owner": owner}).json()["id"] == identifier
    intent = {"publication": "intent", "id": identifier, "owner": owner}
    assert internal(state, {**intent, "step": "readiness"}).json() == {"execute": True}
    path = state[0][0]
    with gateway_db.connection(drafts._database(path), write=True) as conn:
        request = json.loads(tasks._row(conn, identifier)["request"])
        assert request["attempted"] is True
        request["plan"]["expires_at"] = 0
        conn.execute(
            "UPDATE environment_tasks SET request=?,cancel=? WHERE id=?",
            (json.dumps(request), 1 if cancelled else 0, identifier),
        )
    response = internal(state, {**intent, "step": "capacity"})
    if cancelled:
        assert response.status_code in {403, 409}
    else:
        assert response.status_code == 200, response.text
        assert response.json() == {"execute": True}
        # 周期间让出回到 waiting：计划已死但链合法在飞，卡面须保持 queued
        # ——谎报 expired 会让页面停止轮询并藏起「恢复本次开通」这个设计出口。
        yielded = internal(state, {"publication": "yield", "id": identifier, "owner": owner})
        assert yielded.status_code == 200, yielded.text
        with gateway_db.connection(drafts._database(path), readonly=True) as conn:
            assert tasks._public(tasks._row(conn, identifier))["state"] == "queued"


def test_publication_plan_window_still_gates_first_intent(validated):
    """计划窗仍约束启动：未消费首个 intent 的发布在计划到期后不得认领。"""
    state = validated[0][0]
    identifier = publish(validated).json()["id"]
    path = state[0][0]
    with gateway_db.connection(drafts._database(path), write=True) as conn:
        request = json.loads(tasks._row(conn, identifier)["request"])
        request["plan"]["expires_at"] = 0
        conn.execute("UPDATE environment_tasks SET request=? WHERE id=?", (json.dumps(request), identifier))
    claim = internal(state, {"installation_claim": True, "owner": "b" * 32})
    assert claim.status_code == 200 and claim.json()["claimed"] is False
    with gateway_db.connection(drafts._database(path), readonly=True) as conn:
        row = tasks._row(conn, identifier)
        assert row["state"] == "blocked"
        assert row["error"] == "ENVIRONMENT_TASK_OBSERVATION_REQUIRED"


def test_publication_plan_window_gates_first_intent_after_claim(validated):
    """认领成功不等于启动完成：认领后计划到期，首个 intent 仍被拒绝。"""
    state = validated[0][0]
    identifier = publish(validated).json()["id"]
    owner = "c" * 32
    assert internal(state, {"installation_claim": True, "owner": owner}).json()["id"] == identifier
    path = state[0][0]
    with gateway_db.connection(drafts._database(path), write=True) as conn:
        request = json.loads(tasks._row(conn, identifier)["request"])
        request["plan"]["expires_at"] = 0
        conn.execute("UPDATE environment_tasks SET request=? WHERE id=?", (json.dumps(request), identifier))
    response = internal(
        state, {"publication": "intent", "id": identifier, "owner": owner, "step": "readiness"}
    )
    assert response.status_code in {403, 409}


def test_started_step_reauthorizes_execute_but_recorded_result_still_consumes(validated):
    """started 只是授权标记而非执行结果：重领必须重授 execute 让可重入步骤续跑。

    env95 实测（task_8fe0bcdc）：worker 死在 intent 已消费、record 未落账之间，
    账本只剩 {"state":"started"}；旧 one-shot 门把它判成已消费，observe_only
    又无法替未落账的副作用作保，任务在核查 hold 里永挂并占死 BUSY 闸。
    只有 record 落账的结果才把 intent 消费成 observed。
    """
    state = validated[0][0]
    identifier = publish(validated).json()["id"]
    owner = "a" * 32
    assert internal(state, {"installation_claim": True, "owner": owner}).json()["id"] == identifier
    intent = {"publication": "intent", "id": identifier, "owner": owner}
    assert internal(state, {**intent, "step": "central"}).json() == {"execute": True}
    assert internal(state, {**intent, "step": "central"}).json() == {"execute": True}
    recorded = {"state": "prepared", "bundle": {"path": "/x/release.json", "sha256": "0" * 64}}
    assert (
        internal(
            state,
            {
                "publication": "record",
                "id": identifier,
                "owner": owner,
                "step": "central",
                "result": recorded,
            },
        ).json()["recorded"]
        is True
    )
    observed = internal(state, {**intent, "step": "central"}).json()
    assert observed["execute"] is False and observed["observed"] == recorded


def test_started_central_step_resumes_execution_on_next_claim(validated, tmp_path, monkeypatch):
    """env95 事故形复现：central 只消费 intent 未 record，重领按可重入续跑。

    旧门下 intent 重返 execute=False → observe_only → 未落账副作用无法作保
    → ENVIRONMENT_PUBLICATION_RECONCILIATION_REQUIRED 永挂。started 重授
    execute 后 central_bundle 的确认读守卫让续跑收敛到同一 bundle。
    """
    from aiops_diagnostics.onboarding import publication
    from aiops_diagnostics.onboarding import publication_ssh_runner as worker

    state = validated[0][0]
    identifier = publish(validated).json()["id"]
    owner = "d" * 32
    path = state[0][0]
    with gateway_db.connection(drafts._database(path), write=True) as conn:
        request = json.loads(tasks._row(conn, identifier)["request"])
        request["attempted"] = True
        request["steps"] = {
            "readiness": {"state": "passed"},
            "capacity": {"state": "passed", "observed_at": time.time()},
            "central": {"state": "started"},
        }
        request["phase"] = "central"
        conn.execute("UPDATE environment_tasks SET request=? WHERE id=?", (json.dumps(request), identifier))

    seen = []
    bundle = {"path": "/private/release.json", "sha256": "a" * 64}
    monkeypatch.setattr(
        publication, "central_bundle", lambda *a, **kw: seen.append(kw["observe_only"]) or bundle
    )
    monkeypatch.setattr(publication, "connect", lambda *a: {"connected": True})
    monkeypatch.setattr(worker, "local_trial", lambda *a, **kw: {})
    monkeypatch.setattr(worker, "target_task", lambda *a, **kw: {"state": "observed", "phase": "staged"})

    def request_(payload):
        response = internal(state, payload)
        assert response.status_code == 200, response.text
        return response.json()

    assert request_({"installation_claim": True, "owner": owner})["id"] == identifier
    result = worker.execute(tmp_path, request_, identifier, owner)
    assert seen == [False]
    assert result["state"] == "queued"
    with gateway_db.connection(drafts._database(path), readonly=True) as conn:
        steps = json.loads(tasks._row(conn, identifier)["request"])["steps"]
    assert steps["central"]["bundle"] == bundle and steps["target"]["phase"] == "staged"


def test_started_capacity_step_remeasures_instead_of_reconcile_hold(validated, tmp_path, monkeypatch):
    """capacity 的 started 形同样续跑：重测产出从未落账的结果，不是重放。

    「do not replay models」防的是用新测量顶替已记录结果；started 下没有已记录
    结果，核查 hold 没有任何出口。record 落账的非 passed 结果仍经同一闸拒绝。
    """
    from aiops_diagnostics.onboarding import capacity as environment_capacity
    from aiops_diagnostics.onboarding import publication
    from aiops_diagnostics.onboarding import publication_ssh_runner as worker

    state = validated[0][0]
    identifier = publish(validated).json()["id"]
    owner = "d" * 32
    path = state[0][0]
    with gateway_db.connection(drafts._database(path), write=True) as conn:
        request = json.loads(tasks._row(conn, identifier)["request"])
        request["attempted"] = True
        request["steps"] = {
            "readiness": {"state": "passed"},
            "capacity": {"state": "started"},
        }
        request["phase"] = "capacity"
        conn.execute("UPDATE environment_tasks SET request=? WHERE id=?", (json.dumps(request), identifier))

    calls = []
    monkeypatch.setattr(worker, "local_trial", lambda *a, **kw: {})
    monkeypatch.setattr(
        environment_capacity,
        "run",
        lambda *a: calls.append("capacity") or {"state": "passed", "observed_at": time.time()},
    )
    original = publication.dispatch

    def dispatch(context, payload):
        if payload.get("publication") == "capacity-reference":
            return {"shared": []}
        return original(context, payload)

    monkeypatch.setattr(publication, "dispatch", dispatch)

    def request_(payload):
        response = internal(state, payload)
        assert response.status_code == 200, response.text
        return response.json()

    assert request_({"installation_claim": True, "owner": owner})["id"] == identifier
    result = worker.execute(tmp_path, request_, identifier, owner)
    assert calls == ["capacity"] and result["state"] == "queued"
    with gateway_db.connection(drafts._database(path), readonly=True) as conn:
        steps = json.loads(tasks._row(conn, identifier)["request"])["steps"]
    assert steps["capacity"]["state"] == "passed"


def test_stale_capacity_gates_first_mint_but_intent_marker_allows_resume(validated, monkeypatch):
    """容量 900s 新鲜度闸只约束首跑：intent.json 在场的续跑按既有授权收口。

    env95 在挂任务的 capacity 证据已越过 900s——若续跑仍按墙钟复核，
    started 重授只是换死在下一道闸。发行物照样携带原测量时刻，过期
    证据不作假；无 intent.json 的首跑仍被同闸拒绝。
    """
    import copy
    from pathlib import Path
    from types import SimpleNamespace

    import yaml

    from aiops_diagnostics.common.private_files import write_private_text
    from aiops_diagnostics.dify import dify_console_admin, dify_dsl_pull
    from aiops_diagnostics.onboarding import consumer_prep as environment_consumer
    from aiops_diagnostics.onboarding import publication

    original, _ = validated
    state = original[0]
    settings = state[2]
    settings.chat_dify_key_dir = str(settings.data_home / "keys")
    identifier = publish(validated).json()["id"]
    owner = "a" * 32
    internal(state, {"installation_claim": True, "owner": owner})
    staged = internal(state, {"publication": "stage", "id": identifier, "owner": owner}).json()
    request = staged["request"]
    with gateway_db.connection(drafts._database(state[0][0]), readonly=True) as conn:
        child = json.loads(tasks._row(conn, request["children"]["consumer-prepare"])["request"])
    context = SimpleNamespace(settings=settings)
    spec = publication.describe(context, request, identifier)["spec"]
    document = environment_consumer.document(child["consumer_source"], spec["consumer"]["knowledge_base_id"])
    exported = copy.deepcopy(document)
    for variable in exported["workflow"].get("environment_variables", []):
        variable["selector"] = ["env", variable["name"]]
    published = []
    keys = []
    enabled = [False]

    class Console:
        def __init__(self, *a):
            pass

        def _call(self, workspace, path, *, method="GET", body=None, **kwargs):
            if path.endswith("/api-keys"):
                if method == "POST":
                    keys.append({"token": "synthetic-only-app-key", "id": "key-one"})
                    return keys[0]
                return {"data": keys}
            if path.endswith("/api-enable"):
                enabled[0] = body["enable_api"]
                return {}
            return {
                "id": spec["consumer"]["app_id"],
                "workflow": {"id": "fixed-workflow"} if published else None,
                "enable_api": enabled[0],
                "enable_site": False,
            }

    class Dsl:
        def __init__(self, source):
            pass

        def export_dsl(self, *, workflow_id=None):
            return yaml.safe_dump(exported)

        def publish_workflow(self):
            published.append(True)
            return True

        def published_workflow_id(self):
            return "fixed-workflow"

    monkeypatch.setattr(dify_console_admin, "DifyConsoleAdmin", Console)
    monkeypatch.setattr(dify_dsl_pull, "DifyDslClient", Dsl)
    stale = time.time() - 3600
    request["steps"]["capacity"] = {"state": "passed", "observed_at": stale, "runs": ["c", "o", "f"]}
    readiness = {
        "operator_manifest": "frozen",
        "operator_prompt": "sop",
        "gateway_url": "https://a.example.test",
        "catalogue_token": "ckc_synthetic",
        "dify_chat_base_url": "https://dify.example.test/v1",
    }
    # 首跑无 intent 标记：过期容量证据仍被 900s 闸拒。
    with pytest.raises(tasks.TaskRefused, match="CAPACITY"):
        publication.central_bundle(context, request, spec, readiness, identifier)
    # 续跑：intent.json 证明授权已在容量新鲜时铸成，不再按墙钟复核。
    directory = Path(settings.data_home) / "publication-bundles" / identifier
    directory.mkdir(mode=0o700, parents=True)
    write_private_text(
        directory / "intent.json",
        json.dumps(
            {
                "environment": spec["environment"],
                "tenant_id": spec["tenant_id"],
                "workspace_id": spec["consumer"]["workspace_id"],
                "app_id": spec["consumer"]["app_id"],
                "workflow": dify_dsl_pull.workflow_content(yaml.safe_dump(exported)),
            },
            sort_keys=True,
        ),
    )
    result = publication.central_bundle(context, request, spec, readiness, identifier)
    assert len(keys) == len(published) == 1 and enabled[0]
    private = json.loads(Path(result["path"]).read_text())
    assert private["capacity"]["observed_at"] == stale


def test_same_tenant_consumer_residue_in_workspace_does_not_block_publish(validated, monkeypatch):
    """同(environment,tenant)的既有登记是本发布自身的血统残留，不挡续发。

    env95 实测（task_8fe0bcdc）：候选 081be053 发布提升后被 remint 成
    a9624b48（ADR-0312），孤儿登记行仍占同一 workspace；旧闸把它判成外来
    消费者，"workspace has another consumer" 每轮重抛、任务永挂
    （ADR-0317）。同一工作区由他租户或他环境占用仍属外来冲突。
    """
    import copy
    from types import SimpleNamespace

    import yaml

    from aiops_diagnostics.dify import dify_console_admin, dify_dsl_pull
    from aiops_diagnostics.dify.self_service_store import SelfServiceStore
    from aiops_diagnostics.onboarding import consumer_prep as environment_consumer
    from aiops_diagnostics.onboarding import publication

    original, _ = validated
    state = original[0]
    settings = state[2]
    settings.chat_dify_key_dir = str(settings.data_home / "keys")
    identifier = publish(validated).json()["id"]
    owner = "a" * 32
    internal(state, {"installation_claim": True, "owner": owner})
    staged = internal(state, {"publication": "stage", "id": identifier, "owner": owner}).json()
    request = staged["request"]
    with gateway_db.connection(drafts._database(state[0][0]), readonly=True) as conn:
        child = json.loads(tasks._row(conn, request["children"]["consumer-prepare"])["request"])
    context = SimpleNamespace(settings=settings)
    spec = publication.describe(context, request, identifier)["spec"]
    document = environment_consumer.document(child["consumer_source"], spec["consumer"]["knowledge_base_id"])
    exported = copy.deepcopy(document)
    for variable in exported["workflow"].get("environment_variables", []):
        variable["selector"] = ["env", variable["name"]]
    published = []
    keys = []
    enabled = [False]

    class Console:
        def __init__(self, *a):
            pass

        def _call(self, workspace, path, *, method="GET", body=None, **kwargs):
            if path.endswith("/api-keys"):
                if method == "POST":
                    keys.append({"token": "synthetic-only-app-key", "id": "key-one"})
                    return keys[0]
                return {"data": keys}
            if path.endswith("/api-enable"):
                enabled[0] = body["enable_api"]
                return {}
            return {
                "id": spec["consumer"]["app_id"],
                "workflow": {"id": "fixed-workflow"} if published else None,
                "enable_api": enabled[0],
                "enable_site": False,
            }

    class Dsl:
        def __init__(self, source):
            pass

        def export_dsl(self, *, workflow_id=None):
            return yaml.safe_dump(exported)

        def publish_workflow(self):
            published.append(True)
            return True

        def published_workflow_id(self):
            return "fixed-workflow"

    monkeypatch.setattr(dify_console_admin, "DifyConsoleAdmin", Console)
    monkeypatch.setattr(dify_dsl_pull, "DifyDslClient", Dsl)
    request["steps"]["capacity"] = {"state": "passed", "observed_at": time.time(), "runs": ["c", "o", "f"]}
    readiness = {
        "operator_manifest": "frozen",
        "operator_prompt": "sop",
        "gateway_url": "https://a.example.test",
        "catalogue_token": "ckc_synthetic",
        "dify_chat_base_url": "https://dify.example.test/v1",
    }
    store = SelfServiceStore(settings.database_file)
    # 同(environment,tenant)孤儿行：前一候选提升后留下的登记，续发即取代。
    store.register(
        app_id="081be053-orphan",
        tenant_id=spec["tenant_id"],
        workspace_id=spec["consumer"]["workspace_id"],
        agent_name="consumer-081be053-orphan",
        profile_id="",
        actor="acceptance",
        entry="consumer",
        environment=spec["environment"],
        targets={},
    )
    publication.central_bundle(context, request, spec, readiness, identifier)
    assert len(keys) == len(published) == 1 and enabled[0]
    # 取代是发行物指向新应用，不是删旧登记——孤儿行仍在册。
    assert store.app("081be053-orphan") is not None


def test_foreign_consumer_in_workspace_still_blocks_publish(validated, monkeypatch):
    """他租户或他环境占用同一 workspace 仍是外来冲突：豁免只放同血统。"""
    import copy
    import sqlite3
    from types import SimpleNamespace

    import yaml

    from aiops_diagnostics.dify import dify_console_admin, dify_dsl_pull
    from aiops_diagnostics.dify.self_service_store import SelfServiceStore
    from aiops_diagnostics.onboarding import consumer_prep as environment_consumer
    from aiops_diagnostics.onboarding import publication

    original, _ = validated
    state = original[0]
    settings = state[2]
    settings.chat_dify_key_dir = str(settings.data_home / "keys")
    identifier = publish(validated).json()["id"]
    owner = "a" * 32
    internal(state, {"installation_claim": True, "owner": owner})
    staged = internal(state, {"publication": "stage", "id": identifier, "owner": owner}).json()
    request = staged["request"]
    with gateway_db.connection(drafts._database(state[0][0]), readonly=True) as conn:
        child = json.loads(tasks._row(conn, request["children"]["consumer-prepare"])["request"])
    context = SimpleNamespace(settings=settings)
    spec = publication.describe(context, request, identifier)["spec"]
    document = environment_consumer.document(child["consumer_source"], spec["consumer"]["knowledge_base_id"])
    exported = copy.deepcopy(document)
    for variable in exported["workflow"].get("environment_variables", []):
        variable["selector"] = ["env", variable["name"]]
    published = []
    keys = []

    class Console:
        def __init__(self, *a):
            pass

        def _call(self, workspace, path, *, method="GET", body=None, **kwargs):
            if path.endswith("/api-keys"):
                if method == "POST":
                    keys.append({"token": "synthetic-only-app-key", "id": "key-one"})
                    return keys[0]
                return {"data": keys}
            return {
                "id": spec["consumer"]["app_id"],
                "workflow": None,
                "enable_api": False,
                "enable_site": False,
            }

    class Dsl:
        def __init__(self, source):
            pass

        def export_dsl(self, *, workflow_id=None):
            return yaml.safe_dump(exported)

        def publish_workflow(self):
            published.append(True)
            return True

        def published_workflow_id(self):
            return "fixed-workflow"

    monkeypatch.setattr(dify_console_admin, "DifyConsoleAdmin", Console)
    monkeypatch.setattr(dify_dsl_pull, "DifyDslClient", Dsl)
    request["steps"]["capacity"] = {"state": "passed", "observed_at": time.time(), "runs": ["c", "o", "f"]}
    readiness = {
        "operator_manifest": "frozen",
        "operator_prompt": "sop",
        "gateway_url": "https://a.example.test",
        "catalogue_token": "ckc_synthetic",
        "dify_chat_base_url": "https://dify.example.test/v1",
    }
    store = SelfServiceStore(settings.database_file)
    for index, occupant in enumerate(
        (
            {"tenant_id": "other-tenant", "environment": spec["environment"]},
            {"tenant_id": spec["tenant_id"], "environment": "other-env"},
        )
    ):
        store.register(
            app_id=f"foreign-{index}",
            tenant_id=occupant["tenant_id"],
            workspace_id=spec["consumer"]["workspace_id"],
            agent_name=f"consumer-foreign-{index}",
            profile_id="",
            actor="acceptance",
            entry="consumer",
            environment=occupant["environment"],
            targets={},
        )
        with pytest.raises(ValueError, match="another consumer"):
            publication.central_bundle(context, request, spec, readiness, identifier)
        assert not keys and not published
        # 上例外来行不留给下例：每例须单独被自己的占用判拒，残留会掩盖误放行。
        with sqlite3.connect(settings.database_file) as db:
            db.execute("DELETE FROM operator_apps WHERE app_id=?", (f"foreign-{index}",))


@pytest.mark.parametrize("failing_step", ["source-configuration", "directory"])
def test_restore_failure_after_target_restored_stays_reconcile_and_completes_on_retry(
    validated, tmp_path, monkeypatch, failing_step
):
    """目标恢复后中央 source-configuration 或 directory 失败必须保留 reconcile，重领收口。"""
    from aiops_diagnostics.onboarding import capacity as environment_capacity
    from aiops_diagnostics.onboarding import publication, registry
    from aiops_diagnostics.onboarding import publication_ssh_runner as worker

    state = validated[0][0]
    path = state[0][0]
    identifier = publish(validated).json()["id"]
    bundle = {"path": "/private/release.json", "sha256": "a" * 64}
    monkeypatch.setattr(publication, "central_bundle", lambda *a, **kw: bundle)
    monkeypatch.setattr(publication, "connect", lambda *a: {"connected": True})
    monkeypatch.setattr(
        environment_capacity,
        "run",
        lambda *a: {"state": "passed", "observed_at": time.time(), "runs": ["c", "o", "f"]},
    )
    monkeypatch.setattr(
        worker,
        "local_trial",
        lambda *a, **kw: (
            {
                "state": "passed",
                "operation": identifier,
                "operator_runs": ["o", "f"],
                "run_id": "r1",
                "observed_at": time.time(),
                "trials": a[3]["trials"],
            }
            if a[3].get("action") == "publication-trial-complete"
            else {}
        ),
    )
    monkeypatch.setattr(
        worker,
        "verify",
        lambda *a: {
            "state": "passed",
            "operation": identifier,
            "operator_runs": ["o", "f"],
            "observed_at": time.time(),
            "trials": {"order": "t1", "offline": "t2"},
        },
    )
    monkeypatch.setattr(
        worker,
        "target_task",
        lambda *a, **kw: {
            "state": "observed",
            "phase": {"stage": "staged", "activate": "activated", "restore": "restored"}[a[3]],
            "activated": a[3] == "activate",
        },
    )
    original_dispatch = publication.dispatch
    monkeypatch.setattr(
        publication,
        "dispatch",
        lambda context, payload: (
            {"shared": []}
            if payload.get("publication") == "capacity-reference"
            else original_dispatch(context, payload)
        ),
    )

    def request(payload):
        response = internal(state, payload)
        assert response.status_code == 200, response.text
        return response.json()

    for index in range(4):
        owner = f"{index + 1:032x}"
        assert request({"installation_claim": True, "owner": owner})["id"] == identifier
        activated = worker.execute(tmp_path, request, identifier, owner)
    assert activated["state"] == "observed" and activated["result"]["activated"] is True
    assert registry.load(path)["environments"][0]["tenants"][0]["enabled"] is True

    restored_req = state[0][2].post(
        "/v1/dify/management/environment-tasks", json={"restore_publication": identifier}
    )
    assert restored_req.status_code == 200, restored_req.text

    failed_once = []

    def fail_central_restore(payload):
        if payload.get("publication") == failing_step and payload.get("restore") and not failed_once:
            failed_once.append(failing_step)
            raise RuntimeError(f"transient {failing_step} failure")
        return request(payload)

    owner = "b" * 32
    assert request({"installation_claim": True, "owner": owner})["id"] == identifier
    first = worker.execute(tmp_path, fail_central_restore, identifier, owner)
    assert failed_once == [failing_step]
    assert first["state"] == "reconcile", first
    assert first["result"]["phase"] == "restoring"
    assert first["result"]["activated"] is False
    assert "directory" not in first["result"]
    assert first["result"]["error_code"] == "ENVIRONMENT_PUBLICATION_RECONCILIATION_REQUIRED"
    assert registry.load(path)["environments"][0]["tenants"][0]["enabled"] is True

    # 下一轮自动重领应完成中央收尾，状态收敛为 cancelled/restored 且清空旧 error_code 与 directory。
    owner2 = "c" * 32
    assert request({"installation_claim": True, "owner": owner2})["id"] == identifier
    second = worker.execute(tmp_path, request, identifier, owner2)
    assert second["state"] == "cancelled", second
    assert second["result"]["state"] == "cancelled"
    assert second["result"]["phase"] == "restored"
    assert second["result"]["activated"] is False
    assert "directory" not in second["result"]
    assert not second["result"].get("error_code")
    assert registry.load(path)["environments"][0]["tenants"][0]["enabled"] is False


def test_complete_reconcile_precedes_restored_phase_even_from_older_worker(validated):
    """即便旧版 worker 同时上报 state=reconcile 与 phase=restored，服务端仍须保留 reconcile。"""
    state = validated[0][0]
    identifier = publish(validated).json()["id"]
    owner = "d" * 32
    assert internal(state, {"installation_claim": True, "owner": owner}).json()["id"] == identifier
    assert (
        internal(
            state, {"publication": "intent", "id": identifier, "owner": owner, "step": "readiness"}
        ).json()["execute"]
        is True
    )
    cancelled = state[0][2].post(
        "/v1/dify/management/environment-tasks", json={"id": identifier, "cancel": True}
    )
    assert cancelled.status_code == 200, cancelled.text
    completed = internal(
        state,
        {
            "publication": "complete",
            "id": identifier,
            "owner": owner,
            "result": {
                "state": "reconcile",
                "phase": "restored",
                "activated": False,
                "error_code": "ENVIRONMENT_PUBLICATION_RECONCILIATION_REQUIRED",
            },
        },
    )
    assert completed.status_code == 200, completed.text
    body = completed.json()
    assert body["state"] == "reconcile"
    assert body["result"]["phase"] == "restoring"
    assert "directory" not in body["result"]
    assert body["result"]["error_code"] == "ENVIRONMENT_PUBLICATION_RECONCILIATION_REQUIRED"


def test_non_cancelled_task_never_becomes_cancelled_on_restored_phase(validated):
    """未取消任务即使携带 phase=restored 也绝不误落 cancelled，必须收口为 blocked。"""
    state = validated[0][0]
    identifier = publish(validated).json()["id"]
    owner = "e" * 32
    assert internal(state, {"installation_claim": True, "owner": owner}).json()["id"] == identifier

    blocked = internal(
        state,
        {
            "publication": "complete",
            "id": identifier,
            "owner": owner,
            "result": {
                "state": "expired",
                "phase": "restored",
                "activated": False,
                "error_code": "ENVIRONMENT_PUBLICATION_VERIFICATION_EXPIRED",
            },
        },
    )
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["state"] == "blocked"


def test_direct_restore_cleans_stale_directory_and_state(validated):
    """已激活任务一次性直接恢复成功（未经过 reconcile 失败）时，cancelled 终态须清理旧 directory/state。"""
    state = validated[0][0]
    identifier = publish(validated).json()["id"]
    owner = "f" * 32
    assert internal(state, {"installation_claim": True, "owner": owner}).json()["id"] == identifier
    assert (
        internal(
            state, {"publication": "intent", "id": identifier, "owner": owner, "step": "readiness"}
        ).json()["execute"]
        is True
    )
    activated = internal(
        state,
        {
            "publication": "complete",
            "id": identifier,
            "owner": owner,
            "result": {
                "state": "observed",
                "phase": "activated",
                "activated": True,
                "directory": "synchronized",
            },
        },
    )
    assert activated.status_code == 200 and activated.json()["state"] == "observed"
    restored_req = state[0][2].post(
        "/v1/dify/management/environment-tasks", json={"restore_publication": identifier}
    )
    assert restored_req.status_code == 200, restored_req.text
    owner2 = "1" * 32
    assert internal(state, {"installation_claim": True, "owner": owner2}).json()["id"] == identifier
    direct_cancelled = internal(
        state,
        {
            "publication": "complete",
            "id": identifier,
            "owner": owner2,
            "result": {"phase": "restored", "activated": False},
        },
    )
    assert direct_cancelled.status_code == 200, direct_cancelled.text
    body = direct_cancelled.json()
    assert body["state"] == "cancelled"
    assert body["result"]["state"] == "cancelled"
    assert body["result"]["activated"] is False
    assert "directory" not in body["result"]


def test_late_cancel_after_activation_enters_reconcile_instead_of_blocked(validated):
    """激活已同步目录后收到迟到取消请求时，必须转入 reconcile 自动恢复而非困在 blocked。"""
    state = validated[0][0]
    identifier = publish(validated).json()["id"]
    owner = "2" * 32
    assert internal(state, {"installation_claim": True, "owner": owner}).json()["id"] == identifier
    assert (
        internal(
            state, {"publication": "intent", "id": identifier, "owner": owner, "step": "readiness"}
        ).json()["execute"]
        is True
    )
    cancelled = state[0][2].post(
        "/v1/dify/management/environment-tasks", json={"id": identifier, "cancel": True}
    )
    assert cancelled.status_code == 200, cancelled.text
    late_complete = internal(
        state,
        {
            "publication": "complete",
            "id": identifier,
            "owner": owner,
            "result": {
                "state": "observed",
                "phase": "activated",
                "activated": True,
                "directory": "synchronized",
            },
        },
    )
    assert late_complete.status_code == 200, late_complete.text
    body = late_complete.json()
    assert body["state"] == "reconcile"
    assert body["result"]["phase"] == "restoring"
    assert "directory" not in body["result"]
