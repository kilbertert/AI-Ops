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
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiops_diagnostics.conversation.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway.environment_registry import apply as registry_apply
from aiops_diagnostics.gateway.environment_registry import load as registry_load
from aiops_diagnostics.gateway.gateway_api import create_gateway_app
from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway.gateway_store import GatewayStore
from aiops_diagnostics.identity.caller_auth import CALLER_AUTH_INVALID, CallerAuthError
from aiops_diagnostics.identity.capability import CAPABILITY_ASSERTED
from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord
from aiops_diagnostics.knowledge.knowledge_retrieval import MediaProxy, MediaResourceSigner

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
    "{signed_id}": "media_Abcdefghijklmnopqrstuvwxyz012_-9.0123456789abcdef01234567",
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


def _materialize(root: Path, upstream_port: int) -> str:
    """凭据片 + .inc + server 片段渲染到测试目录，返回 server 块文本。"""
    (root / "auth.conf").write_text(
        f'set $aiops_user_service_authorization "{SERVICE_AUTHORIZATION}";\n'
        f'set $aiops_user_operator_source_key "{OPERATOR_SOURCE_KEY}";\n',
        encoding="utf-8",
    )
    inc = INC.read_text("utf-8").replace("http://127.0.0.1:8788", f"http://127.0.0.1:{upstream_port}")
    (root / "proxy.inc").write_text(inc, encoding="utf-8")
    site = CONF.read_text("utf-8").replace("__ENV__", "testenv")
    site = site.replace("/etc/aiops-access-testenv/nginx-user-ingress-auth.conf", str(root / "auth.conf"))
    site = site.replace(
        "/etc/aiops-access-testenv/environment-user-ingress-proxy.inc", str(root / "proxy.inc")
    )
    # media location 内联 proxy_pass（签名即授权，不用共用 .inc），同样指到上游。
    return site.replace("http://127.0.0.1:8788", f"http://127.0.0.1:{upstream_port}")


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

    site = _materialize(root, upstream_port)

    echo = (
        f"location / {{ return 200 '{MARKER}|$request_method|$request_uri|"
        "$http_authorization|$http_x_business_entry|$http_x_third_session|"
        "$http_third_session|$http_x_aiops_source_key|$http_x_aiops_environment|"
        "$http_x_aiops_environment_key|$http_x_aiops_gateway_assertion|$http_cookie|'; }"
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
        f"  server {{\n    listen 127.0.0.1:{proxy_port};\n"
        f"    access_log {root / 'access.log'};\n{site}\n"
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

    request.access_log = root / "access.log"  # type: ignore[attr-defined]

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
    """回显体按位拆字段：[marker, method, uri, auth, entry, x3rd, third, src,
    env, envkey, assertion, cookie, '']。"""
    return body.split("|")


CONSUMER = {
    "X-Business-Entry": "consumer",
    "third-session": "sess-c",
    "Authorization": "Bearer client-own",
    "Cookie": "session=evil",
}
OPERATOR = {
    "X-Business-Entry": "operator",
    "Authorization": "Bearer op-jwt",
    "third-session": "sess-evil",
    "X-Third-Session": "sess-evil2",
    "X-AIOps-Source-Key": "forged-source",
    "X-AIOps-Environment": "41",
    "X-AIOps-Environment-Key": "forged-env-key",
    "X-AIOps-Gateway-Assertion": "forged-assertion",
    "Cookie": "session=evil",
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
    assert f[11] == "", "Cookie 不透传（宿主日志记录它）"


def test_missing_entry_defaults_to_consumer_chain(ingress) -> None:
    """41 实测契约：缺 X-Business-Entry 的旧客户端按 consumer 处理。"""
    status, body = ingress("GET", "/v1/faq/catalog", {"third-session": "sess-old"})
    assert status == 200
    f = _fields(body)
    assert f[3] == SERVICE_AUTHORIZATION
    assert f[4] == "consumer", "缺头按 consumer 转发，上游看到的入口是规范化值"
    assert f[5] == "sess-old"
    assert f[7] == ""


@pytest.mark.parametrize("entry", ["Consumer", " consumer ", "CONSUMER"])
def test_consumer_entry_is_normalized_like_upstream(ingress, entry: str) -> None:
    status, body = ingress("GET", "/v1/faq/catalog", {"X-Business-Entry": entry, "third-session": "s"})
    assert status == 200
    f = _fields(body)
    assert f[3] == SERVICE_AUTHORIZATION and f[4] == "consumer"


def test_operator_keeps_bearer_without_session_or_forged_headers(ingress) -> None:
    status, body = ingress("POST", "/v1/conversations", OPERATOR)
    assert status == 200
    f = _fields(body)
    assert f[3] == "Bearer op-jwt", "operator 透传个人 Bearer"
    assert f[4] == "operator"
    assert f[5] == "" and f[6] == "", "operator 不混 consumer 会话（两种会话头都清空）"
    assert f[7] == OPERATOR_SOURCE_KEY, "operator 注入本环境来源密钥，客户端伪造值被覆盖"
    assert f[8] == "" and f[9] == "" and f[10] == "", "环境/断言类客户端自报头一律清空"
    assert f[11] == "", "Cookie 不透传"


@pytest.mark.parametrize("entry", ["OPERATOR", " operator ", "Operator"])
def test_operator_entry_is_normalized_like_upstream(ingress, entry: str) -> None:
    status, body = ingress(
        "GET", "/v1/faq/catalog", {"X-Business-Entry": entry, "Authorization": "Bearer op"}
    )
    assert status == 200
    f = _fields(body)
    assert f[3] == "Bearer op" and f[4] == "operator" and f[7] == OPERATOR_SOURCE_KEY


def test_operator_client_source_key_never_reaches_upstream(ingress) -> None:
    """只清伪造头这一点单独验：客户端带来源密钥时它到不了上游。"""
    headers = {
        "X-Business-Entry": "operator",
        "Authorization": "Bearer op",
        "X-AIOps-Source-Key": "forged",
    }
    status, body = ingress("GET", "/v1/faq/catalog", headers)
    assert status == 200 and _fields(body)[7] == OPERATOR_SOURCE_KEY


@pytest.mark.parametrize("entry", ["admin", "consumer operator", "operator-admin", "OPERATORS"])
def test_invalid_entry_is_rejected(ingress, entry: str) -> None:
    headers = {"X-Business-Entry": entry, "third-session": "sess"}
    for method, path in [("GET", "/v1/faq/catalog"), ("POST", "/v1/assistant/questions")]:
        status, body = ingress(method, path, headers)
        assert status == 403, f"{entry!r} {method} {path} 应 403，得到 {status}"
        assert MARKER not in body


MEDIA = "/v1/media/media_Abcdefghijklmnopqrstuvwxyz012_-9.0123456789abcdef01234567"


def test_media_needs_no_entry_and_leaks_no_identity_headers(ingress) -> None:
    """签名即授权：不带任何头也转发；伪造身份头一个都到不了上游。"""
    status, body = ingress("GET", MEDIA)
    assert status == 200 and _fields(body)[0] == MARKER, "media 不要求入口头"
    status, body = ingress("GET", MEDIA, OPERATOR)
    assert status == 200
    f = _fields(body)
    assert f[3] == "" and f[4] == "", "Authorization/入口头不透传给 media 上游"
    assert f[5] == "" and f[6] == "" and f[7] == "", "会话/来源头全空"
    assert f[8] == "" and f[9] == "" and f[10] == "" and f[11] == "", "环境/断言/Cookie 全空"


def test_piece_paths_never_reach_access_log_but_company_paths_do(ingress) -> None:
    """签名 URL/会话 URI 是凭据载体：本片 location 的 access_log 全关；
    同 server 内公司其他路径（catch-all 替身）照常记录。"""
    ingress("GET", MEDIA)
    ingress("GET", "/v1/faq/catalog", CONSUMER)
    ingress("POST", "/v1/assistant/questions", OPERATOR)
    ingress("GET", "/company-legacy-path")  # catch-all —— 公司路径替身
    time.sleep(0.2)  # access_log 在请求结束时落盘
    log = ingress.access_log.read_text("utf-8")
    assert "media_" not in log, f"签名 ID 进了 access_log：{log!r}"
    assert "faq" not in log and "assistant" not in log, "本片路径进了 access_log"
    assert "/company-legacy-path" in log, "公司路径应照常记录（片内关日志只关本片）"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/faq/catalog"),
        ("HEAD", "/v1/faq/catalog"),
        ("GET", "/v1/faq/answer"),
        ("GET", "/v1/assistant/questions"),  # 列表未开放
        ("POST", "/v1/assistant/questions/qa_0123456789abcdef0123456789abcdef"),
        ("GET", "/v1/assistant/questions/qa_0123456789abcdef0123456789abcdef/cancel"),
        ("PUT", "/v1/assistant/questions/qa_0123456789abcdef0123456789abcdef/cancel"),
        ("POST", "/v1/media/media_Abcdefghijklmnopqrstuvwxyz012_-9.0123456789abcdef01234567"),
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
        ("POST", "/v1/conversations/conv_0123456789abcdef0123456789abcdef/active-order"),
        ("GET", "/v1/health-report-jobs/hrj_0123456789abcdef0123456789abcdef"),
    ]:
        status, body = ingress(method, path, CONSUMER)
        assert status in (404, 405), f"{method} {path} 泄漏到上游：{status}"
        assert MARKER not in body


def test_id_prefix_shape_guard_rejects_malformed_and_suffixed(ingress) -> None:
    """^~ 前缀内的 $uri 形状闸：同前缀但非白名单形状一律 404，不转发。"""
    for path in [
        "/v1/assistant/questions/",
        "/v1/assistant/questions/qa_0123",
        "/v1/assistant/questions/qa_0123456789abcdef0123456789abcdef/",
        "/v1/assistant/questions/qa_0123456789abcdef0123456789abcdef/cancel/extra",
        "/v1/assistant/questions/notqa_0123456789abcdef0123456789abcdef",
        "/v1/media/",
        "/v1/media/media_tooshort.0123456789abcdef01234567",
        "/v1/media/media_Abcdefghijklmnopqrstuvwxyz012_-9.0123456789abcdef01234567x",
        "/v1/media/media_Abcdefghijklmnopqrstuvwxyz012_-9.0123456789abcdef01234567/path",
        "/v1/media/admin",
        "/v1/conversations/",
        "/v1/conversations/conv_0123456789abcdef0123456789abcde",
        "/v1/conversations/conv_0123456789abcdef0123456789abcdef/",
    ]:
        status, body = ingress("GET", path, CONSUMER)
        assert status == 404, f"{path} 应被形状闸 404，得到 {status}"
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


# ── 真 Gateway 缝：nginx 片段驱动的不再是回显上游而是真进程 ──────────────────
#
# 回显上游证明「转发面==声明+头部合同」；这一层证明同一份片段后面站着
# **真 Gateway 进程**时，匿名/会话/公司链/停用环境/签名媒体的行为是真实
# API 答案，不是替身语义（verify-aiops-gateway 的接缝层，一次性数据根）。

SERVICE_TOKEN = "svc-test-token"  # nginx 注入值减去 "Bearer " —— 替身 resolver 的合同
SEAM_ENV = "testenv"
SEAM_TENANT = "T-1"


class _SeamResolver:
    """契约形状与公司链一致的真实-schema 替身。

    服务令牌+有效会话 → consumer 身份；来源密钥+个人令牌 → operator 身份；
    其余一律 CallerAuthError —— 与 SourceKeyCallerResolver/RedisThirdSession 的
    分派形状相同，身份是真实 ScopeContext。
    """

    def __init__(self) -> None:
        self.seen: list[dict[str, str | None]] = []

    def resolve(
        self,
        token: str,
        *,
        required_scope: str,
        third_session: str | None = None,
        platform_entry: str | None = None,
        source_key: str | None = None,
    ) -> ScopeContext:
        self.seen.append({"token": token, "third": third_session, "entry": platform_entry, "src": source_key})
        if source_key == OPERATOR_SOURCE_KEY:
            if token != "op-jwt":
                raise CallerAuthError("company token rejected", code=CALLER_AUTH_INVALID)
        elif token == SERVICE_TOKEN:
            if third_session != "sess-c":
                raise CallerAuthError("invalid third session", code=CALLER_AUTH_INVALID)
        else:
            raise CallerAuthError("unknown token", code=CALLER_AUTH_INVALID)
        subject = SubjectRecord(b_user_id="c:C-1", c_user_id="C-1", tenant_id=SEAM_TENANT)
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id=SEAM_TENANT,
            data_scope=DataScope(type="self"),
            roles=frozenset(),
            permissions=frozenset({required_scope}),
            capability_source=CAPABILITY_ASSERTED,
        )


