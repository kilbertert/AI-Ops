"""管家非订单回合：模型理解问题，站点事实由当前权限下的CPMS响应生成。"""

import json
import logging
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

#: 订单问题问的是**哪个主题**。判据是封闭词表 + 提问原文，**不看模型输出** ——
#: 模型只写开场语气，事实段由系统按这里选出的字段生成。
#:
#: 为什么需要这一步（2026-11-04 反馈）：此前不论问「电压」还是问「电费」，事实段一律把
#: 20 个字段全列出来，于是两种问法的答复除结尾以外逐字相同。用户读到的不是回答，
#: 是一张把后台整页抄下来的表 —— 而且「电压」这一问里电压其实是**空的**（上游
#: `voltage: null`），满屏其他字段反而掩盖了「这一项没有」。
#:
#: 词表是**维护中的**，不是对自然语言的穷举：匹配不到任何主题时退回原有全量渲染，
#: 所以漏词只会回到旧行为，不会答错主题。
ORDER_TOPIC_FIELDS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "fee",
        ("电费", "服务费", "费用", "多少钱", "收费", "账单", "计费", "金额", "总价", "付款", "支付"),
        (
            "isPrePay",
            "prePayAmount",
            "electricityFee",
            "serviceFee",
            "dsElectricFee",
            "dsServiceFee",
            "launchFee",
            "occupyFee",
            "marketAmount",
            "vat",
            "vatFee",
            "totalAmount",
            "payAmount",
            "payTime",
            "refundAmount",
        ),
    ),
    (
        "energy",
        ("电量", "用电", "度数", "kwh", "充电量"),
        ("electricity", "time", "gunPower", "startSoc", "endSoc"),
    ),
    (
        "electrical",
        ("电压", "电流", "功率", "伏", "安培"),
        ("voltage", "gunPower", "electricity", "time", "deviceCode", "childDeviceCode", "interfaceType"),
    ),
    (
        "process",
        ("过程", "明细", "曲线", "采样", "监测", "时间线", "多久", "时长"),
        ("electricity", "time", "createdTime", "stopTime", "gunPower", "startSoc", "endSoc"),
    ),
    (
        "stop",
        ("停止", "结束", "中断", "故障", "异常", "为什么停", "原因", "状态"),
        ("status", "stoppedReasonCode", "stoppedReasonContent", "stopTime", "createdTime", "time"),
    ),
)

#: 字段总表与渲染顺序。无主题命中时**全部**按这个顺序列出（原有行为）；
#: 命中主题时只保留主题字段，但顺序仍取自这里 —— 同一主题下两次提问的排布一致。
ORDER_FACT_FIELDS_ALL = (
    "status",
    "isPrePay",
    "prePayAmount",
    "electricityFee",
    "serviceFee",
    "dsElectricFee",
    "dsServiceFee",
    "launchFee",
    "occupyFee",
    "marketAmount",
    "vat",
    "vatFee",
    "totalAmount",
    "payAmount",
    "payTime",
    "refundAmount",
    "electricity",
    "time",
    "voltage",
    "gunPower",
    "startSoc",
    "endSoc",
    "deviceCode",
    "childDeviceCode",
    "interfaceType",
    "createdTime",
    "stopTime",
    "stoppedReasonCode",
    "stoppedReasonContent",
)


