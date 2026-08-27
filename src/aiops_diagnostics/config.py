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

from aiops_diagnostics.http_auth import DEFAULT_INTERNAL_TOKEN_EXPIRE_SECONDS
from aiops_diagnostics.platform_paths import (
    default_codex_home,
    default_config_file,
    default_key_dir,
    default_run_root,
)
from aiops_diagnostics.private_files import PrivatePathError, validate_private_file


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


def _default_codex_bin() -> str:
    """Prefer the SDK-pinned native runtime over a shell wrapper on PATH."""
    try:
        from codex_cli_bin import bundled_codex_path

        return str(bundled_codex_path())
    except (ImportError, AttributeError):
        return shutil.which("codex") or ("codex.exe" if os.name == "nt" else "codex")


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


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """A registered, selectable Responses API model provider.

    The Codex-side ``env_key`` is always ``AIOPS_CODEX_PROVIDER_KEY``: every
    provider resolves its own credential and the harness injects it into that
    single env var for the Codex subprocess. ``api_key_env`` is the optional
    environment-variable *source* for the key override; when empty the key is
    read from the private ``keys/<key_slot>.key`` file.
    """

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


def _validate_token_expire_seconds(value: int) -> None:
    if not 60 <= value <= 600:
        raise ValueError("Diag API 令牌有效期必须在 60-600 秒之间")


@dataclass(slots=True)
class InternalTokenSettings:
    secret: str | None = None
    expire_seconds: int | None = DEFAULT_INTERNAL_TOKEN_EXPIRE_SECONDS

    def __post_init__(self) -> None:
        if self.expire_seconds is not None:
            _validate_token_expire_seconds(self.expire_seconds)


