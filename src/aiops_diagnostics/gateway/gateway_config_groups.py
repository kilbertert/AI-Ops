"""Gateway 配置的强类型分组视图与分段校验（认知成本简化 P2.7）。

将 `GatewayServerSettings` 的 80+ 扁平字段按职责归为五组强类型只读子配置，
并将 `common.config.Settings` 以 `DataSourcesConfig` 纳入同一配置树：

- `HttpServerConfig`：监听、工作线程、路径、路由阈值、内部令牌与日志级别
- `CallerAuthConfig`：OAuth2 内省、C 端 3rd_session 与管家公司令牌鉴权
- `DifyIntegrationConfig`：Dify 控制台/发布/检索/调试/试用与内网开户凭据
- `KnowledgeConfig`：kb-service、受限委派、签名媒体与 RAGFlow 控制台
- `OnboardingConfig`：部署/执行环境标识、环境目录、业务连接与执行/路由绑定
- `DataSourcesConfig`：`common.config.Settings`（MySQL/TDengine/Redis/UPMS/Dis/SSH/模型）的统一类型别名

对外环境变量（`AIOPS_GATEWAY_*`）与 `GatewayServerSettings` 扁平字段名保持 100% 兼容，
不改变任何部署配置键或现有调用方代码。
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from aiops_diagnostics.common.config import Settings as DataSourcesConfig
from aiops_diagnostics.common.endpoint_policy import (
    canonical_gateway_url,
    require_secure_endpoint,
)
from aiops_diagnostics.gateway.deployment_environment import canonical_environment
from aiops_diagnostics.gateway.gateway_logging import DEFAULT_LOG_LEVEL, parse_log_level

__all__ = [
    "CONFIG_GROUP_TYPES",
    "CallerAuthConfig",
    "DataSourcesConfig",
    "DifyIntegrationConfig",
    "HttpServerConfig",
    "KnowledgeConfig",
    "OnboardingConfig",
]


@dataclass(frozen=True, slots=True)
class HttpServerConfig:
    """网关进程基础运行参数、监听地址、路由阈值与进程间内部令牌。"""

    data_home: Path
    database_file: Path
    server_config_file: Path | None = None
    bind_host: str = "127.0.0.1"
    port: int = 8787
    allow_fixtures: bool = False
    max_workers: int = 2
    public_base_url: str = ""
    log_level: str = DEFAULT_LOG_LEVEL
    internal_token_secret: str = field(repr=False, default="")
    internal_token_expire_seconds: int = 300
    routing_risk_at_least: float = 0.5
    routing_confidence_at_least: float = 0.8
    routing_risk_always_asks: bool = True

    def validate(self, *, require_server_config_file: bool = True) -> None:
        if not self.bind_host or any(character.isspace() for character in self.bind_host):
            raise ValueError("AIOPS_GATEWAY_BIND is invalid")
        parse_log_level(self.log_level)
        if not 1 <= self.port <= 65535:
            raise ValueError("AIOPS_GATEWAY_PORT must be between 1 and 65535")
        if not 1 <= self.max_workers <= 16:
            raise ValueError("AIOPS_GATEWAY_MAX_WORKERS must be between 1 and 16")
        if not 1 <= self.internal_token_expire_seconds <= 3600:
            raise ValueError("AIOPS_GATEWAY_INTERNAL_TOKEN_EXPIRE_SECONDS must be between 1 and 3600")
        for name, value in (
            ("AIOPS_GATEWAY_ROUTING_RISK_AT_LEAST", self.routing_risk_at_least),
            ("AIOPS_GATEWAY_ROUTING_CONFIDENCE_AT_LEAST", self.routing_confidence_at_least),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        if require_server_config_file and (
            self.server_config_file is None or not self.server_config_file.is_file()
        ):
            raise ValueError("gateway server production.env does not exist")


@dataclass(frozen=True, slots=True)
class CallerAuthConfig:
    """调用方身份校验配置（OAuth2 内省、C 端 3rd_session 与管家公司令牌）。"""

    introspection_url: str = ""
    introspection_client_id: str = ""
    introspection_client_secret: str = field(repr=False, default="")
    standard_api_audience: str = "aiops-api"
    introspection_timeout_seconds: int = 5
    third_session_service_token: str = field(repr=False, default="")
    third_session_key_prefix: str = "app:3rd_session:"
    company_check_token_url: str = ""
    company_token_client_id: str = ""
    company_token_client_secret: str = field(repr=False, default="")
    company_source_key: str = field(repr=False, default="")
    company_jwt_key: str = field(repr=False, default="")
    trust_company_payload: bool = True
    company_gateway_trust_file: str = ""

    def validate(self, *, min_source_key_length: int) -> None:
        if not 1 <= self.introspection_timeout_seconds <= 30:
            raise ValueError("AIOPS_GATEWAY_INTROSPECTION_TIMEOUT_SECONDS must be between 1 and 30")
        if self.company_check_token_url and not (
            self.company_token_client_id and self.company_token_client_secret
        ):
            raise ValueError(
                "AIOPS_GATEWAY_COMPANY_TOKEN_CLIENT_ID and _CLIENT_SECRET are required "
                "when a company check_token URL is set"
            )
        if self.company_jwt_key and self.company_check_token_url:
            raise ValueError(
                "AIOPS_GATEWAY_COMPANY_JWT_KEY and AIOPS_GATEWAY_COMPANY_CHECK_TOKEN_URL are "
                "mutually exclusive: token validity must have exactly one authority"
            )
        if self.company_source_key and not (self.company_check_token_url or self.company_jwt_key):
            raise ValueError(
                "AIOPS_GATEWAY_COMPANY_CHECK_TOKEN_URL or AIOPS_GATEWAY_COMPANY_JWT_KEY is required "
                "when a source key is set"
            )
        if self.company_source_key and len(self.company_source_key) < min_source_key_length:
            raise ValueError(
                f"AIOPS_GATEWAY_COMPANY_SOURCE_KEY must be at least {min_source_key_length} characters"
            )


@dataclass(frozen=True, slots=True)
class DifyIntegrationConfig:
    """Dify 控制台、发布/检索/调试/试用路由及内网工作区开户配置。"""

    dify_chat_base_url: str = ""
    dify_console_base_url: str = ""
    dify_console_api_key: str = field(repr=False, default="")
    dify_console_workspace_id: str = ""
    dify_external_knowledge_api_id: str = ""
    dify_publish_api_key: str = field(repr=False, default="")
    dify_knowledge_api_key: str = field(repr=False, default="")
    dify_knowledge_bindings: str = ""
    dify_knowledge_autoregister: bool = True
    dify_debug_api_key: str = field(repr=False, default="")
    dify_debug_tenant: str = ""
    dify_debug_knowledge_ids: str = ""
    dify_trial_api_key: str = field(repr=False, default="")
    dify_trial_debug_tenant: str = ""
    dify_inner_api_base_url: str = ""
    dify_inner_api_key: str = field(repr=False, default="")
    dify_workspace_model_api_key: str = field(repr=False, default="")
    dify_workspace_model_endpoint: str = "https://api.deepseek.com/v1"
    chat_dify_key_dir: str = ""
    operator_trial_login_url: str = ""
    operator_trial_key: str = field(repr=False, default="")

    def validate(self, *, parse_knowledge_bindings: Any) -> None:
        if bool(self.operator_trial_login_url) != bool(self.operator_trial_key):
            raise ValueError("operator trial login URL and key must be configured together")
        if self.operator_trial_key:
            if len(self.operator_trial_key) < 32:
                raise ValueError("operator trial key must contain at least 32 characters")
            canonical_gateway_url(self.operator_trial_login_url)
        if self.dify_chat_base_url:
            canonical_gateway_url(self.dify_chat_base_url)
        if self.dify_knowledge_bindings and not self.dify_knowledge_api_key:
            raise ValueError(
                "AIOPS_GATEWAY_DIFY_KNOWLEDGE_API_KEY is required when Dify knowledge bindings are set"
            )
        if self.dify_knowledge_api_key and not self.dify_knowledge_bindings:
            raise ValueError(
                "AIOPS_GATEWAY_DIFY_KNOWLEDGE_BINDINGS is required when a Dify knowledge API key is set"
            )
        if self.dify_console_base_url and not self.dify_console_api_key:
            raise ValueError(
                "AIOPS_GATEWAY_DIFY_CONSOLE_API_KEY is required when a Dify console base URL is set"
            )
        if self.dify_console_api_key and not self.dify_console_base_url:
            raise ValueError(
                "AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL is required when a Dify console API key is set"
            )
        if self.dify_console_base_url:
            require_secure_endpoint(self.dify_console_base_url, what="AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL")
        if self.dify_publish_api_key and not (self.dify_console_base_url and self.dify_console_api_key):
            raise ValueError(
                "AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL/API_KEY are required when the Dify publish "
                "route is enabled (the route pulls from that console)"
            )
        for other_name, other in (
            ("AIOPS_GATEWAY_DIFY_KNOWLEDGE_API_KEY", self.dify_knowledge_api_key),
            ("AIOPS_GATEWAY_DIFY_DEBUG_API_KEY", self.dify_debug_api_key),
        ):
            if (
                self.dify_publish_api_key
                and other
                and secrets.compare_digest(self.dify_publish_api_key, other)
            ):
                raise ValueError(
                    f"AIOPS_GATEWAY_DIFY_PUBLISH_API_KEY must differ from {other_name} "
                    "(one key must not authorise both reading knowledge and publishing)"
                )
        if self.dify_debug_api_key and not self.dify_debug_tenant:
            raise ValueError("AIOPS_GATEWAY_DIFY_DEBUG_TENANT is required when a Dify debug API key is set")
        if self.dify_debug_tenant and not self.dify_debug_api_key:
            raise ValueError("AIOPS_GATEWAY_DIFY_DEBUG_API_KEY is required when a Dify debug tenant is set")
        if self.dify_debug_api_key and secrets.compare_digest(
            self.dify_debug_api_key, self.dify_knowledge_api_key
        ):
            raise ValueError(
                "AIOPS_GATEWAY_DIFY_DEBUG_API_KEY must differ from AIOPS_GATEWAY_DIFY_KNOWLEDGE_API_KEY"
            )
        if self.dify_debug_api_key and not self.dify_knowledge_api_key:
            raise ValueError(
                "AIOPS_GATEWAY_DIFY_KNOWLEDGE_API_KEY is required when a Dify debug identity is set "
                "(the debug identity narrows that route; it does not create it)"
            )
        if self.dify_trial_api_key and not self.dify_trial_debug_tenant:
            raise ValueError(
                "AIOPS_GATEWAY_DIFY_TRIAL_DEBUG_TENANT is required when the Dify trial route is enabled"
            )
        if self.dify_trial_debug_tenant and not self.dify_trial_api_key:
            raise ValueError(
                "AIOPS_GATEWAY_DIFY_TRIAL_API_KEY is required when a Dify trial debug tenant is set"
            )
        for other_name, other in (
            ("AIOPS_GATEWAY_DIFY_KNOWLEDGE_API_KEY", self.dify_knowledge_api_key),
            ("AIOPS_GATEWAY_DIFY_DEBUG_API_KEY", self.dify_debug_api_key),
            ("AIOPS_GATEWAY_DIFY_PUBLISH_API_KEY", self.dify_publish_api_key),
            ("AIOPS_GATEWAY_DIFY_CONSOLE_API_KEY", self.dify_console_api_key),
        ):
            if self.dify_trial_api_key and other and secrets.compare_digest(self.dify_trial_api_key, other):
                raise ValueError(
                    f"AIOPS_GATEWAY_DIFY_TRIAL_API_KEY must differ from {other_name} "
                    "(one key must not authorise both running a trial and another Dify surface)"
                )
        parse_knowledge_bindings(self.dify_knowledge_bindings)


@dataclass(frozen=True, slots=True)
class KnowledgeConfig:
    """知识库检索、受限知识委派、签名媒体与 RAGFlow 控制台跳转配置。"""

    kb_service_base_url: str = ""
    knowledge_delegation_file: str = ""
    kb_service_timeout_seconds: float = 10.0
    kb_search_max_results: int = 10
    media_signing_secret: str = field(repr=False, default="")
    media_ttl_seconds: int = 600
    ragflow_credential_key: str = field(repr=False, default="")
    ragflow_tenant_domain: str = "kb.internal"
    ragflow_embedding_base_url: str = ""
    ragflow_embedding_api_key: str = field(repr=False, default="")
    ragflow_embedding_model: str = "text-embedding-3-large"
    ragflow_console_base_url: str = ""
    ragflow_console_public_url: str = ""
    ragflow_console_public_pem: str = ""
    ragflow_console_cookie_domain: str = ""
    knowledge_hop_ttl_seconds: float = 60.0

    def validate(self) -> None:
        if self.knowledge_delegation_file and self.kb_service_base_url:
            raise ValueError("delegated knowledge cannot also expose shared kb-service")
        if not 1 <= self.kb_search_max_results <= 20:
            raise ValueError("AIOPS_GATEWAY_KB_SEARCH_MAX_RESULTS must be between 1 and 20")


@dataclass(frozen=True, slots=True)
class OnboardingConfig:
    """环境身份、多环境接入目录、业务连接与聊天/节点路由绑定配置。"""

    environment: str = ""
    execution_environment: str = ""
    charging_status_base_url: str = ""
    business_environments_file: str = ""
    environment_registry_file: str = ""
    environment_management_registry_file: str = ""
    environment_onboarding_file: str = ""
    environment_installation_file: str = ""
    environment_task_key: str = field(repr=False, default="")
    environment_manifest_file: str = ""
    consumer_order_chat_enabled: bool = False
    consumer_chatflow_enabled: bool = False
    consumer_publication_targets_file: str = ""
    agent_run_config_file: str = ""
    execution_manifest_file: str = ""
    chat_execution_bindings_file: str = ""
    chat_route_bindings_file: str = ""
    chat_node_targets_file: str = ""
    operator_profiles_file: str = ""
    operator_tsdata_url: str = ""

    def validate(
        self,
        *,
        settings: Any,
        safe_profile_name: Any,
        parse_charging_endpoints: Any,
        other_keys: tuple[str, ...],
    ) -> str:
        """校验环境接入与执行绑定，返回归一化后的部署环境标识。"""
        normalized_env = canonical_environment(self.environment) if self.environment else ""
        if self.environment_task_key and (
            len(self.environment_task_key) < 32
            or any(value and secrets.compare_digest(self.environment_task_key, value) for value in other_keys)
        ):
            raise ValueError("environment task key must be independent and at least 32 characters")
        if self.charging_status_base_url:
            parse_charging_endpoints(self.charging_status_base_url)
            if not safe_profile_name.fullmatch(self.execution_environment):
                raise ValueError("charging status requires a logical execution environment")
        if self.environment_management_registry_file:
            from aiops_diagnostics.gateway.environment_registry import MANAGED_SCHEMA, load

            body = load(self.environment_management_registry_file)
            if body["schema"] != MANAGED_SCHEMA:
                raise ValueError("environment management requires managed registry")
            if (
                self.environment_registry_file
                and Path(self.environment_registry_file).resolve()
                != Path(self.environment_management_registry_file).resolve()
            ):
                raise ValueError("management and runtime must share the same environment registry")
        if self.environment_registry_file:
            if self.business_environments_file:
                raise ValueError("runtime environment registry cannot use a separate business catalogue")
            from aiops_diagnostics.gateway.environment_registry import for_settings

            row = for_settings(settings).entry()
            if Path(row["config_ref"]).resolve() != Path(settings.server_config_file).resolve():
                raise ValueError("environment registry configuration does not match this Gateway")
            if self.execution_environment and self.execution_environment != normalized_env:
                raise ValueError("execution environment does not match business environment")
        if self.business_environments_file:
            from aiops_diagnostics.consumer import business_environments

            try:
                for entry in business_environments.load(self.business_environments_file):
                    canonical_gateway_url(entry["base_url"])
            except (business_environments.BusinessCatalogueError, ValueError) as exc:
                raise ValueError(f"business environment catalogue is unusable: {exc}") from exc
        if self.consumer_order_chat_enabled and not self.charging_status_base_url:
            raise ValueError("consumer order chat requires its charging status endpoint")
        if self.agent_run_config_file and not (self.charging_status_base_url and self.execution_environment):
            raise ValueError("agent run delegation requires charging status settings")
        if self.execution_manifest_file and not self.agent_run_config_file:
            raise ValueError("execution manifest requires agent run delegation")
        if self.consumer_chatflow_enabled and not (
            settings.dify_chat_base_url and self.execution_environment
        ):
            raise ValueError("consumer chatflow requires Dify and execution environment")
        if self.chat_execution_bindings_file:
            from aiops_diagnostics.execution import chat_execution

            if not (self.charging_status_base_url and self.execution_environment):
                raise ValueError("chat execution requires charging status settings")
            try:
                bindings = chat_execution.load_bindings(self.chat_execution_bindings_file)
            except chat_execution.ChatBindingError as exc:
                raise ValueError(f"chat execution bindings are unusable: {exc}") from exc
            if (
                any(item.engine == chat_execution.ENGINE_DIFY for item in bindings)
                and not settings.dify_chat_base_url
            ):
                raise ValueError("dify chat execution requires a dify base url")
        if self.chat_route_bindings_file:
            from aiops_diagnostics.execution import chat_execution
            from aiops_diagnostics.execution.chat_route_bindings import (
                ChatRouteBindingStore,
                RouteBindingError,
            )

            try:
                chat_execution.assert_no_binding_collision(settings)
                managed = chat_execution._bindings_from_store(self.chat_route_bindings_file)
            except (chat_execution.ChatBindingError, RouteBindingError) as exc:
                raise ValueError(f"chat route bindings are unusable: {exc}") from exc
            if (
                any(item.engine == chat_execution.ENGINE_DIFY for item in managed)
                and not settings.dify_chat_base_url
            ):
                raise ValueError("dify chat execution requires a dify base url")
            try:
                ChatRouteBindingStore(Path(self.chat_route_bindings_file).expanduser().absolute())
            except (OSError, sqlite3.Error) as exc:  # pragma: no cover - 权限/磁盘问题
                raise ValueError(f"chat route bindings are unusable: {type(exc).__name__}") from exc
        if self.execution_manifest_file:
            from aiops_diagnostics.execution.execution_manifest import (
                ExecutionManifestError,
            )
            from aiops_diagnostics.execution.execution_manifest import (
                load as load_execution_manifest,
            )

            try:
                load_execution_manifest(self.execution_manifest_file)
            except ExecutionManifestError as exc:
                raise ValueError(f"execution manifest is invalid: {exc}") from exc
        return normalized_env


CONFIG_GROUP_TYPES: dict[str, type[Any]] = {
    "http": HttpServerConfig,
    "caller_auth": CallerAuthConfig,
    "dify": DifyIntegrationConfig,
    "knowledge": KnowledgeConfig,
    "onboarding": OnboardingConfig,
}


def build_group(group_cls: type[Any], settings: Any) -> Any:
    """从 `GatewayServerSettings` 实例投影出指定分组的强类型只读视图。"""
    return group_cls(**{item.name: getattr(settings, item.name) for item in fields(group_cls)})
