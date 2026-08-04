from __future__ import annotations

import json
import os

from aiops_diagnostics.gateway_config import (
    GatewayClientProfile,
    gateway_profile_dir,
    gateway_profile_path,
    gateway_token_file,
    validate_profile_name,
)
from aiops_diagnostics.private_files import (
    ensure_private_directory,
    validate_private_file,
    write_private_text,
)

SERVICE_NAME = "aiops-diagnostics-gateway"


class GatewayTokenStoreError(RuntimeError):
    """The local OS credential store could not persist a gateway token."""


def save_profile(profile: GatewayClientProfile) -> None:
    ensure_private_directory(gateway_profile_dir())
    write_private_text(
        profile.file_path,
        json.dumps(
            {
                "name": profile.name,
                "base_url": profile.base_url,
                "device_id": profile.device_id,
                "workspace_id": profile.workspace_id,
            },
            ensure_ascii=False,
        )
        + "\n",
    )


def load_profile(name: str) -> GatewayClientProfile:
    path = gateway_profile_path(name)
    validate_private_file(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    return GatewayClientProfile(
        name=str(payload["name"]),
        base_url=str(payload["base_url"]),
        device_id=str(payload["device_id"]),
        workspace_id=str(payload["workspace_id"]),
    )


def save_token(profile_name: str, token: str) -> str:
    validate_profile_name(profile_name)
    mode = _mode()
    if mode != "file":
        try:
            import keyring

            keyring.set_password(SERVICE_NAME, profile_name, token)
            return "keyring"
        except Exception as exc:
            if mode == "keyring":
                raise GatewayTokenStoreError("OS credential store is unavailable") from exc
    path = gateway_token_file(profile_name)
    write_private_text(path, token + "\n")
    return "private-file"


def load_token(profile_name: str) -> str:
    validate_profile_name(profile_name)
    mode = _mode()
    if mode != "file":
        try:
            import keyring

            token = keyring.get_password(SERVICE_NAME, profile_name)
            if token:
                return token
            if mode == "keyring":
                raise GatewayTokenStoreError("gateway token is not stored in the OS credential store")
        except GatewayTokenStoreError:
            raise
        except Exception as exc:
            if mode == "keyring":
                raise GatewayTokenStoreError("OS credential store is unavailable") from exc
    path = gateway_token_file(profile_name)
    validate_private_file(path)
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise GatewayTokenStoreError("gateway token file is empty")
    return token


def delete_token(profile_name: str) -> None:
    validate_profile_name(profile_name)
    try:
        import keyring

        keyring.delete_password(SERVICE_NAME, profile_name)
    except Exception:
        pass
    path = gateway_token_file(profile_name)
    path.unlink(missing_ok=True)


def _mode() -> str:
    mode = os.getenv("AIOPS_GATEWAY_TOKEN_STORE", "auto").strip().lower()
    if mode not in {"auto", "keyring", "file"}:
        raise GatewayTokenStoreError("AIOPS_GATEWAY_TOKEN_STORE must be auto, keyring, or file")
    return mode
