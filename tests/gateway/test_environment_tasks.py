"""D23：中央任务跨请求持久去重、租约核对与安全取消。"""

import hashlib
import io
import json
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from dify.test_dify_ensure_workspace import upstream as workspace_upstream  # noqa: F401
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from aiops_diagnostics.common.http_auth import build_internal_token_headers
from aiops_diagnostics.common.private_files import write_private_text
from aiops_diagnostics.dify import environment_management as management
from aiops_diagnostics.dify import self_service_identity as auth
from aiops_diagnostics.dify.self_service_store import SetupConflict
from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import environment_plans as plans
from aiops_diagnostics.gateway import environment_registry as registry
from aiops_diagnostics.gateway import environment_tasks as tasks
from aiops_diagnostics.gateway import gateway_db

ADMIN = ("control", "platform", "admin")


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    path = tmp_path / "registry/directory.json"
    registry.apply(
        path,
        [],
        expected_revision=0,
        actor="fixture",
        managers=[
            dict(environment=ADMIN[0], tenant_id=ADMIN[1], user_id=ADMIN[2], environments=["*"]),
            dict(environment=ADMIN[0], tenant_id=ADMIN[1], user_id="second", environments=["*"]),
        ],
    )
    targets = []
    for env in ("a", "b"):
        draft = dict(
            id=env,
            name=env,
            enabled=False,
            gateway_url=f"https://{env}.example.test",
            cpms_base_url=f"https://{env}.example.test/cpms",
            company_gateway_url=f"https://{env}.example.test",
            deployment_target="host-" + env,
            credential_ref="config",
            timezone="UTC",
            tenants=[dict(id="same", name="同号", enabled=False)],
        )
        drafts.save(path, ADMIN, draft, expected_revision=0, registry_revision=1)
        row = dict(id="host-" + env, host="managed-" + env, user="aiops", port=18951, python="3.12")
        revision = "a" * 40
        for key, content in (
            (
                "application",
                {
                    "manifest.json": json.dumps(
                        dict(
                            schema=1,
                            revision=revision,
                            python="3.12",
                            platform="linux-x86_64",
                            version="1.0.0+" + revision[:12],
                        )
                    ).encode(),
                    "wheelhouse/aiops_diagnostics-1.0.0-py3-none-any.whl": b"fixture wheel",
                },
            ),
            (
                "installer",
                {
                    "REVISION": revision.encode(),
                    **{
                        n: b"# fixture"
                        for n in ("install.py", "probe.py", "runtime_check.py", "artifact_checks.py")
                    },
                },
            ),
        ):
            content["SHA256SUMS"] = "".join(
                hashlib.sha256(b).hexdigest() + "  " + n + "\n" for n, b in content.items()
            ).encode()
            file = tmp_path / (env + key)
            with tarfile.open(file, "w:gz") as tar:
                for n, b in content.items():
                    m = tarfile.TarInfo(n)
                    m.size = len(b)
                    tar.addfile(m, io.BytesIO(b))
            file.chmod(0o600)
            row[key] = dict(
                path=str(file), sha256=hashlib.sha256(file.read_bytes()).hexdigest(), revision=revision
            )
        config = write_private_text(tmp_path / (env + ".env"), "AIOPS_MYSQL_PASSWORD='fixture-secret'\n")
        row["configurations"] = [
            dict(id="config", path=str(config), sha256=hashlib.sha256(config.read_bytes()).hexdigest())
        ]
        targets.append(row)
    ref = write_private_text(
        tmp_path / "ref.json", json.dumps(dict(schema="aiops-environment-onboarding/1", targets=targets))
    )
    settings = SimpleNamespace(
        environment="control",
        environment_management_registry_file=str(path),
        environment_onboarding_file=str(ref),
        environment_task_key="worker-fixture",
    )
    context = SimpleNamespace(settings=settings)
    monkeypatch.setattr(
        auth, "identity", lambda *a, **kw: auth.ManagerIdentity("admin", "platform", True, "never-output")
    )
    app = FastAPI()
    for err, status in ((auth.SetupRefused, 403), (SetupConflict, 409)):

        async def handler(_request, exc, status=status):
            return JSONResponse(status_code=status, content={"error": {"code": str(exc)}})

        app.add_exception_handler(err, handler)
    management.register(app, context, lambda: "active-session")
    with TestClient(app) as client:
        yield path, ref, client, settings


def receipt(environment):
    now = time.time()
    ids = (
        "local-path-identity",
        "installed-artifact",
        "configuration",
        "registry",
        "recovery",
        "service-readiness",
        "schema-inflight",
        "resource-headroom",
        "managed-host-identity",
        "formal-network",
        "business",
    )
    return dict(
        id="site_" + "a" * 32,
        scope="installed-instance-readiness-only",
        environment=environment,
        version="1.19.0+" + "a" * 12,
        source_revision="a" * 40,
        artifact_sha256="a" * 64,
        config_sha256="b" * 64,
        registry_sha256="c" * 64,
        checks=[dict(id=k, state="pending" if k in ids[-3:] else "passed") for k in ids],
        state="observed",
        executable=False,
        created_at=now,
        expires_at=now + 900,
        resources=dict(available_memory_bytes=1024**3, available_disk_bytes=1024**3),
    )


def create(prepared, environment="a"):
    path, ref, client, _ = prepared
    plan = plans.preflight(path, ADMIN, environment, 1, 1, ref)
    r = client.post("/v1/dify/management/environment-tasks", json={"plan_id": plan["id"]})
    assert r.status_code == 200, r.text
    return r.json(), plan


def workspace_job(prepared, workspace_upstream, environment="a"):  # noqa: F811
    path, ref, client, settings = prepared
    settings.database_file = path.parent / "gateway.db"
    settings.dify_inner_api_base_url = settings.dify_console_base_url = workspace_upstream.base
    settings.dify_inner_api_key = settings.dify_console_api_key = "fixture-key"
    site, _ = create(prepared, environment)
    tasks.worker(path, ref, {"id": site["id"], "owner": "a" * 32})
    tasks.worker(path, ref, {"id": site["id"], "owner": "a" * 32, "result": receipt(environment)})
    response = client.post(
        "/v1/dify/management/environment-tasks", json={"site_task": site["id"], "tenant_id": "same"}
    )
    assert response.status_code == 200, response.text
    return response.json(), site


