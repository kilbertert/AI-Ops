"""H5 助手面的**转发清单 == 声明**，且**其余路由一条都不转发**（ADR-0092）。

这条判据存在的理由，和 `test_dify_exposure_registry.py` 一模一样：一行 nginx 正则
同时管着「开了什么」和「没开什么」，而它不会自己报错。两种漂移都要拦：

* **声明了却转发不到** —— 前端照样报错，而文档说已经修好；
* **转发面比声明大** —— 正则写松了（比如 `[^/]+` 或整片 `/v1/`），把管理面、
  发布面、Dify 运维面一起敞到消费者站点上，而没有任何信号。

判据用**真的 nginx** 跑一遍，不是用 Python 重新实现一遍正则语义。两边语义不完全
相同（nginx 的 `location ~` 是 PCRE、按最长/最前匹配选，Python 的 `re` 不是），
自己实现一遍等于用同一个错误证明自己正确。这里 `nginx -t` + 起一个真实例，
拿 `$uri` 探到多少次转发，就是多少次转发。

环境里没有 nginx 二进制时**跳过**，不伪装通过（本仓已有先例：缺依赖就跳过，
不写 `assert True`）。
"""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore

ROOT = Path(__file__).parents[1]
SNIPPET = ROOT / "ops" / "nginx" / "ulink-h5-assistant.conf"
DECLARED = ROOT / "ops" / "nginx" / "h5-assistant-routes.txt"

#: 声明文件里的 ID 占位符 → 网关**真实生成**的 id 形状（`gateway_store.py`：
#: `qa_`/`hrj_`/`dx_` + `uuid4().hex`）。用假 id 只是为了让路径能进正则，
#: 判的是**转发到没到**，不是这条 id 在不在库里。
_IDS = {
    "{qa_id}": "qa_0123456789abcdef0123456789abcdef",
    "{job_id}": "hrj_0123456789abcdef0123456789abcdef",
    "{diagnosis_id}": "dx_0123456789abcdef0123456789abcdef",
    "{signed_id}": "media_Abcdefghijklmnopqrstuvwxyz012_-9.0123456789abcdef01234567",
}

_LINE = re.compile(r"^(?P<method>[A-Z]+)\s+(?P<path>/\S+)$", re.MULTILINE)


class _Runtime:
    kb_search_client = None
    media_signer = None

    def shutdown(self) -> None:
        pass


def _declared() -> list[tuple[str, str]]:
    """(method, concrete path) from the declaration file."""
    rows: list[tuple[str, str]] = []
    for match in _LINE.finditer(DECLARED.read_text("utf-8")):
        path = match.group("path")
        for placeholder, value in _IDS.items():
            path = path.replace(placeholder, value)
        rows.append((match.group("method"), path))
    return rows


def _gateway_routes(tmp_path: Path) -> list[tuple[str, str]]:
    """(method, path) from the REAL app's route table — FastAPI's resolved view."""
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=_Runtime(),  # type: ignore[arg-type]
    )
    routes: list[tuple[str, str]] = []
    for route in app.routes:  # type: ignore[attr-defined]
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path is None or not methods:
            continue
        for method in methods:
            routes.append((method, str(path)))
    return sorted(set(routes))


def _concrete(method: str, template: str) -> str:
    """Fill a FastAPI path template with the same fake ids the declaration uses.

    **每个**占位符都要填。漏填一个（比如 `{agent_id}`）探针就会去请求字面量
    `/v1/agents/{agent_id}` —— `{`/`}` 在 URI 里非法，nginx 直接 400，
    正则根本没被触发，负向断言于是安静地全绿。第一版就是这么漏掉整组管理面的。
    """
    del method
    path = template
    path = re.sub(r"\{qa_id\}", _IDS["{qa_id}"], path)
    path = re.sub(r"\{job_id\}", _IDS["{job_id}"], path)
    path = re.sub(r"\{diagnosis_id\}", _IDS["{diagnosis_id}"], path)
    path = path.replace("{signed_id}", _IDS["{signed_id}"])
    # 其余占位符（{agent_id}、{order_no}、{run_id} …）填一个合法段即可 ——
    # 判的是"这条路径会不会被转发"，不是那个 id 在库里存不存在。
    return re.sub(r"\{[a-z_]+\}", "x0123456789abcdef0123456789abcdef", path)


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def test_the_declaration_parses_to_something() -> None:
    """一份解析出空集合的声明会让后面每条断言变成绿色的空转。"""
    rows = _declared()
    assert rows, f"{DECLARED.name} 里没解析出任何路由 —— 形状变了，或它空了"
    assert ("POST", "/v1/assistant/questions") in rows


