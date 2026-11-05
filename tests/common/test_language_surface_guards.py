"""语言集合只能由清单派生 —— 结构性守卫（#1371）。

`GLOSSARY.md` 的语言清单是单一事实来源；消费方不得自行枚举语言。这条守卫
盯住两种曾经把「语言集合」写散的形状：

* `language.startswith("zh")` —— 每处都在自行决定「中文家族」是什么
  （清单上的 `is_chinese` 才是答案）。当前 `src/` 下应一处不剩。
* 由 zh 家族成员组成的字面集合/元组字面量 —— 第二份语言枚举。
  白名单是台账：既存的合法枚举（命名的常量、不同概念的判据集合）
  逐条登记，白名单只许减少，新增一处即红。

仿 `tests/gateway/test_terminal_write_guards.py` 的 AST 做法：查源码而不是
查行为，因为「少了的一处折叠」在任何行为测试里都不可见。
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[2] / "src" / "aiops_diagnostics"
ZH_FAMILY = frozenset({"zh", "zh-Hant", "en"})

#: (相对路径, 排序后的成员) → 允许的出现次数。台账本身也是断言对象：
#: 收敛动作删掉一处时，台账必须跟着删，否则会掩盖「又写回来一份」。
_ALLOWED_ENUMERATIONS = {
    # 具名权威本身的定义 —— 消费方引用 `COMPOSED_LANGUAGES`，不复制它。
    ("consumer/consumer_copy.py", ("en", "zh")): 1,
    # 请求观测的候选判定集合：这是「该入口候选哪种语言」的判定，不是文案表 —
    # 与文案回退是不同概念，收敛到 `COMPOSED_LANGUAGES` 会把两种语义压成一摊。
    ("gateway/request_observation.py", ("en", "zh", "zh-Hant")): 1,
}


def _zh_family_literal_members(tree: ast.AST) -> list[tuple[int, tuple[str, ...]]]:
    """字面集合/元组里成员全部落在 zh 家族且至少两个的 (行号, 成员) 列表。"""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Set, ast.Tuple)):
            members = [
                elt.value for elt in node.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            ]
            if len(members) == len(node.elts) >= 2 and set(members) <= ZH_FAMILY:
                found.append((node.lineno, tuple(sorted(members))))
    return found


def _zh_startswith_calls(tree: ast.AST) -> list[int]:
    """`.startswith("zh")` / `.startswith('zh')` 调用的行号。"""
    lines = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "startswith"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in ("zh", "zh-")
        ):
            lines.append(node.lineno)
    return lines


def _source_files() -> list[Path]:
    return sorted(SOURCE_ROOT.rglob("*.py"))


def test_no_zh_family_fold_remains() -> None:
    offenders = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno in _zh_startswith_calls(tree):
            offenders.append(f"{path.relative_to(SOURCE_ROOT)}:{lineno}")
    assert offenders == [], f"新的 zh 家族折叠应读 `is_chinese`/`fallback` 声明：{offenders}"


def test_language_enumerations_match_the_ledger() -> None:
    found: dict[tuple[str, tuple[str, ...]], int] = {}
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = str(path.relative_to(SOURCE_ROOT))
        for _, members in _zh_family_literal_members(tree):
            key = (rel, members)
            found[key] = found.get(key, 0) + 1
    assert found == dict(_ALLOWED_ENUMERATIONS), (
        "字面语言集合与台账不一致；新增枚举请改用清单常量并收窄台账。\n"
        f"found={found}\nallowed={dict(_ALLOWED_ENUMERATIONS)}"
    )
