"""D37：真实管理/内部API上的安装任务、私有导出与原子任务兼容。"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from dify.test_dify_ensure_workspace import upstream as workspace_upstream  # noqa: F401

from aiops_diagnostics.common.http_auth import build_internal_token_headers
from aiops_diagnostics.common.private_files import write_private_text
from aiops_diagnostics.dify import environment_candidate_preparation as preparation
from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import environment_tasks as tasks
from aiops_diagnostics.gateway import gateway_db
from gateway.test_candidate_knowledge_connection import connected  # noqa: F401
from gateway.test_environment_consumer import consumer_app  # noqa: F401
from gateway.test_environment_knowledge import knowledge  # noqa: F401
from gateway.test_environment_runtime import wiring  # noqa: F401
from gateway.test_environment_tasks import ADMIN, prepared  # noqa: F401


@pytest.fixture
def installation_state(wiring, tmp_path):  # noqa: F811
    settings, payload, node, output, fixture, *_ = wiring
    path, ref, client, _ = fixture
    with gateway_db.connection(drafts._database(path), write=True) as conn:
        source = tasks._row(conn, payload["consumer_task"])
        request = json.loads(source["request"])
        site = tasks._row(conn, request["site_task"])
        observed = json.loads(site["result"])
        observed["runtime_sha256"] = "absent"
        conn.execute("UPDATE environment_tasks SET result=? WHERE id=?", (json.dumps(observed), site["id"]))
        request.update(
            action="candidate-prepare",
            bindings=preparation._bindings(settings, request["consumer_source"]),
            children={
                "site-preflight": site["id"],
                "operator-prepare": payload["operator_task"],
                "consumer-prepare": payload["consumer_task"],
            },
        )
        parent = "task_" + "f" * 32
        conn.execute(
            "INSERT INTO environment_tasks(id,environment,request,state,result,updated_at) "
            "VALUES(?,?,?,'observed',?,?)",
            (parent, "a", json.dumps(request), source["result"], time.time()),
        )
    settings.environment_installation_file = str(
        write_private_text(
            tmp_path / "installation.json",
            json.dumps(
                {
                    "schema": "aiops-environment-installation/1",
                    "targets": [{"id": "host-a", "node_file": str(node)}],
                }
            ),
        )
    )
    settings.data_home = tmp_path / "data"
    settings.data_home.mkdir(mode=0o700)
    return fixture, parent, settings


def internal(state, payload):
    return state[0][2].post(
        "/v1/internal/environment-tasks",
        json=payload,
        headers=build_internal_token_headers(state[2].environment_task_key, 300, int(time.time())),
    )


def submit(state):
    return state[0][2].post(
        "/v1/dify/management/environment-tasks", json={"candidate_task": state[1], "installation": True}
    )


def test_persistent_installation_uses_original_export_and_old_worker_cannot_claim(installation_state):
    state = installation_state
    response = submit(state)
    assert response.status_code == 200, response.text
    identifier = response.json()["id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        repeated = list(pool.map(lambda _: submit(state), range(6)))
    assert all(r.status_code == 200 and r.json()["id"] == identifier for r in repeated)
    with pytest.raises(tasks.TaskRefused):
        tasks.worker(state[0][0], state[0][1], {"id": identifier, "owner": "a" * 32})
    owner = "b" * 32
    claim = internal(state, {"installation_claim": True, "owner": owner})
    assert claim.status_code == 200 and claim.json()["id"] == identifier
    stage = internal(state, {"installation": "stage", "id": identifier, "owner": owner})
    assert stage.status_code == 200, stage.text
    body = stage.json()
    assert body["observe_only"] is False and set(body["archives"]) == {"application", "candidate", "runtime"}
    assert "synthetic-node-secret" not in json.dumps(body)
    repeated_stage = internal(state, {"installation": "stage", "id": identifier, "owner": owner})
    assert repeated_stage.json()["observe_only"] is True
    fake = {
        "id": identifier,
        "environment": "a",
        "scope": "candidate-installation-only",
        "state": "observed",
        "executable": False,
        "activation": "not_executed",
        "registration": "not_executed",
        "business": "unverified",
    }
    assert (
        internal(
            state, {"installation": "complete", "id": identifier, "owner": owner, "result": fake}
        ).status_code
        == 409
    )


def test_installation_staging_works_with_read_only_control_config(installation_state, tmp_path, monkeypatch):
    import errno

    state = installation_state
    config = tmp_path / "read-only-config"
    reference = write_private_text(
        config / "installation.json", Path(state[2].environment_installation_file).read_text()
    )
    state[2].environment_installation_file = str(reference)
    original = reference.read_bytes()
    mkdir = Path.mkdir

    def protected_mkdir(path, *args, **kwargs):
        if path == config or config in path.parents:
            raise OSError(errno.EROFS, "read-only control configuration")
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", protected_mkdir)
    identifier = submit(state).json()["id"]
    owner = "a" * 32
    assert internal(state, {"installation_claim": True, "owner": owner}).json()["id"] == identifier
    payload = {"installation": "stage", "id": identifier, "owner": owner}
    response = internal(state, payload)
    assert response.status_code == 200, response.text
    staged = response.json()
    directory = state[2].data_home / "installation-tasks" / identifier
    for name in ("candidate", "runtime"):
        archive = Path(staged["archives"][name]["path"])
        assert archive.parent == directory and archive.is_file()
        assert archive.stat().st_mode & 0o077 == 0
    assert directory.stat().st_mode & 0o077 == 0
    assert reference.read_bytes() == original and not (config / "installation-tasks").exists()
    repeated = internal(state, payload).json()
    assert repeated["observe_only"] is True and repeated["archives"] == staged["archives"]


@pytest.mark.parametrize("change", ["cancel", "expired", "node", "unconfigured"])
def test_changed_installation_premise_does_not_start_a_write(installation_state, change):
    state = installation_state
    if change == "unconfigured":
        state[2].environment_installation_file = ""
        assert submit(state).status_code == 403
        return
    identifier = submit(state).json()["id"]
    if change == "cancel":
        state[0][2].post("/v1/dify/management/environment-tasks", json={"id": identifier, "cancel": True})
    elif change == "node":
        path = json.loads(__import__("pathlib").Path(state[2].environment_installation_file).read_text())[
            "targets"
        ][0]["node_file"]
        __import__("pathlib").Path(path).write_text("{}")
    else:
        with gateway_db.connection(drafts._database(state[0][0]), write=True) as conn:
            request = json.loads(tasks._row(conn, identifier)["request"])
            request["plan"]["expires_at"] = 0
            conn.execute(
                "UPDATE environment_tasks SET request=? WHERE id=?", (json.dumps(request), identifier)
            )
    response = internal(state, {"installation_claim": True, "owner": "c" * 32})
    assert response.status_code == 200 and response.json()["claimed"] is False


def test_inflight_cancellation_keeps_outcome_and_lost_lease_only_observes(installation_state):
    state = installation_state
    identifier = submit(state).json()["id"]
    owner = "d" * 32
    internal(state, {"installation_claim": True, "owner": owner})
    stage = internal(state, {"installation": "stage", "id": identifier, "owner": owner}).json()
    with gateway_db.connection(drafts._database(state[0][0]), write=True) as conn:
        conn.execute("UPDATE environment_tasks SET lease_until=0 WHERE id=?", (identifier,))
    owner = "e" * 32
    claim = internal(state, {"installation_claim": True, "owner": owner})
    assert claim.json()["id"] == identifier
    assert (
        internal(state, {"installation": "stage", "id": identifier, "owner": owner}).json()["observe_only"]
        is True
    )
    state[0][2].post("/v1/dify/management/environment-tasks", json={"id": identifier, "cancel": True})
    result = {
        "id": identifier,
        "environment": "a",
        "scope": "candidate-installation-only",
        "state": "observed",
        "executable": False,
        "activation": "not_executed",
        "registration": "not_executed",
        "business": "unverified",
        "artifact_sha256": stage["archives"]["application"]["sha256"],
        "runtime_sha256": stage["archives"]["runtime"]["sha256"],
        "source_revision": stage["archives"]["application"]["revision"],
        "version": "1.0.0",
        "installation": "installed",
        "config_sha256": "a" * 64,
        "registry_sha256": "b" * 64,
    }
    final = internal(state, {"installation": "complete", "id": identifier, "owner": owner, "result": result})
    assert (
        final.status_code == 200
        and final.json()["state"] == "cancelled"
        and final.json()["result"]["source_revision"]
    )


def test_cancel_after_artifact_transfer_refuses_final_authorization(installation_state):
    state = installation_state
    identifier = submit(state).json()["id"]
    owner = "f" * 32
    assert internal(state, {"installation_claim": True, "owner": owner}).json()["id"] == identifier
    assert internal(state, {"installation": "stage", "id": identifier, "owner": owner}).status_code == 200
    state[0][2].post("/v1/dify/management/environment-tasks", json={"id": identifier, "cancel": True})
    response = internal(state, {"installation": "authorize", "id": identifier, "owner": owner})
    assert response.status_code == 409


def test_worker_transfer_resolves_logical_target_and_authorizes_after_copy(
    installation_state, tmp_path, monkeypatch
):
    import ast
    import subprocess

    from aiops_diagnostics.gateway import environment_task_worker as worker

    state = installation_state
    identifier = submit(state).json()["id"]
    owner = "a" * 32
    internal(state, {"installation_claim": True, "owner": owner})
    stage = internal(state, {"installation": "stage", "id": identifier, "owner": owner}).json()
    (tmp_path / ".dev-host.toml").write_text('[[targets]]\nname="124"\nhost="managed-a"\n')
    commands = []
    # 传输替身只提供现有已验证的归档字节；不执行真实SSH或生产写入。
    installer_bytes = b"fixed installer"
    stage["installer"]["sha256"] = __import__("hashlib").sha256(installer_bytes).hexdigest()

    def run(args, **kwargs):
        commands.append(args)
        if args[0] == "bash":
            __import__("pathlib").Path(args[-1]).write_bytes(installer_bytes)
        elif args[0] == "dev-host" and args[-1].endswith("python -"):
            ast.parse(kwargs["input"].decode())
            # 固定源read脚本的Path参数来自受控stage。
            script = kwargs["input"].decode()
            source = next(v for v in stage["archives"].values() if repr(v["path"]) in script)
            kwargs["stdout"].write(__import__("pathlib").Path(source["path"]).read_bytes())
        elif "input" in kwargs:
            ast.parse(kwargs["input"])
        return subprocess.CompletedProcess(args, 0)

    def output(args, **kwargs):
        commands.append(args)
        ast.parse(kwargs["input"])
        assert "'run'" in kwargs["input"] and "'/usr/bin/python3'" in kwargs["input"]
        return __import__("json").dumps(
            {
                "id": identifier,
                "environment": "a",
                "scope": "candidate-installation-only",
                "state": "blocked",
                "executable": False,
                "activation": "not_executed",
                "registration": "not_executed",
                "business": "unverified",
            }
        )

    def request(payload):
        if payload["installation"] == "stage":
            return stage
        if payload["installation"] == "authorize":
            assert any(a[:3] == ["dev-host", "cp", "124"] for a in commands)
            return {"authorized": True}
        return internal(state, payload).json()

    monkeypatch.setattr(worker.subprocess, "run", run)
    monkeypatch.setattr(worker.subprocess, "check_output", output)
    result = worker.installation_run(tmp_path, request, identifier, owner)
    assert result["state"] == "blocked"
    assert all(a[2] == "124" for a in commands if a[0] == "dev-host" and a[2] != "41")


@pytest.mark.parametrize("phase", ["queued", "executing", "checking", "running"])
def test_public_install_task_states_do_not_masquerade_as_site_preflight(prepared, tmp_path, phase):  # noqa: F811
    from aiops_diagnostics.gateway.environment_tasks import _public

    path, _, _, _ = prepared
    now = time.time()
    request = {"action": "candidate-install", "plan": {"id": "plan_" + "b" * 32, "expires_at": now + 60}}
    with gateway_db.initialize(drafts._database(path)) as conn:
        drafts.initialize_tasks(conn)
        rowid = "task_" + "c" * 32
        conn.execute(
            "INSERT INTO environment_tasks(id,environment,request,state,result,updated_at,owner,lease_until) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                rowid,
                "a",
                json.dumps(request),
                phase,
                json.dumps({"expires_at": now + 60}),
                now,
                "d" * 32 if phase in {"executing", "running"} else "",
                now + 60 if phase in {"executing", "running"} else 0,
            ),
        )
        value = _public(tasks._row(conn, rowid))
    expected = {"queued": "queued", "executing": "running", "checking": "reconcile", "running": "running"}[
        phase
    ]
    assert value["action"] == "candidate-install" and value["state"] == expected


def test_composed_receipt_requires_source_and_predecessor_not_just_new_digest(installation_state):
    state = installation_state
    identifier = submit(state).json()["id"]
    owner = "d" * 32
    internal(state, {"installation_claim": True, "owner": owner})
    staged = internal(state, {"installation": "stage", "id": identifier, "owner": owner}).json()
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
        version="1.38.0",
        config_sha256="a" * 64,
        registry_sha256="b" * 64,
        artifact_sha256=staged["archives"]["application"]["sha256"],
        runtime_sha256="e" * 64,
        source_revision=staged["archives"]["application"]["revision"],
        source_runtime_sha256=staged["archives"]["runtime"]["sha256"],
    )
    response = internal(
        state, {"installation": "complete", "id": identifier, "owner": owner, "result": result}
    )
    assert response.status_code == 409
    result["previous_runtime_sha256"] = staged["expected"]["runtime_sha256"]
    response = internal(
        state, {"installation": "complete", "id": identifier, "owner": owner, "result": result}
    )
    assert response.status_code == 200 and response.json()["result"]["runtime_sha256"] == "e" * 64
