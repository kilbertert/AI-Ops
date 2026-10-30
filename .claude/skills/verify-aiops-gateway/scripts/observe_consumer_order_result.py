#!/usr/bin/env python3
"""在一次性数据根上跑**真实** `aiops-gateway serve`，经 TCP 驱动本片观察场景。

`TestClient` 通过不算数：这里起真实进程、真实监听、真实 HTTP，断言的是
**服务实际写出的日志行**（stderr 落到文件后逐行解析）。

## 什么是真的、什么是合成的（必须分清）

| 部分 | 本次运行 |
|---|---|
| 进程、监听、HTTP、日志权重与格式 | **真**（`aiops-gateway serve`） |
| 身份解析 | **真** `IntrospectionCallerResolver`，打回环 introspection 桩 |
| 平台判定 / 502 / 403 / 404 判定顺序 | **真** |
| 被测的观察实现 | **真**（`request_observation`） |
| 订单行、平台目录、CPMS | 回环合成 |

订单行在真环境来自 MySQL，平台目录来自 MySQL 的 UPMS 库，CPMS 是外部服务 ——
这三样在验证机上都不该连接。替换它们只改变「业务数据从哪来」，不替换身份解析、
不替换判定顺序、不替换观察实现；被替换的三者都不是本片要证明的东西。
证据文件里原样写清这一点，不用"本地通过"冒充真实业务验收。

    uv run python .claude/skills/verify-aiops-gateway/scripts/observe_consumer_order_result.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

GATEWAY_PORT = 8793
INTROSPECT_PORT = 8794
CPMS_PORT = 8795
CONTROL_PORT = 8796
AUDIENCE = "aiops-api"  # GatewayServerSettings.standard_api_audience default
CLIENT_ID = "verify-client"
CLIENT_SECRET = "verify-secret-0123456789abcdef"
TOKEN_OK = "verify-consumer-token"
TOKEN_NO_READ = "verify-no-orders-read-token"
# 首次解析（aiops:diagnoses:write）成功，分支内再次解析（aiops:orders:read）时凭据已失效。
# 这是生产可达的形状，不是构造边界；401 必须算拒绝，不能算服务故障。
TOKEN_EXPIRES_ON_RECHECK = "verify-expires-on-recheck-token"
TENANT = "tenant-verify"
ORDER = "2096164064667852801"
STRANGER = "2096164064667852802"
QUESTION = "查询我最新一单的状态"

RESULT_PREFIX = "assistant_question_result "

#: Six scenarios produce a line; the concurrent four produce four more; the
#: ordinary question and the unauthenticated request produce none.
EXPECTED_LINES = 11


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


class _Introspection(BaseHTTPRequestHandler):
    """OAuth2 introspection stub — the exact shape `IntrospectionCallerResolver` reads."""

    seen: dict[str, int] = {}
    lock = threading.Lock()

    def log_message(self, *_args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        token = ""
        for part in body.split("&"):
            key, _, value = part.partition("=")
            if key == "token":
                token = urllib.parse.unquote(value)
        scopes = ["aiops:diagnoses:write"]
        if token != TOKEN_NO_READ:
            scopes.append("aiops:orders:read")
        # 那个"再次解析时失效"的令牌：第一次 POST 有效，之后的都无效。
        # 计数放在**这个进程**里，而不是写在令牌字符串上，因为真实世界里变的是
        # 会话状态，不是令牌字面量。
        active = token in {TOKEN_OK, TOKEN_NO_READ}
        if token == TOKEN_EXPIRES_ON_RECHECK:
            with _Introspection.lock:
                seen = _Introspection.seen.get(token, 0)
                _Introspection.seen[token] = seen + 1
            active = seen == 0
        payload = {
            "active": active,
            "sub": "b-user-1",
            "c_user_id": "c-user-1",
            "tenant_id": TENANT,
            "aud": [AUDIENCE],
            "scope": " ".join(scopes),
            "exp": time.time() + 600,
            "data_scope": {"type": "self"},
            "roles": ["consumer"],
        }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _Cpms(BaseHTTPRequestHandler):
    """`/chOrderInfo/chargingInfo/<order>` — what `read_charging_status` calls."""

    wrong_order = False

    def log_message(self, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        served = STRANGER if _Cpms.wrong_order else ORDER
        data = json.dumps(
            {"code": 0, "data": {"orderNo": served, "status": "1", "plateNo": "PRIVATE-PLATE"}}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _Control(BaseHTTPRequestHandler):
    """Flips the in-gateway stubs. Kept off the CPMS port so a scenario switch can
    never be mistaken for a business call."""

    state: dict[str, object] = {"unowned": False, "boom": False}

    def log_message(self, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        key = self.path.strip("/")
        value = self.state.get(key)
        data = json.dumps({"key": key, "value": value}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        _Control.state.update(payload)
        self.do_GET()


def write_stubs(root: Path) -> Path:
    """`sitecustomize` that swaps exactly three external data sources.

    Order rows, the UPMS platform directory and the order authorizer's source all
    live in a MySQL this host must not reach. Each is replaced by a stub that
    answers from the control endpoint above. Nothing else is patched: identity,
    routing order, every refusal and the observation module are the real code.
    """
    directory = root / "sitecustomize_dir"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sitecustomize.py").write_text(
        f"""
