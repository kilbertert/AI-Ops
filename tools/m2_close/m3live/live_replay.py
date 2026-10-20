#!/usr/bin/env python3
"""隔离栈的**一次真实派发**与按 run 采证（M3-LIVE-REPLAY-R1）。

这不是验收框架，是四个已经用过、但没有入仓的动作的最小实现：

- `baseline`：记下**生产面**的当前事实（服务启动时刻、配置内容摘要、`agent_runs` 行数）。
  「down 后生产没变」需要一份**之前**的值，否则只能写「看起来没变」。
- `send`：用构造身份发起**一次**委派读取，然后把回执落盘。
  只发一次是规则，不是习惯：失败必须非零，**不重试**。
- `collect`：按 run 取证据：记录摘要、与该 run 有关的**计数与标记**。
  「领取成功」与「整轮成功」是两件事，证据必须按同一个 run 绑在一起。
- `cleanup`：核对 down 之后容器 0、8794 释放，并与 baseline 对照生产面。
  停栈由 `checks.sh down` 做；这里只判「真的清掉了」。

四条硬约束，都由代码而不是由文档保证：

1. **握手令牌只从 0600 文件读**（`$M2LIVE_CONFIG/gateway/identity.env`），不接受命令行参数、
   不接受环境变量 —— 前者进 shell 历史与 `ps`，后者会被任何子进程继承。
   订单号同样只从 0600 文件读。
2. **发一次是独占的**。发送**之前**在输出目录里原子创建 `.send.claim`；创建失败即拒绝。
   标记一旦落盘就不再删除：网络超时、进程被杀、并发第二个进程，都留下「已发过、结果
   不确定」的状态，重跑一律拒绝 —— 换一次验证就换一个输出目录。
3. **不跟随重定向、只打回环**。`urllib` 默认跟随 302，会把 `Authorization` 带去另一个
   地址；这里装一个**拒绝重定向**的 opener，并要求 base URL 是 http(s) + 回环主机，
   不带 userinfo、不带路径。订单号做 URL 编码。
4. **敏感输出不外泄**。证据里出现的令牌、`sk-` 形式的供应商 key、`Authorization` 头一律
   替换成 `***`；写盘前断言令牌不在正文里。业务快照正文、订单号、构造身份的租户/用户
   都不进证据 —— 只留**摘要素与计数**。

退出码：0 通过；1 判据不成立；2 前置不足；3 拒绝执行（重复发送、路径/输入不安全）。

用法（宿主上；配置根与 `checks.sh` 相同）：

    export M2LIVE_CONFIG=/etc/aiops-m2-live-m3
    export M2LIVE_STATE=/var/lib/aiops-m2-live-m3
    python3 tools/m2_close/m3live/live_replay.py baseline --out evidence/prod-before.json
    python3 tools/m2_close/m3live/live_replay.py send --order-file ~/m3-order --out evidence/
    python3 tools/m2_close/m3live/live_replay.py collect --run-id run_… --out evidence/
    # checks.sh down 之后
    python3 tools/m2_close/m3live/live_replay.py cleanup --out evidence/ \\
        --prod-baseline evidence/prod-before.json
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = Path(os.environ.get("M2LIVE_CONFIG", "/etc/aiops-m2-live"))
DEFAULT_STATE = Path(os.environ.get("M2LIVE_STATE", "/var/lib/aiops-m2-live"))
DEFAULT_BASE_URL = "http://127.0.0.1:8794"
DEFAULT_PROJECT = os.environ.get("M2LIVE_PROJECT", "m2-live-03")
#: 生产面（只读核对）。路径是部署件里写死的，不是可配置项。
PROD_UNIT = "aiops-gateway-41.service"
PROD_CONFIG_DIR = Path("/etc/aiops-41")
PROD_DATABASE = Path("/var/lib/aiops-41/gateway/gateway.db")

#: 与 Gateway 侧同一条订单号判据（`gateway_api.SAFE_ORDER_NO`）。
SAFE_ORDER_NO = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
#: 与节点侧同一条 run_id 判据（`ReportingChatSessionRuntime.runIdOf`）。
SAFE_RUN_ID = re.compile(r"^run_[A-Za-z0-9_-]{20,64}$")

#: 证据里必须抹掉的形状。供应商 key 只在出口里，但错误正文会转述它。
SECRET_PATTERNS = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-***"),
    (re.compile(r"(?i)(authorization\s*:\s*)(\S+)"), r"\1***"),
    (re.compile(r"(?i)(bearer\s+)(\S+)"), r"\1***"),
)

#: 记录里可以进证据的列 —— **白名单**，不是「把 scope_json 拿掉就行」。
#: `order_no`（个人业务标识）、`scope_json`（构造身份的租户/用户）、`snapshot_json`
#: （业务正文）永远不出现；它们各自以摘要或存在性进入证据。
RECORD_FIELDS = (
    "run_id",
    "state",
    "reason",
    "operation",
    "environment",
    "app_id",
    "agent_id",
    "expected_node_id",
    "actual_node_id",
    "node_build",
    "gateway_version",
    "created_at",
    "expires_at",
    "claimed_at",
    "completed_at",
    "expected_agent_version",
    "observed_agent_version",
    "exec_config_state",
    "scope_fingerprint",
)

#: 日志里允许被**计数**的标记。正文本身不进证据：日志行可能带业务内容。
LOG_MARKERS = (
    "sid=",
    "exec_config",
    "manifest",
    "claim",
    "completion",
    "OPTIONS",
    "AGENT_RUN_",
    "model_failed",
    "succeeded",
    "execution_guard",
)


class Refused(RuntimeError):
    """拒绝执行（不是判据失败）：重复发送、路径/输入不安全、文件权限不对。"""


class Incomplete(RuntimeError):
    """必需观测缺失。它必须变成非零退出，而不是被 compare 跳过。"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def redact(text: str, *, extra: str | None = None) -> str:
    """抹掉令牌与供应商 key。`extra` 是本进程知道的握手令牌（它自己不匹配任何模式）。"""
    if extra:
        text = text.replace(extra, "***")
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _private_file(path: Path, *, what: str) -> str:
    """读一个 0600 普通文件的文本。符号链接一律拒绝（读的是链接目标，而声称校验的是这个路径）。"""
    if not os.path.lexists(path):
        raise Refused(f"缺少 {path}（{what}）")
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise Refused(f"{path} 必须是普通文件（不是符号链接）")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise Refused(f"{path} 权限 {stat.S_IMODE(info.st_mode):04o} 过宽，要求 0600")
    return path.read_text(encoding="utf-8")


