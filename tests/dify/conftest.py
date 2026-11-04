"""dify 目录内共享的测试数据构造。

只放**逐字相同**、曾被复制成两份的东西：复制版各自漂移过一次之后，
"两处都对"只能靠人肉比对。
"""

from __future__ import annotations

from pathlib import Path

#: 绑定行的默认租户（`_binding()` 的缺省值）。
DEFAULT_TENANT = "T-1"


def seed_default_workspace(db: Path) -> None:
    """给默认租户种一行 `ready`（#884/#885）。既有用例只关心"试运行能不能跑"。"""
    from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces

    workspaces = DifyWorkspaces(db)
    with workspaces._connection(write=True) as connection:  # noqa: SLF001 — 测试造的是行
        connection.execute(
            "INSERT OR REPLACE INTO dify_workspaces (tenant_id, environment, workspace_id,"
            " setup_state, created_at, updated_at) VALUES (?, ?, ?, 'ready',"
            " '2026-10-21T00:00:00+00:00', '2026-10-21T00:00:00+00:00')",
            (DEFAULT_TENANT, "41", "ws-default"),
        )
