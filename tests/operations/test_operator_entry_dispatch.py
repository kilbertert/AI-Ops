"""登记表模式下 operator 入口的 UPMS 链在已发布问答面端到端成立（ADR-0320 后续）。

Devin 首审缺陷：UPMS 混合范围（org/shop/site 断言）进 `EntryCallerResolver` 后，
`refresh_scope` 复核换成纯站点集合——同一组站点两种指纹，`start` 误报
OPERATOR_SCOPE_CHANGED；纯店铺断言更在复核前被 `operator_scope`（mapper=None）
判成空站点拒绝。本文件用**真实** ``EntryCallerResolver`` + UPMS 合同形状传输替身
+ Redis 存储替身驱动已发布 operator 问答的公网白名单路径（POST/GET
`/v1/assistant/questions`、`/v1/conversations`），证明：

- 混合断言与纯店铺断言的 operator 都能走完 start+read（范围指纹前后一致）；
- 实时站点集合缩小/撤权仍拒绝（复核不是可绕过的形式）；
- 会话链在白名单路径上原样工作（入口分派没把它改道）；
- UPMS 判拒/不可达不落回会话链（无「换链再猜一遍」）。

替身纪律同 ``test_operator_cpms``：只换 I/O（UPMS 传输、Redis 存储、模型
dispatch），范围解析、指纹比对、刷新目录全部真实。
"""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from aiops_diagnostics.common.config import Settings, UpmsSettings
from aiops_diagnostics.identity.caller_auth import EntryCallerResolver, UpmsCallerResolver
from aiops_diagnostics.identity.third_session_auth import (
    RedisThirdSessionResolver,
    ThirdSessionSettings,
    UpmsOperatorSiteScope,
)
from aiops_diagnostics.operator import operator_cpms
from operations.test_operator_cpms import environment, node, request, terminal

UPMS_BASE = "https://upms.example.test"
SERVICE_TOKEN = "svc"
SESSION_TOKEN = "session-123456789012345"


@pytest.fixture
def operator(tmp_path, monkeypatch):
    with environment(tmp_path, monkeypatch) as f:
        yield f


class _UpmsResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _upms_transport(state):
    def transport(req, timeout=None):
        url = urlsplit(req.full_url)
        state["upms_calls"].append(url.path + ("?" + url.query if url.query else ""))
        if url.path == "/user/info":
            return _UpmsResponse({"code": 0, "msg": "ok", "data": state["user_info"]})
        if url.path == "/user/ds":
            return _UpmsResponse({"code": 0, "msg": "ok", "data": state["data_scope"]})
        if url.path == "/shopuser/getShops":
            # 后端权威授权（@ShopDataScope）同一个端点的合同形状：ID 数组。
            return _UpmsResponse({"code": 0, "msg": "ok", "data": state["shops"]})
        return _UpmsResponse({"code": 404, "msg": "not found", "data": None})

    return transport


def _user_info():
    return {
        "sysUser": {
            "id": "OPERATOR-A",
            "userId": "C-OPERATOR-A",
            "username": "ops.a",
            "tenantId": "TENANT-A",
            "type": "5",
        },
        "roles": [{"roleCode": "ROLE_OPERATOR"}],
        "permissions": ["order:diag:view"],
    }


class _RedisStore:
    def __init__(self):
        self.gets = []

    def get(self, key):
        self.gets.append(key)
        return json.dumps({"userId": "C-1", "tenantId": "TENANT-A"}).encode()


class _SiteRows:
    """``ch_site`` 归属映射的存储级替身：行存 shop/site/tenant，不是恒返回范围。"""

    def __init__(self, state):
        self._state = state

    def site_ids_by_shops(self, shop_ids, tenant_id):
        return tuple(
            row["site"]
            for row in self._state["site_rows"]
            if row["shop"] in shop_ids and row["tenant"] == tenant_id
        )

    def site_ids_by_points(self, point_ids, tenant_id):
        return ()


