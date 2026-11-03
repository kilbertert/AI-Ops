"""非订单提问必须经过现役节点，但不能取得订单工具。"""

import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from aiops_diagnostics.identity.scope_context import DataScope
from aiops_diagnostics.operator import operator_conversation as conversation
from aiops_diagnostics.operator import operator_cpms, operator_tools
from operations.test_operator_cpms import (
    APP,
    HEADERS,
    NODE_A_TOKEN,
    environment,
    tenant_owner,
    terminal,
)


@pytest.fixture
def operator(tmp_path, monkeypatch):
    with environment(tmp_path, monkeypatch) as f:
        yield f


@pytest.mark.parametrize(
    "operation, order_no, expected",
    [
        (operator_cpms.SCENARIO, "ORDER-1001", True),
        # 有订单号、类型却是无订单回合：判据必须看类型，不能看订单号。
        (conversation.SCENARIO, "ORDER-1001", False),
        (conversation.SCENARIO, "", False),
        # 没有订单号、类型是订单诊断：同样只看类型。
        (operator_cpms.SCENARIO, "", True),
    ],
)
def test_run_type_decides_not_the_presence_of_an_order_number(operation, order_no, expected):
    """「这次跑的是什么」是**运行类型**说的，不是订单号是否为空说的。

    这两个字段今天恰好一致——签发时就是按有没有订单二选一的——所以拿订单号当判据
    现在也能跑通。这一步就是为了让那条路变红：岗哨写成 ``if run.order_no`` 时，
    上表第 2、4 行会分别错成 True 与 False。
    """
    run = SimpleNamespace(operation=operation, order_no=order_no)
    assert operator_cpms.diagnoses_an_order(run) is expected


def test_no_order_question_reaches_node_without_business_reads(operator, monkeypatch):
    f = operator

    def dispatch(_, config, run_id):
        headers = {"Authorization": "Bearer " + NODE_A_TOKEN}
        publication = f["client"].post(
            "/v1/internal/charging-status/execution-config/" + run_id, headers=headers
        )
        assert publication.status_code == 200, publication.text
        manifest = json.loads(publication.json()["manifest"])
        # 无订单回合可以带工具，但只能是**声明里允许这类运行用的**那些（ADR-0182）。
        assert [tool["name"] for tool in manifest["tools"]] == ["cpms_site_search"]
        assert manifest["tools"][0]["input_schema"] == operator_tools.tool("cpms_site_search").input_schema()
        headers["X-AI-Ops-Exec-Config"] = publication.json()["sha256"]
        response = f["client"].post(
            f"/v1/internal/operator-runs/{run_id}/completions",
            headers=headers,
            json={
                "status": "completed",
                "answer": json.dumps(
                    {"kind": "answer", "text": "状态3表示异常已处理。", "name": "", "address_terms": []}
                ),
            },
        )
        assert response.status_code == 200, response.text

    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
    result = f["client"].post(
        "/v1/assistant/questions", headers=HEADERS, json={"question": "订单状态3是什么意思"}
    )
    assert result.status_code == 202, result.text
    final = terminal(f, result.json()["qa_id"])
    assert final.json()["status"] == "completed", final.text
    assert "异常已处理" in final.text
    assert not f["state"]["calls"]


def selection():
    return {"kind": "site_search", "text": "", "name": "", "address_terms": ["广州", "天河"]}


@pytest.mark.parametrize("text", ["不能直接执行退款、重启设备或修改配置。", "我没有执行退款。"])
def test_explaining_read_only_capability_is_not_an_action_claim(text):
    conversation.validate({"kind": "answer", "text": text, "name": "", "address_terms": []}, "你能做什么？")


@pytest.mark.parametrize("text", ["已执行退款。", "不能重启，但已执行退款。"])
def test_a_real_action_claim_is_still_rejected(text):
    with pytest.raises(conversation.OperatorRefused):
        conversation.validate(
            {"kind": "answer", "text": text, "name": "", "address_terms": []}, "你能做什么？"
        )


def test_generic_site_reference_is_not_a_fabricated_name():
    conversation.validate_site_note("查到的站点结果已列出，您可以核对是否为目标站点。")
    with pytest.raises(conversation.OperatorRefused):
        conversation.validate_site_note("已找到虚构乙站。")


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
    conversation.validate(value, "帮我找一下广州天河附近的充电站")
    result = site_query(caller, value)
    assert result["sites"] == [{"id": "SITE-1", "name": "天河测试站", "address": "广州市天河区测试路"}]
    assert parse_qs(urlsplit(calls[0][0]).query) == {
        "current": ["1"],
        "size": ["50"],
        "type": ["0"],
        "address": ["广州"],
    }
    assert calls[0][1:] == ("private", "TENANT-A")
    text = conversation.render(value, result, "zh")["blocks"][0]["text"]
    assert "天河测试站" in text and "未按距离排序" in text and "PRIVATE" not in text


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
    assert "尚未分配" in conversation.render(selection(), result, "zh")["blocks"][0]["text"]


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
    result = site_query(caller, selection())
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
    result = site_query(caller, selection())
    assert calls == [1, 2, 3, 4] and result["limited"]
    assert len(result["sites"]) == 1


@pytest.mark.parametrize(
    "value",
    [
        {"kind": "site_search", "text": "", "name": "", "address_terms": ["不存在的地点"]},
        {"kind": "site_search", "text": "", "name": "", "address_terms": ["%"]},
        {"kind": [], "text": "", "name": "", "address_terms": []},
    ],
)
def test_model_search_cannot_invent_conditions_or_results(value):
    with pytest.raises(operator_cpms.OperatorRefused):
        conversation.validate(value, "找广州天河充电站%")


