"""防止跨环境错配、未开通租户放行与并发丢失；判据见环境登记验收合同。"""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from aiops_diagnostics.admin_cli import admin_app
from aiops_diagnostics.consumer.knowledge_access import approved_business_endpoint
from aiops_diagnostics.gateway.gateway_api import create_gateway_app
from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway.gateway_store import GatewayStore
from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord
from aiops_diagnostics.onboarding.registry import EnvironmentRegistry, RegistryError, apply, load


def rows(tmp_path):
    return [
        {
            "id": name,
            "enabled": True,
            "gateway_url": f"https://{name}.example.test",
            "cpms_base_url": f"https://{name}.example.test/charging-pile",
            "config_ref": str(tmp_path / f"{name}.env"),
            "tenants": [{"id": "same-tenant", "enabled": True}],
        }
        for name in ["env-a", "env-b"]
    ]


def test_same_tenant_resolves_only_in_selected_environment(tmp_path):
    path = tmp_path / "registry" / "directory.json"
    apply(path, rows(tmp_path), expected_revision=0, actor="operator")
    for name in ["env-a", "env-b"]:
        settings = SimpleNamespace(environment_registry_file=str(path), environment=name)
        assert (
            approved_business_endpoint(settings, "same-tenant")
            == f"https://{name}.example.test/charging-pile"
        )
    with pytest.raises(RegistryError):
        EnvironmentRegistry(path, "env-c")
    with pytest.raises(RegistryError):
        EnvironmentRegistry(path, "env-a").require("unregistered")


def test_concurrent_updates_have_one_winner_and_atomic_audit(tmp_path):
    path = tmp_path / "registry" / "directory.json"
    apply(path, rows(tmp_path), expected_revision=0, actor="operator")

    def publish(actor):
        try:
            return apply(path, rows(tmp_path), expected_revision=1, actor=actor)
        except RegistryError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(publish, ["first", "second"]))
    assert sorted(map(str, result)) == ["2", "ENVIRONMENT_REGISTRY_REVISION_CONFLICT"]
    body = load(path)
    assert [a["revision"] for a in body["audit"]] == [1, 2]
    assert path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("kind", ["duplicate", "http", "missing-key", "duplicate-tenant", "bool-string"])
def test_invalid_update_preserves_existing_registry(tmp_path, kind):
    path = tmp_path / "registry" / "directory.json"
    original = rows(tmp_path)
    apply(path, original, expected_revision=0, actor="operator")
    bad = copy.deepcopy(original)
    if kind == "duplicate":
        bad[1]["id"] = bad[0]["id"]
    elif kind == "http":
        bad[0]["cpms_base_url"] = "http://remote.example.test"
    elif kind == "missing-key":
        del bad[0]["config_ref"]
    elif kind == "duplicate-tenant":
        bad[0]["tenants"] *= 2
    else:
        bad[0]["enabled"] = "false"
    with pytest.raises(ValueError):
        apply(path, bad, expected_revision=1, actor="operator")
    assert load(path)["environments"] == original
    assert load(path)["revision"] == 1


class Resolver:
    def resolve(self, token, **kwargs):
        subject = SubjectRecord(b_user_id="same-user", c_user_id="customer", tenant_id="same-tenant")
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id="same-tenant",
            data_scope=DataScope(type="self"),
            roles=frozenset(),
            permissions=frozenset({"aiops:orders:read"}),
        )


class Orders:
    def __init__(self):
        self.calls = 0

    def can_access(self, context, order_no):
        self.calls += 1
        return True


