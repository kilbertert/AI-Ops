"""consumer-status 工具的私有证据写入。

为什么单独一个模块：两个工具（`independent_cpms` / `status_probe`）各有一份
`_write_private`，两版都是 `mkdir → write_text → chmod`。那有三个问题：

1. **会跟随符号链接**：`write_text` 打开目标链接指向的文件，于是"写私有报告"
   可能覆盖别人放在那里的文件；
2. **没有库外限制**：`--out .` 能把完整订单号写进仓库/工作树；
3. **权限不是从创建起就私有**：先按 umask 创建、再 chmod，中间有一小段窗口。

改为复用 `aiops_diagnostics.private_files.write_private_text`（tempfile + `replace`，
**不跟随符号链接**、创建即 0600、并校验目录属于当前用户且不是符号链接），
再在工具侧补一条"必须在库外"的限制。
"""

from __future__ import annotations

import os
from pathlib import Path

from aiops_diagnostics.private_files import (
    PrivatePathError,
    validate_private_directory,
    write_private_text,
)


class PrivateOutputRefused(ValueError):
    """拒绝写入：路径在仓库内，或目标不是安全的私有位置。"""


#: 仓库/工作树的判据。**用仓库根而不是 cwd**：工作树可能开在别处。
_REPO_MARKERS = (".git",)


def repository_roots(start: Path) -> list[Path]:
    """`start` 及其祖先里所有"仓库根"（含 `.git` 的目录）。"""
    roots: list[Path] = []
    current = start.resolve()
    for candidate in (current, *current.parents):
        if any((candidate / marker).exists() for marker in _REPO_MARKERS):
            roots.append(candidate)
    return roots


def _inside(path: Path, roots: list[Path]) -> Path | None:
    resolved = path.resolve()
    for root in roots:
        with_path = Path(root)
        if resolved == with_path or with_path in resolved.parents:
            return with_path
    return None


def assert_outside_repository(path: Path, *, code_root: Path) -> None:
    """库外限制：完整证据不许写进**任何**仓库/工作树。

    判据看两边：

    * `code_root`（工具代码所在目录）的祖先链 —— 覆盖"写回本仓"；
    * **目标路径自己**的祖先链 —— 覆盖"写进另一个仓库/工作树"。

    只看前者会漏掉后者：`--out /some/other/repo/x.json` 里那个仓库跟工具代码
    没有共同祖先，但那同样是一个会被提交的地方。第一版就漏了这条。
    """
    roots = repository_roots(code_root)
    hit = _inside(path, roots)
    if hit is None:
        hit = _inside(path, repository_roots(path.parent))
    if hit is not None:
        raise PrivateOutputRefused(f"拒绝写入仓库内路径：{path} 落在 {hit} 下")


def _reject_symlink_target(path: Path) -> None:
    """目标本身、以及它的父目录，都不能是符号链接。

    `write_private_text` 用 `replace()` 已经**不跟随**目标链接（链接会被替换成
    普通文件），但那是"运气好"——它仍然会先 `ensure_private_directory(parent)`
    对父目录 `mkdir`/`chmod`。父目录若是符号链接，就会对链接**指向的真实目录**
    动手。所以这里显式拒绝。
    """
    if path.is_symlink():
        raise PrivateOutputRefused(f"拒绝写入符号链接：{path}")
    for parent in path.parents:
        if parent.is_symlink():
            raise PrivateOutputRefused(f"拒绝写入：父目录是符号链接 {parent}")
        if parent == parent.parent:
            break


def write_private_report(path: Path, content: str, *, code_root: Path) -> Path:
    """写一份私有报告。失败一律抛 `PrivateOutputRefused` / `PrivatePathError`。

    顺序是刻意的：**先判路径与符号链接，再落盘**。先写后判等于已经写了。
    """
    assert_outside_repository(path, code_root=code_root)
    _reject_symlink_target(path)
    written = write_private_text(path, content)
    validate_private_directory(written.parent)
    if written.is_symlink():
        raise PrivateOutputRefused(f"写入后仍是符号链接：{written}")
    return written


def describe_failure(exc: Exception) -> str:
    """给终端的脱敏失败说明：不回显完整路径之外的内容。"""
    if isinstance(exc, (PrivateOutputRefused, PrivatePathError)):
        return str(exc)
    return f"{type(exc).__name__}"


__all__ = [
    "PrivateOutputRefused",
    "assert_outside_repository",
    "describe_failure",
    "repository_roots",
    "write_private_report",
]


def _self_check() -> None:
    """最小自检：仓库内路径必须被拒，库外私有目录必须成功。"""
    import tempfile

    me = Path(__file__).resolve()
    try:
        assert_outside_repository(me.parent / "x.json", code_root=me.parent)
    except PrivateOutputRefused:
        pass
    else:  # pragma: no cover
        raise AssertionError("仓库内路径没有被拒绝")

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "ok.json"
        write_private_report(target, "{}", code_root=me.parent)
        assert target.read_text() == "{}"
        assert os.stat(target).st_mode & 0o077 == 0


if __name__ == "__main__":  # pragma: no cover
    _self_check()
    print("self-check ok")