def test_asking_what_sites_i_have_is_a_roster_not_an_error():
    """「我当前有哪些站点？」四个字段全空，是**列举**，不是非法搜索。

    这里曾经要求关键词非空，于是这个最自然的问题被判 ``OPERATOR_INVALID_SEARCH``，
    外面再包成 ``OPERATOR_DIAGNOSIS_FAILED`` —— 用户看到"诊断失败"，而它既不是诊断，
    也不是失败。关键词只用来防模型编造搜索条件；空关键词什么都没编。
    """
    answer = {"kind": "site_search", "text": "", "name": "", "address_terms": []}
    conversation.validate(answer, "我当前有哪些站点？")
    assert answer["text"] == ""


@pytest.mark.parametrize(
    "sites, needle, forbidden",
    [
        (
            {"total": 152, "scope_limited": False, "sites": [{"name": "站A", "address": "地址A"}]},
            "共 152 个",
            None,
        ),
        (
            {"total": 152, "scope_limited": True, "sites": [{"name": "站A", "address": "地址A"}]},
            "本次读取到 1 个",
            "共 152",
        ),
        (
            {"total": 1, "scope_limited": False, "sites": [{"name": "站A", "address": "地址A"}]},
            "站A",
            "共 1 个",
        ),
        # 数字看着"对得上"也必须不信：员工看到 1 条、CPMS 说 total=1，那 1 是**租户**
        # 的计数，不是他这 1 个站点。只有断言"不能出现总数"能区分这两句文案。
        (
            {"total": 1, "scope_limited": True, "sites": [{"name": "站A", "address": "地址A"}]},
            "本次读取到 1 个",
            "共 1",
        ),
    ],
)
def test_listing_reports_the_count_only_when_it_is_trustworthy(sites, needle, forbidden):
    """列举要回答"我有多少个"；员工的 total 是租户口径，不能冒充他自己的站点数。"""
    roster = {
        "source": "cpms.sitePage",
        "observed_at": "2026-10-29T00:00:00+00:00",
        # 直查已经读完了：这里的 total 与本地条数不等纯粹是租户口径/集合过滤造成的。
        # （工具路径只有一页，``limited`` 会另走「另有 N 个」。）
        "limited": False,
        "empty_scope": False,
        **sites,
    }
    text = conversation.render(
        {"kind": "site_search", "text": "", "name": "", "address_terms": []}, roster, "zh-CN"
    )["blocks"][0]["text"]
    assert needle in text
    if forbidden:
        assert forbidden not in text


def test_rendering_never_lists_more_than_the_show_limit():
    """工具栏的证据是**一整页 50 条**，展示上限 10 必须在这里收口。

    2026-11-02 复审实测：上游忽略关键词、返回权限内的 11 条，render 直接 enumerate
    ``sites["sites"]``，于是「共 1 个站点」下面列了 11 行 —— 两个数字彼此矛盾。
    """
    limit = operator_cpms.SITE_MAX_SHOWN
    rows = [{"name": f"站{i}", "address": "地址"} for i in range(1, limit + 2)]
    roster = {
        "source": "cpms.sitePage",
        "observed_at": "2026-10-29T00:00:00+00:00",
        "sites": rows,
        "total": len(rows),
        "limited": True,
        "empty_scope": False,
        "scope_limited": True,
    }
    text = conversation.render(
        {"kind": "site_search", "text": "", "name": "", "address_terms": []}, roster, "zh-CN"
    )["blocks"][0]["text"]
    assert len(re.findall(r"(?m)^\d+\.", text)) == limit
    assert f"另有 {len(rows) - limit} 个" in text
    assert conversation.site_facts(roster, "zh-CN")["site_count"] == str(limit)


@pytest.mark.parametrize("limited", [False, True])
def test_the_note_and_the_body_state_the_same_number(limited):
    """同一段答复里不能有两个总数。

    2026-11-02 复审：``site_facts`` 用的是展示条数，``render`` 用的是 total，
    于是「共 10 个站点」紧跟着「共 500 个站点」。两者的判定必须同源，
    不论这一页是否读完。
    """
    rows = [{"name": f"站{i}", "address": "地址"} for i in range(1, 11)]
    roster = {
        "source": "cpms.sitePage",
        "observed_at": "2026-11-02T00:00:00+00:00",
        "sites": rows,
        "total": 500,
        "limited": limited,
        "empty_scope": False,
        "scope_limited": False,
    }
    answer = {"kind": "site_search", "text": "共 {{fact:site_count}} 个站点", "name": "", "address_terms": []}
    text = conversation.render(answer, roster, "zh-CN")["blocks"][0]["text"]
    note = text.split("\n\n")[0]
    body = text.split("\n\n", 1)[1]
    numbers = {n for n in re.findall(r"共 (\d+) 个", note)}
    assert len(numbers) == 1
    stated = int(re.search(r"共 (\d+) 个", note).group(1))
    body_numbers = set(re.findall(r"共 (\d+) 个", body))
    assert body_numbers <= {str(stated)}, text  # 正文里出现的总数必须与说明一致
    shown = len(re.findall(r"(?m)^\d+\.", text))
    more = re.search(r"另有 (\d+) 个(?:匹配)?站点", text)
    assert shown + (int(more.group(1)) if more else 0) == stated, text


