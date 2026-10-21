"""`ensure-workspace`：幂等、可恢复、**先占位后建**（#880）。

四条判据，每条对着一种"看起来做了、其实造了孤儿"：

1. **重跑是幂等的**：`created`/`ready` 的租户一条 POST 都不发。
2. **并发只建一个**：唯一键 + 先插入把两个并发运行串行化，输的那个拿到 `CREATING`。
3. **明确失败可重建**：收到错误响应 ⇒ `failed`（`workspace_id` 空），重跑建得出来。
4. **结果未知先对账**：超时 ⇒ `unknown`；下一次按名字查 `all-workspaces` —— 命中 1 个
   补 UUID（**不重建**）、命中 0 个才重建、命中 >1 个拒绝并要人工。

变异对照写在每条的 docstring 里；把 `claim` 换成"有行就退"、或把对账拿掉，对应用例变红。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from aiops_diagnostics.dify_ensure_workspace import (
    PLATFORM_OWNER_EMAIL,
    EnsureError,
    ensure_workspace,
    reconcile_by_name,
)
from aiops_diagnostics.dify_inner_api import DifyInnerApi, InnerApiError, InnerApiSettings
from aiops_diagnostics.dify_workspaces import (
    DIFY_WORKSPACE_CREATING,
    DifyWorkspaceError,
    DifyWorkspaces,
    workspace_name,
)

TENANT = "1783000000000000001"
ENVIRONMENT = "41"
CONSOLE_KEY = "console-key-0123456789"
INNER_KEY = "inner-key-0123456789"


class _Upstream:
    """同时扮演 inner_api 与 console 的极简上游，记录它被怎么问的。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.workspaces: list[dict[str, str]] = []
        self.create_status = 200
        self.create_body: Any = None
        self.create_delay = 0.0
        self.base = ""

    def handler(self) -> type[BaseHTTPRequestHandler]:
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                upstream.calls.append({"method": "GET", "path": self.path, "body": None})
                if self.path.startswith("/console/api/all-workspaces"):
                    upstream._reply(
                        self,
                        200,
                        {"data": upstream.workspaces, "has_more": False, "page": 1, "limit": 100},
                    )
                else:
                    upstream._reply(self, 404, {"code": "not_found"})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                upstream.calls.append({"method": "POST", "path": self.path, "body": body})
                if upstream.create_delay:
                    import time

                    time.sleep(upstream.create_delay)
                if self.path == "/inner/api/enterprise/workspace":
                    if upstream.create_body is not None:
                        upstream._reply(self, upstream.create_status, upstream.create_body)
                        return
                    workspace_id = f"ws-{len(upstream.workspaces) + 1}"
                    upstream.workspaces.append({"id": workspace_id, "name": body["name"]})
                    upstream._reply(
                        self,
                        200,
                        {
                            "message": "enterprise workspace created.",
                            "tenant": {"id": workspace_id, "name": body["name"]},
                        },
                    )
                    return
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

    def posts(self, path: str = "/inner/api/enterprise/workspace") -> list[dict[str, Any]]:
        return [call for call in self.calls if call["method"] == "POST" and call["path"] == path]


@pytest.fixture()
def upstream():
    state = _Upstream()
    server = ThreadingHTTPServer(("127.0.0.1", 0), state.handler())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    state.base = f"http://{host}:{port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def _run(tmp_path: Path, upstream: _Upstream, **overrides: Any):
    workspaces = DifyWorkspaces(tmp_path / "gateway.db")
    inner = DifyInnerApi(InnerApiSettings(base_url=upstream.base, api_key=INNER_KEY))
    kwargs: dict[str, Any] = {
        "tenant_id": TENANT,
        "environment": ENVIRONMENT,
        "workspaces": workspaces,
        "inner": inner,
        "console_base_url": upstream.base,
        "console_api_key": CONSOLE_KEY,
        "owner": "test:1",
    }
    kwargs.update(overrides)
    return ensure_workspace(**kwargs), workspaces


# ── 1. 建成 + 幂等 ───────────────────────────────────────────────────────


def test_it_creates_one_workspace_with_the_derived_name(tmp_path: Path, upstream: _Upstream) -> None:
    outcome, workspaces = _run(tmp_path, upstream)

    assert outcome.state == "created"
    assert outcome.workspace_id == "ws-1"
    assert workspaces.resolve(TENANT, ENVIRONMENT) == "ws-1"
    # 名字是派生的，owner 是平台服务身份 —— 两者都不能由调用方自选。
    post = upstream.posts()[0]
    assert post["body"]["name"] == workspace_name(ENVIRONMENT, TENANT) == f"aiops-41-{TENANT}"
    assert post["body"]["owner_email"] == PLATFORM_OWNER_EMAIL


