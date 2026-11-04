"""`agent_serviceability` —— 「还有没有版本在服务」的**唯一判据**（#922）。

判据表就是这两个函数。这里钉的是**两个方向相反的错误**同时被修掉：

- 停用之后仍被当成在服务（安全侧）；
- 编辑草稿就被当成下线（可用性侧）。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from aiops_diagnostics.publication.agent_serviceability import LIVE_STATUSES, live_version, serves


@dataclass(frozen=True)
class _Agent:
    status: str
    published_version: int | None


@pytest.mark.parametrize(
    ("status", "version", "expected"),
    [
        ("published", 1, 1),  # 正常服务
        ("draft", 1, 1),  # 编辑中：线上仍是那一版
        ("draft", None, None),  # 从未发布过
        ("disabled", 1, None),  # 停用：立刻停服，哪怕指针还在
        ("published", None, None),  # 指针被清掉（数据异常）也不算在服务
    ],
)
def test_live_version_is_the_published_pointer(
    status: str, version: int | None, expected: int | None
) -> None:
    assert live_version(_Agent(status, version)) == expected


def test_only_disabled_is_excluded_from_live_statuses() -> None:
    """状态词表来自 `agent_lifecycle.AGENT_STATUSES`；这里只钉「谁不算在服务」。

    新增一个状态时这条会红，提醒回答「它在不在服务」而不是让它默默落进某个分支。
    """
    assert frozenset({"published", "draft"}) == LIVE_STATUSES


def test_serves_is_equality_with_the_live_version() -> None:
    live = _Agent("draft", 3)
    assert serves(live, 3)
    assert not serves(live, 2)  # 更旧的版本不服务
    assert not serves(_Agent("disabled", 3), 3)
