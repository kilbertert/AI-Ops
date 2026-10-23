"""#982：Dify workspace → 租户的 RAGFlow 会话（内部入口）。

这条路由是 PRD #981 的第一跳。它只有一件事值得反复验：**租户从哪来**。

`2026-10-22` 的知识库 `PUT` 越权（#979）根因是"按请求里的字段决定归属"。这条路由如果
接受 `tenant_id` 参数，就是同一个洞再开一次 —— 所以这里的判据不是"忽略"，是 **400**：
忽略会让调用方以为自己指定成功了。

三条边界，各自一种"看起来对、其实不对"：

1. **空 workspace 不回落。** 落到"第一个租户"是这条链路上最坏的一种失败。
2. **环境来自部署身份。** 同一个 workspace id 在另一个环境里是另一个问题。
3. **只查不建。** 查不到就是 404，不顺手造一行。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aiops_diagnostics.dify_workspaces import (
    DIFY_WORKSPACE_AMBIGUOUS,
    DIFY_WORKSPACE_UNRESOLVED,
    DifyWorkspaces,
)
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.http_auth import build_internal_token_headers
from aiops_diagnostics.kb_onboarding import encrypt_credential

INTERNAL_SECRET = "internal-token-secret-0123456789"
ENVIRONMENT = "41"
TENANT = "1783000000000000001"
OTHER_TENANT = "1783000000000000002"
WORKSPACE = "c3f871e1-ffd0-4a10-a84a-b8696f7acd25"


class _Runtime:
    kb_search_client = None
    media_signer = None

    def shutdown(self) -> None:
        pass


def _headers() -> dict[str, str]:
    return build_internal_token_headers(INTERNAL_SECRET, 300, int(time.time()))


def _insert_workspace(
    workspaces: DifyWorkspaces,
    *,
    tenant_id: str = TENANT,
    environment: str = ENVIRONMENT,
    workspace_id: str = WORKSPACE,
    setup_state: str = "ready",
) -> None:
    with workspaces._connection(write=True) as connection:  # noqa: SLF001 — the row is the fixture
        now = "2026-10-22T00:00:00+00:00"
        connection.execute(
            "INSERT INTO dify_workspaces"
            " (tenant_id, environment, workspace_id, setup_state, last_error, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, '', ?, ?)",
            (tenant_id, environment, workspace_id, setup_state, now, now),
        )


def _client(tmp_path: Path, **overrides: Any) -> tuple[TestClient, DifyWorkspaces, GatewayStore]:
    values: dict[str, Any] = {
        "data_home": tmp_path,
        "database_file": tmp_path / "gateway.db",
        "server_config_file": tmp_path / "production.env",
        "environment": ENVIRONMENT,
        "internal_token_secret": INTERNAL_SECRET,
        "ragflow_credential_key": "0123456789abcdef",
        "ragflow_console_base_url": "http://rag.invalid",
        "ragflow_console_public_url": "https://kb.example",
        "ragflow_console_public_pem": str(tmp_path / "public.pem"),
        "ragflow_console_cookie_domain": ".mall.qushiyun.com",
    }
    values.update(overrides)
    settings = GatewayServerSettings(**values)
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    # 用**真实**公钥：走到登录那一步会真的加密密码，假 PEM 会让"加密"变成
    # "随便编一个字符串"，把一条本该验到的路径短路掉。
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (tmp_path / "public.pem").write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    store = GatewayStore(settings.database_file)
    app = create_gateway_app(settings=settings, store=store, runtime=_Runtime())  # type: ignore[arg-type]
    return TestClient(app), DifyWorkspaces(settings.database_file), store


# ── 租户来源：只能来自 workspace 反查 ──────────────────────────────────────


@pytest.mark.parametrize("forbidden", ["tenant_id", "tenant", "environment"])
def test_a_request_carrying_a_tenant_is_refused_not_ignored(tmp_path: Path, forbidden: str) -> None:
    """**400，不是静默忽略。**

    忽略会让调用方以为自己指定成功了 —— 而"请求里能指定租户"正是 #979 那个洞的形状。
    明确拒绝才是把这条路封死。
    """
    client, _workspaces, _store = _client(tmp_path)

    response = client.get(
        "/v1/internal/knowledge/open",
        params={"workspace": WORKSPACE, forbidden: "1783000000000000999"},
        headers=_headers(),
        follow_redirects=False,
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "KNOWLEDGE_OPEN_PARAM_NOT_ACCEPTED"


def test_an_empty_workspace_is_a_400_not_a_default_tenant(tmp_path: Path) -> None:
    """空参数落到"第一个租户"是这条链路上最坏的一种失败。"""
    client, _workspaces, _store = _client(tmp_path)

    response = client.get("/v1/internal/knowledge/open", params={"workspace": "  "}, headers=_headers())

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "KNOWLEDGE_OPEN_WORKSPACE_REQUIRED"


def test_an_unknown_workspace_is_404(tmp_path: Path) -> None:
    client, _workspaces, _store = _client(tmp_path)

    response = client.get("/v1/internal/knowledge/open", params={"workspace": WORKSPACE}, headers=_headers())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == DIFY_WORKSPACE_UNRESOLVED


def test_a_workspace_only_resolves_inside_its_own_environment(tmp_path: Path) -> None:
    """同一个 workspace id 在另一个环境里是另一个问题 —— 不能穿环境命中。"""
    client, workspaces, _store = _client(tmp_path)
    _insert_workspace(workspaces, environment="95")

    response = client.get("/v1/internal/knowledge/open", params={"workspace": WORKSPACE}, headers=_headers())

    assert response.status_code == 404, response.text


def test_a_duplicated_workspace_refuses_instead_of_picking_one(tmp_path: Path) -> None:
    """一个 workspace 对应多行 = 数据坏了。挑一行等于随机选一个租户。"""
    client, workspaces, _store = _client(tmp_path)
    _insert_workspace(workspaces)
    _insert_workspace(workspaces, tenant_id=OTHER_TENANT)

    response = client.get("/v1/internal/knowledge/open", params={"workspace": WORKSPACE}, headers=_headers())

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == DIFY_WORKSPACE_AMBIGUOUS


@pytest.mark.parametrize("state", ["creating", "failed", "unknown"])
def test_a_workspace_that_is_not_usable_is_refused(tmp_path: Path, state: str) -> None:
    """`creating` / `failed` / `unknown` 换出来的会话属于一个"还没建好"的租户。"""
    client, workspaces, _store = _client(tmp_path)
    _insert_workspace(workspaces, setup_state=state)

    response = client.get("/v1/internal/knowledge/open", params={"workspace": WORKSPACE}, headers=_headers())

    assert response.status_code == 409, response.text


# ── 门的形状：内部令牌 + 未配置即 404 ─────────────────────────────────────


def test_the_route_requires_the_internal_token(tmp_path: Path) -> None:
    client, _workspaces, _store = _client(tmp_path)

    response = client.get("/v1/internal/knowledge/open", params={"workspace": WORKSPACE})

    assert response.status_code == 401


def test_the_route_is_absent_when_console_access_is_not_configured(tmp_path: Path) -> None:
    client, workspaces, _store = _client(tmp_path, ragflow_console_base_url="")
    _insert_workspace(workspaces)

    response = client.get("/v1/internal/knowledge/open", params={"workspace": WORKSPACE}, headers=_headers())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "KNOWLEDGE_CONSOLE_DISABLED"


# ── 开户状态：token 到得了这一步，但租户没开 RAGFlow ──────────────────────


def test_a_workspace_whose_tenant_was_never_onboarded_is_409(tmp_path: Path) -> None:
    """查得到 workspace、租户也真实，但那个租户没开 RAGFlow ⇒ 引导去开户，不是 500。"""
    client, workspaces, _store = _client(tmp_path)
    _insert_workspace(workspaces)

    response = client.get("/v1/internal/knowledge/open", params={"workspace": WORKSPACE}, headers=_headers())

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "KNOWLEDGE_TENANT_NOT_ONBOARDED"


def test_an_onboarded_tenant_reaches_the_ragflow_login_step(tmp_path: Path) -> None:
    """开户过的租户会走到真的去登录 RAGFlow —— 在上游不可达时得到 424。

    这里**不**断言 303：那需要一条真的 RAGFlow。本票的判据是"租户解析正确、能走到
    登录那一步"；303 + cookie 的形状由 `test_ragflow_console.py` 与真机验收覆盖。
    """
    client, workspaces, store = _client(tmp_path)
    _insert_workspace(workspaces)
    store.record_ragflow_tenant(
        TENANT,
        ENVIRONMENT,
        ragflow_email="t@kb.internal",
        credential_ciphertext=encrypt_credential("pw", "0123456789abcdef"),
        model_checked=True,
    )

    response = client.get("/v1/internal/knowledge/open", params={"workspace": WORKSPACE}, headers=_headers())

    assert response.status_code == 424, response.text
    assert response.json()["error"]["code"] == "KNOWLEDGE_UPSTREAM_UNAVAILABLE"
