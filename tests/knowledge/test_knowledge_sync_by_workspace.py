"""#1056 / ADR-0122：workspace → 那个租户的 RAGFlow 库 → Dify dataset（内部入口）。

这条路由是「在 RAGFlow 建库 → Dify 自动出现」这条链的**最后一跳**。它只有三件事
值得反复验，其余都是实现细节：

1. **租户从哪来** —— 与 `/v1/internal/knowledge/open` 同一个洞形状（#979）：
   请求里出现租户参数必须是 **400**，不是忽略。忽略会让调用方以为自己指定成功了。
2. **失败不炸调用方** —— 桥把它挂在「添加知识库」弹窗上。上游不可达要回
   `created: 0`，不是 5xx；否则一次抖动会让运营看到红字，甚至挡住弹窗。
3. **只增不减** —— 这里没有删除路径。

前两条各自一条用例；第三条由「无删除动作」这条结构事实保证（`dify_knowledge_datasets.run`
里没有 delete 分支），所以没有单独的用例。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aiops_diagnostics.dify.dify_console_admin import ConsoleApiError
from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.http_auth import build_internal_token_headers

INTERNAL_SECRET = "internal-token-secret-0123456789"
ENVIRONMENT = "41"
TENANT = "1783000000000000001"
WORKSPACE = "c3f871e1-ffd0-4a10-a84a-b8696f7acd25"
API_TEMPLATE = "af41a28b-dd3b-4b7b-8155-0930d526a75a"


class _KBClient:
    """列库的替身。``rows`` 是 kb-service 的原始形状 —— **不改键名**。

    改键名正是 #1049 那个 bug：`libraries_for` 只认 `knowledge_base_id`/`id`，
    传 `dataset_id` 会让每一行被静默跳过，于是"同步成功、零写入"。
    """

    def __init__(self, rows: list[dict[str, Any]], *, raises: bool = False) -> None:
        self._rows = rows
        self._raises = raises
        self.listings: list[str] = []

    def for_tenant(self, tenant_id: str) -> _KBClient:
        bound = type(self)(self._rows, raises=self._raises)
        bound.listings = self.listings
        bound.listings.append(tenant_id)
        return bound

    def list_knowledge_bases(self) -> list[dict[str, Any]]:
        if self._raises:
            raise RuntimeError("kb-service is down")
        return self._rows


class _Runtime:
    media_signer = None

    def __init__(self, *, kb: _KBClient | None = None, workspaces: DifyWorkspaces | None = None) -> None:
        self.kb_search_client = kb
        self.dify_workspaces = workspaces

    def shutdown(self) -> None:
        pass


def _headers() -> dict[str, str]:
    return build_internal_token_headers(INTERNAL_SECRET, 300, int(time.time()))


def _insert_workspace(workspaces: DifyWorkspaces, *, setup_state: str = "ready") -> None:
    with workspaces._connection(write=True) as connection:  # noqa: SLF001 — the row is the fixture
        now = "2026-10-23T00:00:00+00:00"
        connection.execute(
            "INSERT INTO dify_workspaces"
            " (tenant_id, environment, workspace_id, setup_state, last_error, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, '', ?, ?)",
            (TENANT, ENVIRONMENT, WORKSPACE, setup_state, now, now),
        )


def _client(
    tmp_path: Path, *, kb: _KBClient | None = None, with_template: bool = True
) -> tuple[TestClient, DifyWorkspaces]:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
        environment=ENVIRONMENT,
        internal_token_secret=INTERNAL_SECRET,
        dify_console_base_url="http://dify.invalid",
        dify_console_api_key="console-key-0123456789",
        dify_external_knowledge_api_id=API_TEMPLATE if with_template else "",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    store = GatewayStore(settings.database_file)
    workspaces = DifyWorkspaces(settings.database_file)
    app = create_gateway_app(
        settings=settings,
        store=store,
        runtime=_Runtime(kb=kb, workspaces=workspaces),  # type: ignore[arg-type]
    )
    return TestClient(app), workspaces


@pytest.mark.parametrize("forbidden", ["tenant_id", "tenant", "environment"])
def test_a_request_carrying_a_tenant_is_refused_not_ignored(tmp_path: Path, forbidden: str) -> None:
    """**400，不是静默忽略** —— 与 `/v1/internal/knowledge/open` 同一条判据。

    "请求里能指定租户"就是 #979 那个洞的形状。忽略会让调用方以为自己指定成功了。
    """
    client, workspaces = _client(tmp_path)
    _insert_workspace(workspaces)

    response = client.post(
        "/v1/internal/knowledge/sync",
        params={"workspace": WORKSPACE, forbidden: "1783000000000000999"},
        headers=_headers(),
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "KNOWLEDGE_SYNC_PARAM_NOT_ACCEPTED"


def test_an_empty_workspace_is_refused_rather_than_defaulted(tmp_path: Path) -> None:
    """空 workspace 不回落 —— 落到"第一个租户"是这条链最坏的一种失败。"""
    client, _workspaces = _client(tmp_path)

    response = client.post("/v1/internal/knowledge/sync", headers=_headers())

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "KNOWLEDGE_SYNC_WORKSPACE_REQUIRED"


def test_without_the_api_template_the_route_is_absent(tmp_path: Path) -> None:
    """没配模板 id 就整条不启用 —— 不猜模板（ADR-0096 决策一）。"""
    client, workspaces = _client(tmp_path, with_template=False)
    _insert_workspace(workspaces)

    response = client.post("/v1/internal/knowledge/sync", params={"workspace": WORKSPACE}, headers=_headers())

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "KNOWLEDGE_SYNC_DISABLED"


def test_an_unreachable_kb_service_answers_zero_instead_of_failing(tmp_path: Path) -> None:
    """上游挂了 ⇒ ``created: 0``，不是 5xx。

    桥把这条路径挂在「添加知识库」弹窗上：一次上游抖动不该让运营看到红字，
    更不该挡住他选已有的库（ADR-0122 约束 1）。
    """
    client, workspaces = _client(tmp_path, kb=_KBClient([], raises=True))
    _insert_workspace(workspaces)

    response = client.post("/v1/internal/knowledge/sync", params={"workspace": WORKSPACE}, headers=_headers())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] == 0
    assert body.get("unavailable") is True


def test_the_tenant_comes_from_the_workspace_row(tmp_path: Path) -> None:
    """租户来自 workspace 反查，而且 kb-service 是按**那个租户**去列库的。

    Dify 写入在测试里没有上游，所以这条路由会抛 —— 那是**对的**：路由不吞异常，
    兜底在桥那一层（它把任何失败译成 ``created: 0``）。两层都兜会让真实故障
    在日志里消失。这里要验的只是"用哪个租户去列库"。
    """
    kb = _KBClient([])
    client, workspaces = _client(tmp_path, kb=kb)
    _insert_workspace(workspaces)

    # 具体到那条：Dify Console 不可达时 `dify_console_admin` 抛的就是它。
    with pytest.raises(ConsoleApiError):
        client.post("/v1/internal/knowledge/sync", params={"workspace": WORKSPACE}, headers=_headers())

    assert kb.listings == [TENANT]


def test_an_unknown_workspace_is_a_404(tmp_path: Path) -> None:
    """只查不建：未登记的 workspace 不会被顺手造出一行。"""
    client, _workspaces = _client(tmp_path, kb=_KBClient([]))

    response = client.post("/v1/internal/knowledge/sync", params={"workspace": WORKSPACE}, headers=_headers())

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "DIFY_WORKSPACE_UNRESOLVED"


def test_two_workspaces_sync_their_own_tenants(tmp_path: Path) -> None:
    """**不同的 workspace 同步不同的租户** —— review 指出的那条（P1）。

    桥的 `AIOPS_CONSOLE_BRIDGE_WORKSPACE_ID` 是**迁移期的静态回退值**，用它会在多
    workspace 部署里同步错租户，而运营看到的是自己那一页 —— 错误不报，只表现为
    "新库没出现"。所以判据是：换一个 workspace，kb-service 就要被按**另一个租户**
    问一次。
    """
    other_tenant = "1783000000000000002"
    other_workspace = "11111111-2222-3333-4444-555555555555"
    kb = _KBClient([])
    client, workspaces = _client(tmp_path, kb=kb)
    _insert_workspace(workspaces)
    with workspaces._connection(write=True) as connection:  # noqa: SLF001 — the row is the fixture
        now = "2026-10-23T00:00:00+00:00"
        connection.execute(
            "INSERT INTO dify_workspaces"
            " (tenant_id, environment, workspace_id, setup_state, last_error, created_at, updated_at)"
            " VALUES (?, ?, ?, 'ready', '', ?, ?)",
            (other_tenant, ENVIRONMENT, other_workspace, now, now),
        )

    for ws in (WORKSPACE, other_workspace):
        with pytest.raises(ConsoleApiError):
            client.post("/v1/internal/knowledge/sync", params={"workspace": ws}, headers=_headers())

    assert kb.listings == [TENANT, other_tenant], "两个 workspace 必须落到各自的租户"
