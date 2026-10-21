#!/usr/bin/env python3
"""用明确提供的会话驱动隔离栈 M3；不选择客户会话，不替换 Gateway 解析器。

preflight 只检查私有输入文件，零 HTTP；run 才发送一次请求并核对回执与运行记录的配置身份。
输入文件不是登录来源证明：本人授权、正常登录和目标使用正式 gateway_server 入口
须按 docs/agents/m3-user-entry-acceptance.md 核对，工具不能据此声称 App 已验收。
服务令牌只读栈配置；会话、订单只读显式文件。复用 live_replay 的独占标记、
回环限制、拒绝重定向、私有证据写入和记录采集。默认不访问任何模型或业务接口。
"""

from __future__ import annotations

import argparse
import http.client
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_replay  # noqa: E402 - 复用同目录的验收原语

Refused = live_replay.Refused
Incomplete = live_replay.Incomplete
SERVICE_TOKEN_KEY = "AIOPS_GATEWAY_THIRD_SESSION_SERVICE_TOKEN"
DEFAULT_STACK_CONFIG = Path(os.environ.get("M2LIVE_CONFIG", "/etc/aiops-m2-live"))
ROUTE = "/v1/orders/{order_no}/charging-status/delegated"


def read_session(path: Path) -> str:
    lines = live_replay._private_file(path, what="一行已授权 thirdSession").splitlines()
    if len(lines) != 1 or not lines[0] or len(lines[0]) > 256:
        raise Refused("会话文件须恰好一行非空值，长度不超过 256")
    session = lines[0]
    # HTTP 头必须可编码且不能携带控制字符；不把错误输入原文回显。
    if not session.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in session):
        raise Refused("会话文件含空白或不支持的 HTTP 头字符")
    return session


def read_service_token(config: Path) -> str:
    values = []
    # gateway.env 是进程环境，非空时优先于 runtime.env；与 Gateway 取值顺序相同。
    for name in ("gateway/gateway.env", "gateway/runtime.env"):
        path = config / name
        if not os.path.lexists(path):
            continue
        text = live_replay._private_file(path, what="隔离栈私有配置")
        parsed = dotenv_values(stream=io.StringIO(text), interpolate=False)
        value = (parsed.get(SERVICE_TOKEN_KEY) or "").strip()
        if value:
            values.append(value)
    if not values:
        raise Refused("隔离栈私有配置缺少服务令牌")
    token = values[0]
    if "${" in token or not token.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in token):
        raise Refused("服务令牌须为已解析的 HTTP 头值")
    return token


def preflight(*, session_file: Path, order_file: Path, stack_config: Path, base_url: str) -> dict[str, Any]:
    return {
        "base_url": live_replay.validate_base_url(base_url),
        "order_no": live_replay.read_order_no(order_file),
        "session": read_session(session_file),
        "service_token": read_service_token(stack_config),
    }