@pytest.mark.parametrize("disabled", ["tenant", "environment", "unreadable"])
def test_live_http_admission_rechecks_without_restart(tmp_path, disabled):
    path = tmp_path / "registry" / "directory.json"
    data = rows(tmp_path)
    config = tmp_path / "env-a.env"
    config.write_text("# isolated\n")
    config.chmod(0o600)
    apply(path, data, expected_revision=0, actor="operator")
    settings = GatewayServerSettings(
        data_home=tmp_path / "state",
        database_file=tmp_path / "state/gateway.db",
        server_config_file=config,
        environment="env-a",
        environment_registry_file=str(path),
    )
    orders = Orders()
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=SimpleNamespace(shutdown=lambda: None),
        caller_resolver=Resolver(),
        order_authorizer=orders,
    )
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer valid-company-session"}
        assert client.get("/v1/orders/order/access", headers=headers).status_code == 200
        assert orders.calls == 1
        if disabled == "unreadable":
            path.unlink()
        else:
            target = data[0]["tenants"][0] if disabled == "tenant" else data[0]
            target["enabled"] = False
            apply(path, data, expected_revision=1, actor="operator")
        response = client.get("/v1/orders/order/access", headers=headers)
        assert response.status_code == (503 if disabled == "unreadable" else 403)
        assert orders.calls == 1


def test_admin_cli_summary_never_prints_connections(tmp_path):
    path = tmp_path / "registry" / "directory.json"
    incoming = tmp_path / "input.json"
    incoming.write_text(json.dumps(rows(tmp_path)))
    incoming.chmod(0o600)
    runner = CliRunner()
    result = runner.invoke(
        admin_app, ["environment-registry", "apply", "--registry", str(path), "--file", str(incoming)]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"revision": 1, "environments": 2, "tenants": 2}
    result = runner.invoke(
        admin_app, ["environment-registry", "apply", "--registry", str(path), "--file", str(incoming)]
    )
    assert result.exit_code == 1
    assert "REVISION_CONFLICT" in result.output
    assert "example.test" not in result.output


def test_configuration_change_requires_rebind_not_silent_retarget(tmp_path):
    path = tmp_path / "registry" / "directory.json"
    data = rows(tmp_path)
    apply(path, data, expected_revision=0, actor="operator")
    registry = EnvironmentRegistry(path, "env-a")
    data[0]["cpms_base_url"] = "https://replacement.example.test"
    apply(path, data, expected_revision=1, actor="operator")
    with pytest.raises(RegistryError, match="CONFIGURATION_CHANGED"):
        registry.require("same-tenant")


def test_login_refuses_before_workspace_or_account_side_effects(tmp_path, monkeypatch):
    from dify_login_proxy import service as login
    from dify_login_proxy.company import CompanyIdentity
    from dify_login_proxy.core import PermissionDenied

    path = tmp_path / "registry" / "directory.json"
    data = rows(tmp_path)
    data[0]["tenants"][0]["enabled"] = False
    apply(path, data, expected_revision=0, actor="operator")
    service = login.LoginService(
        company=None,
        dify=None,
        store=None,
        policy=None,
        owner_user_id="owner",
        environment_registry=EnvironmentRegistry(path, "env-a"),
    )
    monkeypatch.setattr(
        login,
        "company_login",
        lambda *a, **kw: CompanyIdentity(
            user_id="user", username="user", tenant_id="same-tenant", token="token"
        ),
    )
    monkeypatch.setattr(
        login, "read_permissions", lambda *a: pytest.fail("disabled tenant reached permission/workspace flow")
    )
    with pytest.raises(PermissionDenied, match="未开通"):
        service.login(username="user", password="unused", random_str="captcha", code="unused")


@pytest.mark.parametrize("revision", [True, 1.0, "1", -1])
def test_invalid_revision_cannot_corrupt_registry(tmp_path, revision):
    path = tmp_path / "registry" / "directory.json"
    apply(path, rows(tmp_path), expected_revision=0, actor="operator")
    original = path.read_bytes()
    with pytest.raises(RegistryError):
        apply(path, rows(tmp_path), expected_revision=revision, actor="operator")
    assert path.read_bytes() == original


@pytest.mark.parametrize("unsafe", ["symlink", "writable"])
def test_registry_parent_is_private(tmp_path, unsafe):
    path = tmp_path / "registry" / "directory.json"
    apply(path, rows(tmp_path), expected_revision=0, actor="operator")
    if unsafe == "symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(path.parent, target_is_directory=True)
        path = alias / path.name
    else:
        path.parent.chmod(0o777)
    with pytest.raises(RegistryError):
        load(path)


