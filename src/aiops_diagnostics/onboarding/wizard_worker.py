"""V2.1 dev-host 异步基建配套与正式发布真机执行器（ADR-0358）。

由 dev-host 侧 `aiops-admin environment-task-worker` 在轮询空闲时调用，负责完成所有
跨 `36` / `41` / 动态已钉扎客户主机的生产真机编排，并逐子步通过受签内部端点向
41 `wizard_jobs` 回写实时进度：
1. ``infra_provision``：
   - ``allowlist``：同步 36 `/etc/aiops-dify-api/allowed-sources.conf` 与 41 `aiops-execution-ingress.conf`；
   - ``execution_node``：在 41 幂等登记 Snail 应用/节点/智能体/订阅、签发回调证书并拉起
     ``aiops-environment-node@<env>.service``；
   - ``cold_install_and_onboard``：按目标机 Python 小版本从离线制品池（或现算构建）解析制品，
     驱动 41 `prepare_bundle` 准备隔离工作区/双 KB/双应用/runtime 包，并经钉扎指纹的纯密钥 SSH
     冷装 ``aiops-access-<env>.service`` 与注册双端运行态；
   - ``customer_nginx``：在客户机宝塔扩展目录部署双入口片与 ``0600`` 凭据片（``nginx -t`` 回滚守卫）；
   - ``live_probe``：即时验证公网 HTTPS 匿名问答 ``401`` 与客户既有 ``/upms/tenant/list`` ``200``。
2. ``publish_environment``：启用 41 与客户机目录、同步活跃 ``environment.json`` 与重载服务。
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiops_diagnostics.onboarding import artifact_pool, public_smoke

LOCKED_NODE_JAR_ON_41 = "/opt/aiops-node-runtime/aiops-execution-node-bb51c98c.jar"
HOST_36_ALLOWLIST = "/etc/aiops-dify-api/allowed-sources.conf"
HOST_41_EXEC_INGRESS = (
    "/www/server/panel/vhost/nginx/extension/api.mall.qushiyun.com/aiops-execution-ingress.conf"
)
SNAIL_PLATFORM_URL_ON_41 = "https://172.30.41.3:8900/snail-ai"
SNAIL_ADMIN_SECRET_ON_41 = "/etc/aiops-node-runtime/secrets/platform_admin"
SNAIL_CA_CERT_ON_41 = "/etc/aiops-node-runtime/tls/ca.crt"
SNAIL_CA_KEY_ON_41 = "/etc/aiops-node-runtime/tls/ca.key"
SNAIL_TRUSTSTORE_ON_41 = "/etc/aiops-node-runtime/tls/public/truststore.p12"
NODE_JAVA_IMAGE = "sha256:23cc5734ff46e776f51ce66eb9ee08444fd47caabec0a95b7eb6e94be2d3696d"


class WizardWorkerError(RuntimeError):
    """dev-host 异步基建/发布真机执行失败的稳定错误码异常。"""


def _dev_host_read(central_target: str, remote_path: str, *, cwd: Path | None = None) -> bytes:
    proc = subprocess.run(
        ["dev-host", "read", central_target, remote_path],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise WizardWorkerError(f"DEV_HOST_READ_FAILED:{remote_path}")
    return proc.stdout


def _dev_host_exec(
    target: str,
    shell_cmd: str,
    *,
    input_text: str | None = None,
    timeout_sec: int = 120,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["dev-host", "exec", target, "--allow-service-exec", "--timeout", str(timeout_sec), "--", shell_cmd],
        cwd=str(cwd) if cwd else None,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout_sec + 15,
    )
    return proc


def _dev_host_cp(
    target: str,
    local_file: Path,
    remote_dest: str,
    *,
    sha256_hex: str | None = None,
    cwd: Path | None = None,
) -> None:
    cmd = ["dev-host", "cp", target, str(local_file), "--dest", remote_dest]
    if sha256_hex:
        cmd.extend(["--artifact-sha256", sha256_hex])
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise WizardWorkerError(f"DEV_HOST_CP_FAILED:{remote_dest}")


class PinnedCustomerHost:
    """使用 41 受管 `customer-hosts.json` + 钉扎私钥在临时 0700 目录中直连动态客户主机。"""

    def __init__(
        self,
        *,
        host_ip: str,
        ssh_port: int,
        ssh_user: str,
        key_pem: str,
        host_public_key_line: str,
        expected_fingerprint: str = "",
    ) -> None:
        self.host_ip = host_ip
        self.ssh_port = int(ssh_port or 22)
        self.ssh_user = ssh_user or "root"
        self.key_pem = key_pem
        self.host_public_key_line = host_public_key_line.strip()
        self.expected_fingerprint = expected_fingerprint.strip()

    @classmethod
    def from_central(
        cls, central_target: str, environment: str, *, cwd: Path | None = None
    ) -> PinnedCustomerHost:
        raw_hosts = _dev_host_read(
            central_target, "/var/lib/aiops-41/environment-registry/customer-hosts.json", cwd=cwd
        )
        hosts_doc = json.loads(raw_hosts.decode("utf-8"))
        hosts_map = hosts_doc.get("hosts") or hosts_doc.get("environments") or {}
        rec = hosts_map.get(environment)
        host_ip = str(rec.get("address") or rec.get("public_ip") or "") if isinstance(rec, dict) else ""
        if not isinstance(rec, dict) or not host_ip or not rec.get("host_public_key_line"):
            raise WizardWorkerError("ENVIRONMENT_HOST_NOT_ENROLLED")
        key_id = str(rec.get("key_id") or "").strip()
        default_key_ref = (
            f"/var/lib/aiops-41/environment-registry/customer-host-keys/id_{key_id}"
            if key_id
            else f"/var/lib/aiops-41/environment-registry/customer-host-keys/{environment}.ed25519"
        )
        key_ref = str(rec.get("key_ref") or default_key_ref)
        key_pem = _dev_host_read(central_target, key_ref, cwd=cwd).decode("utf-8")
        return cls(
            host_ip=host_ip,
            ssh_port=int(rec.get("ssh_port") or 22),
            ssh_user=str(rec.get("ssh_user") or "root"),
            key_pem=key_pem,
            host_public_key_line=str(rec["host_public_key_line"]),
            expected_fingerprint=str(
                rec.get("host_fingerprint_sha256") or rec.get("host_key_fingerprint") or ""
            ),
        )

    def _prepare_ssh_dir(self, tmp: Path) -> tuple[Path, Path]:
        key_file = tmp / "id_ed25519"
        known_file = tmp / "known_hosts"
        key_file.write_text(self.key_pem, encoding="utf-8")
        key_file.chmod(0o600)
        line = self.host_public_key_line
        if not line.startswith(f"{self.host_ip} ") and not line.startswith(f"[{self.host_ip}]:"):
            prefix = f"[{self.host_ip}]:{self.ssh_port}" if self.ssh_port != 22 else self.host_ip
            line = f"{prefix} {line}"
        known_file.write_text(line + "\n", encoding="utf-8")
        known_file.chmod(0o600)
        if self.expected_fingerprint:
            fp_proc = subprocess.run(
                ["ssh-keygen", "-lf", str(known_file)], capture_output=True, text=True, check=True
            )
            if self.expected_fingerprint not in fp_proc.stdout:
                raise WizardWorkerError("ENVIRONMENT_HOST_FINGERPRINT_MISMATCH")
        return key_file, known_file

    def run_cmd(
        self, remote_cmd: str, *, input_data: bytes | str | None = None, timeout_sec: int = 240
    ) -> subprocess.CompletedProcess[bytes]:
        with tempfile.TemporaryDirectory(prefix="aiops-pin-ssh-") as tmpdir:
            tmp = Path(tmpdir)
            tmp.chmod(0o700)
            key_file, known_file = self._prepare_ssh_dir(tmp)
            cmd = [
                "ssh",
                "-i",
                str(key_file),
                "-p",
                str(self.ssh_port),
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"UserKnownHostsFile={known_file}",
                "-o",
                "ConnectTimeout=15",
                f"{self.ssh_user}@{self.host_ip}",
                remote_cmd,
            ]
            payload = input_data.encode("utf-8") if isinstance(input_data, str) else input_data
            return subprocess.run(cmd, input=payload, capture_output=True, timeout=timeout_sec)

    def copy_file(self, local_path: Path, remote_path: str, *, timeout_sec: int = 180) -> None:
        with tempfile.TemporaryDirectory(prefix="aiops-pin-scp-") as tmpdir:
            tmp = Path(tmpdir)
            tmp.chmod(0o700)
            key_file, known_file = self._prepare_ssh_dir(tmp)
            cmd = [
                "scp",
                "-i",
                str(key_file),
                "-P",
                str(self.ssh_port),
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"UserKnownHostsFile={known_file}",
                "-o",
                "ConnectTimeout=15",
                str(local_path),
                f"{self.ssh_user}@{self.host_ip}:{remote_path}",
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
            if proc.returncode != 0:
                raise WizardWorkerError("CUSTOMER_SCP_FAILED")


def _step_allowlist(
    root: Path,
    central_target: str,
    egress_ip: str,
    *,
    exec_fn: Callable[..., subprocess.CompletedProcess[str]] = _dev_host_exec,
) -> dict[str, Any]:
    """同步 36 Dify API 与 41 执行入口出口白名单（带 backup + nginx -t 回滚守卫）。"""
    # 1. 更新 36 /etc/aiops-dify-api/allowed-sources.conf
    py_36 = "\n".join(
        [
            "from pathlib import Path; import shutil, subprocess, sys",
            f"p = Path({HOST_36_ALLOWLIST!r})",
            f"ip = {egress_ip!r}",
            "orig = p.read_text(encoding='utf-8') if p.exists() else 'allow 127.0.0.1;\\n'",
            "lines = [ln.rstrip() for ln in orig.splitlines() if ln.strip()]",
            "entry = f'allow {ip};'",
            "if entry not in lines:",
            "    bak = p.with_suffix('.conf.wizard-bak')",
            "    bak.write_text(orig, encoding='utf-8')",
            "    lines.append(entry)",
            "    p.write_text('\\n'.join(lines) + '\\n', encoding='utf-8')",
            "    p.chmod(0o644)",
            "    t = subprocess.run(['nginx', '-t'], capture_output=True, text=True)",
            "    if t.returncode != 0:",
            "        p.write_text(orig, encoding='utf-8')",
            "        p.chmod(0o644)",
            "        raise SystemExit(2)",
            "p.chmod(0o644)",
            "subprocess.run(['nginx', '-s', 'reload'], check=True)",
        ]
    )
    r36 = exec_fn("36", "sudo -n python3 -", input_text=py_36, timeout_sec=60, cwd=root)
    if r36.returncode != 0:
        return {"ok": False, "reason": "ALLOWLIST_36_FAILED"}

    # 2. 更新 41 aiops-execution-ingress.conf（通过本仓 render_execution_ingress.py 渲染）
    cur_41 = _dev_host_read(central_target, HOST_41_EXEC_INGRESS, cwd=root).decode("utf-8", errors="replace")
    sources: list[str] = ["124.243.178.156", "120.55.45.59"]
    for tok in cur_41.splitlines():
        if "$realip_remote_addr" in tok and "^(" in tok:
            inner = tok.split("^(", 1)[1].split(")", 1)[0]
            parsed = [part.replace("[.]", ".") for part in inner.split("|") if part]
            if parsed:
                sources = parsed
            break
    if egress_ip not in sources:
        sources.append(egress_ip)

    with tempfile.TemporaryDirectory(prefix="aiops-exec-ing-") as tmpdir:
        stage_file = Path(tmpdir) / "aiops-execution-ingress.conf"
        render_cmd = [
            sys.executable,
            str(root / "tools/content/render_execution_ingress.py"),
            "--upstream",
            "https://172.30.41.3:8900",
            "--server-name",
            "platform",
            "--ca-file",
            "/etc/aiops-node-runtime/tls/public/ca.crt",
            *[arg for src in sources for arg in ("--allow-source", src)],
            "--out",
            str(stage_file),
        ]
        subprocess.run(render_cmd, cwd=str(root), capture_output=True, text=True, check=True)
        stage_sha = hashlib.sha256(stage_file.read_bytes()).hexdigest()
        _dev_host_cp(
            central_target,
            stage_file,
            "/tmp/wizard-aiops-execution-ingress.conf",
            sha256_hex=stage_sha,
            cwd=root,
        )
    apply_41 = "\n".join(
        [
            "import shutil, subprocess",
            "from pathlib import Path",
            f"dst = Path({HOST_41_EXEC_INGRESS!r})",
            "src = Path('/tmp/wizard-aiops-execution-ingress.conf')",
            "orig = dst.read_text(encoding='utf-8') if dst.exists() else ''",
            "shutil.copyfile(src, dst)",
            "dst.chmod(0o600)",
            "src.unlink(missing_ok=True)",
            "t = subprocess.run(['nginx', '-t'], capture_output=True, text=True)",
            "if t.returncode != 0:",
            "    if orig: dst.write_text(orig, encoding='utf-8')",
            "    raise SystemExit(2)",
            "subprocess.run(['nginx', '-s', 'reload'], check=True)",
        ]
    )
    r41 = exec_fn(central_target, "sudo -n python3 -", input_text=apply_41, timeout_sec=60, cwd=root)
    if r41.returncode != 0:
        return {"ok": False, "reason": "ALLOWLIST_41_FAILED"}
    return {"ok": True}


def _step_execution_node(
    root: Path,
    central_target: str,
    *,
    environment: str,
    api_domain: str,
    exec_fn: Callable[..., subprocess.CompletedProcess[str]] = _dev_host_exec,
) -> dict[str, Any]:
    """在 41 幂等登记 Snail 资源、签发回调证书并启用 node 服务。"""
    revision = subprocess.check_output(["git", "rev-parse", "origin/main"], cwd=str(root), text=True).strip()
    callback_conf = subprocess.run(
        [
            sys.executable,
            str(root / "tools/content/render_node_callback.py"),
            "--environment-egress",
            "--gateway",
            f"https://{api_domain}",
        ],
        cwd=str(root),
        env={**os.environ, "PYTHONPATH": str(root / "src")},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    compose_bytes = (root / "deploy/node-runtime/environment.compose.yaml").read_bytes()
    unit_bytes = (root / "deploy/node-runtime/aiops-environment-node@.service").read_bytes()
    images_lock_bytes = (root / "deploy/m2-live-03/images.lock.json").read_bytes()
    op_tools_init = (root / "src/aiops_diagnostics/operator/__init__.py").read_bytes()
    op_tools_py = (root / "src/aiops_diagnostics/operator/operator_tools.py").read_bytes()
    op_tools_json = (root / "src/aiops_diagnostics/operator/operator_tools.json").read_bytes()
    pkg_init = (root / "src/aiops_diagnostics/__init__.py").read_bytes()

    stage_tar_path: Path | None = None
    with tempfile.TemporaryDirectory(prefix="aiops-node-stage-") as tmpdir:
        tmp = Path(tmpdir)
        dist_src = tmp / "dist"
        for rel, data in (
            ("deploy/node-runtime/environment.compose.yaml", compose_bytes),
            ("deploy/m2-live-03/images.lock.json", images_lock_bytes),
            ("src/aiops_diagnostics/__init__.py", pkg_init),
            ("src/aiops_diagnostics/operator/__init__.py", op_tools_init),
            ("src/aiops_diagnostics/operator/operator_tools.py", op_tools_py),
            ("src/aiops_diagnostics/operator/operator_tools.json", op_tools_json),
        ):
            dest = dist_src / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        stage_tar_path = tmp / f"node-stage-{environment}.tar.gz"
        subprocess.run(["tar", "-czf", str(stage_tar_path), "-C", str(dist_src), "."], check=True)
        tar_sha = hashlib.sha256(stage_tar_path.read_bytes()).hexdigest()
        _dev_host_cp(
            central_target,
            stage_tar_path,
            f"/tmp/wizard-node-stage-{environment}.tar.gz",
            sha256_hex=tar_sha,
            cwd=root,
        )
        cb_path = tmp / f"callback-{environment}.conf"
        cb_path.write_text(callback_conf, encoding="utf-8")
        _dev_host_cp(
            central_target,
            cb_path,
            f"/tmp/wizard-callback-{environment}.conf",
            sha256_hex=hashlib.sha256(cb_path.read_bytes()).hexdigest(),
            cwd=root,
        )
        unit_path = tmp / "aiops-environment-node@.service"
        unit_path.write_bytes(unit_bytes)
        _dev_host_cp(
            central_target,
            unit_path,
            "/tmp/wizard-env-node@.service",
            sha256_hex=hashlib.sha256(unit_bytes).hexdigest(),
            cwd=root,
        )

    remote_script = f"""
