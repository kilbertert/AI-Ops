"""`ensure-ready`：把建出来的 workspace 推到 **能用**（#881）。

判据对着五种"看起来做了、其实推到人身上才发现"：

1. **顺序不能换**：插件 → 凭据 → 模板。模板引用 provider，provider 没凭据时导入照样
   成功，只是那个 app 一跑就 ``ProviderTokenDoesNotExist``。
2. **重跑是幂等的**：装过的插件不再发安装请求，同名凭据不再写，已有 app 不重复导。
3. **凭据失效要说清楚**：Dify 拿真 key 校验，假 key 回 400 ``Credentials validation
   failed`` —— 那是"这把 key 不能用"，不是"服务坏了"（``DIFY_CREDENTIAL_INVALID``）。
4. **模板失败不回滚**：状态停在 ``created``（不是 ``failed``），重跑补齐。写成 ``failed``
   会让下一次 `ensure-workspace` 重建一个永久孤儿。
5. **缺配置不发请求**：模型 key 不在代码里，缺了就拒绝并说去哪填，不猜。

变异对照：把插件/凭据两步的顺序调换（用例 1 变红）、把"已存在就跳过"拿掉（用例 2 变红）、
把模板失败的 :meth:`mark_template_failed` 换成 :meth:`mark_failed`（用例 4 变红）。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from aiops_diagnostics.dify_console_admin import ConsoleApiSettings, DifyConsoleAdmin
from aiops_diagnostics.dify_ensure_workspace import EnsureError
from aiops_diagnostics.dify_workspace_ready import (
    CREDENTIAL_NAME,
    DEFAULT_PLUGIN_IDENTIFIERS,
    ReadySettings,
    ensure_ready,
)
from aiops_diagnostics.dify_workspaces import DifyWorkspaces

TENANT = "1942105476598861824"
ENVIRONMENT = "41"
WORKSPACE = "c3f871e1-0000-4000-8000-000000000001"
MODEL_KEY = "sk-real-looking-but-fake-0123456789"
APP_NAME = "aiops-workspace-template"

_PLUGIN_LIST = "/console/api/workspaces/current/plugin/list"
_PLUGIN_INSTALL = "/console/api/workspaces/current/plugin/install/pkg"
_CREDENTIALS = "/console/api/workspaces/current/model-providers/langgenius/deepseek/deepseek/credentials"
_APPS = "/console/api/apps"
_IMPORT = "/console/api/apps/imports"


class _Console:
    """扮演 Dify 控制台。按 **路径前缀** 分派，因为插件列表带 query 串。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.plugins: list[str] = []
        self.credentials: list[str] = []
        self.apps: list[str] = []
        self.credential_status = 200
        self.credential_body: Any = None
        self.import_status = 200
        self.import_body: Any = None
        self.base = ""

    def handler(self) -> type[BaseHTTPRequestHandler]:
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                upstream.calls.append({"method": "GET", "path": self.path, "body": None})
                if self.path.startswith(_PLUGIN_LIST):
                    upstream._reply(
                        self,
                        200,
                        {
                            "plugins": [
                                {"meta": {"plugin_unique_identifier": item}} for item in upstream.plugins
                            ]
                        },
                    )
                elif self.path.startswith(_CREDENTIALS):
                    upstream._reply(
                        self,
                        200,
                        {"credentials": [{"credential_name": name} for name in upstream.credentials]},
                    )
                elif self.path.startswith(_APPS):
                    upstream._reply(
                        self, 200, {"data": [{"name": name} for name in upstream.apps], "total": 1}
                    )
                else:
                    upstream._reply(self, 404, {"code": "not_found"})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                upstream.calls.append({"method": "POST", "path": self.path, "body": body})
                if self.path == _PLUGIN_INSTALL:
                    upstream.plugins.extend(body["plugin_unique_identifiers"])
                    upstream._reply(self, 200, {"all_installed": True})
                elif self.path == _CREDENTIALS:
                    if upstream.credential_body is not None:
                        upstream._reply(self, upstream.credential_status, upstream.credential_body)
                        return
                    upstream.credentials.append(body["name"])
                    upstream._reply(self, 200, {"result": "success"})
                elif self.path == _IMPORT:
                    if upstream.import_body is not None:
                        upstream._reply(self, upstream.import_status, upstream.import_body)
                        return
                    upstream.apps.append(body.get("name") or APP_NAME)
                    upstream._reply(self, 200, {"app_id": "app-1", "status": "completed"})
                else:
                    upstream._reply(self, 404, {"code": "not_found"})

            def log_message(self, *_: Any) -> None:
                pass

        return Handler

    @staticmethod
    def _reply(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def post_paths(self) -> list[str]:
        return [call["path"] for call in self.calls if call["method"] == "POST"]


@pytest.fixture()
def console():
    state = _Console()
    server = ThreadingHTTPServer(("127.0.0.1", 0), state.handler())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    state.base = f"http://{host}:{port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def _workspaces(tmp_path: Path) -> DifyWorkspaces:
    """一行 `created` 的映射 —— `ensure-workspace` 跑完之后的样子。"""
    workspaces = DifyWorkspaces(tmp_path / "gateway.db")
    workspaces.claim(TENANT, ENVIRONMENT, owner="test:1", lease_seconds=300)
    workspaces.mark_created(TENANT, ENVIRONMENT, WORKSPACE)
    return workspaces


def _run(tmp_path: Path, console: _Console, workspaces: DifyWorkspaces | None = None, **overrides: Any):
    kwargs: dict[str, Any] = {
        "tenant_id": TENANT,
        "environment": ENVIRONMENT,
        "workspaces": workspaces or _workspaces(tmp_path),
        "console": DifyConsoleAdmin(ConsoleApiSettings(base_url=console.base, api_key="admin-key")),
        "settings": ReadySettings(
            console_base_url=console.base,
            console_api_key="admin-key",
            model_api_key=MODEL_KEY,
        ),
        "template_yaml": "kind: app\nversion: 0.1.5\n",
        "app_name": APP_NAME,
    }
    kwargs.update(overrides)
    return ensure_ready(**kwargs)


# ── 1. 三步都做，且按这个顺序 ────────────────────────────────────────────


def test_it_installs_plugins_then_credential_then_template(tmp_path: Path, console: _Console) -> None:
    """顺序就是判据：模板引用 provider，provider 没凭据时"导入成功"是个陷阱。"""
    workspaces = _workspaces(tmp_path)

    outcome = _run(tmp_path, console, workspaces)

    assert console.post_paths() == [_PLUGIN_INSTALL, _CREDENTIALS, _IMPORT]
    assert outcome.plugins_installed == DEFAULT_PLUGIN_IDENTIFIERS
    assert outcome.credential_written is True
    assert outcome.app_id == "app-1"
    assert outcome.state == "ready"
    assert workspaces.row(TENANT, ENVIRONMENT)["setup_state"] == "ready"


def test_the_credential_carries_the_configured_key(tmp_path: Path, console: _Console) -> None:
    """模型 key 来自 ``AIOPS_GATEWAY_DIFY_WORKSPACE_MODEL_API_KEY``，不是代码里的常量。"""
    _run(tmp_path, console)

    posted = [call for call in console.calls if call["path"] == _CREDENTIALS and call["method"] == "POST"]
    assert posted[0]["body"] == {
        "credentials": {"api_key": MODEL_KEY, "endpoint_url": "https://api.deepseek.com/v1"},
        "name": CREDENTIAL_NAME,
    }


# ── 2. 幂等 ─────────────────────────────────────────────────────────────


def test_a_second_run_sends_no_mutating_request(tmp_path: Path, console: _Console) -> None:
    workspaces = _workspaces(tmp_path)
    _run(tmp_path, console, workspaces)
    console.calls.clear()

    outcome = _run(tmp_path, console, workspaces)

    assert console.post_paths() == []
    assert outcome.plugins_installed == ()
    assert outcome.credential_written is False
    assert outcome.app_id == ""
    assert outcome.state == "ready"


def test_it_installs_only_the_plugins_that_are_missing(tmp_path: Path, console: _Console) -> None:
    """已装的不再发一次：安装是异步任务型的，重复触发只是多一次任务。"""
    console.plugins.append(DEFAULT_PLUGIN_IDENTIFIERS[0])

    _run(tmp_path, console)

    install = [call for call in console.calls if call["path"] == _PLUGIN_INSTALL][0]
    assert install["body"]["plugin_unique_identifiers"] == [DEFAULT_PLUGIN_IDENTIFIERS[1]]


# ── 3. 凭据失效是"换 key"，不是"服务坏了" ──────────────────────────────


def test_a_rejected_credential_is_reported_as_an_invalid_credential(
    tmp_path: Path, console: _Console
) -> None:
    console.credential_status = 400
    console.credential_body = {"message": "Credentials validation failed with status code 401"}
    workspaces = _workspaces(tmp_path)

    with pytest.raises(EnsureError) as caught:
        _run(tmp_path, console, workspaces)

    assert caught.value.code == "DIFY_CREDENTIAL_INVALID"
    # 凭据没过 ⇒ 模板没导（陷阱就在这里），但 workspace 还在、状态没退。
    assert console.post_paths() == [_PLUGIN_INSTALL, _CREDENTIALS]
    row = workspaces.row(TENANT, ENVIRONMENT)
    assert row["setup_state"] == "created"
    assert row["workspace_id"] == WORKSPACE


# ── 4. 模板失败不回滚 ───────────────────────────────────────────────────


def test_a_failed_template_import_keeps_the_workspace_and_can_be_retried(
    tmp_path: Path, console: _Console
) -> None:
    """状态停在 ``created``：写成 ``failed`` 会让下一次 `ensure-workspace` 重建孤儿。"""
    console.import_status = 400
    console.import_body = {"message": "invalid dsl"}
    workspaces = _workspaces(tmp_path)

    with pytest.raises(EnsureError) as caught:
        _run(tmp_path, console, workspaces)

    assert caught.value.code == "DIFY_CONSOLE_REJECTED"
    row = workspaces.row(TENANT, ENVIRONMENT)
    assert row["setup_state"] == "created"
    assert "invalid dsl" in row["last_error"]
    assert row["workspace_id"] == WORKSPACE
    # workspace 仍然可解析 —— 人进得去、能改。
    assert workspaces.resolve(TENANT, ENVIRONMENT) == WORKSPACE

    console.import_status = 200
    console.import_body = None
    outcome = _run(tmp_path, console, workspaces)
    assert outcome.state == "ready"
    assert workspaces.row(TENANT, ENVIRONMENT)["setup_state"] == "ready"


# ── 5. 缺配置/缺映射：不发请求，直接拒绝 ────────────────────────────────


def test_a_missing_model_key_refuses_and_names_the_variable(tmp_path: Path, console: _Console) -> None:
    """缺 key ⇒ 拒绝并说**哪一行**没填。不回落示例 key —— Dify 会真校验，假 key 走不通。"""
    settings = ReadySettings(console_base_url=console.base, console_api_key="admin-key")

    with pytest.raises(EnsureError) as caught:
        _run(tmp_path, console, settings=settings)

    assert caught.value.code == "WORKSPACE_READY_CONFIG_MISSING"
    assert "AIOPS_GATEWAY_DIFY_WORKSPACE_MODEL_API_KEY" in caught.value.message
    assert console.calls == [], "缺配置时一条请求都不该发"


def test_an_unresolved_tenant_surfaces_its_own_code(tmp_path: Path, console: _Console) -> None:
    """没有映射行 = 该先跑 `ensure-workspace`。把四个码压成一个就是丢信息。"""
    with pytest.raises(EnsureError) as caught:
        _run(tmp_path, console, workspaces=DifyWorkspaces(tmp_path / "gateway.db"))

    assert caught.value.code == "DIFY_WORKSPACE_UNRESOLVED"
    assert console.calls == []
