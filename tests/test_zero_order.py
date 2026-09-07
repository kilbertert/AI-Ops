from __future__ import annotations

import pytest

from aiops_diagnostics.agent_contracts import ToolName
from aiops_diagnostics.zero_order import (
    ZERO_ORDER_ALLOWED_TOOLS,
    ZeroOrderBlockedError,
    assert_zero_order_toolset_allowed,
)

# Every tool that exists today. The boundary is now strict allow-list
# (default-deny): ALL of these must be rejected, because the allow-list is
# intentionally empty. This is stronger than a deny-list — even a future
# ToolName not in this list is rejected automatically.
_ALL_EXISTING_TOOLS = {
    ToolName.ORDER_SNAPSHOT,
    ToolName.FEE_SNAPSHOT,
    ToolName.DEVICE_SNAPSHOT,
    ToolName.GUN_TIMESERIES,
    ToolName.COMM_MESSAGES,
    ToolName.REDIS_SYNC,
    ToolName.KNOWN_RUNBOOK,
}


def test_zero_order_toolset_starts_empty() -> None:
    """The zero-order toolset has no capabilities yet (T1 red line: nothing to read)."""
    assert frozenset() == ZERO_ORDER_ALLOWED_TOOLS


@pytest.mark.parametrize("tool", [t for t in _ALL_EXISTING_TOOLS])
def test_each_existing_tool_is_blocked(tool: ToolName) -> None:
    """No single existing tool may enter a zero-order toolset (allow-list empty)."""
    with pytest.raises(ZeroOrderBlockedError):
        assert_zero_order_toolset_allowed({tool})


def test_mix_with_any_tool_fails_closed() -> None:
    """A mixed set containing even one non-allowed tool is rejected wholesale."""
    with pytest.raises(ZeroOrderBlockedError):
        assert_zero_order_toolset_allowed({ToolName.ORDER_SNAPSHOT})
    with pytest.raises(ZeroOrderBlockedError):
        assert_zero_order_toolset_allowed({ToolName.GUN_TIMESERIES, ToolName.REDIS_SYNC})


def test_unknown_future_tool_is_rejected() -> None:
    """Strict default-deny: even a hypothetical future ToolName not enumerated
    today cannot enter the zero-order toolset — the boundary widens only by
    deliberately extending the allow-list, never by forgetfulness."""
    with pytest.raises(ZeroOrderBlockedError):
        assert_zero_order_toolset_allowed({"order_snapshot"})  # str form, not ToolName


def test_empty_toolset_is_valid_zero_order_surface() -> None:
    """An empty (or hypothetical references-only) toolset passes the boundary."""
    assert_zero_order_toolset_allowed(set())
    assert_zero_order_toolset_allowed(frozenset())


def test_error_reports_which_tool_is_forbidden() -> None:
    """The failure names the offending tool so a future ticket can adjust."""
    with pytest.raises(ZeroOrderBlockedError) as excinfo:
        assert_zero_order_toolset_allowed({ToolName.COMM_MESSAGES})
    assert "comm_messages" in str(excinfo.value)
