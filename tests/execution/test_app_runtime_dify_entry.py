"""APP-RUNTIME-02：App 聊天 → Dify Workflow → 受限取数 → 原 GET。

这一组测的是**接线与判据**，跑在本仓真实代码上：

  * 真实的绑定表解析（`engine` 判别、半配拒绝、旧节点登记兼容）；
  * 真实的取数凭据（签发、过期、撤销、一次性消费）；
  * 真实执行记录（`chat_dify_runs`）；
  * 真实的 Dify 路线 worker 与回调端点。

替身只有两类：**Dify 服务本身**（一个本地 HTTP 桩，因为本文件不是整链验收 ——
真 Dify 的整链在 `tools/m2_close/m3node/verify_dify_chat_entry.py`）与 CPMS（合成上游）。

这一组的强判据是**计数**：授权不满足时业务请求必须是 0；正常对照必须证明不是"全拒假绿"。
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aiops_diagnostics.config import Settings
from aiops_diagnostics.dify_run_store import DifyRunStore
from aiops_diagnostics.execution import chat_data_grant, chat_execution
from aiops_diagnostics.execution.agent_runs import AgentRunStore, scope_to_json
from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_runtime import GatewayRuntime
from aiops_diagnostics.gateway_store import GatewayStore
from execution.test_app_runtime_node_entry import (
    ORDER_A,
    ORDER_B,
    _bindings_file,
    _cpms,
    _Directory,
    _orders_source,
    _Resolver,
)
from execution.test_m2_close_contract import Orders, _write_config
from execution.test_m3_gateway_exec_config import _write_manifest

HEADERS = {"Authorization": "Bearer token-a", "X-Business-Entry": "consumer"}
LATEST = "check my latest order status"
WORKFLOW_ID = "11111111-2222-3333-4444-555555555555"


# --------------------------------------------------------------------------
# Dify 的 HTTP 桩：只扮演"编排平台"，不扮演网关
# --------------------------------------------------------------------------


class _DifyStub:
    """模拟 Dify Service API 的两个端点（run / stop），并**自己**去调网关的取数端点。

    这一点是刻意的：真 Dify 的 HTTP 步骤是它自己去调网关的，桩也必须这样 ——
    否则测到的就是"Python 替 Dify 取数"，而那正是本片要排除的形态。
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.stops: list[str] = []
        self.paths: list[str] = []
        self.before_started = None
        self.stop_status = 200
        self.latest_workflow_id = WORKFLOW_ID
        self.identity_override: dict = {}
        self.finish_override: dict = {}
        self.drop_finish = False
        self.fail = False
        # 真 Dify 的 HTTP 步骤把响应体当文本，端节点拿回的是 JSON **字符串**。
        # 这几个开关让桩能复现那一种形状（以及三种应当被拒的形状）。
        self.snapshot_as_text = False
        self.snapshot_extra: dict = {}
        self.snapshot_raw_text = ""
        # 取数回调完成之后、返回结果之前的一个钩子（模拟"运维在这一轮期间改了登记"）。
        self.after_callback = None
        self.skip_callback = False
        self.fake_snapshot: dict = {}
        self.gateway_base = ""
        self.token_seen: list[str] = []
        self.runs: dict[str, dict] = {}
        self._server = None

    def start(self) -> str:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def _reply(self, payload: dict, status: int = 200) -> None:
                if getattr(self, "streaming", False):
                    if not stub.drop_finish:
                        payload["event"] = "workflow_finished"
                        payload.update(stub.finish_override)
                        self._event(payload)
                    return
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _event(self, payload):
                self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
                self.wfile.flush()

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = json.loads(self.rfile.read(length) or b"{}")
                if self.path.endswith("/stop"):
                    task_id = self.path.split("/")[-2]
                    stub.stops.append(task_id)
                    return self._reply({"result": "success"}, status=stub.stop_status)
                stub.calls.append(raw)
                stub.paths.append(self.path)
                if stub.fail:
                    return self._reply({"message": "boom"}, status=500)
                run_id = "run-" + str(len(stub.calls))
                selected = (
                    self.path.split("/")[-2] if self.path != "/v1/workflows/run" else stub.latest_workflow_id
                )
                self.selected_workflow = selected
                if raw.get("response_mode") == "streaming":
                    self.streaming = True
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    if stub.before_started:
                        stub.before_started()
                    event = {
                        "event": "workflow_started",
                        "task_id": "task-" + run_id,
                        "workflow_run_id": run_id,
                        "data": {"id": run_id, "workflow_id": selected},
                    }
                    event.update(stub.identity_override)
                    self._event(event)
                inputs = raw.get("inputs") or {}
                token = inputs.get("data_grant") or ""
                stub.token_seen.append(token)
                if stub.skip_callback:
                    # "图没调取数端点"这一种：不回调网关，直接给出一个形状合法的快照。
                    return self._reply(
                        {
                            "task_id": "task-" + run_id,
                            "workflow_run_id": run_id,
                            "data": {
                                "id": run_id,
                                "workflow_id": self.selected_workflow,
                                "status": "succeeded",
                                "outputs": {"answer": "ok", "snapshot": stub.fake_snapshot},
                            },
                        }
                    )
                # **证据点**：桩要用这个 token 自己去网关取数，成功与否决定 run 的结果。
                import urllib.error
                import urllib.request

                request = urllib.request.Request(
                    f"{stub.gateway_base}/v1/internal/charging-status/dify-read",
                    data=b"",
                    method="POST",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                )
                try:
                    with urllib.request.urlopen(request, timeout=20) as response:
                        snapshot = json.loads(response.read().decode())
                    status = response.status
                except urllib.error.HTTPError as exc:
                    snapshot = {"error": exc.code, "body": exc.read().decode()[:200]}
                    status = exc.code
                except Exception as exc:  # noqa: BLE001
                    snapshot, status = {"error": type(exc).__name__}, 0
                if status != 200:
                    return self._reply(
                        {
                            "task_id": "task-" + run_id,
                            "workflow_run_id": run_id,
                            "data": {
                                "id": run_id,
                                "workflow_id": self.selected_workflow,
                                "status": "failed",
                                "outputs": {},
                            },
                        }
                    )
                stub.runs[run_id] = snapshot
                if stub.after_callback is not None:
                    stub.after_callback()
                if stub.snapshot_raw_text:
                    snapshot = stub.snapshot_raw_text
                elif stub.snapshot_as_text:
                    merged = {**snapshot, **stub.snapshot_extra} if isinstance(snapshot, dict) else snapshot
                    snapshot = json.dumps(merged, ensure_ascii=False)
                # **真 Dify 1.17.1 的 blocking 形状**：身份在外层、结果在 `data`。
                # 桩必须照抄这个两层形状 —— 桩把它拍平过一次，结果真链上取不到 task_id
                # 而单测全绿。
                return self._reply(
                    {
                        "task_id": "task-" + run_id,
                        "workflow_run_id": run_id,
                        "data": {
                            "id": run_id,
                            "workflow_id": self.selected_workflow,
                            "status": "succeeded",
                            "outputs": {"answer": "ok", "snapshot": snapshot},
                            "created_at": int(time.time()),
                        },
                    }
                )

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self._server.server_port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