class _SeamDirectory:
    """consumer 有 c_user 行；operator 平台行带 admin client_type。"""

    def roles_for_c_user(self, c_user_id: str, tenant_id: str):
        return (PlatformRoleRecord("B-1", "C-1", SEAM_TENANT, "admin"),)

    def roles_for_b_user(self, b_user_id: str, tenant_id: str):
        return ()


class _SeamRuntime:
    """只要媒体面；其余路由本片探针不打，不伪造属性。"""

    def __init__(self, signer: MediaResourceSigner) -> None:
        self.media_signer = signer
        self.kb_search_client = None
        self.agent_store = None
        self._proxy = MediaProxy(signer, lambda grant: b"MEDIA-BYTES")

    def serve_media(self, signed_id: str, *, range_header: str | None = None):
        return self._proxy.serve_signed(signed_id, range_header=range_header)

    def shutdown(self) -> None:
        pass


def _registry_rows(root: Path, *, enabled: bool) -> list[dict]:
    return [
        {
            "id": SEAM_ENV,
            "enabled": enabled,
            "gateway_url": "https://testenv.example.test",
            "cpms_base_url": "https://testenv.example.test/cpms",
            "config_ref": str(root / "production.env"),
            "tenants": [{"id": SEAM_TENANT, "enabled": True}],
        }
    ]


