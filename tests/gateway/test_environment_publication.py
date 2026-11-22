"""D43原管理任务入口；真实账本、真实已核内容与拒绝，不接受网页passed。"""

import json
import time

import pytest

from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import environment_tasks as tasks
from aiops_diagnostics.gateway import gateway_db
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
    from aiops_diagnostics.dify import environment_publication as publication
    from aiops_diagnostics.gateway import environment_capacity
    from aiops_diagnostics.gateway import environment_publication_worker as worker

    state = validated[0][0]
    identifier = publish(validated).json()["id"]
    calls = []
    target_config = ["original"]
    bundle = {"path": "/private/release.json", "sha256": "a" * 64}
    monkeypatch.setattr(publication, "central_bundle", lambda *a, **kw: bundle)
    monkeypatch.setattr(publication, "connect", lambda *a: {"connected": True})
    monkeypatch.setattr(publication, "activate_directory", lambda *a: {"revision": 2})
    monkeypatch.setattr(worker, "local_trial", lambda *a, **kw: {})
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


def test_central_publication_freezes_real_template_and_reconciles_unique_key(validated, monkeypatch):
    import copy
    from types import SimpleNamespace

    import yaml

    from aiops_diagnostics.consumer.consumer_knowledge_grants import KnowledgeGrants
    from aiops_diagnostics.dify import dify_console_admin, dify_dsl_pull, environment_consumer
    from aiops_diagnostics.dify import environment_publication as publication
    from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces
    from aiops_diagnostics.knowledge import knowledge_delegation

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
    from aiops_diagnostics.gateway import environment_capacity as capacity

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
    from threading import Barrier

    concurrent = [True]
    barrier = Barrier(3)

    def run(root, staged, spec, cap):
        barrier.wait(timeout=5)
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


def test_second_tenant_directory_sync_adds_only_approved_tenant_without_reentrant_lock(validated):
    from types import SimpleNamespace

    from aiops_diagnostics.dify import environment_publication as publication
    from aiops_diagnostics.gateway import environment_registry as registry

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


@pytest.mark.parametrize("artifact", [None, "intent.json", "release.json", "older-gateway"])
def test_cancel_with_unknown_central_publication_remains_reconcilable_without_replay(
    validated, tmp_path, monkeypatch, artifact
):
    from aiops_diagnostics.gateway import environment_publication_worker as worker

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

    if artifact and artifact != "older-gateway":
        from pathlib import Path

        directory = Path(state[2].data_home) / "publication-bundles" / identifier
        directory.mkdir(parents=True)
        (directory / artifact).write_text("{}")

    def request(payload):
        response = internal(state, payload)
        assert response.status_code == 200, response.text
        out = response.json()
        # 旧网关不回 markers 键：判据缺失时恢复结论不得放行。
        if artifact == "older-gateway" and payload.get("publication") == "describe":
            out.pop("markers", None)
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
