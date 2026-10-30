"""`make-certs.sh` 的复用/重建语义。

真正的坑都在「根材料与拆分视图不同步」和「权限把目录锁死」上：文件看起来全对，
容器却读不到或读到旧证书。这些用例用临时目录跑真实脚本（openssl 是本地命令），
不接触任何生产材料。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy/m2-live-03"


pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="需要 openssl")


@pytest.fixture(autouse=True)
def _restore_permissions(tmp_path):
    """脚本会把 tls/ 下的目录设成 0700、文件设成 0600；pytest 清理时要能删掉它们。"""
    yield
    for entry in sorted(tmp_path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        with contextlib.suppress(OSError):
            entry.chmod(0o700 if entry.is_dir() else 0o600)


def _run(config: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(DEPLOY / "make-certs.sh")],
        env={**os.environ, "M2LIVE_CONFIG": str(config)},
        capture_output=True,
        text=True,
        timeout=120,
    )


def _fresh(config: Path) -> Path:
    (config / "platform").mkdir(parents=True, exist_ok=True)
    (config / "platform/application.properties").write_text("server.ssl.enabled=true\n")
    result = _run(config)
    assert result.returncode == 0, result.stderr
    return config / "tls"


def test_it_separates_public_material_from_signing_material(tmp_path):
    tls = _fresh(tmp_path)
    assert (tls / "public/ca.crt").is_file()
    assert (tls / "public/platform.crt").is_file()
    assert not (tls / "public/ca.key").exists(), "公开视图里出现了私钥"
    assert not (tls / "public/platform.key").exists()
    assert (tls / "platform/platform.p12").is_file()
    # 视图目录必须可遍历（0700），否则容器挂载后读不到。
    assert (tls / "public").stat().st_mode & 0o777 == 0o700
    assert (tls / "platform").stat().st_mode & 0o777 == 0o700


def test_reuse_is_a_no_op_and_keeps_the_gateway_password_in_sync(tmp_path):
    tls = _fresh(tmp_path)
    before = (tls / "platform.crt").read_bytes()
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "已有可用材料" in result.stdout
    assert (tls / "platform.crt").read_bytes() == before, "复用路径不该重新签发"
    password = (tls / "password").read_text().strip()
    props = (tmp_path / "platform/application.properties").read_text()
    assert f"server.ssl.key-store-password={password}" in props


def test_a_stale_public_view_is_refreshed(tmp_path):
    """根材料换了、视图还是旧的：只按「存在」检查会让容器继续用旧证书。"""
    tls = _fresh(tmp_path)
    # 用一个**别的**证书替换 public 视图，模拟陈旧副本。
    other = tmp_path / "other.crt"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "2",
            "-keyout",
            str(tmp_path / "other.key"),
            "-out",
            str(other),
            "-subj",
            "/CN=other",
        ],
        check=True,
        capture_output=True,
    )
    shutil.copyfile(other, tls / "public/platform.crt")
    assert (tls / "public/platform.crt").read_bytes() != (tls / "platform.crt").read_bytes()

    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    assert (tls / "public/platform.crt").read_bytes() == (tls / "platform.crt").read_bytes()


def test_renewal_leaves_the_views_traversable(tmp_path):
    """重建路径不能把 public/ 与 platform/ 一起 chmod 600 掉。"""
    tls = _fresh(tmp_path)
    # 破坏根材料以强制重建。
    (tls / "ca.crt").unlink()

    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    assert (tls / "public").stat().st_mode & 0o777 == 0o700
    assert (tls / "platform").stat().st_mode & 0o777 == 0o700
    assert (tls / "public/ca.crt").is_file()


def test_renewal_syncs_the_platform_password(tmp_path):
    tls = _fresh(tmp_path)
    props = tmp_path / "platform/application.properties"
    props.chmod(0o600)
    props.write_text("server.ssl.key-store-password=stale\n")
    props.chmod(0o400)
    (tls / "ca.crt").unlink()

    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    password = (tls / "password").read_text().strip()
    assert f"server.ssl.key-store-password={password}" in props.read_text()


def test_duplicate_password_lines_are_collapsed_even_when_one_matches(tmp_path):
    """Java properties 后者覆盖前者：某一行匹配不等于生效值匹配。"""
    tls = _fresh(tmp_path)
    props = tmp_path / "platform/application.properties"
    password = (tls / "password").read_text().strip()
    props.chmod(0o600)
    props.write_text(f"server.ssl.key-store-password={password}\nserver.ssl.key-store-password=old\n")

    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    lines = [
        line for line in props.read_text().splitlines() if line.startswith("server.ssl.key-store-password=")
    ]
    assert lines == [f"server.ssl.key-store-password={password}"]