def test_a_second_run_sends_no_post_at_all(tmp_path: Path, upstream: _Upstream) -> None:
    """幂等：`created` 的租户重跑一条写都不发。

    变异对照：把 `claim` 换成"不看状态一律重走"，本用例变红（posts 变成 2 条，
    而 Dify 没有删 workspace 的 API —— 第二个就是永久孤儿）。
    """
    _run(tmp_path, upstream)
    before = len(upstream.posts())

    outcome, _ = _run(tmp_path, upstream)

    assert outcome.state == "created"
    assert outcome.workspace_id == "ws-1"
    assert len(upstream.posts()) == before == 1


def test_a_run_that_finds_an_active_lease_reports_creating(tmp_path: Path, upstream: _Upstream) -> None:
    """``creating`` 且租约未过期 ⇒ 不建，报"有人在建"（不重叠、不抢）。"""
    workspaces = DifyWorkspaces(tmp_path / "gateway.db")
    assert workspaces.claim(TENANT, ENVIRONMENT, owner="other:1", lease_seconds=300)

    outcome, _ = _run(tmp_path, upstream, workspaces=workspaces)

    assert outcome.state == "creating"
    assert upstream.posts() == [], "别人正在建时不该发任何 POST"


# ── 2. 并发只建一个 ─────────────────────────────────────────────────────


