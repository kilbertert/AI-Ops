"""登录代理怎么知道"这个人该进哪个 workspace"（#883 / ADR-0059 §5.1）。

代理跑在 36，映射表在**网关的** `gateway.db`（41）。所以代理**不自己存第二份**：
它经网关的只读端点 ``GET /v1/dify/workspace`` 查（内部令牌，ADR-0067），拿到 UUID 之后
再经 `inner_api` 的 member 端点把人 join 进去。

## 三件事，各有各的边界

| 动作 | 谁做 | 为什么 |
|---|---|---|
| 解析 workspace | 代理问**网关** | 一份真值。代理自己存一份就是第二个会漂的源 |
| **建** workspace | **不是代理**（41 本机的 `ensure-workspace`） | 建与写映射必须同机同一次运行 |
| 把人 join 进去 | 代理，经 member 端点 | `create_tenant_member` 没有 `RBAC_ENABLED` 门槛 |

## 自助开通与成员加入

缺少工作区时由平台运营在Dify开通；登录代理只解析现有映射。加入失败记审计并拒绝签发
登录会话，避免用户登录后看到空工作区。保留账号映射，重新登录复用账号并重试成员加入。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from aiops_diagnostics.bounded_http import RequestSpec, join_url, request_json
from aiops_diagnostics.dify.dify_inner_api import DifyInnerApi, InnerApiError
from aiops_diagnostics.dify.dify_workspaces import (
    DIFY_WORKSPACE_CREATING,
    DIFY_WORKSPACE_FAILED,
    DIFY_WORKSPACE_UNKNOWN,
    DIFY_WORKSPACE_UNRESOLVED,
    DifyWorkspaceError,
)

_LOGGER = logging.getLogger("aiops.dify_login_proxy.workspace")


class WorkspaceUnavailable(DifyWorkspaceError):
    """解析不到可用 workspace，且**代理不该自己建**。

    继承 ``DifyWorkspaceError`` 是为了让调用方拿到同一个 ``code``（``UNRESOLVED`` /
    ``FAILED`` / ``UNKNOWN``），而不是被代理再包一层语义。
    """


@dataclass(frozen=True, slots=True)
class GatewayWorkspaceClient:
    """问网关"这个租户的 workspace 是哪一个"（ADR-0067 的只读端点）。

    凭据是**内部令牌**（HMAC + 时间窗），不是 Dify 的钥匙 —— 调用方是我们自己的进程。
    """

    base_url: str
    secret: str = field(repr=False, default="")
    expire_seconds: int = 300
    timeout: float = 10.0

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.secret)

    def resolve(self, tenant_id: str) -> str:
        import time

        from aiops_diagnostics.bounded_http import ErrorMapping, HttpFailure
        from aiops_diagnostics.http_auth import build_internal_token_headers

        def classified(failure: HttpFailure) -> Exception:
            payload: dict[str, Any] = {}
            if failure.code in {
                DIFY_WORKSPACE_UNRESOLVED,
                DIFY_WORKSPACE_CREATING,
                DIFY_WORKSPACE_FAILED,
                DIFY_WORKSPACE_UNKNOWN,
            }:
                code = failure.code
            else:
                code = "DIFY_WORKSPACE_LOOKUP_FAILED"
            payload["code"] = code
            return WorkspaceUnavailable(
                code,
                failure.detail or "网关没有给出 workspace：该租户可能还没有工作区",
                http_status=failure.status or 502,
                retryable=code == DIFY_WORKSPACE_CREATING,
            )

        mapping = ErrorMapping(
            auth_rejected=classified,
            http_error=classified,
            unavailable=classified,
            invalid_body=classified,
            invalid_envelope=classified,
        )
        payload = request_json(
            RequestSpec(
                url=join_url(self.base_url, "/v1/dify/workspace", query={"tenant_id": tenant_id}),
                headers=build_internal_token_headers(self.secret, self.expire_seconds, int(time.time())),
                timeout=self.timeout,
            ),
            mapping=mapping,
        )
        workspace_id = str(payload.get("workspace_id") or "") if isinstance(payload, dict) else ""
        if not workspace_id:
            # 200 但没有 id —— 与网关自己的契约不符，按"不可用"处理，不编一个值。
            raise WorkspaceUnavailable(
                "DIFY_WORKSPACE_LOOKUP_FAILED",
                "网关回了 200 但没有 workspace_id",
                http_status=502,
            )
        return workspace_id


def join_account(
    *,
    inner: DifyInnerApi,
    workspace_id: str,
    account_id: str,
    email: str,
    role: str = "editor",
) -> str:
    """把人 join 进 workspace；返回一个**审计用**的结果串（``joined`` / ``join_failed``）。

    **不抛**：workspace 已经存在，join 失败只意味着这个人打开控制台看到空列表 ——
    那比"登录页报错、他连自己的编辑面都进不去"轻。失败写日志，由运维按 workspace id 查。
    """
    try:
        inner.join_member(workspace_id=workspace_id, account_id=account_id, email=email, role=role)
    except InnerApiError as exc:
        _LOGGER.warning(
            "dify-login: join failed workspace=%s account=%s code=%s",
            workspace_id,
            account_id,
            exc.code,
        )
        return "join_failed"
    return "joined"


__all__ = ["GatewayWorkspaceClient", "WorkspaceUnavailable", "join_account"]
