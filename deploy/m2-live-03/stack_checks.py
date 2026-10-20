"""从渲染后的 compose 配置里核对部署件的硬约束。

输入是 `docker compose config --format json` 的输出（stdin）；参数是判定所需的路径。

    stack_checks.py mounts   <state> <config-dir>
    stack_checks.py users    <uid>:<gid>
    stack_checks.py manifest <gateway-容器内路径> <node-容器内路径>

退出码 0 通过；1 不通过（原因写 stdout，一行）。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path


def mounts(argv: list[str]) -> int:
    """`mounts <state> <config-dir> [extra-readonly-root...]`

    可写挂载必须在状态根之内。只读挂载必须在白名单里：本栈配置，
    以及额外给出的制品/部署目录（平台与节点 jar、初始化 schema）。
    """
    state, config = (os.path.realpath(p) for p in argv[:2])
    readonly_allow = {
        os.path.realpath(os.path.join(config, "gateway")),
        os.path.realpath(os.path.join(config, "platform")),
        os.path.realpath(os.path.join(config, "tls", "public")),
        os.path.realpath(os.path.join(config, "tls", "platform")),
        os.path.realpath(os.path.join(config, "tls", "proxy")),
        os.path.realpath(os.path.join(config, "gateway", "runtime.env")),
        # M3：执行配置清单。节点只读挂载**这一个文件**（不是整个 gateway 目录），
        # 所以它必须在白名单里；否则 preflight 直接拒绝起栈。
        os.path.realpath(os.path.join(config, "gateway", "exec-manifest.json")),
        # 模型出口只挂自己的目录（供应商 key 与代理令牌），看不到业务配置。
        os.path.realpath(os.path.join(config, "model-proxy")),
    }
    readonly_allow.update(os.path.realpath(p) for p in argv[2:])
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
    for name in ("platform", "node", "gateway", "gateway-host-relay", "model-proxy"):
        spec = (data.get("services") or {}).get(name) or {}
        actual = str(spec.get("user") or "")
        if actual != want:
            problems.append(f"{name}={actual or '(root)'}")
    print("; ".join(problems))
    return 1 if problems else 0


def _mounted_host_path(services: dict, service: str, container_path: str) -> str | None:
    """把**容器内**的清单路径解析成宿主上的文件路径。

    只看渲染后的 bind 挂载，不看文件名：`basename` 相等并不能证明两侧读的是同一份，
    而「这个容器路径到底挂到了哪个宿主文件」只有 compose 知道。取最长的 target 前缀，
    与容器解析路径的方式一致（`/etc/m2live` 与 `/etc/m2live/exec-manifest.json` 同时
    存在时，后者生效）。
    """
    if not container_path.startswith("/"):
        return None
    best: tuple[str, str] | None = None
    for volume in (services.get(service) or {}).get("volumes") or []:
        if not isinstance(volume, dict) or volume.get("type") != "bind":
            continue
        target = str(volume.get("target") or "").rstrip("/") or "/"
        source = str(volume.get("source") or "")
        if not source:
            continue
        if container_path == target:
            return source
        if (target == "/" or container_path.startswith(target + "/")) and (
            best is None or len(target) > len(best[0])
        ):
            best = (target, source)
    if best is None:
        return None
    target, source = best
    return os.path.join(source, os.path.relpath(container_path, target))


def _private_regular_problems(service: str, path: str) -> list[str]:
    """与两侧加载器**同一组**前提：普通文件、不是符号链接、非空、无组/他人权限。"""
    if not os.path.lexists(path):
        return [f"{service}: 清单不存在 {path}"]
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        return [f"{service}: 清单是符号链接 {path}"]
    if not stat.S_ISREG(info.st_mode):
        return [f"{service}: 清单不是普通文件 {path}"]
    problems: list[str] = []
    if info.st_size == 0:
        problems.append(f"{service}: 清单为空 {path}")
    mode = stat.S_IMODE(info.st_mode)
    # 两个加载器（Python `execution_manifest` / Java `ExecManifest`）共同拒绝的正是
    # 「组或他人有任何权限」；0400/0600 都在契约之内，不额外要求 0600。
    if mode & 0o077:
        problems.append(f"{service}: 清单权限 {mode:04o} 过宽（只接受 0400/0600）")
    return problems


def manifest(argv: list[str]) -> int:
    """`manifest <gateway 容器内路径> <node 容器内路径>`（空串 = 该侧未配置）

    两侧要么都未配置（M3 守卫关闭，与启用前逐字一致），要么都配置且由渲染后的
    compose 解析到**同一个真实普通文件**。单侧配置不是合法状态：一侧开着一侧关着，
    开的那侧会按漂移拒绝或按未启用放行，两种都不是这次要验的链路。
    """
    gateway_path = argv[0] if len(argv) > 0 else ""
    node_path = argv[1] if len(argv) > 1 else ""
    data = json.load(sys.stdin) or {}
    services = data.get("services") or {}
    if not services:
        print("compose 没有渲染出任何服务：无法核对清单挂载")
        return 1
    if not gateway_path and not node_path:
        print("两侧清单都未配置（M3 守卫不启用）")
        return 0
    if not gateway_path or not node_path:
        print(
            "只有一侧配置了清单（gateway="
            + (gateway_path or "（空）")
            + "，node="
            + (node_path or "（空）")
            + "）：两侧必须同时启用"
        )
        return 1
    hosts: dict[str, str] = {}
    for service, container_path in (("gateway", gateway_path), ("node", node_path)):
        host = _mounted_host_path(services, service, container_path)
        if host is None:
            print(f"{service}: 容器内路径 {container_path} 在渲染后的 compose 里没有对应的 bind 挂载")
            return 1
        hosts[service] = host
    problems: list[str] = []
    for service, host in hosts.items():
        problems.extend(_private_regular_problems(service, host))
    if problems:
        print("; ".join(problems))
        return 1
    gateway_real = os.path.realpath(hosts["gateway"])
    node_real = os.path.realpath(hosts["node"])
    gateway_stat, node_stat = os.stat(gateway_real), os.stat(node_real)
    if (gateway_stat.st_dev, gateway_stat.st_ino) != (node_stat.st_dev, node_stat.st_ino):
        # 同一份字节也可能是两份副本：副本一旦各自更新就是漂移，而漂移在领取时才暴露。
        print(f"两侧清单不是同一个文件：gateway={gateway_real} node={node_real}")
        return 1
    digest = hashlib.sha256(Path(gateway_real).read_bytes()).hexdigest()
    print(f"两侧清单由 compose 解析到同一个文件（{digest[:16]}…，{gateway_real}）")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: stack_checks.py mounts|users|manifest ...", file=sys.stderr)
        return 2
    command, rest = sys.argv[1], sys.argv[2:]
    if command == "mounts":
        return mounts(rest)
    if command == "users":
        return users(rest)
    if command == "manifest":
        return manifest(rest)
    print(f"unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
