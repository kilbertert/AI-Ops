"""在隔离文件系统验证入口安装/失败恢复，不访问宿主nginx。"""

import hashlib
import subprocess
from pathlib import Path


def test_install_restore_and_failed_validation_keep_previous_files(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    assets = repo / "ops/dify-console-bridge"
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    target, allowed = tmp_path / "target.conf", tmp_path / "config" / "allowed.conf"
    allowed.parent.mkdir()
    target.write_text("previous server\n")
    allowed.write_text("allow 127.0.0.2;\n")
    sources = tmp_path / "sources.conf"
    sources.write_text("allow 127.0.0.1;\n")
    sources.chmod(0o600)
    fake = tmp_path / "nginx"
    fake.write_text(
        f'#!/bin/sh\nif [ "$1" = -t ] && [ -f "{tmp_path}/fail" ]; then rm "{tmp_path}/fail"; exit 1; fi\n'
    )
    fake.chmod(0o700)
    script = (assets / "service-api-deploy.sh").read_text()
    replacements = {
        "/www/server/nginx/sbin/nginx": str(fake),
        "/www/server/panel/vhost/nginx/aiops-dify-service-api.conf": str(target),
        "/etc/aiops-dify-api/allowed-sources.conf": str(allowed),
        "/etc/aiops-dify-api": str(allowed.parent),
        "/run/lock/aiops-dify-service-api.lock": str(tmp_path / "lock"),
    }
    for old, new in replacements.items():
        script = script.replace(old, new)
    (bundle / "service-api-deploy.sh").write_text(script)
    (bundle / "dify-service-api-TLS.conf").write_text("candidate server\n")
    (bundle / "REVISION").write_text("a" * 40 + "\n")

    def hashes():
        (bundle / "SHA256SUMS").write_text(
            "".join(
                f"{hashlib.sha256((bundle / n).read_bytes()).hexdigest()}  {n}\n"
                for n in ["REVISION", "service-api-deploy.sh", "dify-service-api-TLS.conf"]
            )
        )

    def run(action, backup):
        return subprocess.run(
            ["bash", str(bundle / "service-api-deploy.sh"), action, str(bundle), str(backup), str(sources)],
            capture_output=True,
            text=True,
        )

    hashes()
    backup = tmp_path / "backup"
    assert run("install", backup).returncode == 0
    assert target.read_text() == "candidate server\n" and allowed.read_text() == sources.read_text()
    assert run("restore", backup).returncode == 0
    assert target.read_text() == "previous server\n" and allowed.read_text() == "allow 127.0.0.2;\n"
    # 仅候选写入后制造语法失败，不能用前置检查失败冒充恢复通过。
    script = script.replace(
        'install -m 644 -- "$bundle/dify-service-api-TLS.conf" "$target"',
        'install -m 644 -- "$bundle/dify-service-api-TLS.conf" "$target"\ntouch "'
        + str(tmp_path / "fail")
        + '"',
    )
    (bundle / "service-api-deploy.sh").write_text(script)
    hashes()
    assert run("install", tmp_path / "failed-backup").returncode != 0
    assert target.read_text() == "previous server\n" and allowed.read_text() == "allow 127.0.0.2;\n"
    sources.write_text("allow 127.0.0.1; include /private/secret;\n")
    assert run("install", tmp_path / "invalid-backup").returncode != 0
    assert target.read_text() == "previous server\n"
