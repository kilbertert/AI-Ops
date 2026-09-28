"""管家端订单授权链的共用替身（PRD #423 的 #424~#428 四个子票）。

三个测试片（订单授权、入口动作、负向与回归）驱动同一条链路：会话 C 端 id →
C→B 映射（#424）→ 店铺集合（``/shopuser/getShops``，#425）→ 站点归属映射
（``ch_site.shop_id → ch_site.id``，#425）→ 订单授权判定（#426）。替身集中在
这里写一次，各片只放自己的场景与断言——替身各写一份会各自漂移，而漂移的表现是
「一个实现退化在所有片里都转绿」。

替身的三条纪律（四个片沿用）：

1. 假 MySQL 只做**范围谓词的行级镜像**（租户/站点/用户），因此「回落到 self」
   「把店铺 id 当站点 id」这类退化会让订单查询直接转红；不参与 SQL 文本断言。
2. 运营商站点范围替身内部走 ``resolve_operator_site_scope`` 这个**真实**解析
   函数，只替换两处 I/O（UPMS 店铺集合、充电库站点归属），保留四跳链里唯一有
   判断的那一跳。
3. 助手入口应用用**真实**的授权判定与**真实**的范围下推，不桩掉范围。

真实数据特征用于构造替身：954 行站点中 ``ch_site.id ≡ ch_site.shop_id`` 的
953 行、1 行不同；310 个代理商账号中只有 131 个有店铺绑定。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from aiops_diagnostics.caller_auth import ScopedOrderAuthorizer
from aiops_diagnostics.config import Settings
from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.query_scope import (
    QueryScope,
    resolve_operator_site_scope,
    static_site_mapper,
)
from aiops_diagnostics.scope_context import (
    SCOPE_TYPE_ORGAN,
    SCOPE_TYPE_SELF,
    DataScope,
    ScopeContext,
    SubjectRecord,
)
from aiops_diagnostics.shortcut_lifecycle import ShortcutManager, ShortcutStore

TENANT = "T-1"
C_USER_ID = "C-1"
B_USER_ID = "B-9"
INSIDE_CREDENTIAL = "upms-internal-token"
UPMS_BASE_URL = "https://upms.example.test"
SESSION_TOKEN = "session-123456789012345"

SITE_IN = "SITE-IN-1"  # 运营商站点集合内
SITE_OUT = "SITE-OUT-2"  # 集合外

#: 四条订单：集合内但挂在别人名下、集合外但只挂在本人名下、本人的一条集合内订单、
#: 一个不存在的订单号（拒绝不区分「不存在」与「无权」的对照组）。
ORDER_INSIDE = "2096164064667852801"
ORDER_OUTSIDE = "2096164064667852802"
ORDER_OWN_IN = "2096164064667852803"
ORDER_MISSING = "9999999999999999999"

ORDERS = (
    {"order_no": ORDER_INSIDE, "tenant_id": TENANT, "site_id": SITE_IN, "user_id": "C-OTHER"},
    {"order_no": ORDER_OUTSIDE, "tenant_id": TENANT, "site_id": SITE_OUT, "user_id": C_USER_ID},
    {"order_no": ORDER_OWN_IN, "tenant_id": TENANT, "site_id": SITE_IN, "user_id": C_USER_ID},
)

#: 「订单检测」的点击问句：不带订单号，由动作的 requires_order 决定去向。
DIAGNOSIS_QUESTION = "帮我检测这个订单的充电异常"
#: 追问问句：只含订单业务线索，因此走活跃订单分支而不是普通问答。
FOLLOWUP_QUESTION = "我刚才那笔充电订单为什么突然停了"
FOLLOWUP_AFTER_LOSS = "那笔订单现在怎么还不退款"
AGENT_VERSION_KEY = "agt_abcdef1234567890#v1"

OPERATOR_HEADERS = {"Authorization": "Bearer service", "X-Business-Entry": "operator"}
CONSUMER_HEADERS = {"Authorization": "Bearer service", "X-Business-Entry": "consumer"}


# --- 会话替身：Redis 会话值 + C→B 映射 -------------------------------------


class _Redis:
    def __init__(self, value: bytes) -> None:
        self.value = value

    def get(self, key: str):
        assert key.startswith("app:3rd_session:")
        return self.value


def java_session(payload: dict[str, Any]) -> bytes:
    """A Redis value as the Java BFF stores it: header + embedded JSON string."""
    return b"\xac\xed\x00\x05t\x00" + json.dumps(payload).encode()


def _redis_session(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any] | None = None) -> None:
    value = java_session(payload or {"userId": C_USER_ID, "tenantId": TENANT})
    monkeypatch.setattr("aiops_diagnostics.third_session_auth.redis.Redis", lambda **_: _Redis(value))


def session_settings():
    from aiops_diagnostics.third_session_auth import ThirdSessionSettings

    return ThirdSessionSettings(
        "127.0.0.1", 6379, 0, password="secret", service_token="svc", key_prefix="app:3rd_session:"
    )


class BSubjectDirectory:
    """In-memory C→B mapping directory recording every lookup.

    ``records`` 可以是按 C 端 id 分组的字典（端点按 ``userId`` 查询后的真实形状），
    也可以是所有查询共用的一组记录（用例更短）。``error`` 注入「上游不可达」这一跳：
    没有它，映射失败路径的用例写不出来。
    """

    def __init__(
        self,
        records: Mapping[str, tuple[SubjectRecord, ...]] | tuple[SubjectRecord, ...] = (),
        *,
        error: Exception | None = None,
    ) -> None:
        self.records = records
        self.error = error
        self.calls: list[str] = []

    def users_by_c_user_id(self, c_user_id: str) -> tuple[SubjectRecord, ...]:
        self.calls.append(c_user_id)
        if self.error is not None:
            raise self.error
        if isinstance(self.records, Mapping):
            return self.records.get(c_user_id, ())
        return self.records


def b_subject(**overrides: Any) -> SubjectRecord:
    fields: dict[str, Any] = {"b_user_id": B_USER_ID, "c_user_id": C_USER_ID, "tenant_id": TENANT}
    fields.update(overrides)
    return SubjectRecord(**fields)


class FakeShops:
    """内存店铺归属目录（#425 的 ``ShopDirectory`` 接缝）。

    ``error`` 让「上游不可达」这一跳可注入：复制一份不带它的替身，就等于让
    失败路径的用例无法在这里写。
    """

    def __init__(self, shop_ids: tuple[str, ...] = (), *, error: Exception | None = None) -> None:
        self.shop_ids = shop_ids
        self.error = error
        self.calls: list[str] = []

    def shop_ids_by_b_user_id(self, b_user_id: str) -> tuple[str, ...]:
        self.calls.append(b_user_id)
        if self.error is not None:
            raise self.error
        return self.shop_ids


class OperatorScope:
    """会话身份的运营商站点范围替身：内部走 #425 的真实解析函数。"""

    def __init__(
        self,
        shop_ids: tuple[str, ...] = ("SHOP-1",),
        sites: dict[str, tuple[str, ...]] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.shops = FakeShops(shop_ids, error=error)
        self.sites = sites or {}
        self.calls: list[tuple[str, str]] = []

    def site_scope_by_b_user_id(self, b_user_id: str, tenant_id: str) -> QueryScope:
        self.calls.append((b_user_id, tenant_id))
        return resolve_operator_site_scope(
            b_user_id,
            tenant_id,
            shops=self.shops,
            mapper=static_site_mapper(sites_by_shop=self.sites),
        )


def resolve_session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    records: tuple[SubjectRecord, ...] = (b_subject(),),
    operator_scope: OperatorScope | None = None,
) -> ScopeContext:
    """resolve one third-session caller, with the operator directories faked.

    ``operator_scope`` 默认 ``None``（= 生产未配置时的接线）：需要运营商站点集合的
    用例显式传一个 ``OperatorScope``。
    """
    from aiops_diagnostics.third_session_auth import RedisThirdSessionResolver

    _redis_session(monkeypatch)
    resolver = RedisThirdSessionResolver(
        session_settings(),
        b_subject_directory=BSubjectDirectory(records),
        operator_scope=operator_scope,
    )
    return resolver.resolve("svc", required_scope="aiops:diagnoses:write", third_session=SESSION_TOKEN)


