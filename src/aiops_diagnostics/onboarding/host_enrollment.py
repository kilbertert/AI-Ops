"""动态客户主机受管清单与双模式一次性安全引导（ADR-0357 P1-1）。

核心安全不变量：
1. 平台核心主机（41/36）仍由仓库 ``.dev-host.toml`` 静态声明治理；客户业务主机纳入控制面
   ``0600`` 动态受管清单（``customer-hosts.json``），接入新客户无需修改 Git 仓库提 PR。
2. **模式 A（一次性口令内存引导）**：支持操作者在页面提供一次性 SSH 登录密码，后端仅在
   内存中瞬时使用以完成主机指纹钉扎、下发专属 Ed25519 公钥、创建锁定非登录账号 ``aiops``
   及补齐 ``python3.X-venv``，并验证纯密钥免密连通，随后立即丢弃口令；严禁落盘、入库、
   写日志或回显至任何返回体或异常。
3. **模式 B（客户侧零密码单行引导脚本）**：生成 256-bit 单次签名令牌（仅存 SHA-256 摘要，
   TTL <= 900s，原子消费即失效），渲染可审计的客户侧自举脚本；客户本机执行后回传非秘密
   主机指纹与环境事实，防重放、防篡改、不移交 root 密码。
4. 后续任何远端动作均必须断言已钉扎的 ``host_fingerprint_sha256``，不匹配即 fail-closed 拒绝。
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import os
import pty
import re
import secrets
import select
import shlex
import socket
import sqlite3
import subprocess
import tempfile
import termios
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiops_diagnostics.common.private_files import (
    ensure_private_directory,
    protect_private_file,
    validate_private_file,
    write_private_text,
)
from aiops_diagnostics.gateway import gateway_db
from aiops_diagnostics.onboarding import drafts, registry

HOST_REGISTRY_SCHEMA = "aiops-customer-hosts/1"
BOOTSTRAP_TOKEN_TTL_SECONDS = 900
DEFAULT_SSH_PORT = 22
DEFAULT_SSH_USER = "root"
DEFAULT_SERVICE_USER = "aiops"

_ENV_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,30}[a-z0-9])?$")
_HOSTNAME_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
_SSH_USER_PATTERN = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_FINGERPRINT_PATTERN = re.compile(r"^SHA256:[A-Za-z0-9+/=]{32,64}$")
_TOKEN_PATTERN = re.compile(r"^bs_[0-9a-f]{64}$")
_PUBKEY_PATTERN = re.compile(r"^ssh-ed25519 [A-Za-z0-9+/=]{32,128}(?: [A-Za-z0-9._@:-]{1,64})?$")
_PYTHON_MINOR_PATTERN = re.compile(r"^3\.\d{1,2}$")
_SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class EnrollmentRefused(ValueError):
    """主机引导或登记被安全边界拒绝时的稳定错误码异常。"""


def _validate_environment(environment: str) -> str:
    if not isinstance(environment, str) or not _ENV_PATTERN.fullmatch(environment):
        raise EnrollmentRefused("ENVIRONMENT_HOST_INVALID_ENVIRONMENT")
    return environment


def _validate_address(address: str) -> str:
    if not isinstance(address, str):
        raise EnrollmentRefused("ENVIRONMENT_HOST_INVALID_ADDRESS")
    raw = address.strip()
    if not raw or any(ch.isspace() for ch in raw):
        raise EnrollmentRefused("ENVIRONMENT_HOST_INVALID_ADDRESS")
    try:
        ipaddress.ip_address(raw)
        return raw
    except ValueError:
        pass
    if not _HOSTNAME_PATTERN.fullmatch(raw):
        raise EnrollmentRefused("ENVIRONMENT_HOST_INVALID_ADDRESS")
    return raw


def _validate_ssh_port(port: Any) -> int:
    if type(port) is not int or not (1 <= port <= 65535):
        raise EnrollmentRefused("ENVIRONMENT_HOST_INVALID_PORT")
    return port


def _validate_ssh_user(user: str) -> str:
    if not isinstance(user, str) or not _SSH_USER_PATTERN.fullmatch(user):
        raise EnrollmentRefused("ENVIRONMENT_HOST_INVALID_SSH_USER")
    return user


def _validate_callback_base_url(url: str) -> str:
    if not isinstance(url, str):
        raise EnrollmentRefused("ENVIRONMENT_HOST_CALLBACK_URL_INVALID")
    raw = url.strip().rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise EnrollmentRefused("ENVIRONMENT_HOST_CALLBACK_URL_INVALID")
    if parsed.query or parsed.fragment:
        raise EnrollmentRefused("ENVIRONMENT_HOST_CALLBACK_URL_INVALID")
    return raw


def _hosts_file(registry_path: str | Path) -> Path:
    return Path(registry_path).parent / "customer-hosts.json"


def _keys_dir(registry_path: str | Path) -> Path:
    return Path(registry_path).parent / "customer-host-keys"


def _initialize_bootstrap_tokens(conn: sqlite3.Connection) -> None:
    drafts.initialize_bootstrap_tokens(conn)


def _load_hosts_unlocked(registry_path: str | Path) -> dict[str, Any]:
    path = _hosts_file(registry_path)
    if not path.exists():
        return {"schema": HOST_REGISTRY_SCHEMA, "revision": 0, "hosts": {}}
    validate_private_file(path)
    body = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(body, dict)
        or body.get("schema") != HOST_REGISTRY_SCHEMA
        or not isinstance(body.get("hosts"), dict)
    ):
        raise EnrollmentRefused("ENVIRONMENT_HOST_REGISTRY_CORRUPT")
    return body


def _save_hosts_unlocked(registry_path: str | Path, body: dict[str, Any]) -> None:
    path = _hosts_file(registry_path)
    ensure_private_directory(path.parent)
    body["schema"] = HOST_REGISTRY_SCHEMA
    body["revision"] = int(body.get("revision") or 0) + 1
    write_private_text(path, json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _ensure_ed25519_keypair(
    registry_path: str | Path,
    environment: str,
    *,
    keygen_runner: Callable[[Path, str], tuple[str, str]] | None = None,
) -> tuple[str, str, Path]:
    """为指定客户环境准备独立 Ed25519 密钥对（私钥 0600 仅留服务端，公钥用于下发）。"""
    env = _validate_environment(environment)
    kdir = _keys_dir(registry_path)
    ensure_private_directory(kdir)
    key_id = f"ed25519-{env}"
    priv_path = kdir / f"id_{key_id}"
    pub_path = kdir / f"id_{key_id}.pub"
    if priv_path.exists() and pub_path.exists():
        validate_private_file(priv_path)
        pub = pub_path.read_text(encoding="utf-8").strip()
        if _PUBKEY_PATTERN.fullmatch(pub):
            return key_id, pub, priv_path
    comment = f"aiops-onboarding-{env}"
    if keygen_runner is not None:
        priv_body, pub_body = keygen_runner(priv_path, comment)
        write_private_text(priv_path, priv_body)
        write_private_text(pub_path, pub_body.strip() + "\n")
        pub = pub_body.strip()
    else:
        if priv_path.exists():
            priv_path.unlink()
        if pub_path.exists():
            pub_path.unlink()
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(priv_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        protect_private_file(priv_path)
        protect_private_file(pub_path)
        pub = pub_path.read_text(encoding="utf-8").strip()
    if not _PUBKEY_PATTERN.fullmatch(pub):
        raise EnrollmentRefused("ENVIRONMENT_HOST_KEYGEN_INVALID")
    return key_id, pub, priv_path


def _public_host_view(entry: dict[str, Any]) -> dict[str, Any]:
    """仅返回可展示给管理页的非秘密主机状态视图（不含私钥路径、密码或令牌）。"""
    return {
        "environment": str(entry["environment"]),
        "host_alias": str(entry["host_alias"]),
        "address": str(entry["address"]),
        "ssh_port": int(entry["ssh_port"]),
        "ssh_user": str(entry["ssh_user"]),
        "key_id": str(entry["key_id"]),
        "host_fingerprint_sha256": str(entry.get("host_fingerprint_sha256") or ""),
        "enrollment_mode": str(entry.get("enrollment_mode") or ""),
        "status": str(entry.get("status") or "pending_script"),
        "hostname": str(entry.get("hostname") or ""),
        "machine_id_sha256": str(entry.get("machine_id_sha256") or ""),
        "python_minor": str(entry.get("python_minor") or ""),
        "service_user_present": bool(entry.get("service_user_present", False)),
        "venv_ready": bool(entry.get("venv_ready", False)),
        "nginx_flavor": str(entry.get("nginx_flavor") or ""),
        "enrolled_at": entry.get("enrolled_at"),
        "enrolled_by": str(entry.get("enrolled_by") or ""),
        "rotate_root_password_recommended": bool(entry.get("rotate_root_password_recommended", False)),
    }


def _authorize(registry_path: str | Path, identity: tuple[str, str, str], environment: str) -> None:
    effective = registry.load(registry_path)
    try:
        allowed = drafts.access(effective, identity)
    except registry.RegistryError as exc:
        raise EnrollmentRefused("ENVIRONMENT_ACCESS_DENIED") from exc
    if "*" not in allowed and environment not in allowed:
        raise EnrollmentRefused("ENVIRONMENT_ACCESS_DENIED")


def get_host_status(
    registry_path: str | Path,
    identity: tuple[str, str, str],
    environment: str,
) -> dict[str, Any]:
    env = _validate_environment(environment)
    with drafts._locked(registry_path):
        _authorize(registry_path, identity, env)
        body = _load_hosts_unlocked(registry_path)
        entry = body["hosts"].get(env)
        if entry is None:
            return {"environment": env, "enrolled": False, "host": None}
        return {
            "environment": env,
            "enrolled": entry.get("status") in {"enrolled", "verified"},
            "host": _public_host_view(entry),
        }


def revoke_host(
    registry_path: str | Path,
    identity: tuple[str, str, str],
    environment: str,
) -> dict[str, Any]:
    env = _validate_environment(environment)
    with drafts._locked(registry_path):
        _authorize(registry_path, identity, env)
        body = _load_hosts_unlocked(registry_path)
        entry = body["hosts"].get(env)
        if entry is None:
            raise EnrollmentRefused("ENVIRONMENT_HOST_NOT_FOUND")
        entry["status"] = "revoked"
        entry["revoked_at"] = time.time()
        _save_hosts_unlocked(registry_path, body)
        return {"environment": env, "enrolled": False, "host": _public_host_view(entry)}


_BOOTSTRAP_JSON_MARKER = "AIOPS_BOOTSTRAP_JSON:"


def _run_ssh_with_ephemeral_pty_password(
    ssh_cmd: list[str],
    password: str,
    *,
    timeout: float = 120.0,
) -> tuple[int, str]:
    """仅用 Python 标准库 pty 在内存中向 ssh 递交一次性口令（零 sshpass 依赖、零磁盘残留）。"""
    master_fd, slave_fd = pty.openpty()
    proc: subprocess.Popen[bytes] | None = None
    chunks: list[bytes] = []
    sent_count = 0
    try:

        def _attach_ctty() -> None:
            os.setsid()
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

        proc = subprocess.Popen(
            ssh_cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=_attach_ctty,
            close_fds=True,
        )
        os.close(slave_fd)
        slave_fd = -1
        deadline = time.monotonic() + timeout
        tail_buf = ""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                raise EnrollmentRefused("ENVIRONMENT_HOST_BOOTSTRAP_TIMEOUT")
            ready, _, _ = select.select([master_fd], [], [], min(0.5, remaining))
            if ready:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data)
                text_part = data.decode("utf-8", errors="replace")
                tail_buf = (tail_buf + text_part)[-512:]
                lowered = tail_buf.lower()
                if sent_count == 0 and ("password:" in lowered or "passphrase" in lowered):
                    os.write(master_fd, (password + "\n").encode("utf-8"))
                    sent_count += 1
                    tail_buf = ""
            elif proc.poll() is not None:
                break
        rc = proc.wait(timeout=5)
    finally:
        password = ""
        if slave_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(slave_fd)
        with contextlib.suppress(OSError):
            os.close(master_fd)
        if proc is not None and proc.poll() is None:
            proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=3)
    return int(rc), b"".join(chunks).decode("utf-8", errors="replace")


def _extract_bootstrap_facts(raw_output: str) -> dict[str, Any]:
    for line in reversed(raw_output.splitlines()):
        cleaned = line.strip()
        idx = cleaned.find(_BOOTSTRAP_JSON_MARKER)
        if idx >= 0:
            payload_text = cleaned[idx + len(_BOOTSTRAP_JSON_MARKER) :].strip()
            parsed = json.loads(payload_text)
            if isinstance(parsed, dict):
                return parsed
    raise EnrollmentRefused("ENVIRONMENT_HOST_PASSWORD_AUTH_OR_BOOTSTRAP_FAILED")


def _default_ephemeral_password_runner(
    *,
    address: str,
    ssh_port: int,
    ssh_user: str,
    password: str,
    public_key: str,
    private_key_path: Path,
    environment: str,
) -> dict[str, Any]:
    """使用瞬时内存口令（stdlib pty）完成指纹钉扎、公钥下发、aiops/venv 自举及纯密钥复验。"""
    # 1. 获取并计算主机 SSH ed25519 指纹（失败即停，绝不盲信）
    scan = subprocess.run(
        ["ssh-keyscan", "-p", str(ssh_port), "-t", "ed25519", "-T", "8", address],
        capture_output=True,
        text=True,
        check=False,
    )
    key_lines = [ln.strip() for ln in scan.stdout.splitlines() if ln.strip() and not ln.startswith("#")]
    if scan.returncode != 0 or not key_lines:
        raise EnrollmentRefused("ENVIRONMENT_HOST_UNREACHABLE")
    key_line = key_lines[0]
    fp_proc = subprocess.run(
        ["ssh-keygen", "-lf", "-"],
        input=key_line + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    parts = fp_proc.stdout.strip().split()
    fingerprint = next((p for p in parts if p.startswith("SHA256:")), "")
    if not _FINGERPRINT_PATTERN.fullmatch(fingerprint):
        raise EnrollmentRefused("ENVIRONMENT_HOST_FINGERPRINT_INVALID")

    # 2. 通过一次性临时 known_hosts + 标准库 pty 内存递交口令完成远端引导（无 sshpass 外部依赖）
    with tempfile.TemporaryDirectory(prefix="aiops-enroll-") as tmp:
        os.chmod(tmp, 0o700)
        kh = Path(tmp) / "known_hosts"
        write_private_text(kh, key_line + "\n")
        remote_bootstrap = _build_remote_python_bootstrap(environment, public_key)
        b64_script = base64.b64encode(remote_bootstrap.encode("utf-8")).decode("ascii")
        remote_cmd = (
            "python3 -c 'import base64, sys; exec(base64.b64decode(sys.argv[1]).decode())' "
            f"{shlex.quote(b64_script)}"
        )
        ssh_cmd = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={kh}",
            "-o",
            "PreferredAuthentications=password,keyboard-interactive",
            "-o",
            "PubkeyAuthentication=no",
            "-o",
            "NumberOfPasswordPrompts=1",
            "-o",
            "ConnectTimeout=10",
            "-p",
            str(ssh_port),
            f"{ssh_user}@{address}",
            remote_cmd,
        ]
        rc, pty_out = _run_ssh_with_ephemeral_pty_password(ssh_cmd, password, timeout=120.0)
        password = ""
        if rc != 0:
            raise EnrollmentRefused("ENVIRONMENT_HOST_PASSWORD_AUTH_OR_BOOTSTRAP_FAILED")
        # 3. 立即用纯 Ed25519 私钥复验免密通道（BatchMode=yes + 钉扎 known_hosts）
        verify = subprocess.run(
            [
                "ssh",
                "-i",
                str(private_key_path),
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"UserKnownHostsFile={kh}",
                "-o",
                "ConnectTimeout=10",
                "-p",
                str(ssh_port),
                f"{ssh_user}@{address}",
                "true",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if verify.returncode != 0:
            raise EnrollmentRefused("ENVIRONMENT_HOST_KEY_VERIFY_FAILED")

    facts = _extract_bootstrap_facts(pty_out)
    facts["host_fingerprint_sha256"] = fingerprint
    facts["host_public_key_line"] = key_line
    return facts


def load_enrolled_host_connection(
    registry_path: str | Path,
    environment: str,
) -> dict[str, Any]:
    """读取服务端受控的已钉扎客户主机连接信息（含私钥路径与公钥行，严禁直接返回前端）。"""
    env = _validate_environment(environment)
    with drafts._locked(registry_path):
        body = _load_hosts_unlocked(registry_path)
        entry = body["hosts"].get(env)
    if not isinstance(entry, dict) or entry.get("status") not in {"enrolled", "verified"}:
        raise EnrollmentRefused("ENVIRONMENT_HOST_NOT_ENROLLED")
    key_id = str(entry.get("key_id") or f"ed25519-{env}")
    priv_path = _keys_dir(registry_path) / f"id_{key_id}"
    if not priv_path.is_file():
        raise EnrollmentRefused("ENVIRONMENT_HOST_KEY_MISSING")
    validate_private_file(priv_path)
    return {
        "environment": env,
        "address": str(entry["address"]),
        "ssh_port": int(entry.get("ssh_port") or DEFAULT_SSH_PORT),
        "ssh_user": str(entry.get("ssh_user") or DEFAULT_SSH_USER),
        "host_fingerprint_sha256": str(entry.get("host_fingerprint_sha256") or ""),
        "host_public_key_line": str(entry.get("host_public_key_line") or ""),
        "private_key_path": str(priv_path),
    }


def _resolve_pinned_known_hosts_line(conn_info: dict[str, Any]) -> str:
    """校验并产出与已钉扎 SHA-256 指纹精确匹配的 known_hosts 公钥行，防中间人漂移。"""
    expected_fp = str(conn_info.get("host_fingerprint_sha256") or "")
    if not _FINGERPRINT_PATTERN.fullmatch(expected_fp):
        raise EnrollmentRefused("ENVIRONMENT_HOST_FINGERPRINT_INVALID")
    cached_line = str(conn_info.get("host_public_key_line") or "").strip()
    candidate_line = cached_line
    if not candidate_line:
        scan = subprocess.run(
            [
                "ssh-keyscan",
                "-p",
                str(conn_info["ssh_port"]),
                "-t",
                "ed25519",
                "-T",
                "8",
                str(conn_info["address"]),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        lines = [ln.strip() for ln in scan.stdout.splitlines() if ln.strip() and not ln.startswith("#")]
        if scan.returncode != 0 or not lines:
            raise EnrollmentRefused("ENVIRONMENT_HOST_UNREACHABLE")
        candidate_line = lines[0]
    fp_proc = subprocess.run(
        ["ssh-keygen", "-lf", "-"],
        input=candidate_line + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    parts = fp_proc.stdout.strip().split()
    actual_fp = next((p for p in parts if p.startswith("SHA256:")), "")
    if actual_fp != expected_fp:
        raise EnrollmentRefused("ENVIRONMENT_HOST_FINGERPRINT_MISMATCH")
    return candidate_line


def run_pinned_ssh(
    conn_info: dict[str, Any],
    remote_args: list[str],
    *,
    stdin_text: str = "",
    timeout: float = 45.0,
) -> subprocess.CompletedProcess[str]:
    """以 StrictHostKeyChecking=yes + 专属 Ed25519 私钥在已钉扎客户主机执行受控命令。"""
    key_line = _resolve_pinned_known_hosts_line(conn_info)
    with tempfile.TemporaryDirectory(prefix="aiops-ssh-") as tmp:
        os.chmod(tmp, 0o700)
        kh = Path(tmp) / "known_hosts"
        write_private_text(kh, key_line + "\n")
        cmd = [
            "ssh",
            "-i",
            str(conn_info["private_key_path"]),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={kh}",
            "-o",
            "ConnectTimeout=10",
            "-p",
            str(conn_info["ssh_port"]),
            f"{conn_info['ssh_user']}@{conn_info['address']}",
            *remote_args,
        ]
        return subprocess.run(
            cmd,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )


def _alloc_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def open_pinned_ssh_tunnel(
    conn_info: dict[str, Any],
    targets: dict[str, tuple[str, int]],
    *,
    startup_timeout: float = 12.0,
) -> Iterator[dict[str, int]]:
    """建立短生命周期纯密钥钉扎本地端口转发隧道（用于控制面无依赖直检客户 MySQL/Redis/tsdata）。"""
    key_line = _resolve_pinned_known_hosts_line(conn_info)
    local_ports = {name: _alloc_loopback_port() for name in targets}
    with tempfile.TemporaryDirectory(prefix="aiops-tun-") as tmp:
        os.chmod(tmp, 0o700)
        kh = Path(tmp) / "known_hosts"
        write_private_text(kh, key_line + "\n")
        cmd = [
            "ssh",
            "-N",
            "-i",
            str(conn_info["private_key_path"]),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={kh}",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ConnectTimeout=10",
            "-p",
            str(conn_info["ssh_port"]),
        ]
        for name, (rhost, rport) in targets.items():
            cmd.extend(["-L", f"127.0.0.1:{local_ports[name]}:{rhost}:{int(rport)}"])
        cmd.append(f"{conn_info['ssh_user']}@{conn_info['address']}")
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + startup_timeout
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise EnrollmentRefused("ENVIRONMENT_HOST_TUNNEL_FAILED")
                all_ready = True
                for lport in local_ports.values():
                    try:
                        with socket.create_connection(("127.0.0.1", lport), timeout=0.3):
                            pass
                    except OSError:
                        all_ready = False
                        break
                if all_ready:
                    break
                time.sleep(0.15)
            else:
                raise EnrollmentRefused("ENVIRONMENT_HOST_TUNNEL_TIMEOUT")
            yield local_ports
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                with contextlib.suppress(Exception):
                    proc.wait(timeout=2)


def _build_remote_python_bootstrap(environment: str, public_key: str) -> str:
    return f"""\
