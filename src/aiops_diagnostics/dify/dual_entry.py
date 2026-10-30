"""双端（客户端客服 / 管家）的词表、开通范围与渠道状态。

## 为什么单独一个模块

"端"这个词在三处已经是同一个事实：`faq.PLATFORMS`、`dify_app_registry.BUSINESS_ENTRIES`
和 `chat_route_bindings.entry`。开通流程要按端分派步骤、按端显示进度、按端判断渠道，
如果每一处各写一遍 ``("consumer", "operator")``，第一次有人加第三个端时就会漏掉一处，
而漏掉的那一处表现是"这一端永远显示未开通"——一个看起来像业务问题的事实错误。

所以取值只在这里定义一次，其余模块从这里读。

## 场景不是端

端是**业务入口**（消费者用哪个、员工用哪个），场景是**执行路线**（这个入口此刻走哪条引擎路线）。
同一个端可以有多个场景（例如客户端除了 Chatflow 客服还有 `consumer_own_order_status` 状态路线），
但本模块只登记**首片闭环使用的那一个**场景；多场景选择器是另一件事，不在这里预先造。

## 渠道状态来自声明，不来自推导

"AI 已准备"和"用户能用"是两件事。渠道是否接入不从这个端有没有应用、有没有正式版推出来——
那样会让"刚开通的客户"显示成"可用"，而实际上没有任何前端在调它。渠道状态读环境清单的
``[[channels]]`` 段；没有声明就是 ``pending``（fail closed：宁可显示"待接入"）。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

#: 业务入口。取值与 `faq.PLATFORMS`、`dify_app_registry.BUSINESS_ENTRIES` 同一份。
ENTRIES = ("consumer", "operator")

SCENARIO_CONSUMER = "consumer_service"
SCENARIO_OPERATOR = "operator_order_diagnosis"

#: 端 → 首片闭环使用的执行场景。
SCENARIOS = {"consumer": SCENARIO_CONSUMER, "operator": SCENARIO_OPERATOR}

#: 页面标签。运营看的是业务名，不是 `consumer`/`operator`。
LABELS = {"consumer": "客户端客服", "operator": "管家"}

#: 开通范围。`both` 是默认值。
SCOPES = ("both", "consumer", "operator")

CHANNEL_CONNECTED = "connected"
CHANNEL_PENDING = "pending"

#: 资源准备、该端已发布、渠道已接入、真实用户已验证 —— 四件事分开报。
READY = "ready"
DRAFT = "draft"
FAILED = "failed"


class DualEntryError(ValueError):
    """可向页面展示的稳定拒绝码。"""


def scenario(entry: str) -> str:
    if entry not in SCENARIOS:
        raise DualEntryError("SETUP_ENTRY_UNKNOWN")
    return SCENARIOS[entry]


def entry_for_scenario(name: str) -> str | None:
    return next((entry for entry, value in SCENARIOS.items() if value == name), None)


def entries_for_scope(scope: str) -> tuple[str, ...]:
    if scope not in SCOPES:
        raise DualEntryError("SETUP_SCOPE_UNKNOWN")
    return ENTRIES if scope == "both" else (scope,)


def scope_for_entries(entries) -> str:
    chosen = tuple(entries)
    if not chosen or len(set(chosen)) != len(chosen) or set(chosen) - set(ENTRIES):
        raise DualEntryError("SETUP_SCOPE_UNKNOWN")
    return "both" if set(chosen) == set(ENTRIES) else chosen[0]


def label(entry: str) -> str:
    return LABELS.get(entry, entry)


def channels(path: str | Path, environment: str) -> dict[tuple[str, str], dict]:
    """读环境清单的 ``[[channels]]``：``(租户, 端) -> {channel, state, note}``。

    文件不存在或没有该段 ⇒ 空表。空表意味着每一端都按 ``pending`` 显示，
    这是有意的：没有声明就没有已接入渠道。
    """
    if not path:
        return {}
    declared = Path(str(path)).expanduser().absolute()
    if not declared.is_file() or declared.is_symlink():
        return {}
    with declared.open("rb") as handle:
        body = tomllib.load(handle)
    rows = body.get("channels") or []
    if not isinstance(rows, list) or len(rows) > 2000:
        raise DualEntryError("SETUP_CHANNEL_DECLARATION_INVALID")
    result: dict[tuple[str, str], dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise DualEntryError("SETUP_CHANNEL_DECLARATION_INVALID")
        tenant = str(row.get("tenant_id") or "")
        entry = str(row.get("entry") or "")
        name = str(row.get("channel") or "")
        state = str(row.get("state") or CHANNEL_PENDING)
        if (
            not tenant
            or entry not in ENTRIES
            or not name
            or state not in {CHANNEL_CONNECTED, CHANNEL_PENDING}
            or (row.get("environment") and str(row["environment"]) != environment)
        ):
            raise DualEntryError("SETUP_CHANNEL_DECLARATION_INVALID")
        key = (tenant, entry)
        if key in result and result[key]["state"] != state:
            # 同一 (租户, 端) 声明了两个互相矛盾的状态：拒绝，不按"后写的赢"。
            raise DualEntryError("SETUP_CHANNEL_DECLARATION_INVALID")
        result[key] = {"channel": name, "state": state, "note": str(row.get("note") or "")}
    return result


def channel_state(declared: dict, tenant_id: str, entry: str) -> dict:
    """一个端此刻的渠道状态。未声明 ⇒ `pending`（不是"未知"，也不是"可用"）。"""
    found = declared.get((tenant_id, entry))
    if found is None:
        return {"state": CHANNEL_PENDING, "channel": "", "note": "该端尚未接入使用渠道"}
    return dict(found)


def readiness(entry_row: dict, channel: dict) -> str:
    """把一端的资源、发布、渠道合成**页面口径**，四件事分开。

    - ``ready``：该端已发布且渠道已接入；
    - ``draft``：应用在、但还没成为正式版（或有正式版但尚未发布本副本）；
    - ``failed``：这一端失败；
    - ``pending``：资源已准备但渠道待接入。
    """
    if entry_row.get("state") == FAILED:
        return FAILED
    if not entry_row.get("published"):
        return DRAFT if entry_row.get("app_id") else FAILED
    return READY if channel.get("state") == CHANNEL_CONNECTED else "pending"
