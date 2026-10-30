"""``aiops-admin ensure-workspace``：把一个租户的 Dify workspace **真的建出来**（#880）。

## 顺序是设计的核心，不能调换

```
1. INSERT (tenant_id, environment, workspace_id='', setup_state='creating', lease…)
      -- UNIQUE 撞键 ⇒ 读回第一行，等它 ready / 或直接报 CREATING
2. 建 workspace：名字 = f"aiops-{environment}-{tenant_id}"，owner = 平台服务身份
3. UPDATE workspace_id, setup_state='created'
```

**为什么先占位**：同一个租户的两次首次登录可以同时越过"表里没有行"这个检查。只有
「唯一键 + 先插入」能串行化；反过来（先建后写）两个请求各建一个，后写的赢，前一个成为
**永久孤儿**（Dify 没有删 tenant 的 API）。

## ``creating`` 不是死胡同

租约到期后别的运行可以**抢占**，但抢占之前**必须先对账**：租约保护的是我们那行映射，
不是 Dify 的创建请求 —— 丢失响应、UPDATE 失败、请求仍在 Dify 里跑，都会在租约到期后
让下一个请求再建一个。对账用 admin key 查 ``all-workspaces`` 按**派生名**精确匹配：

| 命中 | 动作 |
|---|---|
| 0 个 | 安全，接管并重建 |
| 1 个 | 不重建，把它的 UUID 补写进映射，``state='created'`` |
| >1 个 | **人工介入**（不自动选；选错就是串租户） |

## 这张票建出来的仍然是空壳

无插件、无 provider 凭据、无 app —— 那是 #881 的边界。这里只把"建 workspace 是幂等的、
可恢复的"变成可复跑的判据。
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from typing import Any

from aiops_diagnostics.common.bounded_http import RequestSpec, join_url, request_json
from aiops_diagnostics.deployment_environment import canonical_environment
from aiops_diagnostics.dify.dify_inner_api import DifyInnerApi, InnerApiError
from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces, workspace_name

#: 租约长度。够一次创建往返（15s 超时 + 写回），又短到崩掉的创建不会把一个租户锁一小时。
DEFAULT_LEASE_SECONDS = 300.0

#: 平台服务身份：每个 workspace 的 owner。它是**服务身份**，不是人 —— 一个账号在 N 个
#: workspace 里都是 owner 是允许的，而我们从不用它的会话做事（走 ADMIN_API_KEY +
#: X-WORKSPACE-ID，那条分支按 workspace 找 owner 行、与 ``current`` 无关）。
PLATFORM_OWNER_EMAIL = "dify-admin@aiops.local"


class EnsureError(RuntimeError):
    """开户没有完成。``code`` 稳定、非本地化。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class EnsureOutcome:
    tenant_id: str
    environment: str
    state: str
    workspace_id: str
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "environment": self.environment,
            "state": self.state,
            "workspace_id": self.workspace_id,
            "note": self.note,
        }


def _lease_owner() -> str:
    """租约记的是"谁在建" —— 主机 + 进程，够两个运维在 journal 里分辨彼此。"""
    return f"{socket.gethostname()}:{os.getpid()}"


def reconcile_by_name(
    *, console_base_url: str, console_api_key: str, name: str, timeout: float = 15.0
) -> list[dict[str, Any]]:
    """用 admin key 查 ``all-workspaces``，按**派生名精确匹配**。

    这是抢占之前的唯一依据。命中 >1 时**不**替调用方选：``tenants.name`` 在 Dify 里没有
    唯一约束，两个同名 workspace 意味着有人手工建过，选错就是串租户。
    """
    from aiops_diagnostics.common.bounded_http import ErrorMapping, HttpFailure

    def unavailable(failure: HttpFailure) -> Exception:
        return EnsureError(
            "DIFY_RECONCILE_UNAVAILABLE",
            f"对账查不到 Dify 的 workspace 列表（{failure.status or failure.detail}）："
            "这时不能判断有没有建过，按「结果未知」处理，不要重建",
        )

    mapping = ErrorMapping(
        auth_rejected=lambda failure: EnsureError(
            "DIFY_CONSOLE_KEY_REJECTED", "查 all-workspaces 的 admin key 被拒"
        ),
        http_error=unavailable,
        unavailable=unavailable,
        invalid_body=unavailable,
        invalid_envelope=unavailable,
    )
    matches: list[dict[str, Any]] = []
    page, limit = 1, 100
    while True:
        payload = request_json(
            RequestSpec(
                url=join_url(
                    console_base_url,
                    "/console/api/all-workspaces",
                    query={"page": str(page), "limit": str(limit)},
                ),
                headers={"Authorization": f"Bearer {console_api_key}"},
                timeout=timeout,
            ),
            mapping=mapping,
        )
        rows = payload.get("data") if isinstance(payload, dict) else None
        for row in rows or []:
            if isinstance(row, dict) and str(row.get("name") or "") == name:
                matches.append(row)
        if not (isinstance(payload, dict) and payload.get("has_more")):
            break
        page += 1
        if page > 50:  # ponytail: 50 页 = 5000 个 workspace，超过就先人工看
            raise EnsureError("DIFY_RECONCILE_TOO_LARGE", "workspace 列表超过 50 页，先人工核对")
    return matches


