"""Test-suite-wide settings that would otherwise cost real wall-clock time.

One entry, and it is not a style choice — it is a measured 32x on the slowest
file in this suite.
"""

from __future__ import annotations

import pathlib
import socketserver
import tempfile

import pytest

# `socketserver.BaseServer.serve_forever` defaults to `poll_interval=0.5`: the
# loop wakes every half second to look for a shutdown flag, and `shutdown()`
# blocks until it does. Every test in this suite that starts a local HTTP stand-in
# pays that wait in teardown, and there are ~30 such fixtures.
#
# Measured on this repository, 2026-10-18, `tests/test_dify_console_publish.py`:
#
#     default (0.5s)          96.73 s
#     poll_interval=0.01       2.97 s
#
# The per-test `--durations` output showed a flat `teardown = 1.00s` on nearly
# every case, which is what pointed here rather than at the test bodies.
#
# Why a patch rather than editing the call sites: the 14 call sites in this suite
# each pass `serve_forever` straight to `threading.Thread(target=...)`, and the
# production servers in `src/` call it in `main()` with no argument. Patching the
# library default changes only the wait granularity — it does not alter what the
# server does.
#
# Deliberately NOT 0: `selector.select(0)` turns the loop into a busy-spin that
# burns a core per server. 0.01 is sixty times more responsive than the default
# and still ~free.
_ORIGINAL_SERVE_FOREVER = socketserver.BaseServer.serve_forever

# Distinguishes "caller omitted poll_interval" from "caller asked for 0.5".
# Without this sentinel the two are indistinguishable, and the wrapper would
# override a deliberate 0.5 — which is what the first version of this file did.
# `src/aiops_diagnostics/tdengine_proxy.py` passes 0.5 explicitly, so under that
# bug its behaviour silently differed inside any test process importing this file.
_UNSET = object()


def _serve_forever_fast(self, poll_interval=_UNSET) -> None:
    """Same loop, shorter shutdown-poll granularity when the caller is silent."""
    if poll_interval is _UNSET:
        return _ORIGINAL_SERVE_FOREVER(self, 0.01)
    return _ORIGINAL_SERVE_FOREVER(self, poll_interval)


socketserver.BaseServer.serve_forever = _serve_forever_fast


# ── One full run at a time, per checkout ────────────────────────────────────
# Measured 2026-10-23: several concurrent sessions each running the suite took this
# host down (`load average 5.28` → 79; sshd and tailscaled starved, the tailnet
# dropped and remote management was gone). `-n 4` in addopts bounds one run's
# width; it does not bound how many runs there are.
#
# ⚠️ The lock lives HERE rather than in `tools/test.sh` because that wrapper is
# not the only entry point — `implement.md`, the review prompts and CI all spell
# `uv run pytest` directly, and a lock in a wrapper those paths never call protects
# nobody. Measured by review on the first version: the AFK gates bypassed it.
#
# Keyed on the checkout root, so two worktrees do not queue behind each other —
# they share no venv, no SQLite fixture, no cache. Two sessions in the SAME
# checkout must queue, because they share all three.
#
# `flock` blocks rather than failing: a refusal turns "slow" into "the test never
# ran", and a test that silently did not run is the failure mode this repository
# keeps paying for. Verified: two concurrent full runs take 162 s (one alone takes
# 83 s), i.e. they really do queue.
#
# Not held when the caller asked for a single worker: `-n 0` is the documented way
# to run one file while iterating, and serialising that against a full run would
# make the fast path unusable for no safety gain.
def pytest_configure(config: pytest.Config) -> None:
    """Hold a per-checkout lock for the duration of the run."""
    import fcntl
    import hashlib
    import os

    # ⚠️ Only the CONTROLLER process takes the lock. xdist workers run their own
    # `pytest_configure` and would each try to acquire the same file — the first
    # version deadlocked exactly there (measured: two concurrent runs hung forever,
    # a single run exited 0 having executed nothing). The worker marker is what
    # distinguishes them; it is set by xdist before the worker's config is built.
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return
    if config.option.numprocesses == "0":
        return
    checkout = pathlib.Path(__file__).resolve().parents[1]
    root = str(checkout).encode()
    key = hashlib.sha256(root).hexdigest()[:16]
    path = pathlib.Path(tempfile.gettempdir()) / f"aiops-pytest-{key}.lock"
    handle = open(path, "w")  # noqa: SIM115 - held for the whole session
    fcntl.flock(handle, fcntl.LOCK_EX)
    # Keep the handle alive on the config so it is not garbage-collected (which
    # would release the lock mid-run). pytest tears the config down at exit, and
    # the OS releases the lock when the process dies even if it does not.
    config._aiops_lock_handle = handle
