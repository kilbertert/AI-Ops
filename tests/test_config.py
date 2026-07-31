import pytest

from aiops_diagnostics.config import Settings


def test_redacted_config_never_returns_password(monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_MYSQL_PASSWORD", "mysql-secret")
    monkeypatch.setenv("AIOPS_TDENGINE_PASSWORD", "td-secret")
    monkeypatch.setenv("AIOPS_REDIS_PASSWORD", "redis-secret")
    redacted = Settings.from_env().redacted()
    rendered = str(redacted)
    assert "mysql-secret" not in rendered
    assert "td-secret" not in rendered
    assert "redis-secret" not in rendered


def test_safety_limits_cannot_be_overridden_to_unbounded_values(monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_TDENGINE_MAX_ROWS", "1000000")
    with pytest.raises(ValueError, match="tdengine_max_rows"):
        Settings.from_env()
