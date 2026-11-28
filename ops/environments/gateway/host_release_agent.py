"""客户机 Gateway OCI 容器宿主机执行器（纯 Python 标准库，无三方依赖）。

设计目标（ADR-0363 收口版）：
- 取代控制面里用字符串拼接远程 Python/Bash 的写法：本地 IDE、Ruff 与单元测试均可直接检查本文件；
- 由控制面经受钉扎 SSH 的 stdin 投递到目标机 `/usr/bin/python3 - <subcmd> <json>` 执行，
  客户机无需预装 AI-Ops 包；
- 仅支持 OCI 不可变容器镜像模式（`@sha256:...`），不保留历史跨海 venv 分支。
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

OPT_BASE = Path("/opt")
ETC_BASE = Path("/etc")
VAR_BASE = Path("/var/lib")
SYSTEMD_DIR = Path("/etc/systemd/system")
NGINX_VHOST_EXT_BASE = Path("/www/server/panel/vhost/nginx/extension")
NGINX_DEFAULT_BIN = "/www/server/nginx/sbin/nginx"


def _opt_root(env: str) -> Path:
    return OPT_BASE / f"aiops-access-{env}"


def _etc_root(env: str) -> Path:
    return ETC_BASE / f"aiops-access-{env}"


def _var_root(env: str) -> Path:
    return VAR_BASE / f"aiops-access-{env}"


def _systemd_unit(env: str) -> Path:
    return SYSTEMD_DIR / f"aiops-access-{env}.service"


def _nginx_bin() -> str:
    return shutil.which("nginx") or NGINX_DEFAULT_BIN


def _docker_env(env: str) -> dict[str, str]:
    d_env = dict(os.environ)
    dcfg = _etc_root(env) / "docker-config"
    if (dcfg / "config.json").is_file():
        d_env["DOCKER_CONFIG"] = str(dcfg)
    return d_env


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_atomic(path: Path, content: str | bytes, mode: int) -> None:
    tmp = path.with_name(f".{path.name}.next")
    if isinstance(content, bytes):
        tmp.write_bytes(content)
    else:
        tmp.write_text(content, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _hash_files(paths: list[str]) -> dict[str, str]:
    return {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths if Path(p).is_file()}


def _verify_hashes(pre_hashes: dict[str, str]) -> None:
    for path_str, expected in pre_hashes.items():
        path = Path(path_str)
        if not path.is_file():
            raise RuntimeError(f"CONFIG_MISSING:{path_str}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError(f"CONFIG_MODIFIED:{path_str}")


def _poll_health(port: int, target_ver: str | None = None, attempts: int = 30) -> dict[str, Any]:
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                ver_ok = target_ver is None or body.get("version") == target_ver
                if body.get("ok") is True and ver_ok and body.get("business_mutations") == "disabled":
                    return body
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("HEALTH_VERIFY_FAILED")


def _render_container_env(env: str, image_ref: str, port: int, uid: int, gid: int) -> str:
    return "\n".join(
        [
            f"ENV_NAME={env}",
            f"IMAGE_REF={image_ref}",
            f"LOCAL_PORT={port}",
            f"RUN_UID={uid}",
            f"RUN_GID={gid}",
            "",
        ]
    )


def _restore_nginx_backup(bak_dir: Path | None, env: str, api_domain: str) -> bool:
    if not bak_dir or not bak_dir.is_dir() or not api_domain:
        return False
    ext_dir = NGINX_VHOST_EXT_BASE / api_domain
    restored = False
    for src_name, dst in (
        ("aiops-user-ingress.conf", ext_dir / "aiops-user-ingress.conf"),
        ("environment-user-ingress-proxy.inc", _etc_root(env) / "environment-user-ingress-proxy.inc"),
    ):
        src = bak_dir / src_name
        if src.is_file():
            shutil.copy2(src, dst)
            restored = True
    return restored


def _parse_container_env_image(ctr_env_path: Path) -> str:
    if not ctr_env_path.is_file():
        return ""
    for line in ctr_env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("IMAGE_REF="):
            return line.split("=", 1)[1].strip()
    return ""


def cmd_probe(payload: dict[str, Any]) -> dict[str, Any]:
    """只读探测客户机容器运行态、/health、Nginx 入口文件与可回滚版本。"""
    env = str(payload["env"])
    api_domain = str(payload.get("api_domain") or "")
    root = _opt_root(env)
    etc = _etc_root(env)
    inst = _read_json(root / "installed.json")
    port = int(inst.get("port") or 8788)
    ctr_env_p = root / "container.env"
    cur_img_ref = _parse_container_env_image(ctr_env_p) or str(inst.get("image_ref") or "")
    runtime_mode = "docker" if (inst.get("runtime_mode") == "docker" or ctr_env_p.is_file()) else "venv"

    docker_bin = shutil.which("docker")
    docker_ok = (
        bool(docker_bin)
        and subprocess.run(
            [docker_bin, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
        ).returncode
        == 0
    )

    health: dict[str, Any] = {}
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
            health = json.loads(resp.read().decode("utf-8"))
    except Exception:
        health = {}

    uconf_p = (NGINX_VHOST_EXT_BASE / api_domain / "aiops-user-ingress.conf") if api_domain else None
    pinc_p = etc / "environment-user-ingress-proxy.inc"

    prev = inst.get("previous_release")
    prev_out = None
    if isinstance(prev, dict) and prev.get("runtime_mode") == "docker" and prev.get("image_ref"):
        prev_out = prev

    cur_digest = str(inst.get("image_digest") or (cur_img_ref.split("@", 1)[1] if "@" in cur_img_ref else ""))
    return {
        "port": port,
        "runtime_mode": runtime_mode,
        "docker_available": docker_ok,
        "current_image_ref": cur_img_ref,
        "current_image_digest": cur_digest,
        "current_release_name": cur_digest[:19] if cur_digest else "",
        "installed_version": str(inst.get("version") or ""),
        "installed_revision": str(inst.get("revision") or ""),
        "python_minor": str(inst.get("python") or "3.12") or "3.12",
        "health": health,
        "user_conf": uconf_p.read_text(encoding="utf-8") if uconf_p and uconf_p.is_file() else "",
        "proxy_inc": pinc_p.read_text(encoding="utf-8") if pinc_p.is_file() else "",
        "previous_release": prev_out,
    }


def cmd_pull_verify(payload: dict[str, Any]) -> dict[str, Any]:
    """拉取不可变 @sha256 镜像、非 root 只读校验 __version__，并采集受保护配置哈希。"""
    env = str(payload["env"])
    image_ref = str(payload["image_ref"])
    target_ver = str(payload["target_ver"])
    protected = list(payload.get("protected_paths") or [])

    pre_hashes = _hash_files(protected)
    pw = pwd.getpwnam(f"aiops-access-{env}")
    d_env = _docker_env(env)

    subprocess.run(["docker", "pull", image_ref], env=d_env, check=True)
    smoke_ver = subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=32m",
            "--user",
            f"{pw.pw_uid}:{pw.pw_gid}",
            "--security-opt",
            "no-new-privileges:true",
            "--cap-drop",
            "ALL",
            "--entrypoint",
            "/opt/aiops/venv/bin/python",
            image_ref,
            "-c",
            "import aiops_diagnostics; print(aiops_diagnostics.__version__)",
        ],
        env=d_env,
        text=True,
    ).strip()
    if smoke_ver != target_ver:
        raise RuntimeError(f"IMAGE_VERSION_MISMATCH:{smoke_ver}")

    return {
        "pre_hashes": pre_hashes,
        "old_image_ref": _parse_container_env_image(_opt_root(env) / "container.env"),
        "run_uid": pw.pw_uid,
        "run_gid": pw.pw_gid,
    }


def cmd_sync_nginx(payload: dict[str, Any]) -> dict[str, Any]:
    """按需更新受控 Nginx 用户入口文件并执行 `nginx -t` 守卫（失败自动回退备份）。"""
    env = str(payload["env"])
    api_domain = str(payload.get("api_domain") or "")
    stage = Path(str(payload["stage_dir"]))
    bak = Path(str(payload["bak_dir"]))
    nginx_bin = _nginx_bin()

    if not api_domain:
        return {"nginx_changed": False}

    ext_dir = NGINX_VHOST_EXT_BASE / api_domain
    uconf_dst = ext_dir / "aiops-user-ingress.conf"
    pinc_dst = _etc_root(env) / "environment-user-ingress-proxy.inc"
    uconf_new = (stage / "aiops-user-ingress.conf").read_bytes()
    pinc_new = (stage / "environment-user-ingress-proxy.inc").read_bytes()
    uconf_cur = uconf_dst.read_bytes() if uconf_dst.is_file() else b""
    pinc_cur = pinc_dst.read_bytes() if pinc_dst.is_file() else b""

    changed = (uconf_new != uconf_cur) or (pinc_new != pinc_cur)
    if not changed:
        subprocess.run([nginx_bin, "-t"], check=True, capture_output=True, text=True)
        return {"nginx_changed": False}

    ext_dir.mkdir(parents=True, exist_ok=True)
    bak.mkdir(parents=True, exist_ok=True)
    os.chmod(bak, 0o700)
    if uconf_dst.is_file():
        shutil.copy2(uconf_dst, bak / "aiops-user-ingress.conf")
    if pinc_dst.is_file():
        shutil.copy2(pinc_dst, bak / "environment-user-ingress-proxy.inc")

    _write_atomic(uconf_dst, uconf_new, 0o644)
    _write_atomic(pinc_dst, pinc_new, 0o644)
    chk = subprocess.run([nginx_bin, "-t"], capture_output=True, text=True)
    if chk.returncode != 0:
        _restore_nginx_backup(bak, env, api_domain)
        raise RuntimeError("NGINX_TEST_FAILED")
    return {"nginx_changed": True}


def cmd_switch(payload: dict[str, Any]) -> dict[str, Any]:
    """原子切换 `container.env` 与直跑 systemd unit、重启核验 /health，失败自动回退。"""
    env = str(payload["env"])
    port = int(payload["port"])
    bak_dir = Path(str(payload["bak_dir"]))
    stage = Path(str(payload["stage_dir"]))
    api_domain = str(payload.get("api_domain") or "")
    target_ver = str(payload["target_ver"])
    target_rev = str(payload["target_rev"])
    target_tag = str(payload["target_tag"])
    image_ref = str(payload["image_ref"])
    image_digest = str(payload["image_digest"])
    uid = int(payload["run_uid"])
    gid = int(payload["run_gid"])
    nginx_changed = bool(payload.get("nginx_changed"))
    pre_hashes = dict(payload.get("pre_hashes") or {})

    root = _opt_root(env)
    svc = f"aiops-access-{env}.service"
    unit_p = _systemd_unit(env)
    inst_f = root / "installed.json"
    ctr_env_p = root / "container.env"
    old_inst = _read_json(inst_f)
    old_img_ref = str(payload.get("old_image_ref") or old_inst.get("image_ref") or "")

    bak_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(bak_dir, 0o700)
    unit_bak = bak_dir / f"{svc}.bak"
    if unit_p.is_file():
        shutil.copy2(unit_p, unit_bak)
    ctr_bak = bak_dir / "container.env.bak"
    if ctr_env_p.is_file():
        shutil.copy2(ctr_env_p, ctr_bak)

    nginx_bin = _nginx_bin()

    def _auto_rollback() -> None:
        if ctr_bak.is_file():
            shutil.copy2(ctr_bak, ctr_env_p)
        if unit_bak.is_file():
            shutil.copy2(unit_bak, unit_p)
            subprocess.run(["systemctl", "daemon-reload"], check=False)
        if old_inst:
            _write_atomic(inst_f, json.dumps(old_inst, ensure_ascii=False) + "\n", 0o600)
        if nginx_changed and _restore_nginx_backup(bak_dir, env, api_domain):
            subprocess.run([nginx_bin, "-t"], check=False)
            subprocess.run([nginx_bin, "-s", "reload"], check=False)
        subprocess.run(["systemctl", "restart", svc], check=False)

    try:
        ctr_env_txt = _render_container_env(env, image_ref, port, uid, gid)
        _write_atomic(ctr_env_p, ctr_env_txt, 0o644)

        new_unit = (stage / svc).read_bytes()
        cur_unit = unit_p.read_bytes() if unit_p.is_file() else b""
        if new_unit != cur_unit:
            _write_atomic(unit_p, new_unit, 0o644)
            subprocess.run(["systemctl", "daemon-reload"], check=True)

        new_inst = dict(old_inst)
        new_inst.update(
            {
                "runtime_mode": "docker",
                "release": image_digest[:19],
                "version": target_ver,
                "revision": target_rev,
                "tag": target_tag,
                "image_ref": image_ref,
                "image_digest": image_digest,
            }
        )
        if old_inst.get("runtime_mode") == "docker" and old_img_ref:
            new_inst["previous_release"] = {
                "runtime_mode": "docker",
                "version": str(old_inst.get("version") or ""),
                "revision": str(old_inst.get("revision") or ""),
                "image_ref": old_img_ref,
                "image_digest": str(old_inst.get("image_digest") or ""),
                "nginx_backup_dir": str(bak_dir) if nginx_changed else "",
                "recorded_at": time.time(),
            }

        _write_atomic(inst_f, json.dumps(new_inst, ensure_ascii=False, indent=2) + "\n", 0o600)
        subprocess.run(["systemctl", "restart", svc], check=True)
        health = _poll_health(port, target_ver=target_ver)
        if nginx_changed:
            subprocess.run([nginx_bin, "-s", "reload"], check=True)
        _verify_hashes(pre_hashes)
        # 切换与核验全绿后再清理旧版 run-container.sh，确保自动回滚时旧 systemd unit 仍可用
        (root / "run-container.sh").unlink(missing_ok=True)
        shutil.rmtree(stage, ignore_errors=True)
        return {"ok": True, "untouched_config_verified": True, "health": health}
    except Exception as exc:
        _auto_rollback()
        shutil.rmtree(stage, ignore_errors=True)
        raise SystemExit(f"AUTO_ROLLED_BACK:{exc}") from exc


def cmd_rollback(payload: dict[str, Any]) -> dict[str, Any]:
    """一键安全回滚至上一已验证 OCI 容器镜像摘要并恢复 Nginx 备份。"""
    env = str(payload["env"])
    port = int(payload["port"])
    api_domain = str(payload.get("api_domain") or "")
    protected = list(payload.get("protected_paths") or [])

    root = _opt_root(env)
    svc = f"aiops-access-{env}.service"
    inst_f = root / "installed.json"
    inst = _read_json(inst_f)
    prev = inst.get("previous_release")
    if not isinstance(prev, dict) or prev.get("runtime_mode") != "docker":
        raise RuntimeError("PREVIOUS_RELEASE_MISSING")

    prev_img = str(prev.get("image_ref") or "").strip()
    target_ver = str(prev.get("version") or "").strip()
    if not prev_img or not target_ver:
        raise RuntimeError("PREVIOUS_RELEASE_MISSING")

    d_env = _docker_env(env)
    insp = subprocess.run(["docker", "image", "inspect", prev_img], env=d_env, capture_output=True)
    if insp.returncode != 0:
        subprocess.run(["docker", "pull", prev_img], env=d_env, check=True)

    pre_hashes = _hash_files(protected)
    pw = pwd.getpwnam(f"aiops-access-{env}")
    _write_atomic(
        root / "container.env", _render_container_env(env, prev_img, port, pw.pw_uid, pw.pw_gid), 0o644
    )

    bak_raw = str(prev.get("nginx_backup_dir") or "")
    ngx_restored = _restore_nginx_backup(Path(bak_raw) if bak_raw else None, env, api_domain)
    nginx_bin = _nginx_bin()
    if ngx_restored:
        subprocess.run([nginx_bin, "-t"], check=True)

    prev_digest = str(prev.get("image_digest") or (prev_img.split("@", 1)[1] if "@" in prev_img else ""))
    new_inst = dict(inst)
    new_inst.update(
        {
            "runtime_mode": "docker",
            "release": prev_digest[:19],
            "image_ref": prev_img,
            "image_digest": prev_digest,
            "version": target_ver,
            "revision": str(prev.get("revision") or ""),
            "previous_release": None,
        }
    )
    _write_atomic(inst_f, json.dumps(new_inst, ensure_ascii=False, indent=2) + "\n", 0o600)
    subprocess.run(["systemctl", "restart", svc], check=True)
    _poll_health(port, target_ver=target_ver)
    if ngx_restored:
        subprocess.run([nginx_bin, "-s", "reload"], check=True)
    _verify_hashes(pre_hashes)
    return {
        "ok": True,
        "to_version": target_ver,
        "runtime_mode": "docker",
        "nginx_restored": ngx_restored,
        "untouched_config_verified": True,
    }


def cmd_cold_install(payload: dict[str, Any]) -> dict[str, Any]:
    """全新客户机容器原生冷装（拉取 @sha256、初始化只读权限目录、容器内种库并启动服务）。"""
    env = str(payload["env"])
    port = int(payload["port"])
    image_ref = str(payload["image_ref"])
    image_digest = str(payload["image_digest"])
    rev = str(payload.get("revision") or "")
    tag = str(payload.get("tag") or "")
    stage = Path(str(payload["stage_dir"]))

    svc_user = f"aiops-access-{env}"
    if subprocess.run(["id", "-u", svc_user], capture_output=True).returncode != 0:
        subprocess.run(
            ["useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", svc_user],
            check=True,
        )
    pw = pwd.getpwnam(svc_user)
    uid, gid = pw.pw_uid, pw.pw_gid
    d_env = _docker_env(env)

    subprocess.run(["docker", "pull", image_ref], env=d_env, check=True)
    ver = subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=32m",
            "--user",
            f"{uid}:{gid}",
            "--security-opt",
            "no-new-privileges:true",
            "--cap-drop",
            "ALL",
            "--entrypoint",
            "/opt/aiops/venv/bin/python",
            image_ref,
            "-c",
            "import aiops_diagnostics; print(aiops_diagnostics.__version__)",
        ],
        env=d_env,
        text=True,
    ).strip()

    cfg_root = _etc_root(env)
    var_root = _var_root(env)
    opt_root = _opt_root(env)
    for d in (cfg_root / "runtime", cfg_root / "registry", var_root / "keys", var_root / "gateway", opt_root):
        d.mkdir(parents=True, exist_ok=True)
    os.chmod(cfg_root, 0o755)
    os.chmod(opt_root, 0o755)

    subprocess.run(["tar", "-xf", str(stage / "runtime-bundle.tar"), "-C", str(cfg_root)], check=True)
    dify_keys = cfg_root / "dify-keys"
    if dify_keys.is_dir():
        for item in dify_keys.iterdir():
            shutil.move(str(item), str(var_root / "keys" / item.name))
        dify_keys.rmdir()

    _write_atomic(
        cfg_root / "nginx-user-ingress-auth.conf",
        (stage / "nginx-user-ingress-auth.conf").read_bytes(),
        0o600,
    )
    _write_atomic(cfg_root / "production.env", (stage / "template.env").read_bytes(), 0o600)
    os.chown(cfg_root / "production.env", uid, gid)

    for private_dir in (
        cfg_root / "runtime",
        cfg_root / "registry",
        var_root,
        var_root / "keys",
        var_root / "gateway",
    ):
        os.chmod(private_dir, 0o700)
        os.chown(private_dir, uid, gid)
        for child in private_dir.rglob("*"):
            os.chown(child, uid, gid)
            os.chmod(child, 0o700 if child.is_dir() else 0o600)

    seed_code = (
        "import json; "
        "from aiops_diagnostics.onboarding.wizard_provision import seed_target_gateway_db; "
        f"seed_target_gateway_db('{var_root}/gateway/gateway.db', "
        f"json.loads(open('{cfg_root}/runtime/gateway-seed.json').read()))"
    )
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=32m",
            "--user",
            f"{uid}:{gid}",
            "--security-opt",
            "no-new-privileges:true",
            "--cap-drop",
            "ALL",
            "-v",
            f"{cfg_root}/runtime:{cfg_root}/runtime:ro",
            "-v",
            f"{var_root}:{var_root}:rw",
            "--entrypoint",
            "/opt/aiops/venv/bin/python",
            image_ref,
            "-c",
            seed_code,
        ],
        env=d_env,
        check=True,
    )
    for child in (var_root / "gateway").rglob("*"):
        os.chown(child, uid, gid)

    svc = f"aiops-access-{env}.service"
    _write_atomic(opt_root / "container.env", _render_container_env(env, image_ref, port, uid, gid), 0o644)
    _write_atomic(_systemd_unit(env), (stage / svc).read_bytes(), 0o644)
    _write_atomic(
        opt_root / "installed.json",
        json.dumps(
            {
                "schema": 1,
                "runtime_mode": "docker",
                "environment": env,
                "port": port,
                "user": svc_user,
                "version": ver,
                "revision": rev,
                "tag": tag,
                "image_ref": image_ref,
                "image_digest": image_digest,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        0o600,
    )
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", svc], check=False, capture_output=True)
    subprocess.run(["systemctl", "restart", svc], check=True)
    health = _poll_health(port, target_ver=ver)
    shutil.rmtree(stage, ignore_errors=True)
    return {"ok": True, "version": ver, "health": health}


_COMMANDS = {
    "probe": cmd_probe,
    "pull-verify": cmd_pull_verify,
    "sync-nginx": cmd_sync_nginx,
    "switch": cmd_switch,
    "rollback": cmd_rollback,
    "cold-install": cmd_cold_install,
}


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 2 or args[0] not in _COMMANDS:
        raise SystemExit(f"用法: host_release_agent.py <{'|'.join(_COMMANDS)}> <json_payload>")
    payload = json.loads(args[1])
    out = _COMMANDS[args[0]](payload)
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
