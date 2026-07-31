from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


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


@dataclass(slots=True)
class MySQLSettings:
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = ""
    password: str = ""
    database: str = "cloud_charging_pile"


@dataclass(slots=True)
class TDengineSettings:
    url: str = "http://127.0.0.1:6041"
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
    tdengine_port: int = 6041
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379

    def validate(self) -> None:
        if not self.enabled:
            return
        if not self.host or not self.user or not self.key_file:
            raise ValueError("SSH 隧道需要 host、user 和 key_file")
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
            "tdengine_max_rows": (self.tdengine_max_rows, 1, 10_000),
            "redis_max_messages": (self.redis_max_messages, 1, 1_000),
            "max_order_window_hours": (self.max_order_window_hours, 1, 168),
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
                url=_env("AIOPS_TDENGINE_URL", "http://127.0.0.1:6041"),
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
                tdengine_port=_env_int("AIOPS_SSH_TDENGINE_PORT", 6041),
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
        )

    def redacted(self) -> dict[str, Any]:
        data = asdict(self)
        data["mysql"]["password"] = "REDACTED" if self.mysql.password else ""
        data["tdengine"]["password"] = "REDACTED" if self.tdengine.password else ""
        data["redis"]["password"] = "REDACTED" if self.redis.password else ""
        return data
