from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from dotenv import dotenv_values

from aiops_diagnostics.common.platform_paths import (
    default_config_file,
    default_key_dir,
)
from aiops_diagnostics.common.private_files import PrivatePathError, validate_private_file


def _env(name: str, default: str = "", values: Mapping[str, str | None] | None = None) -> str:
    raw = os.getenv(name)
    if raw is None and values is not None:
        raw = values.get(name)
    return (raw if raw is not None else default).strip()


def _env_int(name: str, default: int, values: Mapping[str, str | None] | None = None) -> int:
    raw = _env(name, values=values)
    return int(raw) if raw else default


def _env_int_optional(name: str, values: Mapping[str, str | None] | None = None) -> int | None:
    raw = _env(name, values=values)
    return int(raw) if raw else None


def _env_bool(
    name: str,
    default: bool = False,
    values: Mapping[str, str | None] | None = None,
) -> bool:
    raw = _env(name, values=values)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _parse_providers(env: Callable[[str, str], str]) -> tuple[ProviderConfig, ...]:
    """Parse the ``AIOPS_PROVIDERS`` registry from env or config-file values."""
    raw = env("AIOPS_PROVIDERS", "")
    if not raw:
        return ()
    providers: list[ProviderConfig] = []
    for name in raw.split(","):
        name = name.strip()
        if not name:
            continue
        suffix = provider_env_suffix(name)
        base_url = env(f"AIOPS_PROVIDER_{suffix}_BASE_URL", "")
        if not base_url:
            raise ValueError(f"provider {name} 缺少 AIOPS_PROVIDER_{suffix}_BASE_URL")
        providers.append(
            ProviderConfig(
                name=name,
                base_url=base_url,
                wire_api=env(f"AIOPS_PROVIDER_{suffix}_WIRE_API", "responses"),
                api_key_env=env(f"AIOPS_PROVIDER_{suffix}_KEY_ENV", ""),
                model=env(f"AIOPS_PROVIDER_{suffix}_MODEL", ""),
                default_key_slot=env(f"AIOPS_PROVIDER_{suffix}_KEY_SLOT", "") or name,
            )
        )
    return tuple(providers)


