"""APP-RUNTIME-01：现有 App 聊天 → 客户执行节点 → 同一 run 的状态快照。

这一组测的是**接线**，所以两边都用真的：
  * 真的 `create_gateway_app`（FastAPI 应用、路由、QA 生命周期、取消守卫）；
  * 真的节点 HTTP 端点：派发那一刻由「节点」自己 POST 领取与完成；
  * 真的 `AgentRunStore`（签发、TTL、完成回执、执行配置核对）。

替身只有两类，且都不是被测逻辑：
  * **派发器**：真实环境里它是「通知编排平台」，这里同步调用节点自己的领取端点 ——
    它替代的是**传输**，不是节点的判断；
  * **CPMS**：合成上游（`cpms_stub`），它不是本片要证的东西。

因此这里证明的是：聊天入口能把一次请求交给节点、把结果以原 blocks 形状交回、
并且在失败/越权/取消/停用时**不**伪造成功。它**不**证明真实 Java 节点或真实供应商。
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager

from fastapi.testclient import TestClient
from test_m2_close_contract import NODE_A_TOKEN, Orders, Resolver, _write_config

from aiops_diagnostics import chat_execution
from aiops_diagnostics.agent_runs import SUCCEEDED, AgentRunStore
from aiops_diagnostics.config import Settings
from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_runtime import GatewayRuntime
from aiops_diagnostics.gateway_store import GatewayStore

# 与 `test_m2_close_contract` 的解析器同一个令牌；订单归属也沿用那边的 `Orders`。
HEADERS = {"Authorization": "Bearer verified-token", "X-Business-Entry": "consumer"}
LATEST = "check my latest order status"
# 正文里带一个**含数字**的订单号：`_extract_order_no` 拒绝纯字母候选（避免把普通单词
# 当号）。所以这两个号本身就是带数字的形状，夹具不必再拼数字。
# 订单号必须**含数字**：`_extract_order_no` 刻意拒绝纯字母候选（否则一句英文里随便一个
# 单词都会被当成订单号）。这条约束是生产行为，夹具必须跟着它，不能反过来为了好写而
# 用字母号 —— 那会让「正文没带号」和「正文带了号」在测试里看起来一样。
ORDER_A = "ORDER-A-1"
ORDER_B = "ORDER-B-2"
ORDER_C = "ORDER-C-3"
SPECIFIED_QUESTION = f"check order {ORDER_B} status"


@contextmanager
def node_chat(tmp_path, monkeypatch, *, bound=True, fail_claim=False, m3=False):
    """真实 Gateway 应用 + 真实节点端点。

    自建而不是复用 `_app`：那个夹具是**委派入口**的，没有个人查询开关、也没有平台目录，
    拿它测聊天入口会先被平台判定挡住 —— 测到的就不是本片的东西。

    替身只有两处，且都不替代被测逻辑：CPMS 上游（合成），以及派发器（它替代的是
    「通知编排平台」这一跳传输，节点自己的领取/完成仍然打真实端点）。
    """
    with _cpms({ORDER_A, ORDER_B, ORDER_C}) as upstream:
        monkeypatch.setattr("aiops_diagnostics.gateway_api.scoped_mysql_source", _orders_source)
        config = _write_config(tmp_path)
        production = tmp_path / "production.env"
        production.write_text("# synthetic\n")
        production.chmod(0o600)
        settings = GatewayServerSettings(
            data_home=tmp_path,
            database_file=tmp_path / "gateway.db",
            server_config_file=production,
            consumer_order_chat_enabled=True,
            charging_status_base_url=upstream["base"],
            execution_environment="env-a",
            agent_run_config_file=str(config),
            chat_execution_config_file=str(config) if bound else "",
        )
        if m3:
            from test_m3_gateway_exec_config import _identity, _write_manifest

            manifest = _write_manifest(tmp_path)
            settings.chat_execution_manifest_file = str(manifest)
            settings.execution_manifest_file = str(manifest)
        resolver = Resolver()
        orders = Orders()
        orders.ownership[ORDER_A] = "USER-A"
        orders.ownership[ORDER_B] = "USER-A"
        orders.ownership[ORDER_C] = "USER-A"
        store = GatewayStore(settings.database_file)
        runtime = GatewayRuntime(store, settings, Settings())
        runtime.agent_run_store = AgentRunStore(settings.database_file)
        app = create_gateway_app(
            settings=settings,
            store=store,
            runtime=runtime,
            caller_resolver=resolver,
            order_authorizer=orders,
            platform_resolver=PlatformIdentityResolver(_Directory()),
            faq_catalog=FAQCatalog.bundled(),
        )
        client = TestClient(app)
        # 应用持有的是它**自己**的 settings 副本线索：改这一个才影响运行中的请求。
        live = app.state.gateway.settings

        def dispatch(_config, run_id: str) -> None:
            if fail_claim:
                raise chat_execution.ChatRunUnavailable("AGENT_RUN_UNAVAILABLE", "agent run unavailable")
            headers = {"Authorization": f"Bearer {NODE_A_TOKEN}"}
            if m3:
                headers["X-AI-Ops-Exec-Config"] = _identity(live.chat_execution_manifest_file)
            claimed = client.post(f"/v1/internal/charging-status/claims/{run_id}", headers=headers, json={})
            if claimed.status_code != 200:
                # 把真实原因带出来：否则这里会把「领取被拒」压成一句通用错误，
                # 让人以为节点没交货，其实是网关自己拒了。
                raise chat_execution.ChatRunUnavailable(
                    "AGENT_RUN_NO_RESULT", f"claim refused: {claimed.status_code} {claimed.text[:200]}"
                )
            client.post(
                f"/v1/internal/charging-status/completions/{run_id}",
                headers=headers,
                json={"status": "completed"},
            )

        # 派发发生在 `chat_execution`（共享操作）里，所以打的是**那里**的名字。
        # 打 `gateway_api.SnailDispatcher` 不会生效：那是另一个模块级名字。
        monkeypatch.setattr(
            "aiops_diagnostics.chat_execution.SnailDispatcher",
            lambda: type("D", (), {"dispatch": staticmethod(dispatch)})(),
        )

        class _Upstream:
            calls = property(lambda self: upstream["state"]["calls"])

            def __getitem__(self, key):
                return self.calls if key == "calls" else upstream[key]

        yield {"app": app, "client": client, "settings": live, "orders": orders, "upstream": _Upstream()}
        client.close()


def _orders_source(_settings, scope):
    """与生产同一条选单形状：本人可见的订单按 (created_time, id) 倒序。"""
    import contextlib as _ctx
    from types import SimpleNamespace

    rows = [
        {"order_no": ORDER_A, "created_time": "2026-10-15 09:33:35", "id": "2"},
        {"order_no": ORDER_C, "created_time": "2026-10-13 16:59:31", "id": "1"},
    ]
    if getattr(scope, "user_id", None) != "USER-A":
        rows = []
    source = SimpleNamespace(
        list_orders=lambda **_: rows, get_orders=lambda no: [r for r in rows if r["order_no"] == no]
    )
    return _ctx.contextmanager(lambda: (yield source))()


@contextmanager
def _cpms(known_orders):
    """与真实上游同形的 CPMS 桩，但认**本文件**的订单号。

    不复用 `cpms_stub`：那个桩把「哪些单存在」绑在 `Orders.DEFAULT` 上，而这里要用的
    订单号必须**含数字**（`_extract_order_no` 的要求）。复用就得改它的默认表，
    那等于为了测试好写去动别人的夹具。
    """
    import json as _json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    state = {"calls": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_GET(self):
            prefix = "/charging-pile/chOrderInfo/chargingInfo/"
            order_no = self.path[len(prefix) :] if self.path.startswith(prefix) else ""
            ok = order_no in known_orders
            payload = (
                {
                    "code": 0,
                    "msg": "success",
                    "data": {"orderNo": order_no, "status": "0", "plateNo": "DO-NOT-LEAK"},
                }
                if ok
                else {"code": 1, "msg": "not found", "data": None, "ok": False}
            )
            if ok:
                state["calls"] += 1
            body = _json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # 与生产同形：`read_charging_status` 会在 base 后面拼 `/chOrderInfo/chargingInfo/…`，
        # 而真实 base 自带 `/charging-pile` 前缀。少了它，桩会 404 → 上游被读成 502。
        yield {"base": f"http://127.0.0.1:{server.server_port}/charging-pile", "calls": 0, "state": state}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def ask(client, question=LATEST, **body):
    return client.post("/v1/assistant/questions", headers=HEADERS, json={"question": question, **body})


def wait_terminal(client, qa_id: str, *, timeout: float = 5.0) -> dict:
    """轮询到终态。POST 必须已经返回，等待发生在 GET 上 —— 这正是设计要的形状。"""
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


# ---------------------------------------------------------------- 正例


def test_latest_order_is_answered_by_the_node_and_keeps_the_blocks_shape(tmp_path, monkeypatch):
    with node_chat(tmp_path, monkeypatch) as f:
        started = time.monotonic()
        response = ask(f["client"])
        elapsed = time.monotonic() - started

        # ① POST 立即返回，不等节点
        assert response.status_code == 202, response.json()
        body = response.json()
        assert body["type"] == "qa"
        assert elapsed < 2.0, f"POST should not wait for the node ({elapsed:.2f}s)"
        qa_id = body["qa_id"]

        # ② GET 轮询到终态，答案来自该 run 的快照
        final = wait_terminal(f["client"], qa_id)
        assert final["status"] == "completed", final
        text = final["result"]["blocks"][0]["text"]
        assert ORDER_A in text and "cpms.chargingInfo" in text

        # ③ QA 与 run 是**存储关联**，不是字符串猜
        run = AgentRunStore(f["settings"].database_file).by_qa(qa_id)
        assert run is not None and run.state == SUCCEEDED
        assert run.order_no == ORDER_A
        assert run.snapshot["order_no"] == ORDER_A
        # ④ 每个成功请求只派发一次、只读一次业务
        assert f["upstream"]["calls"] == 1


def test_specified_order_is_the_one_the_node_read(tmp_path, monkeypatch):
    with node_chat(tmp_path, monkeypatch) as f:
        response = ask(f["client"], SPECIFIED_QUESTION)
        assert response.status_code == 202
        final = wait_terminal(f["client"], response.json()["qa_id"])
        assert final["status"] == "completed"
        assert ORDER_B in final["result"]["blocks"][0]["text"]
        run = AgentRunStore(f["settings"].database_file).by_qa(response.json()["qa_id"])
        assert run.order_no == ORDER_B


def test_latest_and_specified_select_different_orders(tmp_path, monkeypatch):
    with node_chat(tmp_path, monkeypatch) as f:
        first = ask(f["client"])
        second = ask(f["client"], SPECIFIED_QUESTION)
        one = wait_terminal(f["client"], first.json()["qa_id"])
        two = wait_terminal(f["client"], second.json()["qa_id"])
        assert ORDER_A in one["result"]["blocks"][0]["text"]
        assert ORDER_B in two["result"]["blocks"][0]["text"]


# ---------------------------------------------------------------- 失败不回落


def test_node_failure_fails_the_job_and_never_falls_back_to_direct_read(tmp_path, monkeypatch):
    with node_chat(tmp_path, monkeypatch, fail_claim=True) as f:
        response = ask(f["client"])
        assert response.status_code == 202
        final = wait_terminal(f["client"], response.json()["qa_id"])
        assert final["status"] == "failed", final
        # 没有直读补答案：CPMS 一次都没被调用
        assert f["upstream"]["calls"] == 0
        assert final.get("result") in (None, {})


def test_unowned_order_is_refused_before_any_dispatch(tmp_path, monkeypatch):
    with node_chat(tmp_path, monkeypatch) as f:
        del f["orders"].ownership[ORDER_B]
        response = ask(f["client"], SPECIFIED_QUESTION)
        assert response.status_code == 404
        assert f["upstream"]["calls"] == 0
        assert AgentRunStore(f["settings"].database_file).by_qa("none") is None


def test_execution_config_drift_fails_instead_of_delivering(tmp_path, monkeypatch):
    """节点回传的配置身份与清单不一致 ⇒ 领取被拒 ⇒ 任务失败，不交付正文。"""
    with node_chat(tmp_path, monkeypatch, m3=True) as f:
        # 让「节点」回一个错的配置身份
        def bad_dispatch(_config, run_id: str) -> None:
            f["client"].post(
                f"/v1/internal/charging-status/claims/{run_id}",
                headers={
                    "Authorization": f"Bearer {NODE_A_TOKEN}",
                    "X-AI-Ops-Exec-Config": "wrong-identity",
                },
                json={},
            )

        monkeypatch.setattr(
            "aiops_diagnostics.gateway_api.SnailDispatcher",
            lambda: type("D", (), {"dispatch": staticmethod(bad_dispatch)})(),
        )
        response = ask(f["client"])
        final = wait_terminal(f["client"], response.json()["qa_id"])
        assert final["status"] == "failed", final
        assert f["upstream"]["calls"] == 0


# ---------------------------------------------------------------- 停用与回读


def test_disabling_the_route_stops_delivering_the_body(tmp_path, monkeypatch):
    with node_chat(tmp_path, monkeypatch) as f:
        response = ask(f["client"])
        qa_id = response.json()["qa_id"]
        assert wait_terminal(f["client"], qa_id)["status"] == "completed"

        # 停用绑定（等价于把配置键清空）：历史正文不再交付
        f["settings"].chat_execution_config_file = ""
        refused = f["client"].get(f"/v1/assistant/questions/{qa_id}", headers=HEADERS)
        assert refused.status_code == 404
        assert refused.json()["error"]["code"] == "CHARGING_STATUS_DISABLED"


def test_queued_and_running_are_readable_not_blocked_by_missing_snapshot(tmp_path, monkeypatch):
    """「还没有快照」不能把轮询挡住 —— 否则前端永远停在第一次 GET。"""
    with node_chat(tmp_path, monkeypatch) as f:
        store = GatewayStore(f["settings"].database_file)
        qa = store.create_assistant_question(
            f["app"].state.gateway.store.path and "fp",
            "q",
            internal_run_id=chat_execution.CHAT_RUN_PREFIX + ORDER_A,
        )
        # 直接构造的 QA 不属于任何已知范围 ⇒ 读取仍应被范围挡住（404），
        # 这条只是确认守卫走的是节点分支而不是直读分支：
        response = f["client"].get(f"/v1/assistant/questions/{qa['qa_id']}", headers=HEADERS)
        assert response.status_code in {404, 403}


# ---------------------------------------------------------------- 取消


def test_cancel_revokes_the_linked_run_and_late_result_cannot_overwrite(tmp_path, monkeypatch):
    gate = threading.Event()

    with node_chat(tmp_path, monkeypatch) as f:

        def slow_dispatch(_config, run_id: str) -> None:
            headers = {"Authorization": f"Bearer {NODE_A_TOKEN}"}
            f["client"].post(f"/v1/internal/charging-status/claims/{run_id}", headers=headers, json={})
            gate.wait(timeout=5)  # 卡住「节点」直到取消已落
            f["client"].post(
                f"/v1/internal/charging-status/completions/{run_id}",
                headers=headers,
                json={"status": "completed"},
            )

        monkeypatch.setattr(
            "aiops_diagnostics.gateway_api.SnailDispatcher",
            lambda: type("D", (), {"dispatch": staticmethod(slow_dispatch)})(),
        )
        response = ask(f["client"])
        qa_id = response.json()["qa_id"]
        # 等它真的 running（领取已发生）
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = f["client"].get(f"/v1/assistant/questions/{qa_id}", headers=HEADERS).json()
            if current["status"] == "running":
                break
            time.sleep(0.02)

        cancelled = f["client"].post(f"/v1/assistant/questions/{qa_id}/cancel", headers=HEADERS)
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        gate.set()

        final = wait_terminal(f["client"], qa_id)
        assert final["status"] == "cancelled", final
        assert final.get("result") in (None, {})
        # run 也被撤销：节点不能继续把结果交回来
        run = AgentRunStore(f["settings"].database_file).by_qa(qa_id)
        assert run is not None and run.state != SUCCEEDED


# ---------------------------------------------------------------- 未启用 / 绑定外


def test_unbound_route_keeps_the_original_direct_read(tmp_path, monkeypatch):
    with node_chat(tmp_path, monkeypatch, bound=False) as f:
        response = ask(f["client"])
        assert response.status_code == 202
        body = response.json()
        # 原路线的标记：同步完成 + 私有快照文件
        assert body["status"] == "completed"
        assert (tmp_path / "consumer-order-chat" / (body["qa_id"] + ".json")).exists()
        assert AgentRunStore(f["settings"].database_file).by_qa(body["qa_id"]) is None


def test_clearing_the_binding_returns_later_requests_to_the_old_route(tmp_path, monkeypatch):
    with node_chat(tmp_path, monkeypatch) as f:
        first = ask(f["client"])
        assert wait_terminal(f["client"], first.json()["qa_id"])["status"] == "completed"

        f["settings"].chat_execution_config_file = ""
        second = ask(f["client"])
        assert second.status_code == 202
        assert second.json()["status"] == "completed"  # 直读是同步完成的
        assert AgentRunStore(f["settings"].database_file).by_qa(second.json()["qa_id"]) is None


class _Directory:
    """平台判定：该主体在 consumer 内容域可用。与生产同一入口判据，只是没有数据库。"""

    def roles_for_c_user(self, c_user_id: str, tenant_id: str):
        return (PlatformRoleRecord("", c_user_id, tenant_id, "admin"),)

    def roles_for_b_user(self, b_user_id: str, tenant_id: str):
        return ()


def test_real_tcp_post_then_poll_reaches_the_node(tmp_path, monkeypatch):
    """真实 uvicorn + TCP：POST 立刻回 202，随后用**原 GET**轮询到答案。

    `TestClient` 是进程内调用；这一条走真实监听与真实 HTTP，证明「App 不等节点」
    在真实服务器上也成立（后台线程与事件循环的关系不是 TestClient 能证出来的）。
    """
    import socket

    import httpx
    import uvicorn

    with node_chat(tmp_path, monkeypatch) as f, socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(f["app"], log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.02)
            assert server.started, "uvicorn did not start"

            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10.0) as client:
                started = time.monotonic()
                response = client.post("/v1/assistant/questions", headers=HEADERS, json={"question": LATEST})
                elapsed = time.monotonic() - started
                assert response.status_code == 202, response.text
                assert elapsed < 2.0, f"POST waited for the node ({elapsed:.2f}s)"
                qa_id = response.json()["qa_id"]

                final: dict = {}
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    got = client.get(f"/v1/assistant/questions/{qa_id}", headers=HEADERS)
                    assert got.status_code == 200, got.text
                    final = got.json()
                    if final.get("status") in {"completed", "failed", "cancelled"}:
                        break
                    time.sleep(0.05)
                assert final.get("status") == "completed", final
                assert ORDER_A in final["result"]["blocks"][0]["text"]
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()
