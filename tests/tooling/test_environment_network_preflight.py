"""正式端点须多次匹配响应，重定向不能把错误入口洗成绿灯。"""

import json
import runpy
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

probe = runpy.run_path(
    str(Path(__file__).resolve().parents[2] / "tools/verification/business-environments/network_preflight.py")
)


def test_real_http_redirect_shape_and_intermittent_failure():
    class Handler(BaseHTTPRequestHandler):
        calls = []

        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            self.calls.append(self.path)
            status = 302 if self.path == "/redirect" else 200
            if self.path == "/flaky" and self.calls.count(self.path) == 2:
                status = 503
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "/destination")
            self.end_headers()
            self.wfile.write(b'{"ok":true}' if self.path != "/wrong" else b"<html>wrong site</html>")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:

        def check(path):
            return probe["run"](
                [{"id": "shared", "url": base + path, "status": 200, "json_equals": {"ok": True}}], 3, 2
            )

        assert check("/ok")["passed"] is True
        for path in ("/redirect", "/wrong", "/flaky"):
            assert check(path)["passed"] is False
        assert "/destination" not in Handler.calls
        assert base not in json.dumps(check("/ok"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize(
    "url",
    ["http://remote.example/", "https://user:secret@example.test", "https://example.test/?token=private"],
)
def test_private_manifest_refuses_unsafe_endpoints(tmp_path, url):
    path = tmp_path / "checks.json"
    path.write_text(json.dumps([{"id": "shared", "url": url, "status": 200}]))
    path.chmod(0o600)
    with pytest.raises(ValueError):
        probe["load"](path)
