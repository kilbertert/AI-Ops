"""Day-2 已上线客户环境版本治理、无损滚动升级与安全回滚测试（ADR-0361）。

拦截的具体错误：
1. 未打官方注释 SemVer 标签或不在 origin/main 祖先上的提交被纳入发布目录；
2. 控制面 41 误开放页面自升级/回滚，绕过受保护 GitHub CD；
3. 客户机升级修改了 production.env / registry/environment.json / nginx-user-ingress-auth.conf；
4. 切换后 /health 校验失败时未自动切回旧 release、未恢复 Nginx 备份或未保留失败回执；
5. 缺少 confirm_rollback 或缺少 rollback_target 时仍执行回滚。
"""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiops_diagnostics.dify import self_service_identity as auth
from aiops_diagnostics.onboarding import (
    host_enrollment,
    registry,
    release_governance,
    wizard_dispatch,
    wizard_jobs,
    wizard_worker,
)


def _setup_registry_with_jiejia(tmp_path: Path) -> tuple[Path, Path]:
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
    identity = ("41", "t-admin", "u-admin")
    host_enrollment.enroll_via_ephemeral_password(
        reg_path,
        identity,
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


def test_release_tag_validation_and_git_annotated_collection(tmp_path: Path) -> None:
    assert release_governance.is_valid_release_tag("v1.43.20") is True
    assert release_governance.is_valid_release_tag("v1.43") is False
    assert release_governance.is_valid_release_tag("deadbeef") is False
    assert release_governance.is_valid_release_tag("v1.43.20-dirty") is False

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
    subprocess.run(["git", "commit", "-q", "-m", "feat: base"], cwd=repo, check=True)
    base_rev = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", base_rev], cwd=repo, check=True)

    # 轻量标签必须被拒绝，只有 annotated tag 才合法
    subprocess.run(["git", "tag", "v1.43.18", base_rev], cwd=repo, check=True)
    subprocess.run(["git", "tag", "-a", "v1.43.19", "-m", "AI-Ops 1.43.19", base_rev], cwd=repo, check=True)

    # 不在 origin/main 祖先上的注释标签也必须被拒绝
    subprocess.run(["git", "checkout", "-q", "-b", "side"], cwd=repo, check=True)
    (repo / "side.txt").write_text("unmerged")
    subprocess.run(["git", "add", "side.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "unmerged"], cwd=repo, check=True)
    subprocess.run(["git", "tag", "-a", "v1.43.99", "-m", "unmerged"], cwd=repo, check=True)

    collected = release_governance.collect_git_annotated_releases(
        repo, pool_entries=[{"revision": base_rev, "python_minor": "3.12"}]
    )
    assert [item["tag"] for item in collected] == ["v1.43.19"]
    assert collected[0]["revision"] == base_rev
    assert collected[0]["pool_python_minors"] == ["3.12"]
    assert collected[0]["ingress_routes"] == ["GET /v1/faq/catalog"]


def test_dispatch_release_overview_guards_control_41_and_validates_inputs(tmp_path: Path) -> None:
    reg_path, assets_dir = _setup_registry_with_jiejia(tmp_path)
    rev19 = "a" * 40
    rev20 = "b" * 40
    release_governance.save_release_catalog(
        assets_dir,
        [
            {
                "tag": "v1.43.20",
                "revision": rev20,
                "summary": "health-v3 routes",
                "ingress_routes": ["GET /v1/faq/catalog", "POST /v1/health-report-jobs"],
                "ingress_sha256": "2" * 64,
                "pool_python_minors": ["3.12"],
            },
            {
                "tag": "v1.43.19",
                "revision": rev19,
                "summary": "base",
                "ingress_routes": ["GET /v1/faq/catalog"],
                "ingress_sha256": "1" * 64,
            },
        ],
    )
    release_governance.save_environment_release_state(
        assets_dir,
        "jiejia",
        {
            "current_version": f"1.43.19+{rev19[:12]}",
            "current_revision": rev19,
            "python_minor": "3.12",
            "business_mutations": "disabled",
            "current_ingress_routes": ["GET /v1/faq/catalog"],
            "current_ingress_sha256": "1" * 64,
        },
    )

    ctx = SimpleNamespace(version="1.43.20+deadbeefcafe")
    session = object()

    def access_fn(_c: object, _s: object) -> tuple[str, dict, set[str]]:
        return ("u-admin", {}, {"*"})

    def path_fn(_c: object) -> str:
        return str(reg_path)

    def identity_fn(_c: object, _caller: object) -> tuple[str, str, str]:
        return ("41", "t-admin", "u-admin")

    overview = wizard_dispatch.dispatch_wizard(
        ctx,
        session,
        {"wizard_action": "release_overview", "wizard_payload": {"environment": "jiejia"}},
        access_fn=access_fn,
        path_fn=path_fn,
        identity_fn=identity_fn,
    )
    assert overview["is_control"] is False
    assert overview["enrolled"] is True
    assert overview["update_available"] is True
    assert overview["latest_release"]["tag"] == "v1.43.20"
    assert overview["latest_release"]["ingress_diff"] == {
        "changed": True,
        "added_routes": ["POST /v1/health-report-jobs"],
        "removed_routes": [],
    }

    # 41 控制面严禁通过该接口自升级或回滚
    with pytest.raises(auth.SetupRefused, match="ENVIRONMENT_CONTROL_PLANE_USE_CD"):
        wizard_dispatch.dispatch_wizard(
            ctx,
            session,
            {
                "wizard_action": "environment_upgrade",
                "wizard_payload": {"environment": "41", "target_tag": "v1.43.20"},
            },
            access_fn=access_fn,
            path_fn=path_fn,
            identity_fn=identity_fn,
        )

    # 未给 confirm_rollback=True 拒绝回滚；无 rollback_target 拒绝回滚
    with pytest.raises(auth.SetupRefused, match="ENVIRONMENT_ROLLBACK_CONFIRM_REQUIRED"):
        wizard_dispatch.dispatch_wizard(
            ctx,
            session,
            {
                "wizard_action": "environment_rollback",
                "wizard_payload": {"environment": "jiejia", "confirm_rollback": False},
            },
            access_fn=access_fn,
            path_fn=path_fn,
            identity_fn=identity_fn,
        )
    with pytest.raises(auth.SetupRefused, match="ENVIRONMENT_ROLLBACK_UNAVAILABLE"):
        wizard_dispatch.dispatch_wizard(
            ctx,
            session,
            {
                "wizard_action": "environment_rollback",
                "wizard_payload": {"environment": "jiejia", "confirm_rollback": True},
            },
            access_fn=access_fn,
            path_fn=path_fn,
            identity_fn=identity_fn,
        )


def test_worker_upgrade_and_rollback_verify_zero_touch_and_auto_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg_path, assets_dir = _setup_registry_with_jiejia(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    (repo / "ops/nginx").mkdir(parents=True)
    (repo / "ops/nginx/environment-user-ingress.conf").write_text(
        "# （环境 __ENV__）\n"
        "location = /v1/faq/catalog {\n"
        "  include /etc/aiops-access-__ENV__/environment-user-ingress-proxy.inc;\n"
        "}\n"
    )
    (repo / "ops/nginx/environment-user-ingress-proxy.inc").write_text("proxy_pass http://127.0.0.1:8788;\n")
    (repo / "ops/nginx/environment-user-ingress-routes.txt").write_text("GET /v1/faq/catalog\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "v19"], cwd=repo, check=True)
    rev19 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    subprocess.run(["git", "tag", "-a", "v1.43.19", "-m", "v1.43.19", rev19], cwd=repo, check=True)

    (repo / "ops/nginx/environment-user-ingress-routes.txt").write_text(
        "GET /v1/faq/catalog\nPOST /v1/health-report-jobs\n"
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "v20"], cwd=repo, check=True)
    rev20 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    subprocess.run(["git", "tag", "-a", "v1.43.20", "-m", "v1.43.20", rev20], cwd=repo, check=True)
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", rev20], cwd=repo, check=True)

    catalog = release_governance.collect_git_annotated_releases(repo)
    release_governance.save_release_catalog(assets_dir, catalog)

    app_archive = tmp_path / "app20.tar.gz"
    app_archive.write_bytes(b"mock-tar")
    app_sha = hashlib.sha256(b"mock-tar").hexdigest()
    monkeypatch.setattr(
        wizard_worker,
        "_ensure_pool_artifacts",
        lambda *_a, **_kw: {
            "revision": rev20,
            "python_minor": "3.12",
            "app_remote": "/var/lib/aiops-41/onboarding-assets/app20.tar.gz",
            "app_sha256": app_sha,
            "app_local": str(app_archive),
            "inst_remote": "/var/lib/aiops-41/onboarding-assets/inst.tar.gz",
            "inst_sha256": "0" * 64,
            "inst_local": "",
        },
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

    current_ver = f"1.43.19+{rev19[:12]}"
    copied_files: list[str] = []

    class FakePinnedHost:
        def copy_file(self, _local: Path, remote: str) -> None:
            copied_files.append(remote)

        def run_cmd(self, cmd: str, *, timeout_sec: int = 120) -> subprocess.CompletedProcess[str]:
            if cmd.startswith("python3 -c "):
                parts = shlex.split(cmd)
                compile(parts[2], "<remote-python>", "exec")
            if "python3 -c" in cmd and "installed.json" in cmd and "current_release_dir" in cmd:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=json.dumps(
                        {
                            "port": 8788,
                            "current_is_symlink": True,
                            "current_release_dir": f"/opt/aiops-access-jiejia/releases/{rev19[:12]}",
                            "current_release_name": rev19[:12],
                            "installed_version": current_ver,
                            "installed_revision": rev19,
                            "python_minor": "3.12",
                            "health": {"ok": True, "version": current_ver, "business_mutations": "disabled"},
                            "user_conf": "",
                            "proxy_inc": "",
                            "previous_release": {
                                "release_dir": f"/opt/aiops-access-jiejia/releases/{rev19[:12]}",
                                "version": f"1.43.19+{rev19[:12]}",
                                "revision": rev19,
                                "nginx_backup_dir": "/var/backups/bak1",
                                "recorded_at": 100.0,
                            },
                        }
                    ),
                    stderr="",
                )
            if "pre_hashes" in cmd and "ARCHIVE_SHA_MISMATCH" in cmd:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=json.dumps(
                        {
                            "pre_hashes": {"/etc/aiops-access-jiejia/production.env": "abc"},
                            "old_release_dir": f"/opt/aiops-access-jiejia/releases/{rev19[:12]}",
                        }
                    ),
                    stderr="",
                )
            if "AUTO_ROLLED_BACK" in cmd:
                nonlocal_update(f"1.43.20+{rev20[:12]}")
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=json.dumps({"ok": True, "untouched_config_verified": True}),
                    stderr="",
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
                            "prev_ver": f"1.43.19+{rev19[:12]}",
                            "prev_rev": rev19,
                            "prev_dir": f"/opt/aiops-access-jiejia/releases/{rev19[:12]}",
                            "has_nginx_bak": True,
                        }
                    ),
                    stderr="",
                )
            if "ROLLBACK_HEALTH_VERIFY_FAILED" in cmd:
                nonlocal_update(f"1.43.19+{rev19[:12]}")
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=json.dumps(
                        {"ok": True, "nginx_restored": True, "untouched_config_verified": True}
                    ),
                    stderr="",
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def nonlocal_update(ver: str) -> None:
        nonlocal current_ver
        current_ver = ver

    job = wizard_jobs.enqueue_job(
        reg_path,
        environment="jiejia",
        action="environment_upgrade",
        request={
            "environment": "jiejia",
            "target_tag": "v1.43.20",
            "target_revision": rev20,
            "target_version": "1.43.20",
            "target_full_version": f"1.43.20+{rev20[:12]}",
            "gateway_url": "https://api.juxichongdian.cn",
            "api_domain": "api.juxichongdian.cn",
            "tenant_id": "1990990768688599040",
        },
    )
    done = wizard_worker.execute_environment_upgrade_job(
        repo,
        "41",
        job,
        request=fake_request,
        pinned_factory=lambda *_a, **_kw: FakePinnedHost(),
        public_smoke_fn=lambda *_a: {"passed": True, "checks": []},
    )
    assert done["passed"] is True
    assert done["to_version"] == f"1.43.20+{rev20[:12]}"
    assert done["untouched_config_verified"] is True
    assert done["nginx_changed"] is True
    assert state_store["jiejia"]["current_version"] == f"1.43.20+{rev20[:12]}"

    # 再验一键回滚回到 1.43.19
    rb_job = wizard_jobs.enqueue_job(
        reg_path,
        environment="jiejia",
        action="environment_rollback",
        request={
            "environment": "jiejia",
            "target_version": f"1.43.19+{rev19[:12]}",
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
        pinned_factory=lambda *_a, **_kw: FakePinnedHost(),
        public_smoke_fn=lambda *_a: {"passed": True, "checks": []},
    )
    assert rb_done["passed"] is True
    assert rb_done["to_version"] == f"1.43.19+{rev19[:12]}"
    assert rb_done["untouched_config_verified"] is True
    assert rb_done["nginx_changed"] is True

    # 模拟 switch_and_verify 失败时记录 rolled_back_automatically=True
    class FailingSwitchHost(FakePinnedHost):
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
            "target_tag": "v1.43.20",
            "target_revision": rev20,
            "target_version": "1.43.20",
            "target_full_version": f"1.43.20+{rev20[:12]}",
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
        pinned_factory=lambda *_a, **_kw: FailingSwitchHost(),
        public_smoke_fn=lambda *_a: {"passed": True, "checks": []},
    )
    assert fail_done["passed"] is False
    assert fail_done["failed_step"] == "switch_and_verify"
    assert fail_done["rolled_back_automatically"] is True