def prepare_http(prepared, job):
    path, ref, client, settings = prepared
    payload = {"id": job["id"], "owner": "b" * 32, "prepare": "workspace"}
    tasks.worker(path, ref, {"id": job["id"], "owner": payload["owner"]})
    headers = build_internal_token_headers(settings.environment_task_key, 300, int(time.time()))
    return client.post("/v1/internal/environment-tasks", json=payload, headers=headers)


def test_candidate_workspaces_are_scoped_and_reused_without_activation(prepared, workspace_upstream):  # noqa: F811
    path, ref, client, _ = prepared
    original = path.read_bytes()
    first, site = workspace_job(prepared, workspace_upstream)
    again = client.post(
        "/v1/dify/management/environment-tasks", json={"site_task": site["id"], "tenant_id": "same"}
    ).json()
    assert first["id"] == again["id"]
    observed = prepare_http(prepared, first).json()
    assert observed["state"] == "observed" and observed["executable"] is False
    assert observed["result"]["checks"] == [
        {"id": "workspace", "state": "passed"},
        *[{"id": k, "state": "pending"} for k in ("application", "knowledge", "business")],
    ]
    second, _ = workspace_job(prepared, workspace_upstream, "b")
    other = prepare_http(prepared, second).json()
    assert other["result"]["workspace_id"] != observed["result"]["workspace_id"]
    assert len(workspace_upstream.posts()) == 2 and path.read_bytes() == original
    with gateway_db.connection(drafts._database(path), write=True) as conn:
        conn.execute("UPDATE environment_plans SET expires_at=0")
    newer, _ = workspace_job(prepared, workspace_upstream)
    assert newer["id"] != first["id"]
    assert (
        prepare_http(prepared, newer).json()["result"]["workspace_id"] == observed["result"]["workspace_id"]
    )
    assert len(workspace_upstream.posts()) == 2


def test_candidate_rejects_unobserved_wrong_tenant_forged_results_and_old_lease(prepared, workspace_upstream):  # noqa: F811
    path, ref, client, _ = prepared
    job, site = workspace_job(prepared, workspace_upstream)
    for tenant in ("missing", ""):
        assert (
            client.post(
                "/v1/dify/management/environment-tasks", json={"site_task": site["id"], "tenant_id": tenant}
            ).status_code
            == 403
        )
    other, _ = create(prepared, "b")
    assert (
        client.post(
            "/v1/dify/management/environment-tasks", json={"site_task": other["id"], "tenant_id": "same"}
        ).status_code
        == 403
    )
    tasks.worker(path, ref, {"id": job["id"], "owner": "b" * 32})
    with pytest.raises(tasks.TaskRefused, match="RESULT_INVALID"):
        tasks.worker(
            path, ref, {"id": job["id"], "owner": "b" * 32, "result": {"scope": "candidate-workspace-only"}}
        )
    with pytest.raises(tasks.TaskRefused, match="LEASE_LOST"):
        management.prepare_workspace(
            SimpleNamespace(settings=prepared[3]),
            {"id": job["id"], "owner": "c" * 32, "prepare": "workspace"},
        )
    assert workspace_upstream.posts() == []


def test_candidate_first_create_checks_expiry_and_does_not_leave_running(prepared, workspace_upstream):  # noqa: F811
    path, ref, _, _ = prepared
    job, _ = workspace_job(prepared, workspace_upstream)
    tasks.worker(path, ref, {"id": job["id"], "owner": "b" * 32})
    with gateway_db.connection(drafts._database(path), write=True) as conn:
        request = json.loads(
            conn.execute("SELECT request FROM environment_tasks WHERE id=?", (job["id"],)).fetchone()[0]
        )
        request["plan"]["expires_at"] = time.time() - 1
        conn.execute("UPDATE environment_tasks SET request=? WHERE id=?", (json.dumps(request), job["id"]))
    result = prepare_http(prepared, job).json()
    assert result["state"] == "blocked" and result["error_code"] == "ENVIRONMENT_TASK_PLAN_EXPIRED"
    assert workspace_upstream.posts() == []