@pytest.mark.parametrize("limited", [False, True])
def test_a_keyword_search_never_states_the_upstream_total_as_its_own_count(limited):
    """搜索问法下 ``total`` 是**过滤前**的上游计数，不能当成「匹配到多少」。

    2026-11-02 复审：工具证据的 total 未受关键词影响，于是「共 500 个」会被读成
    「天河有 500 个站」。搜索问法只能说本地真正握有的行数。
    """
    rows = [{"name": f"天河站{i}", "address": "广州天河"} for i in range(1, 4)]
    roster = {
        "source": "cpms.sitePage",
        "observed_at": "2026-11-02T00:00:00+00:00",
        "sites": rows,
        "total": 500,
        "limited": limited,
        "empty_scope": False,
        "scope_limited": False,
    }
    answer = {"kind": "site_search", "text": "", "name": "天河", "address_terms": []}
    text = conversation.render(answer, roster, "zh-CN")["blocks"][0]["text"]
    assert "500" not in text
    assert conversation.site_facts(roster, "zh-CN", listing=False)["site_count"] == str(len(rows))
    # 列举问法读全了，total 才是答案；没读全就只说展示条数。
    expected = "500" if not limited else str(len(rows))
    assert conversation.site_facts(roster, "zh-CN", listing=True)["site_count"] == expected


def test_a_partial_read_never_claims_a_first_n_of_a_complete_list():
    """只读了一页时说「显示前 N 个」是在承诺一个我们没有的顺序。

    41 上 total=153 是常态（`docs/validation/2026-10-29-site-roster-live.md`），
    而我们一次只读一页。「前 N 个」意味着「按某种顺序的最前面几条，其余也可知」；
    只读到 10 行时必须说「本次读取到 N 个」并把「可能还有更多」交给既有提示。
    读全且超出展示上限时，「前 N 个」才是准的。
    """
    partial = {
        "source": "cpms.sitePage",
        "observed_at": "2026-11-02T00:00:00+00:00",
        "sites": [{"name": f"站{i}", "address": "地址"} for i in range(10)],
        "total": 153,
        "limited": True,
        "empty_scope": False,
        "scope_limited": False,
    }
    listing = {"kind": "site_search", "text": "", "name": "", "address_terms": []}
    text = conversation.render(listing, partial, "zh-CN")["blocks"][0]["text"]
    assert "本次读取到 10 个" in text
    assert "前 10 个" not in text
    assert "可能不完整" in text

    complete = {
        **partial,
        "sites": [{"name": f"站{i}", "address": "地址"} for i in range(12)],
        "total": 12,
        "limited": False,
    }
    text = conversation.render(listing, complete, "zh-CN")["blocks"][0]["text"]
    assert "共 12 个站点，这里显示前 10 个" in text  # 12 行都在手里，这句话是准的


def test_tool_evidence_page_of_many_rows_shows_ten_and_says_how_many_more():
    """工具栏取回的是**一整页**：11 行不突破展示上限，也不能说成「查询不完整」。

    关键词既可能在上游被忽略、又能在本地过滤掉一部分，于是「权限内 11 条全命中」是
    一个真实形状。它必须走「另有 1 个未列出」并给出确定的条数，而不是「达到上限，
    结果可能不完整」——后者是查询没取全的意思，事实不是那样。
    """
    limit = operator_cpms.SITE_MAX_SHOWN
    rows = [{"id": f"SITE-{i}", "name": f"天河站{i}", "address": "广州天河"} for i in range(1, limit + 2)]
    sites = operator_cpms._sites_from_evidence(
        tenant_owner(),
        SimpleNamespace(
            delivered={operator_cpms.SITE_TOOL},
            arguments={operator_cpms.SITE_TOOL: {"name": "天河"}},
            evidence={
                operator_cpms.SITE_TOOL: {
                    "source": "cpms.sitePage",
                    "observed_at": "2026-11-02T00:00:00+00:00",
                    "data": {"records": rows, "total": len(rows), "truncated": False},
                }
            },
        ),
        {"kind": "site_search", "text": "", "name": "天河", "address_terms": []},
    )
    text = conversation.render(
        {"kind": "site_search", "text": "", "name": "天河", "address_terms": []}, sites, "zh-CN"
    )["blocks"][0]["text"]
    assert len(re.findall(r"(?m)^\d+\.", text)) == limit
    assert f"另有 {len(rows) - limit} 个" in text
    assert "不完整" not in text  # 整页 11 条是查全了的，展示上限不改变这一点。
    facts = conversation.site_facts(sites, "zh-CN")
    assert len(re.findall(r"(?m)^\d+\.", facts["site_roster"])) == limit


def test_a_search_that_finds_nothing_still_says_try_another_place():
    """搜索没命中与列举为空是两件事，提示不能混用。"""
    empty = {
        "source": "cpms.sitePage",
        "observed_at": "2026-10-29T00:00:00+00:00",
        "limited": False,
        "empty_scope": False,
        "sites": [],
        "total": 0,
        "scope_limited": False,
    }
    searched = conversation.render(selection(), empty, "zh-CN")["blocks"][0]["text"]
    listed = conversation.render(
        {"kind": "site_search", "text": "", "name": "", "address_terms": []}, empty, "zh-CN"
    )["blocks"][0]["text"]
    assert "未找到匹配站点" in searched
    assert "没有站点" in listed and "未找到匹配" not in listed


def test_a_tool_page_that_matched_nothing_says_the_search_may_be_incomplete():
    """工具只读第一页：本页没命中不等于范围里没有。

    这是审查指出的真实缺口——「第一页无匹配、后续页可能有匹配」时，答复必须带着
    上限提示，不能写成确定的「没有这个站点」。它也不是列举空的「你没有任何站点」。
    """
    empty = {
        "source": "cpms.sitePage",
        "observed_at": "2026-11-02T00:00:00+00:00",
        "limited": True,  # 上游还有剩余页／本页被关键词过滤掉了
        "empty_scope": False,
        "sites": [],
        "total": 152,
        "scope_limited": False,
    }
    text = conversation.render(selection(), empty, "zh-CN")["blocks"][0]["text"]
    assert "未找到匹配站点" in text
    assert "可能不完整" in text
    assert "没有站点" not in text


