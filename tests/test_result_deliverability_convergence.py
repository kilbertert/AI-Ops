"""交付判据的源码级守护（#1443）。

拦截的具体错误：某个交付面「忘了判窗口」，或者把 ``state == "succeeded"`` 与
``expires_at`` 手写组合 —— 这类失效是**静默的**：判错了不会抛异常，只是过期答案
照样交付、过期历史照样进下一轮。判据来自 issue #1443 的 Solution d。

既有检查（单测）只覆盖被调用的那几条路径，无法发现「新加的交付面根本没调谓词」，
所以这条规则必须扫源码本身。
"""

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "aiops_diagnostics"
#: 谓词的定义处：判据只允许出现在这里。
DEFINITION_MODULE = "run_deliverability.py"
#: 出现即说明该作用域走了收敛后的判据，不算「仅凭行状态」。
PREDICATE_NAMES = frozenset({"deliverable", "expired", "still_valid"})


def _python_sources():
    return sorted(path for path in SOURCE_ROOT.rglob("*.py") if path.name != DEFINITION_MODULE)


def _scoped_nodes(function):
    """函数自身的节点，**不下降进嵌套函数/lambda** —— 那是另一个作用域，自己负责。"""
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _compares_state_to_succeeded(nodes):
    for node in nodes:
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Compare):
                continue
            for comparator in sub.comparators:
                left = sub.left
                if not (isinstance(left, ast.Attribute) and left.attr == "state"):
                    continue
                if isinstance(comparator, ast.Constant) and comparator.value == "succeeded":
                    return True
                if isinstance(comparator, ast.Name) and comparator.id == "SUCCEEDED":
                    return True
    return False


def _touches_window(nodes):
    return any(
        isinstance(sub, ast.Attribute) and sub.attr == "expires_at"
        for node in nodes
        for sub in ast.walk(node)
    )


def _uses_predicate(nodes):
    return any(
        isinstance(sub, ast.Name) and sub.id in PREDICATE_NAMES for node in nodes for sub in ast.walk(node)
    )


def _calls_by_qa(nodes):
    return any(
        isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "by_qa"
        for node in nodes
        for sub in ast.walk(node)
    )


def test_no_hand_written_state_and_window_combination():
    """``state == "succeeded"`` 与 ``expires_at`` 不得在同一个布尔表达式里手写组合。"""
    offenders = []
    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.BoolOp):
                continue
            parts = list(node.values)
            if _compares_state_to_succeeded(parts) and _touches_window(parts):
                offenders.append(f"{path.relative_to(SOURCE_ROOT)}:{node.lineno}")
    assert not offenders, (
        "交付判据只能由 execution/run_deliverability.py 回答（#1443）："
        "把手写的 state/expires_at 组合换成 deliverable()。命中：" + ", ".join(offenders)
    )


def test_by_qa_call_sites_do_not_decide_delivery_from_state_alone():
    """``by_qa()`` 之后仅凭 ``state`` 决定交付、又不引用窗口判据的调用点，一律不许存在。"""
    offenders = []
    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = list(_scoped_nodes(node))
            if not _calls_by_qa(body) or not _compares_state_to_succeeded(body):
                continue
            if not _uses_predicate(body) and not _touches_window(body):
                offenders.append(f"{path.relative_to(SOURCE_ROOT)}:{node.lineno} {node.name}")
    assert not offenders, (
        "by_qa() 之后只用 state 决定交付是静默失效（#1443）："
        "改用 deliverable()。命中：" + ", ".join(offenders)
    )
