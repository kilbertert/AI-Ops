"""正式配置边界，以及真正 nginx 的路径/认证透传。"""

import json
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

DEPLOY = Path(__file__).resolve().parents[1] / "deploy/node-runtime"


def test_runtime_is_internal_and_keeps_credentials_separate():
    config = yaml.safe_load((DEPLOY / "compose.yaml").read_text())
    services = config["services"]
    assert services["model-proxy"]["entrypoint"] == [
        "python",
        "/opt/aiops-src/services/model-proxy/model_proxy.py",
    ]
    assert set(services) == {"store", "platform", "node", "model-proxy"}
    for name in ("node", "platform", "store"):
        assert all(config["networks"][n]["internal"] for n in services[name]["networks"])
    assert services["platform"]["networks"]["trial"]["ipv4_address"] == "${AIOPS_NODE_PLATFORM_ADDRESS:?}"
    assert all(not s.get("ports") for s in services.values())
    for name in ("node", "platform", "model-proxy"):
        assert services[name]["user"] == "${AIOPS_NODE_UID:?}:${AIOPS_NODE_GID:?}"
    assert "--aiops.node.managed-publication=true" in services["node"]["command"]
    assert "upstream_api_key" not in json.dumps(services["node"])
    assert all("/etc/aiops-41" not in v for s in services.values() for v in s.get("volumes", []))
    assert services["model-proxy"]["volumes"] == [
        "${AIOPS_NODE_CONFIG:?}/model-proxy:/etc/m2live:ro",
        "${AIOPS_NODE_CONFIG:?}/tls/proxy:/etc/m2live-tls:ro",
    ]


def test_node_tool_declaration_mount_matches_the_jvm_path():
    node = yaml.safe_load((DEPLOY / "compose.yaml").read_text())["services"]["node"]
    options = [part for part in node["command"] if part.startswith("-Daiops.node.tools-file=")]
    assert len(options) == 1, "节点命令必须显式加载工具声明"
    assert node["command"].index(options[0]) < node["command"].index("-jar")
    target = options[0].split("=", 1)[1]
    mount = next(
        volume for volume in node["volumes"] if isinstance(volume, dict) and volume["target"] == target
    )
    assert mount == {
        "type": "bind",
        "source": "${AIOPS_NODE_CONFIG:?}/node/operator-tools.json",
        "target": target,
        "read_only": True,
        "bind": {"create_host_path": False},
    }