def test_candidate_checks_fresh_cancellation_and_site_receipt(prepared, workspace_upstream, monkeypatch):  # noqa: F811
    path, ref, client, _ = prepared
    job, _ = workspace_job(prepared, workspace_upstream)
    original = tasks.workspace_request
    calls = 0

    def cancel_between_reads(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            tasks.inspect(path, ADMIN, identifier=job["id"], cancel=True, reference=ref)
        return original(*args, **kwargs)

    monkeypatch.setattr(tasks, "workspace_request", cancel_between_reads)
    assert prepare_http(prepared, job).json()["state"] == "cancelled"
    monkeypatch.setattr(tasks, "workspace_request", original)
    other, site = workspace_job(prepared, workspace_upstream, "b")
    with gateway_db.connection(drafts._database(path), write=True) as conn:
        value = receipt("b")
        value["created_at"] -= 901
        value["expires_at"] -= 901
        conn.execute("UPDATE environment_tasks SET result=? WHERE id=?", (json.dumps(value), site["id"]))
    blocked = prepare_http(prepared, other).json()
    assert blocked["state"] == "blocked" and blocked["error_code"] == "ENVIRONMENT_TASK_OBSERVATION_REQUIRED"
    assert workspace_upstream.posts() == []


def test_candidate_legacy_uuid_with_empty_name_is_verified_and_filled(prepared, workspace_upstream):  # noqa: F811
    from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces

    path, ref, client, settings = prepared
    job, site = workspace_job(prepared, workspace_upstream)
    db = DifyWorkspaces(settings.database_file)
    db.claim("same", "a", owner="old", lease_seconds=1)
    db.mark_created("same", "a", "legacy")
    db.mark_ready("same", "a")
    workspace_upstream.workspaces.append({"id": "legacy", "name": "Original"})
    assert prepare_http(prepared, job).json()["state"] == "observed"
    assert db.row("same", "a")["workspace_name"] == "Original"
    assert db.row("same", "a")["setup_state"] == "ready"
    assert workspace_upstream.posts() == []


def test_cancelled_unknown_create_reconciles_and_ambiguity_blocks(prepared, workspace_upstream):  # noqa: F811
    from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces

    path, ref, client, settings = prepared
    job, _ = workspace_job(prepared, workspace_upstream)
    workspace_upstream.create_body = {"message": "ok"}
    assert prepare_http(prepared, job).json()["state"] == "reconcile"
    client.post("/v1/dify/management/environment-tasks", json={"id": job["id"], "cancel": True})
    assert prepare_http(prepared, job).json()["state"] == "reconcile"
    assert len(workspace_upstream.posts()) == 1
    name = "aiops-1-a-same"
    workspace_upstream.workspaces.append({"id": "recovered", "name": name})
    recovered = prepare_http(prepared, job).json()
    assert recovered["state"] == "cancelled" and recovered["result"]["workspace_id"] == "recovered"
    assert len(workspace_upstream.posts()) == 1
    other, _ = workspace_job(prepared, workspace_upstream, "b")
    workspaces = DifyWorkspaces(settings.database_file)
    name = "aiops-1-b-same"
    workspaces.claim("same", "b", owner="lost", lease_seconds=1, name=name)
    workspaces.mark_unknown("same", "b", error="timeout")
    workspace_upstream.workspaces.extend([{"id": "one", "name": name}, {"id": "two", "name": name}])
    blocked = prepare_http(prepared, other).json()
    assert blocked["state"] == "blocked" and blocked["error_code"] == "DIFY_WORKSPACE_AMBIGUOUS"
    assert len(workspace_upstream.posts()) == 1


def test_parallel_requests_share_durable_task_across_managers(prepared):
    path, ref, client, _ = prepared
    original = path.read_bytes()
    plan = plans.preflight(path, ADMIN, "a", 1, 1, ref)
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(
            pool.map(
                lambda i: tasks.submit(
                    path, ADMIN if i % 2 else ("control", "platform", "second"), plan["id"], ref
                ),
                range(8),
            )
        )
    assert len({r["id"] for r in rows}) == 1
    listed = client.post("/v1/dify/management/environment-tasks", json={"environment": "a"}).json()["tasks"]
    assert len(listed) == 1 and listed[0]["state"] == "queued"
    assert path.read_bytes() == original and "fixture-secret" not in json.dumps(listed)
    assert str(path.parent) not in json.dumps(listed)


def test_timeout_then_reclaim_rejects_stale_worker_and_other_environment(prepared):
    path, ref, client, _ = prepared
    job, _ = create(prepared)
    first = tasks.worker(path, ref, dict(id=job["id"], owner="a" * 32))
    assert first["claimed"]
    other, _ = create(prepared, "b")
    assert other["id"] != job["id"]
    with gateway_db.connection(drafts._database(path), write=True) as conn:
        conn.execute("UPDATE environment_tasks SET lease_until=0 WHERE id=?", (job["id"],))
    current = client.post("/v1/dify/management/environment-tasks", json={"id": job["id"]}).json()
    assert current["state"] == "reconcile"
    assert tasks.worker(path, ref, dict(id=job["id"], owner="b" * 32))["claimed"]
    with pytest.raises(tasks.TaskRefused, match="LEASE_LOST"):
        tasks.worker(path, ref, dict(id=job["id"], owner="a" * 32, result=receipt("a")))
    with pytest.raises(tasks.TaskRefused, match="RESULT_INVALID"):
        tasks.worker(path, ref, dict(id=job["id"], owner="b" * 32, result=receipt("b")))
    result = tasks.worker(path, ref, dict(id=job["id"], owner="b" * 32, result=receipt("a")))
    assert result["state"] == "observed" and result["executable"] is False
    assert plans.read(path, ADMIN, result["plan_id"], ref)["executable"] is False


def test_inflight_cancel_keeps_receipt_and_queued_cancel_never_claims(prepared):
    path, ref, client, _ = prepared
    job, _ = create(prepared)
    tasks.worker(path, ref, dict(id=job["id"], owner="a" * 32))
    cancelled = client.post(
        "/v1/dify/management/environment-tasks", json={"id": job["id"], "cancel": True}
    ).json()
    assert cancelled["state"] == "running" and cancelled["cancel_requested"]
    done = tasks.worker(path, ref, dict(id=job["id"], owner="a" * 32, result=receipt("a")))
    assert done["state"] == "cancelled" and done["result"]["id"].startswith("site_")
    other, _ = create(prepared, "b")
    client.post("/v1/dify/management/environment-tasks", json={"id": other["id"], "cancel": True})
    assert not tasks.worker(path, ref, dict(id=other["id"], owner="b" * 32))["claimed"]


def test_worker_hmac_separate_and_untrusted_browser_result_refused(prepared):
    path, ref, client, settings = prepared
    job, _ = create(prepared)
    payload = dict(id=job["id"], owner="a" * 32)
    assert client.post("/v1/internal/environment-tasks", json=payload).status_code == 401
    headers = build_internal_token_headers("worker-fixture", 300, int(time.time()))
    assert client.post("/v1/internal/environment-tasks", json=payload, headers=headers).json()["claimed"]
    assert (
        client.post(
            "/v1/dify/management/environment-tasks", json=dict(id=job["id"], result=receipt("a"))
        ).status_code
        == 403
    )
    settings.environment_task_key = ""
    assert client.post("/v1/internal/environment-tasks", json=payload, headers=headers).status_code == 404
    plan = plans.preflight(path, ADMIN, "b", 1, 1, ref)
    assert (
        client.post("/v1/dify/management/environment-tasks", json={"plan_id": plan["id"]}).status_code == 403
    )


def test_revocation_and_drift_prevent_new_execution(prepared):
    path, ref, client, _ = prepared
    job, _ = create(prepared)
    write_private_text(ref, "changed")
    with pytest.raises((tasks.TaskRefused, plans.PlanRefused, ValueError)):
        tasks.worker(path, ref, dict(id=job["id"], owner="a" * 32))
    body = registry.load(path)
    registry.apply(path, body["environments"], expected_revision=1, actor="revoke", managers=[])
    assert client.post("/v1/dify/management/environment-tasks", json={"id": job["id"]}).status_code == 403


def test_worker_reconciles_transport_loss_and_uses_one_task(prepared, tmp_path, monkeypatch):
    import subprocess

    from aiops_diagnostics.gateway import environment_task_worker as worker

    path, ref, client, _ = prepared
    job, _ = create(prepared)
    calls = []

    class Response:
        def __init__(self, content):
            self.content = content

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, _):
            return self.content

    def remote(spec):
        assert spec.follow_redirects is False and spec.timeout == 30
        response = client.post("/v1/internal/environment-tasks", content=spec.body, headers=spec.headers)
        assert response.status_code == 200, response.text
        return Response(response.content)

    monkeypatch.setattr(worker, "open_response", remote)
    monkeypatch.setattr(worker.subprocess, "check_output", lambda *a, **kw: "")
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **kw: None)

    def lost(root, claim):
        calls.append(claim)
        raise subprocess.TimeoutExpired("dev-host", 60)

    monkeypatch.setattr(worker, "execute", lost)
    first = worker.run(tmp_path, "https://control.example", "worker-fixture", job["id"])
    assert first["state"] == "reconcile"
    monkeypatch.setattr(worker, "execute", lambda root, claim: receipt(claim["environment"]))
    second = worker.run(tmp_path, "https://control.example", "worker-fixture", job["id"])
    assert second["id"] == first["id"] and second["state"] == "observed"
    assert calls[0]["target"]["host"] == "managed-a"


