"""实际文件替换与失败恢复；systemd/nginx 为本地命令替身，不接触服务宿主。"""

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("failure", ["none", "reload", "transient_route", "permanent_route"])
def test_bridge_install_waits_for_new_routes_or_restores_all_files(tmp_path, failure):
    fail_reload = failure in {"reload", "permanent_route"}
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    package = bundle / "aiops_diagnostics"
    package.mkdir()
    for name in (
        "__init__",
        "dify_console_publish",
        "async_job_lifecycle",
        "bounded_http",
        "http_auth",
    ):
        shutil.copyfile(ROOT / "src/aiops_diagnostics" / f"{name}.py", package / f"{name}.py")
    for name in ("dify-publish-button.js", "dify-console-publish.conf"):
        shutil.copyfile(ROOT / "ops/dify-console-bridge" / name, bundle / name)
    (bundle / "REVISION").write_text("test-revision\n")
    (bundle / "SHA256SUMS").write_text(
        "".join(
            f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(bundle)}\n"
            for p in sorted(bundle.rglob("*"))
            if p.is_file()
        )
    )
    code = tmp_path / "installed"
    code.mkdir()
    (code / "old.py").write_text("old package\n")
    js, snippet, nginx = (tmp_path / n for n in ("button.js", "snippet.conf", "nginx"))
    js.write_text("old js\n")
    snippet.write_text("old snippet\n")
    nginx.write_text(
        '#!/bin/sh\nif [ "$*" = "-s reload" ] && [ -f "$FAIL_RELOAD" ]; then rm "$FAIL_RELOAD"; exit 1; fi\n'
    )
    nginx.chmod(0o700)
    if failure == "reload":
        (tmp_path / "fail").touch()
    if failure == "transient_route":
        (tmp_path / "old-route").touch()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {
        "systemctl": "exit 0",
        "sleep": "exit 0",
        "curl": 'if [ "$ROUTE_FAILURE" = permanent_route ] || [ -f "$OLD_ROUTE" ]; then '
        'rm -f "$OLD_ROUTE"; printf 404; exit 0; fi; '
        'while [ "$1" != "-o" ]; do shift; done; '
        'printf \'{"published":false,"action":"","reason":"refused"}\' > "$2"; printf 403',
    }.items():
        path = bin_dir / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o700)
    script = (ROOT / "ops/dify-console-bridge/deploy.sh").read_text()
    for before, after in {
        "/opt/aiops-console-bridge/aiops_diagnostics": code,
        "/srv/aiops-console-assets/dify-publish-button.js": js,
        "/www/server/panel/vhost/nginx/snippets/dify-console-publish.conf": snippet,
        "/www/server/nginx/sbin/nginx": nginx,
    }.items():
        script = script.replace(before, str(after))
    result = subprocess.run(
        ["bash", "-c", script, "deploy.sh", str(bundle), str(tmp_path / "backup")],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAIL_RELOAD": str(tmp_path / "fail"),
            "OLD_ROUTE": str(tmp_path / "old-route"),
            "ROUTE_FAILURE": failure,
        },
        capture_output=True,
        text=True,
    )
    assert (result.returncode != 0) == fail_reload, result.stdout + result.stderr
    if fail_reload:
        assert list(code.iterdir()) == [code / "old.py"]
        assert js.read_text() == "old js\n"
        assert snippet.read_text() == "old snippet\n"
    else:
        assert (code / "dify_console_publish.py").read_bytes() == (
            package / "dify_console_publish.py"
        ).read_bytes()
        assert js.read_bytes() == (bundle / "dify-publish-button.js").read_bytes()
        assert snippet.read_bytes() == (bundle / "dify-console-publish.conf").read_bytes()
    assert (tmp_path / "backup/aiops_diagnostics/old.py").read_text() == "old package\n"
