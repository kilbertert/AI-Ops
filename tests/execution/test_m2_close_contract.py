"""委托执行的契约级验收：授权、生命周期与放行时点。

这些用例**不启动任何编排平台或执行节点**：它们证明 Gateway 这一侧的判据 ——
身份、订单可见性、记录的原子领取与终态、放行前复核、以及缓存声明。
执行节点上「工具集合只剩一个受控工具」那一类证据不在本文件，也不由它冒充。

替身的纪律（ADR-0030）：订单替身按同一套身份判据回答，不会比真授权器更宽松；
平台替身只负责「让节点来领一次」，不代替节点、也不代替模型。

逐个用例的编号与验收场景的对应见 `qa-plan.md` 的 DEL-REC 一节。
"""

from __future__ import annotations

import hashlib
import json
import stat
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aiops_diagnostics.execution import agent_runs
from aiops_diagnostics.execution.agent_runs import (
    EXPIRED,
    FAILED,
    ISSUED,
    REVOKED,
    RUNNING,
    SUCCEEDED,
    AgentRunConfig,
    AgentRunStore,
    scope_from_json,
    scope_to_json,
)
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.identity.caller_auth import CallerAuthError
from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord

NODE_A_TOKEN = "node-token-alpha-0123456789"
NODE_A2_TOKEN = "node-token-alpha-2-0123456789"
NODE_B_TOKEN = "node-token-beta-0123456789"
HEADERS = {"Authorization": "Bearer verified-token", "X-Business-Entry": "consumer"}
DELEGATED = "/v1/orders/{order}/charging-status/delegated"


# --------------------------------------------------------------------------
# 替身
# --------------------------------------------------------------------------


def _scope(c_user: str = "USER-A", *, delegated: bool = False, scope_type: str = "self") -> ScopeContext:
    subject = SubjectRecord(b_user_id="", c_user_id=c_user, tenant_id="TENANT-A")
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=delegated,
        effective_tenant_id="TENANT-A",
        data_scope=DataScope(type=scope_type),
        roles=frozenset(),
        permissions=frozenset({"aiops:orders:read"}),
        resolved_at=datetime(2026, 10, 17, tzinfo=UTC),
    )


class Resolver:
    def __init__(self) -> None:
        self.scope = _scope()

    def resolve(self, token: str, *, required_scope: str, **kwargs):
        if token != "verified-token":
            raise CallerAuthError("invalid", code="caller_auth.invalid")
        return self.scope


class Orders:
    """与真授权器同一套判据：订单属于该主体的 C 端用户才可见。"""

    DEFAULT = {"ORDER-A": "USER-A", "ORDER-B": "USER-B", "ORDER-C": "USER-A"}

    def __init__(self) -> None:
        self.ownership = dict(self.DEFAULT)
        self.unavailable = False
        self.calls: list[tuple[str, str]] = []
        #: 只在第 N 次「允许」回调里执行一次，用来在**最后一次授权内部**制造交错（R2-1）。
        #: 用次数而不是「下一次」：领取路径上有两次授权（领取前的预检 + 放行前的终检），
        #: 只挂「下一次」会落在预检里 —— 测到的并不是放行判定本身。
        self.mutations_at: dict[int, Callable[[], None]] = {}
        self.allow_count = 0
        self.mutations: list[int] = []

    def can_access(self, context: ScopeContext, order_no: str) -> bool:
        self.calls.append((context.subject.c_user_id or "", order_no))
        if self.unavailable:
            raise CallerAuthError("unavailable", code="caller_auth.unavailable")
        allowed = (
            context.data_scope.type == "self" and self.ownership.get(order_no) == context.subject.c_user_id
        )
        if allowed:
            self.allow_count += 1
            action = self.mutations_at.pop(self.allow_count, None)
            if action is not None:
                action()
                self.mutations.append(self.allow_count)
        return allowed


class Runtime:
    def shutdown(self) -> None:
        pass


