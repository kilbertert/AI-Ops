"""迁移只做初始化与受控替换：**运营已编辑的提示词不能被静默吞掉**。

`tools/content/upgrade_consumer_flow.py --live-documents` 以前无条件把 answer 节点的
SYSTEM 换成 `consumer_documents.PROMPT`。而运营的提示词就在这个 SYSTEM 里（ADR-0187），
一次迁移就会把它改回去 —— 而且 diff 看起来"模板也是对的"，从评审里看不出来。

契约见 `docs/acceptance/dify-prompt-lifecycle/acceptance.feature` 的「模板只用于初始化与受控迁移」。
"""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

from aiops_diagnostics.consumer.consumer_documents import PROMPT
from consumer.test_consumer_knowledge_picker import picker_graph

ROOT = Path(__file__).resolve().parents[2]


def _module():
    spec = importlib.util.spec_from_file_location(
        "upgrade_consumer_flow", ROOT / "tools/content/upgrade_consumer_flow.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _source(system_text):
    flow = {"workflow": picker_graph(["kb-own"])}
    node = next(n for n in flow["workflow"]["graph"]["nodes"] if n["id"] == "answer")
    node["data"]["prompt_template"] = (
        [{"role": "system", "text": system_text}] if system_text is not None else []
    ) + [{"role": "user", "text": "{{#sys.query#}}"}]
    return flow


def _system(candidate):
    node = next(n for n in candidate["workflow"]["graph"]["nodes"] if n["id"] == "answer")
    return next(p for p in node["data"]["prompt_template"] if p["role"] == "system")["text"]


def test_a_missing_system_prompt_is_initialised():
    """老应用没有 SYSTEM：写入模板，这是初始化，不需要任何人确认。"""
    result = _module().live_documents(_source(None))
    assert _system(result) == PROMPT


def test_an_identical_system_prompt_is_left_alone():
    """内容与模板一致时重复迁移是 no-op —— 幂等，不会因为跑两次就漂。"""
    source = _source(PROMPT)
    before = copy.deepcopy(source)
    result = _module().live_documents(source)
    assert _system(result) == PROMPT
    assert source == before, "迁移不能就地改输入"


def test_an_operator_edit_is_refused_with_the_way_out():
    """运营改过就拒绝，并说清两条路：确认丢弃加开关，要保留就把契约补进去。"""
    edited = "你是 C 端客服。回答要短、要像人说话。\n" + PROMPT
    with pytest.raises(ValueError) as excinfo:
        _module().live_documents(_source(edited))
    message = str(excinfo.value)
    assert "--replace-system" in message
    assert "拒绝覆盖" in message


def test_replacing_is_possible_but_explicit():
    """显式要求替换时才写模板 —— 这是唯一会丢运营内容的入口，必须有人按下它。"""
    result = _module().live_documents(_source("运营自己写的一段"), replace_system=True)
    assert _system(result) == PROMPT


def test_the_refusal_happens_before_any_other_rewrite():
    """拒绝时整份草稿都不动：不能出现"环境变量改了、提示词没改"的半迁移。"""
    edited = "运营自己写的一段"
    source = _source(edited)
    before = copy.deepcopy(source)
    with pytest.raises(ValueError):
        _module().live_documents(source)
    assert source == before