def operator_session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    shop_ids: tuple[str, ...] = ("SHOP-1",),
    sites: dict[str, tuple[str, ...]] | None = None,
    error: Exception | None = None,
) -> tuple[ScopeContext, OperatorScope]:
    scope = OperatorScope(shop_ids, sites, error=error)
    return resolve_session(monkeypatch, operator_scope=scope), scope


def consumer_session(monkeypatch: pytest.MonkeyPatch) -> ScopeContext:
    """A C-side caller with no B 端 account: the normal consumer state."""
    return resolve_session(monkeypatch, records=())


# --- 假 MySQL：渲染出的 WHERE 的行级镜像 -------------------------------------


class Connection:
    """按渲染出的 WHERE 选行的假 MySQL 连接。

    只做范围谓词的行级镜像（租户/站点/用户），不参与任何 SQL 文本断言——断言
    的对象是「能查 / 被拒绝」。
    """

    def __init__(
        self,
        *,
        orders: tuple[dict[str, Any], ...] = ORDERS,
        sites: tuple[dict[str, Any], ...] = (),
    ) -> None:
        self.orders = list(orders)
        self.sites = list(sites)
        self.executed: list[tuple[str, list[Any]]] = []

    def cursor(self) -> Connection:
        return self

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, *_args: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, list(params or [])))

    def fetchall(self) -> list[dict[str, Any]]:
        sql, params = self.executed[-1]
        if "ch_site" in sql:
            tenant_id = str(params[0])
            wanted = {str(value) for value in params[1:]}
            return [
                row
                for row in self.sites
                if str(row.get("tenant_id")) == tenant_id and str(row.get("shop_id")) in wanted
            ]
        where = sql.split(" WHERE ", 1)[1].rsplit(" ORDER BY ", 1)[0]
        if "1=0" in where:
            return []
        rows = list(self.orders)
        remaining = list(params)
        for fragment in where.split(" AND "):
            if fragment.startswith("site_id IN ("):
                sites = {str(remaining.pop(0)) for _ in range(fragment.count("%s"))}
                rows = [row for row in rows if str(row.get("site_id")) in sites]
                continue
            value = str(remaining.pop(0))
            if fragment == "order_no=%s":
                rows = [row for row in rows if str(row.get("order_no")) == value]
            elif fragment == "tenant_id=%s":
                rows = [row for row in rows if str(row.get("tenant_id")) == value]
            elif fragment == "user_id=%s":
                rows = [row for row in rows if str(row.get("user_id")) == value]
            else:
                raise AssertionError(f"unexpected scope fragment: {fragment}")
        return rows

    def fetchone(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


def build_authorizer(monkeypatch: pytest.MonkeyPatch, connection: Connection) -> ScopedOrderAuthorizer:
    settings = Settings.from_env()
    settings.mysql.user = "readonly"
    settings.mysql.password = "secret"
    settings.ssh.enabled = False
    monkeypatch.setattr("aiops_diagnostics.sources.pymysql.connect", lambda **_: connection)
    return ScopedOrderAuthorizer(settings)


def context(data_scope: DataScope, *, c_user_id: str | None = C_USER_ID) -> ScopeContext:
    subject = SubjectRecord(b_user_id=B_USER_ID, c_user_id=c_user_id, tenant_id=TENANT)
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id=TENANT,
        data_scope=data_scope,
        roles=frozenset(),
        permissions=frozenset({"aiops:diagnoses:write"}),
    )