def _entry_state(f, monkeypatch, *, data_scope, shops=("SHOP-9",), site_rows=None, normalize=True):
    """真实分派器 + 真实 ``UpmsOperatorSiteScope`` 装进已装配的应用（Devin 8AU）。

    规范化与 ``refresh_scope`` 复核注入**同一个** ``site_scope_by_b_user_id``
    实例方法——两段的范围都来自 ``GET /shopuser/getShops``（替身传输里的假
    应答）+ ``ch_site`` 行（``_SiteRows`` 存储替身）此刻的真值，改 ``shops``/
    ``site_rows`` 就是模拟真实绑定/撤权，不是另塞一个恒返回 ``QueryScope``
    的回调。
    """
    state = {
        "user_info": _user_info(),
        "data_scope": data_scope,
        "shops": list(shops),
        "site_rows": site_rows
        if site_rows is not None
        else [{"shop": "SHOP-9", "site": "SITE-1", "tenant": "TENANT-A"}],
        "upms_calls": [],
        "redis_store": _RedisStore(),
    }
    # 生产实现内部 ``with mysql_site_mapper(settings)``——替身只换存储这一层，
    # ``UpmsOperatorSiteScope``、``resolve_operator_site_scope`` 与范围合并全是
    # 真代码。
    monkeypatch.setattr(
        "aiops_diagnostics.identity.third_session_auth.mysql_site_mapper",
        lambda _settings: contextlib.nullcontext(_SiteRows(state)),
    )
    site_scope_service = UpmsOperatorSiteScope(
        Settings(upms=UpmsSettings(base_url=UPMS_BASE)), "inside-token"
    )

    monkeypatch.setattr(
        "aiops_diagnostics.common.bounded_http.urllib.request.urlopen",
        _upms_transport(state),
    )
    monkeypatch.setattr(
        "aiops_diagnostics.identity.third_session_auth.redis.Redis",
        lambda **_: state["redis_store"],
    )
    ctx = f["ctx"]
    ctx.caller_resolver = EntryCallerResolver(
        UpmsCallerResolver(Settings(upms=UpmsSettings(base_url=UPMS_BASE))),
        RedisThirdSessionResolver(
            ThirdSessionSettings(
                host="127.0.0.1",
                port=6379,
                database=0,
                password="stub",
                service_token=SERVICE_TOKEN,
                key_prefix="app:3rd_session:",
            )
        ),
        site_scope=site_scope_service.site_scope_by_b_user_id if normalize else None,
    )
    # ``refresh_scope`` 复核用的目录与鉴权规范化同一个实时来源。
    ctx.runtime._trial_operator_scope = SimpleNamespace(
        site_scope_by_b_user_id=site_scope_service.site_scope_by_b_user_id
    )
    return state


def test_mixed_upms_scope_starts_and_reads_published_qa(operator, monkeypatch):
    """混合断言（org+shop+site）经规范化后与复核/回读同一权威站点范围。"""
    f = operator
    state = _entry_state(
        f,
        monkeypatch,
        data_scope={"type": 1, "organIds": ["ORG-1"], "shopIds": ["SHOP-9"], "siteIds": ["SITE-1"]},
    )
    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", lambda _, cfg, run: node(f, run))
    response = request(f["client"])
    assert response.status_code == 202, response.text
    final = terminal(f, response.json()["qa_id"])
    assert final.status_code == 200 and final.json()["status"] == "completed", final.text
    # 会话存储零读取——operator 链没有混进会话凭据。
    assert state["redis_store"].gets == []
    # 授权判定确实走了 /user/info + /user/ds + /shopuser/getShops 上游验证。
    assert "/user/info" in state["upms_calls"]
    assert "/user/ds" in state["upms_calls"]
    assert any(c.startswith("/shopuser/getShops?userId=") for c in state["upms_calls"])


def test_shop_only_upms_scope_maps_through_sites(operator, monkeypatch):
    """type5 纯店铺断言：鉴权规范化先过店铺→站点映射，复核前不被空站点拒。"""
    f = operator
    _entry_state(
        f,
        monkeypatch,
        data_scope={"type": 1, "organIds": [], "shopIds": ["SHOP-9"], "siteIds": []},
    )
    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", lambda _, cfg, run: node(f, run))
    response = request(f["client"])
    assert response.status_code == 202, response.text
    final = terminal(f, response.json()["qa_id"])
    assert final.status_code == 200 and final.json()["status"] == "completed", final.text


