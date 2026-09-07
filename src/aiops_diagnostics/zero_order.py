"""Zero-order tool boundary.

T1 (issue #151) red line: a "general question" Agent must never be able to
read order-scoped business data. This module provides the minimal guard that
the zero-order toolset is built on: every order-affine tool is blocked at the
surface, before it can reach any data source. Real general-answer tools (e.g.
SOP/references-backed answers) are added by later tickets on top of this
boundary; this module is deliberately free of any data-source import.
"""

from __future__ import annotations

from aiops_diagnostics.agent_contracts import ToolName

# Tools that resolve against order-scoped business data (MySQL/TDengine/Redis).
# A zero-order toolset must never expose these to a model.
_ORDER_AFFINE_TOOLS: frozenset[ToolName] = frozenset(
    {
        ToolName.ORDER_SNAPSHOT,
        ToolName.FEE_SNAPSHOT,
        ToolName.DEVICE_SNAPSHOT,
        ToolName.GUN_TIMESERIES,
        ToolName.COMM_MESSAGES,
        ToolName.REDIS_SYNC,
        # KNOWN_RUNBOOK runs the deterministic DiagnosisEngine, which itself
        # reads orders/fees/streams — treat it as order-affine for now. A
        # references-only runbook variant (if needed) is a separate decision.
        ToolName.KNOWN_RUNBOOK,
    }
)

_ZERO_ORDER_BLOCKED_REASON = (
    "tool is order-scoped; not available to the zero-order (general question) toolset"
)


class ZeroOrderBlockedError(ValueError):
    """Raised when order-scoped tool use is attempted against the zero-order boundary."""


def assert_zero_order_toolset_allowed(
    tools: frozenset[ToolName] | set[ToolName] | tuple[ToolName, ...],
) -> None:
    """Fail closed if the proposed toolset exposes any order-affine tool.

    The zero-order toolset may only grow tools that are provably free of
    order-scoped reads (e.g. references-backed answers added by a later
    ticket). If any order-affine tool sneaks in, this raises — the boundary is
    fail-closed by construction, never by convention.
    """
    forbidden = _ORDER_AFFINE_TOOLS.intersection(tools)
    if forbidden:
        names = ", ".join(sorted(t.value for t in forbidden))
        raise ZeroOrderBlockedError(
            f"zero-order toolset exposed order-scoped tool(s): {names}. {_ZERO_ORDER_BLOCKED_REASON}"
        )


ZERO_ORDER_ALLOWED_TOOLS: frozenset[ToolName] = frozenset()
