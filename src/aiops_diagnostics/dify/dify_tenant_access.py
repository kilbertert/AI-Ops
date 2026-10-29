"""给**运维账号**开通某个租户 workspace 的访问权（Dify 控制台里切租户的前提）。

Dify 的 workspace 切换器（`app/components/main-nav/components/workspace-switcher.tsx`）
只在一个条件下隐藏：这个账号的 workspace 列表为空。**成员关系一到，切换器就出现**，
点一下调 `/workspaces/switch`，服务端按 `tenant_account_joins` 校验后改 `current`。

所以"公司运维能在 Dify 里切租户"这件事**不需要改 Dify**，只需要把人 join 进去。
2026-10-22 之前一直以为要 fork 前端重建镜像 —— 那个判断是错的（在编译后的 chunk 里搜
组件名，缩略名把名字改掉了，没搜到）。

## 为什么是"按需"而不是"开户时全量 join"

25 个 `ROLE_ADMIN` 里 18 个半年以上没登录过。全量 join 等于给休眠账号开每一个租户的
workspace 访问权。这里只提供**一条命令**，需要时跑一次。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiops_diagnostics.dify.dify_inner_api import DifyInnerApi, InnerApiError
from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaceError, DifyWorkspaces

#: 给运维账号的角色。`admin` 能管成员，运维不需要；`owner` 每个 workspace 已有。
DEFAULT_ROLE = "editor"


@dataclass(frozen=True, slots=True)
class GrantOutcome:
    tenant_id: str
    environment: str
    workspace_id: str
    account_id: str
    role: str
    joined: bool
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "environment": self.environment,
            "workspace_id": self.workspace_id,
            "account_id": self.account_id,
            "role": self.role,
            "joined": self.joined,
            "note": self.note,
        }


class GrantError(RuntimeError):
    """这一跳没有给出可用结果。``code`` 是给运维看的下一步依据。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def grant_tenant_access(
    *,
    tenant_id: str,
    environment: str,
    workspaces: DifyWorkspaces,
    inner: DifyInnerApi,
    account_id: str,
    email: str,
    role: str = DEFAULT_ROLE,
) -> GrantOutcome:
    """把 ``account_id`` join 进 ``(tenant_id, environment)`` 那个 workspace。

    **幂等**：`create_tenant_member` 已有 join 时只改角色，不重复建行。
    **不碰 `current`**：切哪个 workspace 是人在 Dify 里点的，不是这条命令决定的 ——
    替人改 `current` 会让别的标签页/别的请求看到状态跳动。
    """
    if not account_id.strip():
        raise GrantError("ACCOUNT_ID_REQUIRED", "需要 Dify 的账号 id（不是公司 user id）")
    if not email.strip():
        raise GrantError("ACCOUNT_EMAIL_REQUIRED", "需要该账号在 Dify 里的 email（与账号行必须逐字一致）")

    try:
        workspace_id = workspaces.resolve(tenant_id, environment)
    except DifyWorkspaceError as exc:
        # 四个码原样上抛：缺行 / 正在建（可重试）/ 建失败 / 结果未知，下一步各不相同。
        raise GrantError(exc.code, exc.message) from exc

    try:
        inner.join_member(workspace_id=workspace_id, account_id=account_id, email=email, role=role)
    except InnerApiError as exc:
        raise GrantError(exc.code, f"join 失败：{exc.message}") from exc

    return GrantOutcome(
        tenant_id=tenant_id,
        environment=environment,
        workspace_id=workspace_id,
        account_id=account_id,
        role=role,
        joined=True,
        note="该账号现在能在 Dify 的 workspace 切换器里看到它",
    )


__all__ = ["DEFAULT_ROLE", "GrantError", "GrantOutcome", "grant_tenant_access"]
