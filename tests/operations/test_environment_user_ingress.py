"""环境用户助手入口：**转发面 == 声明** + 按入口分流凭据（ADR-0319）。

与 `test_h5_assistant_routes.py` 同一判据的两半各管一件事：

* **声明了却转发不到** —— 客户端照样 404，而文档说入口已经开通；
* **转发面比声明大** —— 一条写松的正则把管理面、内部回调面一起敞到公网域名上，
  没有任何信号。

本片还多一个判据：身份头分流。consumer 必须换成环境服务凭据并转换会话、
operator 必须透传个人 Bearer 且不混会话头、客户端自报的来源/环境断言头必须
到不了上游。判据用**真的 nginx** 跑，上游是回显请求头的第二个 server 块 ——
「上游收到了什么」直接可读，不是用 Python 重实现一遍 header 语义。

环境里没有 nginx 二进制时**跳过**，不伪装通过。
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
CONF = ROOT / "ops" / "nginx" / "environment-user-ingress.conf"
INC = ROOT / "ops" / "nginx" / "environment-user-ingress-proxy.inc"
DECLARED = ROOT / "ops" / "nginx" / "environment-user-ingress-routes.txt"

_spec = importlib.util.spec_from_file_location(
    "h5_assistant_routes", ROOT / "tests" / "test_h5_assistant_routes.py"
)
_h5 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_h5)

_IDS = {
    "{qa_id}": "qa_0123456789abcdef0123456789abcdef",
    "{conversation_id}": "conv_0123456789abcdef0123456789abcdef",
}

_LINE = re.compile(r"^(?P<method>[A-Z]+)\s+(?P<path>/\S+)$", re.MULTILINE)
MARKER = "UPSTREAM_SAW"

SERVICE_AUTHORIZATION = "Bearer svc-test-token"
OPERATOR_SOURCE_KEY = "op-source-test-key"


def _declared() -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for match in _LINE.finditer(DECLARED.read_text("utf-8")):
        path = match.group("path")
        for placeholder, value in _IDS.items():
            path = path.replace(placeholder, value)
        rows.append((match.group("method"), path))
    return rows


def _concrete(template: str) -> str:
    path = template
    for placeholder, value in _IDS.items():
        path = re.sub(re.escape(placeholder), value, path)
    return re.sub(r"\{[a-z_]+\}", "x0123456789abcdef0123456789abcdef", path)


@pytest.fixture(scope="module")
def ingress(tmp_path_factory: pytest.TempPathFactory):
    """真 nginx + 回显上游。返回 (request 闭包,)。

    上游是第二个 server 块：命中即回显方法与全部身份头，没命中就说明没进
    转发面 —— 转发/拒绝都直接可读。
    """
    if shutil.which("nginx") is None:
        pytest.skip("环境里没有 nginx 二进制 —— 不伪造这条判据")

    root = tmp_path_factory.mktemp("ngx")
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        proxy_port = reserved.getsockname()[1]
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        upstream_port = reserved.getsockname()[1]

    (root / "auth.conf").write_text(
        f'set $aiops_user_service_authorization "{SERVICE_AUTHORIZATION}";\n'
        f'set $aiops_user_operator_source_key "{OPERATOR_SOURCE_KEY}";\n',
        encoding="utf-8",
    )
    inc = INC.read_text("utf-8").replace("http://127.0.0.1:8788", f"http://127.0.0.1:{upstream_port}")
    (root / "proxy.inc").write_text(inc, encoding="utf-8")
    site = CONF.read_text("utf-8").replace("__ENV__", "testenv")
    site = site.replace("/etc/aiops-testenv/nginx-user-ingress-auth.conf", str(root / "auth.conf"))
    site = site.replace("/etc/aiops-testenv/environment-user-ingress-proxy.inc", str(root / "proxy.inc"))

    echo = (
        f"location / {{ return 200 '{MARKER}|$request_method|$request_uri|"
        "$http_authorization|$http_x_business_entry|$http_x_third_session|"
        "$http_third_session|$http_x_aiops_source_key|$http_x_aiops_environment|"
        "$http_x_aiops_environment_key|$http_x_aiops_gateway_assertion|'; }"
    )
    (root / "nginx.conf").write_text(
        "worker_processes 1;\n"
        "daemon off;\n"
        f"error_log {root / 'error.log'};\n"
        f"pid {root / 'nginx.pid'};\n"
        "events { worker_connections 64; }\n"
        "http {\n"
        "  access_log off;\n"
        f"  client_body_temp_path {root / 'body'};\n"
        f"  proxy_temp_path {root / 'proxy'};\n"
        f"  server {{\n    listen 127.0.0.1:{upstream_port};\n    {echo}\n  }}\n"
        f"  server {{\n    listen 127.0.0.1:{proxy_port};\n{site}\n"
        "    location / { return 404; }\n  }\n"
        "}\n",
        encoding="utf-8",
    )
    check = subprocess.run(
        ["nginx", "-t", "-p", str(root), "-c", str(root / "nginx.conf")],
        capture_output=True,
        text=True,
    )
    if check.returncode != 0:
        pytest.fail(f"nginx 不认这个片段：\n{check.stderr}")
    for port in (proxy_port, upstream_port):
        if _h5._listening(port):
            pytest.fail("临时端口已被占用，不能对其他实例取证")

    nginx = subprocess.Popen(  # noqa: S603 - fixed argv
        ["nginx", "-p", str(root), "-c", str(root / "nginx.conf")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def request(method: str, path: str, headers: dict[str, str] | None = None):
        head = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", proxy_port), timeout=0.2) as sock:
                    sock.sendall(
                        f"{method} {path} HTTP/1.1\r\nHost: env.test\r\n{head}"
                        "Content-Length: 0\r\nConnection: close\r\n\r\n".encode()
                    )
                    raw = sock.makefile("rb").read()
                status = int(raw.split(b" ", 2)[1])
                body = raw.rsplit(b"\r\n\r\n", 1)[-1].decode("utf-8", "replace")
                return status, body
            except OSError:
                if nginx.poll() is not None:
                    break
                time.sleep(0.1)
        raise AssertionError(f"nginx 没起来（退出码 {nginx.poll()}）")

    try:
        for _ in range(50):
            if _h5._listening(upstream_port):
                break
            if nginx.poll() is not None:
                pytest.fail("nginx 启动失败")
            time.sleep(0.1)
        else:
            pytest.fail("上游未就绪")
        yield request
    finally:
        nginx.terminate()
        try:
            nginx.wait(timeout=10)
        except subprocess.TimeoutExpired:
            nginx.kill()
            nginx.wait(timeout=10)


def _fields(body: str) -> list[str]:
    """回显体拆成 [marker, method, uri, auth, entry, x3rd, third, src, env, envkey, assertion, '']。"""
    return body.split("|")


CONSUMER = {"X-Business-Entry": "consumer", "third-session": "sess-c", "Authorization": "Bearer client-own"}
OPERATOR = {
    "X-Business-Entry": "operator",
    "Authorization": "Bearer op-jwt",
    "third-session": "sess-evil",
    "X-Third-Session": "sess-evil2",
    "X-AIOps-Source-Key": "forged-source",
    "X-AIOps-Environment": "41",
    "X-AIOps-Environment-Key": "forged-env-key",
    "X-AIOps-Gateway-Assertion": "forged-assertion",
}


def test_declaration_is_nonempty() -> None:
    rows = _declared()
    assert rows, f"{DECLARED.name} 里没解析出任何路由"
    assert ("POST", "/v1/assistant/questions") in rows


@pytest.mark.parametrize(("method", "path"), _declared())
def test_every_declared_path_is_forwarded(ingress, method: str, path: str) -> None:
    status, body = ingress(method, path, CONSUMER)
    assert status == 200
    fields = _fields(body)
    assert fields[0] == MARKER, f"{method} {path} 声明了却没到上游：{status} {body!r}"
    assert fields[1] == method
    assert fields[2] == path


def test_consumer_gets_service_token_and_session_conversion(ingress) -> None:
    status, body = ingress("GET", "/v1/faq/catalog", CONSUMER)
    assert status == 200
    f = _fields(body)
    assert f[3] == SERVICE_AUTHORIZATION, "consumer 应注入环境服务 Bearer，客户端 Authorization 不透传"
    assert f[4] == "consumer"
    assert f[5] == "sess-c", "third-session 必须转成 X-Third-Session"
    assert f[6] == "", "原始 third-session 头不再透传"
    assert f[7] == "", "consumer 不注入来源密钥"


def test_operator_keeps_bearer_without_session_or_forged_headers(ingress) -> None:
    status, body = ingress("POST", "/v1/conversations", OPERATOR)
    assert status == 200
    f = _fields(body)
    assert f[3] == "Bearer op-jwt", "operator 透传个人 Bearer"
    assert f[4] == "operator"
    assert f[5] == "" and f[6] == "", "operator 不混 consumer 会话（两种会话头都清空）"
    assert f[7] == OPERATOR_SOURCE_KEY, "operator 注入本环境来源密钥，客户端伪造值被覆盖"
    assert f[8] == "" and f[9] == "" and f[10] == "", "环境/断言类客户端自报头一律清空"


def test_operator_client_source_key_never_reaches_upstream(ingress) -> None:
    """只清伪造头这一点单独验：客户端带来源密钥时它到不了上游。"""
    headers = {
        "X-Business-Entry": "operator",
        "Authorization": "Bearer op",
        "X-AIOps-Source-Key": "forged",
    }
    status, body = ingress("GET", "/v1/faq/catalog", headers)
    assert status == 200 and _fields(body)[7] == OPERATOR_SOURCE_KEY


@pytest.mark.parametrize("entry", ["admin", "Consumer", "consumer operator", ""])
def test_invalid_or_missing_entry_is_rejected(ingress, entry: str) -> None:
    headers = {} if entry == "" else {"X-Business-Entry": entry, "third-session": "sess"}
    for method, path in [("GET", "/v1/faq/catalog"), ("POST", "/v1/assistant/questions")]:
        status, body = ingress(method, path, headers)
        assert status == 403, f"{entry!r} {method} {path} 应 403，得到 {status}"
        assert MARKER not in body


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/faq/catalog"),
        ("HEAD", "/v1/faq/catalog"),
        ("GET", "/v1/faq/answer"),
        ("GET", "/v1/assistant/questions"),  # 列表未开放
        ("POST", "/v1/assistant/questions/qa_0123456789abcdef0123456789abcdef"),
        ("DELETE", "/v1/conversations"),
        ("PUT", "/v1/conversations/conv_0123456789abcdef0123456789abcdef"),
        ("POST", "/v1/conversations/conv_0123456789abcdef0123456789abcdef"),
    ],
)
def test_undeclared_method_on_exposed_path_is_405_or_404(ingress, method: str, path: str) -> None:
    status, body = ingress(method, path, CONSUMER)
    assert status in (404, 405), f"{method} {path} 应拒，得到 {status}"
    assert MARKER not in body


def test_management_and_internal_paths_never_forwarded(ingress) -> None:
    for method, path in [
        ("POST", "/v1/dify/publish"),
        ("POST", "/v1/dify/manage"),
        ("GET", "/v1/dify/workspace"),
        ("POST", "/v1/agents"),
        ("POST", "/v1/internal/execution-node/capabilities"),
        ("POST", "/v1/internal/consumer-data/status"),
        ("GET", "/v1/assistant/starters"),
        ("GET", "/v1/shortcuts"),
        ("POST", "/v1/assistant/questions/qa_0123456789abcdef0123456789abcdef/cancel"),
        ("POST", "/v1/conversations/conv_0123456789abcdef0123456789abcdef/active-order"),
        ("GET", "/v1/health-report-jobs/hrj_0123456789abcdef0123456789abcdef"),
    ]:
        status, body = ingress(method, path, CONSUMER)
        assert status in (404, 405), f"{method} {path} 泄漏到上游：{status}"
        assert MARKER not in body


def test_no_other_gateway_route_is_forwarded(ingress, tmp_path) -> None:
    """网关 /v1/ 下声明之外的路由，一条都不许到上游。"""
    declared = set(_declared())
    leaked: list[str] = []
    for method, template in _h5._gateway_routes(tmp_path):
        path = _concrete(template)
        if not path.startswith("/v1/") or (method, path) in declared:
            continue
        status, body = ingress(method, path, CONSUMER)
        if MARKER in body:
            leaked.append(f"{method} {template}")
    assert not leaked, f"这些 /v1/ 路由不在声明里却被转发：{leaked}"
