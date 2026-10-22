"""`grant-tenant-access`：把运维账号 join 进租户 workspace（Dify 切租户的前提）。

三条判据，每条对着一种"看起来开通了、其实没开通"：

1. **成员关系真的建立**：调的是 inner_api 的 member 端点，参数逐字（workspace / account / email）。
2. **workspace 解析失败原样上抛**：缺行 / creating / failed / unknown 四个码各自对应不同
   的下一步，合成一句"开通失败"就把那个区别丢了。
3. **不碰 `current`**：这条命令只加成员，切哪个 workspace 是人在 Dify 里点的。替人改会让
   别的标签页看到状态跳动。

变异对照：把 `resolve` 换成常量 ⇒ 第 1 条红；吞掉 `DifyWorkspaceError` ⇒ 第 2 条红。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aiops_diagnostics.dify_inner_api import InnerApiError
from aiops_diagnostics.dify_tenant_access import DEFAULT_ROLE, GrantError, grant_tenant_access
from aiops_diagnostics.dify_workspaces import DifyWorkspaces

TENANT = "1942105476598861824"
ENVIRONMENT = "41"
WORKSPACE = "c3f871e1-ffd0-4a10-a84a-b8696f7acd25"
ACCOUNT = "8b218530-fd08-487a-9c83-6859e1f64674"
EMAIL = "41-1454348737375174658@aiops-login.invalid"


class _Inner:
    """记录 join_member 收到了什么。"""

    def __init__(self, *, fail: str | None = None) -> None:
        self.calls: list[dict[str, str]] = []
        self.fail = fail

    def join_member(self, *, workspace_id: str, account_id: str, email: str, role: str = "editor") -> None:
        self.calls.append(
            {"workspace_id": workspace_id, "account_id": account_id, "email": email, "role": role}
        )
        if self.fail:
            raise InnerApiError(self.fail, "upstream said no", outcome="failed")


def _workspaces(tmp_path: Path) -> DifyWorkspaces:
    ws = DifyWorkspaces(tmp_path / "gateway.db")
    ws.claim(TENANT, ENVIRONMENT, owner="test:1", lease_seconds=300)
    ws.mark_created(TENANT, ENVIRONMENT, WORKSPACE)
    return ws


def _run(tmp_path: Path, inner: _Inner, workspaces: DifyWorkspaces | None = None, **overrides: Any):
    kwargs: dict[str, Any] = {
        "tenant_id": TENANT,
        "environment": ENVIRONMENT,
        "workspaces": workspaces or _workspaces(tmp_path),
        "inner": inner,
        "account_id": ACCOUNT,
        "email": EMAIL,
    }
    kwargs.update(overrides)
    return grant_tenant_access(**kwargs)


def test_it_joins_with_the_exact_workspace_account_and_email(tmp_path: Path) -> None:
    """三个参数都必须**逐字**送达：Dify 按 `id == account` **且** `email == email` 找人。"""
    inner = _Inner()

    outcome = _run(tmp_path, inner)

    assert inner.calls == [
        {"workspace_id": WORKSPACE, "account_id": ACCOUNT, "email": EMAIL, "role": DEFAULT_ROLE}
    ]
    assert outcome.joined is True
    assert outcome.workspace_id == WORKSPACE


def test_the_role_defaults_to_editor(tmp_path: Path) -> None:
    """`admin` 能管成员，运维不需要；`owner` 每个 workspace 已有。"""
    inner = _Inner()
    _run(tmp_path, inner)
    assert inner.calls[0]["role"] == "editor"


def test_a_missing_workspace_row_keeps_its_own_code(tmp_path: Path) -> None:
    """缺行 ⇒ `DIFY_WORKSPACE_UNRESOLVED`，下一步是**开户**，不是"再试一次"。"""
    inner = _Inner()

    with pytest.raises(GrantError) as caught:
        _run(tmp_path, inner, workspaces=DifyWorkspaces(tmp_path / "gateway.db"))

    assert caught.value.code == "DIFY_WORKSPACE_UNRESOLVED"
    assert inner.calls == [], "解析不出 workspace 时不该发任何 join"


def test_an_inner_api_rejection_is_not_reported_as_success(tmp_path: Path) -> None:
    inner = _Inner(fail="INNER_API_REJECTED")

    with pytest.raises(GrantError) as caught:
        _run(tmp_path, inner)

    assert caught.value.code == "INNER_API_REJECTED"


def test_it_never_touches_the_current_workspace(tmp_path: Path) -> None:
    """这条命令只加成员。`current` 由人在 Dify 里点切换时改，命令替人改会让别的标签页跳动。"""
    inner = _Inner()
    _run(tmp_path, inner)

    assert set(inner.calls[0]) == {"workspace_id", "account_id", "email", "role"}, (
        "出现了第四个参数 —— 若它是 current/switch 一类，这条命令就越界了"
    )