@pytest.mark.parametrize("prose", ["为您查询充电站。", "编造站点：恶意站；已退款。"])
def test_a_site_note_written_before_the_query_is_refused(operator, monkeypatch, prose):
    """没有取数依据的找站说明**不能**被当成查询结果交付。

    这里的行为在 2026-10-30 从"静默丢掉模型的话、照常交付站点"改成了"整轮失败"。理由：
    静默丢掉会让写提示词的人以为那段话在生效，而它一次也没有出现过；失败会明确说出
    原因（`OPERATOR_SITE_NOTE_WITHOUT_EVIDENCE`）。**没有任何文字因此被交付**——这一条
    比改动前更严，不是更松。

    说明写在工具结果之后的情形见 `test_a_site_note_leads_and_the_facts_follow`。
    """
    f = operator
    answer = {**selection(), "text": prose}
    monkeypatch.setattr(
        operator_cpms.SnailDispatcher, "dispatch", lambda _, config, run: complete(f, run, answer)
    )
    response = f["client"].post(
        "/v1/assistant/questions", headers=HEADERS, json={"question": "找广州天河充电站"}
    )
    assert response.status_code == 202, response.text
    final = terminal(f, response.json()["qa_id"])
    assert final.json()["status"] == "failed", final.text
    assert "天河测试站" not in final.text
    assert "恶意站" not in final.text and "已退款" not in final.text


def test_a_site_note_leads_and_the_facts_follow():
    """说明负责语气，事实由系统按证据写：名单只有一份，来源与限制都在它下面。"""
    value = {**selection(), "text": "按您当前账号可见范围，我查到的站点在下面。"}
    conversation.validate(value, "找广州天河充电站")
    result = site_query_for(value)
    text = conversation.render(value, result, "zh")["blocks"][0]["text"]
    assert text.startswith("按您当前账号可见范围")
    assert text.index("天河测试站") > text.index("按您当前账号可见范围")
    assert "广州市天河区测试路" in text and "未按距离排序" in text


def site_query_for(value):
    """按当前账号可见范围真查一次（CPMS 用测试桩）。"""
    rows = sites()

    def reader(path, tenant):
        return {"code": 0, "data": {"records": rows, "current": 1, "size": 50, "total": 1}}

    return operator_cpms.query_sites(
        operator_cpms.SiteScope(tenant_id="TENANT-A", site_ids=None, fingerprint="f"),
        operator_cpms.site_keywords(value),
        reader,
        lambda: "f",
    )


@pytest.mark.parametrize(
    "note",
    [
        "附近有 3 个站。",  # 数量：只能由系统写
        "都在 5 公里以内。",  # 距离：没有定位就不成立
        "每度电 1.2 元，有空闲枪口。",  # 价格与可用性：本轮没查
    ],
)
def test_a_site_note_cannot_state_facts(note):
    value = {**selection(), "text": note}
    with pytest.raises(operator_cpms.OperatorRefused):
        conversation.validate(value, "找广州天河充电站")


def test_a_site_note_cannot_retype_a_site_name():
    """站名只能出现在系统列出的清单里：**任何**站名形状都拒，不只是已知的那些。

    只比对已知站名会放行编造的名字（复核反例 `我帮你找到了虚构乙站`）。
    """
    value = {**selection(), "text": "为您查询天河测试站。"}
    with pytest.raises(operator_cpms.OperatorRefused):
        conversation.validate(value, "找广州天河充电站")
    invented = {**selection(), "text": "我帮你找到了虚构乙站。"}
    with pytest.raises(operator_cpms.OperatorRefused):
        conversation.validate(invented, "找广州天河充电站")


@pytest.mark.parametrize(
    "note",
    [
        # 2026-11-02 真实回放里被误拒的三句，逐字取自模型输出。
        "我帮你查了一下，当前账号可见的地点列表已经列在下方。由于清单较长，如需进一步筛选或定位，"
        "可以告诉我站点名称或城市关键词，我再帮你缩小范围。",
        "我帮你查了一下，系统已列出当前账号可见的结果。你可以核对一下，如需按地点或站名筛选，也可以告诉我。",
        "我帮你查了一下，当前账号可见的地点列表在下方。结果较多，系统会分批展示；如需缩小范围，"
        "可以告诉我地点关键词，或直接选择某个具体地点查看详情。",
        "如需查看站点详情，请核对站点列表。",
        "Your stations are listed below.",
        "The station list is below.",
    ],
)
def test_a_generic_site_noun_is_not_a_fabricated_name(note):
    """「站点名称」「站点详情」「Your stations」是**类别**，不是给某个地方起名。

    站点说明的职责是追问筛选条件，说一句「可以告诉我站点名称」是它的正常写法。把它判成
    编造站名，等于让每轮站点问答有概率整轮失败 —— 2026-11-02 真实回放 18 次里就有 3 次
    是这么被拒的（`rule=note_site_name`）。收窄形状后，编造站名仍被拒（见上一条测试）。
    """
    conversation.validate_site_note(note)


@pytest.mark.parametrize(
    "note", ["我帮你找到了虚构乙站详情。", "天河测试站信息如下。", "为您查询天河测试站。"]
)
def test_a_generic_suffix_does_not_launder_a_fabricated_name(note):
    """通用短语只**整条**中性化。按后一个字跳过的写法会放行 `虚构乙站详情`（复审反例）。"""
    with pytest.raises(operator_cpms.OperatorRefused):
        conversation.validate_site_note(note)