def canonical_provider_base_url(value: str) -> str:
    """Validate and normalize the non-secret model provider endpoint."""
    candidate = value.strip()
    try:
        parsed = urlsplit(candidate)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("模型 API base_url 无效") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("模型 API base_url 必须是完整的 http 或 https 地址")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("远程 模型 API base_url 必须使用 https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("模型 API base_url 不得包含认证信息")
    if parsed.query or parsed.fragment:
        raise ValueError("模型 API base_url 不得包含 query 或 fragment")
    return candidate.rstrip("/") + "/"


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """单次模型请求的提供方；凭据只从环境变量或私有 key 文件读取。"""

    name: str
    base_url: str
    wire_api: str = "responses"
    api_key_env: str = ""
    model: str = ""
    default_key_slot: str = ""

    def resolved_key_slot(self) -> str:
        return self.default_key_slot or self.name


def provider_env_suffix(name: str) -> str:
    """Map a provider name to its env-var token, e.g. ``glm-ark`` -> ``GLM_ARK``."""
    return re.sub(r"[^A-Za-z0-9]", "_", name).upper()


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
class UpmsSettings:
    """平台 UPMS 只读服务边界；权限上下文解析使用，不承载任何业务数据查询。

    ``inside_token`` 是 AI-Ops 以**服务侧身份**调用 UPMS 内部端点（``/user/inside/*``，
    如会话身份的 C→B 映射）时出示的凭据，不是调用者平台凭证；只在服务端配置，绝不
    进入 ``ScopeContext``、审计摘要或错误消息。
    """

    base_url: str | None = None
    inside_token: str | None = None
    timeout_seconds: int = 8

    def __post_init__(self) -> None:
        if self.base_url is not None:
            parsed = urlsplit(self.base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("UPMS base_url 必须是完整的 http 或 https 地址")
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("UPMS base_url 不得包含认证信息")
            if parsed.query or parsed.fragment:
                raise ValueError("UPMS base_url 不得包含 query 或 fragment")
        if not 1 <= self.timeout_seconds <= 60:
            raise ValueError("UPMS 超时必须在 1-60 秒之间")


@dataclass(slots=True)
class DisSettings:
    """平台 Dis 点位归属服务边界；代查目标的站点范围解析使用。

    ``token`` 是充电桩 Java ``DisFeignInterceptor`` 同款服务侧静态令牌
    （``dis.token``），不是用户凭证；纳入脱敏输出。
    """

    base_url: str | None = None
    token: str | None = None
    timeout_seconds: int = 8

    def __post_init__(self) -> None:
        if self.base_url is not None:
            parsed = urlsplit(self.base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("Dis base_url 必须是完整的 http 或 https 地址")
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("Dis base_url 不得包含认证信息")
            if parsed.query or parsed.fragment:
                raise ValueError("Dis base_url 不得包含 query 或 fragment")
        if not 1 <= self.timeout_seconds <= 60:
            raise ValueError("Dis 超时必须在 1-60 秒之间")


@dataclass(slots=True)
class SSHSettings:
    enabled: bool = False
    ssh_bin: str = field(default_factory=lambda: shutil.which("ssh") or "ssh")
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
        ssh_bin = Path(self.ssh_bin).expanduser()
        if not ssh_bin.is_file() and shutil.which(self.ssh_bin) is None:
            raise ValueError(f"SSH 客户端不可执行: {self.ssh_bin}")
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
    max_order_window_hours: int = 72

    def __post_init__(self) -> None:
        limits = {
            "query_timeout_seconds": (self.query_timeout_seconds, 1, 60),
            "mysql_max_execution_ms": (self.mysql_max_execution_ms, 100, 30_000),
            "tdengine_max_rows": (self.tdengine_max_rows, 1, 2_000),
            "max_order_window_hours": (self.max_order_window_hours, 1, 168),
        }
        for name, (value, minimum, maximum) in limits.items():
            if not minimum <= value <= maximum:
                raise ValueError(f"{name} 必须在 {minimum}-{maximum} 之间")


@dataclass(slots=True)
class AgentSettings:
    api_base_url: str = "https://api.psydo.top/"
    api_key_env: str = "AIOPS_CODEX_API_KEY"
    api_key_file: str = ""
    key_dir: str = field(default_factory=lambda: str(default_key_dir()))
    key_slot: str = "default"
    model: str = ""
    providers: tuple[ProviderConfig, ...] = ()
    default_provider: str = "aiops-api"

    def select_provider(self, name: str | None) -> ProviderConfig:
        """Return the named provider, the default, or a legacy single provider.

        When ``providers`` is empty the instance is in legacy single-provider
        mode and synthesizes an ``aiops-api`` provider from ``api_base_url``,
        ``api_key_env``, ``model`` and ``key_slot`` so existing configurations
        keep working without a registry.
        """
        if not self.providers:
            if name and name != "aiops-api":
                raise ValueError(f"未知的 model provider: {name}")
            return ProviderConfig(
                name="aiops-api",
                base_url=self.api_base_url,
                wire_api="responses",
                api_key_env=self.api_key_env,
                model=self.model,
                default_key_slot=self.key_slot or "default",
            )
        target = name or self.default_provider
        for provider in self.providers:
            if provider.name == target:
                return provider
        available = ", ".join(provider.name for provider in self.providers)
        raise ValueError(f"未知的 model provider: {target}; 可用: {available}")

    def validate(self) -> None:
        if self.providers:
            names = [provider.name for provider in self.providers]
            if len(names) != len(set(names)):
                raise ValueError("provider 名称不能重复")
            for provider in self.providers:
                validate_key_slot_name(provider.name)
                canonical_provider_base_url(provider.base_url)
                if provider.wire_api not in {"responses", "chat"}:
                    raise ValueError(f"provider {provider.name} 的 wire_api 必须是 responses 或 chat")
                if provider.api_key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", provider.api_key_env):
                    raise ValueError(f"provider {provider.name} 的 key_env 环境变量名无效")
            if self.default_provider not in names:
                raise ValueError(f"default_provider {self.default_provider} 不在注册表中")
        else:
            canonical_provider_base_url(self.api_base_url)
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env):
                raise ValueError("模型 API key 环境变量名无效")
        validate_key_slot_name(self.key_slot)


@dataclass(slots=True)
class Settings:
    mysql: MySQLSettings = field(default_factory=MySQLSettings)
    tdengine: TDengineSettings = field(default_factory=TDengineSettings)
    redis: RedisSettings = field(default_factory=RedisSettings)
    upms: UpmsSettings = field(default_factory=UpmsSettings)
    dis: DisSettings = field(default_factory=DisSettings)
    ssh: SSHSettings = field(default_factory=SSHSettings)
    safety: SafetySettings = field(default_factory=SafetySettings)
    agent: AgentSettings = field(default_factory=AgentSettings)

    @classmethod
    def from_env(cls) -> Settings:
        return cls._from_values(None)

    @classmethod
    def from_config(cls, config_file: Path | None = None) -> Settings:
        selected = selected_config_file(config_file)
        values: Mapping[str, str | None] | None = None
        if selected.is_file():
            try:
                validate_private_file(selected)
            except PrivatePathError as exc:
                raise ValueError(str(exc)) from exc
            values = dotenv_values(selected, interpolate=False)
        elif config_file is not None or os.getenv("AIOPS_CONFIG_FILE", "").strip():
            raise ValueError(f"配置文件不存在: {selected}")
        return cls._from_values(values)

    @classmethod
    def _from_values(cls, values: Mapping[str, str | None] | None) -> Settings:
        def env(name: str, default: str = "") -> str:
            return _env(name, default, values)

        def env_int(name: str, default: int) -> int:
            return _env_int(name, default, values)

        def env_int_optional(name: str) -> int | None:
            return _env_int_optional(name, values)

        def env_bool(name: str, default: bool = False) -> bool:
            return _env_bool(name, default, values)

        portable_home = env("AIOPS_HOME")
        config_home = env("AIOPS_CONFIG_HOME") or portable_home
        key_dir_default = Path(config_home) / "keys" if config_home else default_key_dir()

        providers = _parse_providers(env)
        default_provider = env("AIOPS_DEFAULT_PROVIDER") or (providers[0].name if providers else "aiops-api")

        return cls(
            mysql=MySQLSettings(
                host=env("AIOPS_MYSQL_HOST", "127.0.0.1"),
                port=env_int("AIOPS_MYSQL_PORT", 3306),
                user=env("AIOPS_MYSQL_USER"),
                password=env("AIOPS_MYSQL_PASSWORD"),
                database=env("AIOPS_MYSQL_DATABASE", "cloud_charging_pile"),
            ),
            tdengine=TDengineSettings(
                url=env("AIOPS_TDENGINE_URL", "http://127.0.0.1:16041"),
                user=env("AIOPS_TDENGINE_USER"),
                password=env("AIOPS_TDENGINE_PASSWORD"),
                database=env("AIOPS_TDENGINE_DATABASE", "iot"),
            ),
            redis=RedisSettings(
                host=env("AIOPS_REDIS_HOST", "127.0.0.1"),
                port=env_int("AIOPS_REDIS_PORT", 6379),
                database=env_int("AIOPS_REDIS_DATABASE", 0),
                user=env("AIOPS_REDIS_USER"),
                password=env("AIOPS_REDIS_PASSWORD"),
            ),
            upms=UpmsSettings(
                base_url=env("AIOPS_UPMS_BASE_URL") or None,
                inside_token=env("AIOPS_UPMS_INSIDE_TOKEN") or None,
                timeout_seconds=env_int("AIOPS_UPMS_TIMEOUT_SECONDS", 8),
            ),
            dis=DisSettings(
                base_url=env("AIOPS_DIS_BASE_URL") or None,
                token=env("AIOPS_DIS_TOKEN") or None,
                timeout_seconds=env_int("AIOPS_DIS_TIMEOUT_SECONDS", 8),
            ),
            ssh=SSHSettings(
                enabled=env_bool("AIOPS_SSH_ENABLED"),
                ssh_bin=env("AIOPS_SSH_BIN") or shutil.which("ssh") or "ssh",
                host=env("AIOPS_SSH_HOST"),
                port=env_int("AIOPS_SSH_PORT", 22),
                user=env("AIOPS_SSH_USER", "diagnostic"),
                key_file=env("AIOPS_SSH_KEY_FILE"),
                mysql_host=env("AIOPS_SSH_MYSQL_HOST", "127.0.0.1"),
                mysql_port=env_int("AIOPS_SSH_MYSQL_PORT", 3306),
                tdengine_host=env("AIOPS_SSH_TDENGINE_HOST", "127.0.0.1"),
                tdengine_port=env_int("AIOPS_SSH_TDENGINE_PORT", 16041),
                redis_host=env("AIOPS_SSH_REDIS_HOST", "127.0.0.1"),
                redis_port=env_int("AIOPS_SSH_REDIS_PORT", 6379),
            ),
            safety=SafetySettings(
                query_timeout_seconds=env_int("AIOPS_QUERY_TIMEOUT_SECONDS", 8),
                mysql_max_execution_ms=env_int("AIOPS_MYSQL_MAX_EXECUTION_MS", 3000),
                tdengine_max_rows=env_int("AIOPS_TDENGINE_MAX_ROWS", 2000),
                max_order_window_hours=env_int("AIOPS_MAX_ORDER_WINDOW_HOURS", 72),
            ),
            agent=AgentSettings(
                api_base_url=env("AIOPS_CODEX_BASE_URL", "https://api.psydo.top/"),
                api_key_env=env("AIOPS_CODEX_API_KEY_ENV", "AIOPS_CODEX_API_KEY"),
                api_key_file=env("AIOPS_CODEX_API_KEY_FILE"),
                key_dir=env("AIOPS_CODEX_KEY_DIR") or str(key_dir_default),
                key_slot=env("AIOPS_CODEX_KEY_SLOT", "default"),
                model=env("AIOPS_AGENT_MODEL"),
                providers=providers,
                default_provider=default_provider,
            ),
        )

    def redacted(self) -> dict[str, Any]:
        data = asdict(self)
        data["mysql"]["password"] = "REDACTED" if self.mysql.password else ""
        data["tdengine"]["url"] = _redact_url_credentials(self.tdengine.url)
        data["tdengine"]["password"] = "REDACTED" if self.tdengine.password else ""
        data["redis"]["password"] = "REDACTED" if self.redis.password else ""
        data["upms"]["inside_token"] = "REDACTED" if self.upms.inside_token else ""
        data["dis"]["token"] = "REDACTED" if self.dis.token else ""
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


def selected_config_file(config_file: Path | None = None) -> Path:
    if config_file is not None:
        return config_file.expanduser().resolve()
    configured = os.getenv("AIOPS_CONFIG_FILE", "").strip()
    return Path(configured).expanduser().resolve() if configured else default_config_file().resolve()


def validate_key_slot_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
        raise ValueError("Codex key slot 只能包含字母、数字、点、下划线或连字符")
    return value
