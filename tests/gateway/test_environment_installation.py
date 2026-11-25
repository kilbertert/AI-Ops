"""D37：真实管理/内部API上的安装任务、私有导出与原子任务兼容。"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from dify.test_dify_ensure_workspace import upstream as workspace_upstream  # noqa: F401

from aiops_diagnostics.common.http_auth import build_internal_token_headers
from aiops_diagnostics.common.private_files import write_private_text
from aiops_diagnostics.gateway import gateway_db
from aiops_diagnostics.onboarding import candidate_preparation as preparation
from aiops_diagnostics.onboarding import drafts, tasks
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
    # 未派发重复 stage 允许 run 重试（归档冻结不变）——观察界是 dispatched。
    assert (
        repeated_stage.json()["observe_only"] is False
        and repeated_stage.json()["archives"] == body["archives"]
    )
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
    assert repeated["observe_only"] is False and repeated["archives"] == staged["archives"]


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


def test_inflight_cancellation_keeps_outcome_and_lost_lease_retries_undispatched(installation_state):
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
    # 租约丢失后重领：未派发任务允许 run 重试（远端零 apply 可由 dispatched 未翻转证明）。
    assert (
        internal(state, {"installation": "stage", "id": identifier, "owner": owner}).json()["observe_only"]
        is False
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


def test_attempted_but_undispatched_install_expires_to_terminal(installation_state):
    """真实事故形：租约自然到期（now 至 lease 末端）时计划窗口必然已过期——
    attempted+无 dispatched 认领必须终态化 blocked，无 execution。"""
    state = installation_state
    identifier = submit(state).json()["id"]
    owner = "a" * 32
    internal(state, {"installation_claim": True, "owner": owner})
    assert internal(state, {"installation": "stage", "id": identifier, "owner": owner}).status_code == 200
    with gateway_db.connection(drafts._database(state[0][0]), write=True) as conn:
        request = json.loads(tasks._row(conn, identifier)["request"])
        assert request["attempted"] is True and "dispatched" not in request
        for task_id in (identifier, state[1]):
            stored = json.loads(tasks._row(conn, task_id)["request"])
            stored["plan"]["expires_at"] = 0
            conn.execute("UPDATE environment_tasks SET request=? WHERE id=?", (json.dumps(stored), task_id))
        conn.execute("UPDATE environment_tasks SET lease_until=0 WHERE id=?", (identifier,))
    for next_owner in ("b" * 32, "c" * 32):
        claim = internal(state, {"installation_claim": True, "owner": next_owner})
        assert claim.status_code == 200 and claim.json()["claimed"] is False
    with gateway_db.connection(drafts._database(state[0][0]), readonly=True) as conn:
        row = tasks._row(conn, identifier)
    assert row["state"] == "blocked" and row["error"] == "ENVIRONMENT_TASK_OBSERVATION_REQUIRED"


def test_attempted_but_undispatched_install_cancel_is_terminal(installation_state):
    """未派发任务取消后下次认领落 cancelled，不再回 executing/checking 环。"""
    state = installation_state
    identifier = submit(state).json()["id"]
    owner = "a" * 32
    internal(state, {"installation_claim": True, "owner": owner})
    assert internal(state, {"installation": "stage", "id": identifier, "owner": owner}).status_code == 200
    state[0][2].post("/v1/dify/management/environment-tasks", json={"id": identifier, "cancel": True})
    with gateway_db.connection(drafts._database(state[0][0]), write=True) as conn:
        row = tasks._row(conn, identifier)
        assert row["cancel"] and row["state"] == "executing"
        conn.execute("UPDATE environment_tasks SET lease_until=0 WHERE id=?", (identifier,))
    claim = internal(state, {"installation_claim": True, "owner": "b" * 32})
    assert claim.status_code == 200 and claim.json()["claimed"] is False
    with gateway_db.connection(drafts._database(state[0][0]), readonly=True) as conn:
        assert tasks._row(conn, identifier)["state"] == "cancelled"


def test_undispatched_attempted_install_retries_run_with_frozen_archives(installation_state):
    """主动让出/完成回收的协议能力：窗口内未派发任务可重领 run+authorize。

    首个 worker 以 reconcile 完成自然让出租约（传输失败形→checking 可即领），
    非「crash→等 900s→窗口仍在」的天然续跑——租约自然到期时刻计划必然已
    过期，该形见过期终态测试。
    """
    state = installation_state
    identifier = submit(state).json()["id"]
    first_owner = "a" * 32
    internal(state, {"installation_claim": True, "owner": first_owner})
    staged = internal(state, {"installation": "stage", "id": identifier, "owner": first_owner}).json()
    reconcile = {
        "id": identifier,
        "environment": "a",
        "scope": "candidate-installation-only",
        "state": "reconcile",
        "executable": False,
        "activation": "not_executed",
        "registration": "not_executed",
        "business": "unverified",
    }
    done = internal(
        state, {"installation": "complete", "id": identifier, "owner": first_owner, "result": reconcile}
    )
    assert done.status_code == 200 and done.json()["state"] == "reconcile"
    owner = "b" * 32
    assert internal(state, {"installation_claim": True, "owner": owner}).json()["id"] == identifier
    retry = internal(state, {"installation": "stage", "id": identifier, "owner": owner}).json()
    assert retry["observe_only"] is False and retry["archives"] == staged["archives"]
    authorize = internal(state, {"installation": "authorize", "id": identifier, "owner": owner})
    assert authorize.status_code == 200 and authorize.json() == {"authorized": True}
    # dispatched 已持久化：再次让出后只能观察回收，绝不重放 apply。
    done = internal(
        state, {"installation": "complete", "id": identifier, "owner": owner, "result": reconcile}
    )
    assert done.json()["state"] == "reconcile"
    owner = "c" * 32
    assert internal(state, {"installation_claim": True, "owner": owner}).json()["id"] == identifier
    observed = internal(state, {"installation": "stage", "id": identifier, "owner": owner}).json()
    assert observed["observe_only"] is True and observed["archives"] == staged["archives"]


def test_dispatched_cancelled_install_keeps_blocked_not_cancelled(installation_state):
    """已派发+取消+闸失败落 blocked 不落 cancelled：未知远端副作用不释放互斥。"""
    state = installation_state
    identifier = submit(state).json()["id"]
    owner = "a" * 32
    internal(state, {"installation_claim": True, "owner": owner})
    internal(state, {"installation": "stage", "id": identifier, "owner": owner})
    authorize = internal(state, {"installation": "authorize", "id": identifier, "owner": owner})
    assert authorize.json() == {"authorized": True}
    state[0][2].post("/v1/dify/management/environment-tasks", json={"id": identifier, "cancel": True})
    with gateway_db.connection(drafts._database(state[0][0]), write=True) as conn:
        request = json.loads(tasks._row(conn, identifier)["request"])
        assert request["dispatched"] is True
        # 父任务取消：已派发任务的无条件漂移检查让闸失败。
        conn.execute("UPDATE environment_tasks SET cancel=1 WHERE id=?", (state[1],))
        conn.execute("UPDATE environment_tasks SET lease_until=0 WHERE id=?", (identifier,))
    claim = internal(state, {"installation_claim": True, "owner": "b" * 32})
    assert claim.status_code == 200 and claim.json()["claimed"] is False
    with gateway_db.connection(drafts._database(state[0][0]), readonly=True) as conn:
        row = tasks._row(conn, identifier)
    assert row["cancel"] and row["state"] == "blocked"


@pytest.mark.parametrize(
    "copy_seconds,window_seconds,other_copy_seconds",
    [(0, 900, 5), (250, 900, 5), (350, 900, 5), (80, 90, 5), (10, 30, 5), (250, 900, 80)],
)
def test_worker_transfer_resolves_logical_target_and_authorizes_after_copy(
    installation_state, tmp_path, monkeypatch, copy_seconds, window_seconds, other_copy_seconds
):
    import ast
    import subprocess

    from aiops_diagnostics.onboarding import task_ssh_runner as worker

    state = installation_state
    identifier = submit(state).json()["id"]
    owner = "a" * 32
    internal(state, {"installation_claim": True, "owner": owner})
    stage = internal(state, {"installation": "stage", "id": identifier, "owner": owner}).json()
    stage["expires_at"] = time.time() + window_seconds
    (tmp_path / ".dev-host.toml").write_text('[[targets]]\nname="124"\nhost="managed-a"\n')
    commands = []
    authorized = []
    clock = [1000.0]
    started = clock[0]
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])
    # 传输替身只提供现有已验证的归档字节；不执行真实SSH或生产写入。
    installer_bytes = b"fixed installer"
    stage["installer"]["sha256"] = __import__("hashlib").sha256(installer_bytes).hexdigest()

    def run(args, **kwargs):
        commands.append(args)
        if args[:2] == ["dev-host", "cp"]:
            duration = copy_seconds if args[3].endswith("application.tar.gz") else other_copy_seconds
            if kwargs["timeout"] < duration:
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            clock[0] += duration
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
        assert kwargs["timeout"] <= 775 - (clock[0] - started)
        assert "'run'" in kwargs["input"] and "managed_python()" in kwargs["input"]
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
            authorized.append(True)
            assert any(a[:3] == ["dev-host", "cp", "124"] for a in commands)
            return {"authorized": True}
        return internal(state, payload).json()

    monkeypatch.setattr(worker.subprocess, "run", run)
    monkeypatch.setattr(worker.subprocess, "check_output", output)
    result = worker.installation_run(tmp_path, request, identifier, owner)
    expected = (
        "reconcile"
        if copy_seconds > 300 or window_seconds < copy_seconds + 60 or other_copy_seconds == 80
        else "blocked"
    )
    assert result["state"] == expected
    assert bool(authorized) is (expected == "blocked")
    if expected == "reconcile":
        assert not any("managed_python()" in str(a) for a in commands)
    assert all(a[2] == "124" for a in commands if a[0] == "dev-host" and a[2] != "41")


def test_worker_cold_stage_builds_spec_without_expected_and_selects_cold_action(tmp_path, monkeypatch):
    """冷装stage无expected：spec不能先构造再覆盖，动作必须是cold-run。"""
    import ast
    import hashlib
    import subprocess

    from aiops_diagnostics.onboarding import task_ssh_runner as worker

    identifier = "task_" + "b" * 32
    owner = "a" * 32
    installer_bytes = b"fixed installer"
    archive_bytes = {"application": b"app", "candidate": b"cand"}
    archives = {}
    for name, body in archive_bytes.items():
        path = tmp_path / (name + ".tar.gz")
        path.write_bytes(body)
        path.chmod(0o600)
        archives[name] = {"path": str(path), "sha256": hashlib.sha256(body).hexdigest()}
    archives["application"]["revision"] = "a" * 40
    stage = {
        "environment": "a",
        "expires_at": time.time() + 900,
        "observe_only": False,
        "python": "3.12",
        "target": {"host": "managed-a", "user": "aiops", "port": 2222},
        "installer": {
            "revision": "f" * 40,
            "sha256": hashlib.sha256(installer_bytes).hexdigest(),
        },
        "archives": archives,
    }
    (tmp_path / ".dev-host.toml").write_text('[[targets]]\nname="124"\nhost="managed-a"\n')
    scripts = []
    completed = []
    result_body = {
        "id": identifier,
        "environment": "a",
        "scope": "site-cold-installation-only",
        "state": "observed",
        "executable": False,
        "installation": "installed",
        "version": "1.40.0",
        "source_revision": archives["application"]["revision"],
        "artifact_sha256": archives["application"]["sha256"],
        "config_sha256": "c" * 64,
        "registry_sha256": "d" * 64,
    }

    def run(args, **kwargs):
        if args[0] == "bash":
            Path(args[-1]).write_bytes(installer_bytes)
        elif args[0] == "dev-host" and args[-1].endswith(
            "runuser -u aiops41 -- /opt/aiops-41/.venv/bin/python -"
        ):
            script = kwargs["input"].decode()
            source = next(v for v in stage["archives"].values() if repr(v["path"]) in script)
            kwargs["stdout"].write(Path(source["path"]).read_bytes())
        return subprocess.CompletedProcess(args, 0)

    def output(args, **kwargs):
        scripts.append(kwargs["input"])
        return json.dumps(result_body)

    def request(payload):
        if payload["installation"] == "stage":
            return stage
        if payload["installation"] == "authorize":
            return {"authorized": True}
        if payload["installation"] == "complete":
            completed.append(payload["result"])
            return payload["result"]
        raise AssertionError(payload)

    monkeypatch.setattr(worker.subprocess, "run", run)
    monkeypatch.setattr(worker.subprocess, "check_output", output)
    result = worker.installation_run(tmp_path, request, identifier, owner, cold=True)
    # 远端结果原样回传：若spec构造KeyError被吞，这里只会是reconcile骨架。
    assert result["state"] == "observed" and result["installation"] == "installed"
    assert completed == [result_body]
    assert len(scripts) == 1 and "'cold-run'" in scripts[0]
    spec = json.loads(ast.literal_eval(scripts[0].split(",input=", 1)[1].split(",stdout=", 1)[0]))
    assert "expected" not in spec
    assert spec["python"] == "3.12" and set(spec["archives"]) == {"application", "candidate"}


def test_install_driver_tail_executes_old_and_new_bundle_interpreter_contract(
    installation_state, tmp_path, monkeypatch
):
    """installation_run生成的task驱动尾部真实执行：旧包回退/usr/bin/python3，新包受管选择。

    目标refs可冻结旧installer（其probe无managed_python）；uid断言之上的
    选择/执行行按生成原文逐字抽取，配合等价导入头对替身安装目录实际跑
    `python3 -`，证回退与选择两端都真正执行而非只断言脚本形状。
    """
    import ast
    import os
    import subprocess
    from shutil import which

    from aiops_diagnostics.onboarding import task_ssh_runner as worker

    state = installation_state
    identifier = submit(state).json()["id"]
    owner = "a" * 32
    internal(state, {"installation_claim": True, "owner": owner})
    stage = internal(state, {"installation": "stage", "id": identifier, "owner": owner}).json()
    (tmp_path / ".dev-host.toml").write_text('[[targets]]\nname="124"\nhost="managed-a"\n')
    installer_bytes = b"fixed installer"
    stage["installer"]["sha256"] = __import__("hashlib").sha256(installer_bytes).hexdigest()
    scripts = []
    real = subprocess.run
    real_output = subprocess.check_output

    def run(args, **kwargs):
        # dev-host/bash/git留替身；其余（解释器探针、check_output内部委托）真执行。
        if args[0] not in ("dev-host", "bash", "git"):
            return real(args, **kwargs)
        if args[0] == "bash":
            Path(args[-1]).write_bytes(installer_bytes)
        elif args[0] == "dev-host" and args[-1].endswith("python -"):
            ast.parse(kwargs["input"].decode())
            script = kwargs["input"].decode()
            source = next(v for v in stage["archives"].values() if repr(v["path"]) in script)
            kwargs["stdout"].write(Path(source["path"]).read_bytes())
        elif "input" in kwargs:
            ast.parse(kwargs["input"])
        return subprocess.CompletedProcess(args, 0)

    def output(args, **kwargs):
        if args[0] != "dev-host":
            return real_output(args, **kwargs)
        scripts.append(kwargs["input"])
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
            return {"authorized": True}
        return internal(state, payload).json()

    monkeypatch.setattr(worker.subprocess, "run", run)
    monkeypatch.setattr(worker.subprocess, "check_output", output)
    assert worker.installation_run(tmp_path, request, identifier, owner)["state"] == "blocked"
    remote = "/var/backups/aiops-install-task-" + identifier
    tail = scripts[-1][scripts[-1].index("sys.path.insert(0,str(d))") :]
    tail = tail.replace(remote, str(tmp_path / "bundle"))
    # 逐字尾部只缺d与导入头；等价补齐后对替身安装目录真实执行。
    # 新包受管枚举用确定的合成解释器入口：宿主/usr/bin/python3小版本在CI上
    # 可能不在3.11–3.13；shim对-I探针回报固定3.13、exec时打印标记后委托
    # PATH的真实python3跑stub。roots/owner/check_dir是受管合同自身参数——
    # tmp祖先归属不受测试控制，归属核验面由tests/tooling单测覆盖。
    managed = tmp_path / "bundle" / "managed"
    managed.mkdir(parents=True)
    shim = tmp_path / "bundle" / "targets" / "python3.13"
    shim.parent.mkdir(parents=True)
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-I" ]; then echo 3.13;'
        ' else echo "synthetic-entry:$0"; exec python3 "$@"; fi\n'
    )
    shim.chmod(0o755)
    (managed / "python3").symlink_to(shim)
    resolved = os.path.realpath(managed / "python3")
    tail = tail.replace(
        "probe.system_python(probe.managed_python())",
        "probe.system_python(probe.managed_python({0}),{0})".format(
            f"roots=({str(managed)!r},),owner={os.getuid()},check_dir=lambda _path,_owner:None"
        ),
    )
    for bundle in ("old", "new"):
        directory = tmp_path / "bundle" / "installer"
        directory.mkdir(parents=True, exist_ok=True)
        if bundle == "old":
            (directory / "probe.py").write_text("# 旧固定包probe：无managed_python\n")
        else:
            root = Path(__file__).resolve().parents[2]
            (directory / "probe.py").write_text((root / "ops/environments/gateway/probe.py").read_text())
            (directory / "artifact_checks.py").write_text(
                (root / "src/aiops_diagnostics/onboarding/artifacts.py").read_text()
            )
        (directory / "task.py").write_text("import sys;print(sys.executable)")
        prologue = "import subprocess,sys\nfrom pathlib import Path\nd=Path(" + repr(str(directory)) + ")\n"
        proc = real(["python3", "-"], input=prologue + tail, text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr
        assert "AttributeError" not in proc.stderr
        lines = proc.stdout.strip().splitlines()
        if bundle == "old":
            assert Path(lines[0]).resolve() == Path("/usr/bin/python3").resolve()
        else:
            # 驱动执行的是核验后的解析目标（shim），其打印标记后委托真实python3跑stub。
            assert lines[0] == "synthetic-entry:" + resolved
            assert Path(lines[1]).resolve() == Path(which("python3")).resolve()


@pytest.mark.parametrize("phase", ["queued", "executing", "checking", "running"])
def test_public_install_task_states_do_not_masquerade_as_site_preflight(prepared, tmp_path, phase):  # noqa: F811
    from aiops_diagnostics.onboarding.tasks import _public

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
