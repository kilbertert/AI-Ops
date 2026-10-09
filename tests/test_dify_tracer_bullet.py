"""最小端到端竖切（#582）：Dify 导出 → 拉取 → 发布 → 一条提问被服务。

这不是又一次单元测试，而是**对同一次可复现调用**的回归。`tools/dify_vertical_slice.py`
是 #582 要求的"脚本化调用"，它自己驱动**真**网关 HTTP 面；这里断言那次调用真的跑通了，
并把它落到证据文件里的四个判据逐条读回来 —— 否则一个被改坏的工具会安静地"通过"。

被替换的只有模型会话（脚本化两轮回答）；其余是真的：平台入口判定、选 agent、有界检索闸、
blocks-v1 校验、作业持久化、指标落库、HTTP 路由。因此本文件的覆盖边界与工具一致：
**能**证明契约与接线，**不能**证明模型质量与真实 Dify 可达。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _load_tool():
    """按路径导入 —— `tools/` 不是包（先例：`tests/test_derive_zh_hant_tool.py`）。"""
    spec = importlib.util.spec_from_file_location(
        "dify_vertical_slice", ROOT / "tools" / "dify_vertical_slice.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _run(tmp_path: Path) -> dict:
    tool = _load_tool()
    out = tmp_path / "evidence"
    code = tool.main(["--out", str(out)])
    assert code == 0, f"竖切未通过：exit={code}；证据 {out}"
    return json.loads((out / "dify-vertical-slice.json").read_text(encoding="utf-8"))


def test_the_whole_chain_runs_and_the_answer_comes_from_the_published_version(tmp_path: Path) -> None:
    """一次调用跑完四跳，且**回答确实出自那个已发布版本**。

    关键的一条是最后那句断言：作业被 `agent_version_key` 记录成刚发布的版本号，
    不是"某条路回了话"。没有它，一个没走 RAG 的回答也能让前面几条看起来全绿。
    """
    evidence = _run(tmp_path)

    # 跳 1+2：DSL 拉回并被我们的发布门冻成一个版本。
    published = evidence["published_version"]
    assert published["version_no"] == 1
    assert published["agent_version"].endswith("#v1")

    # 跳 4：一条真实提问经网关 HTTP 面被服务，返回体形状与今天一致。
    assert evidence["answer"]["status"] == "completed"
    assert evidence["answer"]["retrieval_status"] == "found"
    assert evidence["answer"]["block_kinds"] == ["text", "reference"]

    # 检索确实发生过，且用的是**这个版本绑定的**知识库集合。
    assert evidence["kb_search_calls"], "一次知识检索都没发生"
    assert evidence["kb_search_calls"][0][0] == published["knowledge_base_ids"]

    served = [row["agent_version_key"] for row in evidence["metrics_runs"]]
    assert published["agent_version"] in served, f"回答不是这个版本服务的：{served}"


def test_the_mapping_is_declared_not_inferred(tmp_path: Path) -> None:
    """本票的租户映射是**显式输入**（姊妹票 #584 才让它入库持有）。"""
    evidence = _run(tmp_path)
    mapping = evidence["mapping"]
    assert mapping["tenant_id"] and mapping["business_entry"] and mapping["agent_name"]
    assert "not inferred" in mapping["declared"]


def test_repull_and_republish_makes_a_new_version_and_leaves_v1_alone(tmp_path: Path) -> None:
    """草稿→发布→已发布快照的不可变性：再拉再发布产生新版本，旧版本不被就地改写。"""
    evidence = _run(tmp_path)
    assert evidence["republish"]["version_no"] == 2
    assert evidence["republish"]["v1_snapshot_prompt_head"] == evidence["published_version"]["prompt_head"]


def test_the_evidence_states_what_it_did_not_prove(tmp_path: Path) -> None:
    """一份不说边界的证据会被读成"全验过了"，这正是要防的假陈述。"""
    evidence = _run(tmp_path)
    unproven = " ".join(evidence["not_proven"])
    assert "model" in unproven and "36" in unproven and "#587" in unproven
    assert "SDKCodexSession" in evidence["model_substitution"]