@pytest.fixture(scope="module")
def seam(tmp_path_factory: pytest.TempPathFactory):
    """真 nginx + 真 Gateway（uvicorn 上的 create_gateway_app，一次性数据根）。"""
    if shutil.which("nginx") is None:
        pytest.skip("环境里没有 nginx 二进制 —— 不伪造这条判据")

    root = tmp_path_factory.mktemp("seam")
    registry_path = root / "registry" / "directory.json"
    registry_apply(registry_path, _registry_rows(root, enabled=True), expected_revision=0, actor="seam")

    settings = GatewayServerSettings(
        data_home=root,
        database_file=root / "gateway.db",
        server_config_file=root / "production.env",
        environment=SEAM_ENV,
        environment_registry_file=str(registry_path),
    )
    settings.server_config_file.write_text("# seam\n", encoding="utf-8")
    signer = MediaResourceSigner("seam-secret", ttl_seconds=600)
    resolver = _SeamResolver()
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=_SeamRuntime(signer),  # type: ignore[arg-type]
        caller_resolver=resolver,
        platform_resolver=PlatformIdentityResolver(_SeamDirectory()),
        faq_catalog=FAQCatalog.bundled(),
    )

    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        gateway_port = reserved.getsockname()[1]
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        proxy_port = reserved.getsockname()[1]

    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=gateway_port, log_level="error", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    site = _materialize(root, gateway_port)
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
        for _ in range(100):
            if _h5._listening(gateway_port) and _h5._listening(proxy_port):
                break
            if nginx.poll() is not None or not thread.is_alive():
                pytest.fail("nginx 或 Gateway 启动失败")
            time.sleep(0.1)
        else:
            pytest.fail("Gateway/nginx 未就绪")
        yield SimpleNamespace(request=request, resolver=resolver, registry_path=registry_path, signer=signer)
    finally:
        nginx.terminate()
        try:
            nginx.wait(timeout=10)
        except subprocess.TimeoutExpired:
            nginx.kill()
            nginx.wait(timeout=10)
        server.should_exit = True
        thread.join(timeout=10)