def test_settings_bound_endpoint_refuses_live_retarget(tmp_path):
    path = tmp_path / "registry" / "directory.json"
    data = rows(tmp_path)
    apply(path, data, expected_revision=0, actor="operator")
    settings = SimpleNamespace(environment_registry_file=str(path), environment="env-a")
    assert approved_business_endpoint(settings, "same-tenant") == data[0]["cpms_base_url"]
    data[0]["cpms_base_url"] = "https://replacement.example.test"
    apply(path, data, expected_revision=1, actor="operator")
    with pytest.raises(RegistryError, match="CONFIGURATION_CHANGED"):
        approved_business_endpoint(settings, "same-tenant")


def test_tool_refusal_is_http_403_not_unhandled_500(tmp_path):
    from aiops_diagnostics.consumer.consumer_tool_grants import ConsumerToolRefused

    config = tmp_path / "test.env"
    config.write_text("# isolated")
    config.chmod(0o600)
    settings = GatewayServerSettings(
        data_home=tmp_path, database_file=tmp_path / "gateway.db", server_config_file=config
    )
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=SimpleNamespace(shutdown=lambda: None),
        caller_resolver=Resolver(),
    )

    @app.get("/test-tool-refusal")
    def refused():
        raise ConsumerToolRefused("TENANT_ENDPOINT_UNRESOLVED", "private detail")

    with TestClient(app) as client:
        response = client.get("/test-tool-refusal")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "TENANT_ENDPOINT_UNRESOLVED"
        assert "private detail" not in response.text
        assert response.headers["Cache-Control"] == "no-store"


# --- ADR-0320：登记表部署按申报入口分派本环境凭据链 ---------------------------
#
# 安全反例沿用环境目录 QA 的 D19/D43 形状：跨环境令牌交换拒绝、停用拒绝且无默认
# 回落、同号主体只在其登记环境生效。替身纪律与文件内既有用例一致——真实 HTTP
# （TestClient + create_gateway_app 完整装配）、真实 ``_caller_resolver`` 配置链、
# UPMS 形状传输替身与 Redis 存储替身只替换两处 I/O。

SERVICE_TOKEN = "svc"
SESSION_TOKEN = "session-123456789012345"
OPERATOR_TOKEN = "upms-token-ops-1"


class _FakeUpmsResponse:
    def __init__(self, payload, *, status=200):
        self._body = json.dumps(payload).encode()
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class _FakeUpmsTransport:
    """UPMS 合同形状替身：只认 /user/info 与 /user/ds，记录每次请求与凭据。"""

    def __init__(self, *, user_info, data_scope):
        self.user_info = user_info
        self.data_scope = data_scope
        self.requests = []

    def __call__(self, request, timeout=None):
        split = urlsplit(request.full_url)
        self.requests.append((split.path, dict(request.header_items())))
        if split.path == "/user/info":
            return _FakeUpmsResponse({"code": 0, "msg": "ok", "data": self.user_info})
        if split.path == "/user/ds":
            return _FakeUpmsResponse({"code": 0, "msg": "ok", "data": self.data_scope})
        return _FakeUpmsResponse({"code": 404, "msg": "not found", "data": None})


def _operator_info(tenant_id="same-tenant"):
    return {
        "sysUser": {"id": "B-OPS-1", "userId": "C-OPS-1", "username": "ops.one", "tenantId": tenant_id},
        "roles": [{"roleCode": "ROLE_OPS"}],
        "permissions": ["order:diag:view"],
    }


class _RedisStore:
    """会话存储替身：会话键返回 JSON 载荷，记录读过的键——UPMS 拒绝后落回
    会话链的形状会以「本不该出现的 get」直接暴露。"""

    def __init__(self, payload=None):
        self.payload = payload
        self.gets = []

    def get(self, key):
        self.gets.append(key)
        return self.payload


def _session_payload(tenant_id="same-tenant", user_id="C-1"):
    return json.dumps({"userId": user_id, "tenantId": tenant_id}).encode()