def operator_context(sites: tuple[str, ...]) -> ScopeContext:
    return context(DataScope(type=SCOPE_TYPE_ORGAN, site_ids=sites))


def consumer_context() -> ScopeContext:
    return context(DataScope(type=SCOPE_TYPE_SELF))


def order_queries(connection: Connection) -> list[tuple[str, list[Any]]]:
    return [item for item in connection.executed if "ch_order_info" in item[0]]


# --- 助手入口栈：运行时替身 + 平台身份 + 网关应用 ----------------------------


class Runtime:
    """记录「有没有启动作业 / 启动的是哪一单 / 问了什么」的运行时替身。"""

    def __init__(self) -> None:
        self.diagnoses: list[tuple[str, str]] = []
        self.questions: list[dict[str, Any]] = []

    def shutdown(self) -> None:
        return None

    def start_standard_diagnosis(
        self, context: ScopeContext, order_no: str, question: str, indicator_code, language="zh"
    ):
        del context, indicator_code
        self.diagnoses.append((order_no, question))
        return {
            "diagnosis_id": "dx_test000000000000000000000000000001",
            "order_no": order_no,
            "question": question,
            "indicator_code": None,
            "status": "queued",
            "result": None,
            "error_code": None,
            "error_message": None,
            "created_at": "2026-09-07T00:00:00+00:00",
            "updated_at": "2026-09-07T00:00:00+00:00",
            "completed_at": None,
        }

    def get_standard_diagnosis(self, context: ScopeContext, diagnosis_id: str):
        del context, diagnosis_id
        return None

    classified = None

    def classify_lightweight(self, question: str, *, language: str = "zh", tenant_id: str | None = None):
        del question, language, tenant_id
        return self.classified

    def start_assistant_qa(self, context: ScopeContext, question: str, **kwargs: Any):
        del context, kwargs
        record = {
            "qa_id": "qa_test00000000000000000000000000000001",
            "question": question,
            "status": "queued",
            "result": None,
        }
        self.questions.append(record)
        return record

    def get_assistant_qa(self, context: ScopeContext, qa_id: str):
        del context
        return next((item for item in self.questions if item["qa_id"] == qa_id), None)

    def list_assistant_qa(self, context: ScopeContext, *, limit: int = 50):
        del context, limit
        return []


