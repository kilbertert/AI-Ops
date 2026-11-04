#!/usr/bin/env python3
"""计算Gateway构建上下文摘要，按需与本次制品的identity.json核对。

与build-image.sh采用相同文件、排除和字节排序规则。无参数只输出摘要；
--check PATH核对源码、uv.lock和已批准基础镜像，旧记录不能证明新源码。
构建记录不进Git；没有记录必须失败，不能回退到仓库中的历史摘要。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]

#: 参与构造上下文的路径与各自的排除规则 —— 必须与 `build-image.sh` 里的两行 `tar` 一致。
CONTEXT_SPEC = (
    ("src", ("__pycache__", "*.pyc")),
    ("services/model-proxy", ("__pycache__", "*.pyc")),
    ("tools/verification/m2_live", ("__pycache__", "*.pyc")),
    # `m3node/build/` 是 `run.sh` 的编译产物目录（classes/、日志）。它不在
    # `build-image.sh` 的 tar 排除表里，但同样的理由成立且更硬：一份被本机**.gitignore**
    # 掉、每次运行都会变的目录若进了摘要，锁文件里的值就只代表某一台机器的某一次运行。
    # 2026-11-02 实测：本机跑过 run.sh 后本地算出 8240bbed…，CI 在干净树上算出 68b229e2…。
    ("tools/experiments/m2_close", ("__pycache__", "*.pyc", "node/target", "m3node/build")),
)
#: 直接放在上下文根的两个文件（`build-image.sh` 用 `cp` 放进去，日志里的路径是 `./名字`）。
ROOT_FILES = ("deploy/m2-live-03/gateway.Dockerfile", "deploy/m2-live-03/gateway-requirements.txt")
ROOT_NAMES = ("Dockerfile", "gateway-requirements.txt")


def _excluded(relative: Path, patterns: tuple[str, ...]) -> bool:
    parts = relative.parts
    for pattern in patterns:
        if pattern == "__pycache__":
            if "__pycache__" in parts:
                return True
        elif pattern == "*.pyc":
            if relative.suffix == ".pyc":
                return True
        elif pattern == "node/target":
            if relative.parts[:2] == ("node", "target"):
                return True
        elif pattern == "m3node/build":
            if relative.parts[:2] == ("m3node", "build"):
                return True
        else:  # pragma: no cover - 新增规则时应该显式处理
            raise SystemExit(f"unhandled exclude pattern: {pattern}")
    return False


def files() -> list[tuple[str, Path]]:
    """返回 (上下文内路径, 源文件) 列表，按上下文内路径排序。"""
    selected: list[tuple[str, Path]] = []
    for name, source in zip(ROOT_NAMES, ROOT_FILES, strict=True):
        selected.append((f"./{name}", REPO / source))
    for base, patterns in CONTEXT_SPEC:
        root = REPO / base
        if not root.is_dir():
            raise SystemExit(f"missing context path: {base}")
        for path in root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root)
            if _excluded(relative, patterns):
                continue
            selected.append((f"./{base}/{relative.as_posix()}", path))
    selected.sort(key=lambda item: item[0])
    return selected


def digest() -> tuple[str, int]:
    """与 `build-image.sh` 同形的摘要：`sha256sum` 输出再取一次 sha256。"""
    # 与 `find . -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum` 同形：
    # **先按路径排序**，再把 `sha256sum` 的输出文本整体再哈希。顺序错了摘要就不同，
    # 这正是本脚本要消灭的那类「各人各算一遍」的差异。
    entries = files()
    entries.sort(key=lambda item: item[0].encode())
    lines = [
        f"{hashlib.sha256(source.read_bytes()).hexdigest()}  {context_path}"
        for context_path, source in entries
    ]
    joined = "\n".join(lines) + "\n"
    return hashlib.sha256(joined.encode()).hexdigest(), len(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", type=Path, metavar="IDENTITY", help="与本次构建的identity.json比对")
    parser.add_argument("--out", default="", help="把结果写成 JSON")
    args = parser.parse_args()

    value, count = digest()
    record: dict[str, object] = {"source_sha256": value, "files": count}

    code = 0
    if args.check:
        try:
            identity = json.loads(args.check.read_text())
            expected = {
                "source_sha256": value,
                "uv_lock_sha256": hashlib.sha256((REPO / "uv.lock").read_bytes()).hexdigest(),
                "base_digest": json.loads((HERE / "images.lock.json").read_text())["gateway_image"][
                    "base_digest"
                ],
            }
            record["mismatched"] = [key for key, wanted in expected.items() if identity.get(key) != wanted]
            record["match"] = not record["mismatched"]
            code = 0 if record["match"] else 1
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            print("构建记录或输入锁不可读，请重新构建并提供identity.json", file=sys.stderr)
            return 2
    text = json.dumps(record, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n")
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