def complete(f, run_id, answer):
    headers = {"Authorization": "Bearer " + NODE_A_TOKEN}
    publication = f["client"].post("/v1/internal/charging-status/execution-config/" + run_id, headers=headers)
    assert publication.status_code == 200, publication.text
    headers["X-AI-Ops-Exec-Config"] = publication.json()["sha256"]
    denied = f["client"].post(
        f"/v1/internal/operator-runs/{run_id}/tools/cpms_order_detail/authorization", headers=headers, json={}
    )
    assert denied.status_code == 403
    response = f["client"].post(
        f"/v1/internal/operator-runs/{run_id}/completions",
        headers=headers,
        json={"status": "completed", "answer": json.dumps(answer)},
    )
    assert response.status_code == 200, response.text


def drive_site_tool(f, run_id, arguments):
    """替身节点走**真实工具链**：领授权 → 按授权的请求目标取数 → 上报证据。

    不写死路径与证据形状：两者都来自授权回执，与 Java 节点做的事逐字相同。站点工具
    2026-11-02 之前在这条链上有两处断点（网关代理只认订单行、节点只认订单证据），
    只测「授权 200」看不见它们。
    """
    headers = {
        "Authorization": "Bearer " + NODE_A_TOKEN,
        "X-AI-Ops-Tool-Contract": operator_cpms.TOOL_CONTRACT,
    }
    publication = f["client"].post(
        "/v1/internal/charging-status/execution-config/" + run_id,
        headers={"Authorization": "Bearer " + NODE_A_TOKEN},
    )
    assert publication.status_code == 200, publication.text
    headers["X-AI-Ops-Exec-Config"] = publication.json()["sha256"]
    tool = operator_cpms.SITE_TOOL
    response = f["client"].post(
        f"/v1/internal/operator-runs/{run_id}/tools/{tool}/authorization",
        headers=headers,
        json={"arguments": arguments},
    )
    assert response.status_code == 200, response.text
    grant = response.json()
    request = grant["request"]
    url = grant["base_url"] + request["path"]
    if request["query"]:
        url += "?" + request["query"]
    response = f["client"].get(url, headers={"Authorization": "Bearer " + grant["bearer"]})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    evidence = {
        "tool": tool,
        "inputs": grant["inputs"],
        "source": "cpms.sitePage",
        "observed_at": datetime.now(UTC).isoformat(),
        "data": {
            "records": [
                {key: row.get(key, "") for key in ("id", "name", "address")} for row in data["records"]
            ],
            "total": data["total"],
            "sample_limit": 50,
            "truncated": data["total"] > len(data["records"]),
            "ordering_guaranteed": False,
            "complete_timeline": False,
            "samples_returned": len(data["records"]),
            "absence": None if data["records"] else "no_samples",
        },
    }
    response = f["client"].post(
        f"/v1/internal/operator-runs/{run_id}/tools/{tool}/evidence",
        headers=headers,
        json={"authorization_id": grant["authorization_id"], "evidence": evidence},
    )
    assert response.status_code == 200, response.text
    return evidence


@pytest.mark.parametrize("entry", ["formal", "trial", "native", "managed"])
def test_site_search_uses_same_scope_on_all_entries(operator, monkeypatch, entry):
    from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces

    f = operator
    monkeypatch.setattr(
        operator_cpms.SnailDispatcher, "dispatch", lambda _, config, run: complete(f, run, selection())
    )
    f["settings"].dify_trial_api_key = "trial-only"
    f["settings"].environment = "env-a"
    monkeypatch.setattr(
        "aiops_diagnostics.gateway.gateway_api.authenticated_operator_console",
        lambda *_: (f["resolver"].current, "operator-private"),
    )
    with DifyWorkspaces(f["db"])._connection(write=True) as db:
        db.execute(
            "INSERT INTO dify_workspaces "
            "(tenant_id,environment,workspace_id,setup_state,created_at,updated_at) "
            "VALUES (?,?,?,'ready',?,?)",
            ("TENANT-A", "env-a", "workspace", "2026-10-29", "2026-10-29"),
        )
    # 显式标注类型：这个请求体后面会被塞进非字符串字段（`app_id`、`operator_preview`）。
    body: dict[str, object] = {"question": "帮我找一下广州天河附近的充电站"}
    if entry == "managed":
        from aiops_diagnostics.execution import chat_execution

        original = chat_execution.binding_for_key

        def empty_trial_binding(settings, key):
            binding = original(settings, key)
            return replace(binding, manifest_text="") if binding else None

        monkeypatch.setattr(chat_execution, "binding_for_key", empty_trial_binding)
        monkeypatch.setattr(
            chat_execution, "select_binding", lambda *args, **kwargs: replace(f["route"], manifest_text="")
        )
    if entry == "formal":
        response = f["client"].post("/v1/assistant/questions", headers=HEADERS, json=body)
        assert response.status_code == 202, response.text
        final = terminal(f, response.json()["qa_id"])
    else:
        body["app_id"] = APP
        if entry == "native":
            body["operator_preview"] = {"pre_prompt": "无订单业务咨询", "tools": []}
        headers = {"Authorization": "Bearer trial-only", "X-AIOps-Console-Session": "session"}
        response = f["client"].post("/v1/dify/trial", headers=headers, json=body)
        assert response.status_code == 202, response.text
        terminal(f, response.json()["trial_id"])
        final = f["client"].get("/v1/dify/trial/" + response.json()["trial_id"], headers=headers)
    assert final.status_code == 200 and final.json()["status"] == "completed", final.text
    assert "天河测试站" in final.text and "PRIVATE-PHONE" not in final.text
    assert len(f["state"]["calls"]) == 1 and f["state"]["calls"][0][0].startswith("/chSite/page?")


