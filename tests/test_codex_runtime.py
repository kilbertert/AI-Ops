import json
import os
import sys
import threading
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiops_diagnostics.codex_launcher import _clean_environment, _execute_codex
from aiops_diagnostics.codex_runtime import (
    SDKCodexSession,
    codex_launch_args,
    prepare_runtime_home,
    resolve_provider_api_key,
    runtime_config,
)
from aiops_diagnostics.config import (
    AgentSettings,
    ProviderConfig,
    canonical_provider_base_url,
    require_same_provider_base_url,
)
from aiops_diagnostics.private_files import ensure_private_directory, write_private_text


def test_clean_codex_environment_drops_business_credentials() -> None:
    source = {
        "HOME": "/home/claude",
        "USERPROFILE": "C:\\Users\\engineer",
        "SYSTEMROOT": "C:\\Windows",
        "LOCALAPPDATA": "C:\\Users\\engineer\\AppData\\Local",
        "PATH": "/usr/bin",
        "CODEX_HOME": "/tmp/codex-home",
        "AIOPS_MYSQL_PASSWORD": "mysql-secret",
        "OPENAI_API_KEY": "api-secret",
        "AIOPS_CODEX_PROVIDER_KEY": "provider-secret",
        "HTTPS_PROXY": "http://proxy.example:3128",
    }

    clean = _clean_environment(source)

    assert clean["CODEX_HOME"] == "/tmp/codex-home"
    assert clean["SYSTEMROOT"] == "C:\\Windows"
    assert clean["LOCALAPPDATA"].endswith("AppData\\Local")
    assert clean["HTTPS_PROXY"] == "http://proxy.example:3128"
    assert clean["AIOPS_CODEX_PROVIDER_KEY"] == "provider-secret"
    assert "AIOPS_MYSQL_PASSWORD" not in clean
    assert "OPENAI_API_KEY" not in clean
    assert "mysql-secret" not in json.dumps(clean)


def test_windows_launcher_uses_child_process_instead_of_execve(monkeypatch) -> None:
    launched: list[tuple[list[str], dict[str, str]]] = []

    def fake_call(command, *, env):
        launched.append((command, env))
        return 23

    monkeypatch.setattr("aiops_diagnostics.codex_launcher.subprocess.call", fake_call)
    environment = {"CODEX_HOME": "C:\\runtime"}

    exit_code = _execute_codex(
        "C:\\bundle\\codex.exe",
        ["--version"],
        environment,
        platform_name="nt",
    )

    assert exit_code == 23
    assert launched == [(["C:\\bundle\\codex.exe", "--version"], environment)]


def test_prepare_runtime_home_writes_api_provider_profile(tmp_path: Path) -> None:
    runtime_home = tmp_path / "runtime"
    settings = AgentSettings(
        codex_bin=sys.executable,
        codex_runtime_home=str(runtime_home),
        api_base_url="https://proxy.example/",
        key_dir=str(tmp_path / "keys"),
    )

    prepared = prepare_runtime_home(settings)

    assert prepared == runtime_home
    assert not (runtime_home / "auth.json").exists()
    config = (runtime_home / "config.toml").read_text()
    parsed = tomllib.loads(config)
    assert config == runtime_config(settings)
    assert 'default_permissions = "aiops-diagnostic"' in config
    assert 'model_provider = "aiops-api"' in config
    assert 'base_url = "https://proxy.example/"' in config
    assert 'env_key = "AIOPS_CODEX_PROVIDER_KEY"' in config
    assert 'sandbox = "unelevated"' in config
    assert parsed["windows"]["sandbox"] == "unelevated"
    assert f'{json.dumps(str(Path(sys.executable).resolve()))} = "read"' in config
    assert "multi_agent = false" in config
    assert '"state.json" = "deny"' in config
    assert '".inputs/**" = "deny"' in config
    assert 'metrics_exporter = "none"' in config
    if os.name != "nt":
        assert stat_mode(runtime_home) == 0o700
        assert stat_mode(runtime_home / "config.toml") == 0o600


def test_provider_key_slots_are_private_and_pluggable(tmp_path: Path) -> None:
    key_dir = tmp_path / "keys"
    ensure_private_directory(key_dir)
    primary = key_dir / "primary.key"
    backup = key_dir / "backup.key"
    write_private_text(primary, "primary-secret")
    write_private_text(backup, "backup-secret")
    settings = AgentSettings(codex_bin=sys.executable, key_dir=str(key_dir), key_slot="primary")

    assert resolve_provider_api_key(settings) == "primary-secret"
    settings.key_slot = "backup"
    assert resolve_provider_api_key(settings) == "backup-secret"


