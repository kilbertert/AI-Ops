"""1:1:1 的三元组核对（#927 / PRD #923）。

一个租户要"真的能用知识库"，三样东西必须在位，而且**互相指着对方**：

```
公司租户  ──1:1──  Dify 租户工作区      （dify_workspaces / Dify 的 tenants 表）
          ──1:1──  RAGFlow 租户         （ragflow_tenants / kb-service 的 tenancy.db）
```

今天要判断这件事，只能人肉查三张表：网关 SQLite、Dify 的 Postgres、RAGFlow 的 MySQL。
本模块把它变成一条命令，**对每个租户给出三个是/否**，缺哪一个就说哪一个 —— 而不是
一个"有问题"的笼统结论。

## 为什么三项分开报，而不是合成一个布尔

三种半成品**修法完全不同**：

| 缺什么 | 运营看到什么 | 该做什么 |
|---|---|---|
| 只缺 workspace | 进不了 Dify | 重跑开户（#924） |
| 只缺 RAGFlow 租户 | Dify 里没有库可选 | 重跑开户（#924） |
| 两个都在但没校验过模型 | 建了库、永远解析失败 | 重跑开户，它会补齐 |

把它们压成"不完整"，下一个人还得再来查一遍三张表，那正是这条命令要消灭的活。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from aiops_diagnostics.gateway_store import GatewayStore

#: 一个租户的核对结果。三个字段各自独立，见模块 docstring 的表。
OK = "ok"
MISSING_WORKSPACE = "missing-workspace"
MISSING_RAGFLOW = "missing-ragflow-tenant"
UNVERIFIED_MODEL = "model-unverified"
WORKSPACE_DANGLING = "workspace-not-in-dify"


@dataclass(frozen=True, slots=True)
class TenantTriple:
    tenant_id: str
    environment: str
    workspace_id: str
    ragflow_email: str
    model_checked_at: str
    problems: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "environment": self.environment,
            "workspace_id": self.workspace_id,
            "ragflow_email": self.ragflow_email,
            "model_checked_at": self.model_checked_at,
            "ok": self.ok,
            "problems": list(self.problems),
        }


def check(
    *,
    store: GatewayStore,
    environment: str,
    dify_workspace_names: Mapping[str, str],
) -> list[TenantTriple]:
    """Cross-check every tenant this environment knows about.

    ``dify_workspace_names`` is ``{workspace_id: workspace_name}`` as reported by
    Dify's own ``all-workspaces`` listing — passed in rather than fetched here so
    the check itself is a pure function of three snapshots, and a test can drive
    it without a Dify console or a kb-service.

    The tenant set is the **union** of both registries: a tenant that has a
    workspace but no RAGFlow half (or the reverse) is exactly the half-built case
    this exists to surface, so neither side may define the set alone.

    ``dify_workspace_names`` being empty means "Dify was not consulted" (e.g. the
    console key is unset). In that case workspace existence is **not** judged —
    reporting every tenant as broken because we did not look would be worse than
    saying nothing about it.
    """
    from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces

    ragflow = {str(row["tenant_id"]): row for row in store.list_ragflow_tenants(environment)}
    # `dify_workspaces` lives in its own store class (ADR-0059) over the same
    # SQLite file; reading it through that class rather than a second hand-written
    # query is what keeps this check from drifting from the writer.
    workspaces = {
        str(row["tenant_id"]): row for row in DifyWorkspaces(store.path).list_for_environment(environment)
    }

    results: list[TenantTriple] = []
    for tenant_id in sorted(set(ragflow) | set(workspaces)):
        problems: list[str] = []
        ragflow_row = ragflow.get(tenant_id)
        workspace_row = workspaces.get(tenant_id)

        workspace_id = str((workspace_row or {}).get("workspace_id") or "")
        state = str((workspace_row or {}).get("setup_state") or "")
        if workspace_row is None or not workspace_id or state not in {"created", "ready"}:
            problems.append(MISSING_WORKSPACE)
        elif dify_workspace_names and workspace_id not in dify_workspace_names:
            # We recorded a workspace Dify does not have. The mapping is a lie
            # until someone re-runs onboarding, and every later step will fail
            # in a way that points somewhere else.
            problems.append(WORKSPACE_DANGLING)

        if ragflow_row is None:
            problems.append(MISSING_RAGFLOW)
        elif not str(ragflow_row.get("model_checked_at") or ""):
            problems.append(UNVERIFIED_MODEL)

        results.append(
            TenantTriple(
                tenant_id=tenant_id,
                environment=environment,
                workspace_id=workspace_id or str((ragflow_row or {}).get("workspace_id") or ""),
                ragflow_email=str((ragflow_row or {}).get("ragflow_email") or ""),
                model_checked_at=str((ragflow_row or {}).get("model_checked_at") or ""),
                problems=tuple(problems),
            )
        )
    return results


def render(results: list[TenantTriple], *, expected_name_for: Any = None) -> str:
    """Human-readable table. One line per tenant, then a summary.

    Deliberately prints the **derived workspace name** next to the id when a
    caller supplies ``expected_name_for``: that name is the one thing an operator
    can search for in Dify's own console, and "the id is right but the name
    differs" is a real state (someone renamed it).
    """
    lines: list[str] = []
    for item in results:
        name = expected_name_for(item.tenant_id) if expected_name_for else ""
        head = f"{item.tenant_id}  {'OK' if item.ok else ','.join(item.problems)}"
        lines.append(head)
        lines.append(f"  workspace : {item.workspace_id or '(none)'}" + (f"  name={name}" if name else ""))
        lines.append(f"  ragflow   : {item.ragflow_email or '(none)'}")
        lines.append(f"  model     : {item.model_checked_at or '(never verified)'}")
    broken = [item for item in results if not item.ok]
    lines.append("")
    lines.append(f"{len(results)} tenant(s), {len(broken)} incomplete")
    return "\n".join(lines)