import hashlib, json, os, pwd, secrets, shutil, ssl, subprocess, tarfile
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ENV = {environment!r}
REV = {revision!r}
JAR_SRC = Path({LOCKED_NODE_JAR_ON_41!r})
SNAIL_BASE = {SNAIL_PLATFORM_URL_ON_41!r}
CA_CRT = Path({SNAIL_CA_CERT_ON_41!r})
CA_KEY = Path({SNAIL_CA_KEY_ON_41!r})
TRUSTSTORE = Path({SNAIL_TRUSTSTORE_ON_41!r})
ADMIN_PASS = Path({SNAIL_ADMIN_SECRET_ON_41!r}).read_text(encoding='utf-8').strip()

aiops_pw = pwd.getpwnam('aiops41')

def own_private(p: Path, mode: int = 0o600):
    os.chown(p, aiops_pw.pw_uid, aiops_pw.pw_gid)
    p.chmod(mode)

def mkdir_priv(p: Path, uid=None, gid=None, mode=0o700):
    p.mkdir(parents=True, exist_ok=True)
    os.chown(p, aiops_pw.pw_uid if uid is None else uid, aiops_pw.pw_gid if gid is None else gid)
    p.chmod(mode)

# 1. 部署节点发行包 + current 符号链接
dist_base = Path(f'/opt/aiops-node-runtime/environments/{{ENV}}')
rel_dir = dist_base / 'releases' / REV
(dist_base / 'releases').mkdir(parents=True, exist_ok=True)
if not (rel_dir / 'node' / JAR_SRC.name).is_file():
    rel_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(f'/tmp/wizard-node-stage-{{ENV}}.tar.gz', 'r:gz') as tf:
        tf.extractall(rel_dir)
    (rel_dir / 'node').mkdir(exist_ok=True)
    shutil.copyfile(JAR_SRC, rel_dir / 'node' / JAR_SRC.name)
    (rel_dir / 'REVISION').write_text(REV + '\\n', encoding='utf-8')