import hashlib, json, os, pwd, shutil, socket, subprocess, sys
from pathlib import Path

env = {environment!r}
pubkey = {public_key!r}
ssh_dir = Path.home() / ".ssh"
ssh_dir.mkdir(mode=0o700, exist_ok=True)
os.chmod(ssh_dir, 0o700)
ak = ssh_dir / "authorized_keys"
existing = ak.read_text(encoding="utf-8").splitlines() if ak.exists() else []
if pubkey not in existing:
    with ak.open("a", encoding="utf-8") as f:
        if existing and not ak.read_text(encoding="utf-8").endswith("\\n"):
            f.write("\\n")
        f.write(pubkey + "\\n")
os.chmod(ak, 0o600)

try:
    pwd.getpwnam("aiops")
except KeyError:
    nologin = "/usr/sbin/nologin" if Path("/usr/sbin/nologin").exists() else "/sbin/nologin"
    subprocess.run(
        ["useradd", "--system", "--no-create-home", "--home-dir", f"/var/lib/aiops-access-{{env}}",
         "--shell", nologin, "--user-group", "aiops"],
        check=True,
    )
    subprocess.run(["passwd", "-l", "aiops"], check=True)

ver = f"{{sys.version_info.major}}.{{sys.version_info.minor}}"
venv_ok = subprocess.run([sys.executable, "-I", "-c", "import ensurepip, venv"]).returncode == 0
if not venv_ok and shutil.which("apt-get"):
    e = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    subprocess.run(["apt-get", "update"], env=e, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(
        ["apt-get", "install", "-y", "--no-install-recommends", f"python{{ver}}-venv"],
        env=e, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    venv_ok = subprocess.run([sys.executable, "-I", "-c", "import ensurepip, venv"]).returncode == 0

mid = ""
for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
    if Path(p).is_file():
        mid = hashlib.sha256(Path(p).read_bytes().strip()).hexdigest()
        break

nginx_flavor = (
    "baota" if Path("/www/server/panel/vhost/nginx").is_dir()
    else "standard" if Path("/etc/nginx").is_dir()
    else "none"
)
print("AIOPS_BOOTSTRAP_JSON:" + json.dumps({{
    "hostname": socket.gethostname(),
    "machine_id_sha256": mid,
    "python_minor": ver,
    "service_user_present": True,
    "venv_ready": bool(venv_ok),
    "nginx_flavor": nginx_flavor,
}}))
"""


def enroll_via_ephemeral_password(
    registry_path: str | Path,
    identity: tuple[str, str, str],
    *,
    environment: str,
    address: str,
    password: str,
    ssh_port: int = DEFAULT_SSH_PORT,
    ssh_user: str = DEFAULT_SSH_USER,
    bootstrap_runner: Callable[..., dict[str, Any]] | None = None,
    keygen_runner: Callable[[Path, str], tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """模式 A：使用一次性内存口令完成主机引导并立即丢弃口令（绝不落盘、入库或回显）。"""
    env = _validate_environment(environment)
    addr = _validate_address(address)
    port = _validate_ssh_port(ssh_port)
    user = _validate_ssh_user(ssh_user)
    if not isinstance(password, str) or not password or len(password) > 512:
        raise EnrollmentRefused("ENVIRONMENT_HOST_PASSWORD_REQUIRED")

    with drafts._locked(registry_path):
        _authorize(registry_path, identity, env)
        body = _load_hosts_unlocked(registry_path)
        existing = body["hosts"].get(env)
        key_id, pub_key, priv_path = _ensure_ed25519_keypair(registry_path, env, keygen_runner=keygen_runner)
        runner = bootstrap_runner or _default_ephemeral_password_runner
        try:
            facts = runner(
                address=addr,
                ssh_port=port,
                ssh_user=user,
                password=password,
                public_key=pub_key,
                private_key_path=priv_path,
                environment=env,
            )
        finally:
            password = ""  # 显式解除本地引用，不留在异常帧或后续状态中

        fp = str(facts.get("host_fingerprint_sha256") or "")
        if not _FINGERPRINT_PATTERN.fullmatch(fp):
            raise EnrollmentRefused("ENVIRONMENT_HOST_FINGERPRINT_INVALID")
        if (
            existing
            and existing.get("status") in {"enrolled", "verified"}
            and existing.get("host_fingerprint_sha256")
            and existing["host_fingerprint_sha256"] != fp
        ):
            raise EnrollmentRefused("ENVIRONMENT_HOST_FINGERPRINT_MISMATCH")

        py_minor = str(facts.get("python_minor") or "")
        if py_minor and not _PYTHON_MINOR_PATTERN.fullmatch(py_minor):
            py_minor = ""
        mid = str(facts.get("machine_id_sha256") or "")
        if mid and not _SHA256_HEX_PATTERN.fullmatch(mid):
            mid = ""

        entry = {
            "environment": env,
            "host_alias": f"aiops-cust-{env}",
            "address": addr,
            "ssh_port": port,
            "ssh_user": user,
            "key_id": key_id,
            "public_key": pub_key,
            "host_fingerprint_sha256": fp,
            "host_public_key_line": str(facts.get("host_public_key_line") or "").strip()[:512],
            "enrollment_mode": "ephemeral_password",
            "status": "verified",
            "hostname": str(facts.get("hostname") or "")[:128],
            "machine_id_sha256": mid,
            "python_minor": py_minor,
            "service_user_present": bool(facts.get("service_user_present", False)),
            "venv_ready": bool(facts.get("venv_ready", False)),
            "nginx_flavor": str(facts.get("nginx_flavor") or "")[:32],
            "enrolled_at": time.time(),
            "enrolled_by": ":".join(identity),
            "rotate_root_password_recommended": True,
        }
        body["hosts"][env] = entry
        _save_hosts_unlocked(registry_path, body)
        return {
            "environment": env,
            "enrolled": True,
            "mode": "ephemeral_password",
            "password_persisted": False,
            "host": _public_host_view(entry),
        }


def create_bootstrap_script_session(
    registry_path: str | Path,
    identity: tuple[str, str, str],
    *,
    environment: str,
    address: str,
    callback_base_url: str,
    ssh_port: int = DEFAULT_SSH_PORT,
    ssh_user: str = DEFAULT_SSH_USER,
    ttl_seconds: int = BOOTSTRAP_TOKEN_TTL_SECONDS,
    keygen_runner: Callable[[Path, str], tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """模式 B：签发单次短效引导令牌（数据库仅存 SHA-256），返回客户侧一键执行命令。"""
    env = _validate_environment(environment)
    addr = _validate_address(address)
    port = _validate_ssh_port(ssh_port)
    user = _validate_ssh_user(ssh_user)
    base_url = _validate_callback_base_url(callback_base_url)
    if not (60 <= int(ttl_seconds) <= BOOTSTRAP_TOKEN_TTL_SECONDS):
        raise EnrollmentRefused("ENVIRONMENT_HOST_TTL_INVALID")

    with drafts._locked(registry_path):
        _authorize(registry_path, identity, env)
        body = _load_hosts_unlocked(registry_path)
        key_id, pub_key, _ = _ensure_ed25519_keypair(registry_path, env, keygen_runner=keygen_runner)
        token = "bs_" + secrets.token_hex(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.time()
        expires_at = now + int(ttl_seconds)
        db_path = drafts._database(registry_path)
        with gateway_db.initialize(db_path) as conn:
            _initialize_bootstrap_tokens(conn)
            conn.execute(
                """
                INSERT INTO environment_bootstrap_tokens (
                    token_hash, environment, address, ssh_port, ssh_user,
                    key_id, public_key, actor, created_at, expires_at, consumed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    token_hash,
                    env,
                    addr,
                    port,
                    user,
                    key_id,
                    pub_key,
                    ":".join(identity),
                    now,
                    expires_at,
                ),
            )
        protect_private_file(db_path)
        if env not in body["hosts"] or body["hosts"][env].get("status") == "revoked":
            body["hosts"][env] = {
                "environment": env,
                "host_alias": f"aiops-cust-{env}",
                "address": addr,
                "ssh_port": port,
                "ssh_user": user,
                "key_id": key_id,
                "public_key": pub_key,
                "host_fingerprint_sha256": "",
                "enrollment_mode": "bootstrap_script",
                "status": "pending_script",
                "hostname": "",
                "machine_id_sha256": "",
                "python_minor": "",
                "service_user_present": False,
                "venv_ready": False,
                "nginx_flavor": "",
                "enrolled_at": None,
                "enrolled_by": ":".join(identity),
                "rotate_root_password_recommended": False,
            }
            _save_hosts_unlocked(registry_path, body)

        script_url = f"{base_url}/v1/internal/environment-bootstrap/script/{token}"
        command = f"curl -fsSL {shlex.quote(script_url)} | sudo bash"
        return {
            "environment": env,
            "mode": "bootstrap_script",
            "script_url": script_url,
            "one_liner": command,
            "expires_at": expires_at,
            "single_use": True,
            "host": _public_host_view(body["hosts"][env]),
        }


def render_bootstrap_script_for_token(
    registry_path: str | Path,
    token: str,
    callback_base_url: str,
) -> str:
    """按单次未消费且未过期令牌渲染客户侧自举脚本（不消费令牌，消费发生在回调成功时）。"""
    if not isinstance(token, str) or not _TOKEN_PATTERN.fullmatch(token):
        raise EnrollmentRefused("ENVIRONMENT_BOOTSTRAP_TOKEN_INVALID")
    base_url = _validate_callback_base_url(callback_base_url)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = time.time()
    with drafts._locked(registry_path), gateway_db.initialize(drafts._database(registry_path)) as conn:
        _initialize_bootstrap_tokens(conn)
        row = conn.execute(
            "SELECT * FROM environment_bootstrap_tokens WHERE token_hash=?",
            (token_hash,),
        ).fetchone()
    if row is None or row["consumed_at"] is not None or float(row["expires_at"]) <= now:
        raise EnrollmentRefused("ENVIRONMENT_BOOTSTRAP_TOKEN_EXPIRED_OR_USED")

    env = str(row["environment"])
    pub_key = str(row["public_key"])
    callback_url = f"{base_url}/v1/internal/environment-bootstrap/callback"
    return f"""#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: please run with sudo or root" >&2
  exit 1
fi

ENV_ID={shlex.quote(env)}
PUB_KEY={shlex.quote(pub_key)}
CALLBACK_URL={shlex.quote(callback_url)}
BOOTSTRAP_TOKEN={shlex.quote(token)}

mkdir -p /root/.ssh
chmod 700 /root/.ssh
touch /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
grep -qxF "$PUB_KEY" /root/.ssh/authorized_keys || printf '%s\\n' "$PUB_KEY" >> /root/.ssh/authorized_keys

if ! id -u aiops >/dev/null 2>&1; then
  NOLOGIN="/usr/sbin/nologin"
  [[ -x "$NOLOGIN" ]] || NOLOGIN="/sbin/nologin"
  useradd --system --no-create-home --home-dir "/var/lib/aiops-access-$ENV_ID" \\
    --shell "$NOLOGIN" --user-group aiops
  passwd -l aiops >/dev/null
fi

PY_MINOR="$(python3 -c 'import sys; print(f"{{sys.version_info.major}}.{{sys.version_info.minor}}")')"
if ! python3 -I -c 'import ensurepip, venv' >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "python${{PY_MINOR}}-venv"
  fi
fi

VENV_OK="false"
if python3 -I -c 'import ensurepip, venv' >/dev/null 2>&1; then
  VENV_OK="true"
fi

HOST_FP="$(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | awk '{{print $2}}')"
HOSTNAME_VAL="$(hostname)"
MACHINE_HASH=""
if [[ -f /etc/machine-id ]]; then
  MACHINE_HASH="$(tr -d '\\r\\n' < /etc/machine-id | sha256sum | awk '{{print $1}}')"
fi
NGINX_FLAVOR="none"
if [[ -d /www/server/panel/vhost/nginx ]]; then
  NGINX_FLAVOR="baota"
elif [[ -d /etc/nginx ]]; then
  NGINX_FLAVOR="standard"
fi

PAYLOAD="$(python3 - "$ENV_ID" "$HOST_FP" "$HOSTNAME_VAL" "$MACHINE_HASH" \\
  "$PY_MINOR" "$VENV_OK" "$NGINX_FLAVOR" <<'PY'
import json, sys
print(json.dumps({{
  "environment": sys.argv[1],
  "host_fingerprint_sha256": sys.argv[2],
  "hostname": sys.argv[3],
  "machine_id_sha256": sys.argv[4],
  "python_minor": sys.argv[5],
  "service_user_present": True,
  "venv_ready": sys.argv[6] == "true",
  "nginx_flavor": sys.argv[7],
}}))
PY
)"

curl -fsS -X POST "$CALLBACK_URL" \\
  -H "Content-Type: application/json" \\
  -H "X-Bootstrap-Token: $BOOTSTRAP_TOKEN" \\
  -d "$PAYLOAD"
echo ""
echo "AI-Ops host bootstrap completed for $ENV_ID."
"""


def complete_bootstrap_callback(
    registry_path: str | Path,
    token: str,
    report: dict[str, Any],
) -> dict[str, Any]:
    """原子消费单次引导令牌并钉扎客户主机指纹，严禁重放或覆盖已有不匹配指纹。"""
    if not isinstance(token, str) or not _TOKEN_PATTERN.fullmatch(token):
        raise EnrollmentRefused("ENVIRONMENT_BOOTSTRAP_TOKEN_INVALID")
    if not isinstance(report, dict):
        raise EnrollmentRefused("ENVIRONMENT_BOOTSTRAP_REPORT_INVALID")

    fp = str(report.get("host_fingerprint_sha256") or "")
    if not _FINGERPRINT_PATTERN.fullmatch(fp):
        raise EnrollmentRefused("ENVIRONMENT_HOST_FINGERPRINT_INVALID")
    py_minor = str(report.get("python_minor") or "")
    if py_minor and not _PYTHON_MINOR_PATTERN.fullmatch(py_minor):
        raise EnrollmentRefused("ENVIRONMENT_BOOTSTRAP_REPORT_INVALID")
    mid = str(report.get("machine_id_sha256") or "")
    if mid and not _SHA256_HEX_PATTERN.fullmatch(mid):
        raise EnrollmentRefused("ENVIRONMENT_BOOTSTRAP_REPORT_INVALID")

    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = time.time()

    with drafts._locked(registry_path):
        with gateway_db.initialize(drafts._database(registry_path)) as conn:
            _initialize_bootstrap_tokens(conn)
            cur = conn.execute(
                """
                UPDATE environment_bootstrap_tokens
                   SET consumed_at = ?
                 WHERE token_hash = ? AND consumed_at IS NULL AND expires_at > ?
                """,
                (now, token_hash, now),
            )
            if cur.rowcount != 1:
                raise EnrollmentRefused("ENVIRONMENT_BOOTSTRAP_TOKEN_EXPIRED_OR_USED")
            row = conn.execute(
                "SELECT * FROM environment_bootstrap_tokens WHERE token_hash=?",
                (token_hash,),
            ).fetchone()
        assert row is not None
        env = str(row["environment"])
        if report.get("environment") and str(report["environment"]) != env:
            raise EnrollmentRefused("ENVIRONMENT_BOOTSTRAP_ENVIRONMENT_MISMATCH")

        body = _load_hosts_unlocked(registry_path)
        existing = body["hosts"].get(env)
        if (
            existing
            and existing.get("status") in {"enrolled", "verified"}
            and existing.get("host_fingerprint_sha256")
            and existing["host_fingerprint_sha256"] != fp
        ):
            raise EnrollmentRefused("ENVIRONMENT_HOST_FINGERPRINT_MISMATCH")

        entry = {
            "environment": env,
            "host_alias": f"aiops-cust-{env}",
            "address": str(row["address"]),
            "ssh_port": int(row["ssh_port"]),
            "ssh_user": str(row["ssh_user"]),
            "key_id": str(row["key_id"]),
            "public_key": str(row["public_key"]),
            "host_fingerprint_sha256": fp,
            "enrollment_mode": "bootstrap_script",
            "status": "enrolled",
            "hostname": str(report.get("hostname") or "")[:128],
            "machine_id_sha256": mid,
            "python_minor": py_minor,
            "service_user_present": bool(report.get("service_user_present", False)),
            "venv_ready": bool(report.get("venv_ready", False)),
            "nginx_flavor": str(report.get("nginx_flavor") or "")[:32],
            "enrolled_at": now,
            "enrolled_by": str(row["actor"]),
            "rotate_root_password_recommended": False,
        }
        body["hosts"][env] = entry
        _save_hosts_unlocked(registry_path, body)
        return {
            "environment": env,
            "enrolled": True,
            "mode": "bootstrap_script",
            "host": _public_host_view(entry),
        }
