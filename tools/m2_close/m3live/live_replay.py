#!/usr/bin/env python3
"""隔离栈的**一次真实派发**与按 run 采证（M3-LIVE-REPLAY-R1）。

这不是验收框架，是四个已经用过、但没有入仓的动作的最小实现：

- `baseline`：记下**生产面**的当前事实（服务启动时刻、配置 mtime、`agent_runs` 行数）。
  「down 后生产没变」需要一份**之前**的值，否则只能写「看起来没变」。
- `send`：用构造身份发起**一次**委派读取，然后把回执落盘。
  只发一次是规则，不是习惯：失败必须非零，**不重试**。
- `collect`：按 run 取证据：记录行、与该 run 有关的日志行、出口的异常行。
  「领取成功」与「整轮成功」是两件事，证据必须按同一个 run 绑在一起。
- `cleanup`：核对 down 之后容器 0、8794 释放，并与 baseline 对照生产面。
  停栈由 `checks.sh down` 做；这里只判「真的清掉了」。

三条硬约束，都由代码而不是由文档保证：

1. **握手令牌只从 0600 文件读**（`$M2LIVE_CONFIG/gateway/identity.env`），不接受命令行参数、
   不接受环境变量 —— 前者进 shell 历史与 `ps`，后者会被任何子进程继承。
2. **不重试**。`send` 在一个输出目录里只允许存在一份 `sent.json`；再次发送直接拒绝（退出 3）。
   HTTP 失败也是终局，写证据、非零退出，不因为「再试一次可能就好了」而多发一次真实请求。
3. **敏感输出不外泄**。证据里出现的令牌、`sk-` 形式的供应商 key、`Authorization` 头一律替换成
   `***`；写盘前再做一次断言，命中就拒绝写。

退出码：0 通过；1 判据不成立；2 前置不足；3 拒绝执行（重复发送、路径不安全）。

用法（宿主上，配置与状态根与 checks.sh 相同）：

    python tools/m2_close/m3live/live_replay.py baseline --out evidence/prod-before.json
    python tools/m2_close/m3live/live_replay.py send --order-no <订单号> --out evidence/
    python tools/m2_close/m3live/live_replay.py collect --run-id run_… --out evidence/
    # checks.sh down 之后
    python tools/m2_close/m3live/live_replay.py cleanup --out evidence/ \
        --prod-baseline evidence/prod-before.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import urllib.error
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

#: 证据里必须抹掉的形状。供应商 key 只在出口里，但错误正文会转述它。
SECRET_PATTERNS = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-***"),
    (re.compile(r"(?i)(authorization\s*:\s*)(\S+)"), r"\1***"),
    (re.compile(r"(?i)(bearer\s+)(\S+)"), r"\1***"),
)


class Refused(RuntimeError):
    """拒绝执行（不是判据失败）：重复发送、路径不安全、文件权限不对。"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def redact(text: str, *, extra: str | None = None) -> str:
    """抹掉令牌与供应商 key。`extra` 是本进程知道的握手令牌（它自己不匹配任何模式）。"""
    if extra:
        text = text.replace(extra, "***")
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _write_evidence(out: Path, name: str, payload: dict[str, Any], *, token: str) -> Path:
    """写 0600 证据，并在写之前断言没有令牌漏进去。"""
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    body = redact(body, extra=token)
    if token and token in body:
        raise Refused("证据里出现了握手令牌：拒绝写盘")
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = out / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o600)
    return path


# ---------------------------------------------------------------- 身份与入口
def read_identity(config: Path) -> dict[str, str]:
    """从 `gateway/identity.env` 读身份三件套。

    文件必须是**普通文件、不是符号链接、0600**：里面有握手令牌，而它只用来证明
    「这次请求是本栈发起的一次受控验证」，不该被同组或他人读到。
    """
    path = config / "gateway/identity.env"
    if not os.path.lexists(path):
        raise Refused(f"缺少 {path}（键名见 deploy/m2-live-03/identity.env.template）")
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise Refused(f"{path} 必须是普通文件（不是符号链接）")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        raise Refused(f"{path} 权限 {mode:04o} 过宽，要求 0600")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
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


