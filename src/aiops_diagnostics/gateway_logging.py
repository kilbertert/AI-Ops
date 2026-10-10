"""Where the gateway's own log lines go (#636).

**The defect this exists for.** Before this module, the gateway configured no
logging at all. `uvicorn.run()` brings up uvicorn's own `LOGGING_CONFIG`, which
attaches handlers to `uvicorn`, `uvicorn.error` and `uvicorn.access` **only** and
leaves the root logger at Python's WARNING default with **no handler**. Our
module loggers never set a level (so they inherit root) and propagate to root.

Measured on the production host (41), over the whole journal:

* **0** lines from any `aiops.` logger — `faq_platform_decision`,
  `operator_site_scope_empty`, `company token accepted`, and the
  `ORDER_AUTHENTICATION_UNAVAILABLE` warning all went nowhere;
* the `INFO:`/`ERROR:` lines are uvicorn's access log and uvicorn's crash dump;
* one *unprefixed* line does appear (`company JWT signature key is shorter …`),
  and that is the load-bearing clue: it is a **WARNING**. It reaches stderr
  through `logging.lastResort` — which Python attaches when no handler is
  configured *anywhere* — and `lastResort`'s level is WARNING. INFO is below it,
  so INFO was dropped before any handler was consulted.

So the fix is a handler on the root logger and an explicit level for our
namespaces; the WARNINGs that used to leak out unprefixed now carry a level
prefix like every other line in the journal.

**Why the level is a setting and not a constant.** Verbosity is an operational
knob: turning it up to diagnose a live problem is the normal reason anyone reads
these lines, and redeploying to change a log level is the wrong cost. The
default stays the level the code already assumed (`INFO`), so nothing gets
louder by default.

**Scope.** Only the gateway service configures this, from `serve()`. The CLI,
the tests and every library keep Python's defaults: `pytest` manages log capture
itself, and a library that configures logging on import is the thing every
consumer of it ends up working around.
"""

from __future__ import annotations

import logging
from logging.config import dictConfig

#: Our two logger namespaces, both rooted: modules use ``getLogger("aiops.x")``
#: and, in three places, ``getLogger(__name__)`` — which is ``aiops_diagnostics.x``.
#: Setting both is what makes the switch cover the code rather than part of it.
GATEWAY_LOGGER_NAMESPACES = ("aiops", "aiops_diagnostics")

DEFAULT_LOG_LEVEL = "INFO"

_FORMAT = "%(levelname)s %(name)s %(message)s"


def parse_log_level(value: str | None) -> int:
    """Map a configured level name onto a logging level; reject nonsense loudly.

    An unreadable level is a **startup error**, not a silent fallback to INFO: a
    typo in an operational knob that quietly does the opposite of what it says is
    how "the logs got switched on" becomes a false statement. The accepted
    spellings are `logging`'s own (`logging.getLevelNamesMapping`), so there is
    one vocabulary rather than a second one defined here.
    """
    if value is None or not value.strip():
        return logging.getLevelNamesMapping()[DEFAULT_LOG_LEVEL]
    name = value.strip().upper()
    resolved = logging.getLevelNamesMapping().get(name)
    if resolved is None:
        available = ", ".join(sorted(logging.getLevelNamesMapping()))
        raise ValueError(f"日志级别无效: {value!r}（可用: {available}）")
    return resolved


def configure_gateway_logging(level: str | None = None) -> int:
    """Attach a stderr handler and set our namespaces' level. Returns the level.

    The **root** logger carries the handler so one stream gets every line in
    order; the level is set on our namespaces only, which leaves the root at
    WARNING for third-party libraries. That split is the point: raising our
    verbosity must not turn `urllib3`/`botocore`-style chatter on with it.
    """
    resolved = parse_log_level(level)
    dictConfig(
        {
            "version": 1,
            # Not "True": `dictConfig` would disable every logger it does not
            # name, and pytest's capture and uvicorn's own loggers are both
            # configured outside this call.
            "disable_existing_loggers": False,
            "formatters": {"gateway": {"format": _FORMAT}},
            "handlers": {
                "gateway": {
                    "class": "logging.StreamHandler",
                    "formatter": "gateway",
                    "stream": "ext://sys.stderr",
                }
            },
            "root": {"handlers": ["gateway"], "level": logging.WARNING},
            "loggers": {name: {"level": resolved, "propagate": True} for name in GATEWAY_LOGGER_NAMESPACES},
        }
    )
    return resolved