@pytest.fixture()
def dify_stub():
    stub = _DifyStub()
    base = stub.start()
    try:
        yield stub, base
    finally:
        stub.stop()


# --------------------------------------------------------------------------
# 夹具：真 Gateway + Dify 路线绑定
# --------------------------------------------------------------------------


@contextmanager
def dify_chat(tmp_path, monkeypatch, *, stub_base: str, engine: str = "dify"):
    with _cpms({ORDER_A, ORDER_B}) as upstream:
        monkeypatch.setattr("aiops_diagnostics.gateway_api.scoped_mysql_source", _orders_source)
        production = tmp_path / "production.env"
        production.write_text("# synthetic\n")
        production.chmod(0o600)

        key_file = tmp_path / "dify.key"
        key_file.write_text("app-service-key-not-a-real-one\n")
        key_file.chmod(0o600)
        entries: list[dict[str, str]] = [
            {
                "engine": engine,
                "environment": "env-a",
                "tenant_id": "TENANT-A",
                "entry": "consumer",
                "scenario": chat_execution.SCENARIO_OWN_ORDER_STATUS,
                "workflow_id": WORKFLOW_ID,
                "workflow_key_file": str(key_file),
            }
        ]
        bindings = _bindings_file(tmp_path / "chat-bindings.json", entries)

        settings = GatewayServerSettings(
            data_home=tmp_path,
            database_file=tmp_path / "gateway.db",
            server_config_file=production,
            consumer_order_chat_enabled=True,
            charging_status_base_url=upstream["base"],
            execution_environment="env-a",
            agent_run_config_file="",
            chat_execution_bindings_file=str(bindings),
            dify_chat_base_url=stub_base,
        )
        store = GatewayStore(settings.database_file)
        runtime = GatewayRuntime(store, settings, Settings())
        runtime.agent_run_store = AgentRunStore(settings.database_file)
        orders = Orders()
        orders.ownership = {ORDER_A: "USER-A", ORDER_B: "USER-A"}
        app = create_gateway_app(
            settings=settings,
            store=store,
            runtime=runtime,
            caller_resolver=_Resolver(),
            order_authorizer=orders,
            platform_resolver=PlatformIdentityResolver(_Directory()),
            faq_catalog=FAQCatalog.bundled(),
        )
        # 真实监听：Dify 桩要**自己**打回来，TestClient 不是真实 socket。
        import socket

        import uvicorn

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        client = TestClient(app)
        live = app.state.gateway.settings

        class _Upstream:
            calls = property(lambda self: upstream["state"]["calls"])

            def __getitem__(self, key):
                return self.calls if key == "calls" else upstream[key]

        yield {
            "app": app,
            "client": client,
            "settings": live,
            "orders": orders,
            "upstream": _Upstream(),
            "bindings": bindings,
            "key_file": key_file,
            "db": settings.database_file,
            "gateway_base": f"http://127.0.0.1:{port}",
        }
        client.close()
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()


