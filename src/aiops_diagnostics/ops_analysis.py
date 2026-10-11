"""运维分析面：跨订单的确定性聚合（#697 / #698，PRD #693 的 ③ 号路径）。

与单订单诊断是**两个面**：诊断回答「这一条为什么坏」，本模块回答「这一段范围内坏得多不多、
集中在哪」。所以它的输入是**站点集合 + 时间窗**（没有 ``order_no``），输出是第三份对外契约。

**三张卡共用这一份规则**（#698 在同一张面上再加两张）：失败率、离线桩、故障码。它们的
作用域、时间窗语义、范围下推与错误形状完全相同，差别只在聚合与字段——所以共用入口门、
共用散文路径与语言守卫、共用 ``reason_code`` 的词汇，而不是各写一份。

四条纪律决定了本模块的形状：

1. **数字不由模型生成。** 所有计数、比率与风险等级来自确定性计算，模型只写成因叙述与
   建议两段散文。两道墙：结构化输出 schema 里**没有数字字段**，装配时**按名字只读两个
   散文键**。因此即使 provider 无视 ``additionalProperties``，一个模型写出的数字也没有
   路径进入数字字段。
2. **数字可复核。** ``RULE_VERSION`` 与每一张卡自己的 ``definition`` 一起落进契约，读的人
   据此知道这个数字是按哪条规则算的。状态语义的权威是 ``rules.ORDER_STATUS`` 与 staged 的
   ``AbnormalOrderTxDataHandler.java``；设备表与故障表的列名抄的是公司自己的 DDL 与
   mapper（出处写在各聚合函数的 docstring 里）。**公式参考公司既有 mapper，执行落在
   这里的只读 SQL 上**——不把公司运维 HTTP 当授权边界（那些端点无凭据即通、
   ``tenant-id`` 头不改变响应哈希；切流条件见 ADR-0014）。
3. **算不出来就说算不出来。** 空站点集合、窗外没有记录、聚合不可用 ⇒ ``unavailable``，
   绝不由模型补一个数字，也不把「没人查过的范围」说成「没有异常」。
4. **散文出不去不撤数字。** 模型漏了中文（或回了个非文本），换的是散文，**不是数字**：
   把一次已经算完的聚合改写成「暂不可用」是一句关于数据源的假陈述（ADR-0007 那条线）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aiops_diagnostics.answer_language import answer_chinese_leak, record_answer_language_fallback
from aiops_diagnostics.i18n import DEFAULT_LANGUAGE, QA_FALLBACK_MESSAGES

#: 这一族规则的版本。与 ``health_metrics.RULE_VERSION`` 分开：健康报告的规则版本一变，
#: 不该连带让运维分析的复用失效，反之亦然。契约与作业行都盖这一个值。
RULE_VERSION = "ops-analysis-v1"

#: 面上的三张卡。作业行上存一个字符串，轮询时据此还原成调用方要的那张卡——
#: 而不是让调用方自己记住调过哪条路由。
OPERATIONS_CARD_FAILURE_RATE = "failure_rate"
OPERATIONS_CARD_OFFLINE = "offline"
OPERATIONS_CARD_FAULT_CODE = "fault_code"
OPERATIONS_CARDS = frozenset(
    {OPERATIONS_CARD_FAILURE_RATE, OPERATIONS_CARD_OFFLINE, OPERATIONS_CARD_FAULT_CODE}
)

#: ``ch_order_info.status`` 里「异常结束」的取值。证据：``rules.ORDER_STATUS[5]`` 是
#: 「可控异常结束」，``engine._analyze_status`` 把它分类为 ``controlled_abnormal_end``，
#: 而 staged 的 ``AbnormalOrderTxDataHandler.java`` 写明 YKC 停止码 64–73 记正常结束、
#: 其余记异常结束（落库为 5）。**只计 5**：状态 2（不可控异常）与 3（已人工处理）是另外
#: 两种状态，把它们并进来会让这个数字不再可复核。契约里带着这条定义，所以读的人不会
#: 把 ``failure_rate`` 当成「任何异常」。
ABNORMAL_STATUS = 5

#: 风险等级的门限。**这不是本仓既有规则**——树里没有任何一处定义过失败率门限
#: （``engine.py`` 只分类、``rules.py`` 只给标签、原型里那张「异常/失败率」卡没有公式）。
#: 因此这三个门限是本票选的，集中在一个函数里，好让一次标定是一行改动；它们与健康报告
#: 的分级同一个心智模型（``normal / attention / abnormal``，外加 ``unavailable``）。
RISK_ATTENTION_THRESHOLD = 0.02
RISK_ABNORMAL_THRESHOLD = 0.10

#: 算不出来（或散文交不出去）时的可区分原因，写进契约让读的人知道缺的是哪一步。
#: ``SCOPE_EMPTY``：调用者的站点范围为空，查询**根本没发**。
#: ``NO_ORDERS_IN_WINDOW`` / ``NO_INCIDENTS_IN_WINDOW`` / ``NO_FAULTS_IN_WINDOW``：
#: 范围有效但窗内没有记录——三张卡各一个码，因为它们缺的是**三张不同的表**，
#: 读的人据此知道该去查什么，而不是一个笼统的「没数据」。
#: ``PROSE_LEAKED``：数字算出来了，模型写的叙述漏了中文，被定稿点换成兜底文案。
#: ``PROSE_INVALID``：模型回包里的散文字段不是文本（形状漂移），同样不出门。
#: 后两个**不撤数字**——见模块顶部的第 4 条纪律。
REASON_SCOPE_EMPTY = "SCOPE_EMPTY"
REASON_NO_ORDERS = "NO_ORDERS_IN_WINDOW"
REASON_NO_INCIDENTS = "NO_INCIDENTS_IN_WINDOW"
REASON_NO_FAULTS = "NO_FAULTS_IN_WINDOW"
REASON_PROSE_LEAKED = "PROSE_LEAKED"
REASON_PROSE_INVALID = "PROSE_INVALID"


@dataclass(frozen=True, slots=True)
class AnalysisAggregate:
    """一次订单聚合的结果（失败率卡；关系库侧，模型绝不参与）。"""

    total: int
    abnormal_ended: int
    normal_ended: int
    #: 异常结束最多的一台站点，以及它的计数。空元组表示没有可归因的站点。
    top_sites: tuple[tuple[str, int], ...]

    @property
    def failure_rate(self) -> float | None:
        """异常结束占比，四位小数。**唯一的浮点出现处**，且规则写死。"""
        if self.total <= 0:
            return None
        return round(self.abnormal_ended / self.total, 4)

    @property
    def scope_empty(self) -> bool:
        return self.total == 0


@dataclass(frozen=True, slots=True)
class OfflineAggregate:
    """离线桩卡的确定性结果（#698）。"""

    incidents: int
    offline_devices: int
    open_incidents: int
    offline_minutes: int
    #: 范围内的设备总数（分母）。``None`` 表示分母没取到。
    devices_total: int | None = None


