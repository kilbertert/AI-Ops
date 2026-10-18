"""The suite must run on the project's own interpreter, not an overlay.

This pins one property: the Python executing these tests is the project venv's
Python. It exists because the CI recipe installs the parallel scheduler into that
venv, and the obvious alternative — `uv run --with pytest-xdist …` — silently
breaks that property for the whole suite.

Measured on this repository, 2026-10-18:

    uv run python -c 'import sys; print(sys.executable)'
        -> <repo>/.venv/bin/python3
    uv run --with pytest-xdist==3.8.0 python -c 'import sys; print(sys.executable)'
        -> ~/.cache/uv/builds-v0/.tmpXXXXXX/bin/python

`--with` does not merely add a package; it runs the command in a *temporary
overlay environment*, and `sys.executable` and `sysconfig`'s `purelib`/
`platlib` follow it there. Any test that starts a subprocess with
`sys.executable`, or locates site-packages via `sysconfig`, then inspects a
different environment than the one under test — and reports it as a code
failure.

AI-Ops is currently unharmed only because none of its tests read purelib; its
`sys.executable` uses want a stdlib-capable interpreter and get one. This was not
a decision, so it is pinned here rather than left to luck. The same change in
health-flow turns `tests/test_e2e_seed.py` red immediately, which is how the
mechanism was found.

**Reverse-verified**: under `uv run --with pytest-xdist==3.8.0 pytest …` this
test fails; under `uv run pytest` it passes. A guard against a trap that has
never fired here has to be shown to fire somewhere, or it is decoration.
"""

from __future__ import annotations

import sys
import sysconfig
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _is_inside(path: Path, directory: Path) -> bool:
    """Whether ``path`` sits under ``directory`` — **without** resolving symlinks.

    A venv's `bin/python3` is a symlink to the base interpreter
    (`<repo>/.venv/bin/python3 -> ~/miniconda3/bin/python3.13`), so resolving it
    first would move the path OUT of the venv and make this test fail on a
    perfectly good environment. The question here is "which venv is this
    process configured for", and that is the unresolved path.
    """
    return str(path).startswith(str(directory) + "/")


def test_the_test_interpreter_is_the_project_venv_not_an_overlay() -> None:
    """`sys.executable` must live in this repo's `.venv`.

    If this fails, the suite is running under an overlay environment — most
    likely because the CI step (or a developer's shell) used `uv run --with`.
    Install the extra package into the venv instead:
    `uv pip install <pkg>` after `uv sync --locked --inexact --dev`.
    """
    venv = REPO_ROOT / ".venv"
    assert venv.is_dir(), f"没有找到项目 venv：{venv}"
    assert _is_inside(Path(sys.executable), venv), (
        f"测试跑在 {sys.executable}，不在项目的 {venv} 里 —— "
        "说明用的是 uv 的临时覆盖环境（很可能是 `uv run --with`）。"
        "那样 sys.executable 与 purelib 都会指向另一个环境，"
        "任何按它们定位文件或起子进程的测试都会看错环境。"
    )


def test_purelib_is_the_project_venv_too() -> None:
    """The other half of the same property.

    `sys.executable` and `purelib` can be read by different code paths, and the
    health-flow failure came in through `purelib` (a script that bootstraps
    `import app` from site-packages) rather than through `sys.executable`.
    """
    purelib = Path(sysconfig.get_paths()["purelib"])
    venv = REPO_ROOT / ".venv"
    assert _is_inside(purelib, venv), (
        f"purelib 是 {purelib}，不在项目的 {venv} 里 —— 按 site-packages 定位模块的测试会读到错的环境。"
    )


def test_the_parallel_scheduler_can_be_imported() -> None:
    """The suite is meant to run with `-n auto`; without the plugin pytest would
    reject `-n` outright, and CI would fail with an unexplained argument error.

    This asserts only that the import works. Whether the plugin is *used* is the
    CI step's business, and asserting on worker count here would be a probe of
    the invocation rather than of the environment.
    """
    import xdist  # noqa: F401  (import is the assertion)