def ask(client, question=LATEST):
    return client.post("/v1/assistant/questions", headers=HEADERS, json={"question": question})


def wait_terminal(client, qa_id: str, *, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/v1/assistant/questions/{qa_id}", headers=HEADERS)
        if response.status_code == 200:
            last = response.json()
            if last.get("status") in {"completed", "failed", "cancelled", "expired"}:
                return last
        time.sleep(0.02)
    return last


# --------------------------------------------------------------------------
# 绑定表：engine 判别
# --------------------------------------------------------------------------


def test_a_node_binding_without_engine_still_loads(tmp_path):
    """旧节点登记不写 `engine` ⇒ 按 node 读。兼容既有部署是硬要求。"""
    config = _write_config(tmp_path)
    manifest = _write_manifest(tmp_path)
    path = _bindings_file(
        tmp_path / "b.json",
        [
            {
                "environment": "env-a",
                "tenant_id": "TENANT-A",
                "entry": "consumer",
                "scenario": chat_execution.SCENARIO_OWN_ORDER_STATUS,
                "config_file": str(config),
                "manifest_file": str(manifest),
            }
        ],
    )
    bindings = chat_execution.load_bindings(path)
    assert bindings[0].engine == chat_execution.ENGINE_NODE


def test_a_dify_binding_must_not_carry_node_config(tmp_path):
    """Dify 登记带节点配置 ⇒ 拒绝：否则读者会以为它还在跑节点。"""
    key_file = tmp_path / "k"
    key_file.write_text("k\n")
    key_file.chmod(0o600)
    path = _bindings_file(
        tmp_path / "b.json",
        [
            {
                "engine": "dify",
                "environment": "env-a",
                "tenant_id": "TENANT-A",
                "entry": "consumer",
                "scenario": chat_execution.SCENARIO_OWN_ORDER_STATUS,
                "workflow_id": WORKFLOW_ID,
                "workflow_key_file": str(key_file),
                "config_file": "/somewhere",
            }
        ],
    )
    with pytest.raises(chat_execution.ChatBindingError):
        chat_execution.load_bindings(path)


def test_two_bindings_for_one_scenario_are_refused(tmp_path):
    """同一场景两条登记 = 这个场景该走哪个引擎没有唯一答案。拒绝，不按文件顺序裁决。"""
    config = _write_config(tmp_path)
    manifest = _write_manifest(tmp_path)
    row = {
        "environment": "env-a",
        "tenant_id": "TENANT-A",
        "entry": "consumer",
        "scenario": chat_execution.SCENARIO_OWN_ORDER_STATUS,
        "config_file": str(config),
        "manifest_file": str(manifest),
    }
    path = _bindings_file(tmp_path / "b.json", [row, dict(row)])
    with pytest.raises(chat_execution.ChatBindingError):
        chat_execution.load_bindings(path)


def test_an_unknown_engine_is_refused(tmp_path):
    key_file = tmp_path / "k"
    key_file.write_text("k\n")
    key_file.chmod(0o600)
    path = _bindings_file(
        tmp_path / "b.json",
        [
            {
                "engine": "difyx",
                "environment": "env-a",
                "tenant_id": "TENANT-A",
                "entry": "consumer",
                "scenario": chat_execution.SCENARIO_OWN_ORDER_STATUS,
                "workflow_id": WORKFLOW_ID,
                "workflow_key_file": str(key_file),
            }
        ],
    )
    with pytest.raises(chat_execution.ChatBindingError):
        chat_execution.load_bindings(path)


# --------------------------------------------------------------------------
# 取数凭据
# --------------------------------------------------------------------------


def test_a_grant_is_unusable_once_revoked(tmp_path):
    store = chat_data_grant.DataGrantStore(tmp_path / "g.db")
    token, grant = store.issue(
        scope_fingerprint="f" * 64,
        order_no=ORDER_A,
        environment="env-a",
        tenant_id="TENANT-A",
        qa_id="qa_x",
        workflow_id=WORKFLOW_ID,
    )
    assert chat_data_grant.usable(store.by_token(token)) is not None
    store.revoke(grant.grant_id)
    with pytest.raises(chat_data_grant.DataGrantError) as raised:
        chat_data_grant.usable(store.by_token(token))
    assert raised.value.code == "GRANT_REVOKED"


def test_a_grant_is_unusable_once_expired(tmp_path):
    store = chat_data_grant.DataGrantStore(tmp_path / "g.db")
    token, grant = store.issue(
        scope_fingerprint="f" * 64,
        order_no=ORDER_A,
        environment="env-a",
        tenant_id="TENANT-A",
        qa_id="qa_x",
        workflow_id=WORKFLOW_ID,
    )
    import sqlite3
    from datetime import UTC, datetime, timedelta

    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(tmp_path / "g.db") as conn:
        conn.execute("UPDATE chat_data_grants SET expires_at = ? WHERE grant_id = ?", (past, grant.grant_id))
    with pytest.raises(chat_data_grant.DataGrantError) as raised:
        chat_data_grant.usable(store.by_token(token))
    assert raised.value.code == "GRANT_EXPIRED"


def test_a_grant_can_only_be_consumed_once(tmp_path):
    store = chat_data_grant.DataGrantStore(tmp_path / "g.db")
    _token, grant = store.issue(
        scope_fingerprint="f" * 64,
        order_no=ORDER_A,
        environment="env-a",
        tenant_id="TENANT-A",
        qa_id="qa_x",
        workflow_id=WORKFLOW_ID,
    )
    assert store.consume(grant.grant_id) is True
    assert store.consume(grant.grant_id) is False


def test_the_token_is_not_stored_in_plain_text(tmp_path):
    """库里只有哈希：一个能读库的人不该拿到可用的凭据。"""
    path = tmp_path / "g.db"
    store = chat_data_grant.DataGrantStore(path)
    token, _grant = store.issue(
        scope_fingerprint="f" * 64,
        order_no=ORDER_A,
        environment="env-a",
        tenant_id="TENANT-A",
        qa_id="qa_x",
        workflow_id=WORKFLOW_ID,
    )
    raw = path.read_bytes()
    assert token.encode() not in raw


# --------------------------------------------------------------------------
# 整条路线（Dify 由桩扮演；它自己调网关取数）
# --------------------------------------------------------------------------


def test_the_dify_route_delivers_the_same_run_snapshot(tmp_path, monkeypatch, dify_stub):
    stub, base = dify_stub
    with dify_chat(tmp_path, monkeypatch, stub_base=base) as f:
        stub.gateway_base = f["gateway_base"]
        response = ask(f["client"])
        assert response.status_code == 202, response.text
        final = wait_terminal(f["client"], response.json()["qa_id"])
        assert final["status"] == "completed", final
        assert ORDER_A in final["result"]["blocks"][0]["text"]
        # 桩确实被调用了一次，且它自己用凭据去取了数。
        assert len(stub.calls) == 1
        assert (
            stub.token_seen
            and stub.token_seen[0].startswith("ccdg_")
            or stub.token_seen[0].startswith("cdg_")
        )
        # 执行记录：真 run 身份 + succeeded；**没有**伪造 node/match 语义。
        run = DifyRunStore(f["db"]).by_qa(response.json()["qa_id"])
        assert run is not None and run.state == "succeeded"
        assert run.workflow_run_id and run.task_id
        assert run.workflow_id == WORKFLOW_ID
        # 业务只读一次。
        assert f["upstream"]["calls"] == 1
        # 完成之后凭据被撤销。
        states = [g.state for g in _grants(f["db"])]
        assert states == ["revoked"]


def test_a_snapshot_returned_as_json_text_is_accepted(tmp_path, monkeypatch, dify_stub):
    """真 Dify 的 HTTP 步骤把响应体当**文本**，端节点交回来的是 JSON 字符串。

    这一条是照着真链的实测形状写的：桩返回 dict，真 Dify 返回 str —— 两种都必须能放行，
    但**只有**恰好那五个字段的才算数。
    """
    stub, base = dify_stub
    with dify_chat(tmp_path, monkeypatch, stub_base=base) as f:
        stub.gateway_base = f["gateway_base"]
        stub.snapshot_as_text = True
        response = ask(f["client"])
        final = wait_terminal(f["client"], response.json()["qa_id"])
        assert final["status"] == "completed", final
        assert ORDER_A in json.dumps(final["result"], ensure_ascii=False)


def test_a_snapshot_text_with_extra_fields_is_refused(tmp_path, monkeypatch, dify_stub):
    """多一个字段就不收：那说明有人往里塞了别的东西。"""
    stub, base = dify_stub
    with dify_chat(tmp_path, monkeypatch, stub_base=base) as f:
        stub.gateway_base = f["gateway_base"]
        stub.snapshot_as_text = True
        stub.snapshot_extra = {"plate_no": "DO-NOT-LEAK"}
        response = ask(f["client"])
        final = wait_terminal(f["client"], response.json()["qa_id"])
        assert final["status"] == "failed", final
        assert final.get("result") in (None, {})


def test_a_snapshot_text_that_is_not_json_is_refused(tmp_path, monkeypatch, dify_stub):
    """文本不是 JSON ⇒ 拒绝，不是"当成空快照放行"。"""
    stub, base = dify_stub
    with dify_chat(tmp_path, monkeypatch, stub_base=base) as f:
        stub.gateway_base = f["gateway_base"]
        stub.snapshot_raw_text = "订单状态正常，无异常。"
        response = ask(f["client"])
        final = wait_terminal(f["client"], response.json()["qa_id"])
        assert final["status"] == "failed", final
        assert final.get("result") in (None, {})


def test_a_successful_readback_is_refused_after_its_ttl(tmp_path, monkeypatch, dify_stub):
    """成功记录过了有效期，回读正文必须是 410 —— `get()` 的过期收敛只处理 running。

    这一条照着真实现场写过：把 `expires_at` 改成过去时间之后，`get()` 依旧返回
    succeeded，于是"回读必须看 TTL"在成功路径上从未生效过。
    """
    stub, base = dify_stub
    with dify_chat(tmp_path, monkeypatch, stub_base=base) as f:
        stub.gateway_base = f["gateway_base"]
        response = ask(f["client"])
        final = wait_terminal(f["client"], response.json()["qa_id"])
        assert final["status"] == "completed", final
        import sqlite3
        from datetime import UTC, datetime, timedelta

        past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        with sqlite3.connect(f["db"]) as conn:
            conn.execute("UPDATE chat_dify_runs SET expires_at = ?", (past,))
        again = f["client"].get(f"/v1/assistant/questions/{response.json()['qa_id']}", headers=HEADERS)
        assert again.status_code == 410, again.text


def test_the_callback_refuses_after_the_binding_is_removed(tmp_path, monkeypatch, dify_stub):
    """签发之后把绑定换掉 ⇒ 旧凭据连**缓存回放**都不能再交数据。

    只看 run 与 grant 里冻结的值是不够的：那两份都是签发那一刻的快照，彼此一致
    说明不了"现在还能不能走 Dify"。
    """
    stub, base = dify_stub
    with dify_chat(tmp_path, monkeypatch, stub_base=base) as f:
        runs = DifyRunStore(f["db"])
        scope = _Resolver().resolve("token-a", required_scope="x")
        token, grant = chat_data_grant.DataGrantStore(f["db"]).issue(
            scope_fingerprint=scope.scope_fingerprint,
            order_no=ORDER_A,
            environment="env-a",
            tenant_id="TENANT-A",
            qa_id="qa_bind",
            workflow_id=WORKFLOW_ID,
        )
        runs.issue(
            run_id="dify_bind",
            qa_id="qa_bind",
            scope_fingerprint=scope.scope_fingerprint,
            scope_json=scope_to_json(scope),
            binding_key="consumer_own_order_status:env-a:TENANT-A",
            order_no=ORDER_A,
            environment="env-a",
            tenant_id="TENANT-A",
            app_id=WORKFLOW_ID,
            workflow_id=WORKFLOW_ID,
            grant_id=grant.grant_id,
        )
        # 把登记换成 node 引擎：绑定表还在，但这条路线已经不该再跑了。
        _bindings_file(
            f["bindings"],
            [
                {
                    "engine": "node",
                    "environment": "env-a",
                    "tenant_id": "TENANT-A",
                    "entry": "consumer",
                    "scenario": chat_execution.SCENARIO_OWN_ORDER_STATUS,
                    "config_file": str(_write_config(tmp_path)),
                    "manifest_file": str(_write_manifest(tmp_path)),
                }
            ],
        )
        refused = f["client"].post(
            "/v1/internal/charging-status/dify-read", headers={"Authorization": f"Bearer {token}"}
        )
        assert refused.status_code == 403, refused.text
        assert f["upstream"]["calls"] == 0


def test_a_success_without_this_round_snapshot_is_refused(tmp_path, monkeypatch, dify_stub):
    """Dify 直接给出一个形状合法的快照、但本轮**没取过数** ⇒ failed，不是 completed。"""
    stub, base = dify_stub
    with dify_chat(tmp_path, monkeypatch, stub_base=base) as f:
        stub.gateway_base = f["gateway_base"]
        stub.skip_callback = True
        stub.fake_snapshot = {
            "order_no": ORDER_A,
            "status_code": "0",
            "status_role": "charging",
            "observed_at": "2026-10-24T00:00:00+00:00",
            "source": "cpms.chargingInfo",
        }
        response = ask(f["client"])
        final = wait_terminal(f["client"], response.json()["qa_id"])
        run = DifyRunStore(f["db"]).by_qa(response.json()["qa_id"])
        # 断言**这一条**理由，不是"反正失败了"：把判据删掉之后，同一轮会被
        # `succeed()` 拒（它要求已绑上 workflow run）而依旧 failed —— 只写 failed
        # 的话，这条用例在变异下照样绿，等于没守任何东西。
        assert run is not None and run.reason == "dify_no_data", {
            "run_reason": run.reason if run else None,
            "final": final,
        }
        assert final["status"] == "failed", {
            "final": final,
            "run_state": run.state if run else None,
            "run_reason": run.reason if run else None,
            "calls": list(stub.calls),
        }
        assert f["upstream"]["calls"] == 0


def test_the_readback_refuses_when_the_binding_is_removed_after_the_authorization(
    tmp_path, monkeypatch, dify_stub
):
    """回读：外部授权**回来之后**绑定被换掉 ⇒ 不再交付正文。

    上面那次绑定检查在授权之前，挡不住"授权等待期间运维改了登记"这段窗口。
    用授权替身在 `can_access` 里换掉绑定表来复现那个时序 —— 不靠 sleep 赛跑。
    """
    stub, base = dify_stub
    with dify_chat(tmp_path, monkeypatch, stub_base=base) as f:
        stub.gateway_base = f["gateway_base"]
        response = ask(f["client"])
        final = wait_terminal(f["client"], response.json()["qa_id"])
        assert final["status"] == "completed", final

        orders = f["orders"]
        original = orders.can_access

        def swap_then_allow(context, order_no):
            _bindings_file(
                f["bindings"],
                [
                    {
                        "engine": "node",
                        "environment": "env-a",
                        "tenant_id": "TENANT-A",
                        "entry": "consumer",
                        "scenario": chat_execution.SCENARIO_OWN_ORDER_STATUS,
                        "config_file": str(_write_config(tmp_path)),
                        "manifest_file": str(_write_manifest(tmp_path)),
                    }
                ],
            )
            return original(context, order_no)

        monkeypatch.setattr(orders, "can_access", swap_then_allow)
        again = f["client"].get(f"/v1/assistant/questions/{response.json()['qa_id']}", headers=HEADERS)
        assert again.status_code == 404, again.text
        assert not (again.json().get("result") or {}).get("blocks")


def test_the_worker_refuses_to_succeed_when_the_binding_changed(tmp_path, monkeypatch, dify_stub):
    """worker 写成功之前绑定被换掉 ⇒ 落 failed，且不写会话正文。

    **换绑定的时机很关键**：必须在**取数之后**。在取数时换，回调自己就会 403，
    桩把整轮判失败 —— 那种用例是"因为别的原因也 failed"，一条变异都锁不住
    （实测：删掉这条守卫照样绿）。这里用桩的 `after_callback` 钩子在取数完成之后换。

    不核这一条的话，这一轮会被写成 succeeded/completed 并写入会话答案，
    而 GET 守卫随后持续拒绝它 —— 用户既拿不到正文，也拿不到正常的失败终态。
    """
    stub, base = dify_stub
    with dify_chat(tmp_path, monkeypatch, stub_base=base) as f:
        stub.gateway_base = f["gateway_base"]
        swapped = {"done": False}

        def swap_after_callback() -> None:
            if swapped["done"]:
                return
            swapped["done"] = True
            _bindings_file(
                f["bindings"],
                [
                    {
                        "engine": "node",
                        "environment": "env-a",
                        "tenant_id": "TENANT-A",
                        "entry": "consumer",
                        "scenario": chat_execution.SCENARIO_OWN_ORDER_STATUS,
                        "config_file": str(_write_config(tmp_path)),
                        "manifest_file": str(_write_manifest(tmp_path)),
                    }
                ],
            )

        stub.after_callback = swap_after_callback
        response = ask(f["client"])
        final = wait_terminal(f["client"], response.json()["qa_id"])
        run = DifyRunStore(f["db"]).by_qa(response.json()["qa_id"])
        assert swapped["done"] is True, f"取数回调没有被走到：{final}"
        assert final["status"] == "failed", final
        assert not (final.get("result") or {}).get("blocks")
        assert run is not None and run.state == "failed", run.state if run else None


def _grants(database: Path) -> list[chat_data_grant.DataGrant]:
    import sqlite3

    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        store = chat_data_grant.DataGrantStore(database)
        return [store.by_id(row["grant_id"]) for row in conn.execute("SELECT grant_id FROM chat_data_grants")]
    finally:
        conn.close()


def test_upstream_failure_never_becomes_a_success(tmp_path, monkeypatch, dify_stub):
    stub, base = dify_stub
    with dify_chat(tmp_path, monkeypatch, stub_base=base) as f:
        stub.gateway_base = f["gateway_base"]
        stub.fail = True
        response = ask(f["client"])
        final = wait_terminal(f["client"], response.json()["qa_id"])
        assert final["status"] == "failed", final
        assert final.get("result") in (None, {})
        # 失败不读业务、不换引擎。
        assert f["upstream"]["calls"] == 0
        run = DifyRunStore(f["db"]).by_qa(response.json()["qa_id"])
        assert run is not None and run.state == "failed"


def test_the_callback_refuses_a_bad_grant(tmp_path, monkeypatch, dify_stub):
    """凭据不对 ⇒ 401，且**一次业务读取都没有**。"""
    stub, base = dify_stub
    with dify_chat(tmp_path, monkeypatch, stub_base=base) as f:
        response = f["client"].post(
            "/v1/internal/charging-status/dify-read",
            headers={"Authorization": "Bearer cdg_forged_token_value"},
        )
        assert response.status_code == 401, response.text
        assert f["upstream"]["calls"] == 0


def test_the_callback_returns_the_same_snapshot_without_reading_twice(tmp_path, monkeypatch, dify_stub):
    """重复回调只回同轮快照：第二次**不**再读业务。"""
    stub, base = dify_stub
    with dify_chat(tmp_path, monkeypatch, stub_base=base) as f:
        store = chat_data_grant.DataGrantStore(f["db"])
        token, _grant = store.issue(
            scope_fingerprint=_Resolver().resolve("token-a", required_scope="x").scope_fingerprint,
            order_no=ORDER_A,
            environment="env-a",
            tenant_id="TENANT-A",
            qa_id="qa_manual",
            workflow_id=WORKFLOW_ID,
        )
        # 造一条执行记录指向这个 QA
        runs = DifyRunStore(f["db"])
        scope = _Resolver().resolve("token-a", required_scope="x")
        runs.issue(
            run_id="dify_manual",
            qa_id="qa_manual",
            scope_fingerprint=scope.scope_fingerprint,
            scope_json=scope_to_json(scope),
            binding_key="consumer_own_order_status:env-a:TENANT-A",
            order_no=ORDER_A,
            environment="env-a",
            tenant_id="TENANT-A",
            app_id=WORKFLOW_ID,
            workflow_id=WORKFLOW_ID,
            grant_id=_grant.grant_id,
        )
        first = f["client"].post(
            "/v1/internal/charging-status/dify-read", headers={"Authorization": f"Bearer {token}"}
        )
        assert first.status_code == 200, first.text
        second = f["client"].post(
            "/v1/internal/charging-status/dify-read", headers={"Authorization": f"Bearer {token}"}
        )
        assert second.status_code == 200, second.text
        assert second.json() == first.json()
        assert f["upstream"]["calls"] == 1, "a replay must not read the business again"


def test_an_unregistered_tenant_still_takes_the_direct_read(tmp_path, monkeypatch, dify_stub):
    """绑定表里没有这个租户 ⇒ 原直读，Dify 一次都不调（兼容与隔离）。"""
    stub, base = dify_stub
    with dify_chat(tmp_path, monkeypatch, stub_base=base) as f:
        f["settings"].chat_execution_bindings_file = ""
        response = ask(f["client"])
        assert response.status_code == 202
        assert response.json()["status"] == "completed"
        assert stub.calls == []


def test_pinned_workflow_survives_a_new_publish(tmp_path, monkeypatch, dify_stub):
    stub, base = dify_stub
    stub.latest_workflow_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    with dify_chat(tmp_path, monkeypatch, stub_base=base) as f:
        stub.gateway_base = f["gateway_base"]
        response = ask(f["client"])
        final = wait_terminal(f["client"], response.json()["qa_id"])
        assert final["status"] == "completed", final
        assert stub.paths == [f"/v1/workflows/{WORKFLOW_ID}/run"]
        assert stub.calls[0]["response_mode"] == "streaming"
        assert f["upstream"]["calls"] == 1


@pytest.mark.parametrize("field", ["workflow_id", "id"])
def test_missing_started_identity_is_not_filled_from_binding(tmp_path, monkeypatch, dify_stub, field):
    stub, base = dify_stub
    data = {"id": "run-1", "workflow_id": WORKFLOW_ID}
    del data[field]
    stub.identity_override = {"data": data}
    with dify_chat(tmp_path, monkeypatch, stub_base=base) as f:
        stub.gateway_base = f["gateway_base"]
        response = ask(f["client"])
        final = wait_terminal(f["client"], response.json()["qa_id"])
        assert final["status"] == "failed"
        assert final["error"]["code"] == "DIFY_WORKFLOW_INVALID"


@pytest.mark.parametrize("field", ["workflow_run_id", "task_id"])
def test_finished_identity_must_match_started(tmp_path, monkeypatch, dify_stub, field):
    stub, base = dify_stub
    stub.finish_override = {field: "another-execution"}
    with dify_chat(tmp_path, monkeypatch, stub_base=base) as f:
        stub.gateway_base = f["gateway_base"]
        response = ask(f["client"])
        final = wait_terminal(f["client"], response.json()["qa_id"])
        assert final["status"] == "failed"
        assert final["error"]["code"] == "DIFY_WORKFLOW_INVALID"
        assert f["upstream"]["calls"] == 1


def test_stream_eof_after_started_is_failure_without_retry(tmp_path, monkeypatch, dify_stub):
    stub, base = dify_stub
    stub.drop_finish = True
    with dify_chat(tmp_path, monkeypatch, stub_base=base) as f:
        stub.gateway_base = f["gateway_base"]
        response = ask(f["client"])
        final = wait_terminal(f["client"], response.json()["qa_id"])
        assert final["status"] == "failed"
        assert final["error"]["code"] == "DIFY_WORKFLOW_INVALID"
        assert len(stub.calls) == 1
        row = DifyRunStore(f["db"]).by_qa(response.json()["qa_id"])
        assert row.task_id and row.workflow_run_id


@pytest.mark.parametrize("before_id,stop_status", [(False, 200), (True, 200), (False, 500)])
def test_cancel_stops_running_dify_before_result(tmp_path, monkeypatch, dify_stub, before_id, stop_status):
    stub, base = dify_stub
    reached, release = threading.Event(), threading.Event()
    stub.stop_status = stop_status

    def hold():
        reached.set()
        assert release.wait(10)

    if before_id:
        stub.before_started = hold
    else:
        stub.after_callback = hold
    with dify_chat(tmp_path, monkeypatch, stub_base=base) as f:
        stub.gateway_base = f["gateway_base"]
        try:
            response = ask(f["client"])
            qa_id = response.json()["qa_id"]
            assert reached.wait(10), "the requested execution stage was not reached"
            row = DifyRunStore(f["db"]).by_qa(qa_id)
            assert row is not None
            if not before_id:
                assert row.workflow_run_id and row.task_id
                assert f["upstream"]["calls"] == 1
            cancelled = f["client"].post(f"/v1/assistant/questions/{qa_id}/cancel", headers=HEADERS)
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"
            grants = _grants(f["db"])
            assert grants and all(g.state == "revoked" for g in grants)
            if before_id:
                release.set()
            deadline = time.monotonic() + 5
            while not stub.stops and time.monotonic() < deadline:
                time.sleep(0.01)
            assert stub.stops == ["task-run-1"]
            release.set()
            final = wait_terminal(f["client"], qa_id)
            assert final["status"] == "cancelled" and final.get("result") is None
            assert DifyRunStore(f["db"]).by_qa(qa_id).state == "cancelled"
            assert len(stub.calls) == 1
        finally:
            release.set()