def test_site_tool_end_to_end_through_the_real_tool_chain(operator, monkeypatch):
    """同一句原 H5 问题：授权 → 取数 → 证据 → 完成，最终正文来自本轮证据。

    这是「工具参数 → CPMS 过滤器 → 冻结证据 → 最终答复」的整条闭环。只断言授权 200
    会让下面两处断点继续躲着：
      * 网关代理按订单行校验（`siteId`/`orderNo`），站点页没有这些字段；
      * 节点只认订单形状的证据，站点页的证据它在本地就被拒。
    """
    f = operator
    f["state"]["site_address"] = "广州天河测试路"
    seen = []

    def dispatch(_, config, run_id):
        try:
            evidence = drive_site_tool(f, run_id, {"address": "广州 天河"})
            seen.append(evidence)
            # 与 `complete()` 同形，但自己持有响应：那个夹具只返回拒绝回执的 json。
            headers = {"Authorization": "Bearer " + NODE_A_TOKEN}
            publication = f["client"].post(
                "/v1/internal/charging-status/execution-config/" + run_id, headers=headers
            )
            headers["X-AI-Ops-Exec-Config"] = publication.json()["sha256"]
            status = f["client"].post(
                f"/v1/internal/operator-runs/{run_id}/completions",
                headers=headers,
                json={"status": "completed", "answer": json.dumps(selection())},
            )
            seen.append(("complete", status.status_code, status.text))
            return status
        except BaseException as exc:  # noqa: BLE001 - 夹具失败要能在断言里看见
            seen.append(("dispatch failed", repr(exc)))
            raise

    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
    response = f["client"].post(
        "/v1/assistant/questions", headers=HEADERS, json={"question": "帮我找一下广州天河附近的充电站"}
    )
    assert response.status_code == 202, response.text
    final = terminal(f, response.json()["qa_id"])
    assert final.json()["status"] == "completed", (final.text, seen)
    # 事实来自**这一份**证据：站点按工具参数里的地址查回来，不是第二次查询
    cpms_calls = [path for path, _ in f["state"]["calls"] if path.startswith("/chSite/page?")]
    assert len(cpms_calls) == 1
    assert "address=" + quote("广州天河") in cpms_calls[0]
    assert "天河测试站" in final.text


def test_cancel_during_site_read_cannot_deliver(operator, monkeypatch):
    f = operator

    def dispatch(_, config, run):
        qa = f["ctx"].agent_run_store.get(run).qa_id
        f["state"]["hook"] = lambda: f["client"].post(
            "/v1/assistant/questions/" + qa + "/cancel", headers=HEADERS
        )
        complete(f, run, selection())

    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
    response = f["client"].post(
        "/v1/assistant/questions", headers=HEADERS, json={"question": "找广州天河充电站"}
    )
    final = terminal(f, response.json()["qa_id"])
    assert final.json()["status"] != "completed" and not final.json().get("result")


def test_empty_scope_can_ask_general_questions(operator, monkeypatch):
    f = operator
    f["resolver"].current = replace(
        f["resolver"].current, data_scope=DataScope(type="organ"), scope_fingerprint=""
    )
    monkeypatch.setattr(
        operator_cpms.SnailDispatcher,
        "dispatch",
        lambda _, config, run: complete(
            f, run, {"kind": "answer", "text": "可以咨询充电问题。", "name": "", "address_terms": []}
        ),
    )
    response = f["client"].post("/v1/assistant/questions", headers=HEADERS, json={"question": "你能做什么"})
    assert response.status_code == 202, response.text
    assert terminal(f, response.json()["qa_id"]).json()["status"] == "completed"
    assert not f["state"]["calls"]


def test_site_name_with_date_is_not_an_order(operator, monkeypatch):
    f = operator
    monkeypatch.setattr(
        operator_cpms.SnailDispatcher,
        "dispatch",
        lambda _, config, run: complete(
            f, run, {"kind": "site_search", "text": "", "name": "TEST2026-10-29", "address_terms": []}
        ),
    )
    response = f["client"].post(
        "/v1/assistant/questions",
        headers=HEADERS,
        json={"question": "帮我查询充电站 TEST2026-10-29 03:00:09"},
    )
    assert response.status_code == 202, response.text
    assert terminal(f, response.json()["qa_id"]).json()["status"] == "completed"
    assert f["state"]["calls"] and all(p.startswith("/chSite/page?") for p, _ in f["state"]["calls"])


def _template_with_only_order_tools():
    """生产形状的模板：只有两个订单取证工具，没有站点工具。

    2026-11-01 生产事故的形状就是这份模板：`operator_conversation.manifest()` 按模板
    过滤后得到空表，而 INSTRUCTIONS 仍要求模型「先调用 `cpms_site_search`」，模型只能
    写字、网关按 `OPERATOR_SITE_NOTE_WITHOUT_EVIDENCE` 拒收，站点问题 100% 失败。
    """
    return json.dumps(
        {
            "schema": "operator-exec-config/1",
            "name": "operator-node",
            "history_policy": "none",
            "model": {},
            "model_options": {},
            "system_messages": ["应用业务背景：SOP"],
            "tools": [
                {"name": name, "description": "d", "input_schema": operator_tools.tool(name).input_schema()}
                for name in ("cpms_order_detail", "cpms_charging_samples")
            ],
        }
    )


