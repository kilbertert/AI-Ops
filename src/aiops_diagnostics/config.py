from __future__ import annotations

import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _default_codex_bin() -> str:
    """Prefer the SDK-pinned native runtime over a shell wrapper on PATH."""
    try:
        from codex_cli_bin import bundled_codex_path

        return str(bundled_codex_path())
    except (ImportError, AttributeError):
        return shutil.which("codex") or "/home/claude/.local/bin/codex"


def canonical_provider_base_url(value: str) -> str:
    """Validate and normalize the non-secret model provider endpoint."""
    candidate = value.strip()
    try:
        parsed = urlsplit(candidate)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Codex API base_url 无效") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Codex API base_url 必须是完整的 http 或 https 地址")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("远程 Codex API base_url 必须使用 https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Codex API base_url 不得包含认证信息")
    if parsed.query or parsed.fragment:
        raise ValueError("Codex API base_url 不得包含 query 或 fragment")
    return candidate.rstrip("/") + "/"


def require_same_provider_base_url(configured: str, persisted: str) -> str:
    """Resume may rotate credentials, but it must not redirect them to another provider."""
    configured_url = canonical_provider_base_url(configured)
    persisted_url = canonical_provider_base_url(persisted)
    if configured_url != persisted_url:
        raise ValueError("恢复运行的 Codex API base_url 与当前配置不一致；只允许切换同一端点的 key slot")
    return configured_url


@dataclass(slots=True)
class MySQLSettings:
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = ""
    password: str = ""
    database: str = "cloud_charging_pile"


@dataclass(slots=True)
class TDengineSettings:
    url: str = "http://127.0.0.1:16041"
    user: str = ""
    password: str = ""
    database: str = "iot"


@dataclass(slots=True)
class RedisSettings:
    host: str = "127.0.0.1"
    port: int = 6379
    database: int = 0
    user: str = ""
    password: str = ""


@dataclass(slots=True)
class SSHSettings:
    enabled: bool = False
    host: str = ""
    port: int = 22
    user: str = "diagnostic"
    key_file: str = ""
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    tdengine_host: str = "127.0.0.1"
    tdengine_port: int = 16041
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379

    def validate(self) -> None:
        if not self.enabled:
            return
        if not self.host or not self.user or not self.key_file:
            raise ValueError("SSH 隧道需要 host、user 和 key_file")
        if self.user.startswith("-"):
            raise ValueError("SSH user 不能以连字符开头")
        for name, port in (
            ("SSH port", self.port),
            ("MySQL forwarded port", self.mysql_port),
            ("TDengine forwarded port", self.tdengine_port),
            ("Redis forwarded port", self.redis_port),
        ):
            if not 1 <= port <= 65535:
                raise ValueError(f"{name} 必须在 1-65535 之间")
        if not Path(self.key_file).expanduser().is_file():
            raise ValueError("SSH 私钥文件不存在")


@dataclass(slots=True)
class SafetySettings:
    query_timeout_seconds: int = 8
    mysql_max_execution_ms: int = 3000
    tdengine_max_rows: int = 2000
    redis_max_messages: int = 200
    max_order_window_hours: int = 72

    def __post_init__(self) -> None:
        limits = {
            "query_timeout_seconds": (self.query_timeout_seconds, 1, 60),
            "mysql_max_execution_ms": (self.mysql_max_execution_ms, 100, 30_000),
            "tdengine_max_rows": (self.tdengine_max_rows, 1, 2_000),
            "redis_max_messages": (self.redis_max_messages, 1, 1_000),
            "max_order_window_hours": (self.max_order_window_hours, 1, 168),
        }
        for name, (value, minimum, maximum) in limits.items():
            if not minimum <= value <= maximum:
                raise ValueError(f"{name} 必须在 {minimum}-{maximum} 之间")


@dataclass(slots=True)
class AgentSettings:
    codex_bin: str = field(default_factory=_default_codex_bin)
    codex_runtime_home: str = str(Path.home() / ".local/share/aiops-diagnostics/codex-home")
    api_base_url: str = "https://api.psydo.top/"
    api_key_env: str = "AIOPS_CODEX_API_KEY"
    api_key_file: str = ""
    key_dir: str = str(Path.home() / ".config/aiops-diagnostics/keys")
    key_slot: str = "default"
    run_root: str = ".aiops/runs"
    model: str = ""
    max_turns: int = 8
    max_tool_calls: int = 16
    max_validation_retries: int = 2
    turn_timeout_seconds: int = 600

    def validate(self) -> None:
        codex_bin = Path(self.codex_bin).expanduser()
        if not codex_bin.is_file() or not os.access(codex_bin, os.X_OK):
            raise ValueError(f"Codex CLI 不可执行: {codex_bin}")
        runtime_home = Path(self.codex_runtime_home).expanduser().resolve()
        canonical_provider_base_url(self.api_base_url)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env):
            raise ValueError("Codex API key 环境变量名无效")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", self.key_slot):
            raise ValueError("Codex key slot 只能包含字母、数字、点、下划线或连字符")
        if runtime_home == Path(self.key_dir).expanduser().resolve():
            raise ValueError("Codex runtime home 与 key_dir 必须隔离")
        limits = {
            "max_turns": (self.max_turns, 2, 20),
            "max_tool_calls": (self.max_tool_calls, 1, 50),
            "max_validation_retries": (self.max_validation_retries, 0, 5),
            "turn_timeout_seconds": (self.turn_timeout_seconds, 30, 1800),
        }
        for name, (value, minimum, maximum) in limits.items():
            if not minimum <= value <= maximum:
                raise ValueError(f"{name} 必须在 {minimum}-{maximum} 之间")


