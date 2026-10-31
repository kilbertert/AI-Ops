"""开发机运行实际Java报告器，使用回环HTTP合成凭据，结束只清理自己的容器。"""

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

root = pathlib.Path(__file__).resolve().parents[2]
bodies = []
out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "dist/verification/node-capabilities")
out.mkdir(parents=True, exist_ok=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/v1/internal/execution-node/capabilities":
            bodies.append((body, self.headers.get("Authorization")))
        raw = b'{"accepted":true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
name = "aiops-capability-check-" + str(int(time.time()))
cmd = [
    "docker",
    "run",
    "--rm",
    "--name",
    name,
    "--network",
    "host",
    "--user",
    f"{os.getuid()}:{os.getgid()}",
    "-v",
    str(root / "services/execution-node/target/aiops-execution-node.jar") + ":/app.jar:ro",
    "-v",
    str(root / "src/aiops_diagnostics/operator/operator_tools.json") + ":/tools.json:ro",
    "eclipse-temurin:21-jdk",
    "java",
    "-Xmx256m",
    "-Daiops.node.tools-file=/tools.json",
    "-jar",
    "/app.jar",
    "--server.port=0",
    "--server.address=127.0.0.1",
    "--snail-ai.enabled=true",
    "--snail-ai.server.host=127.0.0.1",
    "--snail-ai.server.port=1",
    "--snail-ai.app-id=synthetic-only",
    "--snail-ai.token=synthetic-only",
    "--snail-ai.port=0",
    "--snail-ai.host=127.0.0.1",
    "--aiops.gateway.base-url=http://127.0.0.1:" + str(server.server_port),
    "--aiops.gateway.node-token=synthetic-only",
]
try:
    log = (out / "java-start.log").open("w")
    p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 50
    while time.monotonic() < deadline and not bodies and p.poll() is None:
        time.sleep(0.25)
    assert bodies, "actual Java did not report"
    body, auth = bodies[0]
    assert auth == "Bearer synthetic-only"
    assert body["tool_contract"] == "request-target/1"
    assert set(body["schemas"]) == {"m3-exec-config/1", "operator-exec-config/1", "operator-conversation/1"}
    assert (
        body["tools_sha256"]
        == hashlib.sha256(
            (root / "src/aiops_diagnostics/operator/operator_tools.json").read_bytes()
        ).hexdigest()
    )
    print(
        json.dumps(
            {
                "java_report": True,
                "contract": body["tool_contract"],
                "schemas": body["schemas"],
                "tools_digest_matches": True,
            }
        )
    )
finally:
    subprocess.run(["docker", "stop", "-t", "3", name], capture_output=True)
    server.shutdown()
    server.server_close()
    log.close()