def _write_evidence(out: Path, name: str, payload: dict[str, Any], *, token: str) -> Path:
    """写 0600 证据。

    三件事都在**打开文件之前**做：名字里不许有路径分隔符、目标不是符号链接、
    父目录建好且 0700。`O_NOFOLLOW` 让「检查完之后被换成链接」这个窗口也关上。
    """
    if not name or "/" in name or name in {".", ".."} or "\0" in name:
        raise Refused(f"证据文件名不安全：{name!r}")
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    body = redact(body, extra=token)
    if token and token in body:
        raise Refused("证据里出现了握手令牌：拒绝写盘")
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    if out.is_symlink() or out.stat().st_mode & 0o077:
        raise Refused("证据目录必须是私有目录，不能是符号链接")
    path = out / name
    if os.path.lexists(path) and os.path.islink(path):
        raise Refused(f"证据路径是符号链接：{path}")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        os.fchmod(descriptor, 0o600)
        handle.write(body)
    return path


# ---------------------------------------------------------------- 身份与入口
def read_identity(config: Path) -> dict[str, str]:
    """从 `gateway/identity.env` 读身份三件套（0600、普通文件、非符号链接）。"""
    path = config / "gateway/identity.env"
    text = _private_file(path, what="键名见 deploy/m2-live-03/identity.env.template")
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    missing = [
        key for key in ("AIOPS_M2_LOCAL_TOKEN", "AIOPS_M2_TENANT", "AIOPS_M2_C_USER") if not values.get(key)
    ]
    if missing:
        raise Refused(f"{path} 缺少键: {', '.join(missing)}")
    return values


def read_order_no(path: Path) -> str:
    """订单号只从 0600 文件读：命令行会进 shell 历史与 `ps`。"""
    lines = [line.strip() for line in _private_file(path, what="一行订单号").splitlines() if line.strip()]
    if len(lines) != 1:
        raise Refused(f"{path} 必须恰好一行（现在是 {len(lines)} 行非空）")
    order_no = lines[0]
    if not SAFE_ORDER_NO.fullmatch(order_no):
        raise Refused(f"{path} 里的订单号不像订单号（只允许字母数字与 _.:- ，长度 ≤128）")
    return order_no