def test_explicit_provider_key_symlink_is_rejected(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Windows symlink creation depends on developer mode or elevated privileges")
    target = tmp_path / "target.key"
    target.write_text("private-secret", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "linked.key"
    link.symlink_to(target)

    with pytest.raises(RuntimeError, match="符号链接"):
        resolve_provider_api_key(
            AgentSettings(codex_bin=sys.executable, api_key_file=str(link), key_dir=str(tmp_path / "keys"))
        )


def test_provider_key_slot_directory_must_be_private(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX mode regression")
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    key_dir.chmod(0o750)
    key = key_dir / "default.key"
    key.write_text("private-secret", encoding="utf-8")
    key.chmod(0o600)

    with pytest.raises(RuntimeError, match="目录权限过宽"):
        resolve_provider_api_key(
            AgentSettings(codex_bin=sys.executable, key_dir=str(key_dir), key_slot="default")
        )


def test_frozen_runtime_reenters_same_executable_for_clean_launcher(monkeypatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    settings = AgentSettings(codex_bin=sys.executable)

    args = codex_launch_args(settings)

    assert args[:2] == (sys.executable, "__codex-launcher")
    assert args[-4:] == (str(Path(sys.executable).resolve()), "app-server", "--listen", "stdio://")


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


def test_codex_heartbeat_is_persisted_and_forwarded() -> None:
    class _Workspace:
        def __init__(self) -> None:
            self.events = []

        def append_event(self, event):
            payload = {"at": "2026-08-04T00:00:00+00:00", **event}
            self.events.append(payload)
            return payload

    workspace = _Workspace()
    settings = AgentSettings(codex_bin=sys.executable, heartbeat_interval_seconds=1)
    settings.heartbeat_interval_seconds = 0.01
    session = object.__new__(SDKCodexSession)
    session.workspace = workspace
    session.settings = settings
    session._thread = SimpleNamespace(id="thread-test")
    forwarded = []
    session._progress_callback = forwarded.append
    stop = threading.Event()
    worker = threading.Thread(target=session._heartbeat_loop, args=(stop, "turn-test"))

    worker.start()
    time.sleep(0.04)
    stop.set()
    worker.join(timeout=1)

    assert any(event["type"] == "codex_turn_heartbeat" for event in workspace.events)
    assert any(event["type"] == "codex_turn_heartbeat" for event in forwarded)


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


def test_runtime_config_writes_named_multi_provider(tmp_path: Path) -> None:
    glm = ProviderConfig(
        name="glm-ark",
        base_url="https://ark.cn-beijing.volces.com/api/coding/v3/",
        wire_api="responses",
        model="glm-5-2-260617",
    )
    settings = AgentSettings(
        codex_bin=sys.executable,
        codex_runtime_home=str(tmp_path / "runtime"),
        providers=(glm,),
        default_provider="glm-ark",
    )

    config = runtime_config(settings, provider=glm)
    parsed = tomllib.loads(config)

    assert parsed["model_provider"] == "glm-ark"
    assert parsed["model_providers"]["glm-ark"]["base_url"] == (
        "https://ark.cn-beijing.volces.com/api/coding/v3/"
    )
    assert parsed["model_providers"]["glm-ark"]["env_key"] == "AIOPS_CODEX_PROVIDER_KEY"
    assert parsed["model_providers"]["glm-ark"]["wire_api"] == "responses"
    assert "aiops-api" not in parsed["model_providers"]
    # default provider is selected when none is passed
    assert runtime_config(settings) == config


def test_resolve_provider_key_uses_provider_key_slot(tmp_path: Path) -> None:
    key_dir = tmp_path / "keys"
    ensure_private_directory(key_dir)
    write_private_text(key_dir / "glm-ark.key", "ark-secret")
    glm = ProviderConfig(name="glm-ark", base_url="https://ark.example/", model="glm-5-2")
    settings = AgentSettings(
        codex_bin=sys.executable,
        key_dir=str(key_dir),
        providers=(glm,),
        default_provider="glm-ark",
    )

    assert resolve_provider_api_key(settings, provider=glm) == "ark-secret"
    # key_slot override reads a different slot file for the same provider
    write_private_text(key_dir / "glm-backup.key", "backup-secret")
    assert resolve_provider_api_key(settings, provider=glm, key_slot="glm-backup") == "backup-secret"


def test_developer_instructions_describe_intent_relative_ladder() -> None:
    """提示词合同必须保留意图相关置信阶梯（防未来提示词改动误回退）。"""
    from aiops_diagnostics.codex_runtime import _developer_instructions

    text = _developer_instructions()
    assert "intent-relative ladder" in text
    for phrase in ("status=\"diagnosed\"", "\"medium\"", "\"inconclusive\"", "\"blocked\"", "failed_sources"):
        assert phrase in text, f"阶梯缺少 {phrase}"
    assert "A failed source limits confidence" not in text, "旧的单句劝退必须删除"
    assert "never justifies inconclusive" in text
    # 机械交付合同（结构化输出）原样保留
    assert "The delivery contract is validated mechanically" in text