def ensure_workspace(
    *,
    tenant_id: str,
    environment: str,
    workspaces: DifyWorkspaces,
    inner: DifyInnerApi,
    console_base_url: str,
    console_api_key: str,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    owner: str | None = None,
    company: str = "",
    log: Any = None,
) -> EnsureOutcome:
    """幂等、可恢复地建出 ``(tenant_id, environment)`` 的 workspace。

    返回四种结局之一，**没有第五种**：``ready`` / ``created`` / ``creating`` / ``failed``
    不会从 ``failed`` 里静默返回成功 —— 明确失败会被抛成异常，因为它需要人看见。
    """
    environment = canonical_environment(environment)
    # 公司名只影响**建的那一刻**取什么名字。取到之后写进表，以后对账按那一列 ——
    # 公司改名不会让对账失效（见 workspace_name 的 docstring）。
    name = workspace_name(environment, tenant_id, company=company or "")
    lease_owner = owner or _lease_owner()

    row = workspaces.row(tenant_id, environment)
    if row is not None:
        state = str(row["setup_state"])
        if state in {"created", "ready"}:
            return EnsureOutcome(
                tenant_id, environment, state, str(row["workspace_id"]), "已存在，未做任何写"
            )
        if state == "creating" and not _lease_expired(row):
            return EnsureOutcome(
                tenant_id, environment, state, str(row["workspace_id"]), "有人在建（租约未到期）"
            )
        if state in {"unknown", "creating"}:
            # 租约过期的 creating 与 unknown 都**先对账再决定** —— 它们可能已经建成了。
            # **按建的时候那个名字对账**，不按当前派生值：公司改名之后派生值变了，
            # 而 Dify 里那个 workspace 还叫旧名 —— 按派生值会命中 0 ⇒ 重建 ⇒ 孤儿。
            stored_name = str(row.get("workspace_name") or "") or name
            matches = reconcile_by_name(
                console_base_url=console_base_url,
                console_api_key=console_api_key,
                name=stored_name,
            )
            if len(matches) > 1:
                raise EnsureError(
                    "DIFY_WORKSPACE_AMBIGUOUS",
                    f"Dify 里有 {len(matches)} 个名为 {name} 的 workspace：人工介入，"
                    "机器不自动选（选错就是串租户）",
                )
            if len(matches) == 1:
                found = str(matches[0].get("id") or "")
                # 对账补 UUID 时把**当时那个名字**一并落表 —— 它是这次对账的依据。
                workspaces.mark_created(tenant_id, environment, found, name=stored_name)
                return EnsureOutcome(
                    tenant_id, environment, "created", found, "对账命中 1 个：补写 UUID，未重建"
                )
            # 0 个 ⇒ 安全，接管并重建（下面继续）。

    # 走到这里只有三种起点：没有行 / failed / （creating 或 unknown 且对账命中 0 个）。
    # 对账过的 unknown 才允许被接管 —— 直接在 unknown 上抢会把一个可能已建成的再建一遍。
    reconciled_unknown = row is not None and str(row["setup_state"]) == "unknown"
    if not workspaces.claim(
        tenant_id,
        environment,
        owner=lease_owner,
        lease_seconds=lease_seconds,
        allow_unknown=reconciled_unknown,
    ):
        latest = workspaces.row(tenant_id, environment) or {}
        return EnsureOutcome(
            tenant_id,
            environment,
            str(latest.get("setup_state", "creating")),
            str(latest.get("workspace_id", "")),
            "另一个运行先占位成功（并发到同一条 INSERT）",
        )

    try:
        created = inner.create_workspace(name, PLATFORM_OWNER_EMAIL)
    except InnerApiError as exc:
        if exc.outcome == "unknown":
            workspaces.mark_unknown(tenant_id, environment, error=f"{exc.code}: {exc.message}")
            raise EnsureError(exc.code, exc.message) from exc
        workspaces.mark_failed(tenant_id, environment, error=f"{exc.code}: {exc.message}")
        raise EnsureError(exc.code, exc.message) from exc

    workspaces.mark_created(tenant_id, environment, created.workspace_id, name=name)
    if log is not None:
        log(f"workspace created: {created.name} -> {created.workspace_id}")
    return EnsureOutcome(tenant_id, environment, "created", created.workspace_id, "新建")


def _lease_expired(row: dict[str, Any]) -> bool:
    from aiops_diagnostics.dify.dify_workspaces import _parse_time, now_utc

    raw = str(row.get("lease_expires_at") or "")
    if not raw:
        # 没有租约的 creating = 旧行或崩溃在插入之后：按"已过期"处理，好让对账接手。
        return True
    return _parse_time(raw) <= now_utc()


def render(outcome: EnsureOutcome) -> str:
    return json.dumps(outcome.as_dict(), ensure_ascii=False, indent=2)


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "PLATFORM_OWNER_EMAIL",
    "EnsureError",
    "EnsureOutcome",
    "ensure_workspace",
    "reconcile_by_name",
    "render",
]
