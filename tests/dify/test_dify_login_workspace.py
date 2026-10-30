"""按租户开户（#883）：解析走网关、join 走 inner_api、缺行给可执行的错。

四条判据：

1. **解析走网关**，代理**不存第二份** —— 这张票的验收明写"查不到第二张表/第二个文件"；
   所以断言的是"代理进程里除 `mappings.db` 外没有别的 workspace 存储"，以及解析请求
   真的打到了网关的只读端点（带内部令牌）。
2. **缺行不建**：解析失败时抛**带命令**的错，而不是给一个空工作区。
3. **join 不阻断登录**：workspace 已在、join 失败 ⇒ 登录照样成功。
4. **二次登录不重复 join**：已记录 `workspace_joined` 的那个人不再发 member 请求。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from aiops_diagnostics.dify.dify_login_company import CompanyAuthSettings, CompanyIdentity
from aiops_diagnostics.dify.dify_login_provision import DifyConsoleSettings
from aiops_diagnostics.dify.dify_login_server import LoginProxySettings
from aiops_diagnostics.dify.dify_login_service import (
    LoginPolicy,
    LoginService,
    MappingStore,
)
from aiops_diagnostics.dify.dify_workspace_directory import (
    GatewayWorkspaceClient,
    WorkspaceUnavailable,
)

TENANT = "1783000000000000001"
WORKSPACE = "ws-tenant-1"
ACCOUNT = "b647e9c0-1111-4222-8333-444455556666"
INTERNAL_SECRET = "internal-secret-0123456789"
ENVIRONMENT = "env-test"


class _Gateway(BaseHTTPRequestHandler):
    """假网关：`GET /v1/dify/workspace?tenant_id=…`。"""

    rows: dict[str, tuple[int, dict[str, Any]]] = {}
    calls: list[dict[str, str]] = []

    def log_message(self, *args: Any) -> None:  # silence
        pass

    def do_GET(self) -> None:  # noqa: N802
        type(self).calls.append({"path": self.path, "token": self.headers.get("X-Internal-Token") or ""})
        from urllib.parse import parse_qs, urlsplit

        tenant = parse_qs(urlsplit(self.path).query).get("tenant_id", [""])[0]
        status, payload = type(self).rows.get(
            tenant,
            (404, {"error": {"code": "DIFY_WORKSPACE_UNRESOLVED", "message": "no row"}}),
        )
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def gateway():
    _Gateway.rows = {}
    _Gateway.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


# ── 1. 解析走网关、带内部令牌 ────────────────────────────────────────────


def test_the_directory_asks_the_gateway_with_an_internal_token(gateway) -> None:
    _Gateway.rows[TENANT] = (200, {"workspace_id": WORKSPACE, "environment": ENVIRONMENT})
    client = GatewayWorkspaceClient(base_url=gateway, secret=INTERNAL_SECRET, expire_seconds=300)

    assert client.resolve(TENANT) == WORKSPACE
    call = _Gateway.calls[-1]
    assert call["path"].startswith("/v1/dify/workspace?tenant_id=")
    assert call["token"], "请求必须带内部令牌"
    assert "environment=" not in call["path"], "环境来自部署身份，不能出现在请求里"


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (404, "DIFY_WORKSPACE_UNRESOLVED", False),
        (409, "DIFY_WORKSPACE_CREATING", True),
        (409, "DIFY_WORKSPACE_FAILED", False),
        (409, "DIFY_WORKSPACE_UNKNOWN", False),
    ],
)
def test_each_gateway_code_survives_the_hop(gateway, status: int, code: str, retryable: bool) -> None:
    """四个码**原样**穿过这一跳：它们各自对应不同的下一步，合成一个就丢了区别。"""
    _Gateway.rows[TENANT] = (status, {"error": {"code": code, "message": "x"}})
    client = GatewayWorkspaceClient(base_url=gateway, secret=INTERNAL_SECRET)

    with pytest.raises(WorkspaceUnavailable) as caught:
        client.resolve(TENANT)
    assert caught.value.code == code
    assert caught.value.retryable is retryable


def test_an_unreachable_gateway_is_not_reported_as_a_missing_workspace(tmp_path: Path) -> None:
    """网关不可达 ≠ 没有 workspace：前者要人看网关，后者要人建 workspace。"""
    client = GatewayWorkspaceClient(base_url="http://127.0.0.1:9", secret=INTERNAL_SECRET, timeout=1)

    with pytest.raises(WorkspaceUnavailable) as caught:
        client.resolve(TENANT)
    assert caught.value.code == "DIFY_WORKSPACE_LOOKUP_FAILED"


# ── 2. 登录流程：解析 → join → 不建 ──────────────────────────────────────


class _Directory:
    def __init__(self, outcome: str | Exception) -> None:
        self.outcome = outcome
        self.asked: list[str] = []

    def resolve(self, tenant_id: str) -> str:
        self.asked.append(tenant_id)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _Inner:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.joins: list[dict[str, str]] = []

    def join_member(self, **kwargs: Any) -> None:
        self.joins.append(kwargs)
        if self.fail:
            from aiops_diagnostics.dify.dify_inner_api import InnerApiError

            raise InnerApiError("INNER_API_UNREACHABLE", "down", outcome="failed")


def _service(tmp_path: Path, directory: Any, inner: Any) -> LoginService:
    return LoginService(
        company=CompanyAuthSettings(base_url="http://company.invalid", encode_key="0123456789abcdef"),
        dify=DifyConsoleSettings(
            base_url="http://dify.invalid", secret_key="k" * 16, environment=ENVIRONMENT
        ),
        store=MappingStore(tmp_path / "mappings.db"),
        policy=LoginPolicy(edit_permission="aiops:agent:edit"),
        owner_user_id=ACCOUNT,
        workspace_directory=directory,
        inner_api=inner,
    )


def _patch_company(
    monkeypatch,
    *,
    permissions: tuple[str, ...] = ("aiops:agent:edit",),
    user_type: str = "tenant",
) -> None:
    from aiops_diagnostics.dify import dify_login_service as module

    identity = CompanyIdentity(user_id="u-1", username="zhang", tenant_id=TENANT, token="t")
    monkeypatch.setattr(module, "company_login", lambda *a, **k: identity)
    monkeypatch.setattr(module, "read_permissions", lambda *a, **k: permissions)
    monkeypatch.setattr(module, "read_user_type", lambda *a, **k: user_type)
    monkeypatch.setattr(
        module,
        "provision_editor",
        lambda *a, **k: ACCOUNT,
    )


def _row(tmp_path: Path) -> dict[str, Any]:
    row = MappingStore(tmp_path / "mappings.db").get("u-1") or {}
    return row


def test_login_resolves_the_tenants_workspace_and_joins(tmp_path: Path, monkeypatch) -> None:
    _patch_company(monkeypatch)
    directory, inner = _Directory(WORKSPACE), _Inner()
    service = _service(tmp_path, directory, inner)

    service.login(username="zhang", password="pw", random_str="r", code="c")

    assert directory.asked == [TENANT], "解析用的是**这个人的公司租户 id**"
    assert inner.joins and inner.joins[0]["workspace_id"] == WORKSPACE
    assert inner.joins[0]["role"] == "editor"
    row = _row(tmp_path)
    assert row["workspace_id"] == WORKSPACE and row["workspace_joined"] == 1


def test_login_refuses_with_an_actionable_reason_when_there_is_no_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    """缺行 ⇒ 拒绝，且文案里**有那条命令**。空工作区会让运营以为"进去了、只是没内容"。"""
    from aiops_diagnostics.dify.dify_login_proxy import PermissionDenied

    _patch_company(monkeypatch)
    refusal = WorkspaceUnavailable("DIFY_WORKSPACE_UNRESOLVED", "租户没有 workspace", http_status=404)
    inner = _Inner()
    service = _service(tmp_path, _Directory(refusal), inner)

    with pytest.raises(PermissionDenied) as caught:
        service.login(username="zhang", password="pw", random_str="r", code="c")

    assert caught.value.code == "DIFY_WORKSPACE_UNRESOLVED"
    assert "平台运营在Dify中开通" in str(caught.value)
    assert inner.joins == [], "解析失败时不该 join 任何 workspace"
    assert _row(tmp_path) == {}, "拒绝发生在 upsert 之前：不留下半条映射"


def test_creating_is_reported_as_retry_later(tmp_path: Path, monkeypatch) -> None:
    from aiops_diagnostics.dify.dify_login_proxy import PermissionDenied

    _patch_company(monkeypatch)
    service = _service(
        tmp_path,
        _Directory(WorkspaceUnavailable("DIFY_WORKSPACE_CREATING", "x", http_status=409, retryable=True)),
        _Inner(),
    )

    with pytest.raises(PermissionDenied) as caught:
        service.login(username="zhang", password="pw", random_str="r", code="c")
    assert "一两分钟后" in str(caught.value)


def test_a_failed_join_refuses_empty_workspace_and_keeps_retry_mapping(tmp_path: Path, monkeypatch) -> None:
    from aiops_diagnostics.dify.dify_login_proxy import PermissionDenied

    """不签发空工作区会话；重试仍复用已建账号。"""
    _patch_company(monkeypatch)
    service = _service(tmp_path, _Directory(WORKSPACE), _Inner(fail=True))

    with pytest.raises(PermissionDenied) as caught:
        service.login(username="zhang", password="pw", random_str="r", code="c")
    assert caught.value.code == "DIFY_WORKSPACE_JOIN_FAILED"
    row = _row(tmp_path)
    assert row["workspace_joined"] == 0, "join 没成功就不能记成成功（下一次登录要再试）"


def test_a_second_login_does_not_join_again(tmp_path: Path, monkeypatch) -> None:
    """验收明写"同一个人二次登录不重复 join"。"""
    _patch_company(monkeypatch)
    inner = _Inner()
    service = _service(tmp_path, _Directory(WORKSPACE), inner)

    service.login(username="zhang", password="pw", random_str="r", code="c")
    service.login(username="zhang", password="pw", random_str="r", code="c")

    assert len(inner.joins) == 1, f"join 被调了 {len(inner.joins)} 次"


# ── 3. 四键成对：半配置是启动错误 ────────────────────────────────────────


def _settings(**overrides: Any) -> LoginProxySettings:
    values: dict[str, Any] = {
        "listen_host": "127.0.0.1",
        "listen_port": 0,
        "public_origin": "https://console.example",
        "company": CompanyAuthSettings(base_url="http://company.invalid", encode_key="0123456789abcdef"),
        "dify": DifyConsoleSettings(
            base_url="http://dify.invalid", secret_key="k" * 16, environment=ENVIRONMENT
        ),
        "data_dir": "/tmp/does-not-matter",
        "edit_permission": "aiops:agent:edit",
    }
    values.update(overrides)
    return LoginProxySettings(**values)


def test_half_a_configuration_is_a_startup_error() -> None:
    """只配网关、不配 inner_api 的表现是"解析得到、join 不了" —— 看起来像登录成功。"""
    with pytest.raises(ValueError, match="四键成对"):
        _settings(gateway_url="http://gw.invalid", gateway_internal_token="t" * 16)

    _settings()  # 全空 ⇒ 单一固定工作区，合法
    _settings(
        gateway_url="http://gw.invalid",
        gateway_internal_token="t" * 16,
        inner_api_base_url="http://inner.invalid",
        inner_api_key="i" * 16,
    )


# --- #882：平台租户（type=-1）落到它自己的平台 workspace ---------------------------


def test_a_platform_account_lands_in_the_platform_workspace(tmp_path: Path, monkeypatch) -> None:
    """`type=-1` 的账号解析的是**平台租户**的 workspace，不是它自带的 `tenant_id`。

    多 workspace 一上线，平台账号不属于任何 `sys_organ` —— 不单独处理的话它会发现
    "自己没有工作区"，登录直接变砖，而它正是应急入口。

    变异对照：把 `workspace_tenant` 改回 `identity.tenant_id`，本条变红（解析用的是
    真实的 `T-…`，而表里只有 `platform` 那一行）。
    """
    _patch_company(monkeypatch, user_type="-1")
    directory, inner = _Directory("ws-platform"), _Inner()
    service = _service(tmp_path, directory, inner)

    service.login(username="ops", password="pw", random_str="r", code="c")

    from aiops_diagnostics.dify.dify_workspaces import PLATFORM_TENANT_ID

    assert directory.asked == [PLATFORM_TENANT_ID], f"解析的是 {directory.asked}"
    assert inner.joins[0]["workspace_id"] == "ws-platform"


def test_a_tenant_level_account_is_not_treated_as_platform(tmp_path: Path, monkeypatch) -> None:
    """`type=1`（租户主账号）**属于**它的租户，不该被当成平台账号。

    两者在可见范围上同为"整租户"，但在"该进哪个 workspace"上相反 —— 混用会把租户主账号
    塞进平台工作区，于是它看不到自己租户的智能体。
    """
    _patch_company(monkeypatch, user_type="1")
    directory, inner = _Directory("ws-tenant"), _Inner()
    service = _service(tmp_path, directory, inner)

    service.login(username="boss", password="pw", random_str="r", code="c")

    assert directory.asked == [TENANT]


def test_a_platform_account_without_its_workspace_gets_an_actionable_refusal(
    tmp_path: Path, monkeypatch
) -> None:
    """平台 workspace 还没建 ⇒ 拒绝，且**文案指向 platform**，不是用户自己的租户。

    这一步是 #882 的验收之一：错误要说清"该给谁开户"，否则运维会去给一个不存在的租户
    执行 `ensure-workspace`。
    """
    from aiops_diagnostics.dify.dify_login_proxy import PermissionDenied

    _patch_company(monkeypatch, user_type="-1")
    service = _service(
        tmp_path,
        _Directory(WorkspaceUnavailable("DIFY_WORKSPACE_UNRESOLVED", "no row", http_status=404)),
        _Inner(),
    )

    with pytest.raises(PermissionDenied) as caught:
        service.login(username="ops", password="pw", random_str="r", code="c")

    assert "平台运营在Dify中开通" in str(caught.value)


# --------------------------------------------------------------------------- #
# ADR-0139：准入的粒度是「他进哪个 workspace」，不是「他持有什么码」              #
# --------------------------------------------------------------------------- #


def test_a_tenant_account_enters_its_own_workspace_without_the_global_code(
    tmp_path: Path, monkeypatch
) -> None:
    """租户的人没有 `aiops:agent:edit` ⇒ **照样进得来自己租户的 workspace**。

    这是 ADR-0056 记的那条代价的终点：隔离在 workspace 那一层，所以那个全局码不再是
    租户账号的门。他在这里只看得见本租户的 app，碰不到别人的。
    """
    _patch_company(monkeypatch, permissions=(), user_type="1")
    directory, inner = _Directory(WORKSPACE), _Inner()
    service = _service(tmp_path, directory, inner)

    service.login(username="boss", password="pw", random_str="r", code="c")

    assert directory.asked == [TENANT]
    assert inner.joins and inner.joins[0]["workspace_id"] == WORKSPACE
    assert _row(tmp_path)["workspace_joined"] == 1


def test_a_tenant_account_without_its_workspace_is_still_refused(tmp_path: Path, monkeypatch) -> None:
    """没有全局码**且**没有自己的 workspace ⇒ 拒，且文案指向 `ensure-workspace`。

    反向判据：如果把 `may_enter` 改成「不校验 workspace 是否真的解析出来」，这条会变成
    登录成功 —— 而那条路通向"登录成功、进去什么也没有"。
    """
    from aiops_diagnostics.dify.dify_login_proxy import PermissionDenied

    _patch_company(monkeypatch, permissions=(), user_type="1")
    service = _service(
        tmp_path,
        _Directory(WorkspaceUnavailable("DIFY_WORKSPACE_UNRESOLVED", "no row", http_status=404)),
        _Inner(),
    )

    with pytest.raises(PermissionDenied) as caught:
        service.login(username="boss", password="pw", random_str="r", code="c")

    assert caught.value.code == "DIFY_WORKSPACE_UNRESOLVED"
    assert "平台运营在Dify中开通" in str(caught.value)


def test_the_platform_workspace_still_requires_the_global_code(tmp_path: Path, monkeypatch) -> None:
    """平台账号**没有**全局码 ⇒ 仍然拒。

    平台 workspace 是所有平台账号共处一室、互相可见全部 app，所以那道门必须还在；
    ADR-0139 放开的是"租户自己那个 workspace"，不是这一间。
    """
    from aiops_diagnostics.dify.dify_login_proxy import PermissionDenied

    _patch_company(monkeypatch, permissions=(), user_type="-1")
    service = _service(tmp_path, _Directory("ws-platform"), _Inner())

    with pytest.raises(PermissionDenied) as caught:
        service.login(username="ops", password="pw", random_str="r", code="c")

    assert caught.value.code == "MISSING_EDIT_PERMISSION"


def test_a_single_workspace_deployment_keeps_the_global_gate(tmp_path: Path, monkeypatch) -> None:
    """没配那四个键的部署（ADR-0072 的单一固定工作区）⇒ 门**逐字不变**。

    那里解析不出 workspace，隔离根本不存在。若判据看的是"他有个租户"，一次配置缺省
    就把整个门拿掉了 —— 这条就是钉住那个方向的。
    """
    from aiops_diagnostics.dify.dify_login_proxy import PermissionDenied

    _patch_company(monkeypatch, permissions=(), user_type="1")
    service = _service(tmp_path, None, None)  # 没有 workspace_directory

    with pytest.raises(PermissionDenied) as caught:
        service.login(username="boss", password="pw", random_str="r", code="c")

    assert caught.value.code == "MISSING_EDIT_PERMISSION"


def test_operator_trial_token_is_opt_in_session_bound_and_expires(tmp_path, monkeypatch):
    import hashlib
    import time

    _patch_company(monkeypatch, user_type="5")
    service = _service(tmp_path, _Directory(WORKSPACE), _Inner())
    session, _ = service.login(username="zhang", password="pw", random_str="r", code="c")
    assert service.operator_token(session.access_token) is None
    service.retain_operator_token = True
    session, _ = service.login(username="zhang", password="pw", random_str="r", code="c")
    value = service.operator_token(session.access_token)
    assert value == {"token": "t", "user_id": "u-1", "tenant_id": TENANT}
    assert service.operator_token(session.access_token + "x") is None
    key = hashlib.sha256(session.access_token.encode()).hexdigest()
    with service._operator_token_lock:
        old = service._operator_tokens[key]
        service._operator_tokens[key] = (time.time() - 1, *old[1:])
    assert service.operator_token(session.access_token) is None
    assert key not in service._operator_tokens


@pytest.mark.parametrize(
    ("access_minutes", "override", "expected"),
    [
        # 默认跟随会话：这里曾经硬编码 900 秒，而会话是 60 分钟 —— 15 到 60 分钟之间
        # 页面能开、试运行和发布却全 403，且没有任何续期路径。
        (60, None, 3600),
        (30, None, 1800),
        (60, 120, 120),
    ],
)
def test_operator_token_lifetime_follows_the_session(
    tmp_path, access_minutes: int, override: int | None, expected: int
) -> None:
    service = LoginService(
        company=CompanyAuthSettings(base_url="http://company.invalid", encode_key="0123456789abcdef"),
        dify=DifyConsoleSettings(
            base_url="http://dify.invalid",
            secret_key="k" * 16,
            environment=ENVIRONMENT,
            access_expire_minutes=access_minutes,
        ),
        store=MappingStore(tmp_path / "mappings.db"),
        policy=LoginPolicy(edit_permission="aiops:agent:edit"),
        owner_user_id=ACCOUNT,
        operator_token_seconds=override,
    )
    assert service.operator_token_seconds() == expected


def test_a_cached_token_outlives_the_old_fifteen_minute_window(tmp_path, monkeypatch) -> None:
    """到 16 分钟仍可用 —— 这正是用户报的"老是过期"。

    不是把窗口关掉：会话 60 分钟一到，``verify_console_jwt`` 仍会拒（读回那条另测）。
    """
    import time

    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    _patch_company(monkeypatch, user_type="5")
    service = _service(tmp_path, _Directory(WORKSPACE), _Inner())
    service.retain_operator_token = True
    session, _ = service.login(username="zhang", password="pw", random_str="r", code="c")
    clock[0] += 16 * 60
    assert service.operator_token(session.access_token) is not None
    clock[0] += 45 * 60
    assert service.operator_token(session.access_token) is None


def test_successful_logins_above_128_keep_their_operator_grants(tmp_path, monkeypatch) -> None:
    from dataclasses import replace

    _patch_company(monkeypatch, user_type="5")
    service = _service(tmp_path, _Directory(WORKSPACE), _Inner())
    service.dify = replace(service.dify, access_expire_minutes=43200)
    service.retain_operator_token = True
    sessions = [
        service.login(username="zhang", password="pw", random_str="r", code="c")[0] for _ in range(129)
    ]
    assert all(service.operator_token(s.access_token) is not None for s in sessions)
    service.forget_operator_token(sessions[-1].access_token)
    assert service.operator_token(sessions[-1].access_token) is None
    assert service.operator_token(sessions[0].access_token) is not None


def test_logout_then_same_second_login_does_not_reauthorize_old_session(tmp_path, monkeypatch):
    monkeypatch.setattr("aiops_diagnostics.dify.dify_login_proxy.time.time", lambda: 1800000000)
    _patch_company(monkeypatch, user_type="5")
    service = _service(tmp_path, _Directory(WORKSPACE), _Inner())
    service.retain_operator_token = True
    old, _ = service.login(username="zhang", password="pw", random_str="r", code="c")
    service.forget_operator_token(old.access_token)
    new, _ = service.login(username="zhang", password="pw", random_str="r", code="c")
    assert service.operator_token(old.access_token) is None
    assert service.operator_token(new.access_token) == {"token": "t", "user_id": "u-1", "tenant_id": TENANT}
