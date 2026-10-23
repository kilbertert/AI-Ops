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


# ── Bound how many full runs exist at once, host-wide ───────────────────────
# Measured 2026-10-23: several concurrent sessions each running the suite took this
# host down (`load average 5.28` → 79; sshd and tailscaled starved, the tailnet
# dropped and remote management was gone). Seven `pytest` processes were running.
#
# ⚠️ A per-checkout lock does NOT fix that, and the first version got this wrong: it
# keyed the lock on the checkout root, reasoned that "worktrees share no venv", and
# the incident had been several DIFFERENT worktrees — each took its own lock, none
# queued, `-n 4` per run times seven runs is still 28 workers. The resource being
# exhausted is the host, so the mutex has to be the host.
#
# TWO locks, because they protect two different things:
#   * a host-wide slot (MAX_CONCURRENT_RUNS), which is what the incident needed;
#   * a per-checkout lock, still needed because two runs in the SAME checkout share
#     a venv, a SQLite fixture and a cache dir — serialising those is about
#     correctness, not load.
#
# ⚠️ The lock lives HERE rather than in `tools/test.sh` because that wrapper is not
# the only entry point — `implement.md`, the review prompts and CI all spell
# `uv run pytest` directly, and a lock in a wrapper those paths never call protects
# nobody. Measured by review on the first version: the AFK gates bypassed it.
#
# `flock` blocks rather than failing: a refusal turns "slow" into "the test never
# ran", and a test that silently did not run is the failure mode this repository
# keeps paying for. Nothing here waits forever either — see WAIT_SECONDS.
#
# Not held when the caller asked for a single worker: `-n 0` is the documented way
# to run one file while iterating. It costs one worker and is already the fast
# path; queueing it behind a full run would make it unusable for no safety gain.

#: How many full runs the host will hold at once. Derived from what the box has
#: survived: one `-n auto` run on 8 cores measured 44.5 s and was fine, so the worst
#: case here (2 slots x `-n 4`) is the same 8 workers that were fine. The incident
#: was seven runs, i.e. ~56.
MAX_CONCURRENT_RUNS = 2

#: Give up rather than wait forever. A run that has queued this long is not going to
#: produce a result anyone is still waiting for, and failing loudly beats a silent
#: hang — which is exactly what the deadlocked first version did (measured: two
#: concurrent runs hung with no output until killed).
WAIT_SECONDS = 1800

#: Overridable so a CI box with nothing else on it can widen the gate. Read at
#: acquire time, not import time, so the environment is the caller's.
SLOT_ENV = "AIOPS_PYTEST_SLOTS"


def _host_lock_path(index: int) -> pathlib.Path:
    # Not keyed on the checkout: separate worktrees and separate clones of this repo
    # on one machine draw from the same cores.
    return pathlib.Path(tempfile.gettempdir()) / f"aiops-pytest-host-{index}.lock"


def _acquire(path: pathlib.Path, fcntl, time, what: str) -> object:
    """Take an exclusive flock, polling so the wait can be bounded."""
    handle = open(path, "w")  # noqa: SIM115 - held for the whole session
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                raise pytest.UsageError(
                    f"waited over {WAIT_SECONDS}s for {what}; refusing to queue "
                    f"forever. Check for a stuck pytest, or raise {SLOT_ENV}/"
                    "WAIT_SECONDS."
                ) from None
            time.sleep(0.5)


def _acquire_host_slot(fcntl, time, slots: int) -> object:
    """Lowest free host-wide slot, or a bounded wait.

    `flock` has no queue, so "wait for slot 0" would leave a run blocked behind
    slot 0 while slot 1 sits idle; probing every slot is what makes N the actual
    concurrency.
    """
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        for index in range(slots):
            handle = open(_host_lock_path(index), "w")  # noqa: SIM115
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                continue
            return handle
        if time.monotonic() >= deadline:
            raise pytest.UsageError(
                f"all {slots} host test slots have been busy for {WAIT_SECONDS}s; "
                f"refusing to queue forever. Raise {SLOT_ENV} or WAIT_SECONDS."
            )
        time.sleep(0.5)


def pytest_configure(config: pytest.Config) -> None:
    """Take this checkout's lock and a host slot for the duration of the run."""
    import fcntl
    import hashlib
    import os
    import time

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
    key = hashlib.sha256(str(checkout).encode()).hexdigest()[:16]

    # ⚠️ Order is checkout first, host slot second, and it matters. A run queued
    # behind another in the SAME checkout must not squat a host slot while it waits
    # — measured on the reversed order: two runs in one checkout left the second
    # holding a slot it was not using, so a third checkout was throttled while only
    # one run was actually working. Deadlock is not a risk either way (a checkout
    # lock has exactly one holder and every path takes them in this order).
    held = [
        _acquire(
            pathlib.Path(tempfile.gettempdir()) / f"aiops-pytest-{key}.lock",
            fcntl,
            time,
            "another run in this checkout",
        )
    ]
    held.append(_acquire_host_slot(fcntl, time, int(os.environ.get(SLOT_ENV) or MAX_CONCURRENT_RUNS)))

    # Keep the handles alive on the config so they are not garbage-collected (which
    # would release the locks mid-run). pytest tears the config down at exit, and
    # the OS releases them when the process dies even if it does not.
    config._aiops_lock_handles = held