Path(f'/tmp/wizard-node-stage-{{ENV}}.tar.gz').unlink(missing_ok=True)
cur_link = dist_base / 'current'
tmp_link = dist_base / '.current.tmp'
tmp_link.unlink(missing_ok=True)
tmp_link.symlink_to(rel_dir)
tmp_link.replace(cur_link)

unit_dst = Path('/etc/systemd/system/aiops-environment-node@.service')
shutil.move('/tmp/wizard-env-node@.service', unit_dst)
unit_dst.chmod(0o644)

# 2. 准备 41 节点配置目录与证书
cfg_dir = Path(f'/etc/aiops-node-runtime/environments/{{ENV}}')
mkdir_priv(cfg_dir)
mkdir_priv(cfg_dir / 'tls')
mkdir_priv(cfg_dir / 'tls/callback')
mkdir_priv(cfg_dir / 'tls/public')
shutil.move(f'/tmp/wizard-callback-{{ENV}}.conf', cfg_dir / 'callback.conf')
own_private(cfg_dir / 'callback.conf', 0o600)
shutil.copyfile(CA_CRT, cfg_dir / 'tls/public/ca.crt')
own_private(cfg_dir / 'tls/public/ca.crt', 0o600)
shutil.copyfile(TRUSTSTORE, cfg_dir / 'tls/public/truststore.p12')
own_private(cfg_dir / 'tls/public/truststore.p12', 0o600)

srv_crt = cfg_dir / 'tls/callback/server.crt'
srv_key = cfg_dir / 'tls/callback/server.key'
if not srv_crt.is_file() or not srv_key.is_file():
    csr = f'/tmp/wizard-{{ENV}}.csr'
    ext = f'/tmp/wizard-{{ENV}}.ext'
    Path(ext).write_text('subjectAltName=DNS:gateway-egress\\n', encoding='utf-8')
    subprocess.run(['openssl', 'genrsa', '-out', str(srv_key), '2048'], check=True)
    subprocess.run(
        ['openssl', 'req', '-new', '-key', str(srv_key), '-subj', '/CN=gateway-egress', '-out', csr],
        check=True,
    )
    subprocess.run(
        [
            'openssl', 'x509', '-req', '-in', csr,
            '-CA', str(CA_CRT), '-CAkey', str(CA_KEY), '-CAcreateserial',
            '-out', str(srv_crt), '-days', '825', '-extfile', ext,
        ],
        check=True,
    )
    Path(csr).unlink(missing_ok=True)
    Path(ext).unlink(missing_ok=True)
own_private(srv_crt, 0o600)
own_private(srv_key, 0o600)

state_dir = Path(f'/var/lib/aiops-node-runtime/environments/{{ENV}}')
mkdir_priv(state_dir, uid=999, gid=987, mode=0o700)

# 3. 调 Snail 平台管理接口幂等创建 App / Node / Agent / OpenId / Subscription
ssl_ctx = ssl.create_default_context(cafile=str(CA_CRT))
def snail_call(path, body=None, token='', method=None):
    data = json.dumps(body).encode('utf-8') if body is not None else None
    headers = {{'Content-Type': 'application/json'}}
    if token:
        headers['Snail-Ai-Auth'] = token
    eff_method = method or ('POST' if body is not None else 'GET')
    req = Request(SNAIL_BASE + path, data=data, headers=headers, method=eff_method)
    try:
        with urlopen(req, timeout=20, context=ssl_ctx) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode('utf-8', errors='replace') or '{{}}')

