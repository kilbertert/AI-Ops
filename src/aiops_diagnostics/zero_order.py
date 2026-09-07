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

# The zero-order toolset is strict allow-list (default-deny): ONLY tools listed
# here may appear. Everything else — every order-affine tool AND any unknown or
# future ToolName not yet enumerated — is rejected, so the boundary cannot be
# quietly widened by adding a ToolName elsewhere without deliberately adding it
# here. This is fail-closed by construction, never by a maintained deny-list.
ZERO_ORDER_ALLOWED_TOOLS: frozenset[ToolName] = frozenset()

_ZERO_ORDER_BLOCKED_REASON = (
    "tool is not in the zero-order allow-list; order-scoped or unknown tools are denied for "
    "the general-question (zero-order) toolset"
)


class ZeroOrderBlockedError(ValueError):
    """Raised when a tool outside the zero-order allow-list is attempted."""


def assert_zero_order_toolset_allowed(
    tools: frozenset[ToolName] | set[ToolName] | tuple[ToolName, ...],
) -> None:
    """Fail closed unless every proposed tool is in the zero-order allow-list.

    The zero-order toolset may only hold tools that are provably free of
    order-scoped reads (e.g. references-backed answers added by a later
    ticket). Any other tool — including any future/unknown ToolName not yet
    enumerated — raises, so the boundary cannot silently widen. Accepts str
    members as well as ToolName for a robust error path.
    """
    non_allowed = set(tools) - set(ZERO_ORDER_ALLOWED_TOOLS)
    if non_allowed:
        names = ", ".join(sorted(str(t) for t in non_allowed))
        raise ZeroOrderBlockedError(
            f"zero-order toolset exposed non-allowed tool(s): {names}. {_ZERO_ORDER_BLOCKED_REASON}"
        )
