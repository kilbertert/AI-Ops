"""Test-suite-wide settings that would otherwise cost real wall-clock time.

One entry, and it is not a style choice — it is a measured 32x on the slowest
file in this suite.
"""

from __future__ import annotations

import contextlib
import os
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


#: Where the slot files live. ⚠️ NOT `tempfile.gettempdir()`.
#:
#: The AFK agent runs inside a Docker sandbox (`.sandcastle/profile.ts`), and that
#: container has its OWN `/tmp` — the mounts are settings and MCP only, because
#: nothing needed a shared filesystem until now. Two sandboxed runs therefore take
#: slot 0 of two different filesystems and neither blocks, which is precisely the
#: scenario this gate exists for: the incident was several agent sessions at once.
#: Measured by review on the first version, which used `/tmp` and was correct on
#: the host and inert in every container.
#:
#: So the directory is fixed and host-backed, and `.sandcastle/profile.ts` mounts
#: it into every sandbox at the same path. A fixed path (rather than TMPDIR) is
#: what lets the mount be unconditional: a per-run TMPDIR would need the sandbox to
#: know the run's name before it starts.
SHARED_LOCK_DIR = pathlib.Path("/tmp/aiops-host-test-slots")


def _lock_dir() -> pathlib.Path:
    try:
        SHARED_LOCK_DIR.mkdir(mode=0o777, parents=True, exist_ok=True)
        # ⚠️ `mkdir(mode=...)` is masked by the process umask (027 on this host),
        # so the directory ends up 750 — owned by the host user, unwritable by the
        # sandbox's `agent` user, whose UID differs.
        SHARED_LOCK_DIR.chmod(0o777)
        return SHARED_LOCK_DIR
    except OSError:
        return pathlib.Path(tempfile.gettempdir())


def _host_lock_path(index: int) -> pathlib.Path:
    # ⚠️ The name carries NO repository: separate worktrees, separate clones and
    # SIBLING REPOSITORIES all draw from the same cores. Measured 2026-10-23: the
    # same incident shape was reproduced by sessions working in three different
    # projects, so a per-repository slot file would hand each one its own pair and
    # the host would hold six runs, not two. `host-test-slot-N.lock` is the name
    # the other two repositories use (health-flow #260, genesis-evidence #340);
    # all three must spell it the same way or the cap silently stops adding up.
    return _lock_dir() / f"host-test-slot-{index}.lock"


def _open_slot(path: pathlib.Path) -> object:
    """Open a slot file so that a DIFFERENT uid can open it too.

    ⚠️ The 0o666 is load-bearing, and it took two corrections. This process runs
    as the host user; the sandbox runs as `agent`, a different UID. Under umask
    027 the file is created 0640, so the container gets `PermissionError` — which
    surfaces as a test error inside the sandbox and looks like a broken test
    rather than a lock nobody can share. 0644 is still wrong: `flock` needs
    `O_RDWR`, and a read-only file cannot be opened for write by a non-owner.
    Measured: 0640 raised on open, 0644 raised on open, 0666 locks.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
    with contextlib.suppress(OSError):  # existing files keep their old mode
        os.fchmod(fd, 0o666)
    return os.fdopen(fd, "r+")


def _acquire(path: pathlib.Path, fcntl, time, what: str) -> object:
    """Take an exclusive flock, polling so the wait can be bounded."""
    handle = _open_slot(path)
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
    # ⚠️ `-n 0` is detected properly here — `int`, not the string "0", because xdist
    # converts the option: verified, `-n 0` yields `numprocesses == 0`. Getting this
    # wrong made the documented fast path take the lock: measured, two `-n 0` runs
    # were serialised 5 s / 10 s.
    #
    # ⚠️ But `-n 0` still takes the HOST SLOT, and that is a change from the first
    # version, which returned here and took neither. The reasoning it was written
    # with — "a single-worker run is cheap, do not make it queue" — looked at the
    # wrong cost. The incident was CPU-seconds, and a serial run burns a core just
    # as surely as a parallel one: seven serial sessions would have driven the same
    # load, they just divide their work differently. When the other two repositories
    # got this gate (health-flow #260, genesis-evidence #340) they had no xdist at
    # all, so EVERY run there is serial — keying on `-n` would have made the gate
    # inert, verified by positive control: slots occupied externally, pytest still
    # finished in 4 s. Keeping the two behaviours different across three copies of
    # one rule is the kind of drift this file's header warns about.
    #
    # What `-n 0` still skips is the CHECKOUT lock, which is what the fast path
    # actually needs: two runs in one checkout would otherwise serialise a 3 s file
    # run behind a 76 s full run. The host slot costs a file lock and only waits
    # when two runs are genuinely in flight.
    single_worker = (config.option.numprocesses or 0) == 0

    checkout = pathlib.Path(__file__).resolve().parents[1]
    key = hashlib.sha256(str(checkout).encode()).hexdigest()[:16]

    # ⚠️ Order is checkout first, host slot second, and it matters. A run queued
    # behind another in the SAME checkout must not squat a host slot while it waits
    # — measured on the reversed order: two runs in one checkout left the second
    # holding a slot it was not using, so a third checkout was throttled while only
    # one run was actually working. Deadlock is not a risk either way (a checkout
    # lock has exactly one holder and every path takes them in this order).
    held = []
    if not single_worker:
        held.append(
            _acquire(
                _lock_dir() / f"aiops-pytest-{key}.lock",
                fcntl,
                time,
                "another run in this checkout",
            )
        )
    held.append(_acquire_host_slot(fcntl, time, int(os.environ.get(SLOT_ENV) or MAX_CONCURRENT_RUNS)))

    # Keep the handles alive on the config so they are not garbage-collected (which
    # would release the locks mid-run). pytest tears the config down at exit, and
    # the OS releases them when the process dies even if it does not.
    config._aiops_lock_handles = held