st, login_res = snail_call('/user/login', {{'username': 'admin', 'password': ADMIN_PASS}})
assert st == 200 and login_res.get('status') == 1, 'snail login failed'
token = login_res['data']['token']

app_name = f'aiops-environment-{{ENV}}'
node_id = f'node-{{ENV}}'
app_token = f'SAI_{{secrets.token_hex(16)}}'
node_token = f'aiops_node_{{secrets.token_hex(16)}}'

agent_runs_path = cfg_dir / 'agent-runs.json'
node_env_path = cfg_dir / 'node.env'
if agent_runs_path.is_file():
    existing_runs = json.loads(agent_runs_path.read_text(encoding='utf-8'))
    app_id = existing_runs.get('snail_app_id') or app_name
    app_token = existing_runs.get('snail_app_token') or app_token
else:
    app_id = app_name

def _records(payload):
    d = payload.get('data')
    return d.get('records') if isinstance(d, dict) and 'records' in d else (d if isinstance(d, list) else [])

# 查询或创建应用
_, apps_list = snail_call('/app/page?current=1&size=100', None, token, 'GET')
rows = _records(apps_list)
app_row = next((r for r in rows if r.get('appId') == app_id or r.get('appName') == app_name), None)
if app_row is None:
    app_body = {{'appName': app_name, 'appId': app_id, 'appToken': app_token, 'description': f'env {{ENV}}'}}
    snail_call('/app', app_body, token, 'POST')
else:
    app_id = app_row['appId']
    app_token = app_row.get('appToken') or app_token

# 查询或创建模型配置
_, chats = snail_call('/ai-model/by-type/CHAT', None, token, 'GET')
chat_models = chats.get('data') or []
model_cfg_id = chat_models[0]['id'] if chat_models else 3
for cm in chat_models:
    if 'deepseek' in str(cm.get('modelKey') or '').lower():
        model_cfg_id = cm['id']
        break

# 查询或创建 Agent
agent_name = f'aiops-agent-{{ENV}}'
_, ag_list = snail_call('/agent/page?current=1&size=100', None, token, 'GET')
ag_rows = _records(ag_list)
ag_row = next((r for r in ag_rows if r.get('name') == agent_name or r.get('appId') == app_id), None)
if ag_row is None:
    ag_body = {{
        'name': agent_name,
        'appId': app_id,
        'modelId': model_cfg_id,
        'instruction': '用 charging_status_snapshot 工具取一次快照，并原样作答。',
    }}
    _, ag_created = snail_call('/agent', ag_body, token, 'POST')
    agent_id = int(ag_created.get('data') or 0)
else:
    agent_id = int(ag_row['id'])

# OpenApiUser 与订阅
_, u_list = snail_call('/openapi/user/page?current=1&size=100', None, token, 'GET')
u_rows = _records(u_list)
u_row = next((r for r in u_rows if r.get('username') == app_name or r.get('openId') == app_name), None)
if u_row is None:
    open_id = secrets.token_hex(16)
    u_body = {{'username': app_name, 'openId': open_id, 'nickname': app_name}}
    snail_call('/openapi/user', u_body, token, 'POST')
else:
    open_id = str(u_row['openId'])

snail_call('/openapi/subscription', {{'appId': app_id, 'openId': open_id}}, token, 'POST')

# 读取既有的 node_token 避免重复建错
if node_env_path.is_file():
    for ln in node_env_path.read_text(encoding='utf-8').splitlines():
        if ln.startswith('AIOPS_NODE_TOKEN='):
            node_token = ln.split('=', 1)[1].strip() or node_token
token_sha = hashlib.sha256(node_token.encode()).hexdigest()

tool_desc = (
    'Return the charging status snapshot for the order already authorized for this '
    'conversation. Takes no arguments; the order and identity come from the Gateway.'
)
empty_schema = '{{"type":"object","properties":{{}},"required":[],"additionalProperties":false}}'
manifest_obj = {{
    'schema': 'm3-exec-config/1',
    'name': f'aiops-environment-{{ENV}}',
    'history_policy': 'none',
    'model': {{
        'provider_key': 'openai',
        'adapter_key': 'openai-compatible',
        'model_key': 'deepseek-v4-1-flash-261017',
        'endpoint': 'https://model-proxy:9101',
    }},
    'model_options': {{'max_tokens': 4096, 'temperature': 0.3, 'stop_sequences': []}},
    'system_messages': ['用 charging_status_snapshot 工具取一次快照，并原样作答。'],
    'tools': [{{'name': 'charging_status_snapshot', 'description': tool_desc, 'input_schema': empty_schema}}],
}}
manifest_file = cfg_dir / 'exec-manifest.json'
manifest_file.write_text(json.dumps(manifest_obj, ensure_ascii=False) + '\\n', encoding='utf-8')
own_private(manifest_file)

(cfg_dir / 'agent-runs.json').write_text(json.dumps({{
    'environment': ENV,
    'snail_base_url': 'https://api.mall.qushiyun.com',
    'snail_app_id': app_id,
    'snail_app_token': app_token,
    'snail_agent_id': agent_id,
    'snail_open_id': open_id,
    'expected_node_id': node_id,
    'nodes': [{{'node_id': node_id, 'environment': ENV, 'token_sha256': token_sha}}],
}}, ensure_ascii=False) + '\\n', encoding='utf-8')
own_private(cfg_dir / 'agent-runs.json')

(cfg_dir / 'node.json').write_text(json.dumps({{
    'environment': ENV,
    'config_file': str(cfg_dir / 'agent-runs.json'),
    'manifest_file': str(cfg_dir / 'exec-manifest.json'),
    'proxy_base_url': 'https://gateway-egress:9443',
}}, ensure_ascii=False, indent=2) + '\\n', encoding='utf-8')
own_private(cfg_dir / 'node.json')

node_env_path.write_text(
    f'AIOPS_EXEC_MANIFEST={{cfg_dir}}/exec-manifest.json\\n'
    f'SNAIL_AI_APP_ID={{app_id}}\\n'
    f'SNAIL_AI_TOKEN={{app_token}}\\n'
    f'AIOPS_NODE_TOKEN={{node_token}}\\n',
    encoding='utf-8',
)
own_private(node_env_path)

jar_sha12 = hashlib.sha256(JAR_SRC.read_bytes()).hexdigest()[:12]
(cfg_dir / 'runtime.env').write_text(
    'AIOPS_NODE_UID=999\\n'
    'AIOPS_NODE_GID=987\\n'
    f'AIOPS_NODE_JAVA_IMAGE={NODE_JAVA_IMAGE}\\n'
    f'AIOPS_NODE_BUILD={{REV[:12]}}-{{jar_sha12}}\\n'
    f'AIOPS_ENV_NODE_NAME={{node_id}}\\n'
    f'AIOPS_ENV_NODE_HOST={{node_id}}\\n'
    'AIOPS_SHARED_PLATFORM_HOST=platform\\n'
    'AIOPS_SHARED_PLATFORM_NETWORK=aiops-node-runtime_trial\\n'
    'AIOPS_SHARED_MODEL_NETWORK=aiops-node-runtime_modellink\\n'
    f'AIOPS_ENV_CONFIG={{cfg_dir}}\\n'
    f'AIOPS_ENV_STATE={{state_dir}}\\n'
    f'AIOPS_ENV_DIST={{rel_dir}}\\n',
    encoding='utf-8',
)
own_private(cfg_dir / 'runtime.env')

