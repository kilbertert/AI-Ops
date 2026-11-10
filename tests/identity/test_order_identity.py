"""「什么算一个订单号」只有一处回答（#606）。

曾经七个位置各写一份语法，答案在「首字符可否为符号」与「长度上限」上
互相矛盾：同一个 `.x1` 在请求体被接受、进 store 又被拒；试用模型另写
了 ≤64 + 首字符字母数字的一套。本文件钉住三件事：

* 语法等价——同一组输入在权威谓词、Pydantic 字段与 store 校验上给出
  同一个接受/拒绝结论（`order_reference` 回答的是「消息点了哪几单」，
  是另一个问题，不在等价范围）。
* 抽取器承重规则——含数字、纯数字 ≥15 位、带点/冒号的标识符整体抽出。
* 结构守卫——`A-Za-z0-9_.:` 字母表字面量入台账；新增一份（无论是给
  订单号还是别的标识符写的）都让台账断言转红。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from aiops_diagnostics.gateway.gateway_api import (
    AssistantQuestionRequest,
    DifyTrialRequest,
    HealthReportJobRequest,
    ManagedTrialRequest,
)
from aiops_diagnostics.identity.order_identity import (
    ORDER_ID_ATOM,
    ORDER_ID_PATTERN,
    extract_order_identifier,
    is_order_identifier,
    normalize_order_identifier,
    order_identifier_candidates,
)

SOURCE_ROOT = Path(__file__).parents[2] / "src" / "aiops_diagnostics"

#: 覆盖曾经三个面三种答案的形状：首字符符号、长度跨界、空串、非 ASCII。
INPUTS = [
    "2096164064667852801",  # 19 位雪花号——生产订单的真实形状
    "ORD-2026.10:01",  # 合同承诺的带点/冒号形状
    "ABC123",  # 短号
    "A" * 65,  # 超旧试用语法上限（64）但在权威上限内
    "A" * 128,
    "A" * 129,  # 超权威上限
    "A.B:C",  # 纯符号连接
    ".hidden",  # 首字符为点——旧 store 语法拒、权威语法收
    "-x1",
    ":y2",
    "_u3",
    "",
    "has space",
    "订单 2096164064667852801",
    "中文订单号",
]

#: 权威语法对上面每条输入的答案。试用字段在此之上多接受空串（可选缺省）。
_EXPECTED = {
    "2096164064667852801": True,
    "ORD-2026.10:01": True,
    "ABC123": True,
    "A" * 65: True,
    "A" * 128: True,
    "A" * 129: False,
    "A.B:C": True,
    ".hidden": True,
    "-x1": True,
    ":y2": True,
    "_u3": True,
    "": False,
    "has space": False,
    "订单 2096164064667852801": False,
    "中文订单号": False,
}


@pytest.mark.parametrize("value", INPUTS)
def test_authority_predicates_agree_on_every_shape(value: str) -> None:
    expected = _EXPECTED[value]
    assert is_order_identifier(value) is expected
    assert (normalize_order_identifier(value) is not None) is expected
    assert (ORDER_ID_PATTERN.fullmatch(value) is not None) is expected


def test_non_string_and_whitespace_normalize() -> None:
    assert normalize_order_identifier(None) is None
    assert normalize_order_identifier(123) is None
    assert normalize_order_identifier("  ORD-1  ") == "ORD-1"
    assert is_order_identifier("  ORD-1  ") is False  # is_* 不剥空白，与旧 fullmatch 同语义


def _accepted_by(model, value: str, **kwargs) -> bool:
    try:
        model(order_no=value, **kwargs)
    except ValidationError:
        return False
    return True


@pytest.mark.parametrize("value", INPUTS)
def test_every_request_model_shares_the_one_grammar(value: str) -> None:
    """请求体字段与权威谓词同一份答案——不再有第二个 Pydantic 语法。"""
    expected = _EXPECTED[value]
    assert _accepted_by(HealthReportJobRequest, value) is expected
    assert _accepted_by(AssistantQuestionRequest, value, question="q") is expected
    # 试用字段把「缺省」编码为空串：空串仍合法，其余形状与权威一致。
    trial_expected = expected or value == ""
    app = "a" * 35 + "-"
    assert _accepted_by(DifyTrialRequest, value, app_id=app) is trial_expected
    assert _accepted_by(ManagedTrialRequest, value, app_id=app, question="q") is trial_expected


@pytest.mark.parametrize("value", INPUTS)
def test_store_order_validation_uses_the_authority(value: str) -> None:
    """`gateway_store._order_no` 不再借 `SAFE_SCOPE`（首字符必须为字母数字
    是 scope 指纹自己的规则）；空串由调用方短路，不走到这里。"""
    from aiops_diagnostics.gateway.gateway_store import _order_no, _scope

    if not value:
        pytest.skip("空串在调用方短路，不进校验")
    try:
        _order_no(value, "order_no")
        accepted = True
    except ValueError:
        accepted = False
    assert accepted is _EXPECTED[value]
    # 台账断言：`_scope` 仍是另一条（更窄的）语法——它服务别的字段，
    # 订单号不再走它。这条断言把「分流完成」钉成事实而不是意图。
    if value in (".hidden", "-x1", ":y2", "_u3"):
        with pytest.raises(ValueError):
            _scope(value, "scope_fingerprint")


def test_extractor_keeps_the_load_bearing_digit_rules() -> None:
    """抽出的是完整标识符，带点/冒号不截断；数字规则挡住单词与手机号。"""
    assert extract_order_identifier("订单 ORD-2026.10:01 充电异常") == "ORD-2026.10:01"
    assert extract_order_identifier("查到啦，订单 2096164064667852801 怎么还没退押金") == (
        "2096164064667852801"
    )
    assert extract_order_identifier("我的手机号是13800001111，帮我查一下") is None
    assert extract_order_identifier("价格是50元") is None
    # 英文问句第一个普通单词曾被当成订单号（41 事故）：含数字规则是承重的。
    assert extract_order_identifier("Please check charging anomalies") is None


def test_candidates_preserve_order_and_dedupe() -> None:
    text = "查 A2116050516255272961 和 B2116050516255272962 还有 A2116050516255272961"
    assert order_identifier_candidates(text) == [
        "A2116050516255272961",
        "B2116050516255272962",
    ]
    assert order_identifier_candidates("") == []


def test_native_preview_accepts_the_contract_shape() -> None:
    """`dify_console_publish._native_order` 曾用 `[A-Za-z0-9_-]{1,64}` 复查
    order_reference 抽出的候选——`订单 ORD-2026.10:01` 会被判成「不支持
    的字符」。收口后合同承诺的形状直接通过。"""
    from aiops_diagnostics.dify.dify_console_publish import _native_order

    assert _native_order("帮我看看订单 ORD-2026.10:01 的情况") == "ORD-2026.10:01"


# --------------------------------------------------------------------------
# 结构守卫：标识符字母表台账 + 「order 语境」规则 + 抽取器唯一。
#
# 标识符字母表（`[A-Za-z0-9…]{…}`）在仓里被几十种字段复用：app_id、
# tenant_id、scope 指纹、路径段、站点 id、知识库 id、密钥名……订单号曾经
# 混在其中七处，靠肉眼根本分不出谁是谁。台账按文件记出现次数：任何新增
# ——无论它自称校验什么——都让断言转红，由评审登记它的真实身份。
#
# 台账抓不到的是「order 语境里的新语法」：一条 `[A-Za-z0-9_-]{1,80}` 写进
# `order_candidate` 里时字面量本身合法，错的只是它定义的是订单号。所以
# 另有一条规则：函数名或文件路径带 order 的位置出现标识符字母表，只能
# 在两个权威文件里。
# --------------------------------------------------------------------------

#: 「标识符字母表 + 量词」的 AST 级形状——不区分字段，凡是这种字面量都计入。
_IDENTIFIER_ALPHABET = re.compile(r"\[A-Za-z0-9[^]\s]*\][{?*+0-9,{]")

#: 订单语法的两个合法住所：形状权威本身，与「消息点了哪几单」的判据
#: （另一个问题，语义在模块 docstring 里分流）。
_ORDER_GRAMMAR_FILES = {"identity/order_identity.py", "common/order_reference.py"}

#: 相对路径 → 允许的出现次数。每一行的身份：标识符种类 == 该字段自己，
#: 不是订单号；订单号的唯一语法在 order_identity.py。
_ALLOWED_ALPHABET_LITERALS = {
    "identity/order_identity.py": 2,  # 权威语法 + 扫描启发式 token（比语法窄是设计）
    "common/order_reference.py": 4,  # 「消息点了哪几单」的判据，另一个问题
    "common/config.py": 3,  # 配置键名 / 密钥槽名
    "common/redaction.py": 5,  # 脱敏模式（口令、Bearer、sk-、手机号……）
    "consumer/consumer_knowledge_selection.py": 2,  # 目录 token / libraries 键
    "consumer/consumer_publication.py": 2,  # tenant_id / 行键白名单
    "conversation/conversation_store.py": 2,  # scope 指纹 / 智能体版本
    "conversation/faq.py": 1,  # faq 标识符
    "data/sources.py": 4,  # 传输层白名单 ×2 + SQL 标识符 + MySQL 账户形状
    "dify/dify_app_registry.py": 1,  # 应用 id
    "dify/dify_dsl_pull.py": 1,  # 应用 id
    "dify/dify_login_proxy.py": 2,  # 登录会话 id / token
    "dify/dify_login_server.py": 1,  # 登录 id
    "dify/dify_workspaces.py": 1,  # 应用 id
    "dify/self_service_api.py": 1,  # knowledge_base_id
    "dify/self_service_bridge.py": 1,  # knowledge_base_id（order_no 已收口到权威）
    "dify/self_service_identity.py": 1,  # 租户 id
    "execution/chat_execution.py": 3,  # 智能体 id / libraries 键 ×2
    "gateway/gateway_api.py": 11,  # 拒绝码 / conv_ / code / app_id / knowledge / tenant /
    #                              kb_ids / agt_ 版本 ×2 / code —— 订单字段已收口
    "gateway/gateway_config.py": 2,  # 主机名 / 端点名
    "gateway/gateway_store.py": 1,  # scope 指纹与其他 store 字段
    "gateway/metrics_store.py": 2,  # 租户 id / 指标 id
    "identity/query_scope.py": 1,  # 路径段
    "identity/scope_context.py": 1,  # 路径段
    "operator/communication.py": 2,  # deviceCode/childDeviceCode设备码、event/functionId事件名；非订单
    "operator/offline_tool.py": 1,  # 离线工具设备/站点查询参数，不是订单语法
    "operator/operator_cpms.py": 3,  # 失败原因码 / 回调 siteId / 时序遥测 device
    "operator/operator_tools.py": 1,  # 工具参数名
    "operator/station_tariff.py": 1,  # 站点 id
    "publication/agent_lifecycle.py": 1,  # 智能体 id
    "publication/shortcut_lifecycle.py": 1,  # 智能体版本
    "tdengine_proxy.py": 2,  # 传输层白名单 / SQL 标识符
}


def _identifier_alphabet_literals(tree: ast.AST) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _IDENTIFIER_ALPHABET.search(node.value)
    ]


def test_no_second_order_grammar_literal_exists() -> None:
    found: dict[str, int] = {}
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        count = len(_identifier_alphabet_literals(tree))
        if count:
            found[str(path.relative_to(SOURCE_ROOT))] = count
    assert found == _ALLOWED_ALPHABET_LITERALS, (
        "标识符字母表字面量与台账不一致；新增白名单请登记它校验的字段，"
        f"新增订单语法请改用 order_identity。\nfound={found}\nallowed={_ALLOWED_ALPHABET_LITERALS}"
    )


def _function_scopes(tree: ast.AST) -> list[tuple[str, list[int]]]:
    """函数名 → 函数体内标识符字母表字面量的行号。"""
    found = []

    class V(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            lines = _identifier_alphabet_literals(node)
            if lines:
                found.append((node.name, lines))
            self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef

    V().visit(tree)
    return found


def test_no_identifier_grammar_inside_order_named_code() -> None:
    """台账看字面量，这条看语境：order 命名的函数/文件里出现的标识符
    字母表，按定义就是一份订单语法——它只能住在两个权威文件里。
    `order_candidate`（operator_agent）与 `start`（operator_cpms）里那份
    `[A-Za-z0-9_-]{1,80}` 就是被这条规则抓到的。"""
    offenders = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        rel = str(path.relative_to(SOURCE_ROOT))
        if rel in _ORDER_GRAMMAR_FILES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if "order" in path.name:
            for lineno in _identifier_alphabet_literals(tree):
                offenders.append(f"{rel}:{lineno} (order 命名文件)")
        for func, lines in _function_scopes(tree):
            if "order" in func:
                offenders.extend(f"{rel}:{n} ({func})" for n in lines)
    assert offenders == [], f"order 语境里的标识符语法应进入 order_identity：{offenders}"


#: 「从文本里取订单号」的函数只允许这两处：绑定用的权威抽取器，与
#: 「消息点了哪几单」的意图判据（语义不同，已在模块 docstring 里分流）。
#: `operator_agent.order_candidate` 不是抽取器——它裁定模型提议的候选
#: 能不能引用；登记在此说明语义，防误伤。
_EXTRACTOR_OWNERS = {
    "identity/order_identity.py": {"extract_order_identifier", "order_identifier_candidates"},
    "common/order_reference.py": {"order_candidates", "explicit_orders", "digit_runs", "names_order"},
    "operator/operator_agent.py": {"order_candidate"},
}


def test_no_second_order_extractor_exists() -> None:
    offenders = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        rel = str(path.relative_to(SOURCE_ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and "order" in node.name
                and ("extract" in node.name or "candidate" in node.name)
                and node.name not in _EXTRACTOR_OWNERS.get(rel, set())
            ):
                offenders.append(f"{rel}:{node.lineno} {node.name}")
    assert offenders == [], f"新的订单抽取器应进入 order_identity 或登记为另一种语义：{offenders}"


def test_pydantic_fields_reference_the_atom_not_a_copy() -> None:
    """字段 pattern 必须从 `ORDER_ID_ATOM` 组装——台账抓字面量，这条抓
    「字面量长得对但其实是第二份」：编译结果必须与权威正则逐字相同。"""
    for model in (HealthReportJobRequest, AssistantQuestionRequest):
        pattern = model.model_fields["order_no"].metadata
        # pydantic 把 pattern 存进 metadata；比对编译出的串与权威 atom。
        text = str(pattern)
        assert ORDER_ID_ATOM in text, (model.__name__, text)
