"""隔离门禁：可写状态必须属于本次试运行自己的根，且在任何库被打开之前就判定。

为什么这一层必须存在：`create_gateway_app()` 在启动路径上会写库
（`recover_interrupted_jobs()` 的作业迁移、助手问题收敛、会话槽清理）。一个继承了
生产 `AIOPS_GATEWAY_DATABASE_FILE` 的实例，哪怕业务库只读，也会写生产 Gateway 的
SQLite。「业务库只读」推不出「没有副作用」。

判据分四组，任何一条不成立就拒绝：

1. **根**：存在，且解析后不与生产前缀重叠（也不能是生产前缀的父目录）。
2. **可写路径**：每条解析后都在根之下 —— 符号链接按解析结果算，所以指回生产的链接被拒。
3. **只读依赖**：单独白名单；必须位于根之外、可读，且与任何可写路径不重叠。
   生产配置属于这一类，不能因为「它也是路径」就被算成可写状态。
4. **属主**：以某个 UID 运行时，根与既有可写目录的属主必须是那个 UID。

这个模块同时是**真实启动路径**（`gateway_server serve` 读到 `M2LIVE_ISOLATION_ROOT`
时调用）与**检查脚本**（`python -m aiops_diagnostics.isolation_guard`）的实现，
避免两处判据漂移。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

#: 视为生产的路径前缀。默认值就是 41 上的 Gateway 根与配置目录。
DEFAULT_FORBIDDEN = ("/var/lib/aiops-41", "/etc/aiops-41")

#: 一个隔离实例必须自己声明的可写路径。
REQUIRED_PATHS = ("data_home", "database_file", "run_root", "key_home")

#: 触发门禁的环境变量。不设它时 `enforce_from_env` 是空操作 —— 生产 Gateway 的
#: 启动路径因此逐字不变。
ROOT_ENV = "M2LIVE_ISOLATION_ROOT"
READONLY_ENV = "M2LIVE_READONLY_PATHS"
EXTRA_WRITABLE_ENV = "M2LIVE_EXTRA_WRITABLE"
EXPECTED_UID_ENV = "M2LIVE_EXPECTED_UID"


class IsolationError(RuntimeError):
    """路径或属主不隔离。**在打开任何数据库之前**抛出。"""


def forbidden_prefixes() -> tuple[Path, ...]:
    raw = os.environ.get("M2LIVE_FORBIDDEN", "")
    extra = [part for part in raw.split(os.pathsep) if part.strip()]
    return tuple(Path(p).expanduser().resolve() for p in (*DEFAULT_FORBIDDEN, *extra))


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class GuardSpec:
    root: Path
    writable: Mapping[str, Path]
    readonly: tuple[Path, ...] = ()
    expected_uid: int | None = None
    forbidden: tuple[Path, ...] = field(default_factory=tuple)

    def resolved_forbidden(self) -> tuple[Path, ...]:
        return self.forbidden or forbidden_prefixes()


def assert_isolated(spec: GuardSpec) -> dict[str, Path]:
    """校验并返回**解析后**的可写路径，供调用方直接使用。"""
    # 顺序有讲究：`resolve()` 之后再问 `is_symlink()` 永远是 False（解析结果不是链接）。
    # 符号链接必须用**声明路径**判定，否则「根是指向生产目录的链接」会被静默放行。
    declared_root = Path(spec.root).expanduser()
    if declared_root.is_symlink():
        raise IsolationError(f"隔离根本身是符号链接: {declared_root}")
    root = declared_root.resolve()
    if not root.is_dir():
        raise IsolationError(f"隔离根不存在或不是目录: {root}")

    missing = [name for name in REQUIRED_PATHS if name not in spec.writable]
    if missing:
        raise IsolationError("缺少必须声明的可写路径: " + ", ".join(missing))

    for prefix in spec.resolved_forbidden():
        if _is_within(root, prefix) or _is_within(prefix, root):
            raise IsolationError(f"隔离根与生产前缀重叠: {root} ⊂ {prefix}")

    resolved: dict[str, Path] = {}
    for name, value in spec.writable.items():
        path = Path(value).expanduser().resolve()
        if not _is_within(path, root):
            raise IsolationError(f"{name} 不在隔离根之下: {path} ⊄ {root}")
        if any(_is_within(path, prefix) for prefix in spec.resolved_forbidden()):
            raise IsolationError(f"{name} 落在生产前缀之下: {path}")
        resolved[name] = path

    readonly = tuple(Path(p).expanduser().resolve() for p in spec.readonly)
    for path in readonly:
        if not path.exists():
            raise IsolationError(f"只读依赖不存在: {path}")
        if not os.access(path, os.R_OK):
            raise IsolationError(f"只读依赖不可读: {path}")
        for name, writable in resolved.items():
            if _is_within(writable, path) or _is_within(path, writable):
                raise IsolationError(f"只读依赖与可写路径重叠: {path} vs {name}")

    if spec.expected_uid is not None:
        actual = os.getuid()
        if actual != spec.expected_uid:
            raise IsolationError(f"运行 UID 是 {actual}，期望 {spec.expected_uid}")
        for label, path in (("root", root), *resolved.items()):
            if not path.exists():
                continue
            owner = path.stat().st_uid
            if owner != spec.expected_uid:
                raise IsolationError(f"{label} 属主是 {owner}，期望 {spec.expected_uid}: {path}")
    return resolved


def isolation_report(root: Path, paths: Mapping[str, Path]) -> dict[str, str]:
    """给证据用的一行摘要：只写相对位置，不写绝对路径。"""
    root_resolved = Path(root).expanduser().resolve()
    out = {"root": "."}
    for name, path in paths.items():
        try:
            out[name] = str(Path(path).expanduser().resolve().relative_to(root_resolved))
        except ValueError:
            out[name] = "outside-root"
    return out


def writable_paths_from_env() -> dict[str, Path]:
    """从真实运行环境里取可写路径 —— 与 `gateway_server serve` 用的是同一组变量。

    `M2LIVE_EXTRA_WRITABLE`（`:` 分隔）追加**同栈其它挂载**：平台库、平台状态、节点
    home 都在这份部署件里可写，只核对 Gateway 自己的四个路径会漏掉它们。
    """
    data_home = Path(os.environ.get("AIOPS_GATEWAY_DATA_HOME") or os.environ["AIOPS_DATA_HOME"])
    database_file = Path(os.environ.get("AIOPS_GATEWAY_DATABASE_FILE") or data_home / "gateway.db")
    run_root = Path(os.environ.get("AIOPS_AGENT_RUN_ROOT") or data_home / "runs")
    key_home = Path(os.environ.get("AIOPS_CONFIG_HOME") or data_home / "config") / "keys"
    paths = {
        "data_home": data_home,
        "database_file": database_file,
        "run_root": run_root,
        "key_home": key_home,
    }
    raw = os.environ.get(EXTRA_WRITABLE_ENV, "")
    for index, part in enumerate(part for part in raw.split(os.pathsep) if part.strip()):
        paths[f"mount_{index}"] = Path(part)
    return paths


def spec_from_env() -> GuardSpec | None:
    root = os.environ.get(ROOT_ENV, "").strip()
    if not root:
        return None
    raw_uid = os.environ.get(EXPECTED_UID_ENV, "").strip()
    readonly = tuple(
        Path(part) for part in os.environ.get(READONLY_ENV, "").split(os.pathsep) if part.strip()
    )
    return GuardSpec(
        root=Path(root),
        writable=writable_paths_from_env(),
        readonly=readonly,
        expected_uid=int(raw_uid) if raw_uid else None,
    )


def enforce_from_env() -> dict[str, Path] | None:
    """真实启动路径调用点。未设置隔离根时返回 None（生产行为不变）。"""
    spec = spec_from_env()
    if spec is None:
        return None
    return assert_isolated(spec)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="隔离门禁（真实启动路径与 preflight 共用）")
    parser.add_argument("--root", help=f"覆盖 {ROOT_ENV}")
    parser.add_argument("--readonly", action="append", default=[], help="只读依赖路径（可重复）")
    parser.add_argument("--expected-uid", type=int, help="期望的属主/运行 UID")
    args = parser.parse_args(argv)

    spec = spec_from_env()
    if spec is None:
        if not args.root:
            print(json.dumps({"status": "DISABLED", "reason": f"{ROOT_ENV} 未设置"}))
            return 0
        spec = GuardSpec(root=Path(args.root), writable=writable_paths_from_env())
    spec = GuardSpec(
        root=Path(args.root) if args.root else spec.root,
        writable=spec.writable,
        readonly=(*spec.readonly, *(Path(p) for p in args.readonly)),
        expected_uid=spec.expected_uid if args.expected_uid is None else args.expected_uid,
    )

    try:
        resolved = assert_isolated(spec)
    except IsolationError as exc:
        print(json.dumps({"status": "REFUSED", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(
        json.dumps(
            {"status": "OK", "isolation": isolation_report(spec.root, resolved)},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
