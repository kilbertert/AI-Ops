"""C 端站点：可见范围来自**本人订单**，不是整个租户（ADR-0177）。

## 为什么不能直接复用管家那条路

管家的站点可见范围是「账号的站点集合」，`DataScope` 里就有；C 端的 `DataScope` 是
`self`，`resolve_query_scope` 对它返回 `site_ids=None` —— 而在 `query_sites` 里
`None` 表示**不过滤**。照抄管家那条路，等于把整个租户的站点名单交给任何一个 C 端用户。

所以 C 端的范围必须另算：把他自己下过单的站点取出来，再拿这份集合去收窄。

## 取数事实（公司源码 `iot/cloud-charging-pile:release` 只读核对）

`GET /chOrderInfo/getOrderUserPage` 的 `user-id` 头是 `required = false`，
而 SQL 是 `<if test="param.userId != null and param.userId != ''"> and coi.user_id = #{param.userId}</if>`
—— **头缺失或为空串时这个条件整段消失**，返回的是整个租户的订单。这不是传闻，
是 `ChOrderInfoMapper.xml` 里的 `getOrderPage`。所以本模块的读取函数**永远**显式带
`user-id`，并且在没有 C 端用户身份时直接拒绝，不构造那个请求。

响应行是 `OrderPageResp`：`select coi.*` 带出 `site_id` 与 `tenant_id`，两者都可核对。

## 边界

- 订单最多翻 4 页（每页 50 条）、站点最多 200 个；超限时报告 `scope_truncated`，
  不把截断后的集合说成全部。
- 站点接口本身仍是「最多 4 页、展示最多 10 条」的既有预算，因此本人站点很多时
  关键词搜索可能漏掉排在第 4 页之后的站点。这是既有边界的后果，不在这里悄悄放宽。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

from aiops_diagnostics.common.bounded_http import ErrorMapping, RequestSpec, request_json
from aiops_diagnostics.operator import operator_cpms

#: 订单分页：一页 50 条、最多 4 页。与站点查询的预算同源，一起防「列举变成导出」。
ORDER_PAGE_SIZE = 50
MAX_ORDER_PAGES = 4
#: 本人站点集合的上限。超了就标注截断，不冒充完整。
MAX_OWN_SITES = 200

ORDER_PAGE_PATH = "/chOrderInfo/getOrderUserPage"


class ConsumerSitesRefused(RuntimeError):
    """C 端站点查询不能成立。``code`` 是回给调用方的稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _unavailable(_):
    return ConsumerSitesRefused("SITES_UNAVAILABLE", "site lookup unavailable")


def _get(path: str, tenant_id: str, c_user_id: str, *, timeout: int = 5) -> dict:
    """C 端身份下的只读 GET：`tenant-id` + `user-id`，**不带**任何服务令牌。

    这与既有 C 端取数（充电状态）是同一种凭据形状；公司令牌不进 C 端路径。
    """
    return request_json(
        RequestSpec(
            url=path,
            headers={"Accept": "application/json", "tenant-id": tenant_id, "user-id": c_user_id},
            timeout=timeout,
            max_read_bytes=262144,
            follow_redirects=False,
        ),
        mapping=ErrorMapping(
            auth_rejected=_unavailable,
            http_error=_unavailable,
            unavailable=_unavailable,
            invalid_body=_unavailable,
            invalid_envelope=_unavailable,
        ),
    )


def order_reader(base_url: str, tenant_id: str, c_user_id: str):
    """读本人订单的 `siteId`。**永远**带 `user-id`，否则上游会返回整个租户。"""

    def read(page: int) -> dict:
        query = urlencode({"current": page, "size": ORDER_PAGE_SIZE})
        return _get(base_url + ORDER_PAGE_PATH + "?" + query, tenant_id, c_user_id)

    return read


def site_reader(base_url: str, tenant_id: str, c_user_id: str):
    """给 :func:`operator_cpms.query_sites` 用的读取函数（C 端凭据形状）。"""

    def read(path: str, tenant: str) -> dict:
        return _get(base_url + path, tenant, c_user_id)

    return read


@dataclass(frozen=True, slots=True)
class OwnScope:
    """本人订单得出的站点可见范围。"""

    site_ids: tuple[str, ...]
    orders_seen: int
    truncated: bool