@dataclass(frozen=True, slots=True)
class FaultCodeAggregate:
    """故障码卡的确定性结果（#698）。"""

    total_faults: int
    faulty_devices: int
    top_codes: tuple[tuple[str, int], ...]
    devices_total: int | None = None


def risk_level(failure_rate: float | None, total: int) -> str:
    """失败率 → 等级。``unavailable`` 在前：没有分母时任何等级都是假的。"""
    if failure_rate is None or total <= 0:
        return "unavailable"
    if failure_rate < RISK_ATTENTION_THRESHOLD:
        return "normal"
    if failure_rate < RISK_ABNORMAL_THRESHOLD:
        return "attention"
    return "abnormal"


def _narrative(payload: Any) -> tuple[Any, Any]:
    """从模型回包里取**恰好两个**散文字段，**不做类型强转**。

    只按名字读，且只在 ``payload`` 是映射时读。其余字段（无论模型写了什么）在装配时
    被丢弃——这是「模型不能写数字」的第二道墙：契约里的数字全来自聚合对象，
    而没有一条代码路径从 ``payload`` 取过数字。

    返回值刻意不 ``str()``：把任意形状强转成字符串，正是「provider 回了一个 dict、
    它被序列化进用户可见正文」的成因。类型检查在 ``_prose_fields`` 里做，
    那里判不出来就把散文换成兜底文案。
    """
    if not isinstance(payload, dict):
        return "", ""
    return payload.get("main_cause") or "", payload.get("recommendation") or ""


def _fallback_prose(language: str) -> str:
    pack = QA_FALLBACK_MESSAGES.get(language) or QA_FALLBACK_MESSAGES[DEFAULT_LANGUAGE]
    return str(pack["unavailable"])


def _join_prose(main_cause: Any, recommendation: Any) -> Any:
    """The single string the guard judges — or a non-string, so the caller can refuse it.

    Typed ``Any`` on purpose: the point of the return type is that it may NOT be a
    string, and a signature promising ``str`` would be a promise this function
    cannot keep about provider output.
    """
    parts = [item for item in (main_cause, recommendation) if item]
    if not all(isinstance(item, str) for item in parts):
        return parts
    return "\n".join(parts).strip()


