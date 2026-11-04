"""统一管家中的站点取证：范围、分页与最小字段不依赖回复措辞。"""

from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest

from aiops_diagnostics.identity.scope_context import DataScope
from aiops_diagnostics.operator import operator_cpms
from operations.test_operator_cpms import environment, tenant_owner


@pytest.fixture
def operator(tmp_path, monkeypatch):
    with environment(tmp_path, monkeypatch) as f:
        yield f


def selection():
    return {"kind": "site_search", "text": "", "name": "", "address_terms": ["广州", "天河"]}


def drive_site_tool(f, run_id, arguments=None):
    from operations.test_operator_agent import call_tool

    evidence, headers, publication = call_tool(
        f, run_id, "cpms_site_search", arguments or {"address_terms": []}
    )
    return evidence, headers


def site_query(caller, value, *, auth=None):
    """站点查询的调用壳：可见范围、关键词、读取函数、逐页复核各一处。"""
    return operator_cpms.query_sites(
        operator_cpms.site_scope(caller),
        operator_cpms.site_keywords(value),
        operator_cpms.cpms_site_reader("https://cpms.test", "private"),
        auth or (lambda: caller.scope_fingerprint),
    )


def sites():
    return [
        {
            "id": "SITE-1",
            "tenantId": "TENANT-A",
            "name": "天河测试站",
            "address": "广州市天河区测试路",
            "contactPhone": "PRIVATE-PHONE",
        },
    ]


def test_site_results_are_real_scoped_and_minimized(operator, monkeypatch):
    f = operator
    calls = []

    def cpms(url, bearer, tenant, *, timeout):
        assert timeout == 3
        calls.append((url, bearer, tenant))
        return {"code": 0, "data": {"records": sites(), "current": 1, "size": 50, "total": 1}}

    monkeypatch.setattr(operator_cpms, "cpms_get", cpms)
    caller = f["resolver"].current
    value = selection()
    result = site_query(caller, value)
    assert result["sites"] == [{"id": "SITE-1", "name": "天河测试站", "address": "广州市天河区测试路"}]
    assert parse_qs(urlsplit(calls[0][0]).query) == {
        "current": ["1"],
        "size": ["50"],
        "type": ["0"],
        "address": ["广州"],
    }
    assert calls[0][1:] == ("private", "TENANT-A")


@pytest.mark.parametrize(
    "fault",
    ["tenant", "site", "missing-id", "missing-tenant", "wrong-page", "oversized", "invalid-body", "revoked"],
)
def test_site_search_rejects_bad_evidence(operator, monkeypatch, fault):
    caller = operator["resolver"].current
    rows = sites()
    data = {"records": rows, "current": 1, "size": 50, "total": 1}
    if fault == "tenant":
        rows[0]["tenantId"] = "OTHER"
    if fault == "site":
        rows[0]["id"] = "SITE-OTHER"
    if fault.startswith("missing-"):
        rows[0].pop("id" if fault == "missing-id" else "tenantId")
    if fault == "wrong-page":
        data["current"] = 2
    if fault == "oversized":
        data["records"] = rows * 51
    calls = []

    def cpms(*_, **kwargs):
        calls.append(True)
        return [] if fault == "invalid-body" else {"code": 0, "data": data}

    monkeypatch.setattr(operator_cpms, "cpms_get", cpms)

    def auth():
        return (
            replace(caller, data_scope=DataScope(type="organ", site_ids=("OTHER",)), scope_fingerprint="")
            if fault == "revoked" and calls
            else caller
        )

    with pytest.raises(operator_cpms.OperatorRefused):
        site_query(caller, selection(), auth=lambda: auth().scope_fingerprint)


def test_empty_scope_never_calls_cpms(operator, monkeypatch):
    caller = replace(operator["resolver"].current, data_scope=DataScope(type="organ"), scope_fingerprint="")
    monkeypatch.setattr(operator_cpms, "cpms_get", lambda *_: pytest.fail("empty scope read"))
    result = site_query(caller, selection())
    assert result["empty_scope"] and not result["sites"]


