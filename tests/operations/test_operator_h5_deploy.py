"""真实 Nginx 验证身份/路由；临时文件验证制品校验与失败恢复。"""

import importlib.util
import json
import shutil
import socket
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

ROOT = Path(__file__).parents[2]
spec = importlib.util.spec_from_file_location("operator_h5_deploy", ROOT / "deploy/operator-h5.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_real_nginx_preserves_bearer_and_restricts_routes(tmp_path):
    if not shutil.which("nginx"):
        pytest.skip("nginx unavailable")
    front, upstream = port(), port()
    inc = (
        (ROOT / "ops/nginx/operator-h5-proxy.inc")
        .read_text()
        .replace("http://127.0.0.1:30899", f"http://127.0.0.1:{upstream}")
    )
    (tmp_path / "proxy.inc").write_text(inc)
    site = (
        (ROOT / "ops/nginx/operator-h5-assistant.conf")
        .read_text()
        .replace("/etc/aiops-41/operator-h5-proxy.inc", str(tmp_path / "proxy.inc"))
    )
    config = tmp_path / "nginx.conf"
    config.write_text(
        f"daemon off; pid {tmp_path}/pid; error_log {tmp_path}/error.log; "
        "events {} http { access_log off; "
        f"client_body_temp_path {tmp_path}/body; proxy_temp_path {tmp_path}/proxy; "
        f"server {{ listen 127.0.0.1:{upstream}; location / {{ "
        "return 200 '$request_method|$request_uri|$http_authorization|$http_x_business_entry|"
        "$http_x_aiops_source_key|$http_third_session|$http_x_third_session'; }} "
        f"server {{ listen 127.0.0.1:{front}; {site} location / {{ return 404; }} }} }}"
    )
    subprocess.run(["nginx", "-t", "-p", str(tmp_path), "-c", str(config)], check=True, capture_output=True)
    process = subprocess.Popen(["nginx", "-p", str(tmp_path), "-c", str(config)])
    try:
        for _ in range(60):
            if process.poll() is not None:
                pytest.fail("nginx exited")
            try:
                with socket.create_connection(("127.0.0.1", front), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("nginx not listening")
        qa = "qa_" + "a" * 32
        allowed = [
            ("GET", "/v1/faq/recommendations"),
            ("GET", "/v1/faq/catalog"),
            ("GET", "/v1/shortcuts"),
            ("POST", "/v1/faq/answer"),
            ("POST", "/v1/assistant/questions"),
            ("GET", f"/v1/assistant/questions/{qa}"),
            ("POST", f"/v1/assistant/questions/{qa}/cancel"),
        ]
        denied = [
            ("POST", "/v1/shortcuts"),
            ("GET", "/v1/assistant/questions"),
            ("POST", f"/v1/assistant/questions/{qa}"),
            ("GET", "/v1/agents"),
            ("POST", "/v1/dify/publish"),
            ("POST", "/v1/standard/diagnoses"),
            ("DELETE", f"/v1/assistant/questions/{qa}"),
        ]
        for method, path in allowed + denied:
            request = Request(
                f"http://127.0.0.1:{front}{path}",
                method=method,
                headers={
                    "Authorization": "Bearer own-user",
                    "X-Business-Entry": "consumer",
                    "X-AIOps-Source-Key": "forged",
                    "third-session": "forged",
                    "X-Third-Session": "forged",
                },
            )
            try:
                response = urlopen(request, timeout=3)
            except HTTPError as exc:
                response = exc
            with response:
                body = response.read().decode()
                if (method, path) in allowed:
                    assert response.status == 200
                    assert body == f"{method}|{path}|Bearer own-user|operator|||"
                    assert response.headers["Cache-Control"] == "no-store"
                else:
                    assert response.status in {404, 405}
                    assert "own-user" not in body
    finally:
        process.terminate()
        process.wait(timeout=5)


def fixture(tmp_path):
    build = tmp_path / "build"
    build.mkdir()
    for name in ("index.html", "aiPackage-pages-chat-chat.js"):
        (build / name).write_text("new")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "dirty": False,
                "source_commit": "a" * 40,
                "files": {p.name: deploy.hashlib.sha256(p.read_bytes()).hexdigest() for p in build.iterdir()},
            }
        )
    )
    return build, manifest


def test_rejects_dirty_tampered_and_unlisted_artifacts(tmp_path):
    build, manifest = fixture(tmp_path)
    deploy.verify(build, manifest, "a" * 40)
    with pytest.raises(ValueError):
        deploy.verify(build, manifest, "b" * 40)
    (build / "index.html").write_text("tampered")
    with pytest.raises(ValueError, match="摘要"):
        deploy.verify(build, manifest, "a" * 40)
    (build / "extra").write_text("unlisted")
    with pytest.raises(ValueError, match="集合"):
        deploy.verify(build, manifest, "a" * 40)
    data = json.loads(manifest.read_text())
    data["dirty"] = True
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="干净"):
        deploy.verify(build, manifest, "a" * 40)


def test_failed_nginx_validation_restores_previous_site(tmp_path, monkeypatch):
    build, manifest = fixture(tmp_path)
    old = tmp_path / "old"
    old.mkdir()
    (old / "index.html").write_text("old")
    (old / "static/js").mkdir(parents=True)
    (old / "static/js/old.12345678.js").write_text("cached asset")
    (old / "static/private.json").write_text("not a published chunk")
    vhost = tmp_path / "site.conf"
    vhost.write_text(f"root {old};\n")
    extension = tmp_path / "extension.conf"
    extension.write_text("previous extension")
    proxy = tmp_path / "proxy.inc"
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(deploy.subprocess, "run", run)
    args = SimpleNamespace(
        build=build,
        manifest=manifest,
        revision="a" * 40,
        release=tmp_path / "release",
        vhost=vhost,
        extension=extension,
        proxy=proxy,
        backup=tmp_path / "backup",
        nginx="nginx",
    )
    with pytest.raises(subprocess.CalledProcessError):
        deploy.install(args)
    assert vhost.read_text() == f"root {old};\n"
    assert extension.read_text() == "previous extension"
    assert not proxy.exists()
    assert (old / "index.html").read_text() == "old"
    assert (args.release / "static/js/old.12345678.js").read_text() == "cached asset"
    assert not (args.release / "static/private.json").exists()
    assert calls == [["nginx", "-t"], ["nginx", "-t"], ["nginx", "-s", "reload"]]
