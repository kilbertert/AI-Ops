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
from test_app_runtime_node_entry import (
    ORDER_A,
    ORDER_B,
    _bindings_file,
    _cpms,
    _Directory,
    _orders_source,
    _Resolver,
)
from test_m2_close_contract import Orders, _write_config
from test_m3_gateway_exec_config import _write_manifest

from aiops_diagnostics import chat_data_grant, chat_execution
from aiops_diagnostics.agent_runs import AgentRunStore, scope_to_json
from aiops_diagnostics.config import Settings
from aiops_diagnostics.dify_run_store import DifyRunStore
from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_runtime import GatewayRuntime
from aiops_diagnostics.gateway_store import GatewayStore

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
        self.fail = False
        # 真 Dify 的 HTTP 步骤把响应体当文本，端节点拿回的是 JSON **字符串**。
        # 这几个开关让桩能复现那一种形状（以及三种应当被拒的形状）。
        self.snapshot_as_text = False
        self.snapshot_extra: dict = {}
        self.snapshot_raw_text = ""
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
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = json.loads(self.rfile.read(length) or b"{}")
                if self.path.endswith("/stop"):
                    task_id = self.path.split("/")[-2]
                    stub.stops.append(task_id)
                    return self._reply({"result": "success"})
                stub.calls.append(raw)
                if stub.fail:
                    return self._reply({"message": "boom"}, status=500)
                inputs = raw.get("inputs") or {}
                token = inputs.get("data_grant") or ""
                stub.token_seen.append(token)
                if stub.skip_callback:
                    # "图没调取数端点"这一种：不回调网关，直接给出一个形状合法的快照。
                    return self._reply(
                        {
                            "task_id": "task-skipped",
                            "workflow_run_id": "run-skipped",
                            "data": {
                                "id": "run-skipped",
                                "workflow_id": WORKFLOW_ID,
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
                run_id = "run-" + str(len(stub.runs) + 1)
                if status != 200:
                    return self._reply(
                        {
                            "task_id": "task-" + run_id,
                            "workflow_run_id": run_id,
                            "data": {
                                "id": run_id,
                                "workflow_id": WORKFLOW_ID,
                                "status": "failed",
                                "outputs": {},
                            },
                        }
                    )
                stub.runs[run_id] = snapshot
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
                            "workflow_id": WORKFLOW_ID,
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
        again = f["client"].get(
            f"/v1/assistant/questions/{response.json()['qa_id']}", headers=HEADERS
        )
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