def test_seam_anonymous_gets_real_gateway_401(seam) -> None:
    """匿名请求带服务令牌（缺头→consumer 注入）但无会话 → 真 resolver 拒绝。"""
    status, body = seam.request("GET", "/v1/faq/catalog")
    assert status == 401
    assert "INVALID_ACCESS_TOKEN" in body, f"匿名应答应是真实 401 信封：{body!r}"


def test_seam_consumer_synthetic_session_resolves(seam) -> None:
    """third-session 经 nginx 转换后真解析成 consumer 身份，目录行给真应答。"""
    status, body = seam.request(
        "GET", "/v1/faq/catalog", {"X-Business-Entry": "consumer", "third-session": "sess-c"}
    )
    assert status == 200, body
    assert "faq_version" in body or "entries" in body
    last = seam.resolver.seen[-1]
    assert last["token"] == SERVICE_TOKEN, "注入的必须是服务 Bearer 不是客户端 Authorization"
    assert last["third"] == "sess-c" and last["src"] in (None, "")


def test_seam_operator_bearer_via_source_key(seam) -> None:
    status, body = seam.request(
        "GET",
        "/v1/faq/catalog",
        {"X-Business-Entry": "operator", "Authorization": "Bearer op-jwt"},
    )
    assert status == 200, body
    last = seam.resolver.seen[-1]
    assert last["src"] == OPERATOR_SOURCE_KEY and last["entry"] == "operator"
    assert last["third"] in (None, ""), "operator 链不混 consumer 会话"


