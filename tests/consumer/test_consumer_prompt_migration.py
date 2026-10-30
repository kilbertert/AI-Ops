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


def _source(system_texts):
    flow = {"workflow": picker_graph(["kb-own"])}
    node = next(n for n in flow["workflow"]["graph"]["nodes"] if n["id"] == "answer")
    node["data"]["prompt_template"] = [{"role": "system", "text": text} for text in system_texts] + [
        {"role": "user", "text": "{{#sys.query#}}"}
    ]
    return flow


def _systems(candidate):
    node = next(n for n in candidate["workflow"]["graph"]["nodes"] if n["id"] == "answer")
    return [p["text"] for p in node["data"]["prompt_template"] if p["role"] == "system"]


def test_a_missing_system_prompt_is_initialised():
    """老应用没有 SYSTEM：写入业务起点 + 协议块。"""
    texts = _systems(_module().live_documents(_source([])))
    assert len(texts) == 2
    assert "aiops-protocol" in texts[1]
    assert "充电客服" in texts[0]


def test_an_identical_prompt_is_left_alone():
    """重复迁移是幂等的：协议块只有一份，业务文字不变。"""
    once = _systems(_module().live_documents(_source([PROMPT])))
    twice = _systems(_module().live_documents(_source(once)))
    assert once == twice


def test_an_operator_edit_is_preserved_not_refused():
    """**运营的文字一个字都不动**，协议块由系统替换。

    这是复核要求的关键改动：第一版的做法是"拒绝覆盖，让运营手补协议，或用 --replace-system
    丢弃内容"。两者都不算完成保留式迁移 —— 保留式的意思是**迁移后运营的文字仍在**。
    """
    edited = "你是 C 端客服。回答要短、要像人说话。"
    texts = _systems(_module().live_documents(_source([edited])))
    assert texts[0] == edited
    assert "aiops-protocol" in texts[1]


def test_a_stale_protocol_block_is_refreshed_and_kept_single():
    """旧协议块被替换成当前版本，不会叠成两块。"""
    stale = "<!-- aiops-protocol:1 -->\n旧协议：只输出 kind/answer/reference_ids。"
    texts = _systems(_module().live_documents(_source(["运营的文字", stale])))
    assert texts[0] == "运营的文字"
    assert len(texts) == 2
    assert "aiops-protocol:2" in texts[1] and "旧协议" not in texts[1]


def test_the_fact_contract_flag_is_written():
    """片段契约是**发布版本**的属性：迁移写入开关，旧版本因此停在 4。"""
    candidate = _module().live_documents(_source(["运营的文字"]))
    variables = {v["name"]: v["value"] for v in candidate["workflow"]["environment_variables"]}
    assert variables["answer_fact_contract"] == "slots"


def test_the_input_is_not_mutated_in_place():
    source = _source(["运营的文字"])
    before = copy.deepcopy(source)
    _module().live_documents(source)
    assert source == before
