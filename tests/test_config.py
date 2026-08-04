import os
from pathlib import Path

import pytest

from aiops_diagnostics.config import Settings, SSHSettings
from aiops_diagnostics.private_files import write_private_text


def test_redacted_config_never_returns_password(monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_MYSQL_PASSWORD", "mysql-secret")
    monkeypatch.setenv("AIOPS_TDENGINE_PASSWORD", "td-secret")
    monkeypatch.setenv("AIOPS_REDIS_PASSWORD", "redis-secret")
    monkeypatch.setenv("AIOPS_TDENGINE_URL", "http://url-user:url-secret@127.0.0.1:16041")
    redacted = Settings.from_env().redacted()
    rendered = str(redacted)
    assert "mysql-secret" not in rendered
    assert "td-secret" not in rendered
    assert "redis-secret" not in rendered
    assert "url-user" not in rendered
    assert "url-secret" not in rendered
    assert redacted["tdengine"]["url"] == "http://REDACTED@127.0.0.1:16041"


def test_safety_limits_cannot_be_overridden_to_unbounded_values(monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_TDENGINE_MAX_ROWS", "2001")
    with pytest.raises(ValueError, match="tdengine_max_rows"):
        Settings.from_env()


def test_tdengine_defaults_use_read_only_proxy() -> None:
    settings = Settings.from_env()

    assert settings.tdengine.url == "http://127.0.0.1:16041"
    assert settings.ssh.tdengine_port == 16041


def test_agent_default_uses_sdk_pinned_codex_runtime(monkeypatch) -> None:
    from codex_cli_bin import bundled_codex_path

    monkeypatch.delenv("AIOPS_CODEX_BIN", raising=False)

    assert Settings.from_env().agent.codex_bin == str(bundled_codex_path())


def test_heartbeat_interval_is_bounded(monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_AGENT_HEARTBEAT_INTERVAL_SECONDS", "0")

    settings = Settings.from_env()
    with pytest.raises(ValueError, match="heartbeat_interval_seconds"):
        settings.agent.validate()


def test_ssh_rejects_option_like_usernames() -> None:
    settings = SSHSettings(enabled=True, host="example.test", user="-oProxyCommand", key_file="missing")

    with pytest.raises(ValueError, match="连字符"):
        settings.validate()


def test_private_config_file_loads_values_but_environment_wins(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "production.env"
    write_private_text(
        config,
        "AIOPS_MYSQL_HOST=from-file\nAIOPS_MYSQL_PORT=3307\nAIOPS_MYSQL_PASSWORD=abc#def\nAIOPS_CODEX_BIN=\n",
    )
    monkeypatch.setenv("AIOPS_MYSQL_HOST", "from-environment")

    settings = Settings.from_config(config)

    assert settings.mysql.host == "from-environment"
    assert settings.mysql.port == 3307
    assert settings.mysql.password == "abc#def"
    assert settings.agent.codex_bin


def test_windows_style_home_override_is_platform_independent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_HOME", str(tmp_path / "portable-home"))
    monkeypatch.delenv("AIOPS_CONFIG_HOME", raising=False)
    monkeypatch.delenv("AIOPS_DATA_HOME", raising=False)

    settings = Settings.from_env()

    assert Path(settings.agent.key_dir) == tmp_path / "portable-home" / "keys"
    assert Path(settings.agent.codex_runtime_home) == tmp_path / "portable-home" / "codex-home"
    assert Path(settings.agent.run_root) == tmp_path / "portable-home" / "runs"
    assert settings.agent.windows_sandbox == "unelevated"
    if os.name != "nt":
        assert settings.ssh.ssh_bin