@pytest.fixture(scope="module")
def forwarded(tmp_path_factory: pytest.TempPathFactory) -> dict[str, bool]:
    """Start a real nginx with the snippet and report, per URI, whether it proxied.

    The upstream is a stub that answers any path with a marker body,
    so "did this URI take the location" is observable directly.
    """
    if shutil.which("nginx") is None:
        pytest.skip("环境里没有 nginx 二进制 —— 不伪造这条判据")

    root = tmp_path_factory.mktemp("ngx")
    token_file = root / "token.conf"
    token_file.write_text(
        'set $aiops_service_authorization "Bearer test-token";\n'
        'set $aiops_source_key_injected "test-source";\n',
        encoding="utf-8",
    )
    # 片段与它的 .inc 都装到临时目录，判的是转发面，不是宿主路径。
    inc = (SNIPPET.parent / "ulink-h5-assistant-proxy.inc").read_text("utf-8")
    inc = inc.replace("/etc/aiops-41/nginx-aiops-service-token.conf", str(token_file))
    # 上游换成一个本地 stub：命中 location 就会拿到 marker，没命中走静态根。
    inc = inc.replace("http://172.18.0.1:8788", "http://127.0.0.1:18099")
    (root / "proxy.inc").write_text(inc, encoding="utf-8")
    site = SNIPPET.read_text("utf-8").replace("/etc/aiops-41/h5-assistant-proxy.inc", str(root / "proxy.inc"))
    (root / "root").mkdir()
    (root / "root" / "index.html").write_text("STATIC-FALLBACK", encoding="utf-8")
    (root / "site.conf").write_text(
        f"server {{\n  listen 127.0.0.1:18098;\n  root {root / 'root'};\n  index index.html;\n{site}\n}}\n",
        encoding="utf-8",
    )
    # 前台跑：nginx 默认 daemonize，Popen 的 pid 是启动器而不是 master，
    # terminate() 杀不掉真正在监听的那个 —— 上一版就漏了一个 master 下来，
    # 占着端口让后续测试打到**旧配置**上（判据照样"通过"，其实是空转）。
    (root / "nginx.conf").write_text(
        "worker_processes 1;\n"
        "daemon off;\n"
        f"error_log {root / 'error.log'};\n"
        f"pid {root / 'nginx.pid'};\n"
        "events { worker_connections 64; }\n"
        "http {\n"
        "  access_log off;\n"
        f"  include {root / 'site.conf'};\n"
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

    # 端口被占 ⇒ 这次判据会打到**别人的** nginx 上，那不是证据。先探一下。
    if _listening(18098):
        pytest.fail("127.0.0.1:18098 已被占用 —— 判据会打到别人身上，先清掉再跑")

    stub = subprocess.Popen(  # noqa: S603 - fixed argv, test-only stub
        [
            "python3",
            "-c",
            "import http.server, socketserver\n"
            "class H(http.server.BaseHTTPRequestHandler):\n"
            "    def _ok(self):\n"
            "        self.send_response(200)\n"
            "        self.send_header('Content-Type','application/json')\n"
            "        self.end_headers()\n"
            "        self.wfile.write(b'PROXIED')\n"
            "    do_GET = do_POST = _ok\n"
            "    def log_message(self, *a): pass\n"
            "socketserver.TCPServer.allow_reuse_address=True\n"
            "socketserver.TCPServer(('127.0.0.1',18099),H).serve_forever()\n",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    nginx = subprocess.Popen(  # noqa: S603 - fixed argv
        ["nginx", "-p", str(root), "-c", str(root / "nginx.conf")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def reached(method: str, path: str) -> bool:
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", 18098), timeout=0.2) as sock:
                    sock.sendall(
                        f"{method} {path} HTTP/1.1\r\nHost: h5.test\r\n"
                        "Content-Length: 0\r\nConnection: close\r\n\r\n".encode()
                    )
                    body = sock.makefile("rb").read()
                # 400 = 探针自己把 URI 写坏了，判据会空转成"全都没转发"。必须炸出来。
                if b" 400 " in body.split(b"\r\n", 1)[0] + b" ":
                    raise AssertionError(f"探针 URI 非法（nginx 400）：{method} {path}")
                return b"PROXIED" in body
            except OSError:
                if nginx.poll() is not None:
                    break
                time.sleep(0.1)
        raise AssertionError(f"nginx 没起来（退出码 {nginx.poll()}），判断不了转发面")

    try:
        # 键是 (方法, 路径)，**不是路径**：同一路径上不同方法转发与否是两个答案
        # （`GET /v1/agents` 泄漏了、`POST /v1/agents` 没有），只按路径存会让后一条
        # 覆盖前一条 —— 第一版就是 76 条网关路由被压成 56 个键，负向断言漏掉整组管理面。
        result: dict[tuple[str, str], bool] = {
            (method, path): reached(method, path) for method, path in _declared()
        }

        # 负向：网关 /v1/ 下**不在声明里**的路径，一条都不许转发。
        declared = {(m, p) for m, p in _declared()}
        for method, template in _gateway_routes(Path(root)):
            concrete = _concrete(method, template)
            if not concrete.startswith("/v1/") or (method, concrete) in declared:
                continue
            result[(method, concrete)] = reached(method, concrete)
        media = "/v1/media/" + _IDS["{signed_id}"]
        for method, path in [
            ("POST", media),
            ("DELETE", media),
            ("GET", media[:-1]),
            ("GET", media + "a"),
            ("GET", "/v1/media/admin"),
        ]:
            result[(method, path)] = reached(method, path)
        yield result
    finally:
        for proc in (nginx, stub):
            proc.terminate()
        for proc in (nginx, stub):
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)


def test_every_declared_path_is_forwarded(forwarded: dict[tuple[str, str], bool]) -> None:
    declared = {(m, p) for m, p in _declared()}
    missing = sorted(f"{m} {p}" for (m, p), ok in forwarded.items() if (m, p) in declared and not ok)
    assert not missing, f"声明了但没转发到网关：{missing}"


def test_nothing_outside_the_declaration_is_forwarded(
    forwarded: dict[tuple[str, str], bool],
) -> None:
    """**这是这条测试真正在守的东西。**

    正则写松一格（`[^/]+`、`.*`、整片 `/v1/`），就会把发布面、注册表面、Dify 运维面
    敞到消费者站点上 —— 而那不会让任何现有测试变红。这里会。
    """
    declared = {(m, p) for m, p in _declared()}
    leaked = sorted(f"{m} {p}" for (m, p), ok in forwarded.items() if (m, p) not in declared and ok)
    assert not leaked, (
        f"这些 /v1/ 路由不在声明里，却被转发到了网关：{leaked}\n"
        "要么把它加进 ops/nginx/h5-assistant-routes.txt 并说明理由，"
        "要么把 nginx 正则收窄。"
    )
