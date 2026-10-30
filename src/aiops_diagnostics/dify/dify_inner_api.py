"""Dify 的 ``inner_api``：**建 workspace** 与 **把人 join 进去**（#880）。

Dify 的 console API 没有 ``POST /workspaces``；建 workspace 只有 inner_api 这一个口
（``POST /inner/api/enterprise/workspace``，要求 ``INNER_API=true`` + ``X-Inner-Api-Key``）。
人进 workspace 的正路是同一个口的 member 端点（``create_tenant_member``），**不是邀请**：
``RBAC_ENABLED=False`` 时邀请一个已激活账号会静默不生效（见 ADR-0059 §5.1b）。

## 两种失败必须分开，因为调用方的处置相反

Dify **没有删 workspace 的 API**：建出来的就是永久的。所以：

* ``outcome="failed"`` —— 收到了错误响应（4xx/5xx）或连接被拒。Dify 没有建，**可以重建**。
* ``outcome="unknown"`` —— 超时、读完响应前断开、200 但没有 workspace id。
  请求可能已经到达 Dify，**不能**当失败重试（那会造一个永久孤儿）；先按名字对账。

``bounded_http`` 把"连接被拒"与"超时"都归到 ``unavailable`` —— 从客户端看它们都是
"没拿到响应"，但对建 workspace 后果相反。所以这里看的是 ``HttpFailure.cause`` 的类型，
不是那条粗分类。

## 为什么不用 ``/console/api/workspaces/switch``

它会把该账号其它 join 的 ``current`` 全置假并 commit —— 对同一账号的并发请求可见。
后台自动化要的是 ``ADMIN_API_KEY + X-WORKSPACE-ID``（只在请求上下文里注入，不写库）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aiops_diagnostics.common.bounded_http import (
    UNAVAILABLE,
    ErrorMapping,
    HttpFailure,
    RequestSpec,
    join_url,
    json_body,
    request_json,
)

WORKSPACE_PATH = "/inner/api/enterprise/workspace"
MEMBER_PATH = "/inner/api/enterprise/workspace/member"

#: 读响应途中断掉的异常名。它们的共同点是**请求可能已经到达 Dify**，
#: 与"连接根本没建立"（ConnectionRefused）相反。
_MAY_HAVE_ARRIVED = ("Timeout", "RemoteDisconnected", "IncompleteRead", "BadStatusLine")


class InnerApiError(RuntimeError):
    """inner_api 这一跳没有给出可用结果。"""

    def __init__(self, code: str, message: str, *, outcome: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        #: ``"failed"``（Dify 明确没有建，可重建）或 ``"unknown"``（先对账）。
        self.outcome = outcome


@dataclass(frozen=True, slots=True)
class InnerApiSettings:
    """``AIOPS_GATEWAY_DIFY_INNER_API_*``。

    ``base_url`` 今天是 api 容器内网 IP:5001（36 本机可用）；转发器上线后换成
    ``http://127.0.0.1:15001``。**这是配置项而不是代码** —— 换路径不改一行代码。
    """

    base_url: str
    api_key: str = field(repr=False, default="")
    timeout: float = 15.0

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)


@dataclass(frozen=True, slots=True)
class CreatedWorkspace:
    workspace_id: str
    name: str


def _error_for(failure: HttpFailure, *, what: str) -> InnerApiError:
    """把一次失败归到 ``failed`` 或 ``unknown``，并对每种分类给可操作的文案。

    这一处就是整个模块的判断核心：``cause`` 的类型决定"请求有没有可能到达 Dify"。
    """
    cause_name = type(failure.cause).__name__
    if failure.kind == UNAVAILABLE and any(name in cause_name for name in _MAY_HAVE_ARRIVED):
        return InnerApiError(
            "INNER_API_OUTCOME_UNKNOWN",
            f"{what}的响应在读完之前断了：Dify 可能已经建出来，先按名字对账再决定",
            outcome="unknown",
        )
    code = {
        "auth_rejected": "INNER_API_KEY_REJECTED",
        "http_error": "INNER_API_REJECTED",
        "unavailable": "INNER_API_UNREACHABLE",
        "invalid_body": "INNER_API_UNREADABLE",
        "invalid_envelope": "INNER_API_UNREADABLE",
    }[failure.kind]
    # 解码/信封失败也是"未知"：Dify 已回 200 而我们读不懂它，那只可能是建了。
    outcome = "unknown" if failure.kind.startswith("invalid") else "failed"
    detail = f"（{failure.status}）" if failure.status else ""
    return InnerApiError(code, f"{what}没有成功{detail}：{failure.detail or failure.kind}", outcome=outcome)


def _mapping(what: str) -> ErrorMapping:
    build = lambda failure: _error_for(failure, what=what)  # noqa: E731 — 五类同一份判据
    return ErrorMapping(
        auth_rejected=build,
        http_error=build,
        unavailable=build,
        invalid_body=build,
        invalid_envelope=build,
    )


class DifyInnerApi:
    def __init__(self, settings: InnerApiSettings) -> None:
        self.settings = settings

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Inner-Api-Key": self.settings.api_key, "Content-Type": "application/json"}

    def create_workspace(self, name: str, owner_email: str) -> CreatedWorkspace:
        """建一个 workspace，owner 是一个**已存在**的账号（Dify 查不到就是 404）。"""
        payload = request_json(
            RequestSpec(
                url=join_url(self.settings.base_url, WORKSPACE_PATH),
                method="POST",
                headers=self._headers,
                body=json_body({"name": name, "owner_email": owner_email}),
                timeout=self.settings.timeout,
            ),
            mapping=_mapping("建 workspace"),
        )
        tenant = payload.get("tenant") if isinstance(payload, dict) else None
        tenant = tenant if isinstance(tenant, dict) else {}
        workspace_id = str(tenant.get("id") or "")
        if not workspace_id:
            # 200 但没有 id：Dify 可能建了也可能没建，唯一安全的结论是"未知"。
            raise InnerApiError(
                "INNER_API_UNREADABLE",
                "inner_api 回了 200 但没有 workspace id：按「结果未知」处理，先按名字对账",
                outcome="unknown",
            )
        return CreatedWorkspace(workspace_id=workspace_id, name=str(tenant.get("name") or name))

    def join_member(self, *, workspace_id: str, account_id: str, email: str, role: str = "editor") -> None:
        """把一个**已有账号**加进 workspace。``create_tenant_member``，不经邀请、不碰 ``current``。"""
        request_json(
            RequestSpec(
                url=join_url(self.settings.base_url, MEMBER_PATH),
                method="POST",
                headers=self._headers,
                body=json_body(
                    {"workspace_id": workspace_id, "account_id": account_id, "email": email, "role": role}
                ),
                timeout=self.settings.timeout,
            ),
            mapping=_mapping("加入成员"),
        )