import contextlib
import json
import urllib.request

ORDER = {ORDER!r}
STRANGER = {STRANGER!r}
CONTROL = "http://127.0.0.1:{CONTROL_PORT}"


def _state():
    try:
        with urllib.request.urlopen(CONTROL + "/unowned", timeout=5) as response:
            return json.loads(response.read())
    except Exception:
        return {{"value": False}}


def _boom():
    try:
        with urllib.request.urlopen(CONTROL + "/boom", timeout=5) as response:
            return bool(json.loads(response.read()).get("value"))
    except Exception:
        return False


class _Orders:
    def list_orders(self, **_kwargs):
        if _boom():
            raise RuntimeError("synthetic order source exploded")
        if _state().get("value"):
            return []
        return [{{"order_no": ORDER}}]

    def get_orders(self, order_no, tenant_id=None):
        if _boom():
            raise RuntimeError("synthetic order source exploded")
        if _state().get("value") or str(order_no) != ORDER:
            return []
        return [{{"order_no": ORDER}}]


@contextlib.contextmanager
def _scoped_mysql_source(_settings, _scope):
    yield _Orders()


import aiops_diagnostics.gateway.gateway_api as _api
_api.scoped_mysql_source = _scoped_mysql_source


# The authorizer asks the same synthetic source, so "belongs to the caller" and
# "the latest order" cannot disagree.
import aiops_diagnostics.identity.caller_auth as _auth


class _Authorizer:
    def can_access(self, context, order_no):
        del context
        return not _state().get("value") and str(order_no) == ORDER


_auth.ScopedOrderAuthorizer = lambda *_a, **_k: _Authorizer()
_api.ScopedOrderAuthorizer = lambda *_a, **_k: _Authorizer()


# UPMS is a MySQL database; on this host it is a stub with the one answer these
# scenarios need (a consumer identity that may use the consumer platform).
import aiops_diagnostics.conversation.faq as _faq


class _Directory:
    def roles_for_c_user(self, c_user_id, tenant_id):
        return (_faq.PlatformRoleRecord("b-user-1", c_user_id, tenant_id, "admin"),)

    def roles_for_b_user(self, b_user_id, tenant_id):
        return ()