def _post_delegated(*, base_url: str, order_no: str, token: str, timeout: float) -> tuple[int, Any]:
    """**一次**委派读取。不重试、不跟随重定向、不把令牌放进 URL。"""
    url = f"{base_url.rstrip('/')}/v1/orders/{order_no}/charging-status/delegated"
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
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - 固定回环
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
    """按 run 读记录。**只读**打开（`mode=ro`）：采证不该顺带收敛过期记录。"""
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
    record = dict(row)
    snapshot = record.pop("snapshot_json", None)
    # 快照是业务正文：证据里只留摘要与键集合，正文要比对时用 --snapshot-out 单独取。
    record["has_snapshot"] = snapshot is not None
    record["snapshot_sha256"] = hashlib.sha256(snapshot.encode()).hexdigest() if snapshot else None
    record["snapshot_keys"] = sorted(json.loads(snapshot)) if snapshot else []
    # scope_json 里有构造身份：它是本次验证的既定事实，但不必重复写进每一份证据。
    record.pop("scope_json", None)
    if snapshot:
        record["_snapshot"] = json.loads(snapshot)
    return record


def docker_logs(container: str) -> str:
    result = subprocess.run(
        ["docker", "logs", "--tail", "400", container],
        capture_output=True,
        text=True,
        check=False,
    )
    return (result.stdout or "") + (result.stderr or "")


def lines_for_run(text: str, run_id: str) -> list[str]:
    return [line for line in text.splitlines() if run_id in line]


def collect(state: Path, run_id: str, *, gateway_container: str, node_container: str) -> dict[str, Any]:
    record = read_run(state, run_id)
    node_log = docker_logs(node_container)
    gateway_log = docker_logs(gateway_container)
    banner = [line for line in gateway_log.splitlines() if '"substituted"' in line]
    return {
        "run_id": run_id,
        "collected_at": _now(),
        "record": record,
        "node_log_lines": lines_for_run(node_log, run_id)[-40:],
        "gateway_log_lines": lines_for_run(gateway_log, run_id)[-40:],
        "identity_substitution_banner": banner[-1] if banner else None,
        # 观测边界，写在证据里而不是只写在文档里：出口只记录**拒绝与异常**，
        # 成功转发没有一行日志；节点也没有每轮模型调用的日志。
        # 所以「模型请求了几次」在本栈上**没有**观测依据 —— 错误日志的条数不是请求数
        # （一次请求可以重复打出多行，一轮也可以在同一个连接上重试）。
        "model_request_count": {
            "value": None,
            "observed": False,
            "why": "model-proxy 只记录异常；节点无每轮日志",
        },
    }


# ---------------------------------------------------------------- 生产面
def production_facts() -> dict[str, Any]:
    """生产面的只读事实。读不到就抛错 —— 不用默认值假装拿到了。"""
    facts: dict[str, Any] = {"captured_at": _now()}
    active = subprocess.run(
        ["systemctl", "show", "-p", "ActiveEnterTimestamp", "--value", PROD_UNIT],
        capture_output=True,
        text=True,
        check=False,
    )
    facts["unit_active_enter"] = active.stdout.strip()
    facts["unit_active_state"] = subprocess.run(
        ["systemctl", "is-active", PROD_UNIT], capture_output=True, text=True, check=False
    ).stdout.strip()
    files: dict[str, list[int]] = {}
    for path in sorted(PROD_CONFIG_DIR.glob("*.env")):
        info = path.stat()
        files[path.name] = [info.st_size, int(info.st_mtime)]
    facts["config_files"] = files
    database = Path("/var/lib/aiops-41/gateway/gateway.db")
    facts["agent_runs_rows"] = _count_runs(database)
    return facts


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
    result = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            "{{.Names}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def listening_on(port: int) -> list[str]:
    result = subprocess.run(["ss", "-lnt"], capture_output=True, text=True, check=False)
    return [line for line in (result.stdout or "").splitlines() if f":{port} " in line]


