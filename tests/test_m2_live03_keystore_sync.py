"""keystore 口令同步：重新签发证书后平台配置必须跟着改。

不同步的表现是平台起不来（"keystore password was incorrect"），而文件看起来全对 ——
这类「文件对、链路错」的形状必须由脚本消除，不能靠人记得。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "deploy/m2-live-03/sync-keystore-password.py"


def _module():
    spec = importlib.util.spec_from_file_location("sync_keystore", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_it_replaces_the_password_and_keeps_other_keys(tmp_path):
    props = tmp_path / "application.properties"
    props.write_text(
        "spring.datasource.url=jdbc:mysql://store/snail_ai\n"
        "server.ssl.key-store-password=old\n"
        "server.ssl.enabled=true\n"
    )
    password = tmp_path / "password"
    password.write_text("new-secret\n")

    _module().sync(props, password)

    text = props.read_text()
    assert "server.ssl.key-store-password=new-secret" in text
    assert "old" not in text
    assert "spring.datasource.url=" in text and "server.ssl.enabled=true" in text


def test_it_is_idempotent(tmp_path):
    props = tmp_path / "application.properties"
    props.write_text("server.ssl.key-store-password=old\n")
    password = tmp_path / "password"
    password.write_text("same\n")
    module = _module()
    module.sync(props, password)
    first = props.read_text()
    module.sync(props, password)
    assert props.read_text() == first


def test_the_file_stays_private(tmp_path):
    props = tmp_path / "application.properties"
    props.write_text("server.ssl.key-store-password=old\n")
    password = tmp_path / "password"
    password.write_text("x\n")
    _module().sync(props, password)
    assert props.stat().st_mode & 0o777 == 0o600


def test_a_file_without_ssl_settings_gains_exactly_one_line(tmp_path):
    props = tmp_path / "application.properties"
    props.write_text("spring.datasource.url=x\n")
    password = tmp_path / "password"
    password.write_text("pw\n")
    _module().sync(props, password)
    lines = [
        line for line in props.read_text().splitlines() if line.startswith("server.ssl.key-store-password=")
    ]
    assert lines == ["server.ssl.key-store-password=pw"]


def test_a_read_only_properties_file_is_left_untouched_when_already_current(tmp_path):
    """preflight 允许 0400；复用一份有效证书时不需要改文件，也就不该去改。"""
    props = tmp_path / "application.properties"
    password = tmp_path / "password"
    password.write_text("current\n")
    props.write_text("server.ssl.key-store-password=current\n")
    props.chmod(0o400)

    _module().sync(props, password)  # 不能抛 PermissionError

    assert props.read_text() == "server.ssl.key-store-password=current\n"
    assert props.stat().st_mode & 0o777 == 0o400


def test_a_stale_read_only_file_is_still_updated_by_its_owner(tmp_path):
    props = tmp_path / "application.properties"
    password = tmp_path / "password"
    password.write_text("new\n")
    props.write_text("server.ssl.key-store-password=old\n")
    props.chmod(0o400)

    _module().sync(props, password)

    assert "server.ssl.key-store-password=new" in props.read_text()
