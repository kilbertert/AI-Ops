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


def default_codex_home() -> Path:
    return data_root() / "codex-home"


def default_run_root() -> Path:
    if _path_env("AIOPS_DATA_HOME") or _path_env("AIOPS_HOME"):
        return data_root() / "runs"
    development_root = development_project_root()
    if development_root is not None:
        return development_root / ".aiops" / "runs"
    return data_root() / "runs"


def development_project_root() -> Path | None:
    candidates = [Path.cwd().resolve(), *Path.cwd().resolve().parents]
    source_root = Path(__file__).resolve().parents[2]
    candidates.extend([source_root, *source_root.parents])
    for candidate in dict.fromkeys(candidates):
        if (candidate / "pyproject.toml").is_file() and (candidate / "src" / "aiops_diagnostics").is_dir():
            return candidate
    return None


def reference_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        candidate = Path(frozen_root) / "aiops_diagnostics" / "_bundle"
        if candidate.is_dir():
            return candidate

    development_root = development_project_root()
    if development_root is not None:
        return development_root

    candidate = Path(__file__).resolve().parent / "_bundle"
    if candidate.is_dir():
        return candidate
    raise FileNotFoundError("AI-Ops 诊断参考资料未包含在当前安装包中")


def _windows_local_app_data() -> Path:
    value = os.getenv("LOCALAPPDATA", "").strip()
    if value:
        return Path(value).expanduser()
    return Path.home() / "AppData" / "Local"


def _path_env(name: str) -> Path | None:
    value = os.getenv(name, "").strip()
    return Path(value).expanduser() if value else None