inst_file = Path('/etc/aiops-41/environment-installation.json')
inst_body = json.loads(inst_file.read_text(encoding='utf-8'))
targets = [r for r in inst_body.get('targets', []) if r.get('id') != f'host-{{ENV}}']
targets.append({{
    'id': f'host-{{ENV}}',
    'node_file': str(cfg_dir / 'node.json'),
    'capacity': {{'shared': [{{'host': '41', 'service': 'aiops-gateway-41.service', 'kind': 'systemd'}}]}},
}})
inst_body['targets'] = targets
inst_file.write_text(json.dumps(inst_body, ensure_ascii=False, indent=2) + '\\n', encoding='utf-8')
own_private(inst_file)

subprocess.run(['systemctl', 'daemon-reload'], check=True)
subprocess.run(['systemctl', 'enable', '--now', f'aiops-environment-node@{{ENV}}.service'], check=True)
"""
    res = exec_fn(central_target, "sudo -n python3 -", input_text=remote_script, timeout_sec=180, cwd=root)
    if res.returncode != 0:
        return {"ok": False, "reason": "EXECUTION_NODE_SETUP_FAILED"}
    return {"ok": True}


def _ensure_pool_artifacts(
    root: Path,
    central_target: str,
    *,
    revision: str,
    python_minor: str,
    exec_fn: Callable[..., subprocess.CompletedProcess[str]] = _dev_host_exec,
) -> dict[str, str]:
    """确保控制面离线制品池拥有 (revision, python_minor) 制品，缺失时现场构建并登记。"""
    pool_remote = "/var/lib/aiops-41/onboarding-assets/artifact-pool.json"
    pool_data = {"schema": artifact_pool.ARTIFACT_POOL_SCHEMA, "revision": 0, "entries": []}
    proc = subprocess.run(
        ["dev-host", "read", central_target, pool_remote],
        cwd=str(root),
        capture_output=True,
        timeout=30,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        pool_data = json.loads(proc.stdout.decode("utf-8"))

    for entry in pool_data.get("entries") or []:
        if entry.get("revision") == revision and entry.get("python_minor") == python_minor:
            return {
                "revision": revision,
                "python_minor": python_minor,
                "app_remote": str(entry["application"]["path"]),
                "app_sha256": str(entry["application"]["sha256"]),
                "inst_remote": str(entry["installer"]["path"]),
                "inst_sha256": str(entry["installer"]["sha256"]),
            }

    with tempfile.TemporaryDirectory(prefix="aiops-pool-build-") as tmpdir:
        tmp = Path(tmpdir)
        app_local = tmp / f"application-{python_minor}-{revision[:12]}.tar.gz"
        inst_local = tmp / f"installer-{revision[:12]}.tar.gz"
        subprocess.run(
            ["bash", "ops/environments/gateway/build.sh", revision, python_minor, str(app_local)],
            cwd=str(root),
            check=True,
            timeout=600,
        )
        subprocess.run(
            ["bash", "ops/environments/gateway/build-installer.sh", revision, str(inst_local)],
            cwd=str(root),
            check=True,
            timeout=120,
        )
        app_sha = hashlib.sha256(app_local.read_bytes()).hexdigest()
        inst_sha = hashlib.sha256(inst_local.read_bytes()).hexdigest()
        pool_dir = f"/var/lib/aiops-41/onboarding-assets/pool-{revision[:12]}-py{python_minor}"
        app_remote = f"{pool_dir}/application-{python_minor}.tar.gz"
        inst_remote = f"{pool_dir}/installer.tar.gz"
        _dev_host_cp(
            central_target, app_local, f"/tmp/wizard-app-{revision[:12]}.tar.gz", sha256_hex=app_sha, cwd=root
        )
        _dev_host_cp(
            central_target,
            inst_local,
            f"/tmp/wizard-inst-{revision[:12]}.tar.gz",
            sha256_hex=inst_sha,
            cwd=root,
        )

        reg_script = "\n".join(
            [
                "import json, os, pwd, shutil, time",
                "from pathlib import Path",
                f"pool_dir = Path({pool_dir!r})",
                "pw = pwd.getpwnam('aiops41')",
                "pool_dir.mkdir(parents=True, exist_ok=True)",
                "os.chown(pool_dir, pw.pw_uid, pw.pw_gid)",
                "pool_dir.chmod(0o700)",
                f"shutil.move('/tmp/wizard-app-{revision[:12]}.tar.gz', {app_remote!r})",
                f"shutil.move('/tmp/wizard-inst-{revision[:12]}.tar.gz', {inst_remote!r})",
                f"for p in ({app_remote!r}, {inst_remote!r}):",
                "    os.chown(p, pw.pw_uid, pw.pw_gid)",
                "    Path(p).chmod(0o600)",
                f"pool_file = Path({pool_remote!r})",
                "default_pool = {'schema': 'aiops-artifact-pool/1', 'revision': 0, 'entries': []}",
                "body = json.loads(pool_file.read_text()) if pool_file.exists() else default_pool",
                f"rev, py_m = {revision!r}, {python_minor!r}",
                " ".join(
                    [
                        "entries = [e for e in body['entries']",
                        "if not (e.get('revision') == rev and e.get('python_minor') == py_m)]",
                    ]
                ),
                "entries.append({",
                f"    'revision': {revision!r}, 'python_minor': {python_minor!r},",
                f"    'application': {{'path': {app_remote!r}, 'sha256': {app_sha!r}}},",
                f"    'installer': {{'path': {inst_remote!r}, 'sha256': {inst_sha!r}}},",
                "    'registered_at': time.time(), 'registered_by': 'wizard-worker',",
                "})",
                "body['entries'] = entries",
                "body['revision'] = int(body.get('revision') or 0) + 1",
                "out_txt = json.dumps(body, ensure_ascii=False, indent=2) + '\\n'",
                "pool_file.write_text(out_txt, encoding='utf-8')",
                "os.chown(pool_file, pw.pw_uid, pw.pw_gid)",
                "pool_file.chmod(0o600)",
            ]
        )
        res = exec_fn(central_target, "sudo -n python3 -", input_text=reg_script, timeout_sec=60, cwd=root)
        if res.returncode != 0:
            raise WizardWorkerError("ARTIFACT_POOL_REGISTER_FAILED")
    return {
        "revision": revision,
        "python_minor": python_minor,
        "app_remote": app_remote,
        "app_sha256": app_sha,
        "inst_remote": inst_remote,
        "inst_sha256": inst_sha,
    }


def _step_cold_install_and_onboard(
    root: Path,
    central_target: str,
    request: Callable[[dict[str, Any]], dict[str, Any]],
    job_req: dict[str, Any],
    *,
    pinned_host: PinnedCustomerHost,
) -> dict[str, Any]:
    """解析/构建制品 + 调用 41 prepare_bundle + 钉扎 SSH 客户机冷装与运行态注入。"""
    env = str(job_req["environment"])
    python_minor = str(job_req.get("python_minor") or "3.12")
    if python_minor not in artifact_pool.SUPPORTED_PYTHON_MINORS:
        python_minor = "3.12"
    local_port = int(job_req.get("local_port") or 8788)
    revision = subprocess.check_output(["git", "rev-parse", "origin/main"], cwd=str(root), text=True).strip()

    pool_info = _ensure_pool_artifacts(root, central_target, revision=revision, python_minor=python_minor)
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
        raise WizardWorkerError("CONTROL_PLANE_PREPARE_BUNDLE_FAILED")

    with tempfile.TemporaryDirectory(prefix="aiops-cold-scp-") as tmpdir:
        tmp = Path(tmpdir)
        tmp.chmod(0o700)
        for name, remote_path in (
            ("application.tar.gz", pool_info["app_remote"]),
            ("installer.tar.gz", pool_info["inst_remote"]),
            ("template.env", str(bundle["config_path"])),
            ("runtime-bundle.tar", str(bundle["runtime_bundle_path"])),
            ("nginx-user-ingress-auth.conf", str(bundle["nginx_auth_path"])),
        ):
            out = tmp / name
            out.write_bytes(_dev_host_read(central_target, remote_path, cwd=root))
            out.chmod(0o600)

        stage_remote = f"/var/backups/aiops-wizard-stage-{env}"
        q_stage = shlex.quote(stage_remote)
        pinned_host.run_cmd(f"rm -rf {q_stage} && mkdir -p {q_stage} && chmod 700 {q_stage}")
        for fname in (
            "application.tar.gz",
            "installer.tar.gz",
            "template.env",
            "runtime-bundle.tar",
            "nginx-user-ingress-auth.conf",
        ):
            pinned_host.copy_file(tmp / fname, f"{stage_remote}/{fname}")

        cfg_root = f"/etc/aiops-access-{env}"
        var_root = f"/var/lib/aiops-access-{env}"
        svc_user = f"aiops-access-{env}"
        seed_py = (
            "import json; "
            + "from aiops_diagnostics.onboarding.wizard_provision import seed_target_gateway_db; "
            + f"seed_target_gateway_db('{var_root}/gateway/gateway.db', "
            + f"json.loads(open('{cfg_root}/runtime/gateway-seed.json').read()))"
        )
        health_url = f"http://127.0.0.1:{local_port}/health"
        install_cmds = [
            "set -euo pipefail",
            f"mkdir -p {stage_remote}/inst && chmod 700 {stage_remote}/inst",
            f"tar -xzf {stage_remote}/installer.tar.gz -C {stage_remote}/inst",
            f"chmod 600 {stage_remote}/*.*",
            f"id -u {shlex.quote(svc_user)} >/dev/null 2>&1 || useradd --system --no-create-home "
            f"--home-dir {shlex.quote(var_root)} --shell /usr/sbin/nologin "
            f"--user-group {shlex.quote(svc_user)}",
            f"mkdir -p {cfg_root}/runtime {cfg_root}/registry {var_root}/dify-keys {var_root}/gateway",
            f"chmod 755 {cfg_root}",
            f"tar -xf {stage_remote}/runtime-bundle.tar -C {cfg_root}",
            f"mv -f {cfg_root}/dify-keys/* {var_root}/dify-keys/ 2>/dev/null || true",
            f"rmdir {cfg_root}/dify-keys 2>/dev/null || true",
            f"cp -f {stage_remote}/nginx-user-ingress-auth.conf {cfg_root}/nginx-user-ingress-auth.conf",
            f"chmod 700 {cfg_root}/runtime {cfg_root}/registry {var_root} "
            f"{var_root}/dify-keys {var_root}/gateway",
            f"chmod 600 {cfg_root}/runtime/* {cfg_root}/registry/* {var_root}/dify-keys/* "
            f"{cfg_root}/nginx-user-ingress-auth.conf",
            f"chown -R {svc_user}:{svc_user} {cfg_root}/runtime {cfg_root}/registry {var_root}",
            " ".join(
                [
                    f"python3 {stage_remote}/inst/install.py install --ensure-prerequisites",
                    f"--environment {shlex.quote(env)}",
                    f"--user {shlex.quote(svc_user)}",
                    f"--port {local_port}",
                    f"--archive {stage_remote}/application.tar.gz",
                    f"--sha256 {shlex.quote(pool_info['app_sha256'])}",
                    f"--config {stage_remote}/template.env",
                    f"--config-sha256 {shlex.quote(str(bundle['config_sha256']))}",
                ]
            ),
            f"/opt/aiops-access-{env}/current/venv/bin/python -c {shlex.quote(seed_py)}",
            f"chown -R {svc_user}:{svc_user} {var_root}/gateway",
            f"systemctl restart aiops-access-{env}.service",
            f"for _i in $(seq 1 20); do curl -fsS {health_url} && break; sleep 1; done",
            f"curl -fsS {health_url}",
            f"rm -rf {stage_remote}",
        ]
        remote_install_sh = "; ".join(install_cmds)
        res = pinned_host.run_cmd(remote_install_sh, timeout_sec=420)
        if res.returncode != 0:
            raise WizardWorkerError("CUSTOMER_COLD_INSTALL_FAILED")

    return {
        "ok": True,
        "consumer_kb_id": str(bundle.get("consumer_kb_id") or ""),
        "operator_kb_id": str(bundle.get("operator_kb_id") or ""),
        "workspace_id": str(bundle.get("workspace_id") or ""),
        "consumer_app_id": str(bundle.get("consumer_app_id") or ""),
        "operator_app_id": str(bundle.get("operator_app_id") or ""),
        "tenant_id": str(bundle.get("tenant_id") or ""),
        "tenant_name": str(bundle.get("tenant_name") or ""),
    }


def _step_customer_nginx(
    root: Path,
    job_req: dict[str, Any],
    *,
    pinned_host: PinnedCustomerHost,
) -> dict[str, Any]:
    """在客户机宝塔/Nginx 扩展目录部署工具入口 + 正式用户入口，含 nginx -t 自动回滚守卫。"""
    env = str(job_req["environment"])
    api_domain = str(job_req["api_domain"])
    local_port = int(job_req.get("local_port") or 8788)
    tool_conf = "\n".join(
        [
            f"# AI-Ops managed tool ingress for {env} ({api_domain})",
            "location ^~ /_aiops_internal/ {",
            "    allow 120.55.45.59;",
            "    allow 124.243.178.156;",
            "    allow 127.0.0.1;",
            "    deny all;",
            "    if ($request_method !~ ^(GET|POST)$) { return 405; }",
            f"    proxy_pass http://127.0.0.1:{local_port};",
            "    proxy_set_header Host $host;",
            "    proxy_set_header X-Real-IP $remote_addr;",
            "}",
            "",
        ]
    )
    user_tpl = (root / "ops/nginx/environment-user-ingress.conf").read_text(encoding="utf-8")
    inc_tpl = (root / "ops/nginx/environment-user-ingress-proxy.inc").read_text(encoding="utf-8")
    user_conf = user_tpl.replace("__ENV__", env).replace(
        "http://127.0.0.1:8788", f"http://127.0.0.1:{local_port}"
    )
    proxy_inc = inc_tpl.replace("http://127.0.0.1:8788", f"http://127.0.0.1:{local_port}")

    ext_dir = f"/www/server/panel/vhost/nginx/extension/{api_domain}"
    render_script = root / "tools/content/render_node_callback.py"
    with tempfile.TemporaryDirectory(prefix="aiops-cust-ngx-") as tmpdir:
        tmp = Path(tmpdir)
        if render_script.is_file():
            tool_conf_file = tmp / "aiops-tool-ingress.conf"
            subprocess.run(
                [
                    sys.executable,
                    str(render_script),
                    "--ingress",
                    "--gateway",
                    f"http://127.0.0.1:{local_port}",
                    "--allow-source",
                    "120.55.45.59",
                    "--allow-source",
                    "124.243.178.156",
                    "--out",
                    str(tool_conf_file),
                ],
                cwd=str(root),
                env={**os.environ, "PYTHONPATH": str(root / "src")},
                check=True,
                capture_output=True,
                text=True,
            )
            rendered_tool_conf = tool_conf_file.read_text(encoding="utf-8")
        else:
            rendered_tool_conf = tool_conf
        for name, content in (
            ("aiops-tool-ingress.conf", rendered_tool_conf),
            ("aiops-user-ingress.conf", user_conf),
            ("environment-user-ingress-proxy.inc", proxy_inc),
        ):
            f = tmp / name
            f.write_text(content, encoding="utf-8")
            pinned_host.copy_file(f, f"/tmp/wizard-{env}-{name}")

    apply_cmd = f"""