def validate_base_url(base_url: str) -> str:
    """只接受 http(s) + 回环主机，不带 userinfo、不带路径/查询。

    这条不是洁癖：`Bearer` 令牌就在这次请求里，一个可配置的 base URL 等于把「令牌发给谁」
    变成了参数。回环之外一律拒绝。
    """
    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme not in {"http", "https"}:
        raise Refused(f"base URL 必须是 http/https：{base_url}")
    if parts.username or parts.password:
        raise Refused("base URL 不得带 userinfo")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise Refused(f"base URL 只能是 scheme://host:port，不带路径：{base_url}")
    host = (parts.hostname or "").strip()
    if not host:
        raise Refused("base URL 没有主机名")
    loopback = host == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
    if not loopback:
        raise Refused(f"base URL 必须是回环地址（令牌只发给本机栈）：{host}")
    return base_url.rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """拒绝一切重定向。

    `urllib.request.urlopen` 默认跟随 302，并**保留** `Authorization` 头 —— 一次被指向
    另一台主机的跳转就把令牌带出了回环。这里让它变成错误，由调用方记成失败。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        raise urllib.error.HTTPError(req.full_url, code, f"redirect refused: {msg}", headers, fp)


def _post_delegated(*, base_url: str, order_no: str, token: str, timeout: float) -> tuple[int, Any]:
    """**一次**委派读取。不重试、不跟随重定向、不把令牌放进 URL。"""
    base = validate_base_url(base_url)
    url = f"{base}/v1/orders/{urllib.parse.quote(order_no, safe='')}/charging-status/delegated"
    request = urllib.request.Request(
        url,
        data=b"{}",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Business-Entry": "consumer",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:  # noqa: S310 - 回环，已校验
            return response.status, _load(response.read())
    except urllib.error.HTTPError as error:
        return error.code, _load(error.read())


def _load(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"raw_sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


# ---------------------------------------------------------------- 证据
def read_run(state: Path, run_id: str) -> dict[str, Any] | None:
    """按 run 读记录的**摘要**。只读打开（`mode=ro`）：采证不该顺带收敛过期记录。

    返回的字段来自 `RECORD_FIELDS` 白名单；业务正文只以 sha256 与键集合出现。
    """
    if not SAFE_RUN_ID.fullmatch(run_id):
        raise Refused(f"run_id 形状不对（拒绝用于路径）：{run_id!r}")
    database = state / "gateway/gateway.db"
    if not database.is_file():
        return None
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
    except sqlite3.DatabaseError:
        return None
    finally:
        connection.close()
    if row is None:
        return None
    raw = dict(row)
    summary = {field: raw.get(field) for field in RECORD_FIELDS if field in raw}
    snapshot = raw.get("snapshot_json")
    summary["has_snapshot"] = snapshot is not None
    summary["snapshot_sha256"] = hashlib.sha256(snapshot.encode()).hexdigest() if snapshot else None
    try:
        summary["snapshot_keys"] = sorted(json.loads(snapshot)) if snapshot else []
    except (TypeError, json.JSONDecodeError):
        summary["snapshot_keys"] = None
    return summary


def docker_logs(container: str) -> tuple[str, int]:
    """容器日志与退出码。退出码非零时调用方必须把它当成**观测失败**。"""
    result = subprocess.run(
        ["docker", "logs", "--tail", "400", container],
        capture_output=True,
        text=True,
        check=False,
    )
    return (result.stdout or "") + (result.stderr or ""), result.returncode


def summarise_run_log(text: str, run_id: str) -> dict[str, Any]:
    """只留**计数与标记**，不留正文。

    日志行里可能带业务正文与个人标识；证据要回答的是「这条 run 在日志里出现过几次、
    出现过哪几类事件」，不是一个可以被随手复制出去的正文副本。
    """
    matched = [line for line in text.splitlines() if run_id in line]
    markers: dict[str, int] = {}
    for line in matched:
        for marker in LOG_MARKERS:
            if marker in line:
                markers[marker] = markers.get(marker, 0) + 1
    return {"lines_matching_run": len(matched), "markers": markers}


def _banner_summary(lines: list[str]) -> dict[str, Any] | None:
    """启动器打印的「替换了什么 / 什么是真的」声明 —— 只留它的**键**与条目数。

    原文里有租户、C 端用户与绑定端口；证据只需要能回答「这一次确实是构造身份启动的」。
    """
    if not lines:
        return None
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError:
        return {"present": True, "parsed": False}
    if not isinstance(payload, dict):
        return {"present": True, "parsed": False}
    substituted = payload.get("substituted") or {}
    real = payload.get("real") or []
    return {
        "present": True,
        "parsed": True,
        "substituted_keys": sorted(substituted) if isinstance(substituted, dict) else [],
        "real_count": len(real) if isinstance(real, list) else 0,
    }


def collect(state: Path, run_id: str, *, gateway_container: str, node_container: str) -> dict[str, Any]:
    node_log, node_rc = docker_logs(node_container)
    gateway_log, gateway_rc = docker_logs(gateway_container)
    banner = [line for line in gateway_log.splitlines() if '"substituted"' in line]
    return {
        "run_id": run_id,
        "collected_at": _now(),
        "record": read_run(state, run_id),
        "node_log": summarise_run_log(node_log, run_id),
        "gateway_log": summarise_run_log(gateway_log, run_id),
        "log_observations_ok": node_rc == 0 and gateway_rc == 0,
        # 身份替换声明：只留它说「替换了什么/什么是真的」这一句的结构，不留原文副本。
        "identity_substitution_banner": _banner_summary(banner),
        # 观测边界，写在证据里而不是只写在文档里：出口只记录**拒绝与异常**，
        # 成功转发没有一行日志；节点也没有每轮模型调用的日志。
        "model_request_count": {
            "value": None,
            "observed": False,
            "why": "model-proxy 只记录异常；节点无每轮日志",
        },
    }


# ---------------------------------------------------------------- 生产面
def _run(argv: list[str], *, timeout: float = 20.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Incomplete(f"{argv[0]} 无法执行：{type(exc).__name__}") from exc


def _config_digests() -> dict[str, str]:
    """生产配置文件的**内容**摘要。

    用 mtime 不够：`cp -p`、备份还原、同一秒内的两次写入都会让 mtime 看起来一样或
    看起来变了，而我们要回答的是「文件内容变了没有」。
    """
    digests: dict[str, str] = {}
    for path in sorted(PROD_CONFIG_DIR.glob("*.env")):
        if path.is_symlink() or not path.is_file():
            raise Incomplete(f"生产配置文件不是普通文件：{path}")
        digests[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not digests:
        raise Incomplete(f"{PROD_CONFIG_DIR} 下没有任何 *.env：无法证明生产配置未变")
    return digests


def production_facts() -> dict[str, Any]:
    """生产面的只读事实。**任何一项拿不到都抛 `Incomplete`** —— 不用默认值假装拿到了。"""
    active = _run(["systemctl", "show", "-p", "ActiveEnterTimestamp", "--value", PROD_UNIT])
    if active.returncode != 0:
        raise Incomplete(f"systemctl show {PROD_UNIT} 退出 {active.returncode}")
    state = _run(["systemctl", "is-active", PROD_UNIT])
    if state.returncode != 0:
        raise Incomplete(f"systemctl is-active {PROD_UNIT} 退出 {state.returncode}")
    rows = _count_runs(PROD_DATABASE)
    if rows is None:
        raise Incomplete(f"读不到生产 agent_runs（{PROD_DATABASE}）")
    enter = active.stdout.strip()
    if not enter:
        raise Incomplete("systemctl 没有给出 ActiveEnterTimestamp")
    return {
        "captured_at": _now(),
        "unit_active_enter": enter,
        "unit_active_state": state.stdout.strip(),
        "config_sha256": _config_digests(),
        "agent_runs_rows": rows,
    }


def _count_runs(database: Path) -> int | None:
    if not database.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        return int(connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0])
    except sqlite3.Error:
        return None
    finally:
        connection.close()


# ---------------------------------------------------------------- 清理
def stack_containers(project: str) -> list[str]:
    """本栈容器名。docker 退出码非零 ⇒ `Incomplete`（不是「没有容器」）。"""
    result = _run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            "{{.Names}}",
        ]
    )
    if result.returncode != 0:
        raise Incomplete(f"docker ps 退出 {result.returncode}：无法确认容器是否已清空")
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def listening_on(port: int) -> list[str]:
    """监听 8794 的行。`ss` 退出码非零 ⇒ `Incomplete`。"""
    result = _run(["ss", "-lnt"])
    if result.returncode != 0:
        raise Incomplete(f"ss 退出 {result.returncode}：无法确认端口是否已释放")
    return [line for line in (result.stdout or "").splitlines() if f":{port} " in line]


def compare_production(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """逐项比较。**缺值不是「没变化」**：任一侧缺一项 ⇒ `Incomplete`。"""
    problems: list[str] = []
    for key in ("unit_active_enter", "unit_active_state", "config_sha256", "agent_runs_rows"):
        old, new = before.get(key), after.get(key)
        if old is None or new is None:
            raise Incomplete(f"生产面观测不完整：{key} 在基线或当前事实里缺失")
        if old != new:
            problems.append(f"{key}: {old!r} -> {new!r}")
    return problems


# ---------------------------------------------------------------- 子命令
def _claim_send_slot(out: Path) -> Path:
    """发送**之前**原子占用输出目录。

    `O_CREAT|O_EXCL` 是这里唯一可靠的原语：两个进程同时进来时只有一个能创建成功。
    标记**永不自动删除** —— 网络超时、进程被杀之后，这次派发到底发没发出去是**不确定**的，
    而「不确定」只能靠人工判断，工具不能替它猜。重跑一律拒绝；换一次验证就换一个目录。
    """
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    if out.is_symlink() or out.stat().st_mode & 0o077:
        raise Refused("证据目录必须是私有目录，不能是符号链接")
    if (out / "sent.json").exists():
        raise Refused("sent.json 已存在：不重发旧版本已发过的请求")
    claim = out / ".send.claim"
    if os.path.lexists(claim) and os.path.islink(claim):
        raise Refused(f"{claim} 是符号链接")
    try:
        descriptor = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise Refused(f"{claim} 已存在：本目录已经进入过一次派发（成功或结果不确定），不再重发") from exc
    try:
        os.write(descriptor, json.dumps({"claimed_at": _now(), "pid": os.getpid()}).encode())
    finally:
        os.close(descriptor)
    return claim


def cmd_send(args: argparse.Namespace) -> int:
    out = Path(args.out)
    base_url = validate_base_url(args.base_url)
    identity = read_identity(Path(args.config))
    order_no = read_order_no(Path(args.order_file))
    token = identity["AIOPS_M2_LOCAL_TOKEN"]

    # 顺序是硬的：先占位，再发。反过来（先发后写标记）在网络超时与并发下都会重复发。
    _claim_send_slot(out)

    status: int | None = None
    body: Any = None
    failure: str | None = None
    try:
        status, body = _post_delegated(
            base_url=base_url, order_no=order_no, token=token, timeout=args.timeout
        )
    except (OSError, http.client.HTTPException, urllib.error.URLError) as exc:
        failure = f"{type(exc).__name__}"

    receipt = {
        "sent_at": _now(),
        "base_url": base_url,
        "http_status": status,
        "failure": failure,
        # 回执正文：不含订单号（那是个人业务标识），也不含任何身份值。
        "body": _receipt_summary(body),
    }
    _write_evidence(out, "sent.json", receipt, token=token)
    if failure is not None or status != 200 or not isinstance(body, dict) or not body.get("run_id"):
        print(
            f"派发失败：HTTP {status}，failure={failure}（不重试；标记 .send.claim 保留，重跑会拒绝）",
            file=sys.stderr,
        )
        return 1
    run_id = str(body["run_id"])
    if not SAFE_RUN_ID.fullmatch(run_id):
        print(f"回执里的 run_id 形状不对：{run_id!r}（不据此写文件）", file=sys.stderr)
        return 1
    payload = collect(
        Path(args.state),
        run_id,
        gateway_container=args.gateway_container,
        node_container=args.node_container,
    )
    _write_evidence(out, f"{run_id}.collect.json", payload, token=token)
    record = payload["record"] or {}
    confirmed = (
        body.get("state") == record.get("state") == "succeeded"
        and record.get("has_snapshot") is True
        and record.get("exec_config_state") == "matched"
        and bool(record.get("expected_agent_version"))
        and record.get("expected_agent_version")
        == record.get("observed_agent_version")
        == body.get("agent_version")
    )
    print(json.dumps({"run_id": run_id, "state": body.get("state"), "confirmed": confirmed}))
    return 0 if confirmed else 1


def _receipt_summary(body: Any) -> dict[str, Any]:
    """回执 -> 证据里能留的那几项。业务正文与个人标识都不在里面。"""
    if not isinstance(body, dict):
        return {"kind": type(body).__name__}
    keep = (
        "run_id",
        "state",
        "reason",
        "operation",
        "environment",
        "agent_version_gap",
        "agent_version",
        "agent_version_observed",
        "actual_node_id",
        "node_build",
        "result_sha256",
        "claimed_at",
        "completed_at",
    )
    summary = {key: body.get(key) for key in keep if key in body}
    summary["keys"] = sorted(body)
    return summary


def cmd_collect(args: argparse.Namespace) -> int:
    run_id = args.run_id
    if not SAFE_RUN_ID.fullmatch(run_id):
        raise Refused(f"run_id 形状不对（拒绝用于路径）：{run_id!r}")
    token = read_identity(Path(args.config))["AIOPS_M2_LOCAL_TOKEN"]
    payload = collect(
        Path(args.state),
        run_id,
        gateway_container=args.gateway_container,
        node_container=args.node_container,
    )
    path = _write_evidence(Path(args.out), f"{run_id}.collect.json", payload, token=token)
    record = payload["record"] or {}
    print(json.dumps({"path": str(path), "state": record.get("state"), "reason": record.get("reason")}))
    return 0 if record else 1


def cmd_baseline(args: argparse.Namespace) -> int:
    out = Path(args.out)
    try:
        facts = production_facts()
    except Incomplete as exc:
        print(f"生产面观测不完整（{exc}）：没有基线就别说「生产没变」", file=sys.stderr)
        return 2
    _write_evidence(out.parent, out.name, facts, token="")
    print(json.dumps({"path": str(out), "unit_active_enter": facts["unit_active_enter"]}))
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    report: dict[str, Any] = {
        "checked_at": _now(),
        "project": args.project,
        "containers": None,
        "listeners_on_port": None,
        "production": None,
        "problems": [],
    }
    problems: list[str] = report["problems"]
    try:
        containers = stack_containers(args.project)
        report["containers"] = containers
        if containers:
            problems.append(f"仍有 {len(containers)} 个容器：{', '.join(containers)}")
    except Incomplete as exc:
        problems.append(str(exc))
    try:
        listeners = listening_on(args.port)
        report["listeners_on_port"] = listeners
        if listeners:
            problems.append(f"{args.port} 仍在监听")
    except Incomplete as exc:
        problems.append(str(exc))

    baseline_path = Path(args.prod_baseline)
    try:
        before = json.loads(_private_file(baseline_path, what="baseline 由 baseline 子命令写出"))
    except (Refused, json.JSONDecodeError) as exc:
        problems.append(f"生产面基线不可用：{exc}")
        report["production"] = {"compared": False, "why": str(exc)}
    else:
        try:
            after = production_facts()
            drift = compare_production(before, after)
        except Incomplete as exc:
            problems.append(f"生产面观测不完整：{exc}")
            report["production"] = {"compared": False, "why": str(exc)}
        else:
            report["production"] = {"compared": True, "drift": drift, "after": after}
            problems.extend(f"生产面变化：{item}" for item in drift)

    _write_evidence(Path(args.out), "cleanup.json", report, token="")
    if problems:
        print("; ".join(problems), file=sys.stderr)
        return 1
    print(json.dumps({"cleanup": "ok", "production": report["production"]["compared"]}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(target: argparse.ArgumentParser) -> None:
        target.add_argument("--config", default=str(DEFAULT_CONFIG))
        target.add_argument("--state", default=str(DEFAULT_STATE))
        target.add_argument("--out", required=True)
        target.add_argument("--gateway-container", default="m2live03-gateway")
        target.add_argument("--node-container", default="m2live03-node")

    send = sub.add_parser("send", help="发起一次委派读取（不重试）")
    common(send)
    # 订单号只在 0600 文件里：命令行会进 shell 历史与 `ps`。
    send.add_argument("--order-file", required=True, help="含一行订单号的 0600 文件")
    send.add_argument("--base-url", default=DEFAULT_BASE_URL)
    send.add_argument("--timeout", type=float, default=90.0)
    send.set_defaults(func=cmd_send)

    collect_parser = sub.add_parser("collect", help="按 run 收集证据")
    common(collect_parser)
    collect_parser.add_argument("--run-id", required=True)
    collect_parser.set_defaults(func=cmd_collect)

    baseline = sub.add_parser("baseline", help="记下生产面事实（只读）")
    baseline.add_argument("--out", required=True)
    baseline.set_defaults(func=cmd_baseline)

    cleanup = sub.add_parser("cleanup", help="核对 down 之后容器/端口/生产面")
    cleanup.add_argument("--out", required=True)
    cleanup.add_argument("--project", default=DEFAULT_PROJECT)
    cleanup.add_argument("--port", type=int, default=8794)
    # 基线是**必需**的：没有它，「生产没变」这句话就没有出处。
    cleanup.add_argument("--prod-baseline", required=True)
    cleanup.set_defaults(func=cmd_cleanup)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except Refused as exc:
        print(f"拒绝：{exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
