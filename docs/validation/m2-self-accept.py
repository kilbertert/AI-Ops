#!/usr/bin/env python3
"""独立验收：不 import 候选树的测试文件，自己起 app、自己造替身、自己判。

这是 Codex 在本仓的验收思路：读候选的实现，用**另一份**驱动代码去证同样的判据。
它不复用 `tests/execution/test_m2_close_contract.py` 的任何 helper —— 复用会把「测试和实现
一起错」的可能性原样带进验收。

用法（从候选树根，用该树的解释器）：
  <candidate>/.venv/bin/python /path/to/self_accept.py --repo <candidate>

退出码 0 = 全部判据成立；1 = 有判据不成立（逐条打印）；2 = 环境不足。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import stat
import sys
import tempfile
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--repo", required=True, type=Path)
parser.add_argument("--out", type=Path)
args = parser.parse_args()
REPO = args.repo.resolve()
sys.path.insert(0, str(REPO / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from aiops_diagnostics.execution.agent_runs import AgentRunStore  # noqa: E402
from aiops_diagnostics.gateway_api import create_gateway_app  # noqa: E402
from aiops_diagnostics.gateway_config import GatewayServerSettings  # noqa: E402
from aiops_diagnostics.gateway_store import GatewayStore  # noqa: E402
from aiops_diagnostics.identity.caller_auth import CallerAuthError  # noqa: E402
from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord  # noqa: E402

NODE_1 = "accept-node-1"
NODE_2 = "accept-node-2"
TOKEN_1 = "accept-token-1-0123456789"
TOKEN_2 = "accept-token-2-0123456789"
CALLER = "accept-caller-token"
TENANT = "TENANT-ACC"
USER = "USER-ACC"
HEADERS = {"Authorization": f"Bearer {CALLER}", "X-Business-Entry": "consumer"}
DELEGATE = "/v1/orders/{order}/charging-status/delegated"
CLAIM = "/v1/internal/charging-status/claims/{run}"

checks: list[dict[str, object]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": detail})


def scope() -> ScopeContext:
    subject = SubjectRecord(b_user_id="", c_user_id=USER, tenant_id=TENANT)
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id=TENANT,
        data_scope=DataScope(type="self"),
        roles=frozenset(),
        permissions=frozenset({"aiops:orders:read"}),
        resolved_at=datetime(2026, 10, 18, tzinfo=UTC),
    )


class Resolver:
    def resolve(self, token: str, *, required_scope: str, **_kwargs):
        if token != CALLER:
            raise CallerAuthError("invalid", code="caller_auth.invalid")
        return scope()


class Orders:
    """订单授权替身：`owner` 可变，`down` 可置位。判据与真授权器同一条。"""

    def __init__(self) -> None:
        self.owner = {"ORDER-1": USER}
        self.down = False
        self.allow_calls = 0
        self.hook_at: int | None = None
        self.hook = None
        self.hook_fired = False

    def can_access(self, context: ScopeContext, order_no: str) -> bool:
        if self.down:
            raise CallerAuthError("unavailable", code="caller_auth.unavailable")
        allowed = context.data_scope.type == "self" and self.owner.get(order_no) == USER
        if allowed:
            self.allow_calls += 1
            if self.hook_at == self.allow_calls:
                self.hook_fired = True
                assert self.hook is not None
                self.hook()
        return allowed


class Runtime:
    def shutdown(self) -> None:
        pass


def cpms() -> tuple[HTTPServer, dict]:
    state = {"calls": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a: object) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            prefix = "/pile/chOrderInfo/chargingInfo/"
            order = self.path[len(prefix) :] if self.path.startswith(prefix) else ""
            if order == "ORDER-1":
                state["calls"] += 1
                payload = {
                    "code": 0,
                    "msg": "ok",
                    "data": {"orderNo": order, "status": "0", "plateNo": "NOPE", "userId": "NOPE"},
                }
            else:
                payload = {"code": 1, "msg": "missing", "data": None}
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["base"] = f"http://127.0.0.1:{server.server_port}/pile"
    return server, state


def config_file(root: Path, expected_node: str = NODE_1) -> Path:
    path = root / "agent-runs.json"
    path.write_text(
        json.dumps(
            {
                "environment": "env-acc",
                "snail_base_url": "http://127.0.0.1:1",
                "snail_app_id": "accept-app",
                "snail_app_token": "accept-app-token",
                "snail_agent_id": 3,
                "snail_open_id": "accept-open",
                "expected_node_id": expected_node,
                "nodes": [
                    {
                        "node_id": NODE_1,
                        "environment": "env-acc",
                        "token_sha256": hashlib.sha256(TOKEN_1.encode()).hexdigest(),
                    },
                    {
                        "node_id": NODE_2,
                        "environment": "env-acc",
                        "token_sha256": hashlib.sha256(TOKEN_2.encode()).hexdigest(),
                    },
                ],
            }
        )
    )
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


class Dispatcher:
    def __init__(self) -> None:
        self.client: TestClient | None = None

    def dispatch(self, config, run_id: str) -> str:
        # 平台回话，但**不让**节点领 —— 由测试自己决定何时领取，时序完全可控。
        return "ok"


def build(root: Path, upstream: str):
    cfg = config_file(root)
    prod = root / "production.env"
    prod.write_text("# synthetic acceptance\n")
    prod.chmod(0o600)
    settings = GatewayServerSettings(
        data_home=root,
        database_file=root / "gateway.db",
        server_config_file=prod,
        charging_status_base_url=upstream,
        execution_environment="env-acc",
        agent_run_config_file=str(cfg),
    )
    orders = Orders()
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=Runtime(),
        caller_resolver=Resolver(),
        order_authorizer=orders,
    )
    dispatcher = Dispatcher()
    vendor = sys.modules["aiops_diagnostics.gateway_api"]
    original = vendor.SnailDispatcher
    vendor.SnailDispatcher = lambda: dispatcher
    return TestClient(app), orders, cfg, app, original


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    server, state = cpms()
    root = Path(tempfile.mkdtemp(prefix="m2-self-accept-"))
    client, orders, cfg, app, original = build(root, state["base"])
    store: AgentRunStore = app.state.gateway.agent_run_store
    try:
        # ---------- 判据 1：未配置绑定表时两个入口都不存在 ----------
        prod = root / "production.env"
        bare = GatewayServerSettings(
            data_home=root / "x",
            database_file=root / "x/g.db",
            server_config_file=prod,
            charging_status_base_url=state["base"],
            execution_environment="env-acc",
        )
        bare_app = create_gateway_app(
            settings=bare,
            store=GatewayStore(bare.database_file),
            runtime=Runtime(),
            caller_resolver=Resolver(),
            order_authorizer=Orders(),
        )
        bare_client = TestClient(bare_app)
        r = bare_client.post(DELEGATE.format(order="ORDER-1"), headers=HEADERS, json={})
        check("默认关闭：未配置绑定表时委派入口 404", r.status_code == 404, f"status={r.status_code}")

        # ---------- 判据 2：一次正常委托 + 领取 ----------
        r = client.post(DELEGATE.format(order="ORDER-1"), headers=HEADERS, json={})
        runs = [x for x in _all(store)]
        check("委托入口在 502 之前确实建了记录", len(runs) == 1, f"records={len(runs)}")
        run_id = runs[0]["run_id"]
        claim = client.post(CLAIM.format(run=run_id), headers={"Authorization": f"Bearer {TOKEN_1}"}, json={})
        check("节点领取成功", claim.status_code == 200, f"status={claim.status_code}")
        expected_fields = {
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
        check(
            "领取回执的七个字段 + 位置",
            claim.status_code == 200 and set(claim.json()) == expected_fields,
            str(sorted(claim.json())) if claim.status_code == 200 else "",
        )
        check("敏感字段被裁掉", "NOPE" not in claim.text, "")

        # ---------- 判据 3：最后授权期间切换预期节点，旧节点不得放行 ----------
        # 造第二条成功记录
        client.post(DELEGATE.format(order="ORDER-1"), headers=HEADERS, json={})
        second = [x for x in _all(store) if x["run_id"] != run_id][0]["run_id"]
        c2 = client.post(CLAIM.format(run=second), headers={"Authorization": f"Bearer {TOKEN_1}"}, json={})
        check("第二条记录先成功", c2.status_code == 200, f"status={c2.status_code}")

        # 已成功记录的领取路径只有一次放行回调：钩子挂在「下一次」。
        def rebind() -> None:
            raw = json.loads(cfg.read_text())
            raw["expected_node_id"] = NODE_2
            cfg.write_text(json.dumps(raw))
            cfg.chmod(stat.S_IRUSR | stat.S_IWUSR)

        orders.hook_at = orders.allow_calls + 1
        orders.hook = rebind
        rebound = client.post(
            CLAIM.format(run=second), headers={"Authorization": f"Bearer {TOKEN_1}"}, json={}
        )
        check("钩子确实在最后一次授权里触发", orders.hook_fired, f"allow_calls={orders.allow_calls}")
        check("最后授权期间切换预期节点 ⇒ 拒绝", rebound.status_code == 403, f"status={rebound.status_code}")
        check("拒绝时不含 order_no", "order_no" not in rebound.json(), json.dumps(rebound.json())[:120])
        stored = store.get(second)
        check(
            "记录被钉成 failed/binding_changed",
            stored.state == "failed" and stored.reason == "binding_changed",
            f"{stored.state}/{stored.reason}",
        )
        after = client.post(CLAIM.format(run=second), headers={"Authorization": f"Bearer {TOKEN_1}"}, json={})
        check("旧节点不能再用旧 run 恢复", after.status_code == 403, f"status={after.status_code}")
        other = client.post(CLAIM.format(run=second), headers={"Authorization": f"Bearer {TOKEN_2}"}, json={})
        check("新预期节点也不能把旧 run 恢复出来", other.status_code == 403, f"status={other.status_code}")

        # 复原绑定：上一条判据故意把预期节点改成了 NODE_2；后续判据要回到 NODE_1。
        config_file(root, NODE_1)

        # ---------- 判据 4：授权拒绝后记录终态，恢复也不释放 ----------
        client.post(DELEGATE.format(order="ORDER-1"), headers=HEADERS, json={})
        fresh = max(_all(store), key=lambda x: x["created_at"])["run_id"]
        ok = client.post(CLAIM.format(run=fresh), headers={"Authorization": f"Bearer {TOKEN_1}"}, json={})
        check("新的成功记录可领取", ok.status_code == 200, f"status={ok.status_code}")
        orders.owner["ORDER-1"] = "SOMEONE-ELSE"
        denied = client.post(CLAIM.format(run=fresh), headers={"Authorization": f"Bearer {TOKEN_1}"}, json={})
        check("授权变更 ⇒ 403", denied.status_code == 403, f"status={denied.status_code}")
        st = store.get(fresh)
        check(
            "拒绝后记录进入终态 failed/order_not_visible",
            st.state == "failed" and st.reason == "order_not_visible",
            f"{st.state}/{st.reason}",
        )
        calls_before = state["calls"]
        orders.owner["ORDER-1"] = USER
        again = client.post(CLAIM.format(run=fresh), headers={"Authorization": f"Bearer {TOKEN_1}"}, json={})
        check("恢复授权后同一 run 仍拒绝", again.status_code == 403, f"status={again.status_code}")
        check("恢复后不再读一次上游", state["calls"] == calls_before, f"{calls_before} -> {state['calls']}")

        # ---------- 判据 5：授权源故障同样进终态 ----------
        client.post(DELEGATE.format(order="ORDER-1"), headers=HEADERS, json={})
        third = max(_all(store), key=lambda x: x["created_at"])["run_id"]
        client.post(CLAIM.format(run=third), headers={"Authorization": f"Bearer {TOKEN_1}"}, json={})
        orders.down = True
        outage = client.post(CLAIM.format(run=third), headers={"Authorization": f"Bearer {TOKEN_1}"}, json={})
        check(
            "授权源故障 ⇒ 503 且可重试",
            outage.status_code == 503 and outage.json()["error"]["retryable"] is True,
            f"status={outage.status_code}",
        )
        st = store.get(third)
        check(
            "503 同时进入终态",
            st.state == "failed" and st.reason == "authorization_unavailable",
            f"{st.state}/{st.reason}",
        )
        orders.down = False
        back = client.post(CLAIM.format(run=third), headers={"Authorization": f"Bearer {TOKEN_1}"}, json={})
        check("授权源恢复后仍拒绝", back.status_code == 403, f"status={back.status_code}")

        # ---------- 判据 6：错凭据不破坏别人的记录 ----------
        client.post(DELEGATE.format(order="ORDER-1"), headers=HEADERS, json={})
        fourth = max(_all(store), key=lambda x: x["created_at"])["run_id"]
        client.post(CLAIM.format(run=fourth), headers={"Authorization": f"Bearer {TOKEN_1}"}, json={})
        bad = client.post(CLAIM.format(run=fourth), headers={"Authorization": "Bearer nope"}, json={})
        check("未登记凭据 ⇒ 401", bad.status_code == 401, f"status={bad.status_code}")
        sibling = client.post(
            CLAIM.format(run=fourth), headers={"Authorization": f"Bearer {TOKEN_2}"}, json={}
        )
        check("兄弟节点 ⇒ 403", sibling.status_code == 403, f"status={sibling.status_code}")
        st = store.get(fourth)
        check("别人的请求没有把记录改成终态", st.state == "succeeded" and st.snapshot is not None, st.state)

        # ---------- 判据 7：请求体不能覆盖任何绑定 ----------
        tampered = client.post(
            DELEGATE.format(order="ORDER-1"), headers=HEADERS, json={"expected_node_id": NODE_2}
        )
        check("委派入口多字段 ⇒ 422", tampered.status_code == 422, f"status={tampered.status_code}")
        tampered_claim = client.post(
            CLAIM.format(run=fourth),
            headers={"Authorization": f"Bearer {TOKEN_1}"},
            json={"order_no": "ORDER-1"},
        )
        check(
            "领取入口多字段 ⇒ 422", tampered_claim.status_code == 422, f"status={tampered_claim.status_code}"
        )

        # ---------- 判据 8：no-store 作用面 ----------
        ok_headers = {k.lower(): v for k, v in ok.headers.items()}
        check(
            "成功响应带 no-store",
            ok_headers.get("cache-control") == "no-store",
            str(ok_headers.get("cache-control")),
        )
        err_headers = {k.lower(): v for k, v in denied.headers.items()}
        check(
            "错误响应也带 no-store",
            err_headers.get("cache-control") == "no-store",
            str(err_headers.get("cache-control")),
        )
        health = client.get("/health")
        check("/health 不带 no-store", "cache-control" not in {k.lower() for k in health.headers}, "")
    except Exception as exc:  # noqa: BLE001
        check(f"执行期间异常: {type(exc).__name__}", False, str(exc))
    finally:
        sys.modules["aiops_diagnostics.gateway_api"].SnailDispatcher = original
        server.shutdown()

    status = "PASS" if all(c["ok"] for c in checks) else "FAIL"
    report = {
        "kind": "m2_self_acceptance",
        # 公开报告里不写开发机绝对路径：只留目录名。
        "repo": REPO.name,
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "status": status,
        "checks": checks,
        "code_identity": {
            f: hashlib.sha256((REPO / f).read_bytes()).hexdigest()
            for f in (
                "src/aiops_diagnostics/gateway_api.py",
                "src/aiops_diagnostics/execution/agent_runs.py",
                "src/aiops_diagnostics/gateway_config.py",
            )
        },
    }
    if args.out:
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"{status}: {len(checks)} 项")
    for c in checks:
        if not c["ok"]:
            print(f"  - {c['name']}: {c['detail']}", file=sys.stderr)
    return 0 if status == "PASS" else 1


def _all(store: AgentRunStore) -> list[dict]:
    with store._connection() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM agent_runs").fetchall()]


if __name__ == "__main__":
    sys.exit(main())