_faq.MySQLPlatformDirectory = lambda *_a, **_k: _Directory()
_api.MySQLPlatformDirectory = lambda *_a, **_k: _Directory()
"""
    )
    return directory


def http(
    url: str, *, payload: dict | None = None, token: str | None = None, timeout: float = 20.0
) -> tuple[int, object]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    request.add_header("X-Business-Entry", "consumer")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _json(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return exc.code, _json(exc.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — 0 means "never completed"
        return 0, ""


def _json(text: str) -> object:
    try:
        return json.loads(text)
    except ValueError:
        return text


def _free(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


def wait_port(port: int, *, timeout: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _free(port):
            return True
        time.sleep(0.25)
    return False


def _brief(body: object) -> object:
    if isinstance(body, dict):
        return {k: body.get(k) for k in ("qa_id", "status", "code", "error") if k in body}
    return str(body)[:140]


def _parse(line: str) -> dict[str, str] | None:
    pattern = re.compile(
        r"request_id=(?P<request_id>cor_[0-9a-f]{12}) "
        r"route=consumer_order_chat "
        r"stage=(?P<stage>[a-z_]+) "
        r"outcome=(?P<outcome>[a-z]+) "
        r"http_status=(?P<http_status>\d+) "
        r"error_code=(?P<error_code>[A-Z_\-]+) "
        r"tenant_hash=(?P<tenant_hash>[\-0-9a-f]+) "
        r"qa_id=(?P<qa_id>(?:qa_[0-9a-f]{32})|-) "
        r"duration_ms=(?P<duration_ms>\d+)"
    )
    match = pattern.search(line)
    return match.groupdict() if match else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="var/verify-evidence/consumer-order-observation.json")
    args = parser.parse_args()

    repo = repo_root()
    root = Path(tempfile.mkdtemp(prefix="aiops-verify-observation-"))
    for port in (GATEWAY_PORT, INTROSPECT_PORT, CPMS_PORT, CONTROL_PORT):
        if not _free(port):
            print(f"port {port} already in use — refuse to reuse someone else's listener")
            return 2

    introspect = ThreadingHTTPServer(("127.0.0.1", INTROSPECT_PORT), _Introspection)
    cpms = ThreadingHTTPServer(("127.0.0.1", CPMS_PORT), _Cpms)
    control = ThreadingHTTPServer(("127.0.0.1", CONTROL_PORT), _Control)
    for server in (introspect, cpms, control):
        threading.Thread(target=server.serve_forever, daemon=True).start()

    stubs = write_stubs(root)
    env = {
        **os.environ,
        "AIOPS_HOME": str(root),
        "AIOPS_CONFIG_HOME": str(root / "config"),
        "AIOPS_DATA_HOME": str(root / "data"),
        "AIOPS_GATEWAY_DATA_HOME": str(root / "data"),
        "AIOPS_GATEWAY_DATABASE_FILE": str(root / "data" / "gateway.db"),
        "AIOPS_GATEWAY_ENVIRONMENT": "verify",
        "AIOPS_GATEWAY_BIND": "127.0.0.1",
        "AIOPS_GATEWAY_PORT": str(GATEWAY_PORT),
        "AIOPS_GATEWAY_LOG_LEVEL": "INFO",
        "AIOPS_GATEWAY_INTROSPECTION_URL": f"http://127.0.0.1:{INTROSPECT_PORT}/introspect",
        "AIOPS_GATEWAY_INTROSPECTION_CLIENT_ID": CLIENT_ID,
        "AIOPS_GATEWAY_INTROSPECTION_CLIENT_SECRET": CLIENT_SECRET,
        "AIOPS_GATEWAY_STANDARD_API_AUDIENCE": AUDIENCE,
        "AIOPS_GATEWAY_CONSUMER_ORDER_CHAT_ENABLED": "true",
        "AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL": f"{TENANT}:http://127.0.0.1:{CPMS_PORT}",
        "AIOPS_GATEWAY_EXECUTION_ENVIRONMENT": "verify",
        "PYTHONPATH": os.pathsep.join(
            [str(stubs), str(repo / "src"), os.environ.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep),
    }
    config = root / "config" / "production.env"
    config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    config.write_text("# isolated verification\n", encoding="utf-8")
    config.chmod(0o600)
    out_dir = (repo / args.out).parent.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "consumer-order-observation.gateway.log"
    process = subprocess.Popen(
        ["uv", "run", "aiops-gateway", "serve"],
        cwd=repo,
        env=env,
        stdout=log_path.open("w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    checks: list[dict[str, object]] = []
    failures: list[str] = []

    def check(name: str, ok: bool, detail: object) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})
        print(f"  {'ok  ' if ok else 'FAIL'} {name}: {detail}")
        if not ok:
            failures.append(name)

    def set_control(**values: object) -> None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{CONTROL_PORT}/set",
            data=json.dumps(values).encode(),
            method="POST",
        )
        request.add_header("Content-Type", "application/json")
        urllib.request.urlopen(request, timeout=10).read()

    base = f"http://127.0.0.1:{GATEWAY_PORT}"
    try:
        if not wait_port(GATEWAY_PORT):
            print("gateway never bound; log tail:")
            print(log_path.read_text(encoding="utf-8", errors="replace")[-3000:])
            return 2
        check("gateway boots over TCP", http(f"{base}/health")[0] == 200, http(f"{base}/health")[0])

        def ask(question: str = QUESTION, token: str | None = TOKEN_OK, **extra: object):
            return http(
                f"{base}/v1/assistant/questions",
                payload={"question": question, **extra},
                token=token,
            )

        status, body = ask()
        check(
            "success is 202 completed",
            status == 202 and _brief(body).get("status") == "completed",
            _brief(body),
        )

        status, body = ask(token=TOKEN_NO_READ)
        check("missing orders:read is 403", status == 403, _brief(body))

        # 首次解析成功、分支内再次解析时凭据失效 ⇒ 401。必须是拒绝，不能进 5xx 分母。
        status, body = ask(token=TOKEN_EXPIRES_ON_RECHECK)
        check("credential revoked on re-authentication is 401", status == 401, _brief(body))

        status, body = ask(question="普通问题：怎么绑定手机号")
        check("feature-on ordinary question still answers", status in {200, 202}, _brief(body))

        set_control(unowned=True)
        status, body = ask()
        check("unowned order is 404", status == 404, _brief(body))
        set_control(unowned=False)

        _Cpms.wrong_order = True
        status, body = ask()
        _Cpms.wrong_order = False
        check("CPMS mismatch is 502", status == 502, _brief(body))

        status, body = ask(question=f"查询订单 {STRANGER} 的状态", order_no=STRANGER)
        check("unowned explicit order is 404 before the branch", status == 404, _brief(body))

        set_control(boom=True)
        status, body = ask()
        set_control(boom=False)
        check("unexpected exception is 500", status == 500, _brief(body))

        statuses: list[int] = []

        def one() -> None:
            code, _ = ask()
            statuses.append(code)

        threads = [threading.Thread(target=one) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=40)
        check("four concurrent requests all succeed", statuses == [202] * 4, statuses)

        status, _ = ask(token=None)
        check("unauthenticated is 401", status == 401, status)
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
        for server in (introspect, cpms, control):
            server.shutdown()
            server.server_close()
        shutil.rmtree(root, ignore_errors=True)

    lines = [
        line
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        if RESULT_PREFIX in line
    ]
    records = [_parse(line) for line in lines]
    good = [record for record in records if record]

    check(
        f"one result line per candidate request ({EXPECTED_LINES})",
        len(records) == EXPECTED_LINES,
        len(records),
    )
    check("every line parses", len(good) == len(records), len(records) - len(good))
    check(
        "ordinary question and unauthenticated request produced no line",
        len(records) == EXPECTED_LINES,
        "derived from the count above",
    )
    check(
        "outcomes split into completed / refused / failed",
        {r["outcome"] for r in good} == {"completed", "refused", "failed"},
        sorted({r["outcome"] for r in good}),
    )
    check(
        "each refusal carries its own code",
        {"INSUFFICIENT_SCOPE", "ORDER_NOT_FOUND", "INVALID_ACCESS_TOKEN"} <= {r["error_code"] for r in good},
        sorted({r["error_code"] for r in good}),
    )
    # 这条是本片修的缺陷：401 曾经被算成 failed，于是凭据问题会抬高服务 5xx 率。
    check(
        "a 401 is counted as refused, never as a 5xx failure",
        all(r["outcome"] == "refused" for r in good if r["http_status"] == "401"),
        [r["outcome"] for r in good if r["http_status"] == "401"],
    )
    check(
        "every failed row is a real 5xx (no 4xx leaked into the failure denominator)",
        all(int(r["http_status"]) >= 500 for r in good if r["outcome"] == "failed"),
        {r["http_status"]: r["outcome"] for r in good if r["outcome"] == "failed"},
    )
    check(
        "request ids are unique across concurrent requests",
        len({r["request_id"] for r in good}) == len(good),
        len({r["request_id"] for r in good}),
    )
    check(
        "success lines carry a real qa_id",
        all(r["qa_id"].startswith("qa_") for r in good if r["outcome"] == "completed"),
        sum(1 for r in good if r["outcome"] == "completed"),
    )
    check(
        "refusals never carry a qa_id",
        all(r["qa_id"] == "-" for r in good if r["outcome"] != "completed"),
        "checked",
    )
    durations = sorted(int(r["duration_ms"]) for r in good) or [0]
    check("durations present", durations[-1] >= 0, durations[:3] + ["..."] + durations[-3:])
    check(
        "no line carries the question, an order number, a credential or a raw tenant",
        not any(
            marker in line
            for line in lines
            for marker in (QUESTION, ORDER, STRANGER, TOKEN_OK, "PRIVATE-PLATE", TENANT)
        ),
        "scanned every line",
    )

    evidence = {
        "what_is_real": [
            "aiops-gateway serve: real process, real listener, real HTTP",
            "identity: real IntrospectionCallerResolver against a loopback introspection stub",
            "platform decision, refusal order, status codes: real",
            "the observation module under test: real",
        ],
        "what_is_synthetic": [
            "order rows (production: MySQL) — loopback stub",
            "UPMS platform directory (production: MySQL) — loopback stub",
            "order authorizer's source (production: MySQL) — loopback stub",
            "CPMS chargingInfo (production: external service) — loopback stub",
        ],
        "unproven": (
            "real logged-in user, real MySQL order data, real CPMS; this run proves the "
            "observation contract over real HTTP, not business acceptance"
        ),
        "base": base,
        "gateway_log": str(log_path),
        "result_lines": lines,
        "checks": checks,
        "failures": failures,
    }
    out_path = (repo / args.out).resolve()
    out_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=1))
    print(f"\nevidence: {out_path}")
    print(f"gateway log: {log_path}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
