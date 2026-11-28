"""客户机 Gateway 单一 OCI 容器分发、宿主机执行器与无损升级/回滚测试（ADR-0363 收敛版）。

拦截的具体错误：
1. 允许 `:latest` 或未锁定 `@sha256` 的镜像引用进入编排层；
2. 平铺 `systemd` 容器单元遗漏 `EnvironmentFile`、`--network host`、`--read-only` 或 `--cap-drop ALL`；
3. `host_release_agent.py` 切换失败未原子恢复 `container.env`/`unit`/`installed.json`，或改坏受保护配置；
4. 未发布 OCI 镜像或客户机缺失 Docker 时仍静默降级或未明确报错退出。
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiops_diagnostics.onboarding import (
    container_registry,
    host_enrollment,
    registry,
    release_governance,
    release_worker,
    wizard_dispatch,
    wizard_jobs,
    wizard_worker,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_host_agent():
    spec = importlib.util.spec_from_file_location(
        "host_release_agent",
        REPO_ROOT / "ops/environments/gateway/host_release_agent.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["host_release_agent"] = mod
    spec.loader.exec_module(mod)
    return mod


def _setup_registry(tmp_path: Path) -> tuple[Path, Path]:
    reg_dir = tmp_path / "etc" / "registry"
    assets_dir = tmp_path / "etc" / "onboarding-assets"
    reg_dir.mkdir(parents=True, mode=0o700)
    assets_dir.mkdir(parents=True, mode=0o700)
    reg_path = reg_dir / "registry.json"
    registry.apply(
        reg_path,
        [
            {
                "id": "41",
                "name": "核心环境41",
                "enabled": True,
                "gateway_url": "https://gw41.example",
                "cpms_base_url": "https://cpms41.example",
                "company_gateway_url": "https://api41.example",
                "config_ref": str(tmp_path / "env-41.env"),
                "tenants": [],
            },
            {
                "id": "jiejia",
                "name": "捷佳测试",
                "enabled": True,
                "gateway_url": "https://api.juxichongdian.cn",
                "cpms_base_url": "https://api.juxichongdian.cn/cpms",
                "company_gateway_url": "https://api.juxichongdian.cn",
                "config_ref": str(tmp_path / "env-jiejia.env"),
                "tenants": [{"id": "1990990768688599040", "name": "捷佳", "enabled": True}],
            },
        ],
        expected_revision=0,
        actor="test",
        managers=[
            {"environment": "41", "tenant_id": "t-admin", "user_id": "u-admin", "environments": ["*"]},
        ],
    )
    host_enrollment.enroll_via_ephemeral_password(
        reg_path,
        ("41", "t-admin", "u-admin"),
        environment="jiejia",
        address="203.0.113.10",
        password="ephemeral-password",
        bootstrap_runner=lambda **_kw: {
            "hostname": "jiejia-gw",
            "host_public_key_line": "203.0.113.10 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKey",
            "host_fingerprint_sha256": "SHA256:" + "A" * 43,
            "python_minor": "3.12",
            "service_user_present": True,
            "venv_ready": True,
            "nginx_flavor": "baota",
            "nginx_extension_dir": "/www/server/panel/vhost/nginx/extension",
        },
        keygen_runner=lambda _p, comment: (
            "-----BEGIN OPENSSH PRIVATE KEY-----\nMOCK\n-----END OPENSSH PRIVATE KEY-----\n",
            f"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKey {comment}",
        ),
    )
    return reg_path, assets_dir


def test_validate_image_ref_enforces_immutable_sha256_digest() -> None:
    digest = "sha256:" + "a1" * 32
    ref = f"qushi-registry.cn-hangzhou.cr.aliyuncs.com/release/aiops-gateway@{digest}"
    reg, repo, parsed_digest = container_registry.validate_image_ref(ref)
    assert reg == "qushi-registry.cn-hangzhou.cr.aliyuncs.com"
    assert repo == "release/aiops-gateway"
    assert parsed_digest == digest

    for bad in (
        "",
        "qushi-registry.cn-hangzhou.cr.aliyuncs.com/release/aiops-gateway:latest",
        "qushi-registry.cn-hangzhou.cr.aliyuncs.com/release/aiops-gateway:v1.43.22",
        "qushi-registry.cn-hangzhou.cr.aliyuncs.com/release/aiops-gateway@sha256:short",
        f"qushi-registry.cn-hangzhou.cr.aliyuncs.com/release/aiops-gateway@{digest};rm -rf /",
    ):
        assert container_registry.is_valid_image_ref(bad) is False
        with pytest.raises(container_registry.ContainerRegistryRefused):
            container_registry.validate_image_ref(bad)


def test_flat_systemd_unit_and_isolated_docker_auth_templates() -> None:
    cfg_no_auth = container_registry.parse_registry_config_bytes(
        json.dumps(
            {
                "schema": "aiops-container-registry/1",
                "repository": "qushi-registry.cn-hangzhou.cr.aliyuncs.com/release/aiops-gateway",
            }
        ).encode("utf-8")
    )
    assert cfg_no_auth is not None
    assert container_registry.render_customer_docker_config_json(cfg_no_auth) is None

    cfg_with_auth = container_registry.parse_registry_config_bytes(
        json.dumps(
            {
                "schema": "aiops-container-registry/1",
                "registry": "registry.cn-hangzhou.aliyuncs.com",
                "repository": "registry.cn-hangzhou.aliyuncs.com/aiops/gateway",
                "pull_username": "aiops_reader",
                "pull_password": "secret-token-value",
            }
        ).encode("utf-8")
    )
    rendered_json = container_registry.render_customer_docker_config_json(cfg_with_auth)
    assert rendered_json is not None
    expected_b64 = base64.b64encode(b"aiops_reader:secret-token-value").decode("ascii")
    assert json.loads(rendered_json) == {
        "auths": {"registry.cn-hangzhou.aliyuncs.com": {"auth": expected_b64}}
    }

    unit = container_registry.render_container_systemd_unit("jiejia")
    assert "EnvironmentFile=/opt/aiops-access-jiejia/container.env" in unit
    assert "ExecStartPre=-/usr/bin/docker rm -f aiops-access-jiejia" in unit
    assert "ExecStart=/usr/bin/docker run --rm --name aiops-access-jiejia" in unit
    assert "--network host" in unit
    assert "--read-only" in unit
    assert "--cap-drop ALL" in unit
    assert "--security-opt no-new-privileges:true" in unit
    assert "-v /etc/aiops-access-jiejia:/etc/aiops-access-jiejia:ro" in unit
    assert "-v /var/lib/aiops-access-jiejia:/var/lib/aiops-access-jiejia:rw" in unit
    assert "--user ${RUN_UID}:${RUN_GID}" in unit
    assert "${IMAGE_REF} serve --host 127.0.0.1 --port ${LOCAL_PORT}" in unit
    assert "run-container.sh" not in unit


def test_host_release_agent_switch_auto_rollback_and_manual_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _load_host_agent()
    opt_base = tmp_path / "opt"
    etc_base = tmp_path / "etc"
    systemd_dir = tmp_path / "systemd"
    monkeypatch.setattr(agent, "OPT_BASE", opt_base)
    monkeypatch.setattr(agent, "ETC_BASE", etc_base)
    monkeypatch.setattr(agent, "VAR_BASE", tmp_path / "var/lib")
    monkeypatch.setattr(agent, "SYSTEMD_DIR", systemd_dir)
    monkeypatch.setattr(agent, "NGINX_VHOST_EXT_BASE", tmp_path / "nginx_ext")

    env = "jiejia"
    opt_root = opt_base / f"aiops-access-{env}"
    etc_root = etc_base / f"aiops-access-{env}"
    opt_root.mkdir(parents=True)
    etc_root.mkdir(parents=True)
    systemd_dir.mkdir(parents=True)
    (etc_root / "registry").mkdir()

    prod_env = etc_root / "production.env"
    env_json = etc_root / "registry/environment.json"
    auth_conf = etc_root / "nginx-user-ingress-auth.conf"
    prod_env.write_text("SECRET=keep\n", encoding="utf-8")
    env_json.write_text('{"env":"jiejia"}\n', encoding="utf-8")
    auth_conf.write_text("set $k 1;\n", encoding="utf-8")
    protected = [str(prod_env), str(env_json), str(auth_conf)]
    pre_hashes = agent._hash_files(protected)

    old_digest = "sha256:" + "11" * 32
    old_ref = f"qushi-registry.cn-hangzhou.cr.aliyuncs.com/release/aiops-gateway@{old_digest}"
    new_digest = "sha256:" + "22" * 32
    new_ref = f"qushi-registry.cn-hangzhou.cr.aliyuncs.com/release/aiops-gateway@{new_digest}"

    (opt_root / "container.env").write_text(
        agent._render_container_env(env, old_ref, 8788, 996, 986), encoding="utf-8"
    )
    (opt_root / "run-container.sh").write_text("#!/bin/sh\necho legacy\n", encoding="utf-8")
    (systemd_dir / f"aiops-access-{env}.service").write_text("[Unit]\nDescription=old\n", encoding="utf-8")
    (opt_root / "installed.json").write_text(
        json.dumps(
            {
                "runtime_mode": "docker",
                "version": "1.43.22+aaaaaaaaaaaa",
                "revision": "a" * 40,
                "image_ref": old_ref,
                "image_digest": old_digest,
                "port": 8788,
            }
        ),
        encoding="utf-8",
    )

    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    (stage_dir / f"aiops-access-{env}.service").write_text(
        container_registry.render_container_systemd_unit(env), encoding="utf-8"
    )

    monkeypatch.setattr(agent.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(
        agent, "_poll_health", lambda *_a, **_k: {"ok": True, "version": "1.43.23+bbbbbbbbbbbb"}
    )

    # 1. 成功切换：清理 run-container.sh、平铺 unit、保留 previous_release 并校零改动
    out = agent.cmd_switch(
        {
            "env": env,
            "port": 8788,
            "stage_dir": str(stage_dir),
            "bak_dir": str(tmp_path / "bak1"),
            "api_domain": "",
            "target_ver": "1.43.23+bbbbbbbbbbbb",
            "target_rev": "b" * 40,
            "target_tag": "v1.43.23",
            "image_ref": new_ref,
            "image_digest": new_digest,
            "old_image_ref": old_ref,
            "run_uid": 996,
            "run_gid": 986,
            "nginx_changed": False,
            "pre_hashes": pre_hashes,
        }
    )
    assert out["untouched_config_verified"] is True
    assert not (opt_root / "run-container.sh").exists()
    assert new_ref in (opt_root / "container.env").read_text(encoding="utf-8")
    inst = json.loads((opt_root / "installed.json").read_text(encoding="utf-8"))
    assert inst["previous_release"]["image_ref"] == old_ref

    # 2. 健康检查失败时自动回滚回 v1.43.22+aaaaaaaaaaaa
    stage_fail = tmp_path / "stage_fail"
    stage_fail.mkdir()
    (stage_fail / f"aiops-access-{env}.service").write_text("[Unit]\nDescription=bad\n", encoding="utf-8")
    monkeypatch.setattr(
        agent, "_poll_health", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("HEALTH_VERIFY_FAILED"))
    )
    with pytest.raises(SystemExit, match="AUTO_ROLLED_BACK:HEALTH_VERIFY_FAILED"):
        agent.cmd_switch(
            {
                "env": env,
                "port": 8788,
                "stage_dir": str(stage_fail),
                "bak_dir": str(tmp_path / "bak_fail"),
                "api_domain": "",
                "target_ver": "1.43.24+cccccccccccc",
                "target_rev": "c" * 40,
                "target_tag": "v1.43.24",
                "image_ref": (
                    f"qushi-registry.cn-hangzhou.cr.aliyuncs.com/release/aiops-gateway@sha256:{'33' * 32}"
                ),
                "image_digest": f"sha256:{'33' * 32}",
                "old_image_ref": new_ref,
                "run_uid": 996,
                "run_gid": 986,
                "nginx_changed": False,
                "pre_hashes": pre_hashes,
            }
        )
    assert new_ref in (opt_root / "container.env").read_text(encoding="utf-8")

    # 3. 手动 docker -> docker 回滚回 old_ref
    monkeypatch.setattr(agent.pwd, "getpwnam", lambda _u: SimpleNamespace(pw_uid=996, pw_gid=986))
    monkeypatch.setattr(
        agent, "_poll_health", lambda *_a, **_k: {"ok": True, "version": "1.43.22+aaaaaaaaaaaa"}
    )
    rb_out = agent.cmd_rollback({"env": env, "port": 8788, "api_domain": "", "protected_paths": protected})
    assert rb_out["ok"] is True
    assert rb_out["to_version"] == "1.43.22+aaaaaaaaaaaa"
    assert old_ref in (opt_root / "container.env").read_text(encoding="utf-8")


def test_oci_upgrade_rollback_and_guards_via_host_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg_path, assets_dir = _setup_registry(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    (repo / "ops/nginx").mkdir(parents=True)
    (repo / "ops/nginx/environment-user-ingress.conf").write_text(
        "# （环境 __ENV__）\nlocation = /v1/faq/catalog {\n"
        "  include /etc/aiops-access-__ENV__/environment-user-ingress-proxy.inc;\n}\n"
    )
    (repo / "ops/nginx/environment-user-ingress-proxy.inc").write_text("proxy_pass http://127.0.0.1:8788;\n")
    (repo / "ops/nginx/environment-user-ingress-routes.txt").write_text("GET /v1/faq/catalog\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "v22"], cwd=repo, check=True)
    rev22 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    subprocess.run(["git", "tag", "-a", "v1.43.22", "-m", "v1.43.22", rev22], cwd=repo, check=True)

    (repo / "ops/nginx/environment-user-ingress-routes.txt").write_text(
        "GET /v1/faq/catalog\nGET /v1/shortcuts\n"
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "v23"], cwd=repo, check=True)
    rev23 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    subprocess.run(["git", "tag", "-a", "v1.43.23", "-m", "v1.43.23", rev23], cwd=repo, check=True)
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", rev23], cwd=repo, check=True)

    digest22 = "sha256:" + "c2" * 32
    ref22 = f"qushi-registry.cn-hangzhou.cr.aliyuncs.com/release/aiops-gateway@{digest22}"
    digest23 = "sha256:" + "c3" * 32
    ref23 = f"qushi-registry.cn-hangzhou.cr.aliyuncs.com/release/aiops-gateway@{digest23}"
    catalog = release_governance.collect_git_annotated_releases(
        repo,
        image_entries={
            rev23: {
                "schema": "aiops-gateway-image/1",
                "tag": "v1.43.23",
                "revision": rev23,
                "image_ref": ref23,
                "image_digest": digest23,
                "image_tag": "v1.43.23",
            }
        },
    )
    release_governance.save_release_catalog(assets_dir, catalog)

    monkeypatch.setattr(
        release_worker.container_registry,
        "discover_release_images_from_github",
        lambda *_a, **_kw: {},
    )
    monkeypatch.setattr(
        release_worker,
        "_read_registry_config_from_central",
        lambda *_a, **_kw: None,
    )

    state_store: dict[str, dict] = {}

    def fake_request(payload: dict) -> dict:
        op = payload.get("wizard_job")
        if op == "save_release_catalog":
            return {"ok": True}
        if op == "save_release_state":
            state_store[payload["environment"]] = payload["state"]
            return {"ok": True}
        if op == "progress":
            return wizard_jobs.update_job_progress(
                reg_path,
                payload["job_id"],
                step=payload["step"],
                step_state=payload["step_state"],
                steps_payload=payload["steps"],
            )
        if op == "complete":
            return wizard_jobs.complete_job(
                reg_path,
                payload["job_id"],
                passed=payload["passed"],
                result=payload.get("result") or {},
                error_code=payload.get("error_code") or "",
                failed_step=payload.get("failed_step") or "",
                steps_payload=payload.get("steps"),
            )
        return {}

    runtime_state = {
        "ver": f"1.43.22+{rev22[:12]}",
        "rev": rev22,
        "img_ref": ref22,
        "digest": digest22,
        "prev": None,
        "docker": True,
    }
    copied: list[str] = []

    class FakeAgentHost:
        def copy_file(self, _local: Path, remote: str) -> None:
            copied.append(remote)

        def run_cmd(
            self, cmd: str, *, input_data: bytes | str | None = None, timeout_sec: int = 120
        ) -> subprocess.CompletedProcess[str]:
            if cmd.startswith("python3 - "):
                assert input_data and "def cmd_switch" in str(input_data)
                parts = shlex.split(cmd)
                subcmd = parts[2]
                if subcmd == "probe":
                    return subprocess.CompletedProcess(
                        cmd,
                        0,
                        stdout=json.dumps(
                            {
                                "port": 8788,
                                "runtime_mode": "docker",
                                "docker_available": runtime_state["docker"],
                                "current_image_ref": runtime_state["img_ref"],
                                "current_image_digest": runtime_state["digest"],
                                "current_release_name": runtime_state["digest"][:19],
                                "installed_version": runtime_state["ver"],
                                "installed_revision": runtime_state["rev"],
                                "python_minor": "3.12",
                                "health": {
                                    "ok": True,
                                    "version": runtime_state["ver"],
                                    "business_mutations": "disabled",
                                },
                                "user_conf": "",
                                "proxy_inc": "",
                                "previous_release": runtime_state["prev"],
                            }
                        ),
                        stderr="",
                    )
                if subcmd == "pull-verify":
                    return subprocess.CompletedProcess(
                        cmd,
                        0,
                        stdout=json.dumps(
                            {
                                "pre_hashes": {"/etc/aiops-access-jiejia/production.env": "abc"},
                                "old_image_ref": runtime_state["img_ref"],
                                "run_uid": 996,
                                "run_gid": 986,
                            }
                        ),
                        stderr="",
                    )
                if subcmd == "sync-nginx":
                    return subprocess.CompletedProcess(
                        cmd, 0, stdout=json.dumps({"nginx_changed": True}), stderr=""
                    )
                if subcmd == "switch":
                    runtime_state["prev"] = {
                        "runtime_mode": "docker",
                        "version": f"1.43.22+{rev22[:12]}",
                        "revision": rev22,
                        "image_ref": ref22,
                        "image_digest": digest22,
                        "nginx_backup_dir": "/var/backups/bak",
                        "recorded_at": 100.0,
                    }
                    runtime_state["ver"] = f"1.43.23+{rev23[:12]}"
                    runtime_state["rev"] = rev23
                    runtime_state["img_ref"] = ref23
                    runtime_state["digest"] = digest23
                    return subprocess.CompletedProcess(
                        cmd, 0, stdout=json.dumps({"ok": True, "untouched_config_verified": True}), stderr=""
                    )
                if subcmd == "rollback":
                    runtime_state["ver"] = f"1.43.22+{rev22[:12]}"
                    runtime_state["rev"] = rev22
                    runtime_state["img_ref"] = ref22
                    runtime_state["digest"] = digest22
                    runtime_state["prev"] = None
                    return subprocess.CompletedProcess(
                        cmd,
                        0,
                        stdout=json.dumps(
                            {"ok": True, "nginx_restored": True, "untouched_config_verified": True}
                        ),
                        stderr="",
                    )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    ctx = SimpleNamespace(version="1.43.23+cafecafe1234")
    enqueued = wizard_dispatch.dispatch_wizard(
        ctx,
        object(),
        {
            "wizard_action": "environment_upgrade",
            "wizard_payload": {"environment": "jiejia", "target_tag": "v1.43.23"},
        },
        access_fn=lambda _c, _s: ("u-admin", {}, {"*"}),
        path_fn=lambda _c: str(reg_path),
        identity_fn=lambda _c, _caller: ("41", "t-admin", "u-admin"),
    )
    done = wizard_worker.execute_environment_upgrade_job(
        repo,
        "41",
        enqueued,
        request=fake_request,
        pinned_factory=lambda *_a, **_kw: FakeAgentHost(),
        public_smoke_fn=lambda *_a: {"passed": True, "checks": []},
    )
    assert done["passed"] is True
    assert done["distribution_mode"] == "oci"
    assert done["runtime_mode"] == "docker"
    assert done["image_digest"] == digest23
    assert done["to_version"] == f"1.43.23+{rev23[:12]}"
    assert not any(c.endswith("run-container.sh") for c in copied)
    assert state_store["jiejia"]["current_image_digest"] == digest23

    # 验证未发布镜像版本被严格拒绝（无 venv 降级）
    unbuilt_job = wizard_jobs.enqueue_job(
        reg_path,
        environment="jiejia",
        action="environment_upgrade",
        request={
            "environment": "jiejia",
            "target_tag": "v1.43.22",
            "target_revision": rev22,
            "target_version": "1.43.22",
            "target_full_version": f"1.43.22+{rev22[:12]}",
            "gateway_url": "https://api.juxichongdian.cn",
            "api_domain": "api.juxichongdian.cn",
            "tenant_id": "1990990768688599040",
        },
    )
    unbuilt_done = wizard_worker.execute_environment_upgrade_job(
        repo,
        "41",
        unbuilt_job,
        request=fake_request,
        pinned_factory=lambda *_a, **_kw: FakeAgentHost(),
        public_smoke_fn=lambda *_a: {"passed": True, "checks": []},
    )
    assert unbuilt_done["passed"] is False
    assert unbuilt_done["error_code"] == "ENVIRONMENT_RELEASE_IMAGE_NOT_PUBLISHED"

    # 验证 docker -> docker 手动一键回滚
    rb_job = wizard_jobs.enqueue_job(
        reg_path,
        environment="jiejia",
        action="environment_rollback",
        request={
            "environment": "jiejia",
            "target_version": f"1.43.22+{rev22[:12]}",
            "gateway_url": "https://api.juxichongdian.cn",
            "api_domain": "api.juxichongdian.cn",
            "tenant_id": "1990990768688599040",
        },
    )
    rb_done = wizard_worker.execute_environment_rollback_job(
        repo,
        "41",
        rb_job,
        request=fake_request,
        pinned_factory=lambda *_a, **_kw: FakeAgentHost(),
        public_smoke_fn=lambda *_a: {"passed": True, "checks": []},
    )
    assert rb_done["passed"] is True
    assert rb_done["to_version"] == f"1.43.22+{rev22[:12]}"


def test_oci_cold_install_and_onboard_calls_host_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files_bytes = {
        "/bundle/template.env": b"AIOPS_BIND=127.0.0.1\n",
        "/bundle/runtime-bundle.tar": b"rt-bytes",
        "/bundle/nginx-user-ingress-auth.conf": b"set $x 1;\n",
    }
    hashes = {k: hashlib.sha256(v).hexdigest() for k, v in files_bytes.items()}
    digest = "sha256:" + "d4" * 32
    ref = f"qushi-registry.cn-hangzhou.cr.aliyuncs.com/release/aiops-gateway@{digest}"

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    (repo / "ops/nginx").mkdir(parents=True)
    (repo / "ops/nginx/environment-user-ingress.conf").write_text("location = /v1/faq/catalog {}\n")
    (repo / "ops/nginx/environment-user-ingress-proxy.inc").write_text("proxy_pass http://127.0.0.1:8788;\n")
    (repo / "ops/nginx/environment-user-ingress-routes.txt").write_text("GET /v1/faq/catalog\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "v23"], cwd=repo, check=True)
    rev = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    subprocess.run(["git", "tag", "-a", "v1.43.23", "-m", "v1.43.23", rev], cwd=repo, check=True)
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", rev], cwd=repo, check=True)

    monkeypatch.setattr(
        release_worker.container_registry,
        "discover_release_images_from_github",
        lambda *_a, **_kw: {
            rev: {
                "schema": "aiops-gateway-image/1",
                "tag": "v1.43.23",
                "revision": rev,
                "image_ref": ref,
                "image_digest": digest,
            }
        },
    )
    monkeypatch.setattr(release_worker, "_read_registry_config_from_central", lambda *_a, **_kw: None)
    monkeypatch.setattr(wizard_worker, "_dev_host_read", lambda _t, p, **_k: files_bytes[p])

    subcmds: list[str] = []

    class _Pin:
        def copy_file(self, _src: Path, _dst: str) -> None:
            return None

        def run_cmd(self, cmd: str, **_k):
            if cmd.startswith("python3 - "):
                sub = shlex.split(cmd)[2]
                subcmds.append(sub)
                if sub == "probe":
                    return SimpleNamespace(
                        returncode=0, stdout=json.dumps({"docker_available": True}), stderr=""
                    )
                if sub == "cold-install":
                    return SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True}), stderr="")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    out = wizard_worker._step_cold_install_and_onboard(
        repo,
        "41",
        lambda _p: {
            "ok": True,
            "config_path": "/bundle/template.env",
            "config_sha256": hashes["/bundle/template.env"],
            "runtime_bundle_path": "/bundle/runtime-bundle.tar",
            "runtime_bundle_sha256": hashes["/bundle/runtime-bundle.tar"],
            "nginx_auth_path": "/bundle/nginx-user-ingress-auth.conf",
            "nginx_auth_sha256": hashes["/bundle/nginx-user-ingress-auth.conf"],
        },
        {"environment": "jiejia", "api_domain": "api.juxichongdian.cn", "local_port": 8788},
        pinned_host=_Pin(),
    )
    assert out["ok"] is True
    assert out["runtime_mode"] == "docker"
    assert out["image_digest"] == digest
    assert subcmds == ["probe", "cold-install"]
