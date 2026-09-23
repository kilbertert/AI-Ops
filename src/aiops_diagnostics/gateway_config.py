from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

from aiops_diagnostics.config import selected_config_file, validate_key_slot_name
from aiops_diagnostics.platform_paths import config_root, data_root
from aiops_diagnostics.private_files import PrivatePathError, validate_private_file

SAFE_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


@dataclass(slots=True)
class GatewayServerSettings:
    data_home: Path
    database_file: Path
    bind_host: str = "127.0.0.1"
    port: int = 8787
    server_config_file: Path | None = None
    allow_fixtures: bool = False
    max_workers: int = 2
    allowed_key_slots: tuple[str, ...] = ()
    event_poll_interval_seconds: float = 0.5
    introspection_url: str = ""
    introspection_client_id: str = ""
    introspection_client_secret: str = ""
    standard_api_audience: str = "aiops-api"
    introspection_timeout_seconds: int = 5
    third_session_service_token: str = ""
    third_session_key_prefix: str = "app:3rd_session:"
    kb_service_base_url: str = ""
    #: Routing decision source (#392). Empty base URL or key leaves routing on
    #: the previous behaviour; the dependency is optional by configuration.
    jev_base_url: str = ""
    jev_api_key: str = ""
    jev_model: str = "typesafe/jev"
    jev_timeout_seconds: float = 20.0
    routing_risk_at_least: float = 0.5
    routing_confidence_at_least: float = 0.8
    kb_service_timeout_seconds: float = 10.0
    media_signing_secret: str = ""
    media_ttl_seconds: int = 600

    @classmethod
    def from_env(cls) -> GatewayServerSettings:
        data_home = Path(_env("AIOPS_GATEWAY_DATA_HOME") or data_root() / "gateway").expanduser()
        database_file = Path(_env("AIOPS_GATEWAY_DATABASE_FILE") or data_home / "gateway.db").expanduser()
        configured_server_file = _env("AIOPS_GATEWAY_SERVER_CONFIG_FILE")
        server_config_file = (
            Path(configured_server_file).expanduser().resolve()
            if configured_server_file
            else selected_config_file()
        )
        file_values = _private_config_values(server_config_file)
        slots = tuple(
            validate_key_slot_name(item.strip())
            for item in _env("AIOPS_GATEWAY_ALLOWED_KEY_SLOTS").split(",")
            if item.strip()
        )
        return cls(
            data_home=data_home.resolve(),
            database_file=database_file.resolve(),
            bind_host=_env("AIOPS_GATEWAY_BIND", "127.0.0.1"),
            port=_env_int("AIOPS_GATEWAY_PORT", 8787),
            server_config_file=(
                Path(configured_server_file).expanduser().resolve()
                if configured_server_file
                else selected_config_file()
            ),
            allow_fixtures=_env_bool("AIOPS_GATEWAY_ALLOW_FIXTURES"),
            max_workers=_env_int("AIOPS_GATEWAY_MAX_WORKERS", 2),
            allowed_key_slots=slots,
            event_poll_interval_seconds=_env_float("AIOPS_GATEWAY_EVENT_POLL_SECONDS", 0.5),
            introspection_url=_env("AIOPS_GATEWAY_INTROSPECTION_URL"),
            introspection_client_id=_env("AIOPS_GATEWAY_INTROSPECTION_CLIENT_ID"),
            introspection_client_secret=_env("AIOPS_GATEWAY_INTROSPECTION_CLIENT_SECRET"),
            standard_api_audience=_env("AIOPS_GATEWAY_STANDARD_API_AUDIENCE", "aiops-api"),
            introspection_timeout_seconds=_env_int("AIOPS_GATEWAY_INTROSPECTION_TIMEOUT_SECONDS", 5),
            third_session_service_token=_env("AIOPS_GATEWAY_THIRD_SESSION_SERVICE_TOKEN")
            or _file_value(file_values, "AIOPS_GATEWAY_THIRD_SESSION_SERVICE_TOKEN"),
            third_session_key_prefix=_env("AIOPS_GATEWAY_THIRD_SESSION_KEY_PREFIX")
            or _file_value(file_values, "AIOPS_GATEWAY_THIRD_SESSION_KEY_PREFIX", "app:3rd_session:"),
            kb_service_base_url=_env("AIOPS_GATEWAY_KB_SERVICE_BASE_URL")
            or _file_value(file_values, "AIOPS_GATEWAY_KB_SERVICE_BASE_URL"),
            kb_service_timeout_seconds=_env_float("AIOPS_GATEWAY_KB_SERVICE_TIMEOUT_SECONDS", 10.0),
            media_signing_secret=_env("AIOPS_GATEWAY_MEDIA_SIGNING_SECRET")
            or _file_value(file_values, "AIOPS_GATEWAY_MEDIA_SIGNING_SECRET"),
            media_ttl_seconds=_env_int("AIOPS_GATEWAY_MEDIA_TTL_SECONDS", 600),
            jev_base_url=_env("AIOPS_GATEWAY_JEV_BASE_URL")
            or _file_value(file_values, "AIOPS_GATEWAY_JEV_BASE_URL"),
            jev_api_key=_env("AIOPS_GATEWAY_JEV_API_KEY")
            or _file_value(file_values, "AIOPS_GATEWAY_JEV_API_KEY"),
            jev_model=_env("AIOPS_GATEWAY_JEV_MODEL", "typesafe/jev"),
            jev_timeout_seconds=_env_float("AIOPS_GATEWAY_JEV_TIMEOUT_SECONDS", 20.0),
            routing_risk_at_least=_env_float("AIOPS_GATEWAY_ROUTING_RISK_AT_LEAST", 0.5),
            routing_confidence_at_least=_env_float("AIOPS_GATEWAY_ROUTING_CONFIDENCE_AT_LEAST", 0.8),
        )

    def validate(self) -> None:
        if not self.bind_host or any(character.isspace() for character in self.bind_host):
            raise ValueError("AIOPS_GATEWAY_BIND is invalid")
        if not 1 <= self.port <= 65535:
            raise ValueError("AIOPS_GATEWAY_PORT must be between 1 and 65535")
        if not 1 <= self.max_workers <= 16:
            raise ValueError("AIOPS_GATEWAY_MAX_WORKERS must be between 1 and 16")
        for name, value in (
            ("AIOPS_GATEWAY_ROUTING_RISK_AT_LEAST", self.routing_risk_at_least),
            ("AIOPS_GATEWAY_ROUTING_CONFIDENCE_AT_LEAST", self.routing_confidence_at_least),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.jev_base_url and not self.jev_api_key:
            raise ValueError("AIOPS_GATEWAY_JEV_API_KEY is required when a Jev base URL is set")
        if not 0.1 <= self.event_poll_interval_seconds <= 10:
            raise ValueError("AIOPS_GATEWAY_EVENT_POLL_SECONDS must be between 0.1 and 10")
        if not 1 <= self.introspection_timeout_seconds <= 30:
            raise ValueError("AIOPS_GATEWAY_INTROSPECTION_TIMEOUT_SECONDS must be between 1 and 30")
        if self.server_config_file is None or not self.server_config_file.is_file():
            raise ValueError("gateway server production.env does not exist")


@dataclass(frozen=True, slots=True)
class GatewayClientProfile:
    name: str
    base_url: str
    device_id: str
    workspace_id: str

    @property
    def file_path(self) -> Path:
        return gateway_profile_path(self.name)


def gateway_profile_dir() -> Path:
    return config_root() / "gateway-profiles"


def gateway_profile_path(profile_name: str) -> Path:
    return gateway_profile_dir() / f"{validate_profile_name(profile_name)}.json"


def gateway_token_file(profile_name: str) -> Path:
    return config_root() / "gateway-tokens" / f"{validate_profile_name(profile_name)}.token"


def validate_profile_name(profile_name: str) -> str:
    candidate = profile_name.strip()
    if not SAFE_PROFILE_NAME.fullmatch(candidate):
        raise ValueError("profile name contains unsupported characters")
    return candidate


def canonical_gateway_url(value: str) -> str:
    candidate = value.strip()
    try:
        parsed = urlsplit(candidate)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("gateway URL is invalid") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("gateway URL must be a complete HTTP or HTTPS URL")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("remote gateway URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("gateway URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("gateway URL must not contain query or fragment")
    return candidate.rstrip("/")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    return float(raw) if raw else default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _private_config_values(path: Path) -> dict[str, str | None]:
    if not path.is_file():
        return {}
    try:
        validate_private_file(path)
    except PrivatePathError:
        return {}
    return dict(dotenv_values(path))


def _file_value(values: dict[str, str | None], name: str, default: str = "") -> str:
    return (values.get(name) or default).strip()
