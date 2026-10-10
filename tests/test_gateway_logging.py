"""The gateway's own log lines reach somewhere (#636).

Before `gateway_logging`, the gateway configured no logging. `uvicorn.run()`
attaches handlers to `uvicorn*` only and leaves the root logger at WARNING with
no handler; our namespaces inherit that, so an `INFO` line from
`aiops.gateway_runtime` was created and then dropped. Measured on the production
host over the whole journal: **zero** lines from any `aiops.` logger, while one
*WARNING* did appear — unprefixed, through `logging.lastResort`, which is the
only handler Python attaches when nothing else is configured.

So the assertion that matters is not "the config was called"; it is "an INFO line
from our namespace lands on the stream", with the uvicorn-only arrangement as the
control that must fail. `configure_gateway_logging` installs a root handler, so
these tests save and restore the logging state they disturb.
"""

from __future__ import annotations

import io
import logging

import pytest

from aiops_diagnostics.gateway_logging import (
    GATEWAY_LOGGER_NAMESPACES,
    configure_gateway_logging,
    parse_log_level,
)


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record) or record.getMessage())


@pytest.fixture
def logging_state():
    """Restore the root logger and our namespaces exactly as they were."""
    root = logging.getLogger()
    saved_root = (root.handlers[:], root.level)
    saved_loggers = {
        name: (
            logging.getLogger(name).level,
            logging.getLogger(name).handlers[:],
            logging.getLogger(name).propagate,
        )
        for name in GATEWAY_LOGGER_NAMESPACES
    }
    yield
    root.handlers[:] = saved_root[0]
    root.setLevel(saved_root[1])
    for name, (level, handlers, propagate) in saved_loggers.items():
        logger = logging.getLogger(name)
        # NOT setLevel(0): `dictConfig` sets a named logger's level to NOTSET
        # when the config does not name it, and NOTSET means "inherit root",
        # which is the state these loggers were in before the fix. Restoring the
        # numeric level (0) preserves exactly that.
        logger.level = level
        logger.handlers[:] = handlers
        logger.propagate = propagate


def test_an_info_line_from_our_namespace_reaches_the_stream(logging_state) -> None:
    """The load-bearing claim: INFO from `aiops.*` is delivered, not dropped.

    Written against the ROOT handler because that is where it was lost. The
    control is `logging.lastResort`, whose level is WARNING — which is why the
    defect dropped INFO silently and let one WARNING through.
    """
    stream = io.StringIO()
    configure_gateway_logging("INFO")
    # Redirect whatever the root handler writes; the handler is on root, so this
    # is the same stream an operator's journal would carry.
    logging.getLogger().handlers[0].stream = stream

    logging.getLogger("aiops.gateway_runtime").info("clarification_answered code=X")

    assert "clarification_answered" in stream.getvalue(), (
        "INFO 从 aiops.* 必须被投递 —— 丢掉它正是 #636 的现象"
    )


def test_the_level_is_configurable_and_our_namespaces_carry_it(logging_state) -> None:
    """Raising verbosity is an operational knob, so the configured level must
    actually land on our logger — not only on the root."""
    configure_gateway_logging("DEBUG")
    # `getEffectiveLevel`, not `.level`: only the namespace ROOT (`aiops`) is
    # named in the config, so children inherit it — asserting `.level` here
    # would fail while the behaviour is correct.
    assert logging.getLogger("aiops.gateway").getEffectiveLevel() == logging.DEBUG

    stream = io.StringIO()
    logging.getLogger().handlers[0].stream = stream
    logging.getLogger("aiops.gateway").debug("verbose detail")
    assert "verbose detail" in stream.getvalue()


def test_a_third_party_logger_is_not_made_verbose_by_our_knob(logging_state) -> None:
    """One switch must not turn the whole dependency tree's INFO on with it —
    that is why the level goes on our namespaces and the ROOT keeps WARNING."""
    configure_gateway_logging("DEBUG")
    assert logging.getLogger().level == logging.WARNING
    assert logging.getLogger("urllib3").isEnabledFor(logging.INFO) is False


@pytest.mark.parametrize("value", ["info", " INFO ", "debug"])
def test_level_names_are_accepted_case_and_space_insensitively(logging_state, value: str) -> None:
    configure_gateway_logging(value)
    assert logging.getLogger("aiops.gateway").getEffectiveLevel() == parse_log_level(value)


def test_a_typo_is_a_startup_error_not_a_silent_default() -> None:
    """An operational knob that quietly does the opposite of what it says is how
    "the logs got switched on" becomes a false statement."""
    with pytest.raises(ValueError, match="日志级别无效"):
        parse_log_level("loud")


def test_no_level_configured_means_the_level_the_code_assumed() -> None:
    """The default must not make anything louder or quieter than before."""
    assert parse_log_level(None) == logging.INFO
    assert parse_log_level("") == logging.INFO