@dataclass(slots=True)
class Settings:
    mysql: MySQLSettings = field(default_factory=MySQLSettings)
    tdengine: TDengineSettings = field(default_factory=TDengineSettings)
    redis: RedisSettings = field(default_factory=RedisSettings)
    ssh: SSHSettings = field(default_factory=SSHSettings)
    safety: SafetySettings = field(default_factory=SafetySettings)
    agent: AgentSettings = field(default_factory=AgentSettings)

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            mysql=MySQLSettings(
                host=_env("AIOPS_MYSQL_HOST", "127.0.0.1"),
                port=_env_int("AIOPS_MYSQL_PORT", 3306),
                user=_env("AIOPS_MYSQL_USER"),
                password=_env("AIOPS_MYSQL_PASSWORD"),
                database=_env("AIOPS_MYSQL_DATABASE", "cloud_charging_pile"),
            ),
            tdengine=TDengineSettings(
                url=_env("AIOPS_TDENGINE_URL", "http://127.0.0.1:16041"),
                user=_env("AIOPS_TDENGINE_USER"),
                password=_env("AIOPS_TDENGINE_PASSWORD"),
                database=_env("AIOPS_TDENGINE_DATABASE", "iot"),
            ),
            redis=RedisSettings(
                host=_env("AIOPS_REDIS_HOST", "127.0.0.1"),
                port=_env_int("AIOPS_REDIS_PORT", 6379),
                database=_env_int("AIOPS_REDIS_DATABASE", 0),
                user=_env("AIOPS_REDIS_USER"),
                password=_env("AIOPS_REDIS_PASSWORD"),
            ),
            ssh=SSHSettings(
                enabled=_env_bool("AIOPS_SSH_ENABLED"),
                host=_env("AIOPS_SSH_HOST"),
                port=_env_int("AIOPS_SSH_PORT", 22),
                user=_env("AIOPS_SSH_USER", "diagnostic"),
                key_file=_env("AIOPS_SSH_KEY_FILE"),
                mysql_host=_env("AIOPS_SSH_MYSQL_HOST", "127.0.0.1"),
                mysql_port=_env_int("AIOPS_SSH_MYSQL_PORT", 3306),
                tdengine_host=_env("AIOPS_SSH_TDENGINE_HOST", "127.0.0.1"),
                tdengine_port=_env_int("AIOPS_SSH_TDENGINE_PORT", 16041),
                redis_host=_env("AIOPS_SSH_REDIS_HOST", "127.0.0.1"),
                redis_port=_env_int("AIOPS_SSH_REDIS_PORT", 6379),
            ),
            safety=SafetySettings(
                query_timeout_seconds=_env_int("AIOPS_QUERY_TIMEOUT_SECONDS", 8),
                mysql_max_execution_ms=_env_int("AIOPS_MYSQL_MAX_EXECUTION_MS", 3000),
                tdengine_max_rows=_env_int("AIOPS_TDENGINE_MAX_ROWS", 2000),
                redis_max_messages=_env_int("AIOPS_REDIS_MAX_MESSAGES", 200),
                max_order_window_hours=_env_int("AIOPS_MAX_ORDER_WINDOW_HOURS", 72),
            ),
            agent=AgentSettings(
                codex_bin=_env(
                    "AIOPS_CODEX_BIN",
                    _default_codex_bin(),
                ),
                codex_runtime_home=_env(
                    "AIOPS_CODEX_RUNTIME_HOME",
                    str(Path.home() / ".local/share/aiops-diagnostics/codex-home"),
                ),
                api_base_url=_env("AIOPS_CODEX_BASE_URL", "https://api.psydo.top/"),
                api_key_env=_env("AIOPS_CODEX_API_KEY_ENV", "AIOPS_CODEX_API_KEY"),
                api_key_file=_env("AIOPS_CODEX_API_KEY_FILE"),
                key_dir=_env(
                    "AIOPS_CODEX_KEY_DIR",
                    str(Path.home() / ".config/aiops-diagnostics/keys"),
                ),
                key_slot=_env("AIOPS_CODEX_KEY_SLOT", "default"),
                run_root=_env("AIOPS_AGENT_RUN_ROOT", ".aiops/runs"),
                model=_env("AIOPS_AGENT_MODEL"),
                max_turns=_env_int("AIOPS_AGENT_MAX_TURNS", 8),
                max_tool_calls=_env_int("AIOPS_AGENT_MAX_TOOL_CALLS", 16),
                max_validation_retries=_env_int("AIOPS_AGENT_MAX_VALIDATION_RETRIES", 2),
                turn_timeout_seconds=_env_int("AIOPS_AGENT_TURN_TIMEOUT_SECONDS", 600),
            ),
        )

    def redacted(self) -> dict[str, Any]:
        data = asdict(self)
        data["mysql"]["password"] = "REDACTED" if self.mysql.password else ""
        data["tdengine"]["url"] = _redact_url_credentials(self.tdengine.url)
        data["tdengine"]["password"] = "REDACTED" if self.tdengine.password else ""
        data["redis"]["password"] = "REDACTED" if self.redis.password else ""
        return data


def _redact_url_credentials(value: str) -> str:
    """Remove URL userinfo before configuration is rendered or logged."""
    try:
        parsed = urlsplit(value)
        if parsed.username is None and parsed.password is None:
            return value
        hostname = parsed.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        if parsed.port is not None:
            hostname = f"{hostname}:{parsed.port}"
        return urlunsplit((parsed.scheme, "REDACTED@" + hostname, parsed.path, parsed.query, parsed.fragment))
    except ValueError:
        return "REDACTED"