@pytest.mark.parametrize(
    "change",
    [
        {"state": "completed"},
        {"executable": True},
        {"environment": "b"},
        {"version": "secret-fixture"},
        {"created_at": float("nan")},
        {"resources": {"password": "secret"}},
        {"state": []},
    ],
)
def test_result_schema_never_exposes_worker_supplied_secrets(change):
    value = {**receipt("a"), **change}
    with pytest.raises(tasks.TaskRefused, match="RESULT_INVALID"):
        tasks.validate_receipt(value, "a")


def test_only_unknown_observation_may_reconcile_after_plan_expires(prepared, monkeypatch):
    path, ref, _, _ = prepared
    job, plan = create(prepared)
    tasks.worker(path, ref, {"id": job["id"], "owner": "a" * 32})
    monkeypatch.setattr(time, "time", lambda: plan["expires_at"] + 1)
    assert tasks.worker(path, ref, {"id": job["id"], "owner": "b" * 32})["claimed"]
    assert (
        tasks.worker(path, ref, {"id": job["id"], "owner": "b" * 32, "result": receipt("a")})["state"]
        == "observed"
    )


def test_worker_builds_merged_installer_and_only_invokes_preflight(prepared, tmp_path, monkeypatch):
    import subprocess
    from pathlib import Path

    from aiops_diagnostics.gateway import environment_task_worker as worker

    _, ref, _, _ = prepared
    claim = {"target": dict(host="singapore-server", user="aiops", port=18951), "environment": "a"}
    root = Path(__file__).resolve().parents[2]
    revision = subprocess.check_output(["git", "rev-parse", "origin/main"], cwd=root, text=True).strip()
    archive = tmp_path / "actual-installer.tar.gz"
    subprocess.run(
        ["bash", "ops/environments/gateway/build-installer.sh", revision, str(archive)], cwd=root, check=True
    )
    claim["installer"] = dict(revision=revision, sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
    commands = []
    run = subprocess.run
    output = subprocess.check_output

    def run_fake(args, **kwargs):
        if args[0] != "dev-host":
            return run(args, **kwargs)
        commands.append(args)
        if "input" in kwargs:
            compile(kwargs["input"], "remote-probe", "exec")
        return subprocess.CompletedProcess(args, 0, "", "")

    def output_fake(args, **kwargs):
        if args[0] != "dev-host":
            return output(args, **kwargs)
        commands.append(args)
        return json.dumps(receipt("a"))

    monkeypatch.setattr(worker.subprocess, "run", run_fake)
    monkeypatch.setattr(worker.subprocess, "check_output", output_fake)
    assert worker.execute(root, claim)["state"] == "observed"
    assert commands[-1][-1].endswith("preflight --environment a --user aiops --port 18951")
    assert all("systemctl" not in str(command) for command in commands)
    assert all(command[2] == "124" for command in commands)
    bad = {**claim, "target": {**claim["target"], "host": "--project"}}
    with pytest.raises(ValueError, match="fixed target"):
        worker.execute(root, bad)


def test_worker_key_must_not_reuse_existing_service_secrets(tmp_path):
    from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings

    for key in (
        "internal_token_secret",
        "dify_publish_api_key",
        "dify_trial_api_key",
        "dify_knowledge_api_key",
        "dify_debug_api_key",
    ):
        settings = GatewayServerSettings(
            data_home=tmp_path,
            database_file=tmp_path / "db",
            environment_task_key="x" * 40,
            **{key: "x" * 40},
        )
        with pytest.raises(ValueError, match="task key must be independent"):
            settings.validate()


def test_absent_queue_and_unknown_id_are_not_server_errors(prepared):
    _, _, client, _ = prepared
    assert client.post("/v1/dify/management/environment-tasks", json={"environment": "a"}).json() == {
        "tasks": []
    }
    headers = build_internal_token_headers("worker-fixture", 300, int(time.time()))
    response = client.post(
        "/v1/internal/environment-tasks", json=dict(id="task_" + "f" * 32, owner="a" * 32), headers=headers
    )
    assert response.status_code in (403, 409)


def test_listing_never_creates_missing_draft_database(prepared):
    path, _, client, _ = prepared
    db = drafts._database(path)
    db.unlink()
    for suffix in ("-wal", "-shm"):
        db.with_name(db.name + suffix).unlink(missing_ok=True)
    assert client.post("/v1/dify/management/environment-tasks", json={"environment": "a"}).json() == {
        "tasks": []
    }
    assert not db.exists()


def test_plan_drift_during_and_after_report_expires_preserved_receipt(prepared):
    path, ref, client, _ = prepared
    job, _ = create(prepared)
    tasks.worker(path, ref, dict(id=job["id"], owner="a" * 32))
    draft = json.loads(drafts._rows(drafts._database(path))[0]["body"])
    draft["name"] = "changed during observation"
    drafts.save(path, ADMIN, draft, expected_revision=1, registry_revision=1)
    result = tasks.worker(path, ref, dict(id=job["id"], owner="a" * 32, result=receipt("a")))
    assert result["state"] == "expired" and result["result"]["id"].startswith("site_")
    other, _ = create(prepared, "b")
    tasks.worker(path, ref, dict(id=other["id"], owner="b" * 32))
    tasks.worker(path, ref, dict(id=other["id"], owner="b" * 32, result=receipt("b")))
    write_private_text(ref, "changed after observation")
    assert (
        client.post("/v1/dify/management/environment-tasks", json={"id": other["id"]}).json()["state"]
        == "expired"
    )


def test_unknown_expired_lease_cannot_run_after_input_changes(prepared):
    path, ref, _, _ = prepared
    job, _ = create(prepared)
    tasks.worker(path, ref, dict(id=job["id"], owner="a" * 32))
    tasks.worker(path, ref, dict(id=job["id"], owner="a" * 32, error="ENVIRONMENT_TASK_TARGET_UNCONFIRMED"))
    assert tasks.inspect(path, ADMIN, identifier=job["id"], reference=ref)["state"] == "reconcile"
    write_private_text(ref, "changed")
    with pytest.raises((ValueError, tasks.TaskRefused)):
        tasks.worker(path, ref, dict(id=job["id"], owner="b" * 32))


def test_cancelled_expired_lease_still_reconciles_and_keeps_result(prepared):
    path, ref, client, _ = prepared
    job, _ = create(prepared)
    tasks.worker(path, ref, dict(id=job["id"], owner="a" * 32))
    with gateway_db.connection(drafts._database(path), write=True) as conn:
        conn.execute("UPDATE environment_tasks SET lease_until=0 WHERE id=?", (job["id"],))
    result = client.post(
        "/v1/dify/management/environment-tasks", json={"id": job["id"], "cancel": True}
    ).json()
    assert result["state"] == "reconcile" and result["cancel_requested"]
    assert tasks.worker(path, ref, dict(id=job["id"], owner="b" * 32))["claimed"]
    result = tasks.worker(path, ref, dict(id=job["id"], owner="b" * 32, result=receipt("a")))
    assert result["state"] == "cancelled" and result["result"]["state"] == "observed"


def test_terminal_worker_repeat_expires_after_input_drift(prepared):
    path, ref, client, _ = prepared
    job, _ = create(prepared)
    tasks.worker(path, ref, dict(id=job["id"], owner="a" * 32))
    tasks.worker(path, ref, dict(id=job["id"], owner="a" * 32, result=receipt("a")))
    draft = json.loads(drafts._rows(drafts._database(path))[0]["body"])
    draft["name"] = "new name after completion"
    drafts.save(path, ADMIN, draft, expected_revision=1, registry_revision=1)
    before = tasks.inspect(path, ADMIN, identifier=job["id"], reference=ref)
    repeated = tasks.worker(path, ref, dict(id=job["id"], owner="b" * 32))
    assert before["state"] == repeated["task"]["state"] == "expired"
    assert repeated["claimed"] is False and repeated["task"]["result"]["id"].startswith("site_")


def test_automatic_claim_skips_expired_head_and_leases_one_live_task(prepared, monkeypatch):
    path, ref, _, _ = prepared
    first, first_plan = create(prepared)
    second, _ = create(prepared, "b")
    with gateway_db.connection(drafts._database(path), write=True) as conn:
        request = json.loads(
            conn.execute("SELECT request FROM environment_tasks WHERE id=?", (first["id"],)).fetchone()[0]
        )
        request["plan"]["expires_at"] = time.time() - 1
        conn.execute("UPDATE environment_tasks SET request=? WHERE id=?", (json.dumps(request), first["id"]))
    claimed = tasks.worker(path, ref, {"owner": "a" * 32})
    assert claimed["claimed"] and claimed["id"] == second["id"]
    assert tasks.inspect(path, ADMIN, identifier=first["id"], reference=ref)["state"] == "expired"
    assert tasks.worker(path, ref, {"owner": "b" * 32}) == {"claimed": False, "task": None}
    tasks.worker(path, ref, {"id": second["id"], "owner": "a" * 32, "result": receipt("b")})
    assert tasks.worker(path, ref, {"owner": "c" * 32}) == {"claimed": False, "task": None}


def test_automatic_unknown_retries_later_and_keeps_cancelled_reconciliation(prepared, monkeypatch):
    path, ref, client, _ = prepared
    job, _ = create(prepared)
    assert tasks.worker(path, ref, {"owner": "a" * 32})["id"] == job["id"]
    tasks.worker(
        path, ref, {"id": job["id"], "owner": "a" * 32, "error": "ENVIRONMENT_TASK_TARGET_UNCONFIRMED"}
    )
    assert tasks.worker(path, ref, {"owner": "b" * 32})["claimed"] is False
    client.post("/v1/dify/management/environment-tasks", json={"id": job["id"], "cancel": True})
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 61)
    assert tasks.worker(path, ref, {"owner": "b" * 32})["claimed"]
    result = tasks.worker(path, ref, {"id": job["id"], "owner": "b" * 32, "result": receipt("a")})
    assert result["state"] == "cancelled" and result["result"]["state"] == "observed"