def order_fact_fields(question: str) -> tuple[str, ...] | None:
    """这次提问只看哪几项事实；`None` 表示「没识别出主题，按全量列」。

    返回的顺序固定用 `ORDER_FACT_FIELDS_ALL` 里的相对次序，这样同一个主题下
    两次提问的事实段顺序一致 —— 读者对比两份答复时不会因为顺序不同而多看一眼。
    """
    text = (question or "").casefold()
    if not text:
        return None
    chosen: set[str] = set()
    for _, words, fields in ORDER_TOPIC_FIELDS:
        if any(word.casefold() in text for word in words):
            chosen.update(fields)
    if not chosen:
        return None
    return tuple(name for name in ORDER_FACT_FIELDS_ALL if name in chosen)


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
    # 通用名词短语（`站点名称`/`站名`/`站点详情`…）由 :data:`GENERIC_SITE_WORDS` 先整体
    # 中性化，不走这里 —— 逐个字跳过会放行 `虚构乙站详情`（复审反例）。
    r"[\u4e00-\u9fff]{2,8}(?<!的)(?:充电站|站点|站|充电桩|桩)"
    # 英文同理：「Test Station」是名字，「The station」「Your stations」不是。
    r"|\b(?!(?i:the|a|an|all|any|some|this|that|these|those|each|every|your|our|matching|nearest|visible|no)\s)"
    r"[A-Z][A-Za-z0-9\-]{1,20}\s+(?:station|site)s?\b",
)

#: **通用名词短语**：说的是类别，不是给某个地方起名。整条词一起中性化再判站名形状。
#: 必须是完整词（`站点名称` 含「点」），否则 `虚构乙站详情` 里的「站详情」会被误吞。
#: 2026-11-02 真实回放 18 次里 3 次拒收全部是这个词形状（`可以告诉我站点名称`）。
GENERIC_SITE_WORDS = (
    "充电站名称",
    "充电桩名称",
    "站点名称",
    "站点详情",
    "站点列表",
    "站点信息",
    "站点位置",
    "站点数量",
    "站点标签",
    "站点范围",
    "站点目录",
    "站点内容",
    "站名",
)

#: 找站说明可以填的事实片段。键是协议的一部分，与 :func:`site_facts` 同源。
SITE_FACTS = ("site_count", "site_roster", "source", "observed_at", "limits")
SITE_SLOT = re.compile(r"\{\{fact:([a-z_]{1,32})\}\}")

INSTRUCTIONS = """你是只读充电运营管家。本轮没有选定订单，不提供订单工具或历史订单上下文。
先理解用户的问题，不要把“订单”一词等同于查询某笔订单：状态含义、操作方法、闲聊和能力咨询
正常回答；只有需要查看某次订单事实时才用order_required，说明需要提供或选择单号。
找站、查站点位置时**先调用 cpms_site_search** 取得当前账号可见的真实结果，再据此写一两句说明。
工具已经返回时最终kind必须为site_search；不能改成answer绕开系统站点清单与事实校验。
调用工具前先提取完整地点词到address_terms数组，不能把行政层级拼成一个address字符串。
name和address_terms只从用户原话提取站名或地点关键词，
例如“广州天河附近的充电站”先调用cpms_site_search(name="",address_terms=["广州","天河"])；
关键词是完整地名，不逐字切分；取证失败或无法判断时询问更具体地点。
说明写在该工具的**结果之后**，只负责开场与归纳语气：
text负责自然的开场、核对建议和追问，具体名称与数量由系统清单展示。
必须按工具结果区分有结果、无匹配与空授权范围；未查询的信息可明确说明未查询，不能编造。
不要断言营业时间、实时可用性、价格、距离或写操作已完成；亲切风格可自然回应并询问是否调整条件。
address_terms必须保持本轮工具所用的地址关键词，例如["广州","天河"]，不能取完结果又改为空。
不得写数字、价格、距离、空闲枪口或具体站名地址——名单、数量、截断范围、无定位限制和来源
全部由系统按本轮 CPMS 结果列出。没有调用工具就没有结果，此时text必须为空。
无法确定地点时正常追问地点，不猜坐标、位置、站名、距离、价格或实时枪口状态。
问“我有哪些站点”“我有几个站点”这类**列举**问题时也用同一个工具，但关键词全空：
系统按当前账号可见范围列出站点；你不要补关键词，也不要在text里写名单。
没有知识库检索工具，不得声称读过知识库或业务数据；站点查询只走 cpms_site_search。
应用SOP仅作领域背景，其订单步骤仅适用于订单诊断。
不能执行或声称执行退款、重启、停充、改配置；可以提供操作指引，危险情况优先提示安全措施。
询问指定站点未来充电方案或补充计划条件时调用cpms_station_plan，只传用户本轮提供的条件，
不要重复前文已给的参数。缺条件时仍可用工具返回澄清，不编造日期、功率、电量。
功率/电量参数只写原话数字，对应原话的kW/kWh；日期使用原话YYYY-MM-DD，不推断相对日期。
basis只有用户明确确认“普通按量、不用优惠、不预约、不买保险、充完离场”时才传；
timezone只传用户明确确认的IANA计费时区，如Asia/Shanghai，不从地点猜时区。
该工具结果已含系统计算，最终必须{"kind":"station_plan","text":"","name":"","address_terms":[]}。
仅输出一个JSON对象，恰好四字段：
{"kind":"answer|order_required|site_search|station_plan","text":"回答或澄清","name":"站名关键词或空串","address_terms":[]}
answer/order_required的name必须为空且address_terms必须为空；text以用户要求语言写作。
"""


