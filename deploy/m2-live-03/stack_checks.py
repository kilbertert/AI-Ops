"""从渲染后的 compose 配置里核对部署件的硬约束。

输入是 `docker compose config --format json` 的输出（stdin）；参数是判定所需的路径。

    stack_checks.py mounts   <state> <config-dir>
    stack_checks.py users    <uid>:<gid>
    stack_checks.py manifest

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
    if not container_path.startswith("/") or os.path.normpath(container_path) != container_path:
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
    # 部署文件只需要 owner-read，可选 owner-write；不接受仅写或执行位。
    if mode not in {0o400, 0o600}:
        problems.append(f"{service}: 清单权限 {mode:04o} 不合规（只接受 0400/0600）")
    return problems


GATEWAY_MANIFEST_ENV = "AIOPS_GATEWAY_EXECUTION_MANIFEST_FILE"
NODE_MANIFEST_ENV = "AIOPS_EXEC_MANIFEST"


def _rendered_env(services: dict, service: str, key: str) -> str | None:
    """渲染后的 environment 里的键值（`None` = 该键没有出现在渲染结果里）。

    读**渲染结果**而不是配置文件原文：`M2LIVE_COMPOSE_EXTRA` 覆盖层可以在不改任何文件
    的情况下改掉一个服务的 environment，而预检要判的是「实际会起起来的那个容器」。
    只去两侧空白；**路径内部的空白保留** —— 运行时拿到的是同一个字符串，内部空白就是我
    要判的那个路径的一部分，替它清理只会让判据与运行时不一致。
    """
    environment = (services.get(service) or {}).get("environment") or {}
    if key not in environment:
        return None
    value = environment.get(key)
    if value is None:
        return ""
    return str(value).strip()


def manifest(argv: list[str]) -> int:
    """`manifest`（无参数；输入是渲染后的 compose JSON）

    两侧的清单路径都取自**渲染后的 environment**，而不是配置文件原文。判据：
    两侧要么都未配置（M3 守卫关闭，与启用前逐字一致），要么都配置且解析到**同一个
    真实普通文件**（同一 inode）。单侧配置不是合法状态：开着的那侧会按漂移拒绝或按
    未启用放行，两种都不是这次要验的链路 —— 覆盖层恰好能造出这个形状。
    """
    del argv
    data = json.load(sys.stdin) or {}
    services = data.get("services") or {}
    if not services:
        print("compose 没有渲染出任何服务：无法核对清单挂载")
        return 1
    for service in ("gateway", "node"):
        if service not in services:
            print(f"渲染结果里没有 {service} 服务：无法核对清单")
            return 1
    gateway_path = _rendered_env(services, "gateway", GATEWAY_MANIFEST_ENV)
    node_path = _rendered_env(services, "node", NODE_MANIFEST_ENV)
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
        if not problems:
            user = str(services[service].get("user", "")).split(":", 1)
            uid, gid = user[0], user[-1]
            info = os.stat(host)
            if not uid.isdigit() or int(uid) != info.st_uid or not info.st_mode & stat.S_IRUSR:
                problems.append(f"{service}: 清单属主/读取权限与容器 UID 不匹配")
            # bind 源路径之外的宿主父目录不会出现在容器；仅核对挂载内部的目录。
            for volume in services[service].get("volumes", []):
                if volume.get("type") != "bind":
                    continue
                target = str(volume.get("target", "")).rstrip("/")
                container_path = {"gateway": gateway_path, "node": node_path}[service]
                if not container_path.startswith(target + "/"):
                    continue
                directory = Path(host).parent
                mount = Path(volume["source"])
                while directory == mount or mount in directory.parents:
                    metadata = directory.stat()
                    bit = stat.S_IXOTH
                    if uid.isdigit() and int(uid) == metadata.st_uid:
                        bit = stat.S_IXUSR
                    elif gid.isdigit() and int(gid) == metadata.st_gid:
                        bit = stat.S_IXGRP
                    searchable = metadata.st_mode & bit
                    if not searchable:
                        problems.append(f"{service}: 挂载内父目录不可遍历")
                    if directory == mount:
                        break
                    directory = directory.parent
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
