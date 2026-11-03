"""平台已批准的业务环境清单：开通新客户时**自动**取到业务连接，不再改服务器环境变量。

## 为什么需要这一层

开通一个客户的客户端客服，第一步是取得这个客户的业务环境（CPMS）地址。这个地址今天写在
`AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL` 里按租户登记 —— 也就是**每来一个客户，人去改一次
生产环境文件并重启网关**。那是"一键开通"名不副实的地方：页面能建工作区、装插件、配模型、
复制应用，却卡在一条只有运维能改的字符串上。

这一层把**地址的批准**从"按租户"上移到"按部署"：平台部署时声明本平台批准了哪几套业务
环境，开通时按清单取。今天只有一套，所以这一步是零点击；将来真有第二套，清单就是那个
"必须选、不许猜"的地方。

## 与 `AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL` 的关系

**环境变量仍是权威覆盖，清单只是新租户的来源。**

`approved_business_endpoint()` 先读环境变量：它登记了这个租户 ⇒ 用它的（运维钉死的地址
优先）。只有环境变量**没有**登记这个租户时，才回退到清单里已批准的地址。这样：

- 现役租户（如 Ulink）一个字节都不用动，行为与今天逐字相同；
- 新租户不再需要改环境变量、不需要重启 —— 一键开通真的成立；
- 运维想彻底关掉清单，清空清单路径即可（未登记租户回到 `SETUP_BUSINESS_CONNECTION_REQUIRED`）。

**清单项只能是部署侧声明的，不能来自请求。** 开通请求里没有任何地方能传地址 —— 客户自己
选地址等于让平台按客户给的任意 URL 去取数。

## 多个环境时为什么拒绝而不是选第一个

"这个客户属于哪套业务环境"在清单有多项时没有唯一答案。按文件顺序取第一个就是把顺序
变成语义 —— 那种缺陷的表现是"给 A 客户接了 B 客户的业务系统"，而它在页面上看起来是成功。
所以多于一项且未声明默认时拒绝并给出可操作的码（`SETUP_BUSINESS_ENVIRONMENT_AMBIGUOUS`），
由运维在清单里用 `default: true` 指定，或在环境变量里按租户钉死。

地址的合法性不在本模块判：`canonical_gateway_url` 是唯一那份 URL 规则，这里只调它，
不另写一份（两份实现必然漂移）。
"""

from __future__ import annotations

import json
from pathlib import Path

from aiops_diagnostics.common.private_files import PrivatePathError, validate_private_file

SCHEMA = "aiops-business-environments/1"
MAX_CATALOGUE_BYTES = 64 * 1024


class BusinessCatalogueError(RuntimeError):
    """清单不可用。启动时就应当失败，而不是在第一次开通时才失败。"""


def load(path: str | Path | None) -> tuple[dict, ...]:
    """读整份清单并逐条校验**形状**。任何一条不合格 ⇒ 整份拒绝（不降级、不跳过）。"""
    if not path:
        return ()
    declared = Path(str(path)).expanduser().absolute()
    try:
        validate_private_file(declared)
        if declared.stat().st_size > MAX_CATALOGUE_BYTES:
            raise BusinessCatalogueError("business environment catalogue is too large")
        raw = json.loads(declared.read_text(encoding="utf-8"))
    except (OSError, ValueError, PrivatePathError) as exc:
        raise BusinessCatalogueError("business environment catalogue is unavailable") from exc
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise BusinessCatalogueError("unsupported business environment catalogue schema")
    rows = raw.get("environments")
    if not isinstance(rows, list):
        raise BusinessCatalogueError("business environment catalogue declares no list")
    entries: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise BusinessCatalogueError("business environment entry is not an object")
        identifier = row.get("id")
        base_url = row.get("base_url")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise BusinessCatalogueError("business environment id is missing or duplicated")
        if not isinstance(base_url, str) or not base_url.strip():
            raise BusinessCatalogueError("business environment base_url is missing")
        seen.add(identifier)
        entries.append(
            {**row, "id": identifier, "base_url": base_url.strip(), "default": row.get("default") is True}
        )
    if sum(1 for entry in entries if entry["default"]) > 1:
        # 两条都声称是默认 ⇒ 又回到"没有唯一答案"。拒绝，不按顺序裁决。
        raise BusinessCatalogueError("business environment catalogue declares two defaults")
    return tuple(entries)


def validate(entries: tuple[dict, ...]) -> None:
    """地址合法性：复用 `canonical_gateway_url`，不在这里另写一份 URL 规则。"""
    from aiops_diagnostics.gateway.gateway_config import canonical_gateway_url

    for entry in entries:
        try:
            canonical_gateway_url(entry["base_url"])
        except ValueError as exc:
            raise BusinessCatalogueError(f"business environment {entry['id']} has an invalid URL") from exc


def approved_base_url(entries: tuple[dict, ...]) -> str:
    """从已批准清单里取出**唯一**可用地址；取不到或无法唯一确定时返回空串。

    空串有两种原因（清单为空 / 多项无默认），调用方用 `is_ambiguous` 区分并给出不同的码。
    """
    if not entries:
        return ""
    if len(entries) == 1:
        return entries[0]["base_url"]
    defaults = [entry for entry in entries if entry["default"]]
    return defaults[0]["base_url"] if len(defaults) == 1 else ""


def is_ambiguous(entries: tuple[dict, ...]) -> bool:
    return len(entries) > 1 and not approved_base_url(entries)
