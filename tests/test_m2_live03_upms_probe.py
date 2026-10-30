"""身份预检走真实 HTTP 和生产客户端，错误不能被解析器的 C 端降级吞掉。"""

import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from aiops_diagnostics.common.config import UpmsSettings

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from m2_live import upms_probe


@pytest.mark.parametrize("fault", ["none", "auth", "html", "timeout"])
def test_identity_probe_rejects_failed_dependencies(fault, capsys):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):  # noqa: N802
            calls.append((self.path, self.headers.get("Authorization")))
            if fault == "timeout":
                time.sleep(1.2)
                return
            body = b"<html>ok</html>" if fault == "html" else json.dumps({"code": 0, "data": None}).encode()
            self.send_response(401 if fault == "auth" else 200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        settings = UpmsSettings(
            base_url=f"http://127.0.0.1:{server.server_port}",
            inside_token="private-fixture",
            timeout_seconds=1,
        )
        assert upms_probe.check(settings) == (fault == "none")
        assert calls == [("/user/inside/byUserId/__aiops_preflight__", "Bearer private-fixture")]
        assert "private-fixture" not in capsys.readouterr().out
    finally:
        server.shutdown()
        server.server_close()


def test_deps_fails_when_only_identity_probe_fails():
    checks = Path(__file__).resolve().parents[1] / "deploy/m2-live-03/checks.sh"
    source = checks.read_text()
    deps = source[source.index("\ndeps() {") : source.index("\nstatus() {")]
    script = (
        deps
        + """
say() { :; }
docker() {
  case "$*" in *upms_probe.py*) return 1 ;; esac
  return 0
}
M2LIVE_SYNTH_MODE=0
deps
"""
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 1
