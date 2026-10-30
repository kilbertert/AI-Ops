"""管家非订单回合：模型理解问题，站点事实由当前权限下的CPMS响应生成。"""

import json
import re

from aiops_diagnostics.operator import operator_tools


class OperatorRefused(RuntimeError):
    pass


EXECUTED_MUTATION = re.compile(
    r"(?:"
    r"(?:已|已经|成功|自动|完成|执行了?|进行了?)\s*"
    r"(?:订单)?(?:重算|退款|补发|重放|重试下发|重启|修改|更新|删除|写入)"
    r"|(?:重算|退款|补发|重放|重试下发|重启|修改订单|更新订单|删除订单|写入数据库)"
    r"\s*(?:已完成|成功|完成|完毕)"
    r"|\b(?:refunded|recalculated|replayed|resent|restarted|modified|updated|deleted)\b"
    r")",
    re.IGNORECASE,
)


SCENARIO = "operator_conversation"
SCHEMA = "operator-conversation/1"
INSTRUCTIONS = """你是只读充电运营管家。本轮没有选定订单，不提供订单工具或历史订单上下文。
先理解用户的问题，不要把“订单”一词等同于查询某笔订单：状态含义、操作方法、闲聊和能力咨询
正常回答；只有需要查看某次订单事实时才用order_required，说明需要提供或选择单号。
找站、查站点位置使用site_search，name和address_terms只从用户原话提取站名或地点关键词，
例如“广州天河附近的充电站”对应name="",address_terms=["广州","天河"]。
无法确定地点时正常追问地点，不猜坐标、位置、站名、距离、价格或实时枪口状态。
问“我有哪些站点”“我有几个站点”这类**列举**问题时也用site_search，但四个字段全空：
系统按当前账号可见范围列出站点；你不要补关键词，也不要在text里写名单。
site_search的站点由系统按当前账号可见范围查询，text必须为空，不得在text编造查询结果。
没有检索工具，不得声称读过知识库或业务数据。应用SOP仅作领域背景，其订单步骤仅适用于订单诊断。
不能执行或声称执行退款、重启、停充、改配置；可以提供操作指引，危险情况优先提示安全措施。
仅输出一个JSON对象，恰好四字段：
{"kind":"answer|order_required|site_search","text":"回答或澄清","name":"站名关键词或空串","address_terms":[]}
answer/order_required的name必须为空且address_terms必须为空；text以用户要求语言写作。
"""


def manifest(text):
    from aiops_diagnostics.execution.node_publication import OPERATOR_OUTPUT

    body = json.loads(text)
    body["schema"] = SCHEMA
    # 无订单回合保留**能在这类运行里用的**工具（目前只有站点查询）。订单诊断工具没有
    # 订单就没有取证对象，删掉它们不是裁剪，是正确性。
    kept = []
    for tool in body["tools"]:
        declared = operator_tools.tool(tool["name"])
        if "conversation" not in declared.applies_to:
            continue
        # 参数 schema 来自**声明**，不来自模板：模板只有运营勾选的名单，
        # 参数形状的唯一定义在声明里。
        kept.append({**tool, "input_schema": declared.input_schema()})
    body["tools"] = kept
    sop = "\n".join(body["system_messages"]).removesuffix("\n" + OPERATOR_OUTPUT)
    body["system_messages"] = ["应用业务背景：\n" + sop + "\n\n" + INSTRUCTIONS]
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validate(answer, question):
    if (
        not isinstance(answer, dict)
        or set(answer) != {"kind", "text", "name", "address_terms"}
        or not isinstance(answer["kind"], str)
        or answer["kind"] not in {"answer", "order_required", "site_search"}
        or not isinstance(answer["text"], str)
        or len(answer["text"]) > 6000
        or not isinstance(answer["name"], str)
        or len(answer["name"]) > 80
        or not isinstance(answer["address_terms"], list)
        or len(answer["address_terms"]) > 4
    ):
        raise OperatorRefused("OPERATOR_INVALID_ANSWER")
    terms = answer["address_terms"] + ([answer["name"]] if answer["name"] else [])
    if any(
        not isinstance(term, str)
        or not 1 <= len(term) <= 80
        or not term.strip()
        or term.casefold() not in question.casefold()
        or any(c in term for c in "%_\\\r\n")
        for term in terms
    ):
        raise OperatorRefused("OPERATOR_INVALID_SEARCH")
    if answer["kind"] == "site_search":
        # **空关键词合法**：它表示"列出我可见范围内的站点"，不是"搜某个地点"。
        # 这里曾经要求关键词非空，把「我当前有哪些站点？」判成非法 —— 而那个限制
        # 只为防模型编造搜索条件，空关键词什么都没编。范围也从来不靠关键词收窄：
        # ``query_sites()`` 逐行核对 tenantId 与站点集合。
        # 搜索正文由CPMS生成，模型进度说明既不交付也不持久化。
        answer["text"] = ""
    elif terms or not answer["text"].strip() or EXECUTED_MUTATION.search(answer["text"]):
        raise OperatorRefused("OPERATOR_INVALID_ANSWER")


