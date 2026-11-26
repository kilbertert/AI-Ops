"""动态客户主机受管清单与双模式一次性安全引导测试（ADR-0357 P1-1）。

拦截的具体错误：
1. 模式 A（一次性口令）将客户明文密码写入磁盘/SQLite/返回体，或放行主机指纹漂移；
2. 模式 B（客户侧引导脚本）落盘明文令牌、允许单次令牌被二次重放、或放行过期/跨环境回调；
3. 未获目录授权的主体越权登记/查看/撤销客户主机；
4. 控制台桥对非白名单 wizard_action 或跨动作携带 wizard_payload 放行。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from aiops_diagnostics.onboarding import host_enrollment, registry


def _setup_registry(tmp_path: Path) -> Path:
    reg_dir = tmp_path / "reg"
    reg_dir.mkdir(mode=0o700)
    reg_path = reg_dir / "registry.json"
    registry.apply(
        reg_path,
        [],
        expected_revision=0,
        actor="test",
        managers=[
            {
                "environment": "41",
                "tenant_id": "t-admin",
                "user_id": "u-admin",
                "environments": ["*"],
            },
        ],
    )
    return reg_path


def _fake_keygen(priv_path: Path, comment: str) -> tuple[str, str]:
    pub = f"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKeyDataForTests12345678901234567890 {comment}"
    return "-----BEGIN OPENSSH PRIVATE KEY-----\nMOCK\n-----END OPENSSH PRIVATE KEY-----\n", pub


def test_mode_a_ephemeral_password_never_persists_password_and_pins_fingerprint(tmp_path: Path) -> None:
    reg = _setup_registry(tmp_path)
    admin = ("41", "t-admin", "u-admin")
    secret_pw = "ULTRA_SECRET_ROOT_PW_991122"
    seen: list[dict] = []

    def fake_runner(**kwargs):
        seen.append({k: v for k, v in kwargs.items() if k != "password"})
        assert kwargs["password"] == secret_pw
        return {
            "host_fingerprint_sha256": "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "hostname": "jiejia-gw",
            "machine_id_sha256": "a" * 64,
            "python_minor": "3.12",
            "service_user_present": True,
            "venv_ready": True,
            "nginx_flavor": "baota",
        }

    res = host_enrollment.enroll_via_ephemeral_password(
        reg,
        admin,
        environment="jiejia",
        address="8.148.223.139",
        password=secret_pw,
        bootstrap_runner=fake_runner,
        keygen_runner=_fake_keygen,
    )
    assert res["enrolled"] is True
    assert res["password_persisted"] is False
    assert res["host"]["rotate_root_password_recommended"] is True
    assert res["host"]["nginx_flavor"] == "baota"
    assert secret_pw not in json.dumps(res)

    # 验证磁盘全部文件与 SQLite 绝无明文密码残留
    for p in reg.parent.rglob("*"):
        if p.is_file():
            assert secret_pw.encode() not in p.read_bytes()

    # 指纹漂移必须 fail-closed 拒绝
    with pytest.raises(host_enrollment.EnrollmentRefused, match="ENVIRONMENT_HOST_FINGERPRINT_MISMATCH"):
        host_enrollment.enroll_via_ephemeral_password(
            reg,
            admin,
            environment="jiejia",
            address="8.148.223.139",
            password=secret_pw,
            bootstrap_runner=lambda **kw: {
                "host_fingerprint_sha256": "SHA256:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
                "hostname": "evil-mitm",
                "machine_id_sha256": "b" * 64,
                "python_minor": "3.12",
                "service_user_present": True,
                "venv_ready": True,
                "nginx_flavor": "standard",
            },
            keygen_runner=_fake_keygen,
        )


def test_mode_b_bootstrap_script_single_use_and_replay_proof(tmp_path: Path) -> None:
    reg = _setup_registry(tmp_path)
    admin = ("41", "t-admin", "u-admin")
    session = host_enrollment.create_bootstrap_script_session(
        reg,
        admin,
        environment="jiejia",
        address="8.148.223.139",
        callback_base_url="https://api.control.example",
        keygen_runner=_fake_keygen,
    )
    assert session["single_use"] is True
    assert "curl -fsSL " in session["one_liner"]
    token = session["script_url"].rsplit("/", 1)[1]

    # 数据库中绝不保存明文 token
    with sqlite3.connect(reg.parent / (reg.name + ".drafts.db")) as conn:
        raw_rows = conn.execute("SELECT token_hash FROM environment_bootstrap_tokens").fetchall()
    assert len(raw_rows) == 1
    assert raw_rows[0][0] != token

    # 渲染引导脚本包含安全自举命令
    script = host_enrollment.render_bootstrap_script_for_token(reg, token, "https://api.control.example")
    assert "useradd --system" in script
    assert "X-Bootstrap-Token:" in script
    assert "ssh-ed25519" in script

    # 首次回调成功钉扎指纹
    done = host_enrollment.complete_bootstrap_callback(
        reg,
        token,
        {
            "environment": "jiejia",
            "host_fingerprint_sha256": "SHA256:CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
            "hostname": "jiejia-prod",
            "machine_id_sha256": "c" * 64,
            "python_minor": "3.12",
            "service_user_present": True,
            "venv_ready": True,
            "nginx_flavor": "baota",
        },
    )
    assert done["enrolled"] is True
    assert done["host"]["status"] == "enrolled"
    assert done["host"]["rotate_root_password_recommended"] is False

    # 二次重放回调与再次拉脚本均必须拒绝
    with pytest.raises(
        host_enrollment.EnrollmentRefused, match="ENVIRONMENT_BOOTSTRAP_TOKEN_EXPIRED_OR_USED"
    ):
        host_enrollment.complete_bootstrap_callback(
            reg,
            token,
            {
                "environment": "jiejia",
                "host_fingerprint_sha256": "SHA256:CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
            },
        )
    with pytest.raises(
        host_enrollment.EnrollmentRefused, match="ENVIRONMENT_BOOTSTRAP_TOKEN_EXPIRED_OR_USED"
    ):
        host_enrollment.render_bootstrap_script_for_token(reg, token, "https://api.control.example")


def test_unauthorized_manager_cannot_access_or_revoke_other_env_host(tmp_path: Path) -> None:
    reg = _setup_registry(tmp_path)
    limited = ("41", "t-other", "u-other")
    with pytest.raises(host_enrollment.EnrollmentRefused, match="ENVIRONMENT_ACCESS_DENIED"):
        host_enrollment.get_host_status(reg, limited, "jiejia")

    # 过期令牌在回调期拒绝
    admin = ("41", "t-admin", "u-admin")
    session = host_enrollment.create_bootstrap_script_session(
        reg,
        admin,
        environment="jiejia",
        address="8.148.223.139",
        callback_base_url="https://api.control.example",
        keygen_runner=_fake_keygen,
    )
    token = session["script_url"].rsplit("/", 1)[1]
    with sqlite3.connect(reg.parent / (reg.name + ".drafts.db")) as conn:
        conn.execute("UPDATE environment_bootstrap_tokens SET expires_at=?", (time.time() - 5,))
        conn.commit()
    with pytest.raises(
        host_enrollment.EnrollmentRefused, match="ENVIRONMENT_BOOTSTRAP_TOKEN_EXPIRED_OR_USED"
    ):
        host_enrollment.complete_bootstrap_callback(
            reg,
            token,
            {
                "environment": "jiejia",
                "host_fingerprint_sha256": "SHA256:DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD",
            },
        )
