from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

from aiops_diagnostics.config import selected_config_file, validate_key_slot_name
from aiops_diagnostics.platform_paths import config_root, data_root
from aiops_diagnostics.private_files import PrivatePathError, validate_private_file

SAFE_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

#: 入站来源密钥的最小长度（#444 / ADR-0009 D8）。它是**唯一**一道「请求来自那一次可信注入」
#: 的判据，且对着公网可达的入口，因此短到可枚举就等于没有这道门。长度上限不设：密钥由本侧
#: 生成，比下限长不构成风险。
MIN_SOURCE_KEY_LENGTH = 16


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
    #: 管家端公司 OAuth2 令牌的校验入口与客户端凭据（#443 / ADR-0009）。三个键**全部缺省为空**，
    #: 全空时该路径整体不构造 ⇒ 行为与启用前逐字一致（这既是「先合不启用」的机制保证，也是
    #: 回滚路径：清空即回退）。只设了其中一部分是启动错误，不是静默禁用 —— 与
    #: ``AIOPS_GATEWAY_INTROSPECTION_*`` 同一条形状（URL 有值时凭据必填）。
    company_check_token_url: str = ""
    company_token_client_id: str = ""
    company_token_client_secret: str = ""
    #: 入站来源密钥（#444 / ADR-0009 D8）。由**可信的那一跳**注入，前端不持有；带且验过才走
    #: 公司令牌路径，否则走既有链。**缺省为空 ⇒ 新链路整体不启用**（fail closed，不是放行）——
    #: 与上面三键同一条「全空即与今天逐字一致」的机制保证，也是回滚路径。
    #: 用 ``repr=False``：它与客户端密钥同性质，不得出现在任何 repr / 日志 / 审计摘要里。
    company_source_key: str = field(repr=False, default="")
    #: 本地校验模式的签名密钥（A2 / #448）：配了它就不再调上游 check_token。
    company_jwt_key: str = field(repr=False, default="")
    kb_service_base_url: str = ""
    #: Routing decision source (#392). Empty base URL or key leaves routing on
    #: the previous behaviour; the dependency is optional by configuration.
    jev_base_url: str = ""
    jev_api_key: str = ""
    jev_model: str = "typesafe/jev"
    jev_timeout_seconds: float = 20.0
    routing_risk_at_least: float = 0.5
    routing_confidence_at_least: float = 0.8
    #: High risk asks for context regardless of confidence (#401). False restores
    #: the "high risk AND unsure" form, which real traffic showed never fires.
    routing_risk_always_asks: bool = True
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
            company_check_token_url=_env("AIOPS_GATEWAY_COMPANY_CHECK_TOKEN_URL")
            or _file_value(file_values, "AIOPS_GATEWAY_COMPANY_CHECK_TOKEN_URL"),
            company_token_client_id=_env("AIOPS_GATEWAY_COMPANY_TOKEN_CLIENT_ID")
            or _file_value(file_values, "AIOPS_GATEWAY_COMPANY_TOKEN_CLIENT_ID"),
            company_token_client_secret=_env("AIOPS_GATEWAY_COMPANY_TOKEN_CLIENT_SECRET")
            or _file_value(file_values, "AIOPS_GATEWAY_COMPANY_TOKEN_CLIENT_SECRET"),
            company_source_key=_env("AIOPS_GATEWAY_COMPANY_SOURCE_KEY")
            or _file_value(file_values, "AIOPS_GATEWAY_COMPANY_SOURCE_KEY"),
            company_jwt_key=_env("AIOPS_GATEWAY_COMPANY_JWT_KEY")
            or _file_value(file_values, "AIOPS_GATEWAY_COMPANY_JWT_KEY"),
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
            routing_risk_always_asks=_env_bool("AIOPS_GATEWAY_ROUTING_RISK_ALWAYS_ASKS", True),
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
        # 半配置是启动错误而不是静默禁用：URL 有值说明运维确实想启用这条路径，而此时凭据缺失
        # 若只落回「不构造」，结果是一个看起来正常、实际一直 401 的部署。交给启动失败。
        if self.company_check_token_url and not (
            self.company_token_client_id and self.company_token_client_secret
        ):
            raise ValueError(
                "AIOPS_GATEWAY_COMPANY_TOKEN_CLIENT_ID and _CLIENT_SECRET are required "
                "when a company check_token URL is set"
            )
        # #444：来源密钥与校验入口是**同一条路径的两半**，半配置同样是启动失败。只配密钥会让
        # 运维以为「门装好了、新链路在跑」，而实际上没有可路由的目标 —— 每一条带密钥的请求都会
        # 静默落回既有链（对管家端令牌就是 401），与「没配密钥」在现象上不可区分。
        # 本地校验模式与远端 check_token 互斥；两者都不配时来源密钥无意义。
        if self.company_source_key and not (self.company_check_token_url or self.company_jwt_key):
            raise ValueError(
                "AIOPS_GATEWAY_COMPANY_CHECK_TOKEN_URL or AIOPS_GATEWAY_COMPANY_JWT_KEY is required "
                "when a source key is set"
            )
        if self.company_source_key and len(self.company_source_key) < MIN_SOURCE_KEY_LENGTH:
            raise ValueError(
                f"AIOPS_GATEWAY_COMPANY_SOURCE_KEY must be at least {MIN_SOURCE_KEY_LENGTH} characters"
            )
        if not 0.1 <= self.jev_timeout_seconds <= 120:
            raise ValueError("AIOPS_GATEWAY_JEV_TIMEOUT_SECONDS must be between 0.1 and 120")
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