def test_automatic_absent_database_is_read_only(prepared):
    path, ref, _, _ = prepared
    database = drafts._database(path)
    database.unlink()
    assert tasks.worker(path, ref, {"owner": "a" * 32}) == {"claimed": False, "task": None}
    assert not database.exists()


def test_central_transport_keeps_payload_out_of_shell_and_rejects_unknown_target(tmp_path, monkeypatch):
    from aiops_diagnostics.gateway import environment_task_worker as worker

    (tmp_path / ".dev-host.toml").write_text('[[targets]]\nname="41"\nhost="known-central"\n')
    calls = []

    def capture(args, **kwargs):
        calls.append((args, kwargs))
        return '{"claimed":false,"task":null}'

    monkeypatch.setattr(worker.subprocess, "check_output", capture)
    payload = {"owner": "a" * 32, "result": {"name": "$(touch /tmp/do-not-create)"}}
    assert worker.central_call(tmp_path, "41", payload) == {"claimed": False, "task": None}
    assert "$(touch" not in calls[0][0][-1]
    assert json.loads(calls[0][1]["input"]) == payload
    with pytest.raises(ValueError):
        worker.central_call(tmp_path, "--project", payload)
    with pytest.raises(ValueError):
        worker.central_call(tmp_path, "unknown", payload)
    assert len(calls) == 1


