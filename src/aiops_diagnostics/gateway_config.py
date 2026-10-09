from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

from aiops_diagnostics.config import selected_config_file, validate_key_slot_name
from aiops_diagnostics.conversation_store import CONTEXT_MAX_TOKENS, CONTEXT_MAX_TURNS
from aiops_diagnostics.platform_paths import config_root, data_root
from aiops_diagnostics.private_files import PrivatePathError, validate_private_file

SAFE_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

#: 入站来源密钥的最小长度（#444 / ADR-0009 D8）。它是**唯一**一道「请求来自那一次可信注入」
#: 的判据，且对着公网可达的入口，因此短到可枚举就等于没有这道门。长度上限不设：密钥由本侧
#: 生成，比下限长不构成风险。
MIN_SOURCE_KEY_LENGTH = 16

#: 布尔类开关的「显式关闭」取值表。收全它是为了避免「配了但静默不生效」：
#: 只认一小撮时，运维写 `off` 会看起来像关掉了、实际仍是默认（本仓反复出现的形态）。
_FALSY_SETTINGS = frozenset({"0", "false", "no", "off", "disable", "disabled"})


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
    #: 是否按**公司自己的判据**认管家端令牌（默认 True = 不验签、不判 `exp`，与公司一致）。
    #: 设 False 回到严格模式（验签 + 判 `exp`）。判定写成「不是明确关闭就按公司一致」——
    #: 与本模块其余开关的取向一致：**默认值只有一个来源**。
    trust_company_payload: bool = True
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
    #: 面向 Dify 的知识检索适配路由（#581 / PRD #577）。**三键全空 ⇒ 该路由整体 404**，
    #: 与 ``kb_service_base_url`` 同一条「未配置即不启用」的机制保证；半配置是启动错误
    #: （登记表有值说明运维确实想暴露这个面，此时凭据缺失若只落回「不构造」，结果是一个
    #: 看起来正常、实际一律 401 的部署）。
    #: 凭据用 ``repr=False``：它是 Dify 侧持有的共享密钥，不得出现在 repr / 日志 / 审计里。
    dify_knowledge_api_key: str = field(repr=False, default="")
    #: ``knowledge_id`` → 登记行 的映射单行形式：``id:tenant:kb1|kb2``。Dify 只发
    #: ``knowledge_id``（一个不透明串），**没有租户概念**，所以「哪个租户、哪些知识库」
    #: 必须由我们的登记表决定 —— 这正是「不能成为 Dify 要什么就给什么的后门」的落点。
    dify_knowledge_bindings: str = ""
    #: 本路由转调知识库服务时的 top_k 上限。Dify 自己也会发 top_k；我们**封顶**而不是直接
    #: 采信，理由与 ``KnowledgeSearchGuard`` 的 ``max_results`` 相同：检索范围归我们控制。
    dify_knowledge_max_top_k: int = 5
    #: 调试身份的**专用凭据**（#586）。与生产那把**分离**：运营在 Dify 的调试预览里用它，
    #: 因此它不该是生产凭据（那把能到全部已登记的知识库）。留空 ⇒ 该身份整体不存在，
    #: 配置里的调试子集也随之无效 —— 「没有调试身份」是一个合法状态，不是半配置。
    dify_debug_api_key: str = field(repr=False, default="")
    #: 调试身份固定的**测试租户**（#586）。它**不可由请求改写**：解析函数不接受任何请求字段，
    #: 这个值直接进 ``DifyIdentity.fixed_tenant``。留空 ⇒ 调试身份无效（与凭据成对）。
    dify_debug_tenant: str = ""
    #: 调试身份可用的 ``knowledge_id`` 子集，逗号分隔。空 ⇒ 子集为空（调试身份**什么也取不到**），
    #: 不是"不收窄" —— 「配了调试凭据但没给子集」与「给了一个空子集」在语义上必须是同一件事，
    #: 否则一次漏写子集就等于把生产面开给了调试凭据。
    dify_debug_knowledge_ids: str = ""
    #: Conversation context window (#482). The defaults ARE the contract's
    #: numbers (8 turns / 8k tokens, ``conversation_store`` owns the constants);
    #: they are configurable because a RAG or promotional turn also spends the
    #: model's budget on retrieved chunks, and the only honest way to tune that
    #: trade-off is a knob rather than a second hardcoded copy.
    context_max_turns: int = CONTEXT_MAX_TURNS
    context_max_tokens: int = CONTEXT_MAX_TOKENS

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
            # 显式关闭的取值要**收全**（0/false/no/off/disable…）—— 只认一小撮时，
            # 运维写 `off` 会**静默保持公司一致**（看起来像配了、实际没生效），
            # 这正是本仓反复出现的「静默不生效」形态。未设 = 公司一致（默认）。
            trust_company_payload=(
                _env("AIOPS_GATEWAY_COMPANY_TRUST_PAYLOAD")
                or _file_value(file_values, "AIOPS_GATEWAY_COMPANY_TRUST_PAYLOAD")
                or "1"
            )
            .strip()
            .lower()
            not in _FALSY_SETTINGS,
            context_max_turns=_env_int("AIOPS_GATEWAY_CONVERSATION_MAX_TURNS", CONTEXT_MAX_TURNS),
            context_max_tokens=_env_int("AIOPS_GATEWAY_CONVERSATION_MAX_TOKENS", CONTEXT_MAX_TOKENS),
            kb_service_base_url=_env("AIOPS_GATEWAY_KB_SERVICE_BASE_URL")
            or _file_value(file_values, "AIOPS_GATEWAY_KB_SERVICE_BASE_URL"),
            kb_service_timeout_seconds=_env_float("AIOPS_GATEWAY_KB_SERVICE_TIMEOUT_SECONDS", 10.0),
            media_signing_secret=_env("AIOPS_GATEWAY_MEDIA_SIGNING_SECRET")
            or _file_value(file_values, "AIOPS_GATEWAY_MEDIA_SIGNING_SECRET"),
            media_ttl_seconds=_env_int("AIOPS_GATEWAY_MEDIA_TTL_SECONDS", 600),
            dify_knowledge_api_key=_env("AIOPS_GATEWAY_DIFY_KNOWLEDGE_API_KEY")
            or _file_value(file_values, "AIOPS_GATEWAY_DIFY_KNOWLEDGE_API_KEY"),
            dify_knowledge_bindings=_env("AIOPS_GATEWAY_DIFY_KNOWLEDGE_BINDINGS")
            or _file_value(file_values, "AIOPS_GATEWAY_DIFY_KNOWLEDGE_BINDINGS"),
            dify_knowledge_max_top_k=_env_int("AIOPS_GATEWAY_DIFY_KNOWLEDGE_MAX_TOP_K", 5),
            dify_debug_api_key=_env("AIOPS_GATEWAY_DIFY_DEBUG_API_KEY")
            or _file_value(file_values, "AIOPS_GATEWAY_DIFY_DEBUG_API_KEY"),
            dify_debug_tenant=_env("AIOPS_GATEWAY_DIFY_DEBUG_TENANT")
            or _file_value(file_values, "AIOPS_GATEWAY_DIFY_DEBUG_TENANT"),
            dify_debug_knowledge_ids=_env("AIOPS_GATEWAY_DIFY_DEBUG_KNOWLEDGE_IDS")
            or _file_value(file_values, "AIOPS_GATEWAY_DIFY_DEBUG_KNOWLEDGE_IDS"),
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
        # 两条校验通道**互斥**（A2 / #448）：「谁来断言这条令牌」只能有一个答案。两个都配时
        # 若只落回其中一条，运维看到的是「配置对了」，实际跑的是另一条 —— 因此交给启动失败。
        if self.company_jwt_key and self.company_check_token_url:
            raise ValueError(
                "AIOPS_GATEWAY_COMPANY_JWT_KEY and AIOPS_GATEWAY_COMPANY_CHECK_TOKEN_URL are "
                "mutually exclusive: token validity must have exactly one authority"
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
        # #581：登记表与凭据是**同一个面**的两半，半配置同样是启动错误。只配登记表会让运维
        # 以为「面已经暴露了」，实际每一条请求都因缺凭据 401 —— 与「没配」在现象上不可区分。
        if self.dify_knowledge_bindings and not self.dify_knowledge_api_key:
            raise ValueError(
                "AIOPS_GATEWAY_DIFY_KNOWLEDGE_API_KEY is required when Dify knowledge bindings are set"
            )
        if self.dify_knowledge_api_key and not self.dify_knowledge_bindings:
            raise ValueError(
                "AIOPS_GATEWAY_DIFY_KNOWLEDGE_BINDINGS is required when a Dify knowledge API key is set"
            )
        if not 1 <= self.dify_knowledge_max_top_k <= 20:
            raise ValueError("AIOPS_GATEWAY_DIFY_KNOWLEDGE_MAX_TOP_K must be between 1 and 20")
        # #586：调试身份的两半 —— 凭据与固定租户 —— 必须成对。只给凭据会让运维以为
        # 「调试身份建好了」，而实际它连自己是哪个租户都不知道，于是要么整体无效（静默），
        # 要么退化成「用调试凭据拿到了生产范围」（危险）。交给启动失败。
        if self.dify_debug_api_key and not self.dify_debug_tenant:
            raise ValueError("AIOPS_GATEWAY_DIFY_DEBUG_TENANT is required when a Dify debug API key is set")
        if self.dify_debug_tenant and not self.dify_debug_api_key:
            raise ValueError("AIOPS_GATEWAY_DIFY_DEBUG_API_KEY is required when a Dify debug tenant is set")
        # 调试凭据不得与生产凭据同值：同值等于没有分离，而"分离"正是这个身份存在的理由。
        if self.dify_debug_api_key and secrets.compare_digest(
            self.dify_debug_api_key, self.dify_knowledge_api_key
        ):
            raise ValueError(
                "AIOPS_GATEWAY_DIFY_DEBUG_API_KEY must differ from AIOPS_GATEWAY_DIFY_KNOWLEDGE_API_KEY"
            )
        # 生产面未启用时调试身份无从谈起（它只在同一条路由上生效）。
        if self.dify_debug_api_key and not self.dify_knowledge_api_key:
            raise ValueError(
                "AIOPS_GATEWAY_DIFY_KNOWLEDGE_API_KEY is required when a Dify debug identity is set "
                "(the debug identity narrows that route; it does not create it)"
            )
        # 登记表在这里解析一次：解析失败是**启动错误**，而不是第一个请求到达时才发现 ——
        # 后者会把一个配置错误伪装成一次运行时故障。
        parse_dify_knowledge_bindings(self.dify_knowledge_bindings)
        if not 0.1 <= self.jev_timeout_seconds <= 120:
            raise ValueError("AIOPS_GATEWAY_JEV_TIMEOUT_SECONDS must be between 0.1 and 120")
        if not 0.1 <= self.event_poll_interval_seconds <= 10:
            raise ValueError("AIOPS_GATEWAY_EVENT_POLL_SECONDS must be between 0.1 and 10")
        if not 1 <= self.introspection_timeout_seconds <= 30:
            raise ValueError("AIOPS_GATEWAY_INTROSPECTION_TIMEOUT_SECONDS must be between 1 and 30")
        if self.server_config_file is None or not self.server_config_file.is_file():
            raise ValueError("gateway server production.env does not exist")


@dataclass(frozen=True, slots=True)
class DifyKnowledgeBinding:
    """一条 ``knowledge_id`` 登记：它对应哪个租户、哪些知识库（#581）。

    Dify 侧只发 ``knowledge_id``，所以**租户与知识库集合只能在这里定**。把它做成显式
    登记而不是「从请求里读」，是本路由不是后门的唯一依据。
    """

    knowledge_id: str
    tenant_id: str
    knowledge_base_ids: tuple[str, ...]


def parse_dify_knowledge_bindings(value: str) -> dict[str, DifyKnowledgeBinding]:
    """解析 ``id:tenant:kb1|kb2`` 的多行/分号登记表。

    非法行**抛错**而不是跳过：跳过会让运维看到「配了 5 条、实际生效 3 条」而毫无提示，
    这正是本仓反复出现的「静默不生效」形态。
    """
    bindings: dict[str, DifyKnowledgeBinding] = {}
    for row in value.replace(";", "\n").splitlines():
        row = row.strip()
        if not row or row.startswith("#"):
            continue
        knowledge_id, _, rest = row.partition(":")
        tenant_id, _, kb_part = rest.partition(":")
        kbs = tuple(dict.fromkeys(item.strip() for item in kb_part.split("|") if item.strip()))
        if not knowledge_id.strip() or not tenant_id.strip() or not kbs:
            raise ValueError(f"dify knowledge binding is malformed: {row!r}")
        if knowledge_id in bindings:
            raise ValueError(f"dify knowledge binding is duplicated: {knowledge_id!r}")
        bindings[knowledge_id] = DifyKnowledgeBinding(
            knowledge_id=knowledge_id, tenant_id=tenant_id, knowledge_base_ids=kbs
        )
    return bindings


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
