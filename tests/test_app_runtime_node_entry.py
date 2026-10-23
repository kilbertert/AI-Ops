"""APP-RUNTIME-01：现有 App 聊天 → 客户执行节点 → 同一 run 的状态快照。

这一组测的是**接线**，所以两边都用真的：
  * 真的 `create_gateway_app`（FastAPI 应用、路由、QA 生命周期、取消守卫）；
  * 真的节点 HTTP 端点：派发那一刻由「节点」自己 POST 领取与完成；
  * 真的 `AgentRunStore`（签发、TTL、完成回执、执行配置核对）；
  * 真的**按租户登记**的绑定表（两个租户各一个节点，互不串）。

替身只有两类，且都不是被测逻辑：
  * **派发器**：真实环境里它是「通知编排平台」，这里同步调用节点自己的领取端点 ——
    它替代的是**传输**，不是节点的判断；
  * **CPMS**：合成上游（`cpms_stub`），它不是本片要证的东西。

因此这里证明的是：聊天入口能按租户把一次请求交给**那一个**节点、把结果以原 blocks
形状交回、并且在失败/越权/取消/停用/清单缺失时**不**伪造成功。
真实 Java 节点整链在 `tools/m2_close/m3node/verify_chat_node_entry.py`，不在本文件。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_m2_close_contract import NODE_A_TOKEN, Orders, _write_config
from test_m3_gateway_exec_config import _identity, _write_manifest

from aiops_diagnostics import chat_execution
from aiops_diagnostics.agent_runs import SUCCEEDED, AgentRunStore
from aiops_diagnostics.config import Settings
from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_runtime import GatewayRuntime
from aiops_diagnostics.gateway_store import GatewayStore

NODE_A2_TOKEN = "node-token-beta-0123456789"

TOKENS = {
    "token-a": ("USER-A", "TENANT-A"),
    "token-b": ("USER-B", "TENANT-B"),
    "token-c": ("USER-C", "TENANT-C"),  # 未登记的租户
}
LATEST = "check my latest order status"
ORDER_A = "ORDER-A-1"
ORDER_B = "ORDER-B-2"
ORDER_C = "ORDER-C-3"
SPECIFIED_QUESTION = f"check order {ORDER_B} status"


def headers(token: str = "token-a") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Business-Entry": "consumer"}


HEADERS = headers()


class _Resolver:
    """三个固定令牌映射到三个主体；每个主体落在**自己**的租户里。"""

    def resolve(self, token: str, *, required_scope: str, **kwargs):
        from aiops_diagnostics.caller_auth import CallerAuthError
        from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

        if token not in TOKENS:
            raise CallerAuthError("invalid", code="caller_auth.invalid")
        c_user_id, tenant_id = TOKENS[token]
        subject = SubjectRecord(b_user_id="", c_user_id=c_user_id, tenant_id=tenant_id)
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id=tenant_id,
            data_scope=DataScope(type="self"),
            roles=frozenset(),
            permissions=frozenset({"aiops:orders:read"}),
            resolved_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        )


class _Directory:
    """平台判定：该主体在 consumer 内容域可用。与生产同一入口判据，只是没有数据库。"""

    def roles_for_c_user(self, c_user_id: str, tenant_id: str):
        return (PlatformRoleRecord("", c_user_id, tenant_id, "admin"),)

    def roles_for_b_user(self, b_user_id: str, tenant_id: str):
        return ()


def _bindings_file(
    path: Path,
    entries: list[dict[str, str]],
) -> Path:
    path.write_text(json.dumps({"schema": chat_execution.BINDINGS_SCHEMA, "bindings": entries}))
    path.chmod(0o600)
    return path


@contextmanager
def node_chat(
    tmp_path,
    monkeypatch,
    *,
    tenants: tuple[str, ...] = ("TENANT-A",),
    fail_claim: bool = False,
    matter: bool = True,
    gate_before_dispatch: threading.Event | None = None,
    dispatch_events: list[str] | None = None,
    after_completion: Callable[[dict], None] | None = None,
):
    """真实 Gateway 应用 + 真实节点端点 + 按租户登记的绑定表。

    `tenants` 决定**哪几个租户**登记了绑定：每个租户一个独立 config/manifest，指向
    各自的节点（`node-a1` / `node-b1`）。未登记的租户因此会走原直读分支 —— 这条是
    从「全局没配置」里区分出来的最强判据（复核必修 1）。
    """
    import hashlib

    with _cpms({ORDER_A, ORDER_B, ORDER_C}) as upstream:
        monkeypatch.setattr("aiops_diagnostics.gateway_api.scoped_mysql_source", _orders_source)
        production = tmp_path / "production.env"
        production.write_text("# synthetic\n")
        production.chmod(0o600)

        shared_config = _write_config(tmp_path)
        manifest = _write_manifest(tmp_path)
        entries: list[dict[str, str]] = []
        node_tokens = {"node-a1": NODE_A_TOKEN, "node-b1": NODE_A2_TOKEN}
        for tenant, node_id in (("TENANT-A", "node-a1"), ("TENANT-B", "node-b1")):
            if tenant not in tenants:
                continue
            # 每个租户一份**自己的**执行配置：节点不同、app 不同 —— 否则「派到哪个节点」
            # 这件事根本观察不到。
            raw = json.loads(Path(shared_config).read_text())
            raw["expected_node_id"] = node_id
            raw["snail_app_id"] = f"aiops-{node_id}"
            raw["nodes"] = [
                {
                    "node_id": node_id,
                    "environment": "env-a",
                    "token_sha256": hashlib.sha256(node_tokens[node_id].encode()).hexdigest(),
                }
            ]
            config = tmp_path / f"agent-runs-{tenant}.json"
            config.write_text(json.dumps(raw))
            config.chmod(0o600)
            entries.append(
                {
                    "environment": "env-a",
                    "tenant_id": tenant,
                    "entry": "consumer",
                    "scenario": chat_execution.SCENARIO_OWN_ORDER_STATUS,
                    "config_file": str(config),
                    "manifest_file": str(manifest) if matter else "",
                }
            )
        bindings = _bindings_file(tmp_path / "chat-bindings.json", entries)

        settings = GatewayServerSettings(
            data_home=tmp_path,
            database_file=tmp_path / "gateway.db",
            server_config_file=production,
            consumer_order_chat_enabled=True,
            charging_status_base_url=upstream["base"],
            execution_environment="env-a",
            # 委派入口的绑定**不配**：聊天路线要用自己的那一套，只留聊天绑定也能跑完。
            agent_run_config_file="",
            chat_execution_bindings_file=str(bindings) if entries else "",
        )
        store = GatewayStore(settings.database_file)
        runtime = GatewayRuntime(store, settings, Settings())
        runtime.agent_run_store = AgentRunStore(settings.database_file)
        orders = Orders()
        orders.ownership = {ORDER_A: "USER-A", ORDER_C: "USER-A", ORDER_B: "USER-B"}
        app = create_gateway_app(
            settings=settings,
            store=store,
            runtime=runtime,
            caller_resolver=_Resolver(),
            order_authorizer=orders,
            platform_resolver=PlatformIdentityResolver(_Directory()),
            faq_catalog=FAQCatalog.bundled(),
        )
        client = TestClient(app)
        live = app.state.gateway.settings
        _runtime_state = {"client": client, "settings": live, "orders": orders}
        manifest_identity = _identity(manifest) if matter else ""

        def dispatch(_config, run_id: str) -> None:
            if dispatch_events is not None:
                dispatch_events.append(run_id)
            if gate_before_dispatch is not None:
                gate_before_dispatch.wait(timeout=5)
            if fail_claim:
                raise chat_execution.ChatRunUnavailable("AGENT_RUN_UNAVAILABLE", "agent run unavailable")
            run = AgentRunStore(live.database_file).peek(run_id)
            assert run is not None
            token = node_tokens[run.expected_node_id]
            headers_ = {"Authorization": f"Bearer {token}"}
            if matter:
                headers_["X-AI-Ops-Exec-Config"] = manifest_identity
            claimed = client.post(f"/v1/internal/charging-status/claims/{run_id}", headers=headers_, json={})
            if claimed.status_code != 200:
                # 把真实原因带出来：否则这里会把「领取被拒」压成一句通用错误。
                raise chat_execution.ChatRunUnavailable(
                    "AGENT_RUN_NO_RESULT", f"claim refused: {claimed.status_code} {claimed.text[:200]}"
                )
            client.post(
                f"/v1/internal/charging-status/completions/{run_id}",
                headers=headers_,
                json={"status": "completed"},
            )
            # 节点已经交回结果、worker 还没写 QA 的那个瞬间。用来制造「这里之后归属变了」
            # 与「这里之后被撤销/到期」两类并发 —— 不用 sleep 赌时序。
            if after_completion is not None:
                after_completion(_runtime_state)

        # 派发发生在 `chat_execution`（共享操作）里，所以打的是**那里**的名字。
        monkeypatch.setattr(
            "aiops_diagnostics.chat_execution.SnailDispatcher",
            lambda: type("D", (), {"dispatch": staticmethod(dispatch)})(),
        )

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
            "manifest": manifest,
            "node_tokens": node_tokens,
        }
        client.close()


def _orders_source(_settings, scope):
    """与生产同一条选单形状：本人可见的订单按 (created_time, id) 倒序。"""
    import contextlib as _ctx
    from types import SimpleNamespace

    owners = {ORDER_A: "USER-A", ORDER_C: "USER-A", ORDER_B: "USER-B"}
    rows = [
        {"order_no": ORDER_A, "created_time": "2026-10-15 09:33:35", "id": "3"},
        {"order_no": ORDER_B, "created_time": "2026-10-14 10:00:00", "id": "2"},
        {"order_no": ORDER_C, "created_time": "2026-10-13 16:59:31", "id": "1"},
    ]
    mine = [row for row in rows if owners[row["order_no"]] == getattr(scope, "user_id", None)]
    source = SimpleNamespace(
        list_orders=lambda **_: mine, get_orders=lambda no: [row for row in mine if row["order_no"] == no]
    )
    return _ctx.contextmanager(lambda: (yield source))()


@contextmanager
def _cpms(known_orders):
    """与真实上游同形的 CPMS 桩，但认**本文件**的订单号。"""
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
        yield {"base": f"http://127.0.0.1:{server.server_port}/charging-pile", "calls": 0, "state": state}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def ask(client, question=LATEST, token: str = "token-a", **body):
    return client.post("/v1/assistant/questions", headers=headers(token), json={"question": question, **body})


def wait_terminal(client, qa_id: str, *, token: str = "token-a", timeout: float = 8.0) -> dict:
    """轮询到终态。POST 必须已经返回，等待发生在 GET 上 —— 这正是设计要的形状。"""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/v1/assistant/questions/{qa_id}", headers=headers(token))
        if response.status_code == 200:
            last = response.json()
            if last.get("status") in {"completed", "failed", "cancelled", "expired"}:
                return last
        time.sleep(0.02)
    return last


def wait_status(client, qa_id: str, wanted: str, *, token: str = "token-a", timeout: float = 8.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/v1/assistant/questions/{qa_id}", headers=headers(token))
        assert response.status_code == 200, response.text
        last = response.json()
        if last.get("status") == wanted:
            return last
        time.sleep(0.01)
    raise AssertionError(f"never reached {wanted}: {last}")


# ---------------------------------------------------------------- 正例


def test_latest_order_is_answered_by_the_node_and_keeps_the_blocks_shape(tmp_path, monkeypatch):
    with node_chat(tmp_path, monkeypatch) as f:
        started = time.monotonic()
        response = ask(f["client"])
        elapsed = time.monotonic() - started

        # ① POST 立即返回，不等节点
        assert response.status_code == 202, response.json()
        assert response.json()["type"] == "qa"
        assert elapsed < 2.0, f"POST should not wait for the node ({elapsed:.2f}s)"
        qa_id = response.json()["qa_id"]

        # ② GET 轮询到终态，答案来自该 run 的快照
        final = wait_terminal(f["client"], qa_id)
        assert final["status"] == "completed", final
        text = final["result"]["blocks"][0]["text"]
        assert ORDER_A in text and "cpms.chargingInfo" in text

        # ③ QA 与 run 是**存储关联**，且绑定被冻结在记录上
        run = AgentRunStore(f["settings"].database_file).by_qa(qa_id)
        assert run is not None and run.state == SUCCEEDED
        assert run.order_no == ORDER_A
        assert run.snapshot["order_no"] == ORDER_A
        assert run.chat_binding == "consumer_own_order_status:env-a:TENANT-A"
        assert run.actual_node_id == "node-a1"
        # ④ 每个成功请求只派发一次、只读一次业务
        assert f["upstream"]["calls"] == 1


def test_specified_order_is_the_one_the_node_read(tmp_path, monkeypatch):
    with node_chat(tmp_path, monkeypatch, tenants=("TENANT-A", "TENANT-B")) as f:
        response = ask(f["client"], SPECIFIED_QUESTION, token="token-b")
        assert response.status_code == 202
        final = wait_terminal(f["client"], response.json()["qa_id"], token="token-b")
        assert final["status"] == "completed", final
        assert ORDER_B in final["result"]["blocks"][0]["text"]
        run = AgentRunStore(f["settings"].database_file).by_qa(response.json()["qa_id"])
        assert run.order_no == ORDER_B


def test_two_tenants_are_dispatched_to_their_own_nodes(tmp_path, monkeypatch):
    """复核必修 1 的正例：两个租户各登记各的节点，**互不串**（派发目标逐条断言）。"""
    with node_chat(tmp_path, monkeypatch, tenants=("TENANT-A", "TENANT-B")) as f:
        one = ask(f["client"], token="token-a")
        two = ask(f["client"], SPECIFIED_QUESTION, token="token-b")
        assert wait_terminal(f["client"], one.json()["qa_id"])["status"] == "completed"
        assert wait_terminal(f["client"], two.json()["qa_id"], token="token-b")["status"] == "completed"

        store = AgentRunStore(f["settings"].database_file)
        run_a = store.by_qa(one.json()["qa_id"])
        run_b = store.by_qa(two.json()["qa_id"])
        assert (run_a.expected_node_id, run_a.actual_node_id, run_a.app_id) == (
            "node-a1",
            "node-a1",
            "aiops-node-a1",
        )
        assert (run_b.expected_node_id, run_b.actual_node_id, run_b.app_id) == (
            "node-b1",
            "node-b1",
            "aiops-node-b1",
        )
        assert run_a.chat_binding.endswith("TENANT-A") and run_b.chat_binding.endswith("TENANT-B")
        # 两次成功请求各自读一次业务
        assert f["upstream"]["calls"] == 2


def test_unregistered_tenant_keeps_the_direct_read_and_dispatches_nothing(tmp_path, monkeypatch):
    """复核必修 1 的核心反例：**未登记的租户**不得进入派发。

    「全局没配置」证明不了这条 —— 有绑定文件、但该租户不在表里，才是被要求隔离的形态。
    """
    events: list[str] = []
    # token-c 属于 **TENANT-C**：绑定表里有 TENANT-A 的登记，但没有它这一条。
    # 「全局没配置」证明不了隔离 —— 有绑定文件、该租户不在表里，才是被要求的形态。
    with node_chat(tmp_path, monkeypatch, tenants=("TENANT-A",), dispatch_events=events) as f:
        f["orders"].ownership[ORDER_C] = "USER-C"
        response = ask(f["client"], f"check order {ORDER_C} status", token="token-c")
        assert response.status_code == 202, response.text
        body = response.json()
        # 原路线：同步完成 + 私有快照文件 + 没有 run 关联
        assert body["status"] == "completed"
        assert (tmp_path / "consumer-order-chat" / (body["qa_id"] + ".json")).exists()
        assert AgentRunStore(f["settings"].database_file).by_qa(body["qa_id"]) is None
        assert events == []


def test_two_consumers_cannot_read_each_others_jobs(tmp_path, monkeypatch):
    with node_chat(tmp_path, monkeypatch, tenants=("TENANT-A", "TENANT-B")) as f:
        qa_id = ask(f["client"], token="token-a").json()["qa_id"]
        assert wait_terminal(f["client"], qa_id)["status"] == "completed"
        other = f["client"].get(f"/v1/assistant/questions/{qa_id}", headers=headers("token-b"))
        assert other.status_code == 404, other.text
        assert "blocks" not in other.text


# ---------------------------------------------------------------- 失败不回落


def test_node_failure_fails_the_job_and_never_falls_back_to_direct_read(tmp_path, monkeypatch):
    with node_chat(tmp_path, monkeypatch, fail_claim=True) as f:
        response = ask(f["client"])
        assert response.status_code == 202
        final = wait_terminal(f["client"], response.json()["qa_id"])
        assert final["status"] == "failed", final
        assert f["upstream"]["calls"] == 0
        assert final.get("result") in (None, {})


def test_unowned_order_is_refused_before_any_dispatch(tmp_path, monkeypatch):
    with node_chat(tmp_path, monkeypatch, tenants=("TENANT-A", "TENANT-B")) as f:
        # token-a 请求 USER-B 的订单：在派发之前就被拒
        response = ask(f["client"], SPECIFIED_QUESTION, token="token-a")
        assert response.status_code == 404
        assert f["upstream"]["calls"] == 0
        assert AgentRunStore(f["settings"].database_file).by_qa("none") is None


def test_execution_config_drift_fails_and_sends_the_bad_identity(tmp_path, monkeypatch):
    """领到的是**真的**被拒回执，而且 CPMS 一次都没读。

    上一版这条用例在夹具里把 `str` 传给 `_identity(Path)`，于是它每次都在领取之前就
    抛 AttributeError —— 测试"通过"了，坏摘要却从未发出（复核必修 5）。
    """
    seen: list[dict] = []

    with node_chat(tmp_path, monkeypatch) as f:

        def bad_dispatch(_config, run_id: str) -> None:
            claimed = f["client"].post(
                f"/v1/internal/charging-status/claims/{run_id}",
                headers={
                    "Authorization": f"Bearer {NODE_A_TOKEN}",
                    "X-AI-Ops-Exec-Config": "wrong-identity",
                },
                json={},
            )
            seen.append({"status": claimed.status_code, "body": claimed.json()})

        monkeypatch.setattr(
            "aiops_diagnostics.chat_execution.SnailDispatcher",
            lambda: type("D", (), {"dispatch": staticmethod(bad_dispatch)})(),
        )
        response = ask(f["client"])
        final = wait_terminal(f["client"], response.json()["qa_id"])
        assert final["status"] == "failed", final
        assert f["upstream"]["calls"] == 0
        assert seen == [
            {
                "status": 403,
                "body": {
                    "error": {
                        "code": "AGENT_RUN_EXEC_CONFIG_DRIFT",
                        "message": "agent run refused",
                        "retryable": False,
                    }
                },
            }
        ], seen
        run = AgentRunStore(f["settings"].database_file).by_qa(response.json()["qa_id"])
        assert run.state == "failed" and run.exec_config_state == "mismatched"


def test_claim_without_completion_can_never_succeed(tmp_path, monkeypatch):
    """复核必修 2：只领取、不发完成通知 ⇒ **不能**是成功。

    没有清单绑定时，`AgentRunStore.complete` 会按旧协议把 `running` 直接转 `succeeded`，
    于是"节点只读了业务、整轮模型循环没跑"会被记成成功，`completion_http_calls` 为 0。
    """
    completion_calls: list[int] = []

    with node_chat(tmp_path, monkeypatch) as f:

        def claim_only(_config, run_id: str) -> None:
            claimed = f["client"].post(
                f"/v1/internal/charging-status/claims/{run_id}",
                headers={
                    "Authorization": f"Bearer {NODE_A_TOKEN}",
                    "X-AI-Ops-Exec-Config": _identity(f["manifest"]),
                },
                json={},
            )
            completion_calls.append(claimed.status_code)

        monkeypatch.setattr(
            "aiops_diagnostics.chat_execution.SnailDispatcher",
            lambda: type("D", (), {"dispatch": staticmethod(claim_only)})(),
        )
        response = ask(f["client"])
        final = wait_terminal(f["client"], response.json()["qa_id"])
        assert final["status"] == "failed", final
        assert completion_calls == [200]
        run = AgentRunStore(f["settings"].database_file).by_qa(response.json()["qa_id"])
        # 业务确实被读过一次（快照在），但**整轮没有完成** ⇒ 记录绝不 succeeded。
        assert run.state == "failed", run.state
        assert run.snapshot is not None and run.completed_at is not None
        assert run.exec_config_state == "matched"
        assert f["upstream"]["calls"] == 1


def test_issue_refuses_a_run_without_a_manifest_even_if_a_binding_says_so(tmp_path, monkeypatch):
    """签发时的第二道防御：绑定对象被构造出来、但清单读成 None ⇒ 不签发。

    `AgentRunStore.complete` 对**没有期望身份**的记录会按旧协议直接转 succeeded。
    只要有一条路径能签发出这种 run，"只领取没跑完"就又能变成成功。
    """
    from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

    with node_chat(tmp_path, monkeypatch) as f:
        subject = SubjectRecord(b_user_id="", c_user_id="USER-A", tenant_id="TENANT-A")
        scope = ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id="TENANT-A",
            data_scope=DataScope(type="self"),
            roles=frozenset(),
            permissions=frozenset({"aiops:orders:read"}),
            resolved_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        )
        entry = json.loads(f["bindings"].read_text())["bindings"][0]
        binding = chat_execution.ChatBinding(
            environment="env-a",
            tenant_id="TENANT-A",
            entry="consumer",
            scenario=chat_execution.SCENARIO_OWN_ORDER_STATUS,
            config_file=entry["config_file"],
            manifest_file="",
        )
        store = AgentRunStore(f["settings"].database_file)
        with pytest.raises(chat_execution.ChatRunUnavailable) as raised:
            chat_execution.issue_for_store(
                store=store,
                settings=f["settings"],
                binding=binding,
                caller=scope,
                order_no=ORDER_A,
                gateway_version="test",
            )
        assert raised.value.code == "EXEC_CONFIG_UNAVAILABLE"
        assert store.by_qa("none") is None


def test_a_binding_that_points_at_no_manifest_is_refused_at_validation(tmp_path, monkeypatch):
    """清单键存在但是空 ⇒ 校验**本身**必须拒绝，而不是交给后面那句 `load("")` 返回 None。

    `execution_manifest.load("")` 返回 `None`（"未启用"），而"未启用清单"的 run 走旧协议。
    所以这条判定不能只写在"字段缺失"上 —— 那个空串是能走到校验里的。
    """
    with node_chat(tmp_path, monkeypatch) as f:
        entry = json.loads(f["bindings"].read_text())["bindings"][0]
        binding = chat_execution.ChatBinding(
            environment="env-a",
            tenant_id="TENANT-A",
            entry="consumer",
            scenario=chat_execution.SCENARIO_OWN_ORDER_STATUS,
            config_file=entry["config_file"],
            manifest_file="",
        )
        with pytest.raises(chat_execution.ChatBindingError):
            chat_execution._validate_binding_files(binding)


def test_a_binding_without_a_manifest_cannot_be_loaded(tmp_path, monkeypatch):
    """清单是**必须**的：没有它就退回旧协议（领取即成功）。加载期就拒绝。"""
    with node_chat(tmp_path, monkeypatch) as f:
        raw = json.loads(f["bindings"].read_text())
        # 把一条登记的清单键拿掉：`_parse_binding` 的第一道判定就必须拒绝它。
        del raw["bindings"][0]["manifest_file"]
        f["bindings"].write_text(json.dumps(raw))
        f["bindings"].chmod(0o600)
        with pytest.raises(chat_execution.ChatBindingError):
            chat_execution.load_bindings(f["bindings"])


# ---------------------------------------------------------------- 回读与停用


def test_disabling_the_route_hides_the_body_but_keeps_the_status_readable(tmp_path, monkeypatch):
    """复核必修 4：停用后**正文**不再交付，但本人仍读得到自己那条作业的终态。

    上一版把两者一起 404 掉，于是「取消」在界面上看起来失败了，而库里其实已经取消。
    """
    gate = threading.Event()
    reached: list[str] = []
    with node_chat(tmp_path, monkeypatch, gate_before_dispatch=gate) as f:
        qa_id = ask(f["client"]).json()["qa_id"]
        wait_status(f["client"], qa_id, "running")

        # 停用：把绑定文件清空（等价于撤掉这条登记）
        f["settings"].chat_execution_bindings_file = ""

        # 无正文的状态仍可读
        still = f["client"].get(f"/v1/assistant/questions/{qa_id}", headers=HEADERS)
        assert still.status_code == 200, still.text
        assert still.json()["status"] == "running"

        # 取消也必须可达，并且真的取消成功
        cancelled = f["client"].post(f"/v1/assistant/questions/{qa_id}/cancel", headers=HEADERS)
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"
        reached.append("cancelled")
        gate.set()

        final = wait_terminal(f["client"], qa_id)
        assert final["status"] == "cancelled", final
        assert final.get("result") in (None, {})
        assert reached == ["cancelled"]


def test_disabling_the_route_stops_delivering_the_body(tmp_path, monkeypatch):
    with node_chat(tmp_path, monkeypatch) as f:
        qa_id = ask(f["client"]).json()["qa_id"]
        assert wait_terminal(f["client"], qa_id)["status"] == "completed"

        f["settings"].chat_execution_bindings_file = ""
        refused = f["client"].get(f"/v1/assistant/questions/{qa_id}", headers=HEADERS)
        assert refused.status_code == 404, refused.text
        assert refused.json()["error"]["code"] == "CHARGING_STATUS_DISABLED"
        assert "blocks" not in refused.text


def test_answer_stops_being_delivered_once_the_run_expires(tmp_path, monkeypatch):
    with node_chat(tmp_path, monkeypatch) as f:
        qa_id = ask(f["client"]).json()["qa_id"]
        assert wait_terminal(f["client"], qa_id)["status"] == "completed"

        import sqlite3
        from datetime import UTC, datetime, timedelta

        past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        with sqlite3.connect(f["settings"].database_file) as conn:
            conn.execute("UPDATE agent_runs SET expires_at = ? WHERE qa_id = ?", (past, qa_id))

        refused = f["client"].get(f"/v1/assistant/questions/{qa_id}", headers=HEADERS)
        assert refused.status_code in {403, 404}, refused.text
        assert "blocks" not in refused.text


def test_answer_stops_when_the_node_is_deactivated(tmp_path, monkeypatch):
    with node_chat(tmp_path, monkeypatch) as f:
        qa_id = ask(f["client"]).json()["qa_id"]
        assert wait_terminal(f["client"], qa_id)["status"] == "completed"

        entry = json.loads(f["bindings"].read_text())["bindings"][0]
        config_path = Path(entry["config_file"])
        raw = json.loads(config_path.read_text())
        for node in raw["nodes"]:
            node["active"] = False
        config_path.write_text(json.dumps(raw))
        config_path.chmod(0o600)

        refused = f["client"].get(f"/v1/assistant/questions/{qa_id}", headers=HEADERS)
        assert refused.status_code in {403, 404}, refused.text
        assert "blocks" not in refused.text


def test_answer_stops_when_the_recorded_environment_no_longer_matches(tmp_path, monkeypatch):
    """执行环境换了 ⇒ 同一条记录不再交付（复核必修 3 的第一项）。"""
    with node_chat(tmp_path, monkeypatch) as f:
        qa_id = ask(f["client"]).json()["qa_id"]
        assert wait_terminal(f["client"], qa_id)["status"] == "completed"

        import sqlite3

        with sqlite3.connect(f["settings"].database_file) as conn:
            conn.execute("UPDATE agent_runs SET environment = 'env-other' WHERE qa_id = ?", (qa_id,))

        refused = f["client"].get(f"/v1/assistant/questions/{qa_id}", headers=HEADERS)
        assert refused.status_code in {403, 404}, refused.text
        assert "blocks" not in refused.text


def test_answer_stops_when_the_snapshot_came_from_another_node(tmp_path, monkeypatch):
    """记录里"实际领取的节点"不是登记的那个节点 ⇒ 不交付（受控存储损坏）。

    正常链路走不到这里：领取端点按凭据认节点，错节点当场 403。所以这一条测的是
    放行判据**自己也**独立检查同一件事 —— 不能只依赖领取端点，否则一个被改过的
    行（或一条从别处搬过来的快照）会被当成这条 run 的结果交出去。
    """
    with node_chat(tmp_path, monkeypatch) as f:
        qa_id = ask(f["client"]).json()["qa_id"]
        assert wait_terminal(f["client"], qa_id)["status"] == "completed"

        # 用**登记在案**的另一个活节点：只比"节点存在且有效"会放行它，
        # 因为它确实是这个环境里一台真实的节点 —— 但它不是这条 run 的那一台。
        entry = json.loads(f["bindings"].read_text())["bindings"][0]
        config_path = Path(entry["config_file"])
        raw = json.loads(config_path.read_text())
        raw["nodes"].append(
            {
                "node_id": "node-a2",
                "environment": "env-a",
                "token_sha256": hashlib.sha256(NODE_A2_TOKEN.encode()).hexdigest(),
            }
        )
        config_path.write_text(json.dumps(raw))
        config_path.chmod(0o600)

        import sqlite3

        with sqlite3.connect(f["settings"].database_file) as conn:
            conn.execute("UPDATE agent_runs SET actual_node_id = 'node-a2' WHERE qa_id = ?", (qa_id,))

        refused = f["client"].get(f"/v1/assistant/questions/{qa_id}", headers=HEADERS)
        assert refused.status_code in {403, 404}, refused.text
        assert "blocks" not in refused.text


def test_answer_stops_when_the_stored_exec_config_is_mismatched(tmp_path, monkeypatch):
    """执行配置核对不一致 ⇒ 不交付（复核必修 3 的第二项，受控存储损坏）。"""
    with node_chat(tmp_path, monkeypatch) as f:
        qa_id = ask(f["client"]).json()["qa_id"]
        assert wait_terminal(f["client"], qa_id)["status"] == "completed"

        import sqlite3

        with sqlite3.connect(f["settings"].database_file) as conn:
            conn.execute(
                "UPDATE agent_runs SET exec_config_state = 'mismatched', observed_agent_version = ? "
                "WHERE qa_id = ?",
                ("bad-digest", qa_id),
            )

        refused = f["client"].get(f"/v1/assistant/questions/{qa_id}", headers=HEADERS)
        assert refused.status_code in {403, 404}, refused.text
        assert "blocks" not in refused.text


def test_a_tampered_cached_answer_is_not_delivered(tmp_path, monkeypatch):
    """缓存的正文被改成别的 run 的结果 ⇒ 拒绝（复核必修 3 的第三项）。"""
    with node_chat(tmp_path, monkeypatch) as f:
        qa_id = ask(f["client"]).json()["qa_id"]
        assert wait_terminal(f["client"], qa_id)["status"] == "completed"

        import sqlite3

        with sqlite3.connect(f["settings"].database_file) as conn:
            conn.execute(
                "UPDATE assistant_questions SET result_json = ? WHERE qa_id = ?",
                (json.dumps({"blocks": [{"kind": "text", "text": "WRONG-RESULT-SENTINEL"}]}), qa_id),
            )

        refused = f["client"].get(f"/v1/assistant/questions/{qa_id}", headers=HEADERS)
        assert refused.status_code == 503, refused.text
        assert "WRONG-RESULT-SENTINEL" not in refused.text


def test_queued_and_running_are_readable_not_blocked_by_missing_snapshot(tmp_path, monkeypatch):
    """「还没有快照」不能把轮询挡住 —— 而且用的必须是**同一主体真的 POST 出来的** QA。"""
    gate = threading.Event()
    with node_chat(tmp_path, monkeypatch, gate_before_dispatch=gate) as f:
        qa_id = ask(f["client"]).json()["qa_id"]
        running = wait_status(f["client"], qa_id, "running")
        assert running["result"] is None
        assert running["retry_after_ms"] == 1000
        gate.set()
        assert wait_terminal(f["client"], qa_id)["status"] == "completed"


# ---------------------------------------------------------------- 取消


def test_cancel_before_dispatch_revokes_the_run_without_claiming(tmp_path, monkeypatch):
    """取消早于派发：run **已被撤销**，所以节点即便事后"来领"也领不到。

    「不派发」在真实链路上不成立 —— 派发是发往平台的异步消息，网关撤不回来。
    能保证的是下面这条：取消之后这条 run 已经不可领取，节点拿不到业务正文。
    """
    events: list[str] = []
    with node_chat(
        tmp_path, monkeypatch, gate_before_dispatch=threading.Event(), dispatch_events=events
    ) as f:
        qa_id = ask(f["client"]).json()["qa_id"]
        # 在 worker 派发之前取消：worker 看到 registration.cancelled 就撤销 run。
        f["app"].state.gateway.runtime._qa_registrations[qa_id].cancelled = True
        cancelled = f["client"].post(f"/v1/assistant/questions/{qa_id}/cancel", headers=HEADERS)
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        final = wait_terminal(f["client"], qa_id)
        assert final["status"] == "cancelled", final
        run = AgentRunStore(f["settings"].database_file).by_qa(qa_id)
        if run is not None:
            assert run.state != "succeeded"
            claimed = f["client"].post(
                f"/v1/internal/charging-status/claims/{run.run_id}",
                headers={
                    "Authorization": f"Bearer {NODE_A_TOKEN}",
                    "X-AI-Ops-Exec-Config": _identity(f["manifest"]),
                },
                json={},
            )
            assert claimed.status_code == 403, claimed.text
        else:
            assert events == []


def test_cancel_revokes_the_linked_run_and_late_result_cannot_overwrite(tmp_path, monkeypatch):
    """取消与迟到完成的可控时序：用**事件握手**钉住，不用 sleep 赌。"""
    claimed = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    with node_chat(tmp_path, monkeypatch) as f:

        def slow_dispatch(_config, run_id: str) -> None:
            token = NODE_A_TOKEN
            headers_ = {
                "Authorization": f"Bearer {token}",
                "X-AI-Ops-Exec-Config": _identity(f["manifest"]),
            }
            assert (
                f["client"]
                .post(f"/v1/internal/charging-status/claims/{run_id}", headers=headers_, json={})
                .status_code
                == 200
            )
            claimed.set()  # 领取真的发生过了
            release.wait(timeout=5)  # 等取消落库
            # 迟到的完成通知：网关必须拒绝它，而不是把它记成成功
            late = f["client"].post(
                f"/v1/internal/charging-status/completions/{run_id}",
                headers=headers_,
                json={"status": "completed"},
            )
            f["late"] = [late.status_code]
            finished.set()

        monkeypatch.setattr(
            "aiops_diagnostics.chat_execution.SnailDispatcher",
            lambda: type("D", (), {"dispatch": staticmethod(slow_dispatch)})(),
        )
        qa_id = ask(f["client"]).json()["qa_id"]
        assert claimed.wait(timeout=5), "the node never claimed"

        cancelled = f["client"].post(f"/v1/assistant/questions/{qa_id}/cancel", headers=HEADERS)
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        release.set()
        # 等「迟到的完成通知」真的发出去并拿到应答 —— 否则下一次断言比的是"还没发生"。
        assert finished.wait(timeout=5), "the late completion was never sent"

        final = wait_terminal(f["client"], qa_id)
        assert final["status"] == "cancelled", final
        assert final.get("result") in (None, {})
        run = AgentRunStore(f["settings"].database_file).by_qa(qa_id)
        assert run is not None and run.state == "revoked", run.state
        assert f["late"] == [403], f["late"]


# ---------------------------------------------------------------- 默认关闭 / 绑定外


def test_unbound_route_keeps_the_original_direct_read(tmp_path, monkeypatch):
    with node_chat(tmp_path, monkeypatch, tenants=()) as f:
        response = ask(f["client"])
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "completed"
        assert (tmp_path / "consumer-order-chat" / (body["qa_id"] + ".json")).exists()
        assert AgentRunStore(f["settings"].database_file).by_qa(body["qa_id"]) is None


def test_clearing_the_binding_returns_later_requests_to_the_old_route(tmp_path, monkeypatch):
    with node_chat(tmp_path, monkeypatch) as f:
        first = ask(f["client"])
        assert wait_terminal(f["client"], first.json()["qa_id"])["status"] == "completed"

        f["settings"].chat_execution_bindings_file = ""
        second = ask(f["client"])
        assert second.status_code == 202
        assert second.json()["status"] == "completed"  # 直读是同步完成的
        assert AgentRunStore(f["settings"].database_file).by_qa(second.json()["qa_id"]) is None


# ---------------------------------------------------------------- 真实 TCP


def test_real_tcp_post_then_poll_reaches_the_node(tmp_path, monkeypatch):
    """真实 uvicorn + TCP：POST 立刻回 202，随后用**原 GET**轮询到答案。"""
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


# ---------------------------------------------------------------- 会话轮次


def test_conversation_turn_keeps_the_status_qa_link(tmp_path, monkeypatch):
    from aiops_diagnostics.conversation_store import ConversationStore

    with node_chat(tmp_path, monkeypatch) as f:
        conversations = ConversationStore(f["settings"].database_file)
        scope = _Resolver().resolve("token-a", required_scope="aiops:orders:read")
        conversation = conversations.create(
            scope_fingerprint=scope.scope_fingerprint,
            business_entry="consumer",
            agent_version_key="agt_0123456789abcdef#v1",
        )
        response = f["client"].post(
            "/v1/assistant/questions",
            headers=HEADERS,
            json={"question": LATEST, "conversation_id": conversation["conversation_id"]},
        )
        assert response.status_code == 202, response.text
        qa_id = response.json()["qa_id"]
        assert wait_terminal(f["client"], qa_id)["status"] == "completed"

        import sqlite3

        with sqlite3.connect(f["settings"].database_file) as conn:
            row = conn.execute(
                "SELECT status_qa_id, answer_json FROM conversation_turns WHERE conversation_id = ?",
                (conversation["conversation_id"],),
            ).fetchone()
        assert row is not None, "conversation turn row is missing"
        assert row[0] == qa_id, f"status_qa_id not persisted: {row[0]!r}"
        assert row[1], "answer was not stored"


# ---------------------------------------------------------------- 续办：三个运行缺陷


def test_revocation_during_config_read_never_delivers_the_body(tmp_path, monkeypatch):
    """复核必修 1：配置读完之后必须**重读** run。

    `final_release` 原先先 `get` 拿到 `succeeded`，再去读绑定/配置，然后就用那份早先的
    对象做判定并返回。配置读取期间发生的撤销被完全漏掉 —— 复核实测 GET 仍 200 + 正文，
    而库里已经是 `revoked`。这里的撤销发生在**完全相同的窗口**里（配置读取内部）。
    """
    with node_chat(tmp_path, monkeypatch) as f:
        qa_id = ask(f["client"]).json()["qa_id"]
        assert wait_terminal(f["client"], qa_id)["status"] == "completed"

        store = AgentRunStore(f["settings"].database_file)
        run = store.by_qa(qa_id)
        assert run is not None

        real_callback_config = chat_execution.callback_config
        seen: list[str] = []

        def callback_config_then_revoke(store_, run_id_, settings_):
            config = real_callback_config(store_, run_id_, settings_)
            if not seen:
                seen.append(run_id_)
                # 就在配置读完、判定之前把记录撤销：这是「读配置期间」这个窗口。
                AgentRunStore(f["settings"].database_file).revoke(run_id_, "revoked_during_config")
            return config

        monkeypatch.setattr("aiops_diagnostics.chat_execution.callback_config", callback_config_then_revoke)
        refused = f["client"].get(f"/v1/assistant/questions/{qa_id}", headers=HEADERS)
        assert refused.status_code in {403, 404}, refused.text
        assert "blocks" not in refused.text
        assert AgentRunStore(f["settings"].database_file).peek(run.run_id).state == "revoked"


def test_expiry_during_config_read_never_delivers_the_body(tmp_path, monkeypatch):
    """同上，但到期发生在配置读取期间（另一种终态，另一条判据）。"""
    with node_chat(tmp_path, monkeypatch) as f:
        qa_id = ask(f["client"]).json()["qa_id"]
        assert wait_terminal(f["client"], qa_id)["status"] == "completed"

        store = AgentRunStore(f["settings"].database_file)
        run = store.by_qa(qa_id)
        assert run is not None

        import sqlite3
        from datetime import UTC, datetime, timedelta

        real_callback_config = chat_execution.callback_config
        seen: list[str] = []

        def callback_config_then_expire(store_, run_id_, settings_):
            config = real_callback_config(store_, run_id_, settings_)
            if not seen:
                seen.append(run_id_)
                past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
                with sqlite3.connect(f["settings"].database_file) as conn:
                    conn.execute("UPDATE agent_runs SET expires_at = ? WHERE run_id = ?", (past, run_id_))
            return config

        monkeypatch.setattr("aiops_diagnostics.chat_execution.callback_config", callback_config_then_expire)
        refused = f["client"].get(f"/v1/assistant/questions/{qa_id}", headers=HEADERS)
        assert refused.status_code in {403, 404}, refused.text
        assert "blocks" not in refused.text


def test_credential_rotation_with_an_inactive_sibling_entry_still_delivers(tmp_path, monkeypatch):
    """复核必修 2：同一 `node_id` 允许多条登记（旧 token 停用、新 token 生效）。

    原实现 `next(...)` 取**第一条**同 node_id 的登记；合法轮换时旧项在前且 `active: false`，
    于是生效中的新凭据被忽略 —— 复核实测 GET 403，且一条成功 run 被改写成 `failed`。
    记录多于一条是轮换的正常形态，不是要禁止的配置。
    """
    with node_chat(tmp_path, monkeypatch) as f:
        qa_id = ask(f["client"]).json()["qa_id"]
        assert wait_terminal(f["client"], qa_id)["status"] == "completed"

        entry = json.loads(f["bindings"].read_text())["bindings"][0]
        config_path = Path(entry["config_file"])
        raw = json.loads(config_path.read_text())
        active = raw["nodes"][0]
        # 旧凭据**排在前面**且已停用；生效中的那条留在后面。
        raw["nodes"] = [
            {
                "node_id": active["node_id"],
                "environment": active["environment"],
                "token_sha256": hashlib.sha256(b"rotated-out-token").hexdigest(),
                "active": False,
            },
            active,
        ]
        config_path.write_text(json.dumps(raw))
        config_path.chmod(0o600)

        response = f["client"].get(f"/v1/assistant/questions/{qa_id}", headers=HEADERS)
        assert response.status_code == 200, response.text
        assert "blocks" in response.json()["result"]
        # 成功记录**不能**因为一次合法轮换被改写成失败。
        assert AgentRunStore(f["settings"].database_file).by_qa(qa_id).state == "succeeded"


def test_worker_reauthorizes_the_order_before_writing_the_answer(tmp_path, monkeypatch):
    """复核必修 3：worker 在写结果之前要再判一次本人订单归属。

    复核实测：节点结束后把订单改属他人，worker 仍写 `completed` 并存下会话答案，
    授权计数在节点结束时与写完之后相同（它根本没问过）。GET 后来拒绝不能算 worker 做对了 ——
    那已经是一条虚假完成。
    """
    transferred: list[str] = []

    def revoke_ownership(state: dict) -> None:
        # 节点已经交回结果、worker 还没写 QA 的瞬间：订单转归他人。
        state["orders"].ownership[ORDER_A] = "USER-ELSEWHERE"
        transferred.append(ORDER_A)

    with node_chat(tmp_path, monkeypatch, after_completion=revoke_ownership) as f:
        qa_id = ask(f["client"]).json()["qa_id"]
        final = wait_terminal(f["client"], qa_id)

        assert transferred == [ORDER_A], "the hook never ran — the race window was not created"
        assert final["status"] == "failed", final
        assert final.get("result") in (None, {})

        import sqlite3

        with sqlite3.connect(f["settings"].database_file) as conn:
            answer = conn.execute(
                "SELECT answer_json FROM conversation_turns WHERE status_qa_id = ?", (qa_id,)
            ).fetchone()
        assert answer is None or not answer[0], "the worker wrote a conversation answer it must not have"


def test_worker_treats_authorization_outage_as_a_failure(tmp_path, monkeypatch):
    """授权源不可用同样不能写 `completed`：判不出来 ≠ 可以交付。"""
    outages: list[str] = []

    def outage(state: dict) -> None:
        state["orders"].unavailable = True
        outages.append(ORDER_A)

    with node_chat(tmp_path, monkeypatch, after_completion=outage) as f:
        qa_id = ask(f["client"]).json()["qa_id"]
        final = wait_terminal(f["client"], qa_id)
        assert outages == [ORDER_A]
        assert final["status"] == "failed", final
        assert final.get("result") in (None, {})
