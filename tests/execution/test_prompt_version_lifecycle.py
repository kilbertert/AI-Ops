"""提示词版本的闭环：草稿预览不动正式版，发布推进版本，恢复回到旧版，历史运行不改。

这一片只钉**提示词这一维**：既有用例已经证明发布/撤回/恢复的指针语义
（`tests/dify/test_dify_publish_api.py`、`tests/execution/test_node_publication.py`），
这里补的是"提示词文本本身在哪一步生效"。

契约见 `docs/acceptance/dify-prompt-lifecycle/acceptance.feature` 的「草稿、预览、发布与恢复」。
"""

from __future__ import annotations

import json

from aiops_diagnostics.execution import chat_execution, node_publication
from execution.test_app_runtime_dify_entry import _Resolver
from execution.test_node_publication import APP, publication, publish  # noqa: F401 - fixture


def _preview(f):
    """预览冻结的是**页面当前内容**（这里是 DSL 导出里的草稿提示词），不推进正式指针。

    运营页那条路走的是 `operator_preview`（未保存的编辑框内容），在
    `tests/operations/test_operator_cpms.py` 已单独钉住；这里用节点发布同一条
    `candidate()` 证明同一件事：草稿能生成清单，但版本与绑定不动。
    """
    return json.loads(node_publication.candidate(f["source"], f["target"]))


def test_a_draft_preview_does_not_change_the_published_prompt(publication):  # noqa: F811
    """LC-01：预览用草稿，正式仍用发布版。"""
    f = publication
    published = publish(f, 0)
    assert published is not None and "VERSION_ONE" in published.manifest_text

    f["candidate"]["prompt"] = "VERSION_TWO"
    draft = _preview(f)
    assert draft["system_messages"][0].startswith("VERSION_TWO")

    # 预览之后，正式绑定指向的清单仍是 A —— 页面上的试跑不能顺带改版。
    still = chat_execution.select_binding(f["settings"], _Resolver().resolve("token-a", required_scope="x"))
    assert "VERSION_ONE" in still.manifest_text and "VERSION_TWO" not in still.manifest_text
    assert still.agent_version_no == published.agent_version_no


def test_publishing_the_edited_prompt_advances_the_version(publication):  # noqa: F811
    """LC-02：发布把编辑后的提示词冻结成新版本，绑定摘要跟着变。"""
    f = publication
    first = publish(f, 0)
    f["candidate"]["prompt"] = "VERSION_TWO"
    second = publish(f, first.route_revision)
    assert first is not None and second is not None
    assert second.agent_version_no != first.agent_version_no
    assert "VERSION_TWO" in second.manifest_text
    assert second.manifest_text != first.manifest_text


def test_restoring_the_old_version_brings_the_old_prompt_back(publication):  # noqa: F811
    """LC-04：恢复是**指回旧版本**，不重新渲染、也不悄悄回退成模板。"""
    from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore

    f = publication
    first = publish(f, 0)
    assert first is not None
    old_manifest = first.manifest_text
    f["candidate"]["prompt"] = "VERSION_TWO"
    second = publish(f, first.route_revision)
    assert second is not None

    store = ChatRouteBindingStore(f["settings"].chat_route_bindings_file)
    current = store.get("env-a", "TENANT-A", "consumer", "consumer_own_order_status")
    store.publish(
        environment="env-a",
        tenant_id="TENANT-A",
        entry="consumer",
        scenario="consumer_own_order_status",
        engine="node",
        agent_id=second.agent_id,
        agent_version_no=first.agent_version_no,
        app_id=APP,
        node_config_file=f["target"].config_file,
        expected_revision=current.revision,
    )
    restored = store.get("env-a", "TENANT-A", "consumer", "consumer_own_order_status")
    assert restored.agent_version_no == first.agent_version_no
    # 旧版本的内容没有被重写：恢复用的是那份不可变快照本身。
    version = f["manager"].store.version(first.agent_id, "TENANT-A", first.agent_version_no)
    assert version.snapshot["node_manifest"] == old_manifest
    assert "VERSION_ONE" in old_manifest
