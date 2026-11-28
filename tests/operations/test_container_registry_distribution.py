"""客户机 Gateway OCI 容器镜像分发、不可变摘要锁定与无损升级/回滚测试（ADR-0363）。

拦截的具体错误：
1. 允许 `:latest` 或未锁定 `@sha256` 的镜像引用进入编排层；
2. 容器启动模板遗漏 `--network host`、`--read-only`、`--cap-drop ALL` 或错误读写挂载 `/etc`；
3. OCI 升级/冷装路径仍错误触发 24 MiB 整包 `_ensure_pool_artifacts` 跨海构建传输；
4. `venv -> docker` 迁移或 `docker -> docker` 升级失败时未回滚或破坏客户 `production.env`。
"""

from __future__ import annotations

import base64
import hashlib
import json
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiops_diagnostics.onboarding import (
    container_registry,
    host_enrollment,
    registry,
    release_governance,
    wizard_dispatch,
    wizard_jobs,
    wizard_worker,
)


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
    ref = f"registry.cn-hangzhou.aliyuncs.com/aiops-release/gateway@{digest}"
    reg, repo, parsed_digest = container_registry.validate_image_ref(ref)
    assert reg == "registry.cn-hangzhou.aliyuncs.com"
    assert repo == "aiops-release/gateway"
    assert parsed_digest == digest

    for bad in (
        "",
        "registry.cn-hangzhou.aliyuncs.com/aiops-release/gateway:latest",
        "registry.cn-hangzhou.aliyuncs.com/aiops-release/gateway:v1.43.21",
        "registry.cn-hangzhou.aliyuncs.com/aiops-release/gateway@sha256:short",
        f"registry.cn-hangzhou.aliyuncs.com/aiops-release/gateway@{digest};rm -rf /",
    ):
        assert container_registry.is_valid_image_ref(bad) is False
        with pytest.raises(container_registry.ContainerRegistryRefused):
            container_registry.validate_image_ref(bad)


def test_docker_auth_and_container_runtime_hardening_templates() -> None:
    cfg_no_auth = container_registry.parse_registry_config_bytes(
        json.dumps(
            {
                "schema": "aiops-container-registry/1",
                "repository": "qushi-registry.cn-hangzhou.cr.aliyuncs.com/release/aiops-gateway",
            }
        ).encode("utf-8")
    )
    assert cfg_no_auth is not None
    assert cfg_no_auth["registry"] == "qushi-registry.cn-hangzhou.cr.aliyuncs.com"
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
    parsed = json.loads(rendered_json)
    expected_b64 = base64.b64encode(b"aiops_reader:secret-token-value").decode("ascii")
    assert parsed == {"auths": {"registry.cn-hangzhou.aliyuncs.com": {"auth": expected_b64}}}

    digest = "sha256:" + "b2" * 32
    ref = f"registry.cn-hangzhou.aliyuncs.com/aiops/gateway@{digest}"
    env_txt = container_registry.render_container_env("jiejia", ref, 8788, 996, 986)
    assert f"IMAGE_REF={ref}" in env_txt
    assert "RUN_UID=996" in env_txt
    assert "RUN_GID=986" in env_txt

    launcher = container_registry.render_container_launcher_script()
    assert "--network host" in launcher
    assert "--read-only" in launcher
    assert "--cap-drop ALL" in launcher
    assert "--security-opt no-new-privileges:true" in launcher
    assert "/etc/aiops-access-${ENV_NAME}:/etc/aiops-access-${ENV_NAME}:ro" in launcher
    assert "/var/lib/aiops-access-${ENV_NAME}:/var/lib/aiops-access-${ENV_NAME}:rw" in launcher
    assert '--user "${RUN_UID}:${RUN_GID}"' in launcher

    unit = container_registry.render_container_systemd_unit("jiejia")
    assert "Requires=docker.service" in unit
    assert "ExecStart=/opt/aiops-access-jiejia/run-container.sh" in unit
    assert "ExecStop=/usr/bin/docker stop -t 15 aiops-access-jiejia" in unit