def test_automatic_worker_respects_task_selected_by_central(prepared, tmp_path, monkeypatch):
    from aiops_diagnostics.gateway import environment_task_worker as worker

    path, ref, _, _ = prepared
    job, _ = create(prepared)
    monkeypatch.setattr(worker.subprocess, "check_output", lambda *a, **kw: "")
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **kw: None)
    monkeypatch.setattr(
        worker,
        "central_call",
        lambda root, target, payload: (
            {"advanced": 0}
            if payload == {"advance_candidates": True}
            else {"claimed": False, "task": None}
            if payload.get("installation_claim") is True
            else tasks.worker(path, ref, payload)
        ),
    )
    monkeypatch.setattr(worker, "execute", lambda root, claim: receipt(claim["environment"]))
    result = worker.run(tmp_path, "", "", central_target="41")
    assert result["state"] == "observed" and result["id"] == job["id"]
    assert worker.run(tmp_path, "", "", central_target="41") is None


def test_central_dispatch_uses_fixed_service_identity_and_local_binding(tmp_path, monkeypatch):
    import os
    import pwd

    from typer.testing import CliRunner

    from aiops_diagnostics.admin_cli import admin_app
    from aiops_diagnostics.common import private_files
    from aiops_diagnostics.gateway import environment_task_worker as worker
    from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings

    settings = SimpleNamespace(
        environment="41", environment_task_key="private-central-only", bind_host="127.0.0.1", port=18959
    )
    monkeypatch.setattr(pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=os.getuid()))
    monkeypatch.setattr(private_files, "validate_private_file", lambda _: None)
    monkeypatch.setattr(GatewayServerSettings, "from_env", lambda: settings)
    calls = []
    monkeypatch.setattr(
        worker,
        "call",
        lambda url, key, payload: calls.append((url, key, payload)) or {"claimed": False, "task": None},
    )
    result = CliRunner().invoke(
        admin_app,
        ["--config", "/etc/aiops-41/gateway.env", "environment-task-dispatch"],
        input='{"owner":"' + "a" * 32 + '"}',
    )
    assert result.exit_code == 0, result.output
    assert calls == [("http://127.0.0.1:18959", "private-central-only", {"owner": "a" * 32})]
    assert "private-central-only" not in result.output
    settings.bind_host = "192.0.2.123"
    refused = CliRunner().invoke(
        admin_app, ["--config", "/etc/aiops-41/gateway.env", "environment-task-dispatch"], input="{}"
    )
    assert refused.exit_code == 1 and len(calls) == 1


def test_cancel_request_with_unknown_result_must_keep_automatic_reconciliation(prepared, monkeypatch):
    path, ref, client, _ = prepared
    job, _ = create(prepared)
    tasks.worker(path, ref, {"owner": "a" * 32})
    client.post("/v1/dify/management/environment-tasks", json={"id": job["id"], "cancel": True})
    unknown = tasks.worker(
        path, ref, {"id": job["id"], "owner": "a" * 32, "error": "ENVIRONMENT_TASK_TARGET_UNCONFIRMED"}
    )
    assert unknown["state"] == "reconcile" and unknown["cancel_requested"]
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 61)
    assert tasks.worker(path, ref, {"owner": "b" * 32})["claimed"]
    observed = tasks.worker(path, ref, {"id": job["id"], "owner": "b" * 32, "result": receipt("a")})
    assert observed["state"] == "cancelled" and observed["result"]["state"] == "observed"


def test_busy_environment_does_not_fill_automatic_queue_window(prepared):
    path, ref, _, _ = prepared
    first, _ = create(prepared)
    second, _ = create(prepared, "b")
    tasks.worker(path, ref, {"id": first["id"], "owner": "a" * 32})
    with gateway_db.connection(drafts._database(path), write=True) as conn:
        body = conn.execute("SELECT request FROM environment_tasks WHERE id=?", (first["id"],)).fetchone()[0]
        conn.execute("UPDATE environment_tasks SET updated_at=? WHERE id=?", (time.time() + 20, second["id"]))
        for index in range(64):
            conn.execute(
                "INSERT INTO environment_tasks(id,environment,request,state,updated_at) "
                "VALUES(?,?,?,'queued',?)",
                ("task_" + f"{index + 1:032x}", "a", body, time.time() - 10),
            )
    assert tasks.worker(path, ref, {"owner": "b" * 32})["id"] == second["id"]


