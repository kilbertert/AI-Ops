"""D23：中央任务跨请求持久去重、租约核对与安全取消。"""

import hashlib
import io
import json
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
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
    claim = {"target": dict(host="fixture-host", user="aiops", port=18951), "environment": "a"}
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
    bad = {**claim, "target": {**claim["target"], "host": "--project"}}
    with pytest.raises(ValueError, match="fixed target"):
        worker.execute(root, bad)


def test_worker_key_must_not_reuse_existing_service_secrets(tmp_path):
    from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings

    for key in ("internal_token_secret", "dify_publish_api_key", "dify_trial_api_key"):
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
