"""「这个智能体现在还有没有版本在服务」——**唯一判据**。

同一条规则此前写在四处、给出两种方向相反的答案（#922）：

- 客服问答选择器（`qa_rag.select_customer_agent`）与媒体活性
  （`gateway_runtime._is_active`）都要求 ``status == "published"``；
- 而 `fork_draft` **刻意保留** ``published_version``（`agent_lifecycle.py` 的 UPDATE
  只改 ``status``），词条也写着「草稿编辑不改变正在运行的已发布版本」
  （`GLOSSARY.md:153`）。

于是「打开草稿编辑」在数据层没有中断任何东西，在服务层却同时切断了线上问答与
已签发媒体的回放 —— 一次编辑变成一次下线，而文档说不会。

判据是 ``published_version`` 这个指针，不是 ``status``：

============  =================  ===================
status        published_version  live_version
============  =================  ===================
published     1                  1
draft         1                  1   ← 编辑中，线上仍是这一版
disabled      1                  None ← 停用，立即停服
任意          NULL               None ← 从未发布
============  =================  ===================

本模块只含纯函数与冻结值（沿用 `rules.py` / `order_visibility.py` 的形态），
不含类层次、运行时状态或第三方依赖。
"""

from __future__ import annotations

from typing import Protocol

#: 允许「指针仍指向线上版本」的状态。``disabled`` 不在此列：停用的语义是立刻停服。
LIVE_STATUSES = frozenset({"published", "draft"})


class _Serviceability(Protocol):
    status: str
    published_version: int | None


def live_version(agent: _Serviceability) -> int | None:
    """这个智能体**现在**在服务的版本号；没有则 ``None``。"""
    if agent.status not in LIVE_STATUSES:
        return None
    return agent.published_version


def serves(agent: _Serviceability, version_no: int) -> bool:
    """这个**具体版本**还能不能服务（选择器问的问题）。"""
    return live_version(agent) == version_no
