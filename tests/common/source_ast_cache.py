"""共享只读源码列举与按需 AST 解析辅助。

统一通过剪枝遍历（跳过 `.venv`、`.git` 等无关目录）收集 `.py` 文件，避免
静态守卫误扫虚拟环境；同时仅缓存小体积文本或即用即弃 `ast.Module`，避免
worker 进程长期驻留数百 MB 语法树对象。
"""

from __future__ import annotations

import ast
import os
from functools import lru_cache
from pathlib import Path

_PRUNED_DIRS = frozenset(
    {
        ".venv",
        ".git",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        "dist",
        "build",
        ".mypy_cache",
        ".ruff_cache",
    }
)


def iter_python_files(root: Path, *, exclude_names: frozenset[str] = frozenset()) -> tuple[Path, ...]:
    """返回 `root` 下全部 `.py` 路径（有序元组），自动跳过虚拟环境与构建缓存目录。"""
    found: list[Path] = []
    for _dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in _PRUNED_DIRS)
        for filename in filenames:
            if filename.endswith(".py") and filename not in exclude_names:
                found.append(Path(_dirpath) / filename)
    found.sort()
    return tuple(found)


@lru_cache(maxsize=256)
def _read_cached(path: str, mtime_ns: int, size: int) -> str:
    del mtime_ns, size
    return Path(path).read_text(encoding="utf-8")


def read_source_text(path: Path) -> str:
    """按文件状态指纹缓存读取源码文本。"""
    stat = path.stat()
    return _read_cached(str(path), stat.st_mtime_ns, stat.st_size)


def parse_source_ast(path: Path) -> ast.Module:
    """读取源码并即时解析为 `ast.Module`（不常驻缓存整棵语法树以控制 worker RSS）。"""
    return ast.parse(read_source_text(path), filename=str(path))