def manifest(text):
    from aiops_diagnostics.execution.node_publication import business_prompt

    body = json.loads(text)
    body["schema"] = SCHEMA
    # 无订单回合保留**能在这类运行里用的**工具（站点查询及管家专属方案）。订单诊断工具没有
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
        if not {"conversation", "operator_conversation"}.intersection(declared.applies_to):
            continue
        # 参数 schema 来自**声明**，不来自模板：模板只有运营勾选的名单，
        # 参数形状的唯一定义在声明里。
        kept.append({**tool, "description": declared.description, "input_schema": declared.input_schema()})
        seen.add(tool["name"])
    for declared in operator_tools.tools():
        if (
            not {"conversation", "operator_conversation"}.intersection(declared.applies_to)
            or declared.name in seen
        ):
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


def question_span(value: str, question: str) -> str:
    """只忽略空白和大小写匹配，返回用户原文；保留地名内部空格。"""
    needle = "".join(value.split()).casefold()
    if not needle:
        return ""
    positions = [i for i, char in enumerate(question) if not char.isspace() for _ in char.casefold()]
    folded = "".join(char for char in question if not char.isspace()).casefold()
    start = folded.find(needle)
    if start < 0:
        return ""
    return question[positions[start] : positions[start + len(needle) - 1] + 1]


def validate(answer, question, *, check_site_note=True):
    if (
        not isinstance(answer, dict)
        or set(answer) != {"kind", "text", "name", "address_terms"}
        or not isinstance(answer["kind"], str)
        or answer["kind"] not in {"answer", "order_required", "site_search", "station_plan"}
        or not isinstance(answer["text"], str)
        or len(answer["text"]) > 6000
        or not isinstance(answer["name"], str)
        or len(answer["name"]) > 80
        or not isinstance(answer["address_terms"], list)
        or len(answer["address_terms"]) > 4
    ):
        _answer_refused("answer_shape")
    terms = answer["address_terms"] + ([answer["name"]] if answer["name"] else [])
    spans = []
    for term in terms:
        if not isinstance(term, str) or not 1 <= len(term) <= 80 or not term.strip():
            _answer_refused("search_shape", "OPERATOR_INVALID_SEARCH")
        if any(c in term for c in "%_\\\r\n"):
            _answer_refused("search_characters", "OPERATOR_INVALID_SEARCH")
        span = question_span(term, question)
        if not span:
            _answer_refused("search_source", "OPERATOR_INVALID_SEARCH")
        spans.append(span)
    fixed = dict(
        answer,
        name=spans[-1] if answer["name"] else "",
        address_terms=spans[: len(answer["address_terms"])],
    )
    if answer["kind"] == "station_plan":
        if terms or answer["text"]:
            _answer_refused("plan_content")
    elif answer["kind"] == "site_search":
        # **空关键词合法**：它表示"列出我可见范围内的站点"，不是"搜某个地点"。
        # 这里曾经要求关键词非空，把「我当前有哪些站点？」判成非法 —— 而那个限制
        # 只为防模型编造搜索条件，空关键词什么都没编。范围也从来不靠关键词收窄：
        # ``query_sites()`` 逐行核对 tenantId 与站点集合。
        #
        # 旧快照沿用字符校验；新完成流程在证据检查后独立审核说明（ADR-0195）。
        if check_site_note:
            validate_site_note(answer["text"])
    elif terms or not answer["text"].strip() or claims_mutation(answer["text"]):
        _answer_refused("answer_content")
    return fixed


