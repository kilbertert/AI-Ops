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


def claims_mutation(text):
    return any(
        not re.search(r"(?:不能|没有|不会|未)(?:直接)?$", text[max(0, match.start() - 8) : match.start()])
        for match in EXECUTED_MUTATION.finditer(text)
    )


SCENARIO = "operator_conversation"
SCHEMA = "operator-conversation/1"

#: 找站说明的上限。它是**一两句开场**，不是答复正文：名单、数量、来源由系统写。
MAX_SITE_NOTE_CHARS = 400

#: 说明里不得出现的断言。**封闭词表**：数字，以及价格/距离/可用性的固定说法（中英各一组）。
SITE_FORBIDDEN = re.compile(
    r"\d"
    r"|元|价格|费用|收费|多少钱|免费|不收钱|不要钱|不收费|距离|公里|千米|米内|附近|最近"
    r"|空闲|可用|可以充|能充|可充|有位置|排队"
    r"|\bfree\b|\bavailable\b|\bunavailable\b|\bprice\b|\bcost\b|\bcheap\b|\bdistance\b"
    r"|\bkm\b|\bnearby\b|\bnearest\b|\bvacant\b|\bin stock\b|\bbusy\b",
    re.IGNORECASE,
)

#: 站名形状：以「站/桩」结尾的中文片段，或「… station」的英文片段。
#: 站名只能出现在系统列出的清单里，所以说明里出现**任何**站名形状都拒 —— 只比对已知站名会
#: 放行编造的名字（复核反例：`我帮你找到了虚构乙站`）。
SITE_NAME_SHAPE = re.compile(
    # 「查到的站点」「可见的站」是**说法**不是名字：`的` 紧贴站名后缀时不算站名形状。
    r"[\u4e00-\u9fff]{2,8}(?<!的)(?:充电站|站点|站|充电桩|桩)"
    r"|\b(?!(?:the|a|an|this|that|these|those|each|every|your|our|matching|nearest)\s)"
    r"[A-Z][A-Za-z0-9\-]{1,20}\s+(?:station|site)s?\b",
)

#: 找站说明可以填的事实片段。键是协议的一部分，与 :func:`site_facts` 同源。
SITE_FACTS = ("site_count", "site_roster", "source", "observed_at", "limits")
SITE_SLOT = re.compile(r"\{\{fact:([a-z_]{1,32})\}\}")

INSTRUCTIONS = """你是只读充电运营管家。本轮没有选定订单，不提供订单工具或历史订单上下文。
先理解用户的问题，不要把“订单”一词等同于查询某笔订单：状态含义、操作方法、闲聊和能力咨询
正常回答；只有需要查看某次订单事实时才用order_required，说明需要提供或选择单号。
找站、查站点位置时**先调用 cpms_site_search** 取得当前账号可见的真实结果，再据此写一两句说明。
工具已经返回时最终kind必须为site_search；不能改成answer绕开系统站点清单与事实校验。
name和address_terms只从用户原话提取站名或地点关键词，
例如“广州天河附近的充电站”对应name="",address_terms=["广州","天河"]。
说明写在该工具的**结果之后**，只负责开场与归纳语气：
text不重复任何地名、站名或服务细节，只说明“查到的结果在下方”，可加自然的核对建议和追问。
text里不用“站/桩”字，统一以“结果”“地点”“列表”指代，具体名称只在系统清单中显示。
即使只是复述用户问题，text也不重复地名和“附近/最近”；亲切风格可以回应“我帮你查了一下”，再问是否需要调整查询条件。
address_terms必须保持本轮工具所用的地址关键词，例如["广州","天河"]，不能取完结果又改为空。
不得写数字、价格、距离、空闲枪口或具体站名地址——名单、数量、截断范围、无定位限制和来源
全部由系统按本轮 CPMS 结果列出。没有调用工具就没有结果，此时text必须为空。
无法确定地点时正常追问地点，不猜坐标、位置、站名、距离、价格或实时枪口状态。
问“我有哪些站点”“我有几个站点”这类**列举**问题时也用同一个工具，但关键词全空：
系统按当前账号可见范围列出站点；你不要补关键词，也不要在text里写名单。
没有知识库检索工具，不得声称读过知识库或业务数据；站点查询只走 cpms_site_search。
应用SOP仅作领域背景，其订单步骤仅适用于订单诊断。
不能执行或声称执行退款、重启、停充、改配置；可以提供操作指引，危险情况优先提示安全措施。
仅输出一个JSON对象，恰好四字段：
{"kind":"answer|order_required|site_search","text":"回答或澄清","name":"站名关键词或空串","address_terms":[]}
answer/order_required的name必须为空且address_terms必须为空；text以用户要求语言写作。
"""