set -euo pipefail
NGINX_BIN="$(command -v nginx || echo /www/server/nginx/sbin/nginx)"
mkdir -p {shlex.quote(ext_dir)} /etc/aiops-access-{env}
bak_dir=$(mktemp -d)
trap 'rm -rf "$bak_dir"' EXIT
for f in aiops-tool-ingress.conf aiops-user-ingress.conf; do
    [ -f "{ext_dir}/$f" ] && cp -a "{ext_dir}/$f" "$bak_dir/$f" || true
done
cp -f /tmp/wizard-{env}-aiops-tool-ingress.conf "{ext_dir}/aiops-tool-ingress.conf"
cp -f /tmp/wizard-{env}-aiops-user-ingress.conf "{ext_dir}/aiops-user-ingress.conf"
cp -f "/tmp/wizard-{env}-environment-user-ingress-proxy.inc" \\
    "/etc/aiops-access-{env}/environment-user-ingress-proxy.inc"
chmod 644 "{ext_dir}/aiops-tool-ingress.conf" \\
    "{ext_dir}/aiops-user-ingress.conf" \\
    "/etc/aiops-access-{env}/environment-user-ingress-proxy.inc"
rm -f "/tmp/wizard-{env}-aiops-tool-ingress.conf" \\
    "/tmp/wizard-{env}-aiops-user-ingress.conf" \\
    "/tmp/wizard-{env}-environment-user-ingress-proxy.inc"