def test_seam_forged_source_key_buys_no_company_identity(seam) -> None:
    """伪造真实来源密钥值 + 个人 Bearer 走 consumer 入口：nginx 清空后密钥
    到不了 resolver —— 落回会话链，客户端 Bearer 被服务令牌覆盖，仍是
    consumer 身份而不是公司身份。"""
    status, body = seam.request(
        "GET",
        "/v1/faq/catalog",
        {
            "X-Business-Entry": "consumer",
            "Authorization": "Bearer op-jwt",
            "third-session": "sess-c",
            "X-AIOps-Source-Key": OPERATOR_SOURCE_KEY,
        },
    )
    assert status == 200, body
    last = seam.resolver.seen[-1]
    assert last["src"] in (None, ""), "伪造来源密钥必须到不了上游"
    assert last["token"] == SERVICE_TOKEN


def test_seam_disabled_environment_still_refuses(seam) -> None:
    """登记目录停用本环境 → 注册闸真实 403；随后恢复不影响其余断言。"""
    current = registry_load(seam.registry_path)["revision"]
    try:
        registry_apply(
            seam.registry_path,
            _registry_rows(seam.registry_path.parent.parent, enabled=False),
            expected_revision=current,
            actor="seam",
        )
        status, body = seam.request(
            "GET", "/v1/faq/catalog", {"X-Business-Entry": "consumer", "third-session": "sess-c"}
        )
        assert status == 403, body
        assert "INSUFFICIENT_SCOPE" in body
    finally:
        latest = registry_load(seam.registry_path)["revision"]
        registry_apply(
            seam.registry_path,
            _registry_rows(seam.registry_path.parent.parent, enabled=True),
            expected_revision=latest,
            actor="seam",
        )


def test_seam_media_signature_serves_real_bytes(seam) -> None:
    resource = seam.signer.issue(
        tenant_id=SEAM_TENANT,
        agent_version="agent-1#v1",
        session_id="sess-1",
        knowledge_base_id="kb-1",
        document_id="doc-1",
        chunk_id="chunk-1",
        backend_id="backend-1",
        kind="image",
        mime_type="image/png",
        title="示意图.png",
        reference_id="chunk-1",
    )
    signed = resource.url.rsplit("/", 1)[-1]
    status, body = seam.request("GET", f"/v1/media/{signed}")
    assert status == 200 and "MEDIA-BYTES" in body, f"签名媒体应真出字节：{status} {body!r}"
    forged = signed[:-1] + ("0" if signed[-1] != "0" else "1")
    status, _ = seam.request("GET", f"/v1/media/{forged}")
    assert status == 403, "形状合法但签名被改 → 真 Gateway 的答案是 403"