def _answer_refused(rule, code="OPERATOR_INVALID_ANSWER"):
    logging.getLogger("aiops.operator").warning("operator_conversation_rejected rule=%s", rule)
    raise OperatorRefused(code)


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
        _answer_refused("note_length")
    if not text.strip():
        return
    own = SITE_SLOT.sub(" ", text)
    if {name for name in SITE_SLOT.findall(text)} - set(SITE_FACTS):
        _answer_refused("note_slot")
    generic = re.sub(r"(?:目标|具体|某|该|这些|匹配的)站(?:点)?", "结果", own)
    # 通用名词短语**整条**中性化：它在说类别，不在给某个地方起名。逐个字跳过会放行
    # `虚构乙站详情`（复审反例 `虚构乙站` + 通用尾巴），所以只认完整词。
    for phrase in GENERIC_SITE_WORDS:
        generic = generic.replace(phrase, "结果")
    if SITE_FORBIDDEN.search(own):
        _answer_refused("note_fact")
    if SITE_NAME_SHAPE.search(generic):
        _answer_refused("note_site_name")
    if claims_mutation(own):
        _answer_refused("note_mutation")


#: 站点一次最多展示几条。与 ``operator_cpms.SITE_MAX_SHOWN`` 是**同一个上限**：
#: 那个模块导入本模块，反向导入会成环，所以常量住在这里、那边别名引用。
SITE_MAX_SHOWN = 10


def shown_sites(sites) -> list:
    """渲染与事实填充共用的那十行；截断发生在这里，不在每个调用点。

    上游查询、工具证据、C 端投影各自按自己的规则返回（工具固定第一页 50 条），
    所以不能靠它们已经截过 —— 三条入口都在这里收口。
    """
    return sites["sites"][:SITE_MAX_SHOWN]


def _count_is_total(sites, listing: bool) -> bool:
    """``total`` 能不能当成「匹配到多少个」说出去。

    两个理由说不能：列举之外（搜索）时它是**上游**计数，本地还要再过滤一遍；
    以及这一页没读全（``limited`` 且未超出展示上限）时它只说明「还有更多」。
    员工的租户口径（``scope_limited``）同理。
    """
    return (
        listing
        and not sites.get("scope_limited")
        and not (sites.get("limited") and len(sites["sites"]) <= len(shown_sites(sites)))
    )


def _stated_count(sites, listing: bool) -> int:
    """这次查询**可以说出去**的条数 —— 正文头一句与说明片段共用这一个数。

    没列出的部分由「另有 N 个」交代，两个数因此永远对得上。
    """
    total = sites.get("total") or 0
    if _count_is_total(sites, listing) and total > 0:
        return total
    return len(shown_sites(sites))


def site_facts(sites, language, *, listing: bool = False) -> dict[str, str]:
    """这一轮**可以**填进说明的事实。与 :func:`render` 写的那份清单同源，不另算一遍。"""
    zh = language.startswith("zh")
    rows = shown_sites(sites)
    return {
        # 这个数必须与 :func:`render` 头一句相同：曾经的写法是 `len(shown_sites(...))`，
        # 于是同一段答复里「共 10 个站点」紧跟着「共 500 个站点」（2026-11-02 复审）。
        "site_count": str(_stated_count(sites, listing)),
        "site_roster": "\n".join(
            f"{i}. {row['name']} — {row['address'] or ('地址未提供' if zh else 'Address unavailable')}"
            for i, row in enumerate(rows, 1)
        ),
        "source": sites["source"],
        "observed_at": sites["observed_at"],
        "limits": (
            "未提供定位，未按距离排序；本次结果不表示实时空闲枪口或价格。"
            if zh
            else "No location supplied: not sorted by distance. Live availability and prices not queried."
        ),
    }


