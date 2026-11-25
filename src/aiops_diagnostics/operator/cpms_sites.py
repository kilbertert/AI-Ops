"""CPMS 站点只读查询、关键词匹配、逐页授权复核与工具投影。

从 `operator_cpms.py` 拆出，供管家节点工具、站点方案对比与 C 端站点查询共同复用。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

from aiops_diagnostics.common.scalar_rules import format_stored_time, now_utc
from aiops_diagnostics.operator import operator_conversation, operator_tools
from aiops_diagnostics.operator.cpms_evidence import cpms_get, operator_scope
from aiops_diagnostics.operator.operator_conversation import OperatorRefused

#: 站点查询的三个上限：一页条数、最多页数、最多展示条数（防列举演变为批量导出）。
SITE_PAGE_SIZE = 50
SITE_MAX_PAGES = 4
SITE_MAX_SHOWN = operator_conversation.SITE_MAX_SHOWN
SITE_PATH = "/chSite/page"


@dataclass(frozen=True, slots=True)
class SiteScope:
    """一次站点查询的可见范围。

    ``site_ids`` 三值语义不可混淆：
    - ``()``：空范围（不查 CPMS）；
    - ``None``：未按站点集合收窄（顶层租户口径）；
    - 非空元组：当前账号可见站点集。
    """

    tenant_id: str
    site_ids: tuple[str, ...] | None
    fingerprint: str


@dataclass(frozen=True, slots=True)
class SiteKeywords:
    """一次站点查询的关键词；全空表示列举（ADR-0172）。"""

    name: str = ""
    address_terms: tuple[str, ...] = ()

    @property
    def listing(self) -> bool:
        return not self.name and not self.address_terms

    @property
    def address(self) -> str:
        """答复可把同一地址拆成多个关键词；仅用于对齐已执行的工具参数。"""
        return "".join(self.address_terms)

    def params(self, page: int, address: str | None = None) -> dict[str, object]:
        params: dict[str, object] = {"current": page, "size": SITE_PAGE_SIZE, "type": 0}
        if self.name:
            params["name"] = self.name
        chosen = self.address if address is None else address
        if chosen:
            params["address"] = chosen
        return params

    def address_queries(self) -> tuple[str, ...]:
        """分别发给上游子串匹配的地址词；空元组表示不按地址过滤。"""
        return tuple(term for term in self.address_terms if term.strip())

    def matches(self, site: dict[str, str]) -> bool:
        """站名要求包含 ``name``；多地址词之间取 ``any`` 匹配。"""
        haystack = (site["name"] + " " + site["address"]).casefold()
        if self.name.casefold() not in site["name"].casefold():
            return False
        if not self.address_terms:
            return True
        return any(term.casefold() in haystack for term in self.address_terms)


def site_scope(caller, *, allow_empty: bool = True) -> SiteScope:
    """将调用者数据范围翻译为站点查询可见范围。"""
    scope = operator_scope(caller, allow_empty=allow_empty)
    return SiteScope(caller.effective_tenant_id, scope.site_ids, caller.scope_fingerprint)


def site_keywords(answer) -> SiteKeywords:
    """对话协议字段转站点关键词对象。"""
    return SiteKeywords(answer["name"], tuple(answer["address_terms"]))


def cpms_site_reader(base: str, bearer: str, *, getter=None):
    """构造直连 CPMS 的站点页读取函数。"""
    fetch = cpms_get if getter is None else getter

    def read(path: str, tenant: str):
        return fetch(base + path, bearer, tenant, timeout=3)

    return read


def site_page(scope: SiteScope, body, page: int, *, outside_scope="reject"):
    """校验单页站点响应（信封、分页回显、逐行租户与站点范围、字段形状）。"""
    data = body.get("data") if isinstance(body, dict) else None
    if (
        not isinstance(body, dict)
        or type(body.get("code")) is not int
        or body["code"] != 0
        or not isinstance(data, dict)
        or not isinstance(data.get("records"), list)
        or len(data["records"]) > SITE_PAGE_SIZE
        or type(data.get("total")) is not int
        or data["total"] < 0
        or data.get("current") != page
        or type(data.get("current")) is not int
        or data.get("size") != SITE_PAGE_SIZE
    ):
        raise OperatorRefused("CPMS_INVALID_RESPONSE")
    rows: list[dict[str, str]] = []
    for item in data["records"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not item["id"]
            or str(item.get("tenantId") or "") != scope.tenant_id
        ):
            raise OperatorRefused("CPMS_SCOPE_MISMATCH")
        if scope.site_ids is not None and str(item["id"]) not in scope.site_ids:
            if outside_scope == "skip":
                continue
            raise OperatorRefused("CPMS_SCOPE_MISMATCH")
        selected = {key: str(item.get(key) or "") for key in ("id", "name", "address")}
        if (
            not isinstance(item.get("name"), str)
            or not isinstance(item.get("address"), (str, type(None)))
            or not selected["name"]
            or any(len(v) > 1000 for v in selected.values())
        ):
            raise OperatorRefused("CPMS_INVALID_RESPONSE")
        rows.append(selected)
    return rows, data["total"]


def read_site_pages(scope, keywords, read, reauthorize, *, outside_scope="reject", max_pages=SITE_MAX_PAGES):
    """逐词、逐页读取站点并在每页前后复核权限指纹。"""
    rows = []
    seen = set()
    totals = []
    limited = False
    terms = keywords.address_queries() or (None,)
    if len(terms) > 4:
        raise OperatorRefused("OPERATOR_INVALID_SEARCH")
    if scope.site_ids != ():
        for term in terms:
            total = 0
            for page in range(1, max_pages + 1):
                if reauthorize() != scope.fingerprint:
                    raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
                body = read(SITE_PATH + "?" + urlencode(keywords.params(page, address=term)), scope.tenant_id)
                page_rows, total = site_page(scope, body, page, outside_scope=outside_scope)
                for row in page_rows:
                    if row["id"] not in seen and keywords.matches(row):
                        seen.add(row["id"])
                        rows.append(row)
                more = page * SITE_PAGE_SIZE < total
                if not more or not body["data"]["records"]:
                    limited = limited or more
                    break
                if page == max_pages:
                    limited = True
            totals.append(total)
    if reauthorize() != scope.fingerprint:
        raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
    total = sum(totals) if limited else len(rows)
    if len(terms) == 1:
        total = max(totals, default=0)
    return rows, total, limited


def query_sites(scope: SiteScope, keywords: SiteKeywords, read, reauthorize, *, outside_scope="reject"):
    rows, total, limited = read_site_pages(scope, keywords, read, reauthorize, outside_scope=outside_scope)
    return {
        "source": "cpms.sitePage",
        "observed_at": format_stored_time(now_utc()),
        "sites": rows[:SITE_MAX_SHOWN],
        "limited": limited or len(rows) > SITE_MAX_SHOWN,
        "empty_scope": scope.site_ids == (),
        "total": total,
        "scope_limited": scope.site_ids is not None,
    }


def tool_site_keywords(arguments):
    terms = arguments.get("address_terms")
    if terms is None:
        terms = [arguments["address"]] if arguments.get("address") else []
    return SiteKeywords(arguments.get("name", ""), tuple(terms))


def site_tool_result(scope, body, tool, arguments, *, outside_scope="reject"):
    """裁剪站点证据至声明字段，同时保留节点所需的分页回显与租户身份。"""
    rows, total = site_page(scope, body, 1, outside_scope=outside_scope)
    keywords = tool_site_keywords(arguments)
    rows = [row for row in rows if keywords.matches(row)]
    declared = operator_tools.tool(tool)
    data = {
        "records": [{key: row[key] for key in declared.evidence_fields} for row in rows],
        "total": total,
        "sample_limit": declared.page_size,
        "truncated": total > len(rows),
        "ordering_guaranteed": declared.flags["ordering_guaranteed"],
        "complete_timeline": declared.flags["complete_timeline"],
        "samples_returned": len(rows),
        "absence": declared.flags["absence_when_empty"] if not rows else None,
    }
    response = {
        "code": 0,
        "data": {
            "records": [dict(row, tenantId=scope.tenant_id) for row in data["records"]],
            "total": total,
            "current": 1,
            "size": declared.page_size,
        },
    }
    return data, response


def read_site_tool(scope, tool, arguments, read, reauthorize, *, outside_scope="reject"):
    keywords = tool_site_keywords(arguments)
    rows, total, _ = read_site_pages(
        scope,
        keywords,
        read,
        reauthorize,
        outside_scope=outside_scope,
        max_pages=1,
    )
    body = {
        "code": 0,
        "data": {
            "records": [dict(row, tenantId=scope.tenant_id) for row in rows[:SITE_PAGE_SIZE]],
            "total": total,
            "current": 1,
            "size": SITE_PAGE_SIZE,
        },
    }
    return site_tool_result(scope, body, tool, arguments, outside_scope=outside_scope)