@contextmanager
def cpms_stub():
    """与真实上游同形的桩 CPMS：``code/data`` 信封，查无此单非零 code，正文带敏感字段。

    带敏感字段是刻意的：接口的字段裁剪必须在这里被证出来，而不是靠读代码相信。
    """
    state = {"calls": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            prefix = "/charging-pile/chOrderInfo/chargingInfo/"
            order_no = self.path[len(prefix) :] if self.path.startswith(prefix) else ""
            payload = (
                {
                    "code": 0,
                    "msg": "success",
                    "data": {
                        "orderNo": order_no,
                        "status": "0",
                        "plateNo": "DO-NOT-LEAK",
                        "userId": "DO-NOT-LEAK",
                        "deviceId": "DO-NOT-LEAK",
                    },
                }
                if order_no in Orders.DEFAULT
                else {"code": 1, "msg": f"订单{order_no}不存在", "data": None, "ok": False}
            )
            if order_no in Orders.DEFAULT:
                state["calls"] += 1
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        state["base"] = f"http://127.0.0.1:{server.server_port}/charging-pile"
        yield state
    finally:
        server.shutdown()
        thread.join(timeout=5)


class FakeDispatcher:
    """平台那一跳的替身：`claim` 为 None 时只记录、不领取。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.claim = None
        self.fail = False

    def dispatch(self, config, run_id: str) -> str:
        self.calls.append((config.environment, run_id))
        if self.fail:
            raise agent_runs.AgentRunDispatchError("platform unavailable")
        if self.claim is not None:
            self.claim(run_id)
        return "ok"


def _write_config(tmp_path: Path, *, node_token: str = NODE_A_TOKEN) -> Path:
    path = tmp_path / "agent-runs.json"
    path.write_text(
        json.dumps(
            {
                "environment": "env-a",
                "snail_base_url": "http://127.0.0.1:1",
                "snail_app_id": "aiops-alpha",
                "snail_app_token": "app-token-alpha",
                "snail_agent_id": 7,
                "snail_open_id": "open-id-alpha",
                "expected_node_id": "node-a1",
                "nodes": [
                    {
                        "node_id": "node-a1",
                        "environment": "env-a",
                        "token_sha256": hashlib.sha256(node_token.encode()).hexdigest(),
                    },
                    {
                        "node_id": "node-a2",
                        "environment": "env-a",
                        "token_sha256": hashlib.sha256(NODE_A2_TOKEN.encode()).hexdigest(),
                    },
                ],
            }
        )
    )
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


def _app(tmp_path, *, monkeypatch, node_token: str = NODE_A_TOKEN, upstream_base: str = ""):
    config_file = _write_config(tmp_path, node_token=node_token)
    production = tmp_path / "production.env"
    production.write_text("# synthetic\n")
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=production,
        charging_status_base_url=upstream_base or "http://127.0.0.1:1/charging-pile",
        execution_environment="env-a",
        agent_run_config_file=str(config_file),
    )
    resolver = Resolver()
    orders = Orders()
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=Runtime(),
        caller_resolver=resolver,
        order_authorizer=orders,
    )
    dispatcher = FakeDispatcher()
    # 平台在「派发期间」让节点来领一次：这必须发生在**同一个进程**里，用同一个应用。
    claim_client = TestClient(app)

    def claim(run_id: str) -> None:
        response = claim_client.post(
            f"/v1/internal/charging-status/claims/{run_id}",
            headers={"Authorization": f"Bearer {node_token}"},
            json={},
        )
        if response.status_code != 200:
            raise agent_runs.AgentRunDispatchError("claim failed")

    dispatcher.claim = claim
    monkeypatch.setattr("aiops_diagnostics.gateway_api.SnailDispatcher", lambda: dispatcher)
    return {
        "app": app,
        "client": claim_client,
        "orders": orders,
        "resolver": resolver,
        "dispatcher": dispatcher,
        "store": AgentRunStore(tmp_path / "gateway.db"),
        "config": config_file,
    }


def _delegate(fixture, order: str = "ORDER-A", headers=None):
    # ``headers=None`` 必须保持「用默认头」；用 ``or`` 会把空 dict 变成默认头，
    # 于是「不带凭据」的负例其实带着凭据 —— 一个能让整组负例假通过的写法。
    return fixture["client"].post(
        DELEGATED.format(order=order), headers=HEADERS if headers is None else headers, json={}
    )


def _claim(fixture, run_id: str, token: str = NODE_A_TOKEN, **headers):
    return fixture["client"].post(
        f"/v1/internal/charging-status/claims/{run_id}",
        headers={"Authorization": f"Bearer {token}", **headers},
        json={},
    )


def _runs(tmp_path: Path) -> list[dict]:
    with AgentRunStore(tmp_path / "gateway.db")._connection() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM agent_runs").fetchall()]


# --------------------------------------------------------------------------
# IC-02：不建授权、不调平台、不读业务
# --------------------------------------------------------------------------


def test_ic02_unauthenticated_and_wrong_token_never_create_a_run(tmp_path, monkeypatch):
    with cpms_stub() as upstream:
        fixture = _app(tmp_path, monkeypatch=monkeypatch, upstream_base=upstream["base"])
        assert _delegate(fixture, headers={}).status_code == 401
        assert _delegate(fixture, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert fixture["dispatcher"].calls == []
    assert fixture["orders"].calls == []
    assert _runs(tmp_path) == []


@pytest.mark.parametrize("scope_kwargs", [{"scope_type": "all"}, {"delegated": True}])
def test_ic02_non_consumer_scope_refused_before_any_record(tmp_path, monkeypatch, scope_kwargs):
    fixture = _app(tmp_path, monkeypatch=monkeypatch)
    fixture["resolver"].scope = _scope(**scope_kwargs)
    assert _delegate(fixture).status_code == 403
    assert fixture["dispatcher"].calls == [] and fixture["orders"].calls == []
    assert _runs(tmp_path) == []


def test_ic02_missing_entry_header_is_refused_not_defaulted(tmp_path, monkeypatch):
    fixture = _app(tmp_path, monkeypatch=monkeypatch)
    response = _delegate(fixture, headers={"Authorization": "Bearer verified-token"})
    assert response.status_code == 403
    assert fixture["dispatcher"].calls == []


@pytest.mark.parametrize("order_no", ["ORDER-B", "ORDER-MISSING"])
def test_ic02_other_or_missing_order_is_404_and_creates_nothing(tmp_path, monkeypatch, order_no):
    fixture = _app(tmp_path, monkeypatch=monkeypatch)
    response = _delegate(fixture, order=order_no)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ORDER_NOT_FOUND"
    assert fixture["dispatcher"].calls == [] and _runs(tmp_path) == []


# --------------------------------------------------------------------------
# IC-03：篡改、凭据错配、只持 run_id
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"appId": "aiops-beta"},
        {"agentId": 99},
        {"environment": "env-b"},
        {"sid": "run_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
        {"order_no": "ORDER-C"},
        {"tenant_id": "TENANT-B"},
    ],
)
def test_ic03_tampered_fields_are_rejected_not_ignored(tmp_path, monkeypatch, payload):
    fixture = _app(tmp_path, monkeypatch=monkeypatch)
    response = fixture["client"].post(DELEGATED.format(order="ORDER-A"), headers=HEADERS, json=payload)
    assert response.status_code == 422
    assert fixture["dispatcher"].calls == []


def test_ic03_only_holding_run_id_gets_nothing(tmp_path, monkeypatch):
    with cpms_stub() as upstream:
        fixture = _app(tmp_path, monkeypatch=monkeypatch, upstream_base=upstream["base"])
        assert _delegate(fixture).status_code == 200
    run_id = _runs(tmp_path)[0]["run_id"]
    anonymous = fixture["client"].post(f"/v1/internal/charging-status/claims/{run_id}", json={})
    wrong = _claim(fixture, run_id, token="not-a-node-token")
    assert anonymous.status_code == 401 and wrong.status_code == 401
    assert "order_no" not in anonymous.json() and "order_no" not in wrong.json()


def test_ic03_foreign_and_sibling_node_credentials_cannot_claim(tmp_path, monkeypatch):
    with cpms_stub() as upstream:
        fixture = _app(tmp_path, monkeypatch=monkeypatch, upstream_base=upstream["base"])
        assert _delegate(fixture).status_code == 200
    run_id = _runs(tmp_path)[0]["run_id"]
    foreign = _claim(fixture, run_id, token=NODE_B_TOKEN)  # 不在本环境登记
    sibling = _claim(fixture, run_id, token=NODE_A2_TOKEN)  # 同环境、但不是记录里的节点
    assert foreign.status_code == 401
    assert sibling.status_code == 403
    assert sibling.json()["error"]["code"] == "AGENT_RUN_WRONG_NODE"


def test_ic03_claim_body_cannot_override_anything(tmp_path, monkeypatch):
    fixture = _app(tmp_path, monkeypatch=monkeypatch)
    fixture["dispatcher"].claim = None
    assert _delegate(fixture).status_code == 502  # 平台没让节点领，委托如实失败
    run_id = _runs(tmp_path)[0]["run_id"]
    response = fixture["client"].post(
        f"/v1/internal/charging-status/claims/{run_id}",
        headers={"Authorization": f"Bearer {NODE_A_TOKEN}"},
        json={"order_no": "ORDER-C", "tenant_id": "TENANT-B", "node_id": "node-a2"},
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------
# IC-04 / IC-05：归属、并发、重试
# --------------------------------------------------------------------------


def test_ic04_each_run_keeps_its_own_order_and_identity(tmp_path, monkeypatch):
    fixture = _app(tmp_path, monkeypatch=monkeypatch)
    fixture["dispatcher"].claim = None
    assert _delegate(fixture, order="ORDER-A").status_code == 502
    fixture["resolver"].scope = _scope("USER-B")
    assert _delegate(fixture, order="ORDER-B").status_code == 502
    runs = [_run(fixture, row["run_id"]) for row in _runs(tmp_path)]
    assert [(run.order_no, scope_from_json(run.scope_json).subject.c_user_id) for run in runs] == [
        ("ORDER-A", "USER-A"),
        ("ORDER-B", "USER-B"),
    ]


def _gw_store(fixture):
    """应用**自己**持有的那个 AgentRunStore 实例（路由用它）。

    测试另开的 `fixture["store"]` 指向同一个 SQLite 文件，读写等价，但对它打补丁
    不会影响路由 —— 这一点曾让一条 R1-1 回归看起来「通过」却什么都没验证。
    """
    return fixture["app"].state.gateway.agent_run_store


def _run(fixture, run_id: str):
    run = fixture["store"].get(run_id)
    assert run is not None
    return run


def test_ic05_concurrent_claims_let_only_one_read_the_upstream(tmp_path, monkeypatch):
    fixture = _app(tmp_path, monkeypatch=monkeypatch)
    fixture["dispatcher"].claim = None
    assert _delegate(fixture).status_code == 502
    run_id = _runs(tmp_path)[0]["run_id"]
    assert fixture["store"].claim(run_id) is True  # 另一个执行者已经在读
    second = _claim(fixture, run_id)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "AGENT_RUN_IN_PROGRESS"


def test_ic05_retry_after_success_returns_the_same_snapshot(tmp_path, monkeypatch):
    with cpms_stub() as upstream:
        fixture = _app(tmp_path, monkeypatch=monkeypatch, upstream_base=upstream["base"])
        first = _delegate(fixture)
        assert first.status_code == 200
    run_id = _runs(tmp_path)[0]["run_id"]
    second = _claim(fixture, run_id)
    assert second.status_code == 200
    assert upstream["calls"] == 1  # 重试不再读一次业务
    assert second.json()["run_state"] == SUCCEEDED
    receipt = _run(fixture, run_id).receipt()
    assert receipt["state"] == SUCCEEDED and receipt["result_sha256"]


def test_ic05_two_threads_racing_to_claim_only_one_wins(tmp_path):
    config = AgentRunConfig.load(_write_config(tmp_path))
    store = AgentRunStore(tmp_path / "gateway.db")
    run = store.issue(scope=_scope(), order_no="ORDER-A", config=config, gateway_version="test")
    assert run.state == ISSUED
    results: list[bool] = []
    barrier = threading.Barrier(4)

    def claim() -> None:
        barrier.wait()
        results.append(store.claim(run.run_id))

    threads = [threading.Thread(target=claim) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, False, False, True]
    assert store.get(run.run_id).state == RUNNING
    time.sleep(0.01)
    assert store.claim(run.run_id) is False


# --------------------------------------------------------------------------
# IC-06：过期、撤销、重启、配置纪律
# --------------------------------------------------------------------------


def test_ic06_expired_run_cannot_be_claimed(tmp_path, monkeypatch):
    _app(tmp_path, monkeypatch=monkeypatch)
    store = AgentRunStore(tmp_path / "gateway.db")
    config = AgentRunConfig.load(_write_config(tmp_path))
    run = store.issue(scope=_scope(), order_no="ORDER-A", config=config, gateway_version="test")
    with store._connection(write=True) as connection:
        connection.execute(
            "UPDATE agent_runs SET expires_at = ? WHERE run_id = ?",
            (datetime(2026, 10, 17, tzinfo=UTC).isoformat(), run.run_id),
        )
    assert store.get(run.run_id).state == EXPIRED
    assert store.claim(run.run_id) is False


def test_ic06_revoked_run_releases_nothing(tmp_path, monkeypatch):
    fixture = _app(tmp_path, monkeypatch=monkeypatch)
    fixture["dispatcher"].claim = None
    assert _delegate(fixture).status_code == 502
    run_id = _runs(tmp_path)[0]["run_id"]
    assert fixture["store"].revoke(run_id) is True
    response = _claim(fixture, run_id)
    assert response.status_code == 403
    assert fixture["store"].get(run_id).state == REVOKED
    assert response.json()["error"]["code"] == "AGENT_RUN_NOT_ACTIVE"


def test_ic06_restart_never_requeues_a_running_run(tmp_path, monkeypatch):
    fixture = _app(tmp_path, monkeypatch=monkeypatch)
    fixture["dispatcher"].claim = None
    assert _delegate(fixture).status_code == 502
    run_id = _runs(tmp_path)[0]["run_id"]
    assert fixture["store"].claim(run_id) is True
    reopened = AgentRunStore(tmp_path / "gateway.db")  # 新进程打开同一个库
    assert reopened.get(run_id).state == RUNNING
    assert reopened.claim(run_id) is False


def test_ic06_config_file_permissions_are_enforced(tmp_path):
    path = _write_config(tmp_path)
    path.chmod(0o644)
    with pytest.raises(agent_runs.AgentRunConfigError):
        AgentRunConfig.load(path)
    path.chmod(0o600)
    assert AgentRunConfig.load(path).environment == "env-a"


def test_ic06_environment_mismatch_refuses_to_dispatch(tmp_path, monkeypatch):
    fixture = _app(tmp_path, monkeypatch=monkeypatch)
    fixture["app"].state.gateway.settings.execution_environment = "env-b"
    try:
        response = _delegate(fixture)
    finally:
        fixture["app"].state.gateway.settings.execution_environment = "env-a"
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AGENT_RUN_ENVIRONMENT_MISMATCH"
    assert fixture["dispatcher"].calls == []


# --------------------------------------------------------------------------
# IC-09：上游失败与授权变化
# --------------------------------------------------------------------------


def test_ic09_authorization_revoked_between_issue_and_claim_releases_nothing(tmp_path, monkeypatch):
    fixture = _app(tmp_path, monkeypatch=monkeypatch)
    fixture["dispatcher"].claim = None
    assert _delegate(fixture).status_code == 502
    run_id = _runs(tmp_path)[0]["run_id"]
    fixture["orders"].ownership["ORDER-A"] = "USER-ELSEWHERE"
    response = _claim(fixture, run_id)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AGENT_RUN_ORDER_NOT_VISIBLE"
    assert fixture["store"].get(run_id).snapshot is None


def test_ic09_authorization_outage_is_retryable_not_not_found(tmp_path, monkeypatch):
    fixture = _app(tmp_path, monkeypatch=monkeypatch)
    fixture["dispatcher"].claim = None
    assert _delegate(fixture).status_code == 502
    fixture["orders"].unavailable = True
    response = _claim(fixture, _runs(tmp_path)[0]["run_id"])
    assert response.status_code == 503
    assert response.json()["error"]["retryable"] is True


def test_ic09_platform_failure_marks_the_run_failed(tmp_path, monkeypatch):
    fixture = _app(tmp_path, monkeypatch=monkeypatch)
    fixture["dispatcher"].fail = True
    response = _delegate(fixture)
    assert response.status_code == 502
    run = _run(fixture, _runs(tmp_path)[0]["run_id"])
    assert run.state == "failed" and run.reason == "platform_unavailable"
    assert run.actual_node_id is None and run.snapshot is None


def test_ic09_platform_without_result_is_not_reported_as_success(tmp_path, monkeypatch):
    fixture = _app(tmp_path, monkeypatch=monkeypatch)
    fixture["dispatcher"].claim = None  # 平台回话，但节点没有交回结果
    response = _delegate(fixture)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "AGENT_RUN_NO_RESULT"


# --------------------------------------------------------------------------
# R1-1：放行是单独判定（确定性交错，不用 sleep 猜竞态）
# --------------------------------------------------------------------------


def _succeed_run(tmp_path, monkeypatch):
    """把一条记录推进到 succeeded（快照来自桩 CPMS），返回 (fixture, run_id)。"""
    upstream_ctx = cpms_stub()
    upstream = upstream_ctx.__enter__()
    fixture = _app(tmp_path, monkeypatch=monkeypatch, upstream_base=upstream["base"])
    fixture["_upstream_ctx"] = upstream_ctx
    fixture["_upstream"] = upstream
    fixture["dispatcher"].claim = None  # 由测试自己扮演节点，时序完全可控
    assert _delegate(fixture).status_code == 502
    run_id = _runs(tmp_path)[0]["run_id"]
    claimed = _claim(fixture, run_id)
    assert claimed.status_code == 200, claimed.text
    assert fixture["store"].get(run_id).state == SUCCEEDED
    return fixture, run_id


def test_r1_1_revoked_during_authorization_never_releases_the_snapshot(tmp_path, monkeypatch):
    """交错 1：快照已被取到，但在订单授权回调里撤销了 run。"""
    fixture, run_id = _succeed_run(tmp_path, monkeypatch)
    fixture["store"].revoke(run_id, "revoked_before_release")
    response = _claim(fixture, run_id)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "AGENT_RUN_NOT_ACTIVE"
    assert "order_no" not in response.json()
    assert fixture["store"].get(run_id).state == REVOKED


def test_r1_1_revoked_between_complete_and_release_never_releases(tmp_path, monkeypatch):
    """交错 2：``complete()`` 之后、最后一次读记录之前撤销 —— 这一条原先返回了快照。"""
    fixture, run_id = _succeed_run(tmp_path, monkeypatch)
    store = _gw_store(fixture)
    original_get = store.get
    seen: list[str] = []

    def get(run_id_: str):
        # 放行判定会再读一次记录；在这里把它撤销，模拟「就在交出去之前」。
        run = original_get(run_id_)
        if run is not None and run.state == SUCCEEDED and not seen:
            seen.append(run_id_)
            store.revoke(run_id_, "revoked_between_complete_and_release")
            return original_get(run_id_)
        return run

    store.get = get
    response = _claim(fixture, run_id)
    assert response.status_code == 403, response.text
    assert "order_no" not in response.json()
    assert fixture["store"].get(run_id).state == REVOKED


def test_r1_1_node_deactivated_during_read_never_releases(tmp_path, monkeypatch):
    """交错 3：业务读取进行中把节点置为 inactive —— 结果必须拒绝且记录作废。"""
    from aiops_diagnostics import gateway_api as gateway_api_module

    upstream_ctx = cpms_stub()
    upstream = upstream_ctx.__enter__()
    fixture = _app(tmp_path, monkeypatch=monkeypatch, upstream_base=upstream["base"])
    fixture["dispatcher"].claim = None
    assert _delegate(fixture).status_code == 502
    run_id = _runs(tmp_path)[0]["run_id"]

    original_read = gateway_api_module.read_charging_status
    config_path = Path(fixture["config"])

    def read_then_deactivate(base_url, order_no, *, tenant_id, user_id):
        snapshot = original_read(base_url, order_no, tenant_id=tenant_id, user_id=user_id)
        # 就在这次读取之后把节点停用：放行判定必须重读配置才发现这件事。
        data = json.loads(config_path.read_text())
        data["nodes"][0]["active"] = False
        config_path.write_text(json.dumps(data))
        config_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return snapshot

    monkeypatch.setattr(gateway_api_module, "read_charging_status", read_then_deactivate)
    response = _claim(fixture, run_id)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "AGENT_RUN_NODE_INACTIVE"
    assert "order_no" not in response.json()
    stored = fixture["store"].get(run_id)
    assert stored.state == "failed" and stored.reason == "node_deactivated"
    # 节点被停用后，连同一凭据都不能再领取。
    assert _claim(fixture, run_id).status_code == 401


def test_r1_1_expiry_reached_during_read_never_releases(tmp_path, monkeypatch):
    """期限在读取期间到达：``complete()`` 拒绝，结果不落库也不返回。"""
    from aiops_diagnostics import gateway_api as gateway_api_module

    upstream_ctx = cpms_stub()
    upstream = upstream_ctx.__enter__()
    fixture = _app(tmp_path, monkeypatch=monkeypatch, upstream_base=upstream["base"])
    fixture["dispatcher"].claim = None
    assert _delegate(fixture).status_code == 502
    run_id = _runs(tmp_path)[0]["run_id"]

    original_read = gateway_api_module.read_charging_status

    def read_then_expire(base_url, order_no, *, tenant_id, user_id):
        snapshot = original_read(base_url, order_no, tenant_id=tenant_id, user_id=user_id)
        with fixture["store"]._connection(write=True) as connection:
            connection.execute(
                "UPDATE agent_runs SET expires_at = ? WHERE run_id = ?",
                (datetime(2026, 10, 17, tzinfo=UTC).isoformat(), run_id),
            )
        return snapshot

    monkeypatch.setattr(gateway_api_module, "read_charging_status", read_then_expire)
    response = _claim(fixture, run_id)
    assert response.status_code == 403, response.text
    assert "order_no" not in response.json()
    assert fixture["store"].get(run_id).snapshot is None


def test_r1_1_order_authorization_change_during_read_never_releases(tmp_path, monkeypatch):
    """订单授权在**读取期间**变化：快照已算出，但订单已不归本人 —— 不放行。"""
    from aiops_diagnostics import gateway_api as gateway_api_module

    upstream_ctx = cpms_stub()
    upstream = upstream_ctx.__enter__()
    fixture = _app(tmp_path, monkeypatch=monkeypatch, upstream_base=upstream["base"])
    fixture["dispatcher"].claim = None
    assert _delegate(fixture).status_code == 502
    run_id = _runs(tmp_path)[0]["run_id"]

    original_read = gateway_api_module.read_charging_status

    def read_then_transfer(base_url, order_no, *, tenant_id, user_id):
        snapshot = original_read(base_url, order_no, tenant_id=tenant_id, user_id=user_id)
        fixture["orders"].ownership["ORDER-A"] = "USER-ELSEWHERE"
        return snapshot

    monkeypatch.setattr(gateway_api_module, "read_charging_status", read_then_transfer)
    response = _claim(fixture, run_id)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "AGENT_RUN_ORDER_NOT_VISIBLE"
    stored = fixture["store"].get(run_id)
    # 记录被钉成 failed；库里保留快照只为审计，**放行判定**才是那条界线。
    assert stored.state == "failed" and stored.reason == "order_not_visible"
    retried = _claim(fixture, run_id)
    assert retried.status_code == 403 and "order_no" not in retried.json()


def test_r1_1_delegated_receipt_also_requires_an_active_run(tmp_path, monkeypatch):
    """外部委派回执同样遵守有效状态：撤销后的记录不能再被当作成功回执交回。"""
    fixture, run_id = _succeed_run(tmp_path, monkeypatch)
    fixture["store"].revoke(run_id, "revoked_before_receipt")
    run = fixture["store"].get(run_id)
    assert run.state == REVOKED and run.snapshot is not None  # 快照仍在库里
    assert run.receipt()["state"] == REVOKED  # 回执如实报状态，不冒充 succeeded


# --------------------------------------------------------------------------
# R2-1：授权回调**内部**的变化必须影响同一次放行
# --------------------------------------------------------------------------


def _make_succeeded(fixture, order_no: str = "ORDER-A") -> str:
    """在**同一个 fixture** 上再造一条 succeeded 记录（用于校准与真正的被测对象）。"""
    fixture["dispatcher"].claim = None
    assert _delegate(fixture, order=order_no).status_code == 502
    rows = [
        (value["created_at"], key)
        for key, value in _all_runs_for(fixture).items()
        if value["order_no"] == order_no and value["state"] == "issued"
    ]
    assert rows, f"没有 {order_no} 的 issued 记录"
    run_id = max(rows)[1]
    assert _claim(fixture, run_id).status_code == 200
    return run_id


def _all_runs_for(fixture) -> dict[str, dict]:
    with _gw_store(fixture)._connection() as connection:
        return {row["run_id"]: dict(row) for row in connection.execute("SELECT * FROM agent_runs")}


def _claim_with_mutation(fixture, run_id: str, action) -> tuple[Any, list[int]]:
    """在放行判定的**最后一次**订单授权回调内部执行 ``action``，返回 (响应, 实际触发的序号)。

    领取路径上授权会被调用两次（领取前的预检 + 放行前的终检）。序号不写死：
    先用**另一条**成功记录做一次同样的领取，测出这次路径上一共几次允许回调，
    再在被测记录上按同一序号挂钩子。若钩子没命中，``mutations`` 为空 —— 测试会直接失败，
    不会出现「名字说是终检、实际什么都没触发」的回归（R1 的教训）。
    """
    probe_run = _make_succeeded(fixture, "ORDER-C")
    before = fixture["orders"].allow_count
    assert _claim(fixture, probe_run).status_code == 200
    relative = fixture["orders"].allow_count - before
    assert relative >= 2, f"领取路径上应至少有预检 + 终检两次授权，实际 {relative}"
    index = fixture["orders"].allow_count + relative
    fixture["orders"].mutations_at = {index: action}
    response = _claim(fixture, run_id)
    return response, list(fixture["orders"].mutations)


def test_r2_1_revoke_inside_the_final_authorization_is_refused(tmp_path, monkeypatch):
    fixture, run_id = _succeed_run(tmp_path, monkeypatch)
    store = _gw_store(fixture)
    response, hooks = _claim_with_mutation(
        fixture, run_id, lambda: store.revoke(run_id, "revoked_inside_authorization")
    )
    assert len(hooks) == 1, f"钩子必须真的在**最后一次**授权回调里执行过，实际 {hooks}"
    assert response.status_code == 403, response.text
    assert "order_no" not in response.json()
    assert fixture["store"].get(run_id).state == REVOKED


def test_r2_1_expiry_inside_the_final_authorization_is_refused(tmp_path, monkeypatch):
    fixture, run_id = _succeed_run(tmp_path, monkeypatch)
    store = _gw_store(fixture)

    def expire() -> None:
        with store._connection(write=True) as connection:
            connection.execute(
                "UPDATE agent_runs SET expires_at = ? WHERE run_id = ?",
                (datetime(2026, 10, 17, tzinfo=UTC).isoformat(), run_id),
            )

    response, hooks = _claim_with_mutation(fixture, run_id, expire)
    assert len(hooks) == 1, f"钩子必须真的在**最后一次**授权回调里执行过，实际 {hooks}"
    assert response.status_code == 403, response.text
    assert "order_no" not in response.json()
    assert fixture["store"].get(run_id).state == EXPIRED


def test_r2_1_node_deactivation_inside_the_final_authorization_is_refused(tmp_path, monkeypatch):
    fixture, run_id = _succeed_run(tmp_path, monkeypatch)
    config_path = Path(fixture["config"])

    def deactivate() -> None:
        data = json.loads(config_path.read_text())
        data["nodes"][0]["active"] = False
        config_path.write_text(json.dumps(data))
        config_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    response, hooks = _claim_with_mutation(fixture, run_id, deactivate)
    assert len(hooks) == 1, f"钩子必须真的在**最后一次**授权回调里执行过，实际 {hooks}"
    assert response.status_code == 403, response.text
    assert "order_no" not in response.json()
    stored = fixture["store"].get(run_id)
    assert stored.state == "failed" and stored.reason == "node_deactivated"


def test_r2_1_binding_change_inside_the_final_authorization_is_refused(tmp_path, monkeypatch):
    fixture, run_id = _succeed_run(tmp_path, monkeypatch)
    config_path = Path(fixture["config"])

    def rebind() -> None:
        data = json.loads(config_path.read_text())
        data["snail_agent_id"] = 99  # 换了智能体：旧记录不能被解释成新绑定
        config_path.write_text(json.dumps(data))
        config_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    response, hooks = _claim_with_mutation(fixture, run_id, rebind)
    assert len(hooks) == 1, f"钩子必须真的在**最后一次**授权回调里执行过，实际 {hooks}"
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "AGENT_RUN_WRONG_NODE"
    assert fixture["store"].get(run_id).state == "failed"


def test_r2_1_revoke_after_a_real_complete_is_refused(tmp_path, monkeypatch):
    """这一条执行**真实的** complete()，再在之后的放行判定里撤销（R2-1 的名称与行为要对上）。"""

    upstream_ctx = cpms_stub()
    upstream = upstream_ctx.__enter__()
    fixture = _app(tmp_path, monkeypatch=monkeypatch, upstream_base=upstream["base"])
    fixture["dispatcher"].claim = None
    assert _delegate(fixture).status_code == 502
    run_id = _runs(tmp_path)[0]["run_id"]
    store = _gw_store(fixture)
    completed: list[bool] = []
    original_complete = store.complete

    def complete(*args, **kwargs):
        result = original_complete(*args, **kwargs)
        completed.append(bool(result))
        return result

    monkeypatch.setattr(store, "complete", complete)
    assert _claim(fixture, run_id).status_code == 200
    assert completed == [True], "complete() 必须真的执行并成功"
    fixture["store"].revoke(run_id, "revoked_after_real_complete")
    response = _claim(fixture, run_id)
    assert response.status_code == 403
    assert "order_no" not in response.json()


# --------------------------------------------------------------------------
# R1-2：按人的授权结果不进共享缓存
# --------------------------------------------------------------------------


def test_r1_2_success_responses_are_no_store(tmp_path, monkeypatch):
    fixture, run_id = _succeed_run(tmp_path, monkeypatch)
    fresh = _claim(fixture, run_id)
    assert fresh.status_code == 200
    assert fresh.headers.get("cache-control") == "no-store"
    direct = fixture["client"].get("/v1/orders/ORDER-A/charging-status", headers=HEADERS)
    assert direct.status_code == 200
    assert direct.headers.get("cache-control") == "no-store"
    delegated = _delegate(fixture)
    assert delegated.headers.get("cache-control") == "no-store"


def test_r1_2_error_responses_are_no_store_on_this_slice_only(tmp_path, monkeypatch):
    fixture, run_id = _succeed_run(tmp_path, monkeypatch)
    denied = fixture["client"].post(
        f"/v1/internal/charging-status/claims/{run_id}",
        headers={"Authorization": "Bearer not-a-node-token"},
        json={},
    )
    assert denied.status_code == 401
    assert denied.headers.get("cache-control") == "no-store"
    invalid = fixture["client"].post(
        "/v1/orders/ORDER-A/charging-status/delegated", headers=HEADERS, json={"appId": "x"}
    )
    assert invalid.status_code == 422
    assert invalid.headers.get("cache-control") == "no-store"
    # 与本切片无关的端点不跟着改缓存语义。
    health = fixture["client"].get("/health")
    assert health.status_code == 200
    assert health.headers.get("cache-control") is None


# --------------------------------------------------------------------------
# IC-10：执行位置来自认证，不来自自报
# --------------------------------------------------------------------------


def test_ic10_forged_identity_headers_cannot_change_the_audited_node(tmp_path, monkeypatch):
    with cpms_stub() as upstream:
        fixture = _app(tmp_path, monkeypatch=monkeypatch, upstream_base=upstream["base"])
        # 先只建记录、不领取：让测试自己扮演节点，带上各种「自称身份」的头来领。
        fixture["dispatcher"].claim = None
        assert _delegate(fixture).status_code == 502
        run_id = _runs(tmp_path)[0]["run_id"]
        claimed = _claim(
            fixture,
            run_id,
            **{"X-Node-Id": "node-forged", "Host-Id": "node-forged", "X-AI-Ops-Node-Build": "build-1"},
        )
    assert claimed.status_code == 200
    body = claimed.json()
    assert body["actual_node_id"] == "node-a1"
    receipt = fixture["store"].get(run_id).receipt()
    assert receipt["actual_node_id"] == "node-a1" and receipt["node_build"] == "build-1"


def test_ic10_no_claim_means_no_success_evidence(tmp_path, monkeypatch):
    fixture = _app(tmp_path, monkeypatch=monkeypatch)
    fixture["dispatcher"].claim = None
    assert _delegate(fixture).status_code == 502
    run = fixture["store"].get(_runs(tmp_path)[0]["run_id"])
    assert run.state != SUCCEEDED
    assert run.actual_node_id is None and run.snapshot is None
    receipt = run.receipt()
    assert receipt["actual_node_id"] is None and receipt["result_sha256"] is None


def test_receipt_reports_the_version_gap_instead_of_pretending(tmp_path, monkeypatch):
    with cpms_stub() as upstream:
        fixture = _app(tmp_path, monkeypatch=monkeypatch, upstream_base=upstream["base"])
        response = _delegate(fixture)
        assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["agent_version"] is None
    assert receipt["agent_version_gap"] == "not_wired"
    # Read the version instead of pinning it: what this test asserts is that the
    # receipt *reports* a gateway version, not that the version is any particular
    # number. Pinning it made every release turn this red for a reason that has
    # nothing to do with the receipt — measured 2026-10-23, when bumping
    # `0.1.0` → `0.2.0` failed here.
    expected_version = (Path(__file__).resolve().parents[2] / "VERSION").read_text(encoding="utf-8").strip()
    assert receipt["gateway_version"] == expected_version
    assert receipt["expected_environment"] == "env-a"
    assert receipt["expected_app_id"] == "aiops-alpha"
    assert receipt["actual_node_id"] == "node-a1"
    assert "order_no" not in receipt  # 回执不自带业务正文


# --------------------------------------------------------------------------
# 记录与配置的基本不变量
# --------------------------------------------------------------------------


def test_scope_snapshot_round_trip_refuses_tampering():
    scope = _scope()
    assert scope_from_json(scope_to_json(scope)).scope_fingerprint == scope.scope_fingerprint
    tampered = json.loads(scope_to_json(scope))
    tampered["effective_tenant_id"] = "TENANT-B"
    with pytest.raises(ValueError):
        scope_from_json(json.dumps(tampered))


def test_delegation_requires_charging_settings(tmp_path):
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
        agent_run_config_file=str(_write_config(tmp_path)),
    )
    with pytest.raises(ValueError):
        settings.validate()


def test_run_ttl_is_bounded():
    assert 30 <= agent_runs.RUN_TTL_SECONDS <= 600


def test_claim_response_carries_only_the_seven_fields_plus_location(tmp_path, monkeypatch):
    with cpms_stub() as upstream:
        fixture = _app(tmp_path, monkeypatch=monkeypatch, upstream_base=upstream["base"])
        assert _delegate(fixture).status_code == 200
    claimed = _claim(fixture, _runs(tmp_path)[0]["run_id"]).json()
    assert set(claimed) == {
        "order_no",
        "status_code",
        "status_role",
        "observed_at",
        "source",
        "environment",
        "gateway_version",
        "actual_node_id",
        "run_state",
    }
    assert "plateNo" not in json.dumps(claimed) and "DO-NOT-LEAK" not in json.dumps(claimed)


# --------------------------------------------------------------------------
# 放行复核的两条缺口（M2-BC-PUBLIC-R1）
# --------------------------------------------------------------------------


def test_bc_r1_rebinding_expected_node_inside_the_final_authorization_is_refused(tmp_path, monkeypatch):
    """在最后一次授权里把配置的预期节点从 node-a1 改成 node-a2。

    旧实现只比对「出示凭据的那个节点」与记录的 ``expected_node_id``：两者都还是 node-a1，
    于是旧节点拿到 200 + 业务正文，尽管服务端此刻认为该记录属于 node-a2。
    修复后绑定复核包含**配置里的预期节点**，旧节点与新节点都不能拿旧 run 恢复结果。
    """
    fixture, run_id = _succeed_run(tmp_path, monkeypatch)
    config_path = Path(fixture["config"])

    def rebind() -> None:
        raw = json.loads(config_path.read_text())
        raw["expected_node_id"] = "node-a2"
        config_path.write_text(json.dumps(raw))
        config_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    response, hooks = _claim_with_mutation(fixture, run_id, rebind)
    assert len(hooks) == 1, f"钩子必须真的在**最后一次**授权回调里执行过，实际 {hooks}"
    assert response.status_code == 403, response.text
    assert "order_no" not in response.json()
    stored = fixture["store"].get(run_id)
    assert stored.state == FAILED and stored.reason == "binding_changed"

    # 旧节点：记录已是终态。新节点：它虽是「当前预期节点」，也不能把旧 run 恢复出来。
    assert _claim(fixture, run_id, token=NODE_A_TOKEN).status_code == 403
    assert _claim(fixture, run_id, token=NODE_A2_TOKEN).status_code == 403


def test_bc_r1_rebinding_expected_node_in_the_cached_success_path_is_refused(tmp_path, monkeypatch):
    """成功缓存路径同样要比对预期节点：先改绑定，再让同一节点重试。"""
    fixture, run_id = _succeed_run(tmp_path, monkeypatch)
    config_path = Path(fixture["config"])
    raw = json.loads(config_path.read_text())
    raw["expected_node_id"] = "node-a2"
    config_path.write_text(json.dumps(raw))
    config_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    response = _claim(fixture, run_id)
    assert response.status_code == 403, response.text
    assert "order_no" not in response.json()
    assert fixture["store"].get(run_id).reason == "binding_changed"


def test_bc_r1_denied_success_is_terminal_and_does_not_recover(tmp_path, monkeypatch):
    """授权变更让放行复核失败：记录进入终态，恢复授权后同一 run 仍拒绝且不读上游。

    旧实现调用 ``fail()``，它只作用于 issued/running —— 对 succeeded 记录什么也不做，
    于是记录仍是 succeeded，恢复授权后同一个 run 又返回 200。
    """
    fixture, run_id = _succeed_run(tmp_path, monkeypatch)
    upstream = fixture["_upstream"]
    fixture["orders"].ownership["ORDER-A"] = "USER-ELSEWHERE"

    denied = _claim(fixture, run_id)
    assert denied.status_code == 403, denied.text
    assert "order_no" not in denied.json()
    stored = fixture["store"].get(run_id)
    assert stored.state == FAILED and stored.reason == "order_not_visible"

    calls_after_denial = upstream["calls"]
    fixture["orders"].ownership["ORDER-A"] = "USER-A"

    retried = _claim(fixture, run_id)
    assert retried.status_code == 403, retried.text
    assert "order_no" not in retried.json()
    assert upstream["calls"] == calls_after_denial, "终态之后不得再读一次上游"


def test_bc_r1_unavailable_success_is_terminal_and_does_not_recover(tmp_path, monkeypatch):
    """授权源故障：第一次是 503（可重试语义不变），记录同时进入终态；恢复后仍拒绝。"""
    fixture, run_id = _succeed_run(tmp_path, monkeypatch)
    upstream = fixture["_upstream"]
    fixture["orders"].unavailable = True

    denied = _claim(fixture, run_id)
    assert denied.status_code == 503, denied.text
    assert denied.json()["error"]["retryable"] is True
    stored = fixture["store"].get(run_id)
    assert stored.state == FAILED and stored.reason == "authorization_unavailable"

    calls_after_denial = upstream["calls"]
    fixture["orders"].unavailable = False

    retried = _claim(fixture, run_id)
    assert retried.status_code == 403, retried.text
    assert "order_no" not in retried.json()
    assert upstream["calls"] == calls_after_denial


def test_bc_r1_wrong_credential_cannot_terminate_someone_elses_run(tmp_path, monkeypatch):
    """拒绝要钉住的是**自己领取的那条记录**；拿错凭据的请求连记录都认不出来，不能造成终态。

    这条防的是把「任何错误凭据对任意 run 的请求」做成撤销：那等于让一个无权请求
    破坏别人的授权记录。
    """
    fixture, run_id = _succeed_run(tmp_path, monkeypatch)
    assert _claim(fixture, run_id, token="not-a-registered-node").status_code == 401
    assert _claim(fixture, run_id, token=NODE_A2_TOKEN).status_code == 403  # 兄弟节点也不行
    stored = fixture["store"].get(run_id)
    assert stored.state == SUCCEEDED and stored.snapshot is not None


@pytest.mark.parametrize("unavailable", [False, True])
def test_authorization_denial_is_terminal_when_another_claim_completes(tmp_path, monkeypatch, unavailable):
    fixture = _app(tmp_path, monkeypatch=monkeypatch)
    fixture["dispatcher"].claim = None
    assert _delegate(fixture).status_code == 502
    run_id = _runs(tmp_path)[0]["run_id"]
    store = fixture["store"]
    assert store.claim(run_id)
    original = fixture["orders"].can_access
    completed = []

    def finish_then_deny(scope, order):
        completed.append(
            store.complete(
                run_id, snapshot={"order_no": "ORDER-A"}, actual_node_id="node-a1", node_build="synthetic"
            )
        )
        if unavailable:
            raise CallerAuthError("unavailable", code="caller_auth.unavailable")
        return False

    monkeypatch.setattr(fixture["orders"], "can_access", finish_then_deny)
    denied = _claim(fixture, run_id)
    assert completed == [True]
    assert denied.status_code == (503 if unavailable else 403)
    assert store.get(run_id).state == FAILED
    monkeypatch.setattr(fixture["orders"], "can_access", original)
    retried = _claim(fixture, run_id)
    assert retried.status_code == 403 and "order_no" not in retried.json()
