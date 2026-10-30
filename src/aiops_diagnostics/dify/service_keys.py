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
import json
import sqlite3
import time
from pathlib import Path

from aiops_diagnostics.common.private_files import (
    ensure_private_directory,
    protect_private_file,
    write_private_text,
)
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


class KeyOperations:
    """创建前快照的**持久**记账。

    为什么不能只放在内存里：`ensure` 的一次调用里"创建成功、响应丢失、随后列表也读不到"
    是完全可能的（上游抖动）。下一次尝试（甚至重启之后）如果只看当前列表，就会把上一轮
    创建的 key 当成"本来就有的"，于是再建一枚 —— 每重试一次多一枚，直到撞上 Dify 的
    10 枚上限，而中间每一枚都是没人认领的凭据。

    所以把"这次操作创建前看到了哪些 key"写进库：下一次看到同一行，就用它做差集。
    行在成功后删除；失败时**保留**，因为下一次正是靠它来对账。
    """

    def __init__(self, path):
        self.path = Path(path).expanduser().absolute()
        ensure_private_directory(self.path.parent)
        with self._connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS service_key_ops (
                    app_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    before_json TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
        protect_private_file(self.path)

    def _connection(self):
        connection = sqlite3.connect(str(self.path), timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def get(self, app_id: str) -> dict | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM service_key_ops WHERE app_id=?", (app_id,)).fetchone()
        return dict(row) if row else None

    def start(self, app_id: str, workspace_id: str, before: dict[str, str]) -> None:
        with self._connection() as db:
            db.execute(
                "INSERT INTO service_key_ops(app_id,workspace_id,before_json,stage,updated_at)"
                " VALUES(?,?,?,'creating',?) ON CONFLICT(app_id) DO NOTHING",
                (app_id, workspace_id, json.dumps(before, sort_keys=True), time.time()),
            )

    def clear(self, app_id: str) -> None:
        with self._connection() as db:
            db.execute("DELETE FROM service_key_ops WHERE app_id=?", (app_id,))


def _adopt(path: Path, token: str, ops, app_id: str) -> str:
    _write(path, token)
    if ops is not None:
        ops.clear(app_id)
    return str(path)


def ensure(console, key_dir: str | Path, workspace_id: str, app_id: str, *, database=None) -> str:
    """保证这个 app 有一枚**已确认归属**的 service key，并返回它的 0600 文件路径。

    幂等：文件在位且该 token 仍属于这个 app 时不做任何外部写。跨请求与重启的幂等靠
    `KeyOperations` 里那份创建前快照（见该类的文档）。
    """
    path = key_path(key_dir, app_id)
    ops = KeyOperations(database) if database else None
    before = list_keys(console, workspace_id, app_id)
    existing = ""
    if path.is_file():
        try:
            existing = load_workflow_key(path)
        except ValueError:
            existing = ""
    if existing and existing in before:
        if ops is not None:
            ops.clear(app_id)
        return str(path)
    if existing and existing not in before:
        # 文件里的 key 已经不属于这个应用（被删掉或应用换了）：不复用，也不静默继续。
        existing = ""

    # 上一轮（可能是另一个进程 / 重启前）已经建过一枚但没能确认：先按持久快照对账。
    pending = ops.get(app_id) if ops is not None else None
    if pending and pending["stage"] == "creating":
        known = set(json.loads(pending["before_json"]).values())
        new = {token: key_id for token, key_id in before.items() if key_id not in known}
        if len(new) == 1:
            return _adopt(path, next(iter(new)), ops, app_id)
        if len(new) > 1:
            raise SetupRefused("SETUP_KEY_AMBIGUOUS")
        # 0 枚：上一轮的创建确实没落地，继续走正常创建（快照仍用这次读到的 before）。

    if len(before) >= MAX_KEYS:
        raise SetupRefused("SETUP_KEY_LIMIT")
    if ops is not None:
        ops.start(app_id, workspace_id, before)
    try:
        created = console._call(workspace_id, f"/console/api/apps/{app_id}/api-keys", method="POST", body={})
        token = created.get("token") if isinstance(created, dict) else None
    except Exception as exc:  # noqa: BLE001 - 只区分"可对账"与"不可对账"
        token = None
        lost = exc
    else:
        lost = None
    if isinstance(token, str) and token.strip():
        return _adopt(path, token, ops, app_id)

    # 响应丢失（或读不出 token）：先对账，不重复申请。对账失败时**保留** op 行：
    # 下一次尝试靠它认出"这一枚是我上一轮建的"。
    after = list_keys(console, workspace_id, app_id)
    new = {token: key_id for token, key_id in after.items() if key_id not in set(before.values())}
    if len(new) != 1:
        if lost is not None and not new:
            # 一枚都没新增 ⇒ 上游没有创建成功，抛回原错误（调用方按步骤失败重试）。
            raise SetupRefused("SETUP_KEY_UNAVAILABLE") from lost
        raise SetupRefused("SETUP_KEY_AMBIGUOUS")
    return _adopt(path, next(iter(new)), ops, app_id)


def _write(path: Path, token: str) -> None:
    if not token.strip() or any(ch.isspace() for ch in token) or len(token) > 512:
        raise SetupRefused("SETUP_KEY_UNAVAILABLE")
    ensure_private_directory(path.parent)
    write_private_text(path, token + "\n")
    # 写完回读一次：写到一半的凭据比没有凭据更糟 —— 发布只检查文件在不在。
    if not hmac.compare_digest(load_workflow_key(path), token):
        raise SetupRefused("SETUP_KEY_UNAVAILABLE")