def _prose_fields(
    payload: Any,
    language: str,
    *,
    counted: bool,
    surface: str,
) -> tuple[dict[str, Any], str | None]:
    """三张卡共用的散文定稿点，返回 ``(字段, 散文侧的原因码)``。

    这是输出语言守卫在本模块的**唯一**调用点（#697 的第 5 个面、#698 沿用）：
    三张卡各写一遍就会有一份漏掉守卫而没人发现。

    ``counted=False`` 时根本不读模型——范围为空或窗外没有记录是确定性答案，让模型去
    叙述它只会产生一段想象。原因码**单独返回**而不是塞进字段字典：那本字典会被展开进
    契约，而 ``reason_code`` 在契约里已经有主人（范围/窗口），直接写在字典里会被随后
    的赋值悄悄覆盖，于是「模型漏了中文」这件事在契约里不留痕迹——正是本仓反复防的静默。
    """
    if not counted:
        return {"main_cause": None, "recommendation": None}, None
    main_cause, recommendation = _narrative(payload)
    prose: Any = _join_prose(main_cause, recommendation)
    if not isinstance(prose, str):
        return {"main_cause": _fallback_prose(language), "recommendation": None}, REASON_PROSE_INVALID
    if prose and answer_chinese_leak(prose, language):
        record_answer_language_fallback(language=language, leaked=prose, surface=surface)
        return {"main_cause": _fallback_prose(language), "recommendation": None}, REASON_PROSE_LEAKED
    return {"main_cause": main_cause or None, "recommendation": recommendation or None}, None


def _scope_fields(
    site_ids: tuple[str, ...] | None,
    window_start: datetime | None,
    window_end: datetime | None,
) -> dict[str, Any]:
    return {
        "site_ids": list(site_ids) if site_ids is not None else None,
        "window": {
            "start": _iso_z(window_start) if window_start is not None else None,
            "end": _iso_z(window_end) if window_end is not None else None,
        },
    }


def _ratio(numerator: int, denominator: int | None) -> float | None:
    """占比，四位小数。分母为 ``None`` 或 0 ⇒ ``None``（不是 0.0）。

    ``0.0`` 与「算不出来」在契约里必须是两个值：前者说「一台都没坏」，后者说
    「不知道有多少台」。把它们合并成 0 会让缺数据看起来像好消息。
    """
    if denominator is None or denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def build_failure_rate_contract(
    aggregate: AnalysisAggregate,
    payload: Any,
    *,
    language: str = DEFAULT_LANGUAGE,
    site_ids: tuple[str, ...] | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    reason_code: str | None = None,
) -> dict[str, Any]:
    """失败率卡：一次订单聚合 + 一段模型叙述 → 对外契约。

    ``reason_code`` 非空时**不读模型**：范围为空或窗外没有订单是**确定性答案**。

    ``counted`` 由**聚合**决定，不由散文决定：数字算出来了就交付，即使模型的解释被
    换成了兜底文案（漏中文或形状不对）；范围/窗口的原因才连数字一起撤，因为那时根本
    什么都没算。把一次算完的 ``failure_rate`` 改写成「暂不可用」，是一句关于数据源的
    假陈述（ADR-0007 划下的那条线）。
    """
    counted = reason_code is None
    prose_fields, prose_reason = _prose_fields(
        payload, language, counted=counted, surface="operations_analysis"
    )
    return {
        "failure_rate": aggregate.failure_rate if counted else None,
        "card": OPERATIONS_CARD_FAILURE_RATE,
        **prose_fields,
        "secondary": _secondary(aggregate) if counted else None,
        "risk": risk_level(aggregate.failure_rate, aggregate.total) if counted else "unavailable",
        "rule_version": RULE_VERSION,
        "reason_code": reason_code or prose_reason,
        **_scope_fields(site_ids, window_start, window_end),
        "counts": {
            "total": aggregate.total if counted else None,
            "abnormal_ended": aggregate.abnormal_ended if counted else None,
            "normal_ended": aggregate.normal_ended if counted else None,
        },
        # 定义随数字一起交付：没有它，`failure_rate` 会被读成「任何异常」。
        "definition": {"abnormal": "order_status=5", "window": "half-open [start, end)"},
    }


