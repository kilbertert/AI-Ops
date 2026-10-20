#!/usr/bin/env python3
"""算出 Gateway 镜像的**构造上下文摘要**，并与 `images.lock.json` 里锁的值比对。

`build-image.sh` 的 `m2live.source` 标签是这么来的：把 `src/`、`tools/m2_live`、
`tools/m2_close`、Dockerfile 与 requirements 装进一个目录，然后对「路径 + 内容」的
排序列表再取一次 sha256。preflight 用这个值核对宿主上的镜像。

问题在于它是**行内命令**：只有构建时才算得出来。于是锁文件里那个摘要无法在 CI 里核对，
reviewer 想复核只能自己重写一遍管道 —— 而管道有两处容易走样：

1. `--exclude` 的作用域。`src` 用 `--exclude '__pycache__' --exclude '*.pyc'`；
   `tools/...` 用 `--exclude '__pycache__' --exclude 'node/target'`。**两者不同**，
   写成一组通用 exclude 会得到另一个摘要。
2. 工作树里的未跟踪文件（例如验收产物 `tools/m2_close/m3node/build/`）会被 `tar` 带上。

这个脚本不复刻那两行 `tar`，而是**按同一组排除规则逐文件挑选**，因此与工作树是否干净
无关，也与从哪里运行无关。

    python deploy/m2-live-03/context_digest.py            # 打印摘要
    python deploy/m2-live-03/context_digest.py --check    # 与 images.lock.json 比对

退出码：0 一致 / 1 不一致 / 2 前置缺失。
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
    ("tools/m2_live", ("__pycache__", "*.pyc")),
    ("tools/m2_close", ("__pycache__", "node/target")),
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
            # 相对 `tools/m2_close` 的路径：只有以 `node/target` 开头的才排除。
            if relative.as_posix().startswith("node/target"):
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
    parser.add_argument("--check", action="store_true", help="与 images.lock.json 比对")
    parser.add_argument("--out", default="", help="把结果写成 JSON")
    args = parser.parse_args()

    value, count = digest()
    lock_path = HERE / "images.lock.json"
    record: dict[str, object] = {"source_sha256": value, "files": count}

    code = 0
    if args.check:
        if not lock_path.is_file():
            print(f"missing {lock_path}", file=sys.stderr)
            return 2
        locked = json.loads(lock_path.read_text())["gateway_image"]["source_sha256"]
        record["locked"] = locked
        record["match"] = locked == value
        if locked != value:
            code = 1
    text = json.dumps(record, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n")
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
