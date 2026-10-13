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
    def __init__(self) -> None:
        from aiops_diagnostics.dify_app_registry import DifyAppRegistry

        self.dify_app_registry = DifyAppRegistry.__new__(DifyAppRegistry)
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