class Directory:
    """平台身份目录：该会话有 admin client_type，operator 入口可用。"""

    def roles_for_c_user(self, c_user_id: str, tenant_id: str):
        return (PlatformRoleRecord(B_USER_ID, c_user_id, tenant_id, "admin"),)

    def roles_for_b_user(self, b_user_id: str, tenant_id: str):
        return ()


class Caller:
    """把每个请求都解析成给定的会话身份；身份可在轮与轮之间替换。

    换身份就是「权限变了」的真实形状：下一次请求重新解析会话，得到一个新的
    站点集合（或一个新的范围指纹），而不是沿用上一轮的上下文。
    """

    def __init__(self, context: ScopeContext) -> None:
        self.context = context

    def resolve(self, token: str, *, required_scope: str, third_session: str | None = None) -> ScopeContext:
        del token, third_session, required_scope
        return self.context


class PublisherContext:
    """运营发布者的身份：租户管理员在该租户的 operator 入口发布既有动作。"""

    effective_tenant_id = TENANT
    roles = frozenset({"ROLE_AGENT_ADMIN"})
    caller = SubjectRecord(b_user_id="ops-admin", tenant_id=TENANT)


def gateway_settings(tmp_path: Path) -> GatewayServerSettings:
    import os

    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    os.chmod(settings.server_config_file, 0o600)  # the config is a private file
    return settings


def publish(manager: ShortcutManager, entries: tuple[str, ...]) -> None:
    """按 租户 + 入口 发布种子行（PRD 说的那个数据操作）。"""
    context = PublisherContext()
    for seeded in manager.store.seed_bundled(context, manager):
        if seeded.business_entry in entries:
            manager.publish(context, seeded.shortcut_id, expected_revision=seeded.revision)


def assistant_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caller: Caller,
    connection: Connection,
    *,
    published_entries: tuple[str, ...] = ("operator",),
    runtime: Runtime | None = None,
) -> tuple[TestClient, Runtime]:
    """助手入口应用：真实授权判定 + 真实范围下推 + 已发布的入口动作。"""
    settings = gateway_settings(tmp_path)
    store = GatewayStore(settings.database_file)
    shortcuts = ShortcutManager(ShortcutStore(settings.database_file))
    publish(shortcuts, published_entries)
    selected = runtime if runtime is not None else Runtime()
    app = create_gateway_app(
        settings=settings,
        store=store,
        runtime=selected,  # type: ignore[arg-type]
        caller_resolver=caller,
        order_authorizer=build_authorizer(monkeypatch, connection),
        platform_resolver=PlatformIdentityResolver(Directory()),
        faq_catalog=FAQCatalog.bundled(),
        shortcut_manager=shortcuts,
    )
    return TestClient(app), selected


def operator_caller(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sites: dict[str, tuple[str, ...]],
    shop_ids: tuple[str, ...] = ("SHOP-1",),
) -> Caller:
    """#426 的运营商会话身份：店铺集合 → 站点集合（经 #425 的真实解析函数）。"""
    return Caller(operator_session(monkeypatch, shop_ids=shop_ids, sites=sites)[0])


# --- UPMS 传输替身（三个重复过的同一个替身，只写一次）------------------------


class FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


class FakeUpmsTransport:
    """Serve the documented UPMS internal-endpoint envelope."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.requests: list[tuple[str, dict[str, str]]] = []

    def __call__(self, request: Any, timeout: int | None = None) -> FakeResponse:
        split = urlsplit(request.full_url)
        path = f"{split.path}?{split.query}" if split.query else split.path
        self.requests.append((path, dict(request.header_items())))
        return FakeResponse({"code": 0, "msg": "ok", "data": self.payload})


def patch_transport(monkeypatch: pytest.MonkeyPatch, transport: FakeUpmsTransport) -> None:
    monkeypatch.setattr("aiops_diagnostics.bounded_http.urllib.request.urlopen", transport)


def mysql_settings() -> Settings:
    settings = Settings.from_env()
    settings.mysql.user = "readonly"
    settings.mysql.password = "secret"
    settings.ssh.enabled = False
    return settings


def upms_settings() -> Settings:
    settings = mysql_settings()
    settings.upms.base_url = UPMS_BASE_URL
    return settings
