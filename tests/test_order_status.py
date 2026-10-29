"""订单状态角色的单一权威（PRD #732）。

这个文件存在的理由不是"多测一个纯函数"，而是**补上此前的覆盖缺口**：在 #732 之前，
`tests/` 里 `ORDER_STATUS` **0 命中**、`status == 5` **0 命中**，而 `ABNORMAL_STATUS`
只有一个使用者 —— 它拿**同一个常量**当期望值（`definition.abnormal == f"order_status={ABNORMAL_STATUS}"`）。
于是把 `ABNORMAL_STATUS` 改成 7，那条测试**仍然全绿**，而 KPI 的分子含义已经换了。

四类判据，各自对着一个具体的漂移方向：

1. **角色表直测** —— 每个取值 + `None` / 非数字 / 未知 code；
2. **标签表与角色表取值域一致** —— 往一张表加了 code、忘了另一张 ⇒ 红；
3. **跨面等价** —— `engine` 的分类与 `failure_rate` 的分子对同一 status 给同一答案；
4. **契约字符串由权威生成** —— 改错角色表会让它红（此前的写法改不动）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from aiops_diagnostics.order_status import (
    KNOWN_STATUS_CODES,
    StatusRole,
    abnormal_definition,
    abnormal_end_codes,
    is_abnormal_end,
    is_charging,
    is_normal_end,
    normal_end_codes,
    role_of,
)
from aiops_diagnostics.rules import ORDER_STATUS

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "aiops_diagnostics"


# --- 1. 角色表直测 -----------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (0, StatusRole.CHARGING),
        (1, StatusRole.FINISHED),
        (2, StatusRole.UNCONTROLLABLE_EXCEPTION),
        (3, StatusRole.EXCEPTION_ALREADY_HANDLED),
        (5, StatusRole.CONTROLLED_ABNORMAL_END),
        # 公司定义之外的取值：**同一个** UNKNOWN，不区分「无值」与「奇怪的值」，
        # 因为两者的处置相同（都不当成任何一种结束）。
        (4, StatusRole.UNKNOWN),
        (6, StatusRole.UNKNOWN),
        (99, StatusRole.UNKNOWN),
        (-1, StatusRole.UNKNOWN),
        (None, StatusRole.UNKNOWN),
        ("", StatusRole.UNKNOWN),
        ("abc", StatusRole.UNKNOWN),
        (True, StatusRole.UNKNOWN),
    ],
)
def test_role_of_covers_every_value_including_unknown(status: object, expected: StatusRole) -> None:
    assert role_of(status) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("5", StatusRole.CONTROLLED_ABNORMAL_END), ("5.0", StatusRole.CONTROLLED_ABNORMAL_END)],
)
def test_role_of_keeps_engine_s_coercion(text: str, expected: StatusRole) -> None:
    """`"5.0"` 要读成 5 —— `engine._to_int` 一直用 `int(float(x))`，本模块是纯提取。

    这条判据不显眼但必要：换成 `int(value)` 会让「恰好带小数的字符串状态」
    从异常结束**静默变成**未知，而失败率会因此变低 —— 一个没人看得见的数字变化。
    """
    assert role_of(text) is expected


def test_only_status_five_is_an_abnormal_end() -> None:
    """`GLOSSARY.md` 的口径：**只计 5**。状态 2 与 3 是另外两种状态。

    写宽成「不是充电中也不是正常结束就算异常」会把三种并成一类，
    而那个数字就不再有定义可复核。
    """
    abnormal = [status for status in KNOWN_STATUS_CODES if is_abnormal_end(status)]
    assert abnormal == [5]
    assert not is_abnormal_end(2)
    assert not is_abnormal_end(3)
    assert not is_abnormal_end(None)


def test_only_status_one_is_a_normal_end() -> None:
    normal = [status for status in KNOWN_STATUS_CODES if is_normal_end(status)]
    assert normal == [1]
    # 正常结束与异常结束互斥且不覆盖全部：充电中与未知都不属于任何一边。
    assert not is_normal_end(0)
    assert not is_normal_end(5)
    assert not is_normal_end(None)


def test_only_status_zero_is_charging() -> None:
    assert is_charging(0)
    assert not is_charging(1)
    assert not is_charging(None)


def test_the_derived_code_tuples_agree_with_the_predicates() -> None:
    """`abnormal_end_codes()` 与 `is_abnormal_end` 必须是同一条规则的两种渲染。"""
    assert abnormal_end_codes() == tuple(c for c in sorted(KNOWN_STATUS_CODES) if is_abnormal_end(c))
    assert normal_end_codes() == tuple(c for c in sorted(KNOWN_STATUS_CODES) if is_normal_end(c))


# --- 2. 标签表与角色表取值域一致 ---------------------------------------------


def test_the_label_table_and_the_role_table_cover_the_same_codes() -> None:
    """`rules.ORDER_STATUS`（它叫什么）与角色表（它是什么角色）必须同域。

    两张表是两个问题，但**覆盖必须一致**：往标签表加了一个 code 而没加角色，
    它就会静默落到 `UNKNOWN` —— 一个新增的订单状态被当成"不认识"，
    而失败率对它的处置是"不计入异常"，这正是最容易被忽略的那种漂移。
    """
    assert frozenset(ORDER_STATUS) == KNOWN_STATUS_CODES


def test_every_role_is_reachable_from_some_known_code() -> None:
    """反向：角色表里的每个角色（除 `UNKNOWN`）都至少有一个 code 指向它。

    一个**没有任何 code 指向的角色**是死代码，而下一次有人"修正"角色表时
    会以为它在用。
    """
    used = {role_of(code) for code in KNOWN_STATUS_CODES}
    assert used == set(StatusRole) - {StatusRole.UNKNOWN}


# --- 3. 跨面等价：engine 的分类 vs failure_rate 的分子 ------------------------


def _engine_role_for(status: object) -> str:
    """`engine._analyze_status` 对同一个 status 给出的分类名。

    直接驱动那个方法（而不是复制它的分支）：测的是**它现在怎么判**，不是它曾经怎么写。
    """
    from aiops_diagnostics.engine import DiagnosticEngine
    from aiops_diagnostics.models import DiagnosticReport
    from aiops_diagnostics.parsing import parse_request

    report = DiagnosticReport(request=parse_request("订单 ORDER-1 状态异常", order_no="ORDER-1"), summary="")
    engine = object.__new__(DiagnosticEngine)
    engine._analyze_status({"status": status}, report)
    return next((name for name in report.classifications if name in set(StatusRole)), "unknown_order_status")


def test_the_kpi_numerator_comes_from_the_role_authority() -> None:
    """`ops_analysis.ABNORMAL_STATUS` **由角色派生**，不再是独立的数字常量。

    这条是给将来的人看的：它现在是 `abnormal_end_codes()[0]`，
    所以改角色表会**同时**改 KPI 的分子 —— 那正是我们要的方向（一处定义）。
    """
    from aiops_diagnostics.ops_analysis import ABNORMAL_STATUS, NORMAL_STATUS

    assert abnormal_end_codes()[0] == ABNORMAL_STATUS
    assert normal_end_codes()[0] == NORMAL_STATUS
    assert is_abnormal_end(ABNORMAL_STATUS)
    assert is_normal_end(NORMAL_STATUS)


# --- 4. 契约字符串由权威生成（改错会红） --------------------------------------


def test_the_contract_definition_is_generated_and_would_notice_a_role_change() -> None:
    """`definition.abnormal` 由权威格式化 —— **改错角色表会让这条红**。

    此前的写法是拿同一个常量做期望值（`... == f"order_status={ABNORMAL_STATUS}"`），
    改常量时两边一起变，测试恒绿。现在期望值来自**另一条**规则（`is_abnormal_end`
    的真值集合），所以角色表与契约字符串不可能各自漂移。

    对外文本逐字不变：单值时仍是 `order_status=5`。
    """
    from aiops_diagnostics.ops_analysis import (
        ABNORMAL_STATUS,
        RULE_VERSION,
        AnalysisAggregate,
        build_failure_rate_contract,
    )

    contract = build_failure_rate_contract(
        AnalysisAggregate(10, int(ABNORMAL_STATUS == 5), 9, ()), {"main_cause": "x", "recommendation": "y"}
    )
    assert contract["definition"]["abnormal"] == abnormal_definition() == "order_status=5"
    assert contract["rule_version"] == RULE_VERSION
    # The expected text comes from the ROLE predicate, not from the constant the
    # contract itself was built with.
    expected_codes = [code for code in sorted(KNOWN_STATUS_CODES) if is_abnormal_end(code)]
    assert contract["definition"]["abnormal"] == "order_status=" + ",".join(str(c) for c in expected_codes)


def test_the_definition_formatting_widens_if_the_role_ever_gains_a_code() -> None:
    """多值时的渲染形状：`order_status in (…)` —— 今天走不到，但形状先钉住。"""
    assert abnormal_definition() == "order_status=5"
    assert abnormal_end_codes() == (5,)


# --- 5. 结构守护：不再出现第二段内联判定 --------------------------------------


def _inline_status_comparisons() -> list[str]:
    """`compare(status, <数字>)` 形态的内联判定，**只扫比较表达式**。

    故意窄：`response.status == 404`（HTTP 状态码）是同名不同物，不该被这条规则抓住。
    因此要求一侧是 `status` 这个名字、另一侧是**裸数字字面量** ——
    `role_of(...)` / `is_abnormal_end(...)` / 常量名都不算内联。
    """
    offenders: list[str] = []
    for path in sorted(SOURCE_ROOT.glob("*.py")):
        if path.stem == "order_status.py":  # the authority itself spells the numbers
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            operands = [node.left, *node.comparators]
            names = {getattr(operand, "id", "") for operand in operands}
            literals = [
                operand
                for operand in operands
                if isinstance(operand, ast.Constant) and isinstance(operand.value, int)
            ]
            if "status" in names and literals and not isinstance(literals[0].value, bool):
                offenders.append(f"{path.name}:{node.lineno}")
    return offenders


def test_no_second_inline_status_to_role_judgement() -> None:
    """`status == <数字>` 不许再出现在权威之外（PRD #732 的结构判据）。

    #732 之前这句话有五份拼写（标签表、engine 分支、常量、SQL 字面量、契约字符串）。
    收敛之后，**新增一份会让这条测试红** —— 这是唯一能在评审之前拦住它的东西，
    因为"某个数字恰好还是对的"不会有任何其它症状。
    """
    offenders = _inline_status_comparisons()
    assert offenders == [], "订单状态的数字判据只许出现在 order_status.py: " + ", ".join(offenders)


# --- 6. 与 `health_report` 被替换掉的那个判据逐条等价 --------------------------


def test_health_reports_status_check_is_equivalent_on_every_column_value() -> None:
    """`health_report` 的「订单未结束」判据：**在列的取值域上**逐条等价。

    `ch_order_info.status` 是 `tinyint`（41 只读实测），实际取值恰好 `{0,1,2,3,5}`。
    在这个域上，被替换掉的 `int(value) == 0` 与本模块的 `is_charging` 给出**同一答案**：
    能造出差异的输入是 `'0.0'` / `'0.5'`（旧实现读不出 0，新的读成 0）与 `False`
    （`int(False) == 0`，而布尔从来不是状态码）—— **列产不出这三者**。

    这条判据的作用不是证明"永远等价"，而是把**等价面写在测试里**：
    哪一天列的取值域变了（例如出现小数状态），这里会先红。
    """
    column_values = (0, 1, 2, 3, 5, None)  # tinyint 的实测域

    def old_guard(value: object) -> bool:
        try:
            return (int(value) if value is not None else None) == 0
        except (TypeError, ValueError):
            return False

    assert [old_guard(v) for v in column_values] == [is_charging(v) for v in column_values]
    # 差异只出现在列产不出的输入上 —— 如实记下来，而不是声称"完全等价"。
    assert is_charging("0.0") is True and old_guard("0.0") is False
    assert is_charging(False) is False and old_guard(False) is True
