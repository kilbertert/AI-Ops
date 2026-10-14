"""Route-level tests for the operator self-service publish route (#577).

The route lets an operator freeze what Dify currently holds **without an
engineer on the box**. What is pinned here is therefore not "it publishes" —
that path is already covered end-to-end in ``tests/test_cli.py`` — but the
three properties that make it safe to expose at all:

* the credential is the **separate** publish key, and the user chain is not
  consulted (it is claimable on this deployment);
* the request body can name **one already-bound app**, and nothing else — the
  tenant, agent, type, contract and knowledge binding all come from the
  registry, so the blast radius is the declared binding set;
* an unbound ``app_id`` is a uniform 404, and an unconfigured route is not
  enabled at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore

PUBLISH_KEY = "dify-publish-key-0123456789"
KNOWLEDGE_KEY = "dify-knowledge-key-0123456789"
CONSOLE_KEY = "dify-console-key-0123456789"
APP_ID = "a975c8e5-ad9e-425c-84c9-77581a0a2bed"


class _Runtime:
    def __init__(self, db: Path | None = None) -> None:
        from aiops_diagnostics.dify_app_registry import DifyAppRegistry

        if db is None:
            self.dify_app_registry = DifyAppRegistry.__new__(DifyAppRegistry)
        else:
            # 真注册表（本文件新加的两条用例要它按 (租户,入口) 查）
            self.dify_app_registry = DifyAppRegistry(db)
        self._bindings: tuple[Any, ...] = ()

    def shutdown(self) -> None:  # pragma: no cover - not exercised
        pass


class _Directory:
    def roles_for_c_user(self, c_user_id: str, tenant_id: str):
        return (PlatformRoleRecord("B-1", "C-1", "T-1", "admin"),)

    def roles_for_b_user(self, b_user_id: str, tenant_id: str):
        return ()


def _client(tmp_path: Path, runtime: _Runtime, **overrides: Any) -> TestClient:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
        dify_console_base_url=overrides.pop("dify_console_base_url", "http://dify.invalid:10008"),
        dify_console_api_key=overrides.pop("dify_console_api_key", CONSOLE_KEY),
        **overrides,
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=runtime,  # type: ignore[arg-type]
        caller_resolver=None,  # type: ignore[arg-type]
        order_authorizer=None,  # type: ignore[arg-type]
        platform_resolver=PlatformIdentityResolver(_Directory()),
        faq_catalog=FAQCatalog.bundled(),
    )
    return TestClient(app)


def _binding(app_id: str = APP_ID, tenant: str = "T-1", entry: str = "consumer", name: str = "小趋-客服"):
    from aiops_diagnostics.dify_app_registry import DifyAppBinding

    return DifyAppBinding(tenant_id=tenant, business_entry=entry, app_id=app_id, agent_name=name)


def _patch_registry(monkeypatch, runtime: _Runtime, bindings: list[Any]) -> None:
    monkeypatch.setattr(runtime.dify_app_registry, "all", lambda: tuple(bindings))


def test_the_publish_key_is_required_and_the_user_chain_is_not_consulted(tmp_path, monkeypatch) -> None:
    """无凭据 401；**错**凭据 403；**用户令牌**不被认作授权。

    第三条是本路由存在的理由：本部署 company-parity 不验签，用户 JWT 是**可自报**的
    （41 实测：编造密钥签的令牌打诊断路由得到 404 而不是 401）。所以它不能当授权。
    """
    runtime = _Runtime()
    monkeypatch.setattr(runtime.dify_app_registry, "all", lambda: (_binding(),))
    client = _client(tmp_path, runtime, dify_publish_api_key=PUBLISH_KEY)

    assert client.post("/v1/dify/publish", json={"app_id": APP_ID}).status_code == 401
    assert (
        client.post(
            "/v1/dify/publish", json={"app_id": APP_ID}, headers={"Authorization": "Bearer wrong-key"}
        ).status_code
        == 403
    )
    # 一把**别的** Dify 凭据（知识检索那把）也不行 —— 三把钥匙互不通用。
    assert (
        client.post(
            "/v1/dify/publish", json={"app_id": APP_ID}, headers={"Authorization": f"Bearer {KNOWLEDGE_KEY}"}
        ).status_code
        == 403
    )


def test_an_unconfigured_publish_route_is_not_enabled_at_all(tmp_path, monkeypatch) -> None:
    """没配发布密钥 ⇒ 404（不是 401「需要凭据」）。与检索路由同一条机制。"""
    runtime = _Runtime()
    monkeypatch.setattr(runtime.dify_app_registry, "all", lambda: (_binding(),))
    client = _client(tmp_path, runtime)  # 未给 dify_publish_api_key
    resp = client.post(
        "/v1/dify/publish", json={"app_id": APP_ID}, headers={"Authorization": f"Bearer {PUBLISH_KEY}"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DIFY_PUBLISH_UNAVAILABLE"


def test_an_unbound_app_id_is_a_uniform_404(tmp_path, monkeypatch) -> None:
    """请求里点名一个**没绑定**的 app ⇒ 404，且不透露哪些 app 存在。"""
    runtime = _Runtime()
    monkeypatch.setattr(runtime.dify_app_registry, "all", lambda: (_binding(),))
    client = _client(tmp_path, runtime, dify_publish_api_key=PUBLISH_KEY)
    resp = client.post(
        "/v1/dify/publish",
        json={"app_id": "00000000-0000-0000-0000-000000000000"},
        headers={"Authorization": f"Bearer {PUBLISH_KEY}"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DIFY_APP_NOT_BOUND"


def test_the_request_body_cannot_carry_anything_but_an_app_id(tmp_path, monkeypatch) -> None:
    """**这是爆炸半径的全部**：多一个字段就是 400。

    规格写成 ``extra="forbid"`` 不是洁癖 —— 一个能自报 ``tenant_id`` 或 ``prompt`` 的
    请求体会把「重新发布一个已声明绑定」变成「以任意租户发布任意正文」。
    """
    runtime = _Runtime()
    monkeypatch.setattr(runtime.dify_app_registry, "all", lambda: (_binding(),))
    client = _client(tmp_path, runtime, dify_publish_api_key=PUBLISH_KEY)
    for extra in ({"tenant_id": "T-2"}, {"prompt": "被注入的正文"}, {"agent_name": "别的"}):
        resp = client.post(
            "/v1/dify/publish",
            json={"app_id": APP_ID, **extra},
            headers={"Authorization": f"Bearer {PUBLISH_KEY}"},
        )
        assert resp.status_code == 422, f"请求体接受了 {extra}"


def test_a_bound_app_publishes_through_the_shared_sync_path(tmp_path, monkeypatch) -> None:
    """绑定得住 ⇒ 走**与 CLI 同一个** ``sync_binding_from_dify``，不是第二份实现。

    这里只桩住那一个函数并断言它收到的**绑定与来源**（端点必须用注册表里的 app_id 与
    服务端的控制台凭据），发布本身由 `tests/test_cli.py` 的端到端覆盖。
    """
    runtime = _Runtime()
    binding = _binding()
    monkeypatch.setattr(runtime.dify_app_registry, "all", lambda: (binding,))
    seen: dict[str, Any] = {}

    def _fake_sync(manager, context, got_binding, source, **kwargs):
        from aiops_diagnostics.dify_dsl_pull import SyncOutcome

        seen["binding"] = got_binding
        seen["source"] = source
        seen["context_tenant"] = context.effective_tenant_id
        return SyncOutcome("T-1", "consumer", APP_ID, "小趋-客服", "synced", 4)

    monkeypatch.setattr("aiops_diagnostics.dify_dsl_pull.sync_binding_from_dify", _fake_sync)
    client = _client(tmp_path, runtime, dify_publish_api_key=PUBLISH_KEY)
    resp = client.post(
        "/v1/dify/publish", json={"app_id": APP_ID}, headers={"Authorization": f"Bearer {PUBLISH_KEY}"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "app_id": APP_ID,
        "tenant_id": "T-1",
        "business_entry": "consumer",
        "agent_name": "小趋-客服",
        "action": "synced",
        "version_no": 4,
        "note": "",
    }
    # 用的是**注册表里**的绑定与**服务端**的控制台凭据，不是请求里的任何东西。
    assert seen["binding"].app_id == APP_ID
    assert seen["source"].app_id == APP_ID
    assert seen["source"].api_key == CONSOLE_KEY
    assert seen["context_tenant"] == "T-1"


def test_enabling_publish_without_a_console_credential_is_a_startup_error(tmp_path) -> None:
    """「配了发布密钥、却没配控制台凭据」是**启动错误**，不是运行时 502。

    这是 #586「半配置是启动错误」的同一条：门开着、每次调用都 502 的部署看起来装好了，
    实际整体不可用。所以这条判据钉在 ``validate()`` 上 —— 路由里那个 502 分支因此是
    给「运行期配置被撤掉」留的诚实答案，不是正常状态（构造出这个 app 就会先失败）。
    """
    import pytest

    from aiops_diagnostics.gateway_config import GatewayServerSettings

    (tmp_path / "production.env").write_text("# test\n", encoding="utf-8")
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
        dify_publish_api_key=PUBLISH_KEY,
        dify_console_base_url="http://dify.invalid:10008",
        dify_console_api_key="",
    )
    with pytest.raises(ValueError, match="DIFY_CONSOLE_API_KEY"):
        settings.validate()


def test_the_publish_key_must_differ_from_the_other_dify_keys(tmp_path) -> None:
    """三把钥匙同值 ⇒ 启动错误。同值等于把两件事绑成一个权限。"""
    import pytest

    from aiops_diagnostics.gateway_config import GatewayServerSettings

    (tmp_path / "production.env").write_text("# test\n", encoding="utf-8")
    base: dict[str, object] = dict(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
        dify_publish_api_key="same-key-value",
        dify_console_base_url="http://dify.invalid:10008",
        dify_console_api_key=CONSOLE_KEY,
    )
    with pytest.raises(ValueError, match="must differ"):
        GatewayServerSettings(
            **base,  # type: ignore[arg-type]
            dify_knowledge_api_key="same-key-value",
            dify_knowledge_bindings="kb:T:KB",
        ).validate()
    with pytest.raises(ValueError, match="must differ"):
        GatewayServerSettings(
            **base,
            dify_debug_api_key="same-key-value",
            dify_debug_tenant="T",  # type: ignore[arg-type]
        ).validate()


# ── 运营自助发布（operator 会话身份那条门）──────────────────────────────────


class _OperatorDecisions:
    """按 ``X-Business-Entry`` 给出平台决策 —— 内容不影响本路由，入口才影响。"""

    def __init__(self) -> None:
        self.seen: list[Any] = []

    def resolve(self, caller, *, platform_entry=None):
        self.seen.append(platform_entry)
        from aiops_diagnostics.faq import PlatformDecision

        return PlatformDecision(
            platform=str(platform_entry or "consumer"), available_platforms=(), b_subject_ids=(), reason="ok"
        )


class _OperatorCaller:
    """一个会话解析器：交出运营租户的身份。入口由测试通过 header 声明。"""

    def resolve(self, token, *, required_scope, third_session=None, platform_entry=None, source_key=None):
        from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

        subject = SubjectRecord(b_user_id="c:T-1", c_user_id="T-1", tenant_id="T-1")
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id="T-1",
            data_scope=DataScope(type="self"),
            roles=frozenset(),
            permissions=frozenset({required_scope}),
        )


def _operator_client(tmp_path: Path, runtime: _Runtime, **overrides: Any) -> TestClient:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
        dify_console_base_url="http://dify.invalid:10008",
        dify_console_api_key=CONSOLE_KEY,
        **overrides,
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=runtime,  # type: ignore[arg-type]
        caller_resolver=_OperatorCaller(),  # type: ignore[arg-type]
        order_authorizer=None,  # type: ignore[arg-type]
        platform_resolver=PlatformIdentityResolver(_Directory()),
        faq_catalog=FAQCatalog.bundled(),
    )
    return TestClient(app)


def _bind_operator(runtime: _Runtime, tmp_path: Path) -> None:
    """**真的**往注册表写一行 (T-1, operator) → APP_ID，并让 `_registered_agent` 认。

    用真注册表而不是打桩 `lookup`：本路由的绑定枚举防线正是打在 `lookup` 上的，
    把 lookup 换成桩就等于把要测的东西换掉了。
    """
    runtime.dify_app_registry.put(_binding(entry="operator", name="ops-methodology-700"))
    runtime._registered_agent = lambda t, e: (("ops-methodology-700" if e == "operator" else None), True)


def _run_sync_ok(monkeypatch) -> None:
    """把真正的 sync 换成一个可观察的桩（本文件测的是**门**，不是发布本身）。"""
    from aiops_diagnostics.dify_dsl_pull import SyncOutcome

    calls: list[Any] = []

    def fake_sync(manager, context, binding, source, **kwargs):
        calls.append((binding.app_id, kwargs))
        return SyncOutcome(
            tenant_id=binding.tenant_id,
            business_entry=binding.business_entry,
            app_id=binding.app_id,
            agent_name=binding.agent_name,
            action="unchanged",
            version_no=1,
            note="",
        )

    monkeypatch.setattr("aiops_diagnostics.dify_dsl_pull.sync_binding_from_dify", fake_sync)
    return calls


def test_the_operator_route_needs_no_shared_key(tmp_path, monkeypatch) -> None:
    """**没有**发布密钥也能调 —— 这正是"把触发权交给运营"的意思。"""
    runtime = _Runtime(tmp_path / "gateway.db")
    _bind_operator(runtime, tmp_path)
    calls = _run_sync_ok(monkeypatch)
    client = _operator_client(tmp_path, runtime)  # 注意：没有 dify_publish_api_key
    r = client.post(
        "/v1/operator/publish",
        json={"app_id": APP_ID},
        headers={"Authorization": "Bearer session-token", "X-Business-Entry": "operator"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "unchanged"
    assert calls and calls[0][0] == APP_ID


def test_the_consumer_entry_cannot_publish(tmp_path, monkeypatch) -> None:
    """客户端入口**不能**触发发布 —— 这条门的第一道。"""
    runtime = _Runtime(tmp_path / "gateway.db")
    _bind_operator(runtime, tmp_path)
    calls = _run_sync_ok(monkeypatch)
    client = _operator_client(tmp_path, runtime)
    r = client.post(
        "/v1/operator/publish",
        json={"app_id": APP_ID},
        headers={"Authorization": "Bearer session-token", "X-Business-Entry": "consumer"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "OPERATOR_ENTRY_REQUIRED"
    assert calls == [], "被拒就不该走到发布实现"


def test_a_tenant_without_a_bound_operator_agent_is_a_404(tmp_path, monkeypatch) -> None:
    runtime = _Runtime(tmp_path / "gateway.db")
    runtime._registered_agent = lambda t, e: (None, True)  # 注册表已采纳但没有这一对
    calls = _run_sync_ok(monkeypatch)
    client = _operator_client(tmp_path, runtime)
    r = client.post(
        "/v1/operator/publish",
        json={"app_id": APP_ID},
        headers={"Authorization": "Bearer s", "X-Business-Entry": "operator"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "OPERATOR_NOT_BOUND"
    assert calls == []


def test_an_app_that_is_not_this_entnry_binding_is_a_404(tmp_path, monkeypatch) -> None:
    """**绑定枚举防线**：拿一个**别的** app_id 来试 ⇒ 404，且不走到发布实现。

    没有这一条时，任何运营会话都能用任意 app_id 探测有哪些绑定存在。
    """
    runtime = _Runtime(tmp_path / "gateway.db")
    _bind_operator(runtime, tmp_path)
    calls = _run_sync_ok(monkeypatch)
    client = _operator_client(tmp_path, runtime)
    r = client.post(
        "/v1/operator/publish",
        json={"app_id": "00000000-0000-0000-0000-000000000000"},
        headers={"Authorization": "Bearer s", "X-Business-Entry": "operator"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "DIFY_APP_NOT_BOUND"
    assert calls == []


def test_the_operator_body_cannot_carry_anything_but_an_app_id(tmp_path, monkeypatch) -> None:
    """请求体只许有 app_id：多一个字段就是 422，租户/入口**无法**被请求体改写。"""
    runtime = _Runtime(tmp_path / "gateway.db")
    _bind_operator(runtime, tmp_path)
    _run_sync_ok(monkeypatch)
    client = _operator_client(tmp_path, runtime)
    r = client.post(
        "/v1/operator/publish",
        json={"app_id": APP_ID, "tenant_id": "T-OTHER"},
        headers={"Authorization": "Bearer s", "X-Business-Entry": "operator"},
    )
    assert r.status_code == 422


def test_an_operator_cannot_publish_another_tenants_app(tmp_path, monkeypatch) -> None:
    """**跨租户防线**：T-1 的运营会话去发布 T-2 绑的 app ⇒ 404，不发布。

    这一条是上面「不属于本入口」那一条的**真形态** —— 上一条里那个 app_id 在注册表里
    根本不存在，所以 `_publish_bound_app` 自己也会 404，两条防线分不开。
    这里 T-2 的 app **确实存在**：若只靠 `_publish_bound_app`，它会照发不误。
    """
    from aiops_diagnostics.dify_app_registry import DifyAppBinding

    other = "11111111-1111-1111-1111-111111111111"
    runtime = _Runtime(tmp_path / "gateway.db")
    _bind_operator(runtime, tmp_path)
    # 另一个租户真的绑了另一个 app
    runtime.dify_app_registry.put(
        DifyAppBinding(tenant_id="T-2", business_entry="operator", app_id=other, agent_name="别的")
    )
    calls = _run_sync_ok(monkeypatch)
    client = _operator_client(tmp_path, runtime)
    r = client.post(
        "/v1/operator/publish",
        json={"app_id": other},
        headers={"Authorization": "Bearer s", "X-Business-Entry": "operator"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "DIFY_APP_NOT_BOUND"
    assert calls == [], "跨租户的 app 一个字节都不该被发布"