def test_oci_upgrade_migrates_venv_to_docker_and_supports_rollback_without_tarball_pool(
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
    subprocess.run(["git", "commit", "-q", "-m", "v21"], cwd=repo, check=True)
    rev21 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    subprocess.run(["git", "tag", "-a", "v1.43.21", "-m", "v1.43.21", rev21], cwd=repo, check=True)

    (repo / "ops/nginx/environment-user-ingress-routes.txt").write_text(
        "GET /v1/faq/catalog\nGET /v1/shortcuts\n"
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "v22"], cwd=repo, check=True)
    rev22 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    subprocess.run(["git", "tag", "-a", "v1.43.22", "-m", "v1.43.22", rev22], cwd=repo, check=True)
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", rev22], cwd=repo, check=True)

    digest22 = "sha256:" + "c3" * 32
    ref22 = f"registry.cn-hangzhou.aliyuncs.com/aiops/gateway@{digest22}"
    catalog = release_governance.collect_git_annotated_releases(
        repo,
        image_entries={
            rev22: {
                "schema": "aiops-gateway-image/1",
                "tag": "v1.43.22",
                "revision": rev22,
                "image_ref": ref22,
                "image_digest": digest22,
                "image_tag": "v1.43.22",
            }
        },
    )
    assert catalog[0]["image_ready"] is True
    assert catalog[0]["distribution_mode"] == "oci"
    release_governance.save_release_catalog(assets_dir, catalog)

    # 确认走 OCI 路径时绝对不触发 24 MiB 整包构建
    monkeypatch.setattr(
        wizard_worker,
        "_ensure_pool_artifacts",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("OCI 路径不应构建离线 tarball")),
    )
    monkeypatch.setattr(
        wizard_worker,
        "_read_registry_config_from_central",
        lambda *_a, **_kw: {
            "schema": "aiops-container-registry/1",
            "registry": "registry.cn-hangzhou.aliyuncs.com",
            "repository": "registry.cn-hangzhou.aliyuncs.com/aiops/gateway",
            "pull_username": "aiops_pull",
            "pull_password": "pull-password",
        },
    )
    monkeypatch.setattr(
        wizard_worker,
        "_read_image_catalog_from_central",
        lambda *_a, **_kw: {
            rev22: {
                "schema": "aiops-gateway-image/1",
                "tag": "v1.43.22",
                "revision": rev22,
                "image_ref": ref22,
                "image_digest": digest22,
                "image_repo": "registry.cn-hangzhou.aliyuncs.com/aiops/gateway",
                "image_tag": "v1.43.22",
            }
        },
    )
    monkeypatch.setattr(
        wizard_worker.container_registry,
        "discover_release_images_from_github",
        lambda *_a, **_kw: {},
    )

    state_store: dict[str, dict] = {}
    copied_remotes: list[str] = []

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
        "mode": "venv",
        "ver": f"1.43.21+{rev21[:12]}",
        "rev": rev21,
        "img_ref": "",
        "digest": "",
        "prev": None,
    }

    class FakeOciHost:
        def copy_file(self, _local: Path, remote: str) -> None:
            copied_remotes.append(remote)

        def run_cmd(self, cmd: str, *, timeout_sec: int = 120) -> subprocess.CompletedProcess[str]:
            if cmd.startswith("python3 -c "):
                compile(shlex.split(cmd)[2], "<remote-python>", "exec")
            if "installed.json" in cmd and "docker_available" in cmd:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=json.dumps(
                        {
                            "port": 8788,
                            "runtime_mode": runtime_state["mode"],
                            "docker_available": True,
                            "current_image_ref": runtime_state["img_ref"],
                            "current_image_digest": runtime_state["digest"],
                            "current_is_symlink": runtime_state["mode"] == "venv",
                            "current_release_dir": f"/opt/aiops-access-jiejia/releases/{rev21[:12]}",
                            "current_release_name": rev21[:12],
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
            if "docker" in cmd and "smoke_ver" in cmd and "IMAGE_VERSION_MISMATCH" in cmd:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=json.dumps(
                        {
                            "pre_hashes": {"/etc/aiops-access-jiejia/production.env": "deadbeef"},
                            "old_release_dir": f"/opt/aiops-access-jiejia/releases/{rev21[:12]}",
                            "old_image_ref": runtime_state["img_ref"],
                            "run_uid": 996,
                            "run_gid": 986,
                        }
                    ),
                    stderr="",
                )
            if "AUTO_ROLLED_BACK" in cmd:
                runtime_state["prev"] = {
                    "runtime_mode": "venv",
                    "release_dir": f"/opt/aiops-access-jiejia/releases/{rev21[:12]}",
                    "version": f"1.43.21+{rev21[:12]}",
                    "revision": rev21,
                    "systemd_unit_backup": "/var/backups/bak/unit.bak",
                    "nginx_backup_dir": "/var/backups/bak",
                    "recorded_at": 200.0,
                }
                runtime_state["mode"] = "docker"
                runtime_state["ver"] = f"1.43.22+{rev22[:12]}"
                runtime_state["rev"] = rev22
                runtime_state["img_ref"] = ref22
                runtime_state["digest"] = digest22
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps({"ok": True, "untouched_config_verified": True}), stderr=""
                )
            if "nginx_changed" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps({"nginx_changed": True}), stderr=""
                )
            if "PREVIOUS_RELEASE_MISSING" in cmd:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=json.dumps(
                        {
                            "prev_mode": "venv",
                            "prev_ver": f"1.43.21+{rev21[:12]}",
                            "prev_rev": rev21,
                            "prev_dir": f"/opt/aiops-access-jiejia/releases/{rev21[:12]}",
                            "has_nginx_bak": True,
                        }
                    ),
                    stderr="",
                )
            if "ROLLBACK_HEALTH_VERIFY_FAILED" in cmd:
                runtime_state["mode"] = "venv"
                runtime_state["ver"] = f"1.43.21+{rev21[:12]}"
                runtime_state["rev"] = rev21
                runtime_state["img_ref"] = ""
                runtime_state["digest"] = ""
                runtime_state["prev"] = None
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=json.dumps(
                        {
                            "ok": True,
                            "runtime_mode": "venv",
                            "nginx_restored": True,
                            "untouched_config_verified": True,
                        }
                    ),
                    stderr="",
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    ctx = SimpleNamespace(version="1.43.22+cafecafe1234")
    enqueued = wizard_dispatch.dispatch_wizard(
        ctx,
        object(),
        {
            "wizard_action": "environment_upgrade",
            "wizard_payload": {"environment": "jiejia", "target_tag": "v1.43.22"},
        },
        access_fn=lambda _c, _s: ("u-admin", {}, {"*"}),
        path_fn=lambda _c: str(reg_path),
        identity_fn=lambda _c, _caller: ("41", "t-admin", "u-admin"),
    )
    assert enqueued["request"]["target_image_ref"] == ref22

    done = wizard_worker.execute_environment_upgrade_job(
        repo,
        "41",
        enqueued,
        request=fake_request,
        pinned_factory=lambda *_a, **_kw: FakeOciHost(),
        public_smoke_fn=lambda *_a: {"passed": True, "checks": []},
    )
    assert done["passed"] is True, done
    assert done["distribution_mode"] == "oci"
    assert done["runtime_mode"] == "docker"
    assert done["image_digest"] == digest22
    assert done["to_version"] == f"1.43.22+{rev22[:12]}"
    # 确认没有 SCP application.tar.gz，仅传输了轻量脚本/nginx/隔离认证文件
    assert not any(r.endswith("application.tar.gz") for r in copied_remotes)
    assert any(r.endswith("run-container.sh") for r in copied_remotes)
    assert state_store["jiejia"]["runtime_mode"] == "docker"
    assert state_store["jiejia"]["current_image_digest"] == digest22

    # 校验 Dify 总览页显示 Docker 运行态与 OCI 镜像就绪状态
    overview = wizard_dispatch.dispatch_wizard(
        ctx,
        object(),
        {"wizard_action": "release_overview", "wizard_payload": {"environment": "jiejia"}},
        access_fn=lambda _c, _s: ("u-admin", {}, {"*"}),
        path_fn=lambda _c: str(reg_path),
        identity_fn=lambda _c, _caller: ("41", "t-admin", "u-admin"),
    )
    # 仅验证概览数据结构不报错且包含 OCI 条目
    assert overview["latest_release"]["image_ready"] is True
    assert overview["latest_release"]["image_digest"] == digest22

    # 再验 docker -> venv 回滚也能顺利切回原发布
    rb_job = wizard_jobs.enqueue_job(
        reg_path,
        environment="jiejia",
        action="environment_rollback",
        request={
            "environment": "jiejia",
            "target_version": f"1.43.21+{rev21[:12]}",
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
        pinned_factory=lambda *_a, **_kw: FakeOciHost(),
        public_smoke_fn=lambda *_a: {"passed": True, "checks": []},
    )
    assert rb_done["passed"] is True
    assert rb_done["runtime_mode"] == "venv"
    assert rb_done["to_version"] == f"1.43.21+{rev21[:12]}"

    # 再验 docker -> docker 升级以及 switch_and_verify 失败触发自动回退
    runtime_state["mode"] = "docker"
    runtime_state["img_ref"] = f"registry.cn-hangzhou.aliyuncs.com/aiops/gateway@sha256:{'11' * 32}"
    runtime_state["digest"] = f"sha256:{'11' * 32}"

    class FailingOciSwitchHost(FakeOciHost):
        def run_cmd(self, cmd: str, *, timeout_sec: int = 120) -> subprocess.CompletedProcess[str]:
            if "AUTO_ROLLED_BACK" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 1, stdout="", stderr="AUTO_ROLLED_BACK:HEALTH_VERIFY_FAILED"
                )
            return super().run_cmd(cmd, timeout_sec=timeout_sec)

    fail_job = wizard_jobs.enqueue_job(
        reg_path,
        environment="jiejia",
        action="environment_upgrade",
        request={
            "environment": "jiejia",
            "target_tag": "v1.43.22",
            "target_revision": rev22,
            "target_version": "1.43.22",
            "target_full_version": f"1.43.22+{rev22[:12]}",
            "target_image_ref": ref22,
            "target_image_digest": digest22,
            "gateway_url": "https://api.juxichongdian.cn",
            "api_domain": "api.juxichongdian.cn",
            "tenant_id": "1990990768688599040",
        },
    )
    fail_done = wizard_worker.execute_environment_upgrade_job(
        repo,
        "41",
        fail_job,
        request=fake_request,
        pinned_factory=lambda *_a, **_kw: FailingOciSwitchHost(),
        public_smoke_fn=lambda *_a: {"passed": True, "checks": []},
    )
    assert fail_done["passed"] is False
    assert fail_done["failed_step"] == "switch_and_verify"
    assert fail_done["rolled_back_automatically"] is True