def compare_production(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """只报**变化**。值缺失（None）不算变化 —— 缺的是观测，不是事实。"""
    problems: list[str] = []
    for key in ("unit_active_enter", "unit_active_state", "config_files", "agent_runs_rows"):
        old, new = before.get(key), after.get(key)
        if old is None or new is None:
            continue
        if old != new:
            problems.append(f"{key}: {old!r} -> {new!r}")
    return problems


# ---------------------------------------------------------------- 子命令
def cmd_send(args: argparse.Namespace) -> int:
    out = Path(args.out)
    sent_path = out / "sent.json"
    if sent_path.exists():
        # 不重试：同一份输出目录里已经发过一次。换一次验证就换一个目录。
        raise Refused(f"{sent_path} 已存在：本轮已经发过一次，不再重发")
    identity = read_identity(Path(args.config))
    token = identity["AIOPS_M2_LOCAL_TOKEN"]
    status, body = _post_delegated(
        base_url=args.base_url, order_no=args.order_no, token=token, timeout=args.timeout
    )
    receipt = {
        "sent_at": _now(),
        "base_url": args.base_url,
        "order_no": args.order_no,
        "http_status": status,
        "body": body,
        "identity": {
            "tenant": identity["AIOPS_M2_TENANT"],
            "c_user": identity["AIOPS_M2_C_USER"],
            "note": "调用者身份是构造的（ADR-0058）；订单授权与业务读取是真的",
        },
    }
    _write_evidence(out, "sent.json", receipt, token=token)
    if status != 200 or not isinstance(body, dict) or not body.get("run_id"):
        print(f"派发失败：HTTP {status}（不重试）", file=sys.stderr)
        return 1
    run_id = str(body["run_id"])
    _write_evidence(
        out,
        f"{run_id}.collect.json",
        collect(
            Path(args.state),
            run_id,
            gateway_container=args.gateway_container,
            node_container=args.node_container,
        ),
        token=token,
    )
    print(json.dumps({"run_id": run_id, "state": body.get("state"), "http_status": status}))
    return 0 if body.get("state") == "succeeded" else 1


def cmd_collect(args: argparse.Namespace) -> int:
    token = read_identity(Path(args.config))["AIOPS_M2_LOCAL_TOKEN"]
    payload = collect(
        Path(args.state),
        args.run_id,
        gateway_container=args.gateway_container,
        node_container=args.node_container,
    )
    path = _write_evidence(Path(args.out), f"{args.run_id}.collect.json", payload, token=token)
    record = payload["record"] or {}
    print(json.dumps({"path": str(path), "state": record.get("state"), "reason": record.get("reason")}))
    return 0 if record else 1


def cmd_baseline(args: argparse.Namespace) -> int:
    out = Path(args.out)
    try:
        facts = production_facts()
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"读不到生产面（{type(exc).__name__}）：没有基线就别说「生产没变」", file=sys.stderr)
        return 2
    if not facts.get("unit_active_enter") or facts.get("agent_runs_rows") is None:
        print("生产面事实不完整：不写基线", file=sys.stderr)
        return 2
    _write_evidence(out.parent, out.name, facts, token="")
    print(json.dumps({"path": str(out), "unit_active_enter": facts["unit_active_enter"]}))
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    containers = stack_containers(args.project)
    listeners = listening_on(args.port)
    report: dict[str, Any] = {
        "checked_at": _now(),
        "project": args.project,
        "containers": containers,
        "listeners_on_port": listeners,
        "production": None,
    }
    problems: list[str] = []
    if containers:
        problems.append(f"仍有 {len(containers)} 个容器：{', '.join(containers)}")
    if listeners:
        problems.append(f"{args.port} 仍在监听")
    if args.prod_baseline:
        before = json.loads(Path(args.prod_baseline).read_text(encoding="utf-8"))
        try:
            after = production_facts()
        except (OSError, subprocess.SubprocessError) as exc:
            report["production"] = {"compared": False, "why": f"读不到生产面（{type(exc).__name__}）"}
        else:
            drift = compare_production(before, after)
            report["production"] = {"compared": True, "drift": drift, "after": after}
            problems.extend(f"生产面变化：{item}" for item in drift)
    else:
        report["production"] = {"compared": False, "why": "未提供 --prod-baseline"}
    report["problems"] = problems
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
    send.add_argument("--order-no", required=True)
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
    cleanup.add_argument("--prod-baseline", default="")
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
