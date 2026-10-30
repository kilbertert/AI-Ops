"""受管应用的 Service API 凭据：申请、对账、落盘。

## 为什么申请凭据这件事需要单独一个模块

Dify 的 app key 是**一次性展示**的资源：`POST /console/api/apps/<id>/api-keys` 的响应里才有
token，之后任何读取都拿不回来（1.17.1 的 app key 列表按固定源码返回完整 token，dataset key
才是掩码版；这里依赖的正是 app key 那一条，`controllers/console/apikey.py` 的
`_get_api_key_list`）。所以「申请成功但响应丢了」有两个不同的后果：

* **列表能读回 token** ⇒ 可以按「新增恰好一枚」对账复用，不重复申请；
* **对账不唯一**（0 枚或多枚） ⇒ 归属不明，**不切正式指针**，也不猜用别人的集成 key。

## 三个刻意的决定

### 1. 先快照，再创建，再核对

只按"创建前没有、创建后有一枚"是不够的：创建前如果已经有一枚别人的 key，创建后有两枚，
"取第一枚"就会拿到别人的。所以快照按 **id + token** 记，新增集合必须是恰好一枚。

### 2. 文件是唯一交付形态

`<key_dir>/<app_id>.key`，0600，原子写。发布只推路径（`_key_ref_for`），全程不需要把密钥
写进表、日志或任务 JSON。已有文件里的 key 必须仍在该 app 的 key 列表里才复用 —— 否则
（key 被人工删掉、或换了应用）会变成"发布了但跑不起来"。

### 3. 不删除别人的 key

数量达到 Dify 的 10 枚上限时拒绝（`SETUP_KEY_LIMIT`），让运维去清理，而不是为了让流程
通过就删掉一枚看起来不用的 key —— 那一枚可能正是别的集成在用的。
"""

from __future__ import annotations

import hmac
from pathlib import Path

from aiops_diagnostics.common.private_files import ensure_private_directory, write_private_text
from aiops_diagnostics.dify.dify_chat_workflow import load_workflow_key
from aiops_diagnostics.execution.chat_execution import key_file_for_app

from .self_service_identity import SetupRefused

#: Dify 1.17.1 `BaseApiKeyListResource.max_keys`。超过即 400 `max_keys_exceeded`。
MAX_KEYS = 10


def key_path(key_dir: str | Path, app_id: str) -> Path:
    if not str(key_dir).strip():
        raise SetupRefused("SETUP_KEY_DIRECTORY_REQUIRED")
    return Path(key_file_for_app(key_dir, app_id))


def list_keys(console, workspace_id: str, app_id: str) -> dict[str, str]:
    """``token -> key_id``。读不到就拒绝：把"列表读失败"当成"没有 key"会重复申请。"""
    payload = console._call(workspace_id, f"/console/api/apps/{app_id}/api-keys")
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise SetupRefused("SETUP_KEY_LIST_UNAVAILABLE")
    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise SetupRefused("SETUP_KEY_LIST_UNAVAILABLE")
        token, key_id = row.get("token"), row.get("id")
        if not isinstance(token, str) or not token.strip() or not isinstance(key_id, str):
            raise SetupRefused("SETUP_KEY_LIST_UNAVAILABLE")
        result[token] = key_id
    return result


def ensure(console, key_dir: str | Path, workspace_id: str, app_id: str) -> str:
    """保证这个 app 有一枚**已确认归属**的 service key，并返回它的 0600 文件路径。

    幂等：文件在位且该 token 仍属于这个 app 时不做任何外部写。
    """
    path = key_path(key_dir, app_id)
    before = list_keys(console, workspace_id, app_id)
    existing = ""
    if path.is_file():
        try:
            existing = load_workflow_key(path)
        except ValueError:
            existing = ""
    if existing and existing in before:
        return str(path)
    if existing and existing not in before:
        # 文件里的 key 已经不属于这个应用（被删掉或应用换了）：不复用，也不静默继续。
        existing = ""
    if len(before) >= MAX_KEYS:
        raise SetupRefused("SETUP_KEY_LIMIT")

    try:
        created = console._call(workspace_id, f"/console/api/apps/{app_id}/api-keys", method="POST", body={})
        token = created.get("token") if isinstance(created, dict) else None
    except Exception as exc:  # noqa: BLE001 - 只区分"可对账"与"不可对账"
        token = None
        lost = exc
    else:
        lost = None
    if isinstance(token, str) and token.strip():
        _write(path, token)
        return str(path)

    # 响应丢失（或读不出 token）：先对账，不重复申请。
    after = list_keys(console, workspace_id, app_id)
    new = {token: key_id for token, key_id in after.items() if key_id not in set(before.values())}
    if len(new) != 1:
        if lost is not None and not new:
            # 一枚都没新增 ⇒ 上游没有创建成功，抛回原错误（调用方按步骤失败重试）。
            raise SetupRefused("SETUP_KEY_UNAVAILABLE") from lost
        raise SetupRefused("SETUP_KEY_AMBIGUOUS")
    token = next(iter(new))
    _write(path, token)
    return str(path)


def _write(path: Path, token: str) -> None:
    if not token.strip() or any(ch.isspace() for ch in token) or len(token) > 512:
        raise SetupRefused("SETUP_KEY_UNAVAILABLE")
    ensure_private_directory(path.parent)
    write_private_text(path, token + "\n")
    # 写完回读一次：写到一半的凭据比没有凭据更糟 —— 发布只检查文件在不在。
    if not hmac.compare_digest(load_workflow_key(path), token):
        raise SetupRefused("SETUP_KEY_UNAVAILABLE")
