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
_PROVIDERS = "/console/api/workspaces/current/model-providers"
_SWITCH = "/console/api/workspaces/current/model-providers/langgenius/deepseek/deepseek/credentials/switch"
_APPS = "/console/api/apps"
_IMPORT = "/console/api/apps/imports"
_PLUGIN_TASKS = "/console/api/workspaces/current/plugin/tasks"


class _Console:
    """扮演 Dify 控制台。按 **路径前缀** 分派，因为插件列表带 query 串。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.plugins: list[str] = []
        self.credentials: list[str] = []
        #: 前 N 次读 provider 列表时"看不见"名为 CREDENTIAL_NAME 的凭据
        #: （模拟"检查时还没有、写入时已被别人加上"的竞态窗口）。
        self.hidden_credential_reads = 0
        self.apps: list[str] = []
        self.credential_status = 200
        self.credential_body: Any = None
        self.import_status = 200
        self.import_body: Any = None
        self.apps_status = 200
        self.apps_body: Any = None
        #: 每页条数（>0 时按真实分页回答 ``/console/api/apps``）。
        self.page_size = 0
        #: 另一个 provider 上的同名凭据（验证 ``already exists`` 的 provider 维度）。
        self.other_provider_credential = ""
        #: 异步安装的模拟：>0 时安装请求回一个 ``task_id``，任务在**第 N 次轮询**才成功。
        self.task_polls_until_success = 0
        #: 任务终态改成 failed（验证失败不会被当成成功）。
        self.task_fails = False
        self._task_polls = 0
        self._pending_plugins: list[str] = []
        self._list_reads = 0
        #: 被 switch 到的 credential_id（None 表示从未切换）。
        self.switched_to: str | None = None
        #: 让 provider 列表报告的 current 名字"卡住"不变 —— 模拟 Dify 按 created_at 选默认，
        #: 刚写的新凭据排在旧凭据后面。
        self.pinned_current: str | None = None
        #: ``top-level`` 时按真实形状回答 ``/plugin/list``（标识在顶层，meta 是任意 map）。
        self.plugin_list_shape = "nested"
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
                            "plugins": (
                                [
                                    {"plugin_unique_identifier": item, "meta": {"position": i}}
                                    for i, item in enumerate(upstream.plugins)
                                ]
                                if upstream.plugin_list_shape == "top-level"
                                else [
                                    {"meta": {"plugin_unique_identifier": item}} for item in upstream.plugins
                                ]
                            )
                        },
                    )
                elif self.path.rstrip("/") == _PROVIDERS:
                    upstream._list_reads += 1
                    # 真实形状（36 上只读核对过）：列表里每个 provider 带
                    # custom_configuration.available_credentials[{credential_id, credential_name}]。
                    upstream._reply(
                        self,
                        200,
                        {
                            "data": [
                                {
                                    "provider": "langgenius/deepseek/deepseek",
                                    "custom_configuration": {
                                        "status": "active" if upstream.credentials else "no-configure",
                                        "current_credential_name": (
                                            upstream.pinned_current
                                            if upstream.pinned_current is not None
                                            else (upstream.credentials[-1] if upstream.credentials else None)
                                        ),
                                        "available_credentials": [
                                            {"credential_id": f"cid-{i}", "credential_name": name}
                                            for i, name in enumerate(upstream.credentials)
                                            if not (
                                                name == CREDENTIAL_NAME
                                                and upstream._list_reads <= upstream.hidden_credential_reads
                                            )
                                        ],
                                    },
                                }
                            ]
                        },
                    )
                elif self.path.startswith(_PLUGIN_TASKS):
                    upstream._task_polls += 1
                    done = upstream._task_polls >= upstream.task_polls_until_success
                    if done and not upstream.task_fails:
                        for item in upstream._pending_plugins:
                            if item not in upstream.plugins:
                                upstream.plugins.append(item)
                    status = "failed" if upstream.task_fails else ("success" if done else "running")
                    upstream._reply(
                        self,
                        200,
                        {
                            "task": {
                                "status": status,
                                "total_plugins": len(upstream._pending_plugins),
                                "completed_plugins": len(upstream.plugins) if done else 0,
                                "plugins": [
                                    {
                                        "plugin_unique_identifier": item,
                                        "message": "boom" if upstream.task_fails else "",
                                    }
                                    for item in (upstream._pending_plugins if upstream.task_fails else [])
                                ],
                            }
                        },
                    )
                elif self.path.startswith(_APPS):
                    if upstream.page_size:
                        page = int(self.path.split("page=")[1].split("&")[0]) if "page=" in self.path else 1
                        start = (page - 1) * upstream.page_size
                        chunk = upstream.apps[start : start + upstream.page_size]
                        upstream._reply(
                            self,
                            200,
                            {
                                "data": [{"name": name} for name in chunk],
                                "total": len(upstream.apps),
                                "has_more": start + upstream.page_size < len(upstream.apps),
                            },
                        )
                        return
                    if upstream.apps_body is not None:
                        upstream._reply(self, upstream.apps_status, upstream.apps_body)
                        return
                    upstream._reply(
                        self,
                        200,
                        {"data": [{"name": name} for name in upstream.apps], "total": 1, "has_more": False},
                    )
                else:
                    upstream._reply(self, 404, {"code": "not_found"})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                upstream.calls.append({"method": "POST", "path": self.path, "body": body})
                if self.path == _PLUGIN_INSTALL:
                    if upstream.task_polls_until_success or upstream.task_fails:
                        # 异步形态：立刻回 task_id，插件在轮询到 success 之后才出现。
                        upstream._pending_plugins = list(body["plugin_unique_identifiers"])
                        upstream._task_polls = 0
                        upstream._reply(self, 200, {"all_installed": False, "task_id": "task-1"})
                        return
                    upstream.plugins.extend(body["plugin_unique_identifiers"])
                    upstream._reply(self, 200, {"all_installed": True})
                elif self.path == _SWITCH:
                    upstream.switched_to = body["credential_id"]
                    upstream._reply(self, 200, {"result": "success"})
                elif self.path == _CREDENTIALS:
                    if upstream.credential_body is not None:
                        upstream._reply(self, upstream.credential_status, upstream.credential_body)
                        return
                    if body["name"] == upstream.other_provider_credential:
                        # 另一个 provider 上已有同名凭据：Dify 按
                        # ``tenant_id + provider_name + credential_name`` 判重。
                        upstream._reply(
                            self,
                            400,
                            {"message": f"Credential with name '{body['name']}' already exists."},
                        )
                        return
                    if body["name"] in upstream.credentials:
                        # Dify 的真实行为：同名第二次写入回 400。
                        upstream._reply(
                            self,
                            400,
                            {"message": f"Credential with name '{body['name']}' already exists."},
                        )
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
        "console": DifyConsoleAdmin(
            ConsoleApiSettings(
                base_url=console.base,
                api_key="admin-key",
                install_timeout=5.0,
                install_poll_interval=0.01,
            )
        ),
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


# ── 6. 安装是**异步**的：不等它落地，下一步就会读错 ─────────────────────


def test_it_waits_for_the_install_task_before_writing_the_credential(
    tmp_path: Path, console: _Console
) -> None:
    """`/plugin/install/pkg` 只回一个 task_id —— 插件真正出现在 list 里是几秒之后。

    变异对照：`install_plugins` 发完 POST 就返回（不等任务）⇒ 本用例红：凭据请求发出时
    `/plugin/list` 里根本没有那个 provider 插件，而 Dify 会因此报"provider 不存在"，
    那个报错会被读成"配置错了"。
    """
    console.task_polls_until_success = 3

    outcome = _run(tmp_path, console)

    assert outcome.state == "ready"
    assert set(DEFAULT_PLUGIN_IDENTIFIERS) <= set(console.plugins)
    # 断言的是**次序**，不是计数：第一次写凭据、第一次导模板都必须排在最后一次轮询之后。
    # 原来的写法只排除凭据那一条，模板导入的 POST 仍被算进"之前"，所以即便把轮询挪到
    # 写凭据之后也照样绿 —— 评审指出它证明不了自己声称的规则，这里改成按键位断言。
    paths = [call["path"] for call in console.calls]
    last_poll = max(i for i, path in enumerate(paths) if path == f"{_PLUGIN_TASKS}/task-1")
    first_credential = paths.index(_CREDENTIALS)
    first_import = paths.index(_IMPORT)
    assert last_poll < first_credential, "写凭据发生在安装任务落地之前"
    assert last_poll < first_import, "导模板发生在安装任务落地之前"


def test_a_failed_install_task_stops_before_the_credential(tmp_path: Path, console: _Console) -> None:
    """任务 failed ⇒ 报 ``DIFY_PLUGIN_INSTALL_FAILED`` 并带上上游给的原因，不往下走。"""
    console.task_fails = True
    workspaces = _workspaces(tmp_path)

    with pytest.raises(EnsureError) as caught:
        _run(tmp_path, console, workspaces)

    assert caught.value.code == "DIFY_PLUGIN_INSTALL_FAILED"
    assert "boom" in caught.value.message
    assert console.post_paths() == [_PLUGIN_INSTALL]


def test_a_credential_that_already_exists_is_success_not_a_failure(tmp_path: Path, console: _Console) -> None:
    """列表与写入之间可能有人手工加过同名凭据。Dify 回 400 ``already exists`` —— 那是
    "已经达成"，不是失败：结果就是要这个名字的凭据在位。

    变异对照：把 ``already exists`` 当作普通 400 上抛 ⇒ 本用例红（`ensure_ready` 抛
    `DIFY_CONSOLE_REJECTED`，一个已经就绪的 workspace 被报成失败）。
    """
    console.credentials.append(CREDENTIAL_NAME)
    # 模拟竞态窗口：**第一次**读列表时还看不到它（那时确实没有），之后能看到。
    console.hidden_credential_reads = 1

    outcome = _run(tmp_path, console)

    assert outcome.credential_written is False
    assert outcome.state == "ready"


# ── 7. 真实响应形状（评审在 run 39577679698 上逐条复现出来的） ───────────


def test_the_bundled_template_passes_the_repos_own_dsl_validators() -> None:
    """模板必须过**我们自己**的 DSL 校验器，否则 41 上第一次导入就失败。

    三个被复现的形状缺陷都钉在这里：``version`` 必须是 0.7.x（我们支持的那个）、
    ``model.name`` 非空（本模板刻意留空 ⇒ 这里只校验版本与 datasets 形状）、
    ``dataset_configs.datasets`` **不能是列表**（Dify 读的是路由对象，给列表会在
    ``.get()`` 上抛 ``AttributeError``）。
    """
    import yaml

    from aiops_diagnostics.dify_dsl_pull import _require_supported_version
    from aiops_diagnostics.dify_workspace_ready import bundled_template

    parsed = yaml.safe_load(bundled_template())

    _require_supported_version(parsed["version"])
    datasets = (parsed["model_config"].get("dataset_configs") or {}).get("datasets")
    assert datasets is None, "dataset_configs.datasets 必须是路由对象或缺席，绝不能是列表"


def test_plugin_ids_are_read_from_the_top_level_field(tmp_path: Path, console: _Console) -> None:
    """真实 ``/plugin/list`` 把标识放在**顶层**，``meta`` 是任意 map。

    变异对照：只读 ``meta.plugin_unique_identifier`` ⇒ 本用例红（每次都重发安装请求）。
    """
    console.plugin_list_shape = "top-level"
    workspaces = _workspaces(tmp_path)

    first = _run(tmp_path, console, workspaces)
    second = _run(tmp_path, console, workspaces)

    assert first.plugins_installed == DEFAULT_PLUGIN_IDENTIFIERS
    # 第二次必须"已经装好了"⇒ 一条安装请求都不发。读错字段时这里是 2。
    assert console.post_paths().count(_PLUGIN_INSTALL) == 1
    assert second.plugins_installed == ()


def test_a_failed_re_run_never_demotes_a_ready_workspace(tmp_path: Path, console: _Console) -> None:
    """``created → ready`` 单向。重跑里的一次读取抖动不能把它降级，否则没人再推得回去。

    变异对照：去掉 ``mark_template_failed`` 里的 ``AND setup_state <> 'ready'`` ⇒ 本用例红。
    """
    workspaces = _workspaces(tmp_path)
    _run(tmp_path, console, workspaces)
    assert workspaces.row(TENANT, ENVIRONMENT)["setup_state"] == "ready"

    # 模拟**读取**失败（不是导入）：模板 app 已在，`app_names` 这一步 503。
    console.apps_status = 503
    console.apps_body = {"message": "upstream down"}
    with pytest.raises(EnsureError):
        _run(tmp_path, console, workspaces)

    row = workspaces.row(TENANT, ENVIRONMENT)
    assert row["setup_state"] == "ready", "已 ready 的行不能被一次失败的重跑降级"
    assert "upstream down" in row["last_error"], "但原因要留下"


def test_a_pending_import_is_reported_as_pending_not_unreadable(tmp_path: Path, console: _Console) -> None:
    """202/pending 是"等人确认"，不是"回了个看不懂的响应" —— 下一步完全不同。"""
    console.import_status = 202
    console.import_body = {"id": "imp-1", "status": "pending", "app_id": None}
    workspaces = _workspaces(tmp_path)

    with pytest.raises(EnsureError) as caught:
        _run(tmp_path, console, workspaces)

    assert caught.value.code == "DIFY_IMPORT_PENDING"
    assert "imp-1" in caught.value.message


def test_an_import_failure_carries_difys_own_reason(tmp_path: Path, console: _Console) -> None:
    """Dify 的导入失败体把原因放在 ``error`` 键；``bounded_http`` 默认不读它。"""
    console.import_status = 400
    console.import_body = {"id": "imp-2", "status": "failed", "error": "Missing app data in YAML content"}
    workspaces = _workspaces(tmp_path)

    with pytest.raises(EnsureError) as caught:
        _run(tmp_path, console, workspaces)

    assert "Missing app data" in caught.value.message


def test_the_endpoint_from_the_config_file_is_not_silently_replaced(tmp_path: Path) -> None:
    """``AIOPS_GATEWAY_DIFY_WORKSPACE_MODEL_ENDPOINT`` 有非空默认值，所以
    "属性为空才补"的合并对它是死代码 —— 运维照着 runbook 写进 ``--config`` 也不生效。

    变异对照：把 ``_settings_with_config_file`` 的判据换回 ``not getattr(...)`` ⇒ 本用例红。
    """
    import os

    from aiops_diagnostics.admin_cli import _settings_with_config_file

    config = tmp_path / "production.env"
    config.write_text(
        "AIOPS_GATEWAY_ENVIRONMENT=41\n"
        "AIOPS_GATEWAY_DIFY_WORKSPACE_MODEL_ENDPOINT=https://relay.example/v1\n",
        encoding="utf-8",
    )
    os.chmod(config, 0o600)
    saved = os.environ.pop("AIOPS_GATEWAY_DIFY_WORKSPACE_MODEL_ENDPOINT", None)
    try:
        settings = _settings_with_config_file(_Ctx(config))
    finally:
        if saved is not None:
            os.environ["AIOPS_GATEWAY_DIFY_WORKSPACE_MODEL_ENDPOINT"] = saved

    assert settings.dify_workspace_model_endpoint == "https://relay.example/v1"


class _Ctx:
    """``typer.Context`` 的最小替身：`_settings_with_config_file` 只读 ``obj['config_file']``。"""

    def __init__(self, config: Path) -> None:
        self.obj = {"config_file": str(config)}


def test_app_names_walks_every_page(tmp_path: Path, console: _Console) -> None:
    """模板 app 落在第 2 页时不能重复导入 —— 翻页判据是 ``has_more``。

    变异对照：把 ``app_names`` 改回只读第 1 页 ⇒ 本用例红（第二次跑会再导一份同名 app）。
    """
    console.page_size = 2
    console.apps = ["a1", "a2", APP_NAME]  # 模板在第 2 页
    workspaces = _workspaces(tmp_path)

    _run(tmp_path, console, workspaces)
    console.calls.clear()
    outcome = _run(tmp_path, console, workspaces)

    assert outcome.app_id == "", "模板已在第 2 页，不该再导一次"
    assert console.post_paths() == []


def test_a_credential_name_taken_on_another_provider_is_not_treated_as_done(
    tmp_path: Path, console: _Console
) -> None:
    """``already exists`` 的重名判据含 provider：别的 provider 上有同名凭据不代表我们要的
    provider 上也有。错判的后果是 workspace 被标 ready 而模型根本没有凭据。

    变异对照：只看 ``"already exists" in message`` ⇒ 本用例红（凭据没写却报成功）。
    """
    console.other_provider_credential = CREDENTIAL_NAME

    with pytest.raises(EnsureError) as caught:
        _run(tmp_path, console)

    assert caught.value.code == "DIFY_CREDENTIAL_NAME_TAKEN"
    assert "另一个 provider" in caught.value.message


def test_import_warnings_reach_the_operator(tmp_path: Path, console: _Console) -> None:
    """``completed-with-warnings`` 是成功但有损；采集了就必须说出来。"""
    console.import_status = 200
    console.import_body = {
        "app_id": "app-1",
        "status": "completed-with-warnings",
        "warnings": [{"code": "tool_not_found", "message": "tool provider unresolved"}],
    }

    outcome = _run(tmp_path, console)

    assert outcome.template_warnings == ("tool provider unresolved",)
    assert "tool provider unresolved" in outcome.note


def test_an_existing_but_stale_credential_is_switched_to_ours(tmp_path: Path, console: _Console) -> None:
    """迁移过的 workspace 里，旧凭据仍被 Dify 当作"当前" —— 写完必须切过去。

    2026-10-22 在 41 的租户 1942 上实测到这个：`ensure-ready` 把 `aiops-workspace` 写进去
    并推到 `ready`，真实对话却 401，因为生效的是迁移时复制过来、key 已失效的
    `deepseek-official`。Dify 按 `created_at DESC` 选默认，不按"最新写入"。

    变异对照：去掉 `activate_provider_credential` 那一步 ⇒ 本用例红（`switched_to` 为 None，
    而 current 仍是旧名字）。
    """
    console.credentials.append("deepseek-official")  # 迁移带来的旧凭据
    console.pinned_current = "deepseek-official"  # Dify 把它当作当前

    outcome = _run(tmp_path, console)

    assert outcome.credential_written is True
    assert outcome.credential_activated is True, "写了新凭据但没切过去 —— 这正是 41 上的 401"
    assert console.switched_to is not None
    assert outcome.state == "ready"


def test_a_rerun_does_not_switch_again(tmp_path: Path, console: _Console) -> None:
    """幂等：已经是我们那份就不发 switch。"""
    workspaces = _workspaces(tmp_path)
    _run(tmp_path, console, workspaces)
    console.switched_to = None
    console.calls.clear()

    outcome = _run(tmp_path, console, workspaces)

    assert outcome.credential_activated is False
    assert console.switched_to is None
    assert console.post_paths() == []
