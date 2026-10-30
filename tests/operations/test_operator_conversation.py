"""非订单提问必须经过现役节点，但不能取得订单工具。"""

import json
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

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
            "显示前 1 个",
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
            "显示前 1 个",
            "共 1",
        ),
    ],
)
def test_listing_reports_the_count_only_when_it_is_trustworthy(sites, needle, forbidden):
    """列举要回答"我有多少个"；员工的 total 是租户口径，不能冒充他自己的站点数。"""
    roster = {
        "source": "cpms.sitePage",
        "observed_at": "2026-10-29T00:00:00+00:00",
        "limited": sites["total"] > 1,
        "empty_scope": False,
        **sites,
    }
    text = conversation.render(
        {"kind": "site_search", "text": "", "name": "", "address_terms": []}, roster, "zh-CN"
    )["blocks"][0]["text"]
    assert needle in text
    if forbidden:
        assert forbidden not in text


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