def test_unbounded_scope_is_not_filtered_and_says_so_in_the_count(operator, monkeypatch):
    """顶层账号没有站点集合：不按站点收窄，但计数口径必须标成租户口径。

    ``site_ids is None`` 与 ``site_ids == ()`` 是两件相反的事 —— 前者"不过滤"，
    后者"什么都看不到"。混用就会让一个空范围的账号看见整个租户。
    """
    caller = tenant_owner()
    seen = []

    def cpms(url, bearer, tenant, *, timeout):
        page = int(parse_qs(urlsplit(url).query)["current"][0])
        seen.append((url, tenant))
        # 一页 50 条、total=152 ⇒ 会翻到第 2 页；分页回显不匹配就是 CPMS_INVALID_RESPONSE。
        return {
            "code": 0,
            "data": {"records": sites() if page == 1 else [], "current": page, "size": 50, "total": 152},
        }

    monkeypatch.setattr(operator_cpms, "cpms_get", cpms)
    # **单词**查询：`total` 才等于 CPMS 的计数。多词是逐词各查一轮，同一个站会被
    # 数两遍，那时不报 total（见 `query_sites` 的注释）—— 所以这里用单词问法。
    single = {"kind": "site_search", "text": "", "name": "", "address_terms": ["天河"]}
    result = site_query(caller, single)
    assert result["empty_scope"] is False
    assert result["scope_limited"] is False, "顶层账号的站点集合是 None，不是被站点集合收窄"
    assert result["total"] == 152 and len(result["sites"]) == 1
    assert seen[0][1] == "TENANT-A"


def test_a_search_without_a_match_is_empty_not_an_error(operator, monkeypatch):
    """搜索没命中是"0 条结果"，不是"查不到"；计数仍来自 CPMS 自己的 total。"""
    caller = operator["resolver"].current
    monkeypatch.setattr(
        operator_cpms,
        "cpms_get",
        lambda *_a, **_k: {
            "code": 0,
            "data": {"records": sites(), "current": 1, "size": 50, "total": 7},
        },
    )
    result = site_query(caller, {"kind": "site_search", "text": "", "name": "", "address_terms": ["哈尔滨"]})
    assert result["sites"] == []
    assert result["empty_scope"] is False and result["total"] == 7


def test_page_budget_is_bounded_and_not_claimed_complete(operator, monkeypatch):
    caller = operator["resolver"].current
    calls = []

    def cpms(url, *_, **kwargs):
        page = int(parse_qs(urlsplit(url).query)["current"][0])
        calls.append(page)
        return {"code": 0, "data": {"records": sites(), "current": page, "size": 50, "total": 10000}}

    monkeypatch.setattr(operator_cpms, "cpms_get", cpms)
    # 单词问法：分页预算就是 `SITE_MAX_PAGES` 页，不多翻。
    single = {"kind": "site_search", "text": "", "name": "", "address_terms": ["天河"]}
    result = site_query(caller, single)
    assert calls == [1, 2, 3, 4] and result["limited"]
    assert len(result["sites"]) == 1


def test_each_address_term_gets_its_own_page_budget(operator, monkeypatch):
    """多词查询是**逐词**翻页，不是共享一份页预算。

    上游 `address` 是子串匹配：「广州天河」整串查不到，「天河」能查到。所以每个词
    都要自己翻页 —— 否则第二个词还没开始就被第一个词用光预算。
    代价是请求数 = 词数 × 页数，所以词数受协议上限（至多 4 个）约束。
    """
    caller = operator["resolver"].current
    calls = []

    def cpms(url, *_, **kwargs):
        query = parse_qs(urlsplit(url).query)
        calls.append((query.get("address", [""])[0], int(query["current"][0])))
        return {
            "code": 0,
            "data": {"records": [], "current": int(query["current"][0]), "size": 50, "total": 0},
        }

    monkeypatch.setattr(operator_cpms, "cpms_get", cpms)
    site_query(caller, selection())  # ["广州", "天河"]
    addresses = {address for address, _ in calls}
    assert addresses == {"广州", "天河"}, f"两个词都要被查询到，实际：{calls}"