def _entry_app(tmp_path, monkeypatch, *, transport, redis_store, tenants=None, environment="env-a"):
    """真实装配：登记表 + 会话链 + UPMS 三项都满足时 ``_caller_resolver`` 选分派器。"""
    registry = tmp_path / "registry" / "directory.json"
    data = rows(tmp_path)
    if tenants is not None:
        data[0]["tenants"] = tenants
    revision = load(registry)["revision"] if registry.exists() else 0
    apply(registry, data, expected_revision=revision, actor="operator")
    config = tmp_path / "env-a.env"
    config.write_text(
        "AIOPS_REDIS_HOST=127.0.0.1\n"
        "AIOPS_REDIS_PASSWORD=stub-password\n"
        "AIOPS_UPMS_BASE_URL=https://upms.example.test\n"
    )
    config.chmod(0o600)
    settings = GatewayServerSettings(
        data_home=tmp_path / "state",
        database_file=tmp_path / "state/gateway.db",
        server_config_file=config,
        environment=environment,
        environment_registry_file=str(registry),
        third_session_service_token=SERVICE_TOKEN,
    )
    monkeypatch.setattr("aiops_diagnostics.common.bounded_http.urllib.request.urlopen", transport)
    monkeypatch.setattr("aiops_diagnostics.identity.third_session_auth.redis.Redis", lambda **_: redis_store)
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=SimpleNamespace(shutdown=lambda: None),
        order_authorizer=Orders(),
    )
    return app


def test_registered_operator_entry_resolves_via_upms(tmp_path, monkeypatch):
    transport = _FakeUpmsTransport(user_info=_operator_info(), data_scope={"type": 4})
    redis_store = _RedisStore(_session_payload())
    app = _entry_app(tmp_path, monkeypatch, transport=transport, redis_store=redis_store)
    with TestClient(app) as client:
        response = client.get(
            "/v1/orders/order/access",
            headers={"Authorization": f"Bearer {OPERATOR_TOKEN}", "X-Business-Entry": "operator"},
        )
    assert response.status_code == 200
    # 个人 Bearer 原样透传给 UPMS 验证；会话存储一次都没被读——没混链。
    assert [path for path, _ in transport.requests] == ["/user/info", "/user/ds"]
    for _path, headers in transport.requests:
        assert headers["Authorization"] == f"Bearer {OPERATOR_TOKEN}"
    assert redis_store.gets == []


def test_registered_consumer_entry_stays_on_session_chain(tmp_path, monkeypatch):
    transport = _FakeUpmsTransport(user_info=_operator_info(), data_scope={"type": 4})
    redis_store = _RedisStore(_session_payload())
    app = _entry_app(tmp_path, monkeypatch, transport=transport, redis_store=redis_store)
    with TestClient(app) as client:
        for headers in (
            # 显式 consumer 与**缺头**（旧客户端）都走会话链——41 契约逐字保留。
            {"X-Business-Entry": "consumer"},
            {},
        ):
            response = client.get(
                "/v1/orders/order/access",
                headers={
                    "Authorization": f"Bearer {SERVICE_TOKEN}",
                    "X-Third-Session": SESSION_TOKEN,
                    **headers,
                },
            )
            assert response.status_code == 200, response.text
    assert redis_store.gets == ["app:3rd_session:" + SESSION_TOKEN] * 2
    # 会话链从不打 UPMS——两条链互不借用对方的验证。
    assert transport.requests == []


def test_registered_operator_disabled_or_foreign_tenant_forbidden(tmp_path, monkeypatch):
    # 停用租户拒绝（D43 准入关闭即拒）；登记在他环境的同名/异名租户同样拒绝——
    # 跨环境令牌交换没有默认回落（D19）。
    for tenants, tenant_id in (
        ([{"id": "same-tenant", "enabled": False}], "same-tenant"),
        ([{"id": "same-tenant", "enabled": True}], "other-tenant"),
    ):
        transport = _FakeUpmsTransport(user_info=_operator_info(tenant_id), data_scope={"type": 4})
        app = _entry_app(
            tmp_path,
            monkeypatch,
            transport=transport,
            redis_store=_RedisStore(_session_payload()),
            tenants=tenants,
        )
        with TestClient(app) as client:
            response = client.get(
                "/v1/orders/order/access",
                headers={
                    "Authorization": f"Bearer {OPERATOR_TOKEN}",
                    "X-Business-Entry": "operator",
                },
            )
        assert response.status_code == 403, (tenants, tenant_id, response.text)