def test_conversation_tools_come_from_declaration_not_from_template():
    """模板缺站点工具时，会话回合仍要下发它 —— 判据是 `applies_to`，不是模板。

    这条是上面那个生产事故的回归测试。夹具（`environment()`）用
    `operator_tools.names()` 造模板，三个工具都在，所以旧测试一直是绿的：
    夹具与判据同源，夹具就成了唯一事实来源。
    """
    manifest = json.loads(conversation.manifest(_template_with_only_order_tools()))
    names = [tool["name"] for tool in manifest["tools"]]
    assert names == ["cpms_site_search"]
    assert manifest["tools"][0]["input_schema"] == operator_tools.tool("cpms_site_search").input_schema()
    # 订单工具仍然不下发：无订单回合用不上它们。
    assert "cpms_order_detail" not in names
    assert "cpms_charging_samples" not in names


def test_template_site_tool_is_not_duplicated():
    """模板已经列了站点工具时，不因为按声明补齐而重复一条。"""
    body = json.loads(_template_with_only_order_tools())
    body["tools"].append(
        {
            "name": "cpms_site_search",
            "description": "from-template",
            "input_schema": operator_tools.tool("cpms_site_search").input_schema(),
        }
    )
    manifest = json.loads(conversation.manifest(json.dumps(body)))
    names = [tool["name"] for tool in manifest["tools"]]
    assert names == ["cpms_site_search"]
    # 模板那一条优先保留（描述沿用模板），schema 仍以声明为准。
    assert manifest["tools"][0]["description"] == "from-template"


@pytest.mark.parametrize(
    "text, rule",
    [
        ("私有内容" * 101, "note_length"),
        ("私有内容{{fact:secret}}", "note_slot"),
        ("私有内容7", "note_fact"),
        ("私有虚构乙站", "note_site_name"),
        ("私有内容已执行退款", "note_mutation"),
    ],
)
def test_site_note_rejection_logs_only_the_rule(caplog, text, rule):
    with pytest.raises(conversation.OperatorRefused, match="OPERATOR_INVALID_ANSWER"):
        conversation.validate_site_note(text)
    assert "operator_conversation_rejected rule=" + rule in caplog.text
    assert "私有" not in caplog.text and "secret" not in caplog.text


def test_conversation_rejection_logs_shape_without_answer(caplog):
    with pytest.raises(conversation.OperatorRefused, match="OPERATOR_INVALID_ANSWER"):
        conversation.validate({"private": "secret"}, "私有问题")
    assert "rule=answer_shape" in caplog.text
    assert "secret" not in caplog.text and "私有" not in caplog.text


# ─────────────────────── 2026-11-03：上游不吐 JSON 的三道防线
#
# 实测（41 生产，每组 25 次）：`response_format=json_object` 对供应商只是**提示**，
# 带工具轮次的最后一轮有 36–44% 的响应是通篇正文。三件事一起做：
#   ① 把「只输出 JSON」写进节点看到的那条用户消息 → 降到 8%
#   ② 上游失约时有界重派一次（最多一次）
#   ③ 节点的失败原因码不再被网关丢掉


def test_the_node_question_carries_the_json_only_reminder(operator, monkeypatch):
    """提醒必须真的到达模型看得到的那条消息里 —— 写在别处等于没写。"""
    f = operator
    seen = {}

    # 注意读的是**实例**：`question` 是类属性，实例赋值只落在实例上，
    # 读类属性会拿回那句固定的默认提问，测试就永远看不到真正发出去的内容。
    def dispatch(self, config, run_id):
        seen["question"] = self.question
        raise RuntimeError("stop here")

    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
    posted = f["client"].post(
        "/v1/assistant/questions", headers=HEADERS, json={"question": "订单状态3是什么意思"}
    )
    terminal(f, posted.json()["qa_id"])
    assert "最后一步只输出那个JSON对象本身" in seen["question"]
    assert "第一个字符必须是 {" in seen["question"]


def test_a_non_json_answer_is_redispatched_at_most_once(operator, monkeypatch, caplog):
    """上游失约 → 重派一次；**再失约就按失败交付**，不无限重试。

    **假派发必须照抄生产的行为**：节点拒绝回执后，平台那次调用是**失败**的，
    `SnailDispatcher` 把它映射成 `AgentRunDispatchError` 抛出来 —— 拒绝是以**异常**
    回来的，不是以状态回来的（2026-11-03 复审指出第一版测试没模拟这一点，
    于是它测的是一条生产上永远不会走到的路径）。
    """
    import logging

    from aiops_diagnostics.execution.agent_runs import AgentRunDispatchError

    f = operator
    attempts = {"n": 0}

    def dispatch(self, config, run_id):
        attempts["n"] += 1
        try:
            return _dispatch_body(self, config, run_id)
        except BaseException as exc:
            print("DEBUG_DISPATCH_EXC", type(exc).__name__, str(exc)[:400])
            raise

    def _dispatch_body(self, config, run_id):
        headers = {"Authorization": "Bearer " + NODE_A_TOKEN}
        publication = f["client"].post(
            "/v1/internal/charging-status/execution-config/" + run_id, headers=headers
        )
        headers["X-AI-Ops-Exec-Config"] = publication.json()["sha256"]
        # 每一次都回一段**纯文本**，网关会拒；平台因此判定这次调用失败。
        f["client"].post(
            f"/v1/internal/operator-runs/{run_id}/completions",
            headers=headers,
            json={"status": "completed", "answer": "我帮你核对了本次记录，建议对照原账单。"},
        )
        raise AgentRunDispatchError("agent platform unavailable")

    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
    with caplog.at_level(logging.WARNING, logger="aiops.operator"):
        posted = f["client"].post(
            "/v1/assistant/questions", headers=HEADERS, json={"question": "订单状态3是什么意思"}
        )
        final = terminal(f, posted.json()["qa_id"])
    assert attempts["n"] == 1 + operator_cpms.MAX_RESAMPLES
    assert final.json()["status"] == "failed"
    assert "operator_resample" in caplog.text


