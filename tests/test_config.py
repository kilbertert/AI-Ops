import os
from pathlib import Path

import pytest

from aiops_diagnostics.config import DisSettings, ProviderConfig, Settings, SSHSettings, UpmsSettings
from aiops_diagnostics.gateway_config import GatewayServerSettings
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


def test_gateway_reads_third_session_token_from_private_server_config(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "gateway.env"
    write_private_text(
        config,
        "AIOPS_GATEWAY_THIRD_SESSION_SERVICE_TOKEN=service-token\n"
        "AIOPS_GATEWAY_THIRD_SESSION_KEY_PREFIX=app:test-session:\n",
    )
    monkeypatch.setenv("AIOPS_GATEWAY_SERVER_CONFIG_FILE", str(config))
    monkeypatch.delenv("AIOPS_GATEWAY_THIRD_SESSION_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("AIOPS_GATEWAY_THIRD_SESSION_KEY_PREFIX", raising=False)

    settings = GatewayServerSettings.from_env()

    assert settings.third_session_service_token == "service-token"
    assert settings.third_session_key_prefix == "app:test-session:"


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


def _provider_env(monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_PROVIDERS", "glm-ark,gpt-psydo")
    monkeypatch.setenv("AIOPS_DEFAULT_PROVIDER", "glm-ark")
    monkeypatch.setenv("AIOPS_PROVIDER_GLM_ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/coding/v3/")
    monkeypatch.setenv("AIOPS_PROVIDER_GLM_ARK_MODEL", "glm-5-2-260617")
    monkeypatch.setenv("AIOPS_PROVIDER_GLM_ARK_KEY_ENV", "AIOPS_PROVIDER_GLM_ARK_KEY")
    monkeypatch.setenv("AIOPS_PROVIDER_GPT_PSYDO_BASE_URL", "https://api.psydo.top/")
    monkeypatch.setenv("AIOPS_PROVIDER_GPT_PSYDO_MODEL", "gpt-5")


def test_provider_registry_parses_from_env(monkeypatch) -> None:
    _provider_env(monkeypatch)
    settings = Settings.from_env()
    names = settings.agent.provider_names()
    assert names == ("glm-ark", "gpt-psydo")

    glm = settings.agent.select_provider("glm-ark")
    assert glm.base_url == "https://ark.cn-beijing.volces.com/api/coding/v3/"
    assert glm.model == "glm-5-2-260617"
    assert glm.api_key_env == "AIOPS_PROVIDER_GLM_ARK_KEY"
    assert glm.wire_api == "responses"
    assert glm.resolved_key_slot() == "glm-ark"

    psydo = settings.agent.select_provider("gpt-psydo")
    assert psydo.base_url == "https://api.psydo.top/"
    assert psydo.model == "gpt-5"
    assert psydo.resolved_key_slot() == "gpt-psydo"


def test_default_provider_selected_when_name_omitted(monkeypatch) -> None:
    _provider_env(monkeypatch)
    settings = Settings.from_env()
    assert settings.agent.select_provider(None).name == "glm-ark"


def test_default_provider_defaults_to_first_when_unset(monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_PROVIDERS", "glm-ark,gpt-psydo")
    monkeypatch.setenv("AIOPS_PROVIDER_GLM_ARK_BASE_URL", "https://ark.example/")
    monkeypatch.setenv("AIOPS_PROVIDER_GPT_PSYDO_BASE_URL", "https://psydo.example/")
    settings = Settings.from_env()
    assert settings.agent.default_provider == "glm-ark"


def test_select_provider_rejects_unknown_name(monkeypatch) -> None:
    _provider_env(monkeypatch)
    settings = Settings.from_env()
    with pytest.raises(ValueError, match="未知"):
        settings.agent.select_provider("unknown-provider")


def test_registry_rejects_duplicate_and_missing_base_url(monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_PROVIDERS", "glm-ark,glm-ark")
    monkeypatch.setenv("AIOPS_PROVIDER_GLM_ARK_BASE_URL", "https://ark.example/")
    settings = Settings.from_env()
    with pytest.raises(ValueError, match="重复"):
        settings.agent.validate()

    monkeypatch.setenv("AIOPS_PROVIDERS", "glm-ark")
    monkeypatch.delenv("AIOPS_PROVIDER_GLM_ARK_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="缺少"):
        Settings.from_env()


def test_legacy_single_provider_synthesized_without_registry() -> None:
    from aiops_diagnostics.config import AgentSettings

    settings = AgentSettings(codex_bin="codex", api_base_url="https://proxy.example/", model="gpt-5")
    provider = settings.select_provider(None)
    assert provider.name == "aiops-api"
    assert provider.base_url == "https://proxy.example/"
    assert provider.model == "gpt-5"
    assert provider.api_key_env == settings.api_key_env


def test_registry_validation_requires_default_in_list(monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_PROVIDERS", "glm-ark,gpt-psydo")
    monkeypatch.setenv("AIOPS_DEFAULT_PROVIDER", "missing")
    monkeypatch.setenv("AIOPS_PROVIDER_GLM_ARK_BASE_URL", "https://ark.example/")
    monkeypatch.setenv("AIOPS_PROVIDER_GPT_PSYDO_BASE_URL", "https://psydo.example/")
    settings = Settings.from_env()
    with pytest.raises(ValueError, match="不在注册表"):
        settings.agent.validate()


def test_provider_config_resolved_key_slot_falls_back_to_name() -> None:
    provider = ProviderConfig(name="glm-ark", base_url="https://ark.example/")
    assert provider.resolved_key_slot() == "glm-ark"
    explicit = ProviderConfig(name="glm-ark", base_url="https://ark.example/", default_key_slot="primary")
    assert explicit.resolved_key_slot() == "primary"


def test_http_settings_defaults_and_env_names(monkeypatch) -> None:
    settings = Settings.from_env()
    assert settings.http.base_url is None
    assert settings.http.internal_token.secret is None
    assert settings.http.internal_token.expire_seconds == 300
    assert settings.diag_api is settings.http

    monkeypatch.setenv("AIOPS_HTTP_BASE_URL", "https://diag.example.test")
    monkeypatch.setenv("AIOPS_HTTP_INTERNAL_TOKEN_SECRET", "http-secret")
    monkeypatch.setenv("AIOPS_HTTP_INTERNAL_TOKEN_EXPIRE_SECONDS", "120")
    settings = Settings.from_env()
    assert settings.http.base_url == "https://diag.example.test"
    assert settings.http.internal_token.secret == "http-secret"
    assert settings.http.internal_token.expire_seconds == 120


def test_http_settings_internal_token_is_redacted() -> None:
    settings = Settings.from_env()
    settings.http.internal_token.secret = "http-secret"
    redacted = settings.redacted()
    assert redacted["http"]["internal_token"]["secret"] == "REDACTED"
    assert redacted["diag_api"]["token_secret"] == "REDACTED"
    assert "http-secret" not in str(redacted)


def test_http_settings_new_env_names_override_legacy_names(monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_HTTP_BASE_URL", "https://http.example.test")
    monkeypatch.setenv("AIOPS_DIAG_API_BASE_URL", "https://legacy.example.test")
    monkeypatch.setenv("AIOPS_HTTP_INTERNAL_TOKEN_SECRET", "http-secret")
    monkeypatch.setenv("AIOPS_DIAG_API_TOKEN_SECRET", "legacy-secret")
    monkeypatch.setenv("AIOPS_HTTP_INTERNAL_TOKEN_EXPIRE_SECONDS", "120")
    monkeypatch.setenv("AIOPS_DIAG_API_TOKEN_EXPIRE_SECONDS", "300")
    monkeypatch.setenv("AIOPS_HTTP_TIMEOUT_SECONDS", "12")
    monkeypatch.setenv("AIOPS_DIAG_API_TIMEOUT_SECONDS", "24")

    settings = Settings.from_env()

    assert settings.http.base_url == "https://http.example.test"
    assert settings.http.internal_token.secret == "http-secret"
    assert settings.http.internal_token.expire_seconds == 120
    assert settings.http.timeout_seconds == 12


def test_http_settings_legacy_env_names_still_work(monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DIAG_API_BASE_URL", "https://legacy.example.test")
    monkeypatch.setenv("AIOPS_DIAG_API_TOKEN_SECRET", "legacy-secret")
    monkeypatch.setenv("AIOPS_DIAG_API_TOKEN_EXPIRE_SECONDS", "180")
    monkeypatch.setenv("AIOPS_DIAG_API_TIMEOUT_SECONDS", "12")

    settings = Settings.from_env()

    assert settings.http.base_url == "https://legacy.example.test"
    assert settings.http.internal_token.secret == "legacy-secret"
    assert settings.http.internal_token.expire_seconds == 180
    assert settings.http.timeout_seconds == 12


def test_http_settings_token_expire_setter_still_validates() -> None:
    settings = Settings.from_env()

    settings.http.token_expire_seconds = 600
    assert settings.http.token_expire_seconds == 600
    settings.http.token_expire_seconds = 300
    assert settings.http.token_expire_seconds == 300

    with pytest.raises(ValueError, match="令牌有效期"):
        settings.http.token_expire_seconds = 601


def test_upms_settings_defaults_and_env_names(monkeypatch) -> None:
    settings = Settings.from_env()
    assert settings.upms.base_url is None
    assert settings.upms.inside_token is None
    assert settings.upms.timeout_seconds == 8

    monkeypatch.setenv("AIOPS_UPMS_BASE_URL", "https://upms.example.test")
    monkeypatch.setenv("AIOPS_UPMS_TIMEOUT_SECONDS", "12")
    settings = Settings.from_env()
    assert settings.upms.base_url == "https://upms.example.test"
    assert settings.upms.timeout_seconds == 12
    assert settings.upms.base_url in str(settings.redacted())


def test_upms_inside_token_is_redacted(monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_UPMS_BASE_URL", "https://upms.example.test")
    monkeypatch.setenv("AIOPS_UPMS_INSIDE_TOKEN", "upms-inside-token")
    settings = Settings.from_env()

    assert settings.upms.inside_token == "upms-inside-token"
    redacted = str(settings.redacted())
    assert "upms-inside-token" not in redacted
    assert redacted.count("REDACTED") >= 1


def test_upms_settings_rejects_credential_bearing_or_unbounded_values() -> None:
    with pytest.raises(ValueError, match="UPMS base_url"):
        UpmsSettings(base_url="https://user:pass@upms.example.test")
    with pytest.raises(ValueError, match="UPMS base_url"):
        UpmsSettings(base_url="not-a-url")
    with pytest.raises(ValueError, match="UPMS 超时"):
        UpmsSettings(timeout_seconds=0)
    with pytest.raises(ValueError, match="UPMS 超时"):
        UpmsSettings(timeout_seconds=61)


def test_dis_settings_defaults_and_env_names(monkeypatch) -> None:
    settings = Settings.from_env()
    assert settings.dis.base_url is None
    assert settings.dis.token is None
    assert settings.dis.timeout_seconds == 8

    monkeypatch.setenv("AIOPS_DIS_BASE_URL", "https://dis.example.test")
    monkeypatch.setenv("AIOPS_DIS_TOKEN", "dis-service-token")
    monkeypatch.setenv("AIOPS_DIS_TIMEOUT_SECONDS", "12")
    settings = Settings.from_env()
    assert settings.dis.base_url == "https://dis.example.test"
    assert settings.dis.token == "dis-service-token"
    assert settings.dis.timeout_seconds == 12
    redacted = str(settings.redacted())
    assert "dis-service-token" not in redacted
    assert "REDACTED" in redacted


def test_dis_settings_rejects_credential_bearing_or_unbounded_values() -> None:
    with pytest.raises(ValueError, match="Dis base_url"):
        DisSettings(base_url="https://user:pass@dis.example.test")
    with pytest.raises(ValueError, match="Dis base_url"):
        DisSettings(base_url="not-a-url")
    with pytest.raises(ValueError, match="Dis 超时"):
        DisSettings(timeout_seconds=0)
    with pytest.raises(ValueError, match="Dis 超时"):
        DisSettings(timeout_seconds=61)
