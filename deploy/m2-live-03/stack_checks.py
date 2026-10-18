"""从渲染后的 compose 配置里核对部署件的两条硬约束。

输入是 `docker compose config --format json` 的输出（stdin）；参数是判定所需的路径。

    stack_checks.py mounts <state> <aiops41-dir> <config-dir>
    stack_checks.py users  <uid>:<gid>

退出码 0 通过；1 不通过（原因写 stdout，一行）。
"""

from __future__ import annotations

import json
import os
import sys


def mounts(argv: list[str]) -> int:
    """`mounts <state> <aiops41-dir> <config-dir> [extra-readonly-root...]`

    可写挂载必须在状态根之内。只读挂载必须在白名单里：生产配置/key、本栈配置，
    以及额外给出的制品/部署目录（平台与节点 jar、初始化 schema）。
    """
    state, aio41, config = (os.path.realpath(p) for p in argv[:3])
    readonly_allow = {
        os.path.realpath(aio41),
        os.path.realpath(os.path.join(aio41, "keys")),
        os.path.realpath(os.path.join(aio41, "production.env")),
        os.path.realpath(os.path.join(config, "gateway")),
        os.path.realpath(os.path.join(config, "platform")),
        os.path.realpath(os.path.join(config, "tls", "public")),
        os.path.realpath(os.path.join(config, "tls", "platform")),
        os.path.realpath(os.path.join(config, "tls", "proxy")),
        os.path.realpath(os.path.join(config, "gateway", "runtime.env")),
    }
    readonly_allow.update(os.path.realpath(p) for p in argv[3:])
    data = json.load(sys.stdin) or {}
    problems: list[str] = []
    for name, spec in (data.get("services") or {}).items():
        for volume in spec.get("volumes") or []:
            if volume.get("type") != "bind":
                continue
            source = os.path.realpath(volume.get("source", ""))
            inside_state = source == state or source.startswith(state + os.sep)
            if volume.get("read_only"):
                if not inside_state and source not in readonly_allow:
                    problems.append(f"{name}: 只读挂载不在白名单 {source}")
            elif not inside_state:
                problems.append(f"{name}: 可写挂载在状态根之外 {source}")
    print("; ".join(problems))
    return 1 if problems else 0


def users(argv: list[str]) -> int:
    want = argv[0]
    data = json.load(sys.stdin) or {}
    problems: list[str] = []
    for name in ("platform", "node", "gateway", "gateway-host-relay"):
        spec = (data.get("services") or {}).get(name) or {}
        actual = str(spec.get("user") or "")
        if actual != want:
            problems.append(f"{name}={actual or '(root)'}")
    print("; ".join(problems))
    return 1 if problems else 0


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: stack_checks.py mounts|users ...", file=sys.stderr)
        return 2
    command, rest = sys.argv[1], sys.argv[2:]
    if command == "mounts":
        return mounts(rest)
    if command == "users":
        return users(rest)
    print(f"unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