def fill_site_note(text, sites, language, *, listing: bool = False) -> str:
    """把说明里的片段填成系统给出的事实。未知片段是协议错误，不是措辞问题。"""
    facts = site_facts(sites, language, listing=listing)
    if {name for name in SITE_SLOT.findall(text)} - set(facts):
        raise OperatorRefused("OPERATOR_INVALID_ANSWER")
    return SITE_SLOT.sub(lambda match: facts[match.group(1)], text)


def render(answer, sites, language, *, reviewed=False):
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
                for i, s in enumerate(shown_sites(sites), 1)
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
        raw = shown_sites(sites)
        if _count_is_total(sites, listing):
            # 可信的总数与展示同源：说「共 N 个」，就得交代少列的那部分。
            remaining = _stated_count(sites, listing) - len(raw)
            line = (
                f"另有 {remaining} 个站点未在此列出。"
                if zh
                else f"{remaining} more sites are not listed here."
            )
        else:
            # 只能指出**本地确实握有**的匹配行，不能拿上游计数冒充：搜索问法下
            # total 是过滤前的计数，说成「另有 N 个匹配」就是编的。
            remaining = len(sites["sites"]) - len(raw)
            line = (
                f"另有 {remaining} 个匹配站点未在此列出。"
                if zh
                else f"{remaining} more matching sites are not listed here."
            )
        if remaining > 0:
            text += "\n" + line
        # 模型写的说明只能**在前**：它负责语气，事实全部在它下面由系统写出。
        # 片段（{{fact:site_roster}} 等）由系统填值，说明里不会出现它自己编的名单。
        note = (answer.get("text") or "").strip()
        if note:
            if not reviewed:
                validate_site_note(note)
            text = fill_site_note(note, sites, language, listing=listing) + "\n\n" + text
    return {"blocks": [{"kind": "text", "text": text}], "retrieval_status": "unavailable"}


def _roster_count(sites, zh):
    """列举问法的头一句：「你的范围内共 N 个站点，这里显示前 M 个」。

    只在**数字可信**时给出总数：员工的 CPMS 计数是租户口径，还要再经站点集合过滤，
    两者不等时那个 total 不是"你有多少个"，此时只说显示了几条，不编总数。

    与 :func:`site_facts` 的 ``site_count`` 是同一个判断：说明片段里的数字若与这里
    的头一句不同，读者会在同一段答复里看到两个总数。
    """
    shown = len(shown_sites(sites))
    total = sites.get("total") or 0
    if total <= 0:
        return ""
    if not _count_is_total(sites, True):
        # 这一页之外还有没读过的东西。此时**不能说「前 N 个」**：那意味着「按某种顺序
        # 取到的最前面几条，其余也可知」。41 上 total=153 是常态，而我们只读了一页；
        # 「本次读取到 N 个」才是事实。余量由「结果可能不完整」交代（limited），
        # 已握有的多余行由「另有 N 个」交代。
        return (
            f"当前账号可见范围内，本次读取到 {shown} 个站点："
            if zh
            else f"Read {shown} sites in your scope on this pass:"
        )
    if total == shown:
        return ""
    # 读全了且超出展示上限：这里的「前 N 个」是准的 —— total 行都在手里。
    return (
        f"当前账号可见范围内共 {total} 个站点，这里显示前 {shown} 个："
        if zh
        else f"{total} sites are visible to your account; showing the first {shown}:"
    )
