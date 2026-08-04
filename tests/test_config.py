import pytest

from aiops_diagnostics.config import Settings, SSHSettings


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


def test_ssh_rejects_option_like_usernames() -> None:
    settings = SSHSettings(enabled=True, host="example.test", user="-oProxyCommand", key_file="missing")

    with pytest.raises(ValueError, match="连字符"):
        settings.validate()