def test_upms_rejection_never_falls_back_to_session(tmp_path, monkeypatch):
    # UPMS 判拒（凭据无效）→ 401；会话存储零读取——拒绝不换链重试。
    def rejected(request, timeout=None):
        return _FakeUpmsResponse({"code": 403, "msg": "denied", "data": None})

    redis_store = _RedisStore(_session_payload())
    app = _entry_app(tmp_path, monkeypatch, transport=rejected, redis_store=redis_store)
    with TestClient(app) as client:
        response = client.get(
            "/v1/orders/order/access",
            headers={"Authorization": f"Bearer {OPERATOR_TOKEN}", "X-Business-Entry": "operator"},
        )
    assert response.status_code == 401
    assert redis_store.gets == []


def test_upms_unavailable_keeps_chain_classification(tmp_path, monkeypatch):
    import urllib.error

    # UPMS 不可达按本链既有合同判 invalid（401，UpmsCallerResolver 的既有映射）；
    # 会话存储仍零读取。分类属于链本身，分派不抹平也不放大。
    def unavailable(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    redis_store = _RedisStore(_session_payload())
    app = _entry_app(tmp_path, monkeypatch, transport=unavailable, redis_store=redis_store)
    with TestClient(app) as client:
        response = client.get(
            "/v1/orders/order/access",
            headers={"Authorization": f"Bearer {OPERATOR_TOKEN}", "X-Business-Entry": "operator"},
        )
    assert response.status_code == 401
    assert redis_store.gets == []

    # 对照：会话链自己的不可用仍是 unavailable（503）——两条链各自保持真实分类。
    class DownRedis:
        def get(self, key):
            import redis

            raise redis.RedisError("down")

    app = _entry_app(
        tmp_path,
        monkeypatch,
        transport=_FakeUpmsTransport(user_info=_operator_info(), data_scope={"type": 4}),
        redis_store=DownRedis(),
    )
    with TestClient(app) as client:
        response = client.get(
            "/v1/orders/order/access",
            headers={
                "Authorization": f"Bearer {SERVICE_TOKEN}",
                "X-Third-Session": SESSION_TOKEN,
            },
        )
    assert response.status_code == 503


def test_entry_dispatch_selects_only_with_registry_and_upms(tmp_path):
    """装配形状：分派只在「登记表 + 会话链 + 本环境 UPMS」三者齐备时出现。"""
    from aiops_diagnostics.gateway.gateway_api import _caller_resolver
    from aiops_diagnostics.identity.caller_auth import EntryCallerResolver
    from aiops_diagnostics.identity.third_session_auth import RedisThirdSessionResolver

    def settings(**overrides):
        config = tmp_path / "env-a.env"
        config.write_text(
            "AIOPS_REDIS_PASSWORD=stub-password\nAIOPS_UPMS_BASE_URL=https://upms.example.test\n"
        )
        config.chmod(0o600)
        base = dict(
            data_home=tmp_path,
            database_file=tmp_path / "gateway.db",
            server_config_file=config,
            third_session_service_token=SERVICE_TOKEN,
        )
        return GatewayServerSettings(**{**base, **overrides})

    resolver = _caller_resolver(settings(environment_registry_file="registry.json"))
    assert isinstance(resolver, EntryCallerResolver)
    # 无登记表：返回值与分派引入之前逐字一致——旧 41 合同原样保留。
    assert isinstance(_caller_resolver(settings()), RedisThirdSessionResolver)


def test_upms_operator_scope_uses_ds_and_shop_mapping(tmp_path, monkeypatch):
    """/user/ds 的站点与店铺断言原样进入查询范围（type5 级账号无特例）。

    不只判类型：数据范围须经 ``resolve_query_scope`` 展开——``siteIds`` 原样
    保留，``shopIds`` 经站点归属映射换算成站点集合。"""
    from aiops_diagnostics.identity.query_scope import resolve_query_scope, static_site_mapper

    transport = _FakeUpmsTransport(
        user_info=_operator_info(),
        data_scope={"type": 1, "organIds": ["ORG-1"], "shopIds": ["SHOP-9"], "siteIds": ["SITE-DS-1"]},
    )
    redis_store = _RedisStore()
    app = _entry_app(tmp_path, monkeypatch, transport=transport, redis_store=redis_store)

    context = app.state.gateway.caller_resolver.resolve(
        OPERATOR_TOKEN,
        required_scope="aiops:orders:read",
        platform_entry="operator",
    )
    query_scope = resolve_query_scope(
        context, mapper=static_site_mapper(sites_by_shop={"SHOP-9": ("SITE-SHOP-1",)})
    )
    # /user/ds 断言的两个来源都不能丢：siteIds 原样 + shopIds 经归属映射换算。
    assert query_scope.site_ids == ("SITE-DS-1", "SITE-SHOP-1")
    assert query_scope.user_id is None and query_scope.tenant_id == "same-tenant"


def test_require_enabled_and_is_manager_enforce_scope_schema_and_drift(tmp_path):
    """ADR-0332：平台准入只看本环境启用，目录管理员仅认 schema2 本环境显式主体且漂移必拒。"""
    path = tmp_path / "registry" / "directory.json"
    v1_rows = rows(tmp_path)
    apply(path, v1_rows, expected_revision=0, actor="operator")
    reg_v1 = EnvironmentRegistry(path, "env-a")
    assert reg_v1.require_enabled()["id"] == "env-a"
    assert reg_v1.enabled_tenant_ids() == frozenset({"same-tenant"})
    assert reg_v1.is_manager("-1", "admin") is False

    managed_path = tmp_path / "registry" / "managed.json"
    managed_rows = [
        {**r, "name": r["id"], "company_gateway_url": "https://company.example.test"}
        for r in copy.deepcopy(v1_rows)
    ]
    for r in managed_rows:
        for t in r["tenants"]:
            t["name"] = t["id"]
    managed_rows[0]["enabled"] = False
    managers = [
        {"environment": "env-a", "tenant_id": "-1", "user_id": "admin-a", "environments": ["*"]},
        {"environment": "env-b", "tenant_id": "-1", "user_id": "admin-b", "environments": ["*"]},
    ]
    apply(managed_path, managed_rows, expected_revision=0, actor="operator", managers=managers)
    reg = EnvironmentRegistry(managed_path, "env-a")
    with pytest.raises(RegistryError, match="TENANT_ENVIRONMENT_NOT_ENABLED"):
        reg.require_enabled()
    assert reg.is_manager("-1", "admin-a") is True
    # 他环境的目录管理员不得跨环境生效。
    assert reg.is_manager("-1", "admin-b") is False
    assert reg.is_manager("", "admin-a") is False
    assert reg.is_manager("-1", "") is False

    # 撤回目录管理员即刻生效；任一连接配置（含 company_gateway_url）漂移时即使仍在 managers 也必须失败关闭。
    apply(managed_path, managed_rows, expected_revision=1, actor="operator", managers=[managers[1]])
    assert reg.is_manager("-1", "admin-a") is False
    drifted_company = copy.deepcopy(managed_rows)
    drifted_company[0]["company_gateway_url"] = "https://changed-company.example.test"
    apply(managed_path, drifted_company, expected_revision=2, actor="operator", managers=managers)
    with pytest.raises(RegistryError, match="CONFIGURATION_CHANGED"):
        reg.is_manager("-1", "admin-a")
    managed_rows[0]["cpms_base_url"] = "https://changed.example.test/cpms"
    apply(managed_path, managed_rows, expected_revision=3, actor="operator", managers=managers)
    with pytest.raises(RegistryError, match="CONFIGURATION_CHANGED"):
        reg.is_manager("-1", "admin-a")