def _post_delegated(*, base_url: str, order_no: str, service_token: str, session: str, timeout: float):
    base = live_replay.validate_base_url(base_url)
    request = urllib.request.Request(
        base + ROUTE.format(order_no=urllib.parse.quote(order_no, safe="")),
        data=b"{}",
        method="POST",
        headers={
            "Authorization": f"Bearer {service_token}",
            "X-Third-Session": session,
            "X-Business-Entry": "consumer",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), live_replay._NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, live_replay._load(response.read())
    except urllib.error.HTTPError as error:
        return error.code, live_replay._load(error.read())
    except (OSError, http.client.HTTPException) as error:
        return None, {"failure": type(error).__name__}


def _write(out: Path, name: str, payload: dict[str, Any], *, secrets: tuple[str, ...]) -> Path:
    body = json.dumps(payload, ensure_ascii=False)
    for secret in secrets:
        body = live_replay.redact(body, extra=json.dumps(secret, ensure_ascii=False)[1:-1])
    return live_replay._write_evidence(out, name, json.loads(body), token="")


def cmd_preflight(args: argparse.Namespace) -> int:
    checked = preflight(
        session_file=Path(args.session_file),
        order_file=Path(args.order_file),
        stack_config=Path(args.stack_config),
        base_url=args.base_url,
    )
    payload = {
        "checked_at": live_replay._now(),
        "sent_requests": 0,
        "login_provenance_verified": False,
        "preflight": {
            "method": "POST",
            "path": ROUTE,
            "headers": ["Authorization", "X-Third-Session", "X-Business-Entry: consumer"],
        },
    }
    _write(Path(args.out), "preflight.json", payload, secrets=(checked["session"], checked["service_token"]))
    print(json.dumps({"input_files_valid": True, "sent_requests": 0, "login_provenance_verified": False}))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    checked = preflight(
        session_file=Path(args.session_file),
        order_file=Path(args.order_file),
        stack_config=Path(args.stack_config),
        base_url=args.base_url,
    )
    state = Path(args.state)
    if not (state / "gateway/gateway.db").is_file():
        raise Incomplete("没有可采证的隔离栈数据库，不发送请求")
    out = Path(args.out)
    secrets = (checked["session"], checked["service_token"], checked["order_no"])
    live_replay._claim_send_slot(out)
    status, body = _post_delegated(**checked, timeout=args.timeout)
    receipt = {
        "received_at": live_replay._now(),
        "http_status": status,
        "caller_input": "explicit session file",
        "login_provenance_verified": False,
        "body": live_replay._receipt_summary(body),
        "failure": body.get("failure") if isinstance(body, dict) else None,
    }
    _write(out, "sent.json", receipt, secrets=secrets)
    if status != 200 or not isinstance(body, dict):
        print(json.dumps({"http_status": status, "confirmed": False}))
        return 1
    run_id = body.get("run_id")
    if not isinstance(run_id, str) or not live_replay.SAFE_RUN_ID.fullmatch(run_id):
        print("回执缺少合法 run_id，不据此采证", file=sys.stderr)
        return 1
    collected = live_replay.collect(
        state,
        run_id,
        gateway_container=args.gateway_container,
        node_container=args.node_container,
    )
    record = collected["record"] or {}
    confirmed = bool(
        collected["log_observations_ok"]
        and not collected["identity_substitution_banner"]
        and record.get("run_id") == run_id
        and body.get("state") == record.get("state") == "succeeded"
        and record.get("has_snapshot") is True
        and record.get("exec_config_state") == "matched"
        and record.get("expected_agent_version")
        and record.get("expected_agent_version")
        == record.get("observed_agent_version")
        == body.get("agent_version")
        == body.get("agent_version_observed")
        and body.get("agent_version_gap") is None
    )
    collected.update({"confirmed": confirmed, "login_provenance_verified": False})
    _write(out, f"{run_id}.collect.json", collected, secrets=secrets)
    print(json.dumps({"run_id": run_id, "confirmed": confirmed, "login_provenance_verified": False}))
    return 0 if confirmed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, function in (("preflight", cmd_preflight), ("run", cmd_run)):
        command = sub.add_parser(name)
        command.add_argument("--session-file", required=True)
        command.add_argument("--order-file", required=True)
        command.add_argument("--stack-config", default=str(DEFAULT_STACK_CONFIG))
        command.add_argument("--base-url", default="http://127.0.0.1:8794")
        command.add_argument("--out", required=True)
        if name == "run":
            command.add_argument("--state", required=True, help="隔离栈状态根；必须核对运行记录")
            command.add_argument("--gateway-container", default="m2live03-gateway")
            command.add_argument("--node-container", default="m2live03-node")
            command.add_argument("--timeout", type=float, default=90.0)
        command.set_defaults(func=function)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except Refused as exc:
        print(f"拒绝：{exc}", file=sys.stderr)
        return 3
    except (Incomplete, OSError) as exc:
        print(f"前置或观测不完整：{type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
