"""正式配置边界，以及真正 nginx 的路径/认证透传。"""

import json
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
    text = (DEPLOY / "gateway-callback.conf.template").read_text()
    for key, value in {
        "AIOPS_NODE_BRIDGE": "127.0.0.1",
        "AIOPS_NODE_STATE": str(tmp_path),
        "AIOPS_NODE_CONFIG": str(tmp_path),
        "AIOPS_PRODUCTION_GATEWAY": f"http://127.0.0.1:{upstream.server_port}",
    }.items():
        text = text.replace("${" + key + "}", value)
    config = tmp_path / "nginx.conf"
    config.write_text(text.replace(":8788", f":{port}"))
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
        for path, method in [
            (root + "/tools/shell/authorization", "POST"),
            (root + "/cpms/chOrderInfo/stopCharging/ORDER-1", "GET"),
            (root + "/cpms/chOrderInfo/getOrderInfoDetail/ORDER-1", "POST"),
        ]:
            with pytest.raises(urllib.error.HTTPError) as error:
                opener.open(
                    urllib.request.Request(f"https://127.0.0.1:{port}{path}", method=method), timeout=2
                )
            assert error.value.code in (403, 404)
        assert len(seen) == 7
    finally:
        proc.terminate()
        proc.communicate(timeout=5)
        upstream.shutdown()


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
