from __future__ import annotations

import os
import sys
from pathlib import Path

APP_SLUG = "aiops-diagnostics"
WINDOWS_APP_DIR = "AI-Ops-Diagnostics"


def config_root() -> Path:
    override = _path_env("AIOPS_CONFIG_HOME") or _path_env("AIOPS_HOME")
    if override:
        return override
    if os.name == "nt":
        return _windows_local_app_data() / WINDOWS_APP_DIR / "config"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / WINDOWS_APP_DIR / "config"
    return _path_env("XDG_CONFIG_HOME") or Path.home() / ".config" / APP_SLUG


def data_root() -> Path:
    override = _path_env("AIOPS_DATA_HOME") or _path_env("AIOPS_HOME")
    if override:
        return override
    if os.name == "nt":
        return _windows_local_app_data() / WINDOWS_APP_DIR
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / WINDOWS_APP_DIR
    return _path_env("XDG_DATA_HOME") or Path.home() / ".local" / "share" / APP_SLUG


def default_config_file() -> Path:
    return config_root() / "production.env"


def default_key_dir() -> Path:
    return config_root() / "keys"


def _windows_local_app_data() -> Path:
    value = os.getenv("LOCALAPPDATA", "").strip()
    if value:
        return Path(value).expanduser()
    return Path.home() / "AppData" / "Local"


def _path_env(name: str) -> Path | None:
    value = os.getenv(name, "").strip()
    return Path(value).expanduser() if value else None