def test_unnormalized_upms_scope_falsely_reports_scope_change(operator, monkeypatch):
    """红反馈钉住缺陷形状：不规范化时同一组站点被判成权限变化。

    接缝未注入 → ``data_scope`` 保留 UPMS 原样断言——``refresh_scope`` 复核后
    换成纯站点集合，``start`` 的指纹比对把它当成权限变化拒掉。
    """
    f = operator
    _entry_state(
        f,
        monkeypatch,
        data_scope={"type": 1, "organIds": ["ORG-1"], "shopIds": ["SHOP-9"], "siteIds": ["SITE-1"]},
        normalize=False,
    )
    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", lambda _, cfg, run: node(f, run))
    response = request(f["client"])
    assert response.status_code != 202, response.text
    assert "OPERATOR_SCOPE_CHANGED" in response.text or "OPERATOR_SITE_SCOPE" in response.text


def test_direct_site_grant_without_shop_mapping_is_refused(operator, monkeypatch):
    """ds 直授站点不并入授权集合（Devin 8z-S 的精确边界）。

    ``/user/ds`` 断言 ``siteIds`` 是 UPMS 数据范围快照，不是本链的授权来源：
    权威是 ``getShops`` 此刻的店铺→站点映射。直授站点不在映射内 → 规范化收窄
    （**既有** ``refresh_scope`` 行为的首鉴对齐，不是新增收窄）；映射为空 →
    空站点按既有 fail closed 拒。不并集——并集等于给快照自行加授权。
    """
    f = operator
    _entry_state(
        f,
        monkeypatch,
        data_scope={"type": 1, "organIds": [], "shopIds": [], "siteIds": ["SITE-DIRECT"]},
        site_rows=[],  # 直授站点没有任何店铺归属映射
    )
    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", lambda _, cfg, run: node(f, run))
    response = request(f["client"])
    assert response.status_code != 202, response.text
    assert "OPERATOR_SITE_SCOPE" in response.text or "OPERATOR_SCOPE_CHANGED" in response.text


def test_live_sites_shrink_still_refuses(operator, monkeypatch):
    """复核不是形式：实时站点集合缩小 → 指纹真实变化 → 拒。"""
    f = operator
    state = _entry_state(
        f,
        monkeypatch,
        data_scope={"type": 1, "organIds": ["ORG-1"], "shopIds": ["SHOP-9"], "siteIds": ["SITE-1"]},
    )

    def shrink_during_run(_, cfg, run):
        state["site_rows"] = [{"shop": "SHOP-9", "site": "SITE-OTHER", "tenant": "TENANT-A"}]
        return node(f, run)

    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", shrink_during_run)
    response = request(f["client"])
    assert response.status_code == 202, response.text
    final = terminal(f, response.json()["qa_id"])
    # 撤权后回读拿不到 completed。
    assert final.status_code != 200 or final.json()["status"] != "completed", final.text


def test_session_chain_unchanged_on_whitelisted_routes(operator, monkeypatch):
    """入口分派不动会话链：consumer 显式入口与缺头（旧客户端）都按会话解析。"""
    f = operator
    state = _entry_state(f, monkeypatch, data_scope={"type": 1, "shopIds": ["SHOP-9"], "siteIds": []})
    for headers in ({"X-Business-Entry": "consumer"}, {}):
        response = f["client"].get(
            "/v1/conversations",
            headers={
                "Authorization": f"Bearer {SERVICE_TOKEN}",
                "X-Third-Session": SESSION_TOKEN,
                **headers,
            },
        )
        # 鉴权已过（非 401）：会话身份走到平台决策/会话存储层。
        assert response.status_code != 401, response.text
    assert state["redis_store"].gets == ["app:3rd_session:" + SESSION_TOKEN] * 2
    assert state["upms_calls"] == []  # 会话链从不打 UPMS


def test_upms_rejection_never_falls_back_on_qa_path(operator, monkeypatch):
    """UPMS 判拒（凭据无效）→ 401；会话存储零读取——拒绝不换链重试。"""
    f = operator
    state = _entry_state(f, monkeypatch, data_scope={"type": 1, "siteIds": ["SITE-1"]})

    def rejected(req, timeout=None):
        state["upms_calls"].append(urlsplit(req.full_url).path)
        return _UpmsResponse({"code": 403, "msg": "denied", "data": None})

    monkeypatch.setattr("aiops_diagnostics.common.bounded_http.urllib.request.urlopen", rejected)
    response = request(f["client"])
    assert response.status_code == 401, response.text
    assert state["redis_store"].gets == []
