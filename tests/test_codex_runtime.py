import json
import os
from pathlib import Path

import pytest

from aiops_diagnostics.codex_launcher import _clean_environment
from aiops_diagnostics.codex_runtime import (
    prepare_runtime_home,
    resolve_provider_api_key,
    runtime_config,
)
from aiops_diagnostics.config import (
    AgentSettings,
    canonical_provider_base_url,
    require_same_provider_base_url,
)


def test_clean_codex_environment_drops_business_credentials() -> None:
    source = {
        "HOME": "/home/claude",
        "PATH": "/usr/bin",
        "CODEX_HOME": "/tmp/codex-home",
        "AIOPS_MYSQL_PASSWORD": "mysql-secret",
        "OPENAI_API_KEY": "api-secret",
        "AIOPS_CODEX_PROVIDER_KEY": "provider-secret",
        "HTTPS_PROXY": "http://proxy.example:3128",
    }

    clean = _clean_environment(source)

    assert clean["CODEX_HOME"] == "/tmp/codex-home"
    assert clean["HTTPS_PROXY"] == "http://proxy.example:3128"
    assert clean["AIOPS_CODEX_PROVIDER_KEY"] == "provider-secret"
    assert "AIOPS_MYSQL_PASSWORD" not in clean
    assert "OPENAI_API_KEY" not in clean
    assert "mysql-secret" not in json.dumps(clean)


def test_prepare_runtime_home_writes_api_provider_profile(tmp_path: Path) -> None:
    runtime_home = tmp_path / "runtime"
    settings = AgentSettings(
        codex_bin="/bin/true",
        codex_runtime_home=str(runtime_home),
        api_base_url="https://proxy.example/",
        key_dir=str(tmp_path / "keys"),
    )

    prepared = prepare_runtime_home(settings)

    assert prepared == runtime_home
    assert not (runtime_home / "auth.json").exists()
    config = (runtime_home / "config.toml").read_text()
    assert config == runtime_config(settings)
    assert 'default_permissions = "aiops-diagnostic"' in config
    assert 'model_provider = "aiops-api"' in config
    assert 'base_url = "https://proxy.example/"' in config
    assert 'env_key = "AIOPS_CODEX_PROVIDER_KEY"' in config
    assert '"/usr/bin/true" = "read"' in config
    assert "multi_agent = false" in config
    assert '"state.json" = "deny"' in config
    assert '".inputs/**" = "deny"' in config
    assert 'metrics_exporter = "none"' in config
    assert stat_mode(runtime_home) == 0o700
    assert stat_mode(runtime_home / "config.toml") == 0o600


def test_provider_key_slots_are_private_and_pluggable(tmp_path: Path) -> None:
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    key_dir.chmod(0o700)
    primary = key_dir / "primary.key"
    backup = key_dir / "backup.key"
    primary.write_text("primary-secret", encoding="utf-8")
    backup.write_text("backup-secret", encoding="utf-8")
    primary.chmod(0o600)
    backup.chmod(0o600)
    settings = AgentSettings(codex_bin="/bin/true", key_dir=str(key_dir), key_slot="primary")

    assert resolve_provider_api_key(settings) == "primary-secret"
    settings.key_slot = "backup"
    assert resolve_provider_api_key(settings) == "backup-secret"


def test_explicit_provider_key_symlink_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.key"
    target.write_text("private-secret", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "linked.key"
    link.symlink_to(target)

    with pytest.raises(RuntimeError, match="符号链接"):
        resolve_provider_api_key(
            AgentSettings(codex_bin="/bin/true", api_key_file=str(link), key_dir=str(tmp_path / "keys"))
        )


def test_provider_key_slot_directory_must_be_private(tmp_path: Path) -> None:
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    key_dir.chmod(0o750)
    key = key_dir / "default.key"
    key.write_text("private-secret", encoding="utf-8")
    key.chmod(0o600)

    with pytest.raises(RuntimeError, match="目录权限过宽"):
        resolve_provider_api_key(
            AgentSettings(codex_bin="/bin/true", key_dir=str(key_dir), key_slot="default")
        )


def test_provider_base_url_is_canonical_and_resume_cannot_redirect_key() -> None:
    assert canonical_provider_base_url("https://api.example/v1") == "https://api.example/v1/"
    assert (
        require_same_provider_base_url("https://api.example/v1/", "https://api.example/v1")
        == "https://api.example/v1/"
    )
    with pytest.raises(ValueError, match="只允许切换"):
        require_same_provider_base_url("https://api.example/v1/", "https://attacker.example/v1/")
    with pytest.raises(ValueError, match="认证信息"):
        canonical_provider_base_url("https://user:secret@api.example/v1/")


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