def test_oci_cold_install_and_onboard_skips_tarball_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files_bytes = {
        "/bundle/template.env": b"AIOPS_BIND=127.0.0.1\n",
        "/bundle/runtime-bundle.tar": b"rt-bytes",
        "/bundle/nginx-user-ingress-auth.conf": b"set $x 1;\n",
    }
    hashes = {k: hashlib.sha256(v).hexdigest() for k, v in files_bytes.items()}
    digest = "sha256:" + "d4" * 32
    ref = f"registry.cn-hangzhou.aliyuncs.com/aiops/gateway@{digest}"

    monkeypatch.setattr(subprocess, "check_output", lambda *_a, **_k: "e" * 40 + "\n")
    monkeypatch.setattr(
        wizard_worker,
        "_ensure_pool_artifacts",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("OCI 冷装不应触发 tarball 构建")),
    )
    monkeypatch.setattr(
        wizard_worker,
        "_resolve_oci_image_for_revision",
        lambda *_a, **_kw: {
            "schema": "aiops-gateway-image/1",
            "tag": "v1.43.22",
            "revision": "e" * 40,
            "image_ref": ref,
            "image_digest": digest,
        },
    )
    monkeypatch.setattr(wizard_worker, "_read_registry_config_from_central", lambda *_a, **_kw: None)
    monkeypatch.setattr(wizard_worker, "_dev_host_read", lambda _t, p, **_k: files_bytes[p])

    copied: list[str] = []
    cmds: list[str] = []

    class _Pin:
        def copy_file(self, _src: Path, dst: str) -> None:
            copied.append(dst)

        def run_cmd(self, cmd: str, **_k):
            cmds.append(cmd)
            if "AIOPS_DOCKER_OK" in cmd:
                return SimpleNamespace(returncode=0, stdout=b"AIOPS_DOCKER_OK\n", stderr=b"")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    out = wizard_worker._step_cold_install_and_onboard(
        tmp_path,
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
    assert not any(c.endswith("application.tar.gz") or c.endswith("installer.tar.gz") for c in copied)
    assert any("docker pull" in c and "seed_target_gateway_db" in c for c in cmds)