def own_site_scope(tenant_id: str, c_user_id: str, read_orders) -> OwnScope:
    """本人订单里出现过的站点标识。

    没有 C 端用户身份时**拒绝**，不返回空集合，也不退化成不说「不按用户过滤」——
    后者会把整租户交出去，而前者会被读成「你没有站点」。
    """
    if not c_user_id or not tenant_id:
        raise ConsumerSitesRefused("CONSUMER_SCOPE_REQUIRED", "consumer identity required")
    sites: list[str] = []
    seen: set[str] = set()
    orders = 0
    truncated = False
    for page in range(1, MAX_ORDER_PAGES + 1):
        body = read_orders(page)
        data = body.get("data") if isinstance(body, dict) else None
        if (
            not isinstance(body, dict)
            or type(body.get("code")) is not int
            or body["code"] != 0
            or not isinstance(data, dict)
            or not isinstance(data.get("records"), list)
            or len(data["records"]) > ORDER_PAGE_SIZE
            or type(data.get("total")) is not int
            or data["total"] < 0
            or data.get("current") != page
            or type(data.get("current")) is not int
            or data.get("size") != ORDER_PAGE_SIZE
        ):
            raise ConsumerSitesRefused("SITES_INVALID_RESPONSE", "order list response is invalid")
        for row in data["records"]:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("siteId"), str)
                or not row["siteId"]
                or str(row.get("tenantId") or "") != tenant_id
            ):
                raise ConsumerSitesRefused("SITES_SCOPE_MISMATCH", "order row is outside the tenant")
            orders += 1
            site_id = row["siteId"]
            if site_id not in seen:
                if len(sites) >= MAX_OWN_SITES:
                    truncated = True
                    continue
                seen.add(site_id)
                sites.append(site_id)
        more = page * ORDER_PAGE_SIZE < data["total"]
        if not more or not data["records"]:
            break
        truncated = truncated or more and page == MAX_ORDER_PAGES
    return OwnScope(tuple(sites), orders, truncated)


def consumer_site_scope(caller, read_orders):
    """把 C 端会话身份翻译成 `query_sites` 能吃的可见范围。

    空元组表示「本人订单里没有站点」——`query_sites` 对空范围不发任何 CPMS 请求，
    这正是「确实没有」与「关键词没匹配」需要分开的两种情形之一。
    """
    scope = own_site_scope(caller.effective_tenant_id, caller.subject.c_user_id or "", read_orders)
    return (
        operator_cpms.SiteScope(caller.effective_tenant_id, scope.site_ids, caller.scope_fingerprint),
        scope,
    )


#: 调用方可以指定想显示的条数，但**不能**超过服务端固定的展示上限。
MAX_SHOWN = operator_cpms.SITE_MAX_SHOWN
#: 关键词的字符纪律：与管家工具同一条（ADR-0176），但**没有**「必须出现在提问原文里」
#: 那一半 —— C 端的关键词是调用方直接提交的，没有原文可比。范围仍完全由服务端推导。
_KEYWORD_FORBIDDEN = ("%", "_", "\\", "\r", "\n")


def parse_keywords(name: str, address: str) -> operator_cpms.SiteKeywords:
    """校验 C 端提交的关键词并转成查询用的值对象。"""
    for value in (name, address):
        if not isinstance(value, str) or len(value) > 80:
            raise ConsumerSitesRefused("SITES_INVALID_QUERY", "keyword is too long")
        if any(char in value for char in _KEYWORD_FORBIDDEN):
            raise ConsumerSitesRefused("SITES_INVALID_QUERY", "keyword contains a forbidden character")
    return operator_cpms.SiteKeywords(name, (address,) if address else ())


def parse_limit(limit: str | int | None) -> int:
    """调用方要更多就**拒绝**，不静默截到上限 —— 静默会让它以为拿到了全部。"""
    if limit in (None, ""):
        return MAX_SHOWN
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise ConsumerSitesRefused("SITES_INVALID_QUERY", "limit is not a number") from exc
    if not 1 <= value <= MAX_SHOWN:
        raise ConsumerSitesRefused("SITES_LIMIT_REJECTED", "limit exceeds the server-side cap")
    return value


def sites_for_consumer(caller, base_url: str, *, name: str = "", address: str = "", limit: int = MAX_SHOWN):
    """C 端站点查询的唯一实现：推导范围 → 读 CPMS → 投影。

    与管家那条路共用 :func:`operator_cpms.query_sites`；差别只在**范围怎么来**
    （本人订单对账号站点集合）与**计数口径的可信度**。
    """
    keywords = parse_keywords(name, address)
    c_user_id = caller.subject.c_user_id or ""
    read_orders = order_reader(base_url, caller.effective_tenant_id, c_user_id)
    scope, own = consumer_site_scope(caller, read_orders)
    result = operator_cpms.query_sites(
        scope,
        keywords,
        site_reader(base_url, caller.effective_tenant_id, c_user_id),
        lambda: caller.scope_fingerprint,
        # 上游按租户返回，范围外的站点是预期内的：本地过滤，不当违约。
        outside_scope="skip",
    )
    if scope.site_ids == ():
        empty = "none_visible"
    elif not result["sites"]:
        empty = "no_match"
    else:
        empty = None
    return {
        "source": result["source"],
        "observed_at": result["observed_at"],
        "sites": result["sites"][:limit],
        "shown": len(result["sites"][:limit]),
        # 上游的 total 是**租户**口径的匹配数：本站点集合还要再与本人可见范围取交集，
        # 两者不等，所以这里不给总数，而不是给一个会被人当成"你有多少个"的数字。
        "total": None,
        "scope": "own_orders",
        "scope_truncated": own.truncated,
        "limited": result["limited"] or own.truncated,
        "empty": empty,
    }
