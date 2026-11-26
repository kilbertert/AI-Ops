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

import os
import sys
import sysconfig
from pathlib import Path

import pytest

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
    """The scheduler must be importable *where something asks for it*.

    The suite is meant to run with `-n auto`; without the plugin pytest rejects
    `-n` outright, and the job dies with an unexplained argument error that looks
    like a code fault.

    **Why this is not an unconditional assertion.** `xdist` is deliberately not a
    declared dependency (see ADR 0038: adding one changes `uv.lock` and the CD
    dependency-drift gate then refuses to deploy). Three runners execute this
    suite and only CI installs the scheduler explicitly:

    - CI installs it after `uv sync --locked --inexact --dev`;
    - `.sandcastle/sandbox-prepare.sh` runs plain `uv sync --locked --dev`;
    - `.sandcastle/review/prompt.md` runs `uv sync --extra dev && uv run pytest`.

    An unconditional `import xdist` fails in the last two — which is what review
    caught: it would have broken the AFK review/fix workflow's mandatory
    verification step, and it would have broken it *inside the change that exists
    to stop silent breakage*. So the requirement is conditioned on the run that
    actually needs it.

    The condition is `PYTEST_XDIST_WORKER`, which xdist sets in each worker
    process — a fact about *this* invocation rather than a guess about intent.
    """
    under_xdist = "PYTEST_XDIST_WORKER" in os.environ
    try:
        import xdist  # noqa: F401
    except ImportError:
        if under_xdist:
            raise AssertionError(
                "本次运行带着 xdist worker（PYTEST_XDIST_WORKER 已设），却 import 不到 xdist —— "
                "说明并行调度器与 runner 不是从同一个环境来的。"
            ) from None
        pytest.skip("未装 xdist：本次是串行运行，不需要它（见本函数 docstring）")


def test_conftest_bounds_malloc_arenas_and_enables_temp_sqlite_sync_off() -> None:
    import conftest

    from aiops_diagnostics.gateway import gateway_db

    assert os.environ.get("MALLOC_ARENA_MAX") == "2"
    assert gateway_db._TEST_SYNC_OFF_ROOT is not None
    assert hasattr(conftest, "_available_memory_mb")


def test_host_slot_file_permissions_are_world_rw(tmp_path: Path) -> None:
    import conftest

    slot = tmp_path / "host-test-slot-0.lock"
    old_umask = os.umask(0o027)
    try:
        handle = conftest._open_slot(slot)
        handle.close()
    finally:
        os.umask(old_umask)
    assert (slot.stat().st_mode & 0o777) == 0o666


def test_auto_worker_budget_respects_ci_env_and_low_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    import conftest

    monkeypatch.setenv("CI", "1")
    monkeypatch.delenv(conftest.WORKERS_ENV, raising=False)
    assert conftest.pytest_xdist_auto_num_workers(None) == 4  # type: ignore[arg-type]

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(conftest, "_available_memory_mb", lambda: 8192)
    assert conftest.pytest_xdist_auto_num_workers(None) == 2  # type: ignore[arg-type]

    monkeypatch.setattr(conftest, "_available_memory_mb", lambda: 2048)
    assert conftest.pytest_xdist_auto_num_workers(None) == 1  # type: ignore[arg-type]

    monkeypatch.setenv(conftest.WORKERS_ENV, "3")
    assert conftest.pytest_xdist_auto_num_workers(None) == 3  # type: ignore[arg-type]


def test_clear_fastapi_callable_caches_respects_threshold() -> None:
    import conftest
    from fastapi.dependencies import models as fastapi_models

    def _sample_endpoint() -> None:
        return None

    fastapi_models._is_coroutine_callable_cached(fastapi_models._CallIdentity(_sample_endpoint))
    assert fastapi_models._is_coroutine_callable_cached.cache_info().currsize >= 1
    assert conftest._clear_fastapi_callable_caches(threshold=100_000) is False
    assert conftest._clear_fastapi_callable_caches(threshold=1) is True
    assert fastapi_models._is_coroutine_callable_cached.cache_info().currsize == 0


def test_local_default_markers_only_filter_untargeted_local_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    import conftest

    root = Path(__file__).resolve().parents[1]

    def _cfg(args: list[str], *, markexpr: str = "", keyword: str = "") -> SimpleNamespace:
        return SimpleNamespace(
            args=args,
            rootpath=root,
            option=SimpleNamespace(markexpr=markexpr, keyword=keyword),
        )

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv(conftest.ALL_TESTS_ENV, raising=False)
    assert conftest._should_apply_local_default_markers(_cfg([])) is True  # type: ignore[arg-type]
    assert conftest._should_apply_local_default_markers(_cfg(["tests"])) is True  # type: ignore[arg-type]
    assert conftest._should_apply_local_default_markers(_cfg(["tests/"])) is True  # type: ignore[arg-type]

    assert (
        conftest._should_apply_local_default_markers(
            _cfg(["tests/dify/test_dify_idempotent_import.py"])  # type: ignore[arg-type]
        )
        is False
    )
    assert conftest._should_apply_local_default_markers(_cfg([], markexpr="docker")) is False  # type: ignore[arg-type]
    assert conftest._should_apply_local_default_markers(_cfg([], keyword="dify")) is False  # type: ignore[arg-type]

    monkeypatch.setenv(conftest.ALL_TESTS_ENV, "1")
    assert conftest._should_apply_local_default_markers(_cfg([])) is False  # type: ignore[arg-type]

    monkeypatch.delenv(conftest.ALL_TESTS_ENV, raising=False)
    monkeypatch.setenv("CI", "1")
    assert conftest._should_apply_local_default_markers(_cfg([])) is False  # type: ignore[arg-type]
