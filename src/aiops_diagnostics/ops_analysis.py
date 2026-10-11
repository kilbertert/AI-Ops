"""运维分析面：跨订单的确定性聚合（#697，PRD #693 的 ③ 号路径）。

与单订单诊断是**两个面**：诊断回答「这一条为什么坏」，本模块回答「这一段范围内坏得多不多、
集中在哪」。所以它的输入是**站点集合 + 时间窗**（没有 ``order_no``），输出是第三份对外契约
``{failure_rate, main_cause, secondary, risk, recommendation, ...}``。

三条纪律决定了本模块的形状：

1. **数字不由模型生成。** ``failure_rate`` / ``risk`` / 计数全部来自确定性计算，
   模型只写 ``main_cause`` 叙述与 ``recommendation`` 两段散文。装配时**按名字读两个键**
   （``_narrative``），因此即使 provider 无视 ``additionalProperties``，一个模型写出的
   数字也没有路径进入数字字段。
2. **数字可复核。** ``RULE_VERSION`` 与「异常结束 = ``ch_order_info.status = 5``」一起落进
   契约，读的人据此知道这个数字是按哪条规则算的。状态语义的权威是
   ``rules.ORDER_STATUS`` 与 staged 的 ``AbnormalOrderTxDataHandler.java``（YKC 停止码
   64–73 记正常结束、其余记异常结束 ⇒ 落库为状态 5）——公式参考公司既有 mapper，
   但**执行落在这里的只读 SQL 上**，不把公司运维 HTTP 当授权边界（那些端点无凭据即通、
   ``tenant-id`` 头不改变响应哈希；切流条件见 ADR-0014）。
3. **算不出来就说算不出来。** 空站点集合、窗外没有订单、聚合不可用 ⇒ ``unavailable``，
   绝不由模型补一个数字，也不把「没人查过的范围」说成「没有异常」。
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
#: ``NO_ORDERS_IN_WINDOW``：范围有效但窗内没有订单——与上一条是不同的事实。
#: ``PROSE_LEAKED``：数字算出来了，模型写的叙述漏了中文，被定稿点换成兜底文案。
#: ``PROSE_INVALID``：模型回包里的散文字段不是文本（形状漂移），同样不出门。
REASON_SCOPE_EMPTY = "SCOPE_EMPTY"
REASON_NO_ORDERS = "NO_ORDERS_IN_WINDOW"
REASON_PROSE_LEAKED = "PROSE_LEAKED"
REASON_PROSE_INVALID = "PROSE_INVALID"


@dataclass(frozen=True, slots=True)
class AnalysisAggregate:
    """一次确定性聚合的结果（关系库侧，模型绝不参与）。"""

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
    被丢弃——这是「模型不能写数字」的第二道墙：契约里的数字全来自 ``AnalysisAggregate``，
    而没有一条代码路径从 ``payload`` 取过数字。

    返回值刻意不 ``str()``：把任意形状强转成字符串，正是「provider 回了一个 dict、
    它被序列化进用户可见正文」的成因。类型检查在调用方（``_join_prose``）做，
    那里判不出来就整轮拒绝。
    """
    if not isinstance(payload, dict):
        return "", ""
    return payload.get("main_cause") or "", payload.get("recommendation") or ""


