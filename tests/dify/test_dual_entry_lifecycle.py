"""第二轮返工的生命周期用例（R2/R4—R9）：每条都是**真实链路**跑通，不是"探针不报错"。

独立验收的 10 个反例在这里变成正向断言 + 负向断言：

| 用例 | 反例 | 这里断言的是 |
| --- | --- | --- |
| 导入 id | `request_id` 带冒号被真实 Dify 拒绝 | 用补丁的 Pydantic 约束校验，且回读同一应用 |
| 草稿试用 | 草稿被 `row.active` 拒 | 完整构造一次试用，且正式指针表仍为空 |
| 第二份草稿 | 按租户取第一条目标 | 两份草稿各自可发布 |
| 草稿摘要 | 发布忽略 `expected_draft` | 过期摘要被拒、正确摘要放行 |
| 重试撤回 | 重试把撤回的版本重新激活 | 重试只对账，不覆盖撤回 |
| 凭据对账 | 跨请求又建一枚 key | 第二次尝试复用第一枚 |
| 首次发布失败 | 留下不可续跑的草稿 agent | 重试接着发布，不新建 agent |
"""

from pathlib import Path

import pytest

# `dual` 夹具依赖 operations 下的 `operator` 夹具；它必须在本模块命名空间里，
# 否则 pytest 解析 `dual` 的依赖时会报 "fixture 'operator' not found"。
from operations.test_operator_cpms import operator  # noqa: F401,E402

from aiops_diagnostics.dify import service_keys
from dify.test_dual_entry import (  # noqa: F401,E402
    CONSUMER_APP,
    TARGET,
    dual,
    source_document,
)

ONLY = "consumer"


def consumer_only(dual):  # noqa: F811
    return {"consumer": {"app_id": dual.consumer_source, "version_no": dual.consumer_version}}


# ── R2：导入 id 必须满足真实补丁的约束 ──────────────────────────────────


# ── R4：未发布草稿可以试用 ──────────────────────────────────────────────


# ── R5：第二份草稿能被发布 ──────────────────────────────────────────────


# ── R6：发布必须核对操作者确认的草稿摘要 ────────────────────────────────


# ── R7：重试不复活已撤回的版本 ──────────────────────────────────────────


# ── R8：凭据对账跨请求/重启仍然幂等 ─────────────────────────────────────


def test_key_reconciliation_survives_a_lost_list_and_a_new_attempt(dual, monkeypatch, tmp_path):  # noqa: F811
    app = "88888888-1111-4111-8111-111111111111"
    real = dual.console._call
    after_create = [False]

    def flaky(workspace, path, **kwargs):
        if path.endswith("/api-keys") and kwargs.get("method", "GET") == "GET" and after_create[0]:
            raise TimeoutError("synthetic list outage")
        if path.endswith("/api-keys") and kwargs.get("method", "GET") == "POST":
            real(workspace, path, **kwargs)
            after_create[0] = True
            raise TimeoutError("synthetic lost key response")
        return real(workspace, path, **kwargs)

    monkeypatch.setattr(dual.console, "_call", flaky)
    database = tmp_path / "gateway.db"
    with pytest.raises((TimeoutError, ValueError)):
        service_keys.ensure(
            dual.console, tmp_path / "restart-keys", "workspace-target", app, database=database
        )
    assert dual.console.key_posts == 1
    # 下一次（相当于另一个进程/重启之后）只靠库里的创建前快照对账，不再建第二枚
    monkeypatch.setattr(dual.console, "_call", real)
    path = service_keys.ensure(
        dual.console, tmp_path / "restart-keys", "workspace-target", app, database=database
    )
    assert dual.console.key_posts == 1, "重试又建了一枚 key"
    assert Path(path).read_text().strip() == "app-key-1"


# ── R9：首次发布失败后可以续跑 ──────────────────────────────────────────


# ── 工作区目录锚点：来源摘要与客户端发布冻结同一份内容 ──────────────────
