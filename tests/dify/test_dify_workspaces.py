"""#879：环境身份 + `dify_workspaces` + `resolve_workspace`（只查不建）。

四条判据，每条对着一种"看起来做了、其实没做"：

1. **失败必须可区分。** 一个返回 ``""`` 的解析函数会让调用方把"没有 workspace"
   与"workspace 是空"混成一件事，而后者在 Dify 上的含义是平台根。所以这里逐码断言，
   并在最后**把解析函数改回返回空串**做一次变异，证明这些用例会红。
2. **环境只来自部署身份。** 请求带了 ``environment`` 就是 400，且**不**回退到部署身份。
3. **撞号 fail-closed。** 同一 ``tenant_id`` 在两个环境下的两行互不可见。
4. **判据只有一处。** 网关与登录代理共用 ``canonical_environment``：网关拒绝的值
   代理也拒绝。两处各写一份"非空"检查会让它们漂开。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aiops_diagnostics.common.http_auth import build_internal_token_headers
from aiops_diagnostics.deployment_environment import canonical_environment
from aiops_diagnostics.dify.dify_login_provision import (
    DifyConsoleSettings,
    DifyProvisionError,
    account_email_for,
)
from aiops_diagnostics.dify.dify_workspaces import (
    DIFY_WORKSPACE_CREATING,
    DIFY_WORKSPACE_FAILED,
    DIFY_WORKSPACE_UNKNOWN,
    DIFY_WORKSPACE_UNRESOLVED,
    DifyWorkspaceError,
    DifyWorkspaces,
)
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore

INTERNAL_SECRET = "internal-token-secret-0123456789"
ENVIRONMENT = "41"
OTHER_ENVIRONMENT = "95"
TENANT = "1783000000000000001"


def _insert(
    workspaces: DifyWorkspaces,
    *,
    tenant_id: str = TENANT,
    environment: str = ENVIRONMENT,
    workspace_id: str = "",
    setup_state: str = "creating",
    last_error: str = "",
) -> None:
    """Write one row the way the provisioning ticket (#880) will.

    Deliberately reaches the table directly: this ticket owns the *shape* of the
    row and the *reading* of it, not the provisioning flow.
    """
    with workspaces._connection(write=True) as connection:  # noqa: SLF001 — the row is the fixture
        now = "2026-10-20T00:00:00+00:00"
        connection.execute(
            """
            INSERT INTO dify_workspaces
                (tenant_id, environment, workspace_id, setup_state, last_error, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (tenant_id, environment, workspace_id, setup_state, last_error, now, now),
        )


# ── 1. 解析：三态 + 可区分的失败 ───────────────────────────────────────────


def test_a_ready_row_resolves_to_its_workspace_id(tmp_path: Path) -> None:
    workspaces = DifyWorkspaces(tmp_path / "gateway.db")
    _insert(workspaces, workspace_id="ws-ready", setup_state="ready")

    assert workspaces.resolve(TENANT, ENVIRONMENT) == "ws-ready"


def test_created_also_resolves(tmp_path: Path) -> None:
    """`created` 是"建出来了、后续步骤还没跑完"。对"是哪一个"这个问题答案相同。"""
    workspaces = DifyWorkspaces(tmp_path / "gateway.db")
    _insert(workspaces, workspace_id="ws-created", setup_state="created")

    assert workspaces.resolve(TENANT, ENVIRONMENT) == "ws-created"


@pytest.mark.parametrize(
    ("setup_state", "workspace_id", "code", "retryable"),
    [
        ("creating", "", DIFY_WORKSPACE_CREATING, True),
        ("failed", "", DIFY_WORKSPACE_FAILED, False),
        ("unknown", "", DIFY_WORKSPACE_UNKNOWN, False),
    ],
)
def test_each_unusable_state_has_its_own_code(
    tmp_path: Path, setup_state: str, workspace_id: str, code: str, retryable: bool
) -> None:
    """**彼此可区分**是这一段的主要产出：调用方据此决定重试 / 重建 / 对账。"""
    workspaces = DifyWorkspaces(tmp_path / "gateway.db")
    _insert(workspaces, workspace_id=workspace_id, setup_state=setup_state)

    with pytest.raises(DifyWorkspaceError) as caught:
        workspaces.resolve(TENANT, ENVIRONMENT)
    assert caught.value.code == code
    assert caught.value.retryable is retryable


def test_no_row_is_unresolved_and_the_message_says_so(tmp_path: Path) -> None:
    workspaces = DifyWorkspaces(tmp_path / "gateway.db")

    with pytest.raises(DifyWorkspaceError) as caught:
        workspaces.resolve(TENANT, ENVIRONMENT)
    assert caught.value.code == DIFY_WORKSPACE_UNRESOLVED
    assert caught.value.http_status == 404
    assert caught.value.retryable is False, "缺行不是「稍后重试」—— 那会让人一直等一个不会发生的事"


def test_a_failed_row_carries_the_reason_it_can_be_rebuilt_from(tmp_path: Path) -> None:
    workspaces = DifyWorkspaces(tmp_path / "gateway.db")
    _insert(workspaces, workspace_id="", setup_state="failed", last_error="connection refused")

    with pytest.raises(DifyWorkspaceError) as caught:
        workspaces.resolve(TENANT, ENVIRONMENT)
    assert "connection refused" in caught.value.message
    assert "可重建" in caught.value.message, "消息要能指导下一步：失败是可重建的，未知才是要先对账"


def test_a_state_saying_ready_without_an_id_is_not_resolvable(tmp_path: Path) -> None:
    """一行不可用的 `create` / `ready` 不能变成"答案是一个空串"。

    空 workspace_id 在 Dify 一侧的含义是**平台根** —— 调用方照它去读写，就是打到
    别人的工作区。所以这一行按"没有"回答，而不是把它当作有效值交出去。
    """
    workspaces = DifyWorkspaces(tmp_path / "gateway.db")
    _insert(workspaces, workspace_id="", setup_state="ready")

    with pytest.raises(DifyWorkspaceError) as caught:
        workspaces.resolve(TENANT, ENVIRONMENT)
    assert caught.value.code == DIFY_WORKSPACE_UNRESOLVED


def test_resolve_never_creates_a_row(tmp_path: Path) -> None:
    """解析函数**只查不建**。建是开户那一步的显式动作（#880）。

    变异对照：把 `resolve` 改成"缺行就插一行再返回空串"，本用例变红。
    """
    workspaces = DifyWorkspaces(tmp_path / "gateway.db")
    with pytest.raises(DifyWorkspaceError):
        workspaces.resolve(TENANT, ENVIRONMENT)

    assert not workspaces.exists(TENANT, ENVIRONMENT), "解析函数不得留下任何行"


def test_exists_sees_rows_resolve_refuses(tmp_path: Path) -> None:
    """`exists` 与 `resolve` 不是同一个问题：开户要问前者，使用方要问后者。"""
    workspaces = DifyWorkspaces(tmp_path / "gateway.db")
    _insert(workspaces, setup_state="creating")

    assert workspaces.exists(TENANT, ENVIRONMENT) is True
    with pytest.raises(DifyWorkspaceError):
        workspaces.resolve(TENANT, ENVIRONMENT)


# ── 2. 撞号：两个环境互不可见 ──────────────────────────────────────────────


def test_the_same_tenant_in_two_environments_does_not_cross(tmp_path: Path) -> None:
    """41 与 95 的 `tenant_id` 各自生成，撞号是可能的；撞号时**不能**串租户。

    这是 `environment` 列存在的全部理由。变异对照：把解析函数改成只按 `tenant_id`
    查，本用例读到的会是另一个环境的 workspace。
    """
    workspaces = DifyWorkspaces(tmp_path / "gateway.db")
    _insert(workspaces, environment=ENVIRONMENT, workspace_id="ws-41", setup_state="ready")
    _insert(workspaces, environment=OTHER_ENVIRONMENT, workspace_id="ws-95", setup_state="ready")

    assert workspaces.resolve(TENANT, ENVIRONMENT) == "ws-41"
    assert workspaces.resolve(TENANT, OTHER_ENVIRONMENT) == "ws-95"


def test_the_unique_key_is_the_tenant_environment_pair(tmp_path: Path) -> None:
    import sqlite3

    workspaces = DifyWorkspaces(tmp_path / "gateway.db")
    _insert(workspaces)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(workspaces, workspace_id="ws-second")


# ── 3. 环境标识：唯一判据、不归一化 ─────────────────────────────────────────


@pytest.mark.parametrize("value", ["41", "95", "prod-41", "prod_41", "a", "a1"])
def test_canonical_forms_are_accepted(value: str) -> None:
    assert canonical_environment(value) == value


@pytest.mark.parametrize("value", ["", "   ", "prod.41", "prod/41", "Prod-41", "-41", "41-", "a" * 33])
def test_non_canonical_forms_are_refused_not_normalised(value: str) -> None:
    """**不归一化**：`prod.41` 与 `prod/41` 化成同一个值，就是把两个部署并成一个。"""
    with pytest.raises(ValueError):
        canonical_environment(value)


def test_the_gateway_and_the_login_proxy_share_one_predicate() -> None:
    """两个组件对"我在哪个环境"的答案必须逐字相同 —— 否则 `dify_workspaces` 会漂。

    这条用例打的是**判据只有一处**这个事实：把同一个值同时喂给两边，结论必须一致。
    写第二份更宽松的"非空"检查（允许 `prod.41`）会让本用例在网关侧报错而代理侧通过，
    而那种漂移在运行时的表现是"某个租户的工作区有时找得到、有时找不到"。
    """
    accepted = ["41", "95", "prod-41", "prod_41"]
    refused = ["", "prod.41", "prod/41", "Prod-41"]

    for value in accepted:
        assert canonical_environment(value) == value
        DifyConsoleSettings(base_url="http://dify.invalid", secret_key="k", environment=value)

    for value in refused:
        with pytest.raises(ValueError):
            canonical_environment(value)
        with pytest.raises(DifyProvisionError):
            DifyConsoleSettings(base_url="http://dify.invalid", secret_key="k", environment=value)
        with pytest.raises(DifyProvisionError):
            account_email_for("1001", environment=value)


def test_the_gateway_reads_the_deployment_identity_from_its_own_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_GATEWAY_ENVIRONMENT", ENVIRONMENT)
    monkeypatch.setenv("AIOPS_GATEWAY_SERVER_CONFIG_FILE", str(tmp_path / "gateway.env"))
    (tmp_path / "gateway.env").write_text("# test\n", encoding="utf-8")

    settings = GatewayServerSettings.from_env()
    assert settings.environment == ENVIRONMENT


def test_a_non_canonical_deployment_identity_is_a_startup_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_GATEWAY_ENVIRONMENT", "prod.41")
    monkeypatch.setenv("AIOPS_GATEWAY_SERVER_CONFIG_FILE", str(tmp_path / "gateway.env"))
    (tmp_path / "gateway.env").write_text("# test\n", encoding="utf-8")

    with pytest.raises(ValueError):
        GatewayServerSettings.from_env().validate()


def test_serving_without_an_environment_refuses_to_start(tmp_path: Path, monkeypatch) -> None:
    """漏配 ⇒ 进程不起。让它起来的表现是"某个租户的工作区有时找得到、有时找不到"。"""
    from typer.testing import CliRunner

    from aiops_diagnostics.gateway_server import app as gateway_app

    monkeypatch.setenv("AIOPS_HOME", str(tmp_path))
    monkeypatch.setenv("AIOPS_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("AIOPS_DATA_HOME", str(tmp_path / "data"))
    # `serve` validates the private config file before it looks at anything else, so a
    # missing one would fail for the wrong reason and this case would prove nothing.
    config = tmp_path / "production.env"
    config.write_text("# test\n", encoding="utf-8")
    monkeypatch.setenv("AIOPS_GATEWAY_SERVER_CONFIG_FILE", str(config))
    monkeypatch.delenv("AIOPS_GATEWAY_ENVIRONMENT", raising=False)
    # `serve` refuses before it ever opens a socket, so no port is taken here.
    result = CliRunner().invoke(gateway_app, ["serve"])
    assert result.exit_code == 2
    assert "AIOPS_GATEWAY_ENVIRONMENT is required" in result.output


def test_the_m2_launchers_also_require_the_deployment_identity() -> None:
    """`gateway_server serve` 不是唯一的启动路径（评审指出，成立）。

    M2 隔离栈的入口是两个 launcher，它们构造 app 后自己调 `uvicorn.run` —— 只把必备性
    写在 `serve` 里，等于让这台"不知道自己在哪个环境"的网关照常起监听，而这条路径正是
    连真实业务库的那一条。判据必须只有一处实现，且三个入口都要过它。
    """
    root = Path(__file__).resolve().parents[2]
    for launcher in (
        root / "tools/m2_close/gateway_launcher.py",
        root / "tools/m2_close/m3live/identity_launcher.py",
    ):
        text = launcher.read_text(encoding="utf-8")
        assert "require_deployment_identity(" in text, f"{launcher.name} 没有过部署身份这道门"
        # 顺序也要紧：门必须在建库与建 app **之前**，否则进程已经动了状态才拒绝。
        assert text.index("require_deployment_identity(") < text.index("GatewayStore("), (
            f"{launcher.name} 在建库之后才判部署身份"
        )


def test_require_deployment_identity_refuses_an_empty_value() -> None:
    from aiops_diagnostics.deployment_environment import require_deployment_identity

    with pytest.raises(ValueError, match="AIOPS_GATEWAY_ENVIRONMENT is required"):
        require_deployment_identity("")


def test_the_m2_template_declares_the_deployment_identity() -> None:
    """`compose.yaml` 用这份模板作 `env_file`；缺这一行时**栈起不来**。"""
    template = (Path(__file__).resolve().parents[2] / "deploy/m2-live-03/gateway.env.template").read_text(
        encoding="utf-8"
    )
    assert "AIOPS_GATEWAY_ENVIRONMENT=41" in template


def test_the_token_expiry_is_read_from_the_private_config_beside_its_secret(
    tmp_path: Path, monkeypatch
) -> None:
    """密钥从私有配置读，窗口也必须能在那儿调（评审指出，成立）。

    只看进程 env 时，运维改了 `production.env` 里紧挨着密钥的那一行会**静默不生效** ——
    而读者有充分理由认为两者同源。
    """
    from aiops_diagnostics.common.private_files import write_private_text

    config = tmp_path / "gateway.env"
    write_private_text(
        config,
        "AIOPS_GATEWAY_INTERNAL_TOKEN_SECRET=secret-0123456789abcdef\n"
        "AIOPS_GATEWAY_INTERNAL_TOKEN_EXPIRE_SECONDS=120\n",
    )
    monkeypatch.setenv("AIOPS_GATEWAY_SERVER_CONFIG_FILE", str(config))
    monkeypatch.delenv("AIOPS_GATEWAY_INTERNAL_TOKEN_EXPIRE_SECONDS", raising=False)

    assert GatewayServerSettings.from_env().internal_token_expire_seconds == 120


# ── 4. 只读端点：环境来自部署身份，不是请求 ───────────────────────────────


class _Runtime:
    """Only the two surfaces the adapter reads; this file is not about the search."""

    kb_search_client = None
    media_signer = None

    def shutdown(self) -> None:
        pass


def _client(tmp_path: Path, **overrides: Any) -> tuple[TestClient, DifyWorkspaces]:
    values: dict[str, Any] = {
        "data_home": tmp_path,
        "database_file": tmp_path / "gateway.db",
        "server_config_file": tmp_path / "production.env",
        "environment": ENVIRONMENT,
        "internal_token_secret": INTERNAL_SECRET,
    }
    values.update(overrides)
    settings = GatewayServerSettings(**values)
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    store = GatewayStore(settings.database_file)
    app = create_gateway_app(settings=settings, store=store, runtime=_Runtime())  # type: ignore[arg-type]
    return TestClient(app), DifyWorkspaces(settings.database_file)


def _headers() -> dict[str, str]:
    import time

    return build_internal_token_headers(INTERNAL_SECRET, 300, int(time.time()))


def test_the_endpoint_returns_the_workspace_id_for_a_ready_row(tmp_path: Path) -> None:
    client, workspaces = _client(tmp_path)
    _insert(workspaces, workspace_id="ws-ready", setup_state="ready")

    response = client.get("/v1/dify/workspace", params={"tenant_id": TENANT}, headers=_headers())

    assert response.status_code == 200, response.text
    assert response.json()["workspace_id"] == "ws-ready"


@pytest.mark.parametrize(
    ("setup_state", "expected_status", "expected_code"),
    [
        ("creating", 409, DIFY_WORKSPACE_CREATING),
        ("failed", 409, DIFY_WORKSPACE_FAILED),
        ("unknown", 409, DIFY_WORKSPACE_UNKNOWN),
    ],
)
def test_the_endpoint_maps_each_state_to_its_own_code(
    tmp_path: Path, setup_state: str, expected_status: int, expected_code: str
) -> None:
    client, workspaces = _client(tmp_path)
    _insert(workspaces, setup_state=setup_state)

    response = client.get("/v1/dify/workspace", params={"tenant_id": TENANT}, headers=_headers())

    assert response.status_code == expected_status, response.text
    assert response.json()["error"]["code"] == expected_code


def test_no_row_is_404_with_the_unresolved_code(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    response = client.get("/v1/dify/workspace", params={"tenant_id": TENANT}, headers=_headers())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == DIFY_WORKSPACE_UNRESOLVED


def test_an_environment_parameter_is_refused_and_does_not_fall_back(tmp_path: Path) -> None:
    """带了 `environment` 就 400 —— **不**回退到部署身份。

    只忽略它会让调用方以为自己拿到了另一个环境的答案，而"跨环境查同一个 tenant_id"
    正是这条端点的越权口。
    """
    client, workspaces = _client(tmp_path)
    _insert(workspaces, workspace_id="ws-ready", setup_state="ready")

    response = client.get(
        "/v1/dify/workspace",
        params={"tenant_id": TENANT, "environment": OTHER_ENVIRONMENT},
        headers=_headers(),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "DIFY_WORKSPACE_ENVIRONMENT_NOT_ACCEPTED"


def test_the_endpoint_requires_the_internal_token(tmp_path: Path) -> None:
    client, workspaces = _client(tmp_path)
    _insert(workspaces, workspace_id="ws-ready", setup_state="ready")

    missing = client.get("/v1/dify/workspace", params={"tenant_id": TENANT})
    assert missing.status_code == 401

    wrong = client.get(
        "/v1/dify/workspace",
        params={"tenant_id": TENANT},
        headers=build_internal_token_headers("not-the-secret-0123456789", 300, 1_700_000_000),
    )
    assert wrong.status_code == 401


def test_an_unconfigured_secret_leaves_the_route_404(tmp_path: Path) -> None:
    """未配置 ⇒ 整条路由 404，而不是"不需要令牌"（与其余 Dify 路由同一条机制）。"""
    client, _ = _client(tmp_path, internal_token_secret="")

    response = client.get("/v1/dify/workspace", params={"tenant_id": TENANT})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DIFY_WORKSPACE_API_UNAVAILABLE"


def test_an_invalid_tenant_id_is_a_400_not_a_500(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    response = client.get("/v1/dify/workspace", params={"tenant_id": "bad tenant!"}, headers=_headers())

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "DIFY_WORKSPACE_TENANT_INVALID"