def render(answer, sites, language):
    if sites is None:
        text = answer["text"]
    else:
        zh = language.startswith("zh")
        # 空关键词 = "我有哪些站点"。它与"按地点搜"是两种问法，回答也不能同形：
        # 搜索没命中可以说"换个更具体的地址"，列举没命中只能说明这个账号确实没有站点。
        listing = not any([answer["name"], *answer["address_terms"]])
        if sites["empty_scope"]:
            text = (
                "当前账号尚未分配可查询站点，请联系管理员分配站点。"
                if zh
                else "No sites are assigned to this account. Please contact your administrator."
            )
        elif sites["sites"]:
            count = _roster_count(sites, zh)
            if listing:
                lines = [
                    count or ("当前账号可见的充电站：" if zh else "Charging sites visible to your account:")
                ]
            else:
                lines = [
                    "当前账号可见的匹配充电站：" if zh else "Matching charging sites visible to your account:"
                ]
            lines.extend(
                f"{i}. {s['name']} — {s['address'] or ('地址未提供' if zh else 'Address unavailable')}"
                for i, s in enumerate(sites["sites"], 1)
            )
            text = "\n".join(lines)
        elif listing:
            text = "当前账号可见范围内没有站点。" if zh else "No sites are visible to your account."
        else:
            text = (
                "本次查询在当前账号可见范围内未找到匹配站点，可换用站名或更具体的地址。"
                if zh
                else "No matching sites found in your scope. Try a name or a more specific address."
            )
        text += "\n\n" + (
            "未提供定位，未按距离排序；本次结果不表示实时空闲枪口或价格。"
            if zh
            else "No location supplied: not sorted by distance. Live availability and prices not queried."
        )
        if sites["limited"]:
            text += "\n" + (
                "查询达到显示或分页上限，结果可能不完整，请缩小地点范围。"
                if zh
                else "The display or page limit was reached; narrow your search for more complete results."
            )
        if not sites["empty_scope"]:
            text += "\n" + sites["source"] + " · " + sites["observed_at"]
    return {"blocks": [{"kind": "text", "text": text}], "retrieval_status": "unavailable"}


def _roster_count(sites, zh):
    """列举问法的头一句：「你的范围内共 N 个站点，这里显示前 M 个」。

    只在**数字可信**时给出总数：员工的 CPMS 计数是租户口径，还要再经站点集合过滤，
    两者不等时那个 total 不是"你有多少个"，此时只说显示了几条，不编总数。
    """
    shown = len(sites["sites"])
    total = sites.get("total") or 0
    trustworthy = not sites.get("scope_limited")
    if total <= 0 or (trustworthy and total == shown):
        return ""
    if not trustworthy:
        return (
            f"当前账号可见范围内显示前 {shown} 个站点："
            if zh
            else f"Showing the first {shown} sites in your scope:"
        )
    return (
        f"当前账号可见范围内共 {total} 个站点，这里显示前 {shown} 个："
        if zh
        else f"{total} sites are visible to your account; showing the first {shown}:"
    )
