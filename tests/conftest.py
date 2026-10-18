"""Test-suite-wide settings that would otherwise cost real wall-clock time.

One entry, and it is not a style choice — it is a measured 32x on the slowest
file in this suite.
"""

from __future__ import annotations

import socketserver

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