def build_offline_contract(
    aggregate: OfflineAggregate,
    payload: Any,
    *,
    language: str = DEFAULT_LANGUAGE,
    site_ids: tuple[str, ...] | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    reason_code: str | None = None,
) -> dict[str, Any]:
    """离线桩卡（#698）：与失败率卡同形的另一种卡。

    数字同样全在这里算，模型同样只写叙述。**时长是下界**：仍在离线中的记录没有恢复
    时间，只计数不进时长（``open_incidents`` 单独交付），所以同一窗口两次查询不会因为
    「现在几点」而给出不同的数字。这一点写进 ``definition``，不靠读代码才知道。
    """
    counted = reason_code is None
    prose_fields, prose_reason = _prose_fields(
        payload, language, counted=counted, surface="operations_analysis"
    )
    return {
        "incidents": aggregate.incidents if counted else None,
        "offline_devices": aggregate.offline_devices if counted else None,
        "open_incidents": aggregate.open_incidents if counted else None,
        "offline_minutes": aggregate.offline_minutes if counted else None,
        "offline_ratio": _ratio(aggregate.offline_devices, aggregate.devices_total) if counted else None,
        "devices_total": aggregate.devices_total if counted else None,
        "risk": _offline_risk(aggregate) if counted else "unavailable",
        "card": OPERATIONS_CARD_OFFLINE,
        **prose_fields,
        "rule_version": RULE_VERSION,
        "reason_code": reason_code or prose_reason,
        **_scope_fields(site_ids, window_start, window_end),
        "definition": {
            "offline": "iot_device_offline_monitor.offline_time in window",
            "duration": "recovered incidents only, clamped to window end (a lower bound)",
            "window": "half-open [start, end)",
        },
    }


def build_fault_code_contract(
    aggregate: FaultCodeAggregate,
    payload: Any,
    *,
    language: str = DEFAULT_LANGUAGE,
    site_ids: tuple[str, ...] | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    reason_code: str | None = None,
) -> dict[str, Any]:
    """故障码排行卡（#698）。

    排行只含**故障码与次数**，不含成因分类——``error_reason`` 是一列自由文本，
    在它上面聚合会把同一个物理故障拆成多种写法。要按成因聚合是另一件事（需要
    一个受控词表），不在本期。
    """
    counted = reason_code is None
    prose_fields, prose_reason = _prose_fields(
        payload, language, counted=counted, surface="operations_analysis"
    )
    return {
        "total_faults": aggregate.total_faults if counted else None,
        "faulty_devices": aggregate.faulty_devices if counted else None,
        "top_codes": (
            [{"error_code": code, "fault_count": count} for code, count in aggregate.top_codes]
            if counted
            else None
        ),
        "faulty_ratio": _ratio(aggregate.faulty_devices, aggregate.devices_total) if counted else None,
        "devices_total": aggregate.devices_total if counted else None,
        "risk": _fault_risk(aggregate) if counted else "unavailable",
        "card": OPERATIONS_CARD_FAULT_CODE,
        **prose_fields,
        "rule_version": RULE_VERSION,
        "reason_code": reason_code or prose_reason,
        **_scope_fields(site_ids, window_start, window_end),
        "definition": {
            "fault": "iot_device_failure_records.fault_time in window",
            "window": "half-open [start, end)",
        },
    }


def _offline_risk(aggregate: OfflineAggregate) -> str:
    """离线风险等级：按**当前仍在离线中**的设备占比，不是按历史累计台数。

    历史累计台数会随窗口变长单调增长，用它定级会让「看了三个月」永远比
    「看了一小时」严重。仍在离线中的才是此刻需要处理的。
    """
    return _ratio_risk(_ratio(aggregate.open_incidents, aggregate.devices_total))


def _fault_risk(aggregate: FaultCodeAggregate) -> str:
    return _ratio_risk(_ratio(aggregate.faulty_devices, aggregate.devices_total))


def _ratio_risk(ratio: float | None) -> str:
    """占比 → 等级，与失败率卡同一套门限（三张卡的分级必须是同一把尺）。"""
    if ratio is None:
        return "unavailable"
    if ratio < RISK_ATTENTION_THRESHOLD:
        return "normal"
    if ratio < RISK_ABNORMAL_THRESHOLD:
        return "attention"
    return "abnormal"


def _secondary(aggregate: AnalysisAggregate) -> dict[str, Any] | None:
    """异常结束最集中的站点（若可归因）。

    「次因占比」在本仓**算不出来**：成因分类住在 ``rules.classify_stop_reason`` 里，是
    Python 而不是一列，按成因聚合需要把窗口内的行全拉回来——那会破掉「不下推就不查」
    这条房规。因此本票给的是**可直接行动**的那个维度：哪里最集中。契约里字段名叫
    ``secondary`` 而不是 ``second_cause``，以免读的人以为它是成因占比。
    """
    if not aggregate.top_sites:
        return None
    site_id, count = aggregate.top_sites[0]
    return {"top_site": site_id, "abnormal_ended": count}


def _iso_z(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
