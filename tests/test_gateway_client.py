from pathlib import Path

import pytest

from aiops_diagnostics.gateway_client import GatewayClient, GatewayClientError
from aiops_diagnostics.gateway_config import GatewayClientProfile
from aiops_diagnostics.gateway_tokens import load_profile, load_token, save_profile, save_token


def test_profile_and_file_token_store_are_private(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AIOPS_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("AIOPS_GATEWAY_TOKEN_STORE", "file")
    profile = GatewayClientProfile(
        name="default",
        base_url="https://gateway.example.test",
        device_id="dev-1",
        workspace_id="ops",
    )

    save_profile(profile)
    assert save_token("default", "aops-secret") == "private-file"
    assert load_profile("default").workspace_id == "ops"
    assert load_token("default") == "aops-secret"
    assert (tmp_path / "config" / "gateway-profiles" / "default.json").stat().st_mode & 0o077 == 0
    assert (tmp_path / "config" / "gateway-tokens" / "default.token").stat().st_mode & 0o077 == 0


def test_gateway_client_requires_https_for_remote_endpoints() -> None:
    with pytest.raises(ValueError):
        GatewayClient("http://gateway.example.test")
    with pytest.raises(GatewayClientError):
        GatewayClient("http://127.0.0.1:8787").list_runs()


def test_profile_name_cannot_escape_private_config_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AIOPS_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("AIOPS_GATEWAY_TOKEN_STORE", "file")

    with pytest.raises(ValueError):
        save_token("../outside", "aops-secret")
    with pytest.raises(ValueError):
        load_profile("../outside")