def test_the_node_failure_reason_is_no_longer_collapsed(operator, monkeypatch, caplog):
    """节点已经在发 `model_failed` / `model_stream_incomplete`；网关不能再把它糊成一个码。

    2026-11-03 实测：上游额度耗尽时节点报 `model_failed`，网关记成 `operator_node_failed`，
    用户看到 `OPERATOR_DIAGNOSIS_FAILED` —— 与「模型写错 JSON」完全同形，排查方向被引偏。
    """
    import logging

    f = operator

    def dispatch(_, config, run_id):
        headers = {"Authorization": "Bearer " + NODE_A_TOKEN}
        publication = f["client"].post(
            "/v1/internal/charging-status/execution-config/" + run_id, headers=headers
        )
        headers["X-AI-Ops-Exec-Config"] = publication.json()["sha256"]
        f["client"].post(
            f"/v1/internal/operator-runs/{run_id}/completions",
            headers=headers,
            json={"status": "failed", "reason": "model_failed"},
        )

    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
    with caplog.at_level(logging.WARNING, logger="aiops.operator"):
        posted = f["client"].post(
            "/v1/assistant/questions", headers=HEADERS, json={"question": "订单状态3是什么意思"}
        )
        terminal(f, posted.json()["qa_id"])
    assert "operator_node_reported_failure reason=model_failed" in caplog.text


def test_a_rejection_after_evidence_is_still_redispatched(operator, monkeypatch, caplog):
    """**已经取完证据、上报过证据**才被拒的 run 也必须重派 —— 那是生产上最常见的一种。

    2026-11-03 部署后实测：第一版判据要求「一次工具都没领过」，于是工具轮先跑完、
    模型最后一轮才吐错的 run 全都不重派。判据写错了方向。

    重派安全的原因写在 `_untouched` 的注释里：证据有缓存（`grant` 里
    `if tool in task.evidence: return …`），重派复用同一份证据，不会二次请求 CPMS。
    """
    import logging

    from aiops_diagnostics.execution.agent_runs import AgentRunDispatchError

    f = operator
    attempts = {"n": 0}
    delivered = []

    def dispatch(self, config, run_id):
        attempts["n"] += 1
        headers = {"Authorization": "Bearer " + NODE_A_TOKEN}
        publication = f["client"].post(
            "/v1/internal/charging-status/execution-config/" + run_id, headers=headers
        )
        assert publication.status_code == 200, publication.text
        headers["X-AI-Ops-Exec-Config"] = publication.json()["sha256"]
        headers["X-AI-Ops-Tool-Contract"] = operator_cpms.TOOL_CONTRACT

        tool = operator_cpms.TOOL_NAMES[0]
        granted = f["client"].post(
            f"/v1/internal/operator-runs/{run_id}/tools/{tool}/authorization",
            headers=headers,
            json={"arguments": {}},
        )
        assert granted.status_code == 200, granted.text
        grant = granted.json()
        fetched = f["client"].get(
            grant["base_url"] + grant["request"]["path"],
            headers={"Authorization": "Bearer " + grant["bearer"]},
        )
        assert fetched.status_code == 200, fetched.text
        body = fetched.json()
        from aiops_diagnostics.operator.operator_cpms import project_evidence

        # 证据必须与网关**自己投影出来的那份**逐字相等（`project_evidence` 会裁剪字段）。
        # 直接回原始响应体会被判 OPERATOR_EVIDENCE_MISMATCH —— 那正是第一次尝试踩的坑。
        projected = project_evidence(body, "ORDER-1001", tool)
        # **上报证据**：`task.delivered` 只在这里被填充。不调它就只能覆盖「有证据」，
        # 覆盖不到「已交付」（2026-11-03 复审指出）。
        reported = f["client"].post(
            f"/v1/internal/operator-runs/{run_id}/tools/{tool}/evidence",
            headers=headers,
            json={
                "authorization_id": grant["authorization_id"],
                "evidence": {
                    "tool": tool,
                    "inputs": grant["inputs"],
                    "source": "cpms.orderDetail",
                    "observed_at": datetime.now(UTC).isoformat(),
                    "data": projected["data"],
                },
            },
        )
        assert reported.status_code == 200, reported.text
        delivered.append(True)

        # 再回一段纯文本：网关会拒，平台因此判定这次调用失败。
        f["client"].post(
            f"/v1/internal/operator-runs/{run_id}/completions",
            headers=headers,
            json={"status": "completed", "answer": "我帮你核对了本次记录，建议对照原账单。"},
        )
        raise AgentRunDispatchError("agent platform refused the completion")

    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
    with caplog.at_level(logging.WARNING, logger="aiops.operator"):
        posted = f["client"].post(
            "/v1/assistant/questions",
            headers=HEADERS,
            json={"question": "帮我分析这笔单的电费 ORDER-1001"},
        )
        terminal(f, posted.json()["qa_id"])
    assert delivered, "证据上报必须真的发生过，否则覆盖不到 delivered 非空"
    assert attempts["n"] == 1 + operator_cpms.MAX_RESAMPLES, "取过证据后再被拒，仍应重派一次"
    assert "operator_resample" in caplog.text
