"""客户机 Gateway 单一 OCI 容器分发与 Day-2 升级/回滚执行器（ADR-0363 简化收敛版）。

设计原则：
- **唯一路径**：新机冷装与已上线客户机升级/回滚统一走阿里云 ACR 不可变 `@sha256` 容器镜像，
  不再保留跨海 24 MiB tarball 与双轨 `venv` 分支；
- **零字符串拼代码**：客户机动作全部收敛在可独立 lint/单测且随包发布的
  `src/aiops_diagnostics/onboarding/host_release_agent.py`，
  本模块仅通过 stdin 投递并解析返回的 JSON 结果；
- **单向镜像真源**：镜像 `@sha256` 只从 GitHub Release `gateway-image.json`
  发现并写入 `release-catalog.json`。
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from aiops_diagnostics.onboarding import (
    container_registry,
    public_smoke,
    release_governance,
)

_AGENT_SCRIPT_PATH = Path(__file__).resolve().parent / "host_release_agent.py"
_LAST_RELEASE_CATALOG_DIGEST = ""


def _decode_output(raw: bytes | str | None) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw or "")


def _host_agent_source() -> str:
    return _AGENT_SCRIPT_PATH.read_text(encoding="utf-8")


def run_host_agent(
    pinned_host: Any,
    subcmd: str,
    payload: dict[str, Any],
    *,
    timeout_sec: int = 120,
) -> dict[str, Any]:
    """经钉扎 SSH stdin 执行 `host_release_agent.py <subcmd> <json>` 并解析末尾 JSON 行。"""
    remote_cmd = f"python3 - {shlex.quote(subcmd)} {shlex.quote(json.dumps(payload, ensure_ascii=False))}"
    res = pinned_host.run_cmd(remote_cmd, input_data=_host_agent_source(), timeout_sec=timeout_sec)
    stdout_txt = _decode_output(res.stdout).strip()
    if res.returncode != 0 or not stdout_txt:
        err_tail = _decode_output(res.stderr).strip().splitlines()
        hint = err_tail[-1][:80] if err_tail else ""
        raise RuntimeError(hint or f"HOST_AGENT_{subcmd.upper().replace('-', '_')}_FAILED")
    return json.loads(stdout_txt.splitlines()[-1])


def _read_registry_config_from_central(
    read_central_fn: Callable[..., bytes],
    root: Path,
    central_target: str,
) -> dict[str, Any] | None:
    if not central_target:
        return None
    remote_path = "/var/lib/aiops-41/onboarding-assets/registry-config.json"
    try:
        raw = read_central_fn(central_target, remote_path, cwd=root)
    except Exception:
        return None
    return container_registry.parse_registry_config_bytes(raw)


def ensure_customer_docker_auth(
    pinned_host: Any,
    env: str,
    registry_cfg: dict[str, Any] | None,
    tmp_dir: Path,
) -> str:
    """将独立拉取凭据写入 `/etc/aiops-access-<env>/docker-config/config.json`，避免触碰宿主机 config。"""
    cfg_json = container_registry.render_customer_docker_config_json(registry_cfg)
    if not cfg_json:
        return ""
    local_file = tmp_dir / f"docker-config-{env}.json"
    local_file.write_text(cfg_json, encoding="utf-8")
    local_file.chmod(0o600)
    stage_json = f"/tmp/.aiops-dcfg-{env}-{int(time.time())}.json"
    pinned_host.copy_file(local_file, stage_json)
    dcfg_dir = f"/etc/aiops-access-{env}/docker-config"
    setup_cmd = " && ".join(
        [
            "set -euo pipefail",
            f"mkdir -p {shlex.quote(dcfg_dir)}",
            f"chmod 700 {shlex.quote(dcfg_dir)}",
            f"mv -f {shlex.quote(stage_json)} {shlex.quote(dcfg_dir)}/config.json",
            f"chown root:root {shlex.quote(dcfg_dir)} {shlex.quote(dcfg_dir)}/config.json",
            f"chmod 600 {shlex.quote(dcfg_dir)}/config.json",
        ]
    )
    res = pinned_host.run_cmd(setup_cmd, timeout_sec=30)
    if res.returncode != 0:
        raise RuntimeError("CUSTOMER_DOCKER_AUTH_WRITE_FAILED")
    return dcfg_dir


def sync_release_catalog_to_central(
    root: Path,
    central_target: str,
    *,
    request: Callable[[dict[str, Any]], dict[str, Any]],
    fetch_tags: bool = True,
) -> list[dict[str, Any]]:
    """从 origin/main 注释 SemVer 标签与 GitHub Release `gateway-image.json` 同步目录至 41。"""
    global _LAST_RELEASE_CATALOG_DIGEST
    if fetch_tags:
        with suppress(subprocess.SubprocessError, OSError):
            subprocess.run(
                ["git", "fetch", "--tags", "origin", "main", "--quiet"],
                cwd=str(root),
                capture_output=True,
                timeout=30,
            )
    base_releases = release_governance.collect_git_annotated_releases(root)
    image_entries = container_registry.discover_release_images_from_github(root, base_releases)
    entries = release_governance.collect_git_annotated_releases(root, image_entries=image_entries)
    digest = hashlib.sha256(json.dumps(entries, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if entries and digest != _LAST_RELEASE_CATALOG_DIGEST:
        request({"wizard_job": "save_release_catalog", "entries": entries})
        _LAST_RELEASE_CATALOG_DIGEST = digest
    return entries


def _render_revision_user_ingress(root: Path, revision: str, env: str, local_port: int) -> dict[str, Any]:
    conf_proc = subprocess.run(
        ["git", "show", f"{revision}:ops/nginx/environment-user-ingress.conf"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    inc_proc = subprocess.run(
        ["git", "show", f"{revision}:ops/nginx/environment-user-ingress-proxy.inc"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    routes_proc = subprocess.run(
        ["git", "show", f"{revision}:ops/nginx/environment-user-ingress-routes.txt"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=15,
    )
    tpl_conf = conf_proc.stdout
    tpl_inc = inc_proc.stdout
    return {
        "user_conf": tpl_conf.replace("__ENV__", env).replace(
            "http://127.0.0.1:8788", f"http://127.0.0.1:{local_port}"
        ),
        "proxy_inc": tpl_inc.replace("http://127.0.0.1:8788", f"http://127.0.0.1:{local_port}"),
        "routes": (
            release_governance.parse_ingress_routes_text(routes_proc.stdout)
            if routes_proc.returncode == 0
            else []
        ),
        "template_sha256": release_governance.render_ingress_sha256(tpl_conf, tpl_inc),
    }


def _match_routes_by_nginx_locations(user_conf: str) -> list[str]:
    locations = re.findall(r"^\s*location\s+(?:=|\^~)\s+(/v1/[^\s{]+)", user_conf, re.MULTILINE)
    return [f"NGINX {loc}" for loc in locations]


def probe_customer_release_state(
    pinned_host: Any,
    env: str,
    api_domain: str,
    catalog_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    raw = run_host_agent(pinned_host, "probe", {"env": env, "api_domain": api_domain}, timeout_sec=30)
    port = int(raw.get("port") or 8788)
    uconf = str(raw.get("user_conf") or "")
    pinc = str(raw.get("proxy_inc") or "")
    norm_conf = (
        uconf.replace(f"/etc/aiops-access-{env}/", "/etc/aiops-access-__ENV__/")
        .replace(f"（环境 {env}）", "（环境 __ENV__）")
        .replace(f"http://127.0.0.1:{port}", "http://127.0.0.1:8788")
    )
    norm_inc = pinc.replace(f"http://127.0.0.1:{port}", "http://127.0.0.1:8788")
    norm_sha = release_governance.render_ingress_sha256(norm_conf, norm_inc) if uconf and pinc else ""

    health = raw.get("health") if isinstance(raw.get("health"), dict) else {}
    cur_ver = str(health.get("version") or raw.get("installed_version") or "")
    cur_rev = str(raw.get("installed_revision") or "")
    if not cur_rev and "+" in cur_ver:
        cur_rev = cur_ver.split("+", 1)[1].strip()

    matched_entry = next(
        (e for e in catalog_entries if norm_sha and e.get("ingress_sha256") == norm_sha), None
    )
    if matched_entry is None and cur_rev:
        matched_entry = next(
            (e for e in catalog_entries if str(e.get("revision") or "").startswith(cur_rev[:12])), None
        )
    routes = (
        list(matched_entry.get("ingress_routes") or [])
        if matched_entry
        else _match_routes_by_nginx_locations(uconf)
    )

    prev = raw.get("previous_release")
    rollback_target = None
    if isinstance(prev, dict) and prev.get("version"):
        prev_digest = str(prev.get("image_digest") or "")
        rollback_target = {
            "version": str(prev.get("version") or ""),
            "revision": str(prev.get("revision") or ""),
            "release_name": prev_digest[:19] if prev_digest else "",
            "runtime_mode": str(prev.get("runtime_mode") or "docker"),
            "image_digest": prev_digest,
            "recorded_at": prev.get("recorded_at"),
            "has_nginx_backup": bool(prev.get("nginx_backup_dir")),
        }

    return {
        "port": port,
        "runtime_mode": str(raw.get("runtime_mode") or "docker"),
        "docker_available": bool(raw.get("docker_available")),
        "current_image_ref": str(raw.get("current_image_ref") or ""),
        "current_image_digest": str(raw.get("current_image_digest") or ""),
        "current_release_name": str(raw.get("current_release_name") or ""),
        "current_version": cur_ver,
        "current_revision": cur_rev,
        "python_minor": str(raw.get("python_minor") or "3.12") or "3.12",
        "health_ok": bool(health.get("ok")),
        "business_mutations": str(health.get("business_mutations") or ""),
        "current_ingress_sha256": norm_sha,
        "current_ingress_routes": routes,
        "rollback_target": rollback_target,
    }


def _persist_release_state(
    request: Callable[[dict[str, Any]], dict[str, Any]], env: str, probe_data: dict[str, Any]
) -> None:
    request(
        {
            "wizard_job": "save_release_state",
            "environment": env,
            "state": {
                "current_version": probe_data["current_version"],
                "current_revision": probe_data["current_revision"],
                "python_minor": probe_data["python_minor"],
                "runtime_mode": probe_data["runtime_mode"],
                "current_image_digest": probe_data["current_image_digest"],
                "current_image_ref": probe_data["current_image_ref"],
                "docker_available": probe_data["docker_available"],
                "health_ok": probe_data["health_ok"],
                "business_mutations": probe_data["business_mutations"],
                "current_ingress_sha256": probe_data["current_ingress_sha256"],
                "current_ingress_routes": probe_data["current_ingress_routes"],
                "rollback_target": probe_data["rollback_target"],
                "last_probed_at": time.time(),
            },
        }
    )


def _protected_paths(env: str) -> list[str]:
    return [
        f"/etc/aiops-access-{env}/production.env",
        f"/etc/aiops-access-{env}/registry/environment.json",
        f"/etc/aiops-access-{env}/nginx-user-ingress-auth.conf",
    ]


def _job_helpers(
    request: Callable[[dict[str, Any]], dict[str, Any]], job_id: str, steps: list[dict[str, str]]
) -> tuple[Callable[[int, str, str], None], Callable[[int, str, dict[str, Any] | None], dict[str, Any]]]:
    def _update(idx: int, state: str, reason: str = "") -> None:
        steps[idx]["state"] = state
        steps[idx]["reason"] = reason
        request(
            {
                "wizard_job": "progress",
                "job_id": job_id,
                "step": steps[idx]["step"],
                "step_state": state,
                "steps": steps,
            }
        )

    def _fail(idx: int, code: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        _update(idx, "failed", code)
        return request(
            {
                "wizard_job": "complete",
                "job_id": job_id,
                "passed": False,
                "error_code": code,
                "failed_step": steps[idx]["step"],
                "steps": steps,
                "result": dict(extra or {}),
            }
        )

    return _update, _fail


def _validate_target_release_tag(root: Path, target_tag: str, expected_rev: str) -> str:
    if not release_governance.is_valid_release_tag(target_tag):
        raise ValueError("ENVIRONMENT_RELEASE_TAG_INVALID")
    tag_type = subprocess.run(
        ["git", "cat-file", "-t", target_tag], cwd=str(root), capture_output=True, text=True, timeout=15
    )
    if tag_type.returncode != 0 or tag_type.stdout.strip() != "tag":
        raise ValueError("ENVIRONMENT_RELEASE_TAG_NOT_ANNOTATED")
    resolved = subprocess.run(
        ["git", "rev-parse", "--verify", f"{target_tag}^{{commit}}"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=15,
    )
    rev = resolved.stdout.strip().lower()
    if resolved.returncode != 0 or (expected_rev and rev != expected_rev):
        raise ValueError("ENVIRONMENT_RELEASE_REVISION_MISMATCH")
    anc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", rev, "origin/main"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=15,
    )
    if anc.returncode != 0:
        raise ValueError("ENVIRONMENT_RELEASE_NOT_ON_MAIN")
    return rev


def execute_release_probe_job(
    root: Path,
    central_target: str,
    job: dict[str, Any],
    *,
    request: Callable[[dict[str, Any]], dict[str, Any]],
    pinned_factory: Callable[..., Any],
) -> dict[str, Any]:
    """只读探测客户机线上容器运行态、/health、Nginx 入口与可回滚目标。"""
    job_id = str(job["job_id"])
    req = dict(job.get("request") or {})
    env = str(job["environment"])
    steps = [{"step": "probe_target", "state": "running", "reason": ""}]
    request(
        {
            "wizard_job": "progress",
            "job_id": job_id,
            "step": "probe_target",
            "step_state": "running",
            "steps": steps,
        }
    )
    try:
        catalog = sync_release_catalog_to_central(root, central_target, request=request)
        pinned_host = pinned_factory(central_target, env, cwd=root)
        probe_data = probe_customer_release_state(pinned_host, env, str(req.get("api_domain") or ""), catalog)
        _persist_release_state(request, env, probe_data)
    except Exception as exc:
        reason = str(exc).strip()[:120] or "TARGET_RELEASE_PROBE_FAILED"
        steps[0]["state"] = "failed"
        steps[0]["reason"] = reason
        return request(
            {
                "wizard_job": "complete",
                "job_id": job_id,
                "passed": False,
                "error_code": reason,
                "failed_step": "probe_target",
                "steps": steps,
            }
        )

    steps[0]["state"] = "passed"
    return request(
        {
            "wizard_job": "complete",
            "job_id": job_id,
            "passed": True,
            "steps": steps,
            "result": {
                "current_version": probe_data["current_version"],
                "current_revision": probe_data["current_revision"],
                "python_minor": probe_data["python_minor"],
                "runtime_mode": probe_data["runtime_mode"],
                "current_image_digest": probe_data["current_image_digest"],
                "docker_available": probe_data["docker_available"],
                "health_ok": probe_data["health_ok"],
                "business_mutations": probe_data["business_mutations"],
            },
        }
    )


def execute_environment_upgrade_job(
    root: Path,
    central_target: str,
    job: dict[str, Any],
    *,
    request: Callable[[dict[str, Any]], dict[str, Any]],
    pinned_factory: Callable[..., Any],
    read_central_fn: Callable[..., bytes],
    public_smoke_fn: Callable[[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """执行已上线客户环境无损 OCI 容器升级（国内秒拉 + 零触碰核验 + 自动回滚）。"""
    job_id = str(job["job_id"])
    req = dict(job.get("request") or {})
    env = str(job["environment"])
    target_tag = str(req.get("target_tag") or "").strip()
    target_rev = str(req.get("target_revision") or "").strip()
    target_full_version = str(req.get("target_full_version") or "").strip()
    target_image_ref = str(req.get("target_image_ref") or "").strip()
    gateway_url = str(req.get("gateway_url") or "").strip()
    api_domain = str(req.get("api_domain") or "").strip()
    tenant_id = str(req.get("tenant_id") or "").strip()

    steps = [
        {"step": name, "state": "pending", "reason": ""}
        for name in (
            "resolve_artifact",
            "install_release",
            "sync_nginx_ingress",
            "switch_and_verify",
            "public_smoke",
        )
    ]
    _update, _fail = _job_helpers(request, job_id, steps)

    # Step 0: 校验注释标签、发现不可变镜像摘要、探测 Docker 与渲染受控 Nginx 入口
    _update(0, "running", "")
    try:
        target_rev = _validate_target_release_tag(root, target_tag, target_rev)
        catalog = sync_release_catalog_to_central(root, central_target, request=request, fetch_tags=False)
        if not target_image_ref:
            matched_cat = next((e for e in catalog if str(e.get("revision") or "") == target_rev), None)
            if isinstance(matched_cat, dict):
                target_image_ref = str(matched_cat.get("image_ref") or "").strip()
        if not container_registry.is_valid_image_ref(target_image_ref):
            raise ValueError("ENVIRONMENT_RELEASE_IMAGE_NOT_PUBLISHED")
        _, _, target_digest = container_registry.validate_image_ref(target_image_ref)

        pinned_host = pinned_factory(central_target, env, cwd=root)
        before_probe = probe_customer_release_state(pinned_host, env, api_domain, catalog)
        if not before_probe.get("docker_available"):
            raise ValueError("ENVIRONMENT_DOCKER_REQUIRED")

        local_port = int(before_probe["port"])
        registry_cfg = _read_registry_config_from_central(read_central_fn, root, central_target)
        nginx_bundle = _render_revision_user_ingress(root, target_rev, env, local_port)
    except Exception as exc:
        return _fail(0, str(exc).strip()[:120] or "RELEASE_RESOLVE_ARTIFACT_FAILED", None)
    _update(0, "passed", "")

    stage_remote = f"/var/backups/aiops-upgrade-stage-{env}"
    bak_dir = f"/var/backups/aiops-upgrade-bak-{env}-{job_id[-8:]}"
    protected = _protected_paths(env)

    # Step 1: 写入隔离 Docker 认证（如配置）、推送轻量单元与 Nginx 文件、拉取并预检镜像
    _update(1, "running", "")
    try:
        q_stage = shlex.quote(stage_remote)
        pinned_host.run_cmd(f"rm -rf {q_stage} && mkdir -p {q_stage} && chmod 700 {q_stage}", timeout_sec=30)
        with tempfile.TemporaryDirectory(prefix="aiops-oci-up-") as tmpdir:
            tmp = Path(tmpdir)
            tmp.chmod(0o700)
            ensure_customer_docker_auth(pinned_host, env, registry_cfg, tmp)
            unit_f = tmp / f"aiops-access-{env}.service"
            unit_f.write_text(container_registry.render_container_systemd_unit(env), encoding="utf-8")
            unit_f.chmod(0o644)
            pinned_host.copy_file(unit_f, f"{stage_remote}/aiops-access-{env}.service")
            for fname, txt in (
                ("aiops-user-ingress.conf", nginx_bundle["user_conf"]),
                ("environment-user-ingress-proxy.inc", nginx_bundle["proxy_inc"]),
            ):
                nf = tmp / fname
                nf.write_text(txt, encoding="utf-8")
                nf.chmod(0o644)
                pinned_host.copy_file(nf, f"{stage_remote}/{fname}")

        pull_info = run_host_agent(
            pinned_host,
            "pull-verify",
            {
                "env": env,
                "image_ref": target_image_ref,
                "target_ver": target_full_version,
                "protected_paths": protected,
            },
            timeout_sec=240,
        )
    except Exception as exc:
        return _fail(1, str(exc).strip()[:120] or "TARGET_OCI_PULL_VERIFY_FAILED", None)
    _update(1, "passed", "")

    # Step 2: 对比同步受控 Nginx 入口 + nginx -t
    _update(2, "running", "")
    try:
        ngx_info = run_host_agent(
            pinned_host,
            "sync-nginx",
            {
                "env": env,
                "api_domain": api_domain,
                "stage_dir": stage_remote,
                "bak_dir": bak_dir,
            },
            timeout_sec=45,
        )
        nginx_changed = bool(ngx_info.get("nginx_changed"))
    except Exception as exc:
        return _fail(2, str(exc).strip()[:120] or "CUSTOMER_NGINX_SYNC_FAILED", None)
    _update(2, "passed", "")

    # Step 3: 原子切换 container.env 与 systemd unit、/health 与零触碰 SHA 校验；异常自动回退
    _update(3, "running", "")
    try:
        run_host_agent(
            pinned_host,
            "switch",
            {
                "env": env,
                "port": local_port,
                "stage_dir": stage_remote,
                "bak_dir": bak_dir,
                "api_domain": api_domain,
                "target_ver": target_full_version,
                "target_rev": target_rev,
                "target_tag": target_tag,
                "image_ref": target_image_ref,
                "image_digest": target_digest,
                "old_image_ref": pull_info.get("old_image_ref") or "",
                "run_uid": pull_info["run_uid"],
                "run_gid": pull_info["run_gid"],
                "nginx_changed": nginx_changed,
                "pre_hashes": pull_info.get("pre_hashes") or {},
            },
            timeout_sec=120,
        )
    except Exception as exc:
        return _fail(
            3,
            str(exc).strip()[:120] or "TARGET_SWITCH_VERIFY_ROLLED_BACK",
            {"rolled_back_automatically": True},
        )
    _update(3, "passed", "")

    # Step 4: 公网 HTTPS 冒烟 + 回写 41 版本账本
    _update(4, "running", "")
    try:
        smoke_fn = public_smoke_fn or public_smoke.run_post_publish_https_smoke
        if not smoke_fn(gateway_url, tenant_id).get("passed"):
            raise ValueError("POST_UPGRADE_PUBLIC_SMOKE_FAILED")
        after_probe = probe_customer_release_state(pinned_host, env, api_domain, catalog)
        _persist_release_state(request, env, after_probe)
    except Exception as exc:
        return _fail(4, str(exc).strip()[:120] or "POST_UPGRADE_PUBLIC_SMOKE_FAILED", None)
    _update(4, "passed", "")

    return request(
        {
            "wizard_job": "complete",
            "job_id": job_id,
            "passed": True,
            "steps": steps,
            "result": {
                "from_version": before_probe["current_version"],
                "to_version": after_probe["current_version"],
                "target_tag": target_tag,
                "distribution_mode": "oci",
                "runtime_mode": "docker",
                "image_digest": target_digest,
                "nginx_changed": nginx_changed,
                "untouched_config_verified": True,
                "rolled_back_automatically": False,
                "smoke_passed": True,
                "business_mutations": after_probe["business_mutations"] or "disabled",
                "business_accuracy": "unconfirmed",
            },
        }
    )


def execute_environment_rollback_job(
    root: Path,
    central_target: str,
    job: dict[str, Any],
    *,
    request: Callable[[dict[str, Any]], dict[str, Any]],
    pinned_factory: Callable[..., Any],
    public_smoke_fn: Callable[[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """一键安全回滚客户机至上一已验证 Docker OCI 镜像摘要（含 Nginx 备份联动恢复）。"""
    job_id = str(job["job_id"])
    req = dict(job.get("request") or {})
    env = str(job["environment"])
    gateway_url = str(req.get("gateway_url") or "").strip()
    api_domain = str(req.get("api_domain") or "").strip()
    tenant_id = str(req.get("tenant_id") or "").strip()

    steps = [
        {"step": name, "state": "pending", "reason": ""}
        for name in ("verify_previous_release", "switch_and_verify", "public_smoke")
    ]
    _update, _fail = _job_helpers(request, job_id, steps)

    _update(0, "running", "")
    try:
        catalog = sync_release_catalog_to_central(root, central_target, request=request, fetch_tags=False)
        pinned_host = pinned_factory(central_target, env, cwd=root)
        before_probe = probe_customer_release_state(pinned_host, env, api_domain, catalog)
        rb_target = before_probe.get("rollback_target")
        if not isinstance(rb_target, dict) or not rb_target.get("version"):
            raise ValueError("ENVIRONMENT_ROLLBACK_UNAVAILABLE")
        local_port = int(before_probe["port"])
    except Exception as exc:
        return _fail(0, str(exc).strip()[:120] or "ENVIRONMENT_ROLLBACK_UNAVAILABLE", None)
    _update(0, "passed", "")

    _update(1, "running", "")
    try:
        rb_info = run_host_agent(
            pinned_host,
            "rollback",
            {
                "env": env,
                "port": local_port,
                "api_domain": api_domain,
                "protected_paths": _protected_paths(env),
            },
            timeout_sec=120,
        )
    except Exception as exc:
        return _fail(1, str(exc).strip()[:120] or "TARGET_ROLLBACK_FAILED", None)
    _update(1, "passed", "")

    _update(2, "running", "")
    try:
        smoke_fn = public_smoke_fn or public_smoke.run_post_publish_https_smoke
        if not smoke_fn(gateway_url, tenant_id).get("passed"):
            raise ValueError("POST_ROLLBACK_PUBLIC_SMOKE_FAILED")
        after_probe = probe_customer_release_state(pinned_host, env, api_domain, catalog)
        _persist_release_state(request, env, after_probe)
    except Exception as exc:
        return _fail(2, str(exc).strip()[:120] or "POST_ROLLBACK_PUBLIC_SMOKE_FAILED", None)
    _update(2, "passed", "")

    return request(
        {
            "wizard_job": "complete",
            "job_id": job_id,
            "passed": True,
            "steps": steps,
            "result": {
                "from_version": before_probe["current_version"],
                "to_version": after_probe["current_version"],
                "runtime_mode": "docker",
                "nginx_changed": bool(rb_info.get("nginx_restored")),
                "untouched_config_verified": True,
                "smoke_passed": True,
                "business_mutations": after_probe["business_mutations"] or "disabled",
                "business_accuracy": "unconfirmed",
            },
        }
    )


def run_oci_cold_install_and_onboard(
    root: Path,
    central_target: str,
    request: Callable[[dict[str, Any]], dict[str, Any]],
    job_req: dict[str, Any],
    *,
    pinned_host: Any,
    read_central_fn: Callable[..., bytes],
) -> dict[str, Any]:
    """全新客户机容器原生冷装（拉取最新官方 OCI @sha256，不再传 24 MiB 整包）。"""
    env = str(job_req["environment"])
    local_port = int(job_req.get("local_port") or 8788)
    revision = subprocess.check_output(["git", "rev-parse", "origin/main"], cwd=str(root), text=True).strip()

    probe = run_host_agent(
        pinned_host,
        "probe",
        {"env": env, "api_domain": str(job_req.get("api_domain") or "")},
        timeout_sec=30,
    )
    if not probe.get("docker_available"):
        raise ValueError("ENVIRONMENT_DOCKER_REQUIRED")

    releases = release_governance.collect_git_annotated_releases(root, limit=30)
    target_rel = next((r for r in releases if r.get("revision") == revision), None) or (
        releases[0] if releases else None
    )
    if target_rel is None:
        raise ValueError("ENVIRONMENT_RELEASE_IMAGE_NOT_PUBLISHED")
    gh_imgs = container_registry.discover_release_images_from_github(root, [target_rel])
    image_desc = gh_imgs.get(str(target_rel["revision"]))
    if not image_desc:
        raise ValueError("ENVIRONMENT_RELEASE_IMAGE_NOT_PUBLISHED")
    image_ref = str(image_desc["image_ref"])
    image_digest = str(image_desc["image_digest"])
    container_registry.validate_image_ref(image_ref)
    registry_cfg = _read_registry_config_from_central(read_central_fn, root, central_target)

    bundle = request(
        {
            "wizard_job": "prepare_bundle",
            "environment": env,
            "api_domain": job_req["api_domain"],
            "tenant_id": job_req.get("tenant_id", ""),
            "tenant_name": job_req.get("tenant_name", ""),
            "customer_name": job_req.get("customer_name", env),
            "local_port": local_port,
            "actor": job_req.get("actor", "wizard-worker"),
        }
    )
    if not isinstance(bundle, dict) or not bundle.get("ok"):
        raise ValueError("CONTROL_PLANE_PREPARE_BUNDLE_FAILED")

    stage_remote = f"/var/backups/aiops-wizard-stage-{env}"
    with tempfile.TemporaryDirectory(prefix="aiops-oci-cold-") as tmpdir:
        tmp = Path(tmpdir)
        tmp.chmod(0o700)
        for name, remote_path, expected_sha in (
            ("template.env", str(bundle["config_path"]), str(bundle.get("config_sha256") or "")),
            (
                "runtime-bundle.tar",
                str(bundle["runtime_bundle_path"]),
                str(bundle.get("runtime_bundle_sha256") or ""),
            ),
            (
                "nginx-user-ingress-auth.conf",
                str(bundle["nginx_auth_path"]),
                str(bundle.get("nginx_auth_sha256") or ""),
            ),
        ):
            raw_bytes = read_central_fn(central_target, remote_path, cwd=root)
            if expected_sha and hashlib.sha256(raw_bytes).hexdigest() != expected_sha:
                raise ValueError(f"BUNDLE_FILE_SHA_MISMATCH:{name}")
            out = tmp / name
            out.write_bytes(raw_bytes)
            out.chmod(0o600)

        q_stage = shlex.quote(stage_remote)
        pinned_host.run_cmd(f"rm -rf {q_stage} && mkdir -p {q_stage} && chmod 700 {q_stage}")
        for fname in ("template.env", "runtime-bundle.tar", "nginx-user-ingress-auth.conf"):
            pinned_host.copy_file(tmp / fname, f"{stage_remote}/{fname}")

        ensure_customer_docker_auth(pinned_host, env, registry_cfg, tmp)
        unit_file = tmp / f"aiops-access-{env}.service"
        unit_file.write_text(container_registry.render_container_systemd_unit(env), encoding="utf-8")
        unit_file.chmod(0o644)
        pinned_host.copy_file(unit_file, f"{stage_remote}/aiops-access-{env}.service")

    run_host_agent(
        pinned_host,
        "cold-install",
        {
            "env": env,
            "port": local_port,
            "image_ref": image_ref,
            "image_digest": image_digest,
            "revision": str(target_rel.get("revision") or revision),
            "tag": str(target_rel.get("tag") or ""),
            "stage_dir": stage_remote,
        },
        timeout_sec=300,
    )

    return {
        "ok": True,
        "runtime_mode": "docker",
        "image_digest": image_digest,
        "consumer_kb_id": str(bundle.get("consumer_kb_id") or ""),
        "operator_kb_id": str(bundle.get("operator_kb_id") or ""),
        "workspace_id": str(bundle.get("workspace_id") or ""),
        "consumer_app_id": str(bundle.get("consumer_app_id") or ""),
        "operator_app_id": str(bundle.get("operator_app_id") or ""),
        "tenant_id": str(bundle.get("tenant_id") or ""),
        "tenant_name": str(bundle.get("tenant_name") or ""),
    }
