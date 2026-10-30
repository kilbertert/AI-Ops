"""C 端站点只读接口（#1192）：范围来自**本人订单**，不是整个租户。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aiops_diagnostics.consumer import consumer_sites
from aiops_diagnostics.gateway.gateway_api import create_gateway_app
from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway.gateway_store import GatewayStore
from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord

CPMS = "https://cpms.test"
HEADERS = {"Authorization": "Bearer service", "X-Business-Entry": "consumer"}


class _Caller:
    """C 端会话：`self` 范围、有 C 端用户标识、没有站点集合。"""

    def __init__(self, *, c_user_id: str | None = "C-1") -> None:
        self.c_user_id = c_user_id

    def resolve(self, token, *, required_scope, third_session=None, platform_entry=None, source_key=None):
        subject = SubjectRecord(b_user_id="c:C-1", c_user_id=self.c_user_id, tenant_id="T-1")
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id="T-1",
            data_scope=DataScope(type="self"),
            roles=frozenset(),
            permissions=frozenset({required_scope}),
        )


class _Directory:
    def roles_for_c_user(self, c_user_id, tenant_id):
        return ()

    def roles_for_b_user(self, b_user_id, tenant_id):
        return ()


class _Runtime:
    def shutdown(self):
        pass


def client(tmp_path: Path, *, c_user_id: str | None = "C-1") -> TestClient:
    config = tmp_path / "production.env"
    config.write_text("# test\n", encoding="utf-8")
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=config,
        charging_status_base_url=CPMS,
        execution_environment="env-a",
    )
    return TestClient(
        create_gateway_app(
            settings=settings,
            store=GatewayStore(settings.database_file),
            runtime=_Runtime(),  # type: ignore[arg-type]
            caller_resolver=_Caller(c_user_id=c_user_id),
            platform_resolver=None,
        )
    )


def page(rows, *, total=None, current=1):
    return {
        "code": 0,
        "data": {
            "records": rows,
            "total": total if total is not None else len(rows),
            "current": current,
            "size": 50,
        },
    }


def order(site: str, tenant: str = "T-1"):
    return {"orderNo": "O-" + site, "siteId": site, "tenantId": tenant}


def site(site_id: str, name: str, address: str = "", tenant: str = "T-1"):
    return {"id": site_id, "tenantId": tenant, "name": name, "address": address}


def wire(monkeypatch, *, orders, sites):
    """把出站请求接到合成 CPMS，并把每一个 `RequestSpec` 记下来。"""
    seen = []

    def fake(spec, **_kwargs):
        seen.append(spec)
        if consumer_sites.ORDER_PAGE_PATH in spec.url:
            return orders(spec)
        return sites(spec)

    monkeypatch.setattr("aiops_diagnostics.consumer.consumer_sites.request_json", fake)
    return seen


def test_sites_are_scoped_to_the_callers_own_orders(tmp_path, monkeypatch):
    """本人订单里是 A；租户站点列表里还有 B。只能看到 A。"""
    monkeypatch_seen = wire(
        monkeypatch,
        orders=lambda _spec: page([order("SITE-A")]),
        sites=lambda _spec: page([site("SITE-A", "本人站"), site("SITE-B", "别人的站")]),
    )
    with client(tmp_path) as http:
        response = http.get("/v1/sites", headers=HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [s["id"] for s in body["sites"]] == ["SITE-A"]
    assert body["scope"] == "own_orders"
    # 上游那个 total 是租户口径的匹配数，与「本人可见」不是同一个集合。
    assert body["total"] is None
    assert monkeypatch_seen, "至少发出了一次读取"


def test_order_query_always_carries_the_derived_user_id(tmp_path, monkeypatch):
    """上游 `user-id` 缺失时**不过滤用户**，返回整个租户。所以必须永远显式带上。"""
    seen = wire(
        monkeypatch,
        orders=lambda _spec: page([]),
        sites=lambda _spec: page([]),
    )
    with client(tmp_path) as http:
        http.get("/v1/sites", headers=HEADERS)
    order_specs = [s for s in seen if consumer_sites.ORDER_PAGE_PATH in s.url]
    assert order_specs, "订单列表必须真的被请求过"
    for spec in order_specs:
        assert spec.headers.get("user-id") == "C-1"
        assert spec.headers.get("tenant-id") == "T-1"
        # C 端路径不带任何服务令牌。
        assert "Authorization" not in spec.headers


def test_blank_consumer_identity_is_refused_before_any_read(tmp_path, monkeypatch):
    seen = wire(
        monkeypatch,
        orders=lambda _spec: page([order("SITE-A")]),
        sites=lambda _spec: page([site("SITE-A", "站")]),
    )
    with client(tmp_path, c_user_id=None) as http:
        response = http.get("/v1/sites", headers=HEADERS)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CONSUMER_SCOPE_REQUIRED"
    assert not seen, "没有 C 端身份时一个请求都不该发出去"


def test_a_missing_account_holds_a_refusal_not_an_empty_list(tmp_path, monkeypatch):
    """空身份要**拒绝**，不能退化成"你没有站点"——后者会让排查方向完全错掉。"""
    with pytest.raises(consumer_sites.ConsumerSitesRefused) as refused:
        consumer_sites.own_site_scope("T-1", "", lambda _page: page([]))
    assert refused.value.code == "CONSUMER_SCOPE_REQUIRED"


def test_cross_tenant_order_rows_are_rejected(tmp_path, monkeypatch):
    wire(
        monkeypatch,
        orders=lambda _spec: page([order("SITE-A", tenant="OTHER")]),
        sites=lambda _spec: page([]),
    )
    with client(tmp_path) as http:
        response = http.get("/v1/sites", headers=HEADERS)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SITES_SCOPE_MISMATCH"


def test_truncated_scope_is_reported_not_passed_off_as_complete(tmp_path, monkeypatch):
    """订单翻到上限还没翻完，就必须标出来。"""
    wire(
        monkeypatch,
        orders=lambda spec: page(
            [order(f"SITE-{spec.url[-8:]}")], total=10_000, current=int(spec.url.rsplit("current=", 1)[1][:1])
        ),
        sites=lambda _spec: page([site("SITE-1", "站")]),
    )
    with client(tmp_path) as http:
        body = http.get("/v1/sites", headers=HEADERS).json()
    assert body["scope_truncated"] is True
    assert body["limited"] is True


def test_limit_above_the_server_cap_is_rejected(tmp_path, monkeypatch):
    """静默截到上限会让调用方以为拿到了全部，所以直接拒。"""
    wire(monkeypatch, orders=lambda _spec: page([]), sites=lambda _spec: page([]))
    with client(tmp_path) as http:
        too_big = http.get("/v1/sites?limit=11", headers=HEADERS)
        ok = http.get("/v1/sites?limit=3", headers=HEADERS)
    assert too_big.status_code == 400
    assert too_big.json()["error"]["code"] == "SITES_LIMIT_REJECTED"
    assert ok.status_code == 200


def test_empty_reasons_are_distinguishable(tmp_path, monkeypatch):
    """「确实没有」与「关键词没匹配」的下一步不同，不能混成同一个空。"""
    wire(monkeypatch, orders=lambda _spec: page([]), sites=lambda _spec: page([]))
    with client(tmp_path) as http:
        none_visible = http.get("/v1/sites", headers=HEADERS).json()

    wire(
        monkeypatch,
        orders=lambda _spec: page([order("SITE-A")]),
        sites=lambda _spec: page([site("SITE-A", "天河测试站")]),
    )
    with client(tmp_path) as http:
        no_match = http.get("/v1/sites?name=哈尔滨", headers=HEADERS).json()

    assert none_visible["empty"] == "none_visible"
    assert no_match["empty"] == "no_match"


def test_the_endpoint_uses_the_single_scope_derivation(tmp_path, monkeypatch):
    """范围推导只有一处：接口拿到的集合必须等于直接调那个函数的结果。"""
    rows = [order("SITE-A"), order("SITE-B"), order("SITE-A")]
    wire(
        monkeypatch,
        orders=lambda _spec: page(rows),
        sites=lambda _spec: page([site("SITE-A", "甲"), site("SITE-B", "乙")]),
    )
    with client(tmp_path) as http:
        body = http.get("/v1/sites", headers=HEADERS).json()
    direct = consumer_sites.own_site_scope("T-1", "C-1", consumer_sites.order_reader(CPMS, "T-1", "C-1"))
    assert tuple(s["id"] for s in body["sites"]) == direct.site_ids


def test_keywords_are_validated_without_the_model_provenance_rule():
    """C 端关键词由调用方直接提交，没有「提问原文」可比：只受长度与字符约束。"""
    assert consumer_sites.parse_keywords("天河", "广州").address_terms == ("广州",)
    for bad in ("\n", "%", "_", "\\"):
        with pytest.raises(consumer_sites.ConsumerSitesRefused):
            consumer_sites.parse_keywords(bad, "")
    with pytest.raises(consumer_sites.ConsumerSitesRefused):
        consumer_sites.parse_keywords("x" * 81, "")