def test_local_trial_dispatch_pins_loopback_source(tmp_path, monkeypatch):
    import http.client
    import os
    import pwd
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from typer.testing import CliRunner

    from aiops_diagnostics.admin_cli import admin_app
    from aiops_diagnostics.common import private_files
    from aiops_diagnostics.common.http_auth import validate_internal_token
    from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings

    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.client_address[0], self.path, body))
            assert validate_internal_token(
                self.headers.get("X-Internal-Token"), self.headers.get("X-Request-Timestamp"), "synthetic-key"
            )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"candidate": {}, "activated": false}')

    server = ThreadingHTTPServer(("127.0.0.2", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = SimpleNamespace(
        environment="41", environment_task_key="synthetic-key", bind_host="127.0.0.2", port=server.server_port
    )
    monkeypatch.setattr(pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=os.getuid()))
    monkeypatch.setattr(private_files, "validate_private_file", lambda _: None)
    monkeypatch.setattr(GatewayServerSettings, "from_env", lambda: settings)
    original, addresses = http.client.HTTPConnection, []

    def connect(*args, **kwargs):
        addresses.append(kwargs["source_address"])
        return original(*args, **kwargs)

    monkeypatch.setattr(http.client, "HTTPConnection", connect)
    try:
        result = CliRunner().invoke(
            admin_app,
            ["--config", "/etc/aiops-41/gateway.env", "environment-trial-dispatch"],
            input='{"action":"consumer-describe","task_id":"synthetic"}',
        )
        assert result.exit_code == 0, result.output
        assert addresses == [("127.0.0.1", 0)]
        assert calls == [
            (
                "127.0.0.1",
                "/v1/internal/environment-trial",
                {"action": "consumer-describe", "task_id": "synthetic"},
            )
        ]
        assert "synthetic-key" not in result.output
        settings.bind_host = "192.0.2.123"
        refused = CliRunner().invoke(
            admin_app, ["--config", "/etc/aiops-41/gateway.env", "environment-trial-dispatch"], input="{}"
        )
        assert refused.exit_code == 1 and len(calls) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def verdict(environment, *, blocked=False, **overrides):
    now = time.time()
    evidence = dict(
        declared_target="singapore-server",
        resolved_host="124",
        remote_hostname="ecs-2c54",
        machine_id_sha256="d" * 64,
        service_user_present=True,
    )
    if blocked:
        evidence.update(remote_hostname="", machine_id_sha256="", service_user_present=False)
    value = dict(
        id="verify_" + "a" * 32,
        scope="managed-host-verification-only",
        environment=environment,
        check="managed-host-identity",
        state="blocked" if blocked else "observed",
        executable=False,
        evidence=evidence,
        created_at=now,
        expires_at=now + 900,
    )
    value.update(overrides)
    return value


def verify_job(prepared, environment="a"):
    path, ref, client, _ = prepared
    plan = plans.preflight(path, ADMIN, environment, 1, 1, ref)
    response = client.post(
        "/v1/dify/management/environment-tasks",
        json={"verify_task": "managed-host-identity", "plan_id": plan["id"]},
    )
    assert response.status_code == 200, response.text
    return response.json(), plan


def test_host_verify_dedupes_per_plan_and_names_check(prepared):
    path, _, client, _ = prepared
    first, plan = verify_job(prepared)
    again = client.post(
        "/v1/dify/management/environment-tasks",
        json={"verify_task": "managed-host-identity", "plan_id": plan["id"]},
    )
    assert again.status_code == 200 and again.json()["id"] == first["id"]
    assert first["action"] == "host-verify" and first["executable"] is False
    other, _ = verify_job(prepared, environment="b")
    assert other["id"] != first["id"] and other["environment"] == "b"


def test_host_verify_rejects_unknown_check_and_bad_payload(prepared):
    _, _, client, _ = prepared
    plan = plans.preflight(*(prepared[0], ADMIN, "a", 1, 1, prepared[1]))
    refused = client.post(
        "/v1/dify/management/environment-tasks",
        json={"verify_task": "formal-network", "plan_id": plan["id"]},
    )
    assert refused.status_code in (403, 409), refused.text
    extra = client.post(
        "/v1/dify/management/environment-tasks",
        json={"verify_task": "managed-host-identity", "plan_id": plan["id"], "evidence": {}},
    )
    assert extra.status_code in (403, 409, 422), extra.text


@pytest.mark.parametrize(
    "change",
    [
        {"scope": "installed-instance-readiness-only"},
        {"check": "formal-network"},
        {"environment": "b"},
        {"executable": True},
        {"id": "site_" + "a" * 32},
        {"state": "passed"},
        {"created_at": float("nan")},
        {"evidence": {}},
        {"extra": "x"},
    ],
)
def test_verify_schema_rejects_forged_or_alien_results(change):
    value = {**verdict("a"), **change}
    with pytest.raises(tasks.TaskRefused, match="RESULT_INVALID"):
        tasks.validate_verify(value, "a")


@pytest.mark.parametrize(
    "state,evidence_change",
    [
        ("observed", {"remote_hostname": ""}),
        ("observed", {"machine_id_sha256": "z" * 64}),
        ("observed", {"machine_id_sha256": ""}),
        ("observed", {"service_user_present": False}),
        (
            "blocked",
            {"remote_hostname": "x", "machine_id_sha256": "d" * 64, "service_user_present": True},
        ),
    ],
)
def test_verify_state_must_match_captured_evidence(state, evidence_change):
    value = verdict("a", blocked=True)
    value["state"] = state
    value["evidence"].update(evidence_change)
    with pytest.raises(tasks.TaskRefused, match="RESULT_INVALID"):
        tasks.validate_verify(value, "a")


def test_verify_blocked_with_partial_evidence_is_honest(prepared):
    path, ref, _, _ = prepared
    job, _ = verify_job(prepared)
    tasks.worker(path, ref, {"id": job["id"], "owner": "a" * 32})
    value = verdict("a", blocked=True)
    value["evidence"]["remote_hostname"] = "ecs-2c54"
    done = tasks.worker(path, ref, {"id": job["id"], "owner": "a" * 32, "result": value})
    assert done["state"] == "blocked" and done["result"]["evidence"]["remote_hostname"] == "ecs-2c54"


def test_host_verify_claim_result_and_cancel(prepared):
    path, ref, _, _ = prepared
    job, _ = verify_job(prepared)
    claim = tasks.worker(path, ref, {"id": job["id"], "owner": "a" * 32})
    assert claim["claimed"] and claim["action"] == "host-verify"
    assert claim["check"] == "managed-host-identity" and claim["target"]["host"] == "managed-a"
    with pytest.raises(tasks.TaskRefused, match="RESULT_INVALID"):
        tasks.worker(path, ref, {"id": job["id"], "owner": "a" * 32, "result": receipt("a")})
    done = tasks.worker(path, ref, {"id": job["id"], "owner": "a" * 32, "result": verdict("a")})
    assert done["state"] == "observed" and done["result"]["scope"] == "managed-host-verification-only"
    blocked_job, _ = verify_job(prepared, environment="b")
    tasks.worker(path, ref, {"id": blocked_job["id"], "owner": "b" * 32})
    blocked = tasks.worker(
        path, ref, {"id": blocked_job["id"], "owner": "b" * 32, "result": verdict("b", blocked=True)}
    )
    assert blocked["state"] == "blocked"


def test_host_verify_worker_collects_readonly_facts(monkeypatch):
    from pathlib import Path

    from aiops_diagnostics.gateway import environment_task_worker as worker

    claim = {
        "target": dict(host="singapore-server", user="aiops", port=18951),
        "environment": "a",
        "check": "managed-host-identity",
    }
    root = Path(__file__).resolve().parents[2]
    commands = []

    def output_fake(args, **kwargs):
        commands.append(args)
        compile(kwargs["input"], "remote-verify", "exec")
        assert "systemctl" not in kwargs["input"] and "curl" not in kwargs["input"]
        return json.dumps(
            {"hostname": "ecs-2c54", "machine_id_sha256": "e" * 64, "service_user_present": True}
        )

    monkeypatch.setattr(worker.subprocess, "check_output", output_fake)
    result = worker.verify(root, claim)
    assert result["state"] == "observed" and result["scope"] == "managed-host-verification-only"
    assert result["evidence"] == {
        "declared_target": "singapore-server",
        "resolved_host": "124",
        "remote_hostname": "ecs-2c54",
        "machine_id_sha256": "e" * 64,
        "service_user_present": True,
    }
    assert commands[0][1] == "exec" and commands[0][2] == "124" and "--allow-service-exec" in commands[0]
    missing = dict(claim["target"], user="no-such-user-ok")
    facts = json.dumps({"hostname": "ecs-2c54", "machine_id_sha256": "e" * 64, "service_user_present": False})
    monkeypatch.setattr(worker.subprocess, "check_output", lambda *a, **kw: facts)
    assert worker.verify(root, {**claim, "target": missing})["state"] == "blocked"
    bad = {**claim, "target": {**claim["target"], "host": "--project"}}
    with pytest.raises(ValueError, match="fixed target"):
        worker.verify(root, bad)


def test_host_verify_run_dispatches_and_unreachable_reconciles(prepared, tmp_path, monkeypatch):
    import subprocess

    from aiops_diagnostics.gateway import environment_task_worker as worker

    path, ref, client, _ = prepared
    job, _ = verify_job(prepared)

    class Response:
        def __init__(self, content):
            self.content = content

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, _):
            return self.content

    def remote(spec):
        response = client.post("/v1/internal/environment-tasks", content=spec.body, headers=spec.headers)
        assert response.status_code == 200, response.text
        return Response(response.content)

    monkeypatch.setattr(worker, "open_response", remote)
    monkeypatch.setattr(worker.subprocess, "check_output", lambda *a, **kw: "")
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **kw: None)
    monkeypatch.setattr(
        worker, "verify", lambda *a, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired("d", 30))
    )
    first = worker.run(tmp_path, "https://control.example", "worker-fixture", job["id"])
    assert first["state"] == "reconcile"
    monkeypatch.setattr(worker, "verify", lambda root, claim: verdict(claim["environment"]))
    second = worker.run(tmp_path, "https://control.example", "worker-fixture", job["id"])
    assert second["id"] == first["id"] and second["state"] == "observed"


