"""Dify 草稿工具身份；现役执行由原生工作流或节点清单负责。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DeclaredTool:
    """One ``agent_mode.tools[]`` entry, as Dify reported it.

    ``enabled`` is Dify's own switch: an operator who toggled a tool off expects
    it gone from production, so a disabled entry never enters the allow-list
    (#772 E). It is read even for entries we would refuse anyway, because
    "disabled" and "unknown identity" are different answers for the operator.
    """

    provider_type: str
    provider_id: str
    tool_name: str
    enabled: bool

    @property
    def identity(self) -> str:
        """How this entry is named back to the operator (never how it is keyed)."""
        return f"{self.provider_id}/{self.tool_name}" if self.provider_id else self.tool_name
