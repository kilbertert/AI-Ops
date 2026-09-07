from __future__ import annotations

import pytest

from aiops_diagnostics.agent_contracts import ToolName
from aiops_diagnostics.zero_order import (
    ZERO_ORDER_ALLOWED_TOOLS,
    ZeroOrderBlockedError,
    assert_zero_order_toolset_allowed,
)

# Every order-scoped tool that exists today. If this list becomes stale
# (a new order-affine ToolName is added and not listed here), the test below
# will not catch it directly — but the boundary itself is fail-closed by
# intersection, so a new tool that IS listed is blocked automatically.
_ALL_ORDER_AFFINE = {
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


@pytest.mark.parametrize("tool", [t for t in _ALL_ORDER_AFFINE])
def test_each_order_affine_tool_is_blocked(tool: ToolName) -> None:
    """No single order-affine tool may enter a zero-order toolset."""
    with pytest.raises(ZeroOrderBlockedError):
        assert_zero_order_toolset_allowed({tool})


def test_any_mix_with_order_tool_fails_closed() -> None:
    """A mixed set containing even one order-affine tool is rejected wholesale."""
    with pytest.raises(ZeroOrderBlockedError):
        assert_zero_order_toolset_allowed({ToolName.ORDER_SNAPSHOT})
    with pytest.raises(ZeroOrderBlockedError):
        assert_zero_order_toolset_allowed({ToolName.GUN_TIMESERIES, ToolName.REDIS_SYNC})


def test_empty_toolset_is_valid_zero_order_surface() -> None:
    """An empty (or hypothetical references-only) toolset passes the boundary."""
    assert_zero_order_toolset_allowed(set())
    assert_zero_order_toolset_allowed(frozenset())


def test_error_reports_which_tool_is_forbidden() -> None:
    """The failure names the offending tool so a future ticket can adjust."""
    with pytest.raises(ZeroOrderBlockedError) as excinfo:
        assert_zero_order_toolset_allowed({ToolName.COMM_MESSAGES})
    assert "comm_messages" in str(excinfo.value)
