#!/usr/bin/env python3
"""算出 `services/execution-node/` 的**源码摘要**，并与 `images.lock.json` 里锁的值比对。

## 它解决的是什么

2026-10-30 的一次线上故障：网关自动升到 #1210，节点还停在 #1189 之前，管家侧所有工具
领授权一律 `403 OPERATOR_TOOL_CONTRACT_OUTDATED`。查下来发现 `images.lock.json` 里
`node.sha256` 是个**死常量** —— 既不等于 41 上实际在跑的 jar，也不等于 `HOST-CHANGE.md`
声称的源码修订；而当时唯一的测试只断言「lock 与 HOST-CHANGE **两个常量彼此一致**」。
两个常量互相点头，谁也没看真实源码。

## 为什么是**源码**摘要，不是 jar 摘要

CI 不构建 Java（`ci.yml` 只有 ruff / pytest / compileall）。一条「用当前源码构建出的 jar
摘要 == lock」的断言在 CI 里跑不起来，于是「节点源码改了但制品记录没更新」这件事永远
没有信号。

源码摘要把这件事拆成两半，各自落在能验证它的地方：

* **CI**：源码摘要变了而 lock 没变 ⇒ 红。这是「改了节点源码却忘了更新制品记录」的
  机械判据，不需要任何 JDK。
* **部署时**：jar 摘要与 lock 里的 `sha256` 比对（`deploy/node-runtime/README.md`
  的切换规程第 2 步已经在做）。那是制品身份，只能在有制品的地方验。

## 用法

    python deploy/node-runtime/source_digest.py            # 打印摘要
    python deploy/node-runtime/source_digest.py --check    # 与 images.lock.json 比对

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
SOURCE = REPO / "services" / "execution-node"
LOCK = REPO / "deploy" / "m2-live-03" / "images.lock.json"

#: 不参与摘要的路径：构建产物与 IDE 目录。`target/` 是 maven 输出，不是源码。
EXCLUDED = ("target", "__pycache__", ".idea")


def digest(root: Path = SOURCE) -> tuple[str, int]:
    """返回 ``(摘要, 文件数)``。

    摘要 = 对「按相对路径排序」的每条 `路径\\0文件sha256\\n` 再取一次 sha256。
    排序是**按路径**而不是按目录遍历顺序：换一台机器、换一个文件系统，遍历顺序会变，
    而这份摘要必须与「从哪台机器上算的」无关。
    """
    files = [
        path
        for path in root.rglob("*")
        if path.is_file() and not any(part in EXCLUDED for part in path.relative_to(root).parts)
    ]
    if not files:
        raise SystemExit(f"no source files under {root}")
    lines = []
    for path in sorted(files, key=lambda p: p.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix()
        lines.append(f"{rel}\0{hashlib.sha256(path.read_bytes()).hexdigest()}\n")
    blob = "".join(lines).encode()
    return hashlib.sha256(blob).hexdigest(), len(files)


def locked() -> str:
    return json.loads(LOCK.read_text(encoding="utf-8"))["node"].get("source_sha256", "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="与 images.lock.json 比对")
    args = parser.parse_args()
    value, count = digest()
    if not args.check:
        print(json.dumps({"source_sha256": value, "files": count}, indent=2))
        return 0
    expected = locked()
    print(
        json.dumps(
            {"source_sha256": value, "files": count, "locked": expected, "match": value == expected},
            indent=2,
        )
    )
    return 0 if value == expected else 1


if __name__ == "__main__":
    sys.exit(main())