def _renderer():
    """加载仓库里的回调配置渲染器（测试不自己拼白名单，那正是漏掉站点工具的方式）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "render_node_callback", DEPLOY.parents[1] / "tools/content/render_node_callback.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_nginx_forwards_only_node_posts_with_original_credentials(tmp_path):
    nginx = shutil.which("nginx")
    if not nginx:
        pytest.skip("nginx 未安装；该环境不证明实际代理行为")
    certificate = tmp_path / "tls/callback"
    certificate.mkdir(parents=True)
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=callback.test",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            str(certificate / "server.key"),
            "-out",
            str(certificate / "server.crt"),
        ],
        check=True,
        capture_output=True,
    )
    context = ssl.create_default_context(cafile=str(certificate / "server.crt"))
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append((self.path, self.headers.get("Authorization"), None, b""))
            self.send_response(204)
            self.end_headers()

        def do_POST(self):  # noqa: N802
            seen.append(
                (
                    self.path,
                    self.headers.get("Authorization"),
                    self.headers.get("X-AI-Ops-Exec-Config"),
                    self.rfile.read(int(self.headers["Content-Length"])),
                )
            )
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # 用**真实渲染器**生成配置：工具白名单与取数路径来自声明，不在这里手写。
    # 2026-11-02 的站点工具事故正是"测试与模板各写一份、声明里加了一个白白漏掉"，
    # 夹具若自己拼白名单，就等于把同一处漏掉在测试里再藏一次。
    module = _renderer()

    config = tmp_path / "nginx.conf"
    config.write_text(
        module.render(
            "127.0.0.1", str(tmp_path), str(tmp_path), f"http://127.0.0.1:{upstream.server_port}"
        ).replace(":8788", f":{port}")
    )
    proc = subprocess.Popen(
        [nginx, "-e", "stderr", "-c", str(config), "-g", "daemon off;"], stderr=subprocess.PIPE
    )
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                assert proc.poll() is None, proc.stderr.read().decode()
                time.sleep(0.02)
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
        )
        path = "/v1/internal/charging-status/claims/run_test"
        untrusted = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with pytest.raises(urllib.error.URLError) as error:
            untrusted.open(f"https://127.0.0.1:{port}{path}", data=b"{}", timeout=2)
        assert isinstance(error.value.reason, ssl.SSLCertVerificationError)
        with pytest.raises(urllib.error.HTTPError) as error:
            opener.open(f"http://127.0.0.1:{port}{path}", data=b"{}", timeout=2)
        assert error.value.code == 400
        assert seen == []
        for endpoint in ("claims", "completions", "execution-config"):
            path = f"/v1/internal/charging-status/{endpoint}/run_test"
            req = urllib.request.Request(
                f"https://127.0.0.1:{port}{path}",
                data=b"{}",
                headers={"Authorization": "Bearer synthetic", "X-AI-Ops-Exec-Config": "digest"},
            )
            with opener.open(req, timeout=2) as res:
                assert res.status == 204
            assert seen[-1] == (path, "Bearer synthetic", "digest", b"{}")
        for path, method in [
            ("/v1/assistant/questions", "POST"),
            ("/v1/internal/charging-status/claims/run_test", "GET"),
            ("/v1/internal/charging-status/claims/not-a-run", "POST"),
        ]:
            with pytest.raises(urllib.error.HTTPError) as error:
                opener.open(
                    urllib.request.Request(f"https://127.0.0.1:{port}{path}", method=method), timeout=2
                )
            assert error.value.code in (403, 404)
        assert len(seen) == 3
        root = "/v1/internal/operator-runs/run_test"
        for tail in (
            "completions",
            "tools/cpms_order_detail/authorization",
            "tools/cpms_charging_samples/evidence",
        ):
            path = root + "/" + tail
            with opener.open(
                urllib.request.Request(
                    f"https://127.0.0.1:{port}{path}",
                    data=b"{}",
                    headers={"Authorization": "Bearer node-token", "X-AI-Ops-Exec-Config": "manifest"},
                ),
                timeout=2,
            ) as res:
                assert res.status == 204
            assert seen[-1] == (path, "Bearer node-token", "manifest", b"{}")
        path = root + "/cpms/iotChargingDevice/getChargingMonitor/ORDER-1?current=1&size=50"
        with opener.open(
            urllib.request.Request(
                f"https://127.0.0.1:{port}{path}", headers={"Authorization": "Bearer task-grant"}
            ),
            timeout=2,
        ) as res:
            assert res.status == 204
        assert seen[-1] == (path, "Bearer task-grant", None, b"")
        # 声明里的**每一个**工具都必须能从节点到达网关。站点工具（无订单参数）
        # 2026-11-02 就是漏在这里：声明有、白名单没有，运行期只看到 cpms_unavailable。
        from aiops_diagnostics.operator import operator_tools

        for tool in operator_tools.tools():
            tail = tool.path_template.lstrip("/").replace("{order_no}", "ORDER-1")
            path = root + "/cpms/" + tail
            with opener.open(
                urllib.request.Request(
                    f"https://127.0.0.1:{port}{path}", headers={"Authorization": "Bearer task-grant"}
                ),
                timeout=2,
            ) as res:
                assert res.status == 204, f"{tool.name} 的取数路径没有放行"
            assert seen[-1][0] == path
        for path, method in [
            (root + "/tools/shell/authorization", "POST"),
            (root + "/cpms/chOrderInfo/stopCharging/ORDER-1", "GET"),
            (root + "/cpms/chOrderInfo/getOrderInfoDetail/ORDER-1", "POST"),
            (root + "/cpms/chSite/page?current=1&size=50", "POST"),
            # 白名单是**每条 path_template 各自成立**，不是「像某个前缀就行」。
            # 2026-11-02 的候选实现给所有路径统一加可选斜杠/尾段，实测放行了下面三种
            # 声明里根本没有的形状（站点分页没有尾段、订单路径必须有单号）。
            (root + "/cpms/chSite/pageBOGUS", "GET"),
            (root + "/cpms/chSite/page/ORDER-1", "GET"),
            (root + "/cpms/chOrderInfo/getOrderInfoDetail", "GET"),
        ]:
            with pytest.raises(urllib.error.HTTPError) as error:
                opener.open(
                    urllib.request.Request(f"https://127.0.0.1:{port}{path}", method=method), timeout=2
                )
            assert error.value.code in (403, 404)
        assert len(seen) == 7 + len(operator_tools.tools())
    finally:
        # 「起得来」不等于「活得下去」：`nginx -t` 通过、启动后退出的形状是实撞过的
        # （lua 模块加载失败）。整段请求跑完，进程必须仍是活的。
        assert proc.poll() is None, proc.stderr.read().decode()
        proc.terminate()
        proc.communicate(timeout=5)
        upstream.shutdown()


def test_the_template_carries_no_lua_directive_by_default(tmp_path):
    """模板不能无条件带 Lua 指令：普通 nginx 会以 `unknown directive` 起不来。

    41 的宝塔构建需要那两行，但它是**该环境**的受控配置，不是模板默认值。渲染器把
    `AIOPS_NODE_LUA_DIRECTIVES` 原样拼进去 —— 传什么就有什么，不传就是空行。
    """
    module = _renderer()
    plain = module.render("127.0.0.1", "/s", "/c", "http://127.0.0.1:8788")
    assert not [line for line in plain.splitlines() if line.strip().startswith("lua_")]
    lua = 'lua_package_path "/www/server/nginx/lib/lua/?.lua;;";'
    assert lua in module.render("127.0.0.1", "/s", "/c", "http://127.0.0.1:8788", lua)


# --------------------------------------------------------------------------
# 节点制品记录：源码摘要必须跟着源码走
# --------------------------------------------------------------------------
def _digest_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("node_source_digest", DEPLOY / "source_digest.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_node_source_digest_matches_the_lock():
    """「节点源码改了但制品记录没更新」必须在 CI 变红。

    2026-10-30 的线上故障就是这样漏过去的：`images.lock.json` 的 `node.sha256` 是个死常量，
    而当时唯一的测试只断言「lock 与 HOST-CHANGE **两个常量彼此一致**」—— 两个常量互相点头，
    谁也没看真实源码。CI 不构建 Java，所以 jar 摘要验不了；源码摘要不需要 JDK。
    """
    module = _digest_module()
    value, count = module.digest()
    assert count > 10, "源码摘要只覆盖了很少的文件，规则可能写错了"
    assert value == module.locked(), (
        "services/execution-node 的源码摘要与 images.lock.json 不一致：\n"
        "  改了节点源码 → 按 deploy/node-runtime/README.md 构建并部署，然后更新\n"
        "  images.lock.json 的 node.source_sha256 / sha256 / bytes / source_commit。\n"
        "  只改文档或注释也照样要更新（摘要按字节算），这是有意的：制品记录必须指向真实源码。"
    )


def test_node_source_commit_is_a_real_commit():
    """`node.source_commit` 必须是一个提交号，不能是分支名或占位符。

    2026-10-31：#1239 把它写成了分支名占位符
    `pending-merge:fix/dual-entry-runtime-compatibility` 并合入 main。它不影响任何校验
    （jar 摘要与源码摘要各验各的），所以没人发现——但它是这一块唯一的「这是哪份源码」
    的指路字段，写成非 SHA 就等于把指路牌摘了。

    **判据是格式，不是"这个提交在历史里"**：CI 是 `fetch-depth: 1` 的浅克隆，祖先提交
    在检出里根本不存在，按可达性断言会让正确的值在 CI 里假红（本 PR 第一次推送就是这样）。
    而这次真正的缺陷形态恰是「不是 SHA」，格式校验抓得住它，且与克隆深度无关。
    """
    locked = json.loads((DEPLOY.parent / "m2-live-03" / "images.lock.json").read_text(encoding="utf-8"))
    commit = locked["node"]["source_commit"]
    assert re.fullmatch(r"[0-9a-f]{7,40}", commit), (
        f"images.lock.json 的 node.source_commit={commit!r} 不是提交号。"
        " 节点制品记录必须指向真实源码修订，不能写分支名或占位符。"
    )


def test_the_source_digest_actually_depends_on_the_source(tmp_path: Path):
    """对照组：源码变了摘要必须变，否则上面那条只是「函数恒返回同一个值」。"""
    module = _digest_module()
    base, _ = module.digest()
    copy = tmp_path / "node"
    shutil.copytree(module.SOURCE, copy, ignore=shutil.ignore_patterns("target", "__pycache__"))
    target = copy / "pom.xml"
    target.write_text(target.read_text(encoding="utf-8") + "\n<!-- one more comment -->\n", encoding="utf-8")
    changed, _ = module.digest(copy)
    assert changed != base
    # 构建产物不参与摘要：往 target/ 里丢文件不该改变它。
    (copy / "target").mkdir(exist_ok=True)
    (copy / "target" / "junk.jar").write_bytes(b"x")
    assert module.digest(copy)[0] == changed