def test_two_concurrent_runs_create_exactly_one_workspace(tmp_path: Path, upstream: _Upstream) -> None:
    """**唯一键 + 先插入**是唯一能串行化的顺序。

    变异对照：把 `claim` 改成"先查有没有行、没有就建、建完再写"（先建后写），两个线程
    都会越过检查 → 两个 POST、Dify 里两个 workspace、映射里只有一个 → 另一个是永久孤儿。
    """
    upstream.create_delay = 0.3
    results: list[Any] = []
    errors: list[BaseException] = []

    def run(owner: str) -> None:
        try:
            outcome, _ = _run(tmp_path, upstream, owner=owner)
            results.append(outcome)
        except BaseException as exc:  # noqa: BLE001 - 测试要把失败带回来
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(f"t:{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    assert len(upstream.posts()) == 1, "只有一个运行能拿到创建权"
    assert sorted(result.state for result in results) == ["created", "creating"]


# ── 3. 明确失败 ⇒ failed，可重建 ────────────────────────────────────────


def test_an_error_response_is_failed_and_a_rerun_can_still_create(
    tmp_path: Path, upstream: _Upstream
) -> None:
    upstream.create_status = 500
    upstream.create_body = {"message": "boom"}

    with pytest.raises(EnsureError):
        _run(tmp_path, upstream)
    workspaces = DifyWorkspaces(tmp_path / "gateway.db")
    row = workspaces.row(TENANT, ENVIRONMENT) or {}
    assert row["setup_state"] == "failed"
    assert row["workspace_id"] == "", "失败时 workspace_id 必须保持空 —— 空才有重建的余地"

    upstream.create_status = 200
    upstream.create_body = None
    outcome, _ = _run(tmp_path, upstream)
    assert outcome.state == "created"
    assert workspaces.resolve(TENANT, ENVIRONMENT) == "ws-1"


# ── 4. 结果未知 ⇒ 先对账，不重建 ────────────────────────────────────────


def test_a_timeout_is_unknown_never_failed(tmp_path: Path, upstream: _Upstream) -> None:
    """超时/读中断 ≠ 失败：请求可能已经到了 Dify，重试会造孤儿。

    这里让上游睡过客户端超时，断言落 `unknown`（而不是 `failed`）。
    """
    upstream.create_delay = 1.5
    workspaces = DifyWorkspaces(tmp_path / "gateway.db")
    inner = DifyInnerApi(InnerApiSettings(base_url=upstream.base, api_key=INNER_KEY, timeout=0.4))

    with pytest.raises(EnsureError):
        ensure_workspace(
            tenant_id=TENANT,
            environment=ENVIRONMENT,
            workspaces=workspaces,
            inner=inner,
            console_base_url=upstream.base,
            console_api_key=CONSOLE_KEY,
            owner="t:1",
        )

    row = workspaces.row(TENANT, ENVIRONMENT) or {}
    assert row["setup_state"] == "unknown"
    with pytest.raises(DifyWorkspaceError) as caught:
        workspaces.resolve(TENANT, ENVIRONMENT)
    assert caught.value.code == DIFY_WORKSPACE_CREATING or "UNKNOWN" in caught.value.code


def test_an_unknown_row_is_reconciled_by_name_instead_of_rebuilt(tmp_path: Path, upstream: _Upstream) -> None:
    """对账命中 1 个 ⇒ **补 UUID，不重建**（这一条就是"不造孤儿"的判据）。"""
    workspaces = DifyWorkspaces(tmp_path / "gateway.db")
    workspaces.claim(TENANT, ENVIRONMENT, owner="crashed:1", lease_seconds=1)
    workspaces.mark_unknown(TENANT, ENVIRONMENT, error="timeout")
    # Dify 那边其实建成了（崩溃前发出的请求已经到达）。
    upstream.workspaces.append({"id": "ws-recovered", "name": workspace_name(ENVIRONMENT, TENANT)})

    outcome, _ = _run(tmp_path, upstream, workspaces=workspaces)

    assert outcome.workspace_id == "ws-recovered"
    assert outcome.state == "created"
    assert upstream.posts() == [], "对账命中时必须一条 POST 都不发"


def test_zero_matches_at_reconcile_means_a_rebuild_is_safe(tmp_path: Path, upstream: _Upstream) -> None:
    workspaces = DifyWorkspaces(tmp_path / "gateway.db")
    workspaces.claim(TENANT, ENVIRONMENT, owner="crashed:1", lease_seconds=1)
    workspaces.mark_unknown(TENANT, ENVIRONMENT, error="timeout")

    outcome, _ = _run(tmp_path, upstream, workspaces=workspaces)

    assert outcome.workspace_id == "ws-1"
    assert len(upstream.posts()) == 1


def test_two_matches_at_reconcile_refuses_and_asks_a_human(tmp_path: Path, upstream: _Upstream) -> None:
    """命中 >1 个 ⇒ 拒绝。``tenants.name`` 没有唯一约束，自动选一个就是串租户。"""
    workspaces = DifyWorkspaces(tmp_path / "gateway.db")
    workspaces.claim(TENANT, ENVIRONMENT, owner="crashed:1", lease_seconds=1)
    workspaces.mark_unknown(TENANT, ENVIRONMENT, error="timeout")
    name = workspace_name(ENVIRONMENT, TENANT)
    upstream.workspaces.extend([{"id": "ws-a", "name": name}, {"id": "ws-b", "name": name}])

    with pytest.raises(EnsureError) as caught:
        _run(tmp_path, upstream, workspaces=workspaces)
    assert caught.value.code == "DIFY_WORKSPACE_AMBIGUOUS"
    assert upstream.posts() == []


# ── 5. inner_api 的两侧分类 ─────────────────────────────────────────────


def test_a_refused_connection_is_a_definite_failure_not_an_unknown() -> None:
    """连接被拒 ⇒ Dify 没收到请求 ⇒ 可以重建。与超时的后果相反。"""
    inner = DifyInnerApi(InnerApiSettings(base_url="http://127.0.0.1:9", api_key=INNER_KEY, timeout=1))

    with pytest.raises(InnerApiError) as caught:
        inner.create_workspace("x", "y@z.invalid")
    assert caught.value.outcome == "failed"


def test_a_200_without_a_tenant_id_is_unknown(tmp_path: Path, upstream: _Upstream) -> None:
    upstream.create_body = {"message": "ok"}
    workspaces = DifyWorkspaces(tmp_path / "gateway.db")
    inner = DifyInnerApi(InnerApiSettings(base_url=upstream.base, api_key=INNER_KEY))

    with pytest.raises(EnsureError):
        ensure_workspace(
            tenant_id=TENANT,
            environment=ENVIRONMENT,
            workspaces=workspaces,
            inner=inner,
            console_base_url=upstream.base,
            console_api_key=CONSOLE_KEY,
            owner="t:1",
        )
    assert (workspaces.row(TENANT, ENVIRONMENT) or {})["setup_state"] == "unknown"


def test_reconcile_sends_the_console_key_and_matches_exactly(tmp_path: Path, upstream: _Upstream) -> None:
    """对账按**派生名精确匹配**：名字只差一点的不能算命中。"""
    name = workspace_name(ENVIRONMENT, TENANT)
    upstream.workspaces.extend([{"id": "ws-exact", "name": name}, {"id": "ws-near", "name": name + "-old"}])

    matches = reconcile_by_name(console_base_url=upstream.base, console_api_key=CONSOLE_KEY, name=name)

    assert [row["id"] for row in matches] == ["ws-exact"]