def _fallback_prose(language: str) -> str:
    pack = QA_FALLBACK_MESSAGES.get(language) or QA_FALLBACK_MESSAGES[DEFAULT_LANGUAGE]
    return str(pack["unavailable"])


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
    """把一次聚合与一段模型叙述装配成对外契约（第三份）。

    ``reason_code`` 非空时**不读模型**：范围为空或窗外没有订单是**确定性答案**，
    没有可解释的成因，让模型去叙述它只会产生一段想象。

    输出语言守卫挂在这一个函数里（本模块是本仓**第 4 个**定稿点），且在**两个散文字段
    拼接之后**判定：它们是这次运行里唯一由模型产出的文本，也正是一次检索成功、模型把
    中文原样粘出来时最容易漏水的地方（ADR-0007 的形状）。判定共享、处置按面不同：
    本面没有修复回路，因此命中即换成已本地化的兜底文案，而**数字不被改写**——把
    ``failure_rate`` 改成「暂不可用」等于对外声明数据源当时不可用，那是一句关于数据的
    假陈述。见 ``_unserviceable_contract`` 的 ``keep_numbers``。
    """
    total = aggregate.total
    rate = aggregate.failure_rate if reason_code is None else None
    risk = risk_level(rate, total) if reason_code is None else "unavailable"

    if reason_code is not None:
        main_cause, recommendation = "", ""
    else:
        main_cause, recommendation = _narrative(payload)

    # The language guard's contract payload is a plain string, and the caller
    # proves it is one rather than handing over an `Any`. The proof is not
    # decoration: the shape guard refuses an unproven read, and without it the
    # invariant "what the model wrote is text" would live only in `_narrative`'s
    # implementation — the kind of thing nobody notices breaking until a
    # provider returns a dict and it is serialised into the answer.
    prose: Any = _join_prose(main_cause, recommendation)
    if not isinstance(prose, str):
        return _unserviceable_contract(
            aggregate,
            language,
            reason_code=REASON_PROSE_INVALID,
            site_ids=site_ids,
            window_start=window_start,
            window_end=window_end,
        )
    leaked = bool(prose) and bool(answer_chinese_leak(prose, language))
    if leaked:
        record_answer_language_fallback(language=language, leaked=prose, surface="operations_analysis")
        # The JUDGEMENT is shared with every other surface; the DISPOSITION is
        # this surface's (ADR-0007). There is no repair loop here — the analysis
        # is complete and its numbers are already fixed — so a leak is answered
        # with the localized honest placeholder, and the numeric fields are
        # deliberately left alone: rewriting `failure_rate` to "unavailable"
        # would state something false about the data source.
        return _unserviceable_contract(
            aggregate,
            language,
            reason_code=REASON_PROSE_LEAKED,
            site_ids=site_ids,
            window_start=window_start,
            window_end=window_end,
            keep_numbers=True,
        )

    return {
        "failure_rate": rate,
        "main_cause": main_cause or None,
        "secondary": _secondary(aggregate) if reason_code is None else None,
        "risk": risk,
        "recommendation": recommendation or None,
        "rule_version": RULE_VERSION,
        "reason_code": reason_code,
        "site_ids": list(site_ids) if site_ids is not None else None,
        "window": {
            "start": _iso_z(window_start) if window_start is not None else None,
            "end": _iso_z(window_end) if window_end is not None else None,
        },
        "counts": {
            "total": total if reason_code is None else None,
            "abnormal_ended": aggregate.abnormal_ended if reason_code is None else None,
            "normal_ended": aggregate.normal_ended if reason_code is None else None,
        },
        # 定义随数字一起交付：没有它，`failure_rate` 会被读成「任何异常」。
        "definition": {"abnormal": "order_status=5", "window": "half-open [start, end)"},
    }


def _join_prose(main_cause: Any, recommendation: Any) -> Any:
    """The single string the guard judges — or a non-string, so the caller can refuse it.

    Typed ``Any`` on purpose: the point of the return type is that it may NOT be
    a string, and a signature promising ``str`` would be a promise this function
    cannot keep about provider output.
    """
    parts = [item for item in (main_cause, recommendation) if item]
    if not all(isinstance(item, str) for item in parts):
        return parts
    return "\n".join(parts).strip()


def _unserviceable_contract(
    aggregate: AnalysisAggregate,
    language: str,
    *,
    reason_code: str,
    site_ids: tuple[str, ...] | None,
    window_start: datetime | None,
    window_end: datetime | None,
    keep_numbers: bool = False,
) -> dict[str, Any]:
    """A terminal contract whose prose could not be delivered.

    ``keep_numbers=True`` is the leak case: the aggregate ran and its numbers are
    true, so they are delivered unchanged and only the prose is replaced. The
    default is the case where nothing was computed, and then every numeric field
    is ``None`` with ``risk="unavailable"`` — reporting a rate nobody measured
    would be the same false claim in the opposite direction.
    """
    counted = keep_numbers and aggregate.total > 0
    return {
        "failure_rate": aggregate.failure_rate if counted else None,
        "main_cause": _fallback_prose(language),
        "secondary": _secondary(aggregate) if counted else None,
        "risk": risk_level(aggregate.failure_rate, aggregate.total) if counted else "unavailable",
        "recommendation": None,
        "rule_version": RULE_VERSION,
        "reason_code": reason_code,
        "site_ids": list(site_ids) if site_ids is not None else None,
        "window": {
            "start": _iso_z(window_start) if window_start is not None else None,
            "end": _iso_z(window_end) if window_end is not None else None,
        },
        "counts": {
            "total": aggregate.total if counted else None,
            "abnormal_ended": aggregate.abnormal_ended if counted else None,
            "normal_ended": aggregate.normal_ended if counted else None,
        },
        "definition": {"abnormal": "order_status=5", "window": "half-open [start, end)"},
    }


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