if ! "$NGINX_BIN" -t >/dev/null 2>&1; then
    for f in aiops-tool-ingress.conf aiops-user-ingress.conf; do
        if [ -f "$bak_dir/$f" ]; then cp -f "$bak_dir/$f" "{ext_dir}/$f"; else rm -f "{ext_dir}/$f"; fi
    done
    exit 2
fi
"$NGINX_BIN" -s reload
"""
    res = pinned_host.run_cmd(apply_cmd, timeout_sec=60)
    if res.returncode != 0:
        return {"ok": False, "rolled_back": True, "reason": "CUSTOMER_NGINX_DEPLOY_FAILED"}
    return {"ok": True}


def _step_live_probe(gateway_url: str, *, caller: public_smoke.HttpCaller | None = None) -> dict[str, Any]:
    http_fn = caller or public_smoke._default_http_caller
    base = gateway_url.strip().rstrip("/")
    anon_code, _ = http_fn(
        "POST",
        f"{base}/v1/assistant/questions",
        {"Content-Type": "application/json"},
        {"question": "live-probe-check"},
    )
    upms_code, _ = http_fn("GET", f"{base}/upms/tenant/list", {"Accept": "application/json"}, None)
    ok = anon_code == 401 and upms_code == 200
    return {
        "ok": ok,
        "anon_assistant_status": anon_code,
        "upms_tenant_list_status": upms_code,
        "reason": "" if ok else "LIVE_PROBE_CONTRACT_MISMATCH",
    }


def execute_infra_provision_job(
    root: Path,
    central_target: str,
    job: dict[str, Any],
    *,
    request: Callable[[dict[str, Any]], dict[str, Any]],
    pinned_factory: Callable[..., PinnedCustomerHost] | None = None,
    live_probe_fn: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """顺序执行 5 项基建配套并逐子步回写进度，任一步失败即止。"""
    job_id = str(job["job_id"])
    req = dict(job.get("request") or {})
    env = str(job["environment"])
    steps = [
        {"step": name, "state": "pending", "reason": ""}
        for name in (
            "allowlist",
            "execution_node",
            "cold_install_and_onboard",
            "customer_nginx",
            "live_probe",
        )
    ]

    def _update(idx: int, step_state: str, reason: str = "", extra: dict[str, Any] | None = None) -> None:
        steps[idx]["state"] = step_state
        steps[idx]["reason"] = reason
        if extra:
            for k, v in extra.items():
                if k not in {"step", "state", "reason"}:
                    steps[idx][k] = v
        request(
            {
                "wizard_job": "progress",
                "job_id": job_id,
                "step": steps[idx]["step"],
                "step_state": step_state,
                "steps": steps,
                **({"result_patch": extra} if extra else {}),
            }
        )

    def _fail_step(
        idx: int,
        reason: str,
        *,
        result: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _update(idx, "failed", reason, extra)
        return request(
            {
                "wizard_job": "complete",
                "job_id": job_id,
                "passed": False,
                "error_code": reason,
                "failed_step": steps[idx]["step"],
                "steps": steps,
                "result": result_summary if result is None else result,
            }
        )

    def _err_code(exc: Exception, default: str) -> str:
        return (str(exc).strip()[:120] or default) if isinstance(exc, WizardWorkerError) else default

    result_summary: dict[str, Any] = {}
    try:
        pinned_host = (pinned_factory or PinnedCustomerHost.from_central)(central_target, env, cwd=root)
    except Exception as exc:
        return _fail_step(0, _err_code(exc, "ENVIRONMENT_HOST_LOAD_FAILED"))

    _update(0, "running")
    try:
        r0 = _step_allowlist(root, central_target, str(req["egress_ip"]))
    except Exception as exc:
        return _fail_step(0, _err_code(exc, "ALLOWLIST_EXECUTION_ERROR"))
    if not r0.get("ok"):
        return _fail_step(0, str(r0.get("reason") or "ALLOWLIST_FAILED"))
    _update(0, "passed")

    _update(1, "running")
    try:
        r1 = _step_execution_node(root, central_target, environment=env, api_domain=str(req["api_domain"]))
    except Exception as exc:
        return _fail_step(1, _err_code(exc, "EXECUTION_NODE_ERROR"))
    if not r1.get("ok"):
        return _fail_step(1, str(r1.get("reason") or "EXECUTION_NODE_FAILED"))
    _update(1, "passed")

    _update(2, "running")
    try:
        r2 = _step_cold_install_and_onboard(root, central_target, request, req, pinned_host=pinned_host)
    except Exception as exc:
        return _fail_step(2, str(exc) or "COLD_INSTALL_FAILED")
    for k in (
        "consumer_kb_id",
        "operator_kb_id",
        "workspace_id",
        "consumer_app_id",
        "operator_app_id",
        "tenant_id",
        "tenant_name",
    ):
        if r2.get(k):
            result_summary[k] = r2[k]
    _update(2, "passed", extra=result_summary)

    _update(3, "running")
    try:
        r3 = _step_customer_nginx(root, req, pinned_host=pinned_host)
    except Exception as exc:
        return _fail_step(3, _err_code(exc, "CUSTOMER_NGINX_ERROR"))
    if not r3.get("ok"):
        return _fail_step(3, str(r3.get("reason") or "CUSTOMER_NGINX_FAILED"))
    if result_summary.get("workspace_id") and result_summary.get("consumer_kb_id"):
        try:
            conn_res = request(
                {
                    "wizard_job": "connect_consumer_plugin",
                    "environment": env,
                    "api_domain": str(req["api_domain"]),
                    "tenant_id": str(result_summary.get("tenant_id") or req.get("tenant_id") or ""),
                    "workspace_id": str(result_summary["workspace_id"]),
                    "consumer_kb_id": str(result_summary["consumer_kb_id"]),
                }
            )
        except Exception as exc:
            return _fail_step(3, _err_code(exc, "CONSUMER_PLUGIN_CONNECT_FAILED"))
        if not isinstance(conn_res, dict) or not conn_res.get("ok"):
            return _fail_step(3, "CONSUMER_PLUGIN_CONNECT_FAILED")
    _update(3, "passed")

    _update(4, "running")
    try:
        probe = (live_probe_fn or _step_live_probe)(str(req["gateway_url"]))
    except Exception as exc:
        return _fail_step(4, _err_code(exc, "LIVE_PROBE_ERROR"))
    probe_meta = {
        "anon_assistant_status": probe.get("anon_assistant_status"),
        "upms_tenant_list_status": probe.get("upms_tenant_list_status"),
    }
    if not probe.get("ok"):
        return _fail_step(
            4,
            str(probe.get("reason") or "LIVE_PROBE_FAILED"),
            result={**result_summary, **probe_meta},
            extra=probe_meta,
        )
    _update(4, "passed", extra=probe_meta)
    return request(
        {
            "wizard_job": "complete",
            "job_id": job_id,
            "passed": True,
            "steps": steps,
            "result": {**result_summary, **probe_meta},
        }
    )


def execute_publish_environment_job(
    root: Path,
    central_target: str,
    job: dict[str, Any],
    *,
    request: Callable[[dict[str, Any]], dict[str, Any]],
    pinned_factory: Callable[..., PinnedCustomerHost] | None = None,
) -> dict[str, Any]:
    """执行 Step 5 正式发布：启用 41 主目录 + 同步客户机活跃目录并重载验证。"""
    job_id = str(job["job_id"])
    req = dict(job.get("request") or {})
    env = str(job["environment"])
    steps = [
        {"step": "enable_registry", "state": "pending", "reason": ""},
        {"step": "sync_target_runtime", "state": "pending", "reason": ""},
        {"step": "activate_node_route", "state": "pending", "reason": ""},
    ]

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

    def _fail_publish(idx: int, reason: str) -> dict[str, Any]:
        _update(idx, "failed", reason)
        return request(
            {
                "wizard_job": "complete",
                "job_id": job_id,
                "passed": False,
                "error_code": reason,
                "failed_step": steps[idx]["step"],
                "steps": steps,
            }
        )

    def _pub_err(exc: Exception, default: str) -> str:
        return (str(exc).strip()[:120] or default) if isinstance(exc, WizardWorkerError) else default

    _update(0, "running")
    try:
        activated = request({"wizard_job": "activate_registry", **req})
    except Exception as exc:
        return _fail_publish(0, _pub_err(exc, "ACTIVATE_REGISTRY_ERROR"))
    if not isinstance(activated, dict) or not activated.get("ok"):
        return _fail_publish(0, "ACTIVATE_REGISTRY_FAILED")
    _update(0, "passed")

    _update(1, "running")
    try:
        pinned_host = (pinned_factory or PinnedCustomerHost.from_central)(central_target, env, cwd=root)
        with tempfile.TemporaryDirectory(prefix="aiops-pub-sync-") as tmpdir:
            tmp = Path(tmpdir)
            env_json = tmp / "environment.json"
            env_json.write_bytes(
                _dev_host_read(central_target, str(activated["environment_json_path"]), cwd=root)
            )
            env_json.chmod(0o600)
            pinned_host.copy_file(env_json, f"/tmp/wizard-{env}-active-environment.json")
        local_port = int(req.get("local_port") or 8788)
        target_env_json = f"/etc/aiops-access-{env}/registry/environment.json"
        sync_cmds = [
            "set -euo pipefail",
            f"mv -f /tmp/wizard-{env}-active-environment.json {target_env_json}",
            f"chown aiops-access-{env}:aiops-access-{env} {target_env_json}",
            f"chmod 600 {target_env_json}",
            f"systemctl restart aiops-access-{env}.service",
            f"for _i in $(seq 1 20); do curl -fsS http://127.0.0.1:{local_port}/health "
            "&& break; sleep 1; done",
            f"curl -fsS http://127.0.0.1:{local_port}/health",
        ]
        sync_res = pinned_host.run_cmd("; ".join(sync_cmds), timeout_sec=120)
    except Exception as exc:
        return _fail_publish(1, _pub_err(exc, "TARGET_SYNC_ERROR"))
    if sync_res.returncode != 0:
        return _fail_publish(1, "TARGET_SYNC_FAILED")
    _update(1, "passed")

    _update(2, "running")
    try:
        node_chk = _dev_host_exec(
            central_target,
            f"systemctl is-active --quiet aiops-environment-node@{env}.service",
            timeout_sec=30,
            cwd=root,
        )
    except Exception as exc:
        return _fail_publish(2, _pub_err(exc, "NODE_CHECK_ERROR"))
    if node_chk.returncode != 0:
        return _fail_publish(2, "NODE_NOT_ACTIVE")
    _update(2, "passed")
    return request(
        {
            "wizard_job": "complete",
            "job_id": job_id,
            "passed": True,
            "steps": steps,
            "result": {
                "published": True,
                "tenant_id": req.get("tenant_id", ""),
                "gateway_url": req.get("gateway_url", ""),
                "registry_revision": activated.get("registry_revision"),
            },
        }
    )


def run_pending_wizard_jobs(
    root: str | Path,
    central_target: str,
    *,
    request: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any] | None:
    """在空闲轮询周期认领并执行下一条 V2.1 异步向导任务。"""
    claim_resp = request({"wizard_job": "claim"})
    if not isinstance(claim_resp, dict):
        return None
    job = claim_resp.get("job")
    if not isinstance(job, dict) or not job.get("job_id"):
        return None
    root_path = Path(root)
    action = str(job.get("action") or "")
    if action == "infra_provision":
        return execute_infra_provision_job(root_path, central_target, job, request=request)
    if action == "publish_environment":
        return execute_publish_environment_job(root_path, central_target, job, request=request)
    return request(
        {
            "wizard_job": "complete",
            "job_id": str(job["job_id"]),
            "passed": False,
            "error_code": "ENVIRONMENT_WIZARD_ACTION_UNSUPPORTED",
        }
    )