def test_submit_verify_rejects_unhashable_check(prepared):
    _, _, client, _ = prepared
    plan = plans.preflight(*(prepared[0], ADMIN, "a", 1, 1, prepared[1]))
    refused = client.post(
        "/v1/dify/management/environment-tasks",
        json={"verify_task": ["managed-host-identity"], "plan_id": plan["id"]},
    )
    assert refused.status_code in (403, 409), refused.text
    assert "CHECK_INVALID" in refused.text


@pytest.mark.parametrize(
    "key,value",
    [
        ("declared_target", "a" * 32),
        ("resolved_host", "b" * 32),
        ("remote_hostname", "c" * 32),
    ],
)
def test_verify_schema_rejects_raw_machine_id_leak(key, value):
    result = verdict("a", blocked=True)
    result["evidence"][key] = value
    with pytest.raises(tasks.TaskRefused, match="RESULT_INVALID"):
        tasks.validate_verify(result, "a")


def test_host_verify_worker_unresolved_declaration_is_blocked(tmp_path, monkeypatch):
    from aiops_diagnostics.gateway import environment_task_worker as worker

    claim = {
        "target": dict(host="singapore-server", user="aiops", port=18951),
        "environment": "a",
        "check": "managed-host-identity",
    }
    (tmp_path / ".dev-host.toml").write_text(
        '[[targets]]\nname="x"\nhost="singapore-server"\n[[targets]]\nname="y"\nhost="singapore-server"\n'
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("unresolved declaration must not reach the target")

    monkeypatch.setattr(worker.subprocess, "check_output", forbidden)
    result = worker.verify(tmp_path, claim)
    assert result["state"] == "blocked"
    assert result["evidence"]["declared_target"] == "singapore-server"
    assert result["evidence"]["resolved_host"] == ""
    (tmp_path / ".dev-host.toml").write_text('[[targets]]\nname="other"\nhost="elsewhere"\n')
    absent = worker.verify(tmp_path, claim)
    assert absent["state"] == "blocked" and absent["evidence"]["resolved_host"] == ""


def test_host_verify_worker_malformed_output_is_unconfirmed(tmp_path, monkeypatch):
    from aiops_diagnostics.gateway import environment_task_worker as worker

    claim = {
        "target": dict(host="singapore-server", user="aiops", port=18951),
        "environment": "a",
        "check": "managed-host-identity",
    }
    (tmp_path / ".dev-host.toml").write_text('[[targets]]\nname="124"\nhost="singapore-server"\n')
    for output in ("", "   \n", "123", '"text"', "[1]", "noise\n42"):
        monkeypatch.setattr(worker.subprocess, "check_output", lambda *a, _o=output, **kw: _o)
        with pytest.raises(ValueError):
            worker.verify(tmp_path, claim)
