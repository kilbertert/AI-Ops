"""网络与凭据边界的回归：平台必须走**真 HTTPS**，生产配置只能以白名单子集进入。

两条都来自审查：`snail_base_url` 走回环中继等于把「远端必须是 HTTPS」的校验凑出来；
挂整份 `production.env` 等于让试运行实例继承生产里所有未覆盖的开关与凭据。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

DEPLOY = Path(__file__).resolve().parents[1] / "deploy/m2-live-03"


def test_platform_url_is_https_and_not_a_loopback_relay():
    template = (DEPLOY / "gateway.env.template").read_text(encoding="utf-8")
    assert "M2LIVE_PLATFORM_URL=https://platform:8900" in template

    compose_text = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
    services = yaml.safe_load(compose_text)["services"]["gateway"]
    command = " ".join(services.get("command", []))
    assert "loopback_relay" not in command, "正式启动仍在用回环中继转发平台流量"
    assert "ca-bundle.crt" in compose_text, "网关没有信任拼接后的信任库"
    assert "SSL_CERT_FILE" in compose_text, "网关没有设置信任库变量"


def test_platform_is_configured_for_tls_not_plain_http():
    text = (DEPLOY / "make-certs.sh").read_text(encoding="utf-8")
    assert "serverAuth" in text and "DNS:platform" in text
    # CA 必须带 keyUsage，否则 OpenSSL 3 在链校验时拒绝（实测过一次）。
    assert "keyCertSign" in text


def test_gateway_does_not_mount_the_whole_production_config():
    compose_text = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
    assert "production.env:/etc/aiops-41/production.env:ro" not in compose_text
    assert "runtime.env:/etc/m2live/runtime.env:ro" in compose_text


def test_runtime_whitelist_takes_only_read_only_dependencies(tmp_path):
    source = tmp_path / "production.env"
    source.write_text(
        "AIOPS_REDIS_HOST=127.0.0.1\n"
        "AIOPS_REDIS_PORT=6379\n"
        "AIOPS_MYSQL_HOST=10.0.0.1\n"
        "AIOPS_MYSQL_PORT=3306\n"
        "AIOPS_MYSQL_USER=u\nAIOPS_MYSQL_PASSWORD=p\nAIOPS_MYSQL_DATABASE=db\n"
        "AIOPS_GATEWAY_THIRD_SESSION_SERVICE_TOKEN=t\n"
        "AIOPS_GATEWAY_JEV_BASE_URL=https://should-not-copy/\n"
        "AIOPS_PROVIDER_BAOYUN_KEY_SLOT=slot\n"
        "AIOPS_GATEWAY_MEDIA_SIGNING_SECRET=no\n",
        encoding="utf-8",
    )
    target = tmp_path / "runtime.env"
    result = subprocess.run(
        ["bash", str(DEPLOY / "build-runtime-config.sh"), str(source), str(target)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    written = target.read_text(encoding="utf-8")
    assert "AIOPS_REDIS_HOST=127.0.0.1" in written
    assert "AIOPS_GATEWAY_THIRD_SESSION_SERVICE_TOKEN=t" in written
    for excluded in ("JEV_BASE_URL", "PROVIDER_BAOYUN", "MEDIA_SIGNING_SECRET"):
        assert excluded not in written, f"{excluded} 不应进入试运行配置"
    assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_runtime_whitelist_refuses_incomplete_input(tmp_path):
    source = tmp_path / "production.env"
    source.write_text("AIOPS_REDIS_HOST=127.0.0.1\n", encoding="utf-8")
    target = tmp_path / "runtime.env"
    result = subprocess.run(
        ["bash", str(DEPLOY / "build-runtime-config.sh"), str(source), str(target)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert not target.exists(), "缺必需键时不应留下半个配置文件"


def test_the_trust_store_keeps_public_cas():
    """SSL_CERT_FILE 是替换而不是追加：只放本栈 CA 会让 CPMS/UPMS 全部失败。"""
    text = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
    assert "/etc/ssl/certs/ca-certificates.crt" in text, "没有拼接系统信任库"
    assert "ca-bundle.crt" in text


def test_certificate_reuse_requires_a_valid_unexpired_cert():
    text = (DEPLOY / "make-certs.sh").read_text(encoding="utf-8")
    assert "-checkend" in text, "复用前没有检查剩余有效期"
    assert "openssl verify -CAfile" in text, "复用前没有验证签发链"


def test_runtime_whitelist_does_not_carry_ssh(tmp_path):
    source = tmp_path / "production.env"
    source.write_text(
        "AIOPS_REDIS_HOST=127.0.0.1\nAIOPS_REDIS_PORT=6379\n"
        "AIOPS_MYSQL_HOST=h\nAIOPS_MYSQL_PORT=3306\nAIOPS_MYSQL_USER=u\n"
        "AIOPS_MYSQL_PASSWORD=p\nAIOPS_MYSQL_DATABASE=db\n"
        "AIOPS_GATEWAY_THIRD_SESSION_SERVICE_TOKEN=t\nAIOPS_SSH_ENABLED=true\n",
        encoding="utf-8",
    )
    target = tmp_path / "runtime.env"
    result = subprocess.run(
        ["bash", str(DEPLOY / "build-runtime-config.sh"), str(source), str(target)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "AIOPS_SSH_ENABLED" not in target.read_text(encoding="utf-8"), "SSH 开关进入了试运行"


def test_runtime_whitelist_requires_business_credentials(tmp_path):
    source = tmp_path / "production.env"
    source.write_text(
        "AIOPS_REDIS_HOST=127.0.0.1\nAIOPS_REDIS_PORT=6379\n"
        "AIOPS_MYSQL_HOST=h\nAIOPS_MYSQL_PORT=3306\n"
        "AIOPS_GATEWAY_THIRD_SESSION_SERVICE_TOKEN=t\n",
        encoding="utf-8",
    )
    target = tmp_path / "runtime.env"
    result = subprocess.run(
        ["bash", str(DEPLOY / "build-runtime-config.sh"), str(source), str(target)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert not target.exists()


def test_the_gateway_never_sees_signing_material():
    """网关只需要验证平台，不需要签名能力：私钥与 keystore 不能出现在它的挂载里。"""
    compose_text = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
    gateway = compose_text.split("  gateway:", 1)[1].split("\nsecrets:", 1)[0]
    assert "/tls/public" in gateway, "网关没有挂公开证书目录"
    assert "/tls:" not in gateway and "/tls/platform" not in gateway, "网关挂到了含私钥的目录"
    platform = compose_text.split("  platform:", 1)[1].split("  node:", 1)[0]
    assert "/tls/platform" in platform