def manifest(text):
    from aiops_diagnostics.execution.node_publication import business_prompt

    body = json.loads(text)
    body["schema"] = SCHEMA
    # 无订单回合保留**能在这类运行里用的**工具（目前只有站点查询）。订单诊断工具没有
    # 订单就没有取证对象，删掉它们不是裁剪，是正确性。
    #
    # 名单以**声明**为准，不以模板为准：模板是「运营在这个源应用里勾了什么」，而会话回合
    # 的工具集是「这类运行允许用什么」（ADR-0182，见 `applies_to`）。两者不是同一件事。
    #
    # 2026-11-01 生产事故：模板里只有两个订单工具，本函数按模板过滤后得到空表，而下面的
    # INSTRUCTIONS 仍在要求模型「先调用 cpms_site_search」。模型只能写字，网关按
    # `OPERATOR_SITE_NOTE_WITHOUT_EVIDENCE` 拒收，站点问题 100% 失败。测试一直是绿的，
    # 是因为 fixture 用 `operator_tools.names()` 造模板，恰好含这个工具 —— 夹具与判据同源，
    # 夹具就成了唯一事实来源。见 `tests/operations/test_operator_conversation.py` 里那两例。
    kept = []
    seen = set()
    for tool in body["tools"]:
        declared = operator_tools.tool(tool["name"])
        if "conversation" not in declared.applies_to:
            continue
        # 参数 schema 来自**声明**，不来自模板：模板只有运营勾选的名单，
        # 参数形状的唯一定义在声明里。
        kept.append({**tool, "input_schema": declared.input_schema()})
        seen.add(tool["name"])
    for declared in operator_tools.tools():
        if "conversation" not in declared.applies_to or declared.name in seen:
            continue
        # 模板没列这个工具：按声明补一条。schema 同样来自声明。
        kept.append(
            {
                "name": declared.name,
                "description": declared.description,
                "input_schema": declared.input_schema(),
            }
        )
    body["tools"] = kept
    sop = business_prompt("\n".join(body["system_messages"]))
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
        #
        # **说明文字不再一律清空**：节点先调工具取数、再据此写说明时，那段话是有依据的，
        # 交给 `validate_site_note` 判。没有取数依据的说明由 `operator_cpms` 拒绝
        # （见 `OPERATOR_SITE_NOTE_WITHOUT_EVIDENCE`），不在这里默认放行。
        validate_site_note(answer["text"])
    elif terms or not answer["text"].strip() or claims_mutation(answer["text"]):
        raise OperatorRefused("OPERATOR_INVALID_ANSWER")


def validate_site_note(text):
    """找站说明只负责语气：任何**事实**都不许它写。

    判据由两组**封闭集合**构成，不是对自然语言的理解：

    * 数字，以及价格/距离/可用性的固定说法（中英各一组）；
    * **站名形状**：说明里不许出现任何"以站/桩结尾"或"… station"的片段。名单由系统列出，
      所以说明连已知站名也不必重抄；这样"编一个不存在的站"与"重抄一个真的站"都被挡住 ——
      只比对已知站名会放行前者（复核反例 `我帮你找到了虚构乙站`）。

    数量、名单、截断范围、无定位限制与来源由 :func:`render` 按本轮证据写出。
    """
    if not isinstance(text, str) or len(text) > MAX_SITE_NOTE_CHARS:
        raise OperatorRefused("OPERATOR_INVALID_ANSWER")
    if not text.strip():
        return
    own = SITE_SLOT.sub(" ", text)
    if {name for name in SITE_SLOT.findall(text)} - set(SITE_FACTS):
        raise OperatorRefused("OPERATOR_INVALID_ANSWER")
    generic = re.sub(r"(?:目标|具体|某|该|这些|匹配的)站(?:点)?", "结果", own)
    if SITE_FORBIDDEN.search(own) or SITE_NAME_SHAPE.search(generic):
        raise OperatorRefused("OPERATOR_INVALID_ANSWER")
    if claims_mutation(own):
        raise OperatorRefused("OPERATOR_INVALID_ANSWER")


def site_facts(sites, language) -> dict[str, str]:
    """这一轮**可以**填进说明的事实。与 :func:`render` 写的那份清单同源，不另算一遍。"""
    zh = language.startswith("zh")
    return {
        "site_count": str(len(sites["sites"])),
        "site_roster": "\n".join(
            f"{i}. {row['name']} — {row['address'] or ('地址未提供' if zh else 'Address unavailable')}"
            for i, row in enumerate(sites["sites"], 1)
        ),
        "source": sites["source"],
        "observed_at": sites["observed_at"],
        "limits": (
            "未提供定位，未按距离排序；本次结果不表示实时空闲枪口或价格。"
            if zh
            else "No location supplied: not sorted by distance. Live availability and prices not queried."
        ),
    }


def fill_site_note(text, sites, language) -> str:
    """把说明里的片段填成系统给出的事实。未知片段是协议错误，不是措辞问题。"""
    facts = site_facts(sites, language)
    if {name for name in SITE_SLOT.findall(text)} - set(facts):
        raise OperatorRefused("OPERATOR_INVALID_ANSWER")
    return SITE_SLOT.sub(lambda match: facts[match.group(1)], text)


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
        # 模型写的说明只能**在前**：它负责语气，事实全部在它下面由系统写出。
        # 片段（{{fact:site_roster}} 等）由系统填值，说明里不会出现它自己编的名单。
        note = (answer.get("text") or "").strip()
        if note:
            validate_site_note(note)
            text = fill_site_note(note, sites, language) + "\n\n" + text
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