@dataclass(slots=True)
class HttpSettings:
    base_url: str | None = None
    internal_token: InternalTokenSettings = field(default_factory=InternalTokenSettings)
    timeout_seconds: int = 8

    def __post_init__(self) -> None:
        if self.base_url is not None:
            parsed = urlsplit(self.base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("Diag API base_url 必须是完整的 http 或 https 地址")
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("Diag API base_url 不得包含认证信息")
            if parsed.query or parsed.fragment:
                raise ValueError("Diag API base_url 不得包含 query 或 fragment")
        if not 1 <= self.timeout_seconds <= 60:
            raise ValueError("Diag API 超时必须在 1-60 秒之间")

    @property
    def token_secret(self) -> str | None:
        return self.internal_token.secret

    @token_secret.setter
    def token_secret(self, value: str | None) -> None:
        self.internal_token.secret = value

    @property
    def token_expire_seconds(self) -> int | None:
        return self.internal_token.expire_seconds

    @token_expire_seconds.setter
    def token_expire_seconds(self, value: int | None) -> None:
        if value is not None:
            _validate_token_expire_seconds(value)
        self.internal_token.expire_seconds = value


# Deprecated flat name kept as an alias so existing callers/tests keep working.
DiagApiSettings = HttpSettings


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
    codex_runtime_home: str = field(default_factory=lambda: str(default_codex_home()))
    api_base_url: str = "https://api.psydo.top/"
    api_key_env: str = "AIOPS_CODEX_API_KEY"
    api_key_file: str = ""
    key_dir: str = field(default_factory=lambda: str(default_key_dir()))
    key_slot: str = "default"
    run_root: str = field(default_factory=lambda: str(default_run_root()))
    model: str = ""
    max_turns: int = 8
    max_tool_calls: int = 16
    max_validation_retries: int = 2
    turn_timeout_seconds: int = 600
    heartbeat_interval_seconds: int = 10
    windows_sandbox: str = "unelevated"
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

    def provider_names(self) -> tuple[str, ...]:
        return tuple(provider.name for provider in self.providers)

    def validate(self) -> None:
        codex_bin = Path(self.codex_bin).expanduser()
        if not codex_bin.is_file() or not os.access(codex_bin, os.X_OK):
            raise ValueError(f"Codex CLI 不可执行: {codex_bin}")
        runtime_home = Path(self.codex_runtime_home).expanduser().resolve()
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
                raise ValueError("Codex API key 环境变量名无效")
        validate_key_slot_name(self.key_slot)
        if runtime_home == Path(self.key_dir).expanduser().resolve():
            raise ValueError("Codex runtime home 与 key_dir 必须隔离")
        if self.windows_sandbox not in {"elevated", "unelevated"}:
            raise ValueError("Windows Codex sandbox 必须是 elevated 或 unelevated")
        limits = {
            "max_turns": (self.max_turns, 2, 20),
            "max_tool_calls": (self.max_tool_calls, 1, 50),
            "max_validation_retries": (self.max_validation_retries, 0, 5),
            "turn_timeout_seconds": (self.turn_timeout_seconds, 30, 1800),
            "heartbeat_interval_seconds": (self.heartbeat_interval_seconds, 1, 120),
        }
        for name, (value, minimum, maximum) in limits.items():
            if not minimum <= value <= maximum:
                raise ValueError(f"{name} 必须在 {minimum}-{maximum} 之间")


@dataclass(slots=True)
class Settings:
    mysql: MySQLSettings = field(default_factory=MySQLSettings)
    tdengine: TDengineSettings = field(default_factory=TDengineSettings)
    redis: RedisSettings = field(default_factory=RedisSettings)
    http: HttpSettings = field(default_factory=HttpSettings)
    ssh: SSHSettings = field(default_factory=SSHSettings)
    safety: SafetySettings = field(default_factory=SafetySettings)
    agent: AgentSettings = field(default_factory=AgentSettings)

    @property
    def diag_api(self) -> HttpSettings:
        """Deprecated flat alias for ``http``; kept for existing callers and tests."""
        return self.http

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
        data_home = env("AIOPS_DATA_HOME") or portable_home
        key_dir_default = Path(config_home) / "keys" if config_home else default_key_dir()
        codex_home_default = Path(data_home) / "codex-home" if data_home else default_codex_home()
        run_root_default = Path(data_home) / "runs" if data_home else default_run_root()

        providers = _parse_providers(env)
        default_provider = env("AIOPS_DEFAULT_PROVIDER") or (providers[0].name if providers else "aiops-api")

        http_base_url = env("AIOPS_HTTP_BASE_URL") or env("AIOPS_DIAG_API_BASE_URL") or None
        http_secret = env("AIOPS_HTTP_INTERNAL_TOKEN_SECRET") or env("AIOPS_DIAG_API_TOKEN_SECRET") or None
        http_expire_seconds = env_int_optional("AIOPS_HTTP_INTERNAL_TOKEN_EXPIRE_SECONDS")
        if http_expire_seconds is None:
            http_expire_seconds = env_int_optional("AIOPS_DIAG_API_TOKEN_EXPIRE_SECONDS")
        if http_expire_seconds is None:
            http_expire_seconds = DEFAULT_INTERNAL_TOKEN_EXPIRE_SECONDS
        http_timeout_seconds = env_int_optional("AIOPS_HTTP_TIMEOUT_SECONDS")
        if http_timeout_seconds is None:
            http_timeout_seconds = env_int_optional("AIOPS_DIAG_API_TIMEOUT_SECONDS")
        if http_timeout_seconds is None:
            http_timeout_seconds = 8

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
            http=HttpSettings(
                base_url=http_base_url,
                internal_token=InternalTokenSettings(
                    secret=http_secret,
                    expire_seconds=http_expire_seconds,
                ),
                timeout_seconds=http_timeout_seconds,
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
                redis_max_messages=env_int("AIOPS_REDIS_MAX_MESSAGES", 200),
                max_order_window_hours=env_int("AIOPS_MAX_ORDER_WINDOW_HOURS", 72),
            ),
            agent=AgentSettings(
                codex_bin=env("AIOPS_CODEX_BIN") or _default_codex_bin(),
                codex_runtime_home=env("AIOPS_CODEX_RUNTIME_HOME") or str(codex_home_default),
                api_base_url=env("AIOPS_CODEX_BASE_URL", "https://api.psydo.top/"),
                api_key_env=env("AIOPS_CODEX_API_KEY_ENV", "AIOPS_CODEX_API_KEY"),
                api_key_file=env("AIOPS_CODEX_API_KEY_FILE"),
                key_dir=env("AIOPS_CODEX_KEY_DIR") or str(key_dir_default),
                key_slot=env("AIOPS_CODEX_KEY_SLOT", "default"),
                run_root=env("AIOPS_AGENT_RUN_ROOT") or str(run_root_default),
                model=env("AIOPS_AGENT_MODEL"),
                max_turns=env_int("AIOPS_AGENT_MAX_TURNS", 8),
                max_tool_calls=env_int("AIOPS_AGENT_MAX_TOOL_CALLS", 16),
                max_validation_retries=env_int("AIOPS_AGENT_MAX_VALIDATION_RETRIES", 2),
                turn_timeout_seconds=env_int("AIOPS_AGENT_TURN_TIMEOUT_SECONDS", 600),
                heartbeat_interval_seconds=env_int("AIOPS_AGENT_HEARTBEAT_INTERVAL_SECONDS", 10),
                windows_sandbox=env("AIOPS_WINDOWS_SANDBOX", "unelevated"),
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
        data["http"]["internal_token"]["secret"] = "REDACTED" if self.http.internal_token.secret else ""
        data["diag_api"] = {
            "base_url": self.http.base_url,
            "token_secret": "REDACTED" if self.http.token_secret else "",
            "token_expire_seconds": self.http.token_expire_seconds,
            "timeout_seconds": self.http.timeout_seconds,
        }
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
