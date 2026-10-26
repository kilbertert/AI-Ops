from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import platform
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aiops_diagnostics import __version__, chat_execution, execution_manifest, request_observation
from aiops_diagnostics.agent_content import AGENT_STARTERS
from aiops_diagnostics.agent_lifecycle import (
    AGENT_MANAGE_SCOPE,
    AgentConfig,
    AgentConflict,
    AgentError,
    AgentForbidden,
    AgentManager,
    AgentNotFound,
    AgentStore,
    allowed_models_from_settings,
)
from aiops_diagnostics.agent_runs import (
    EXPIRED,
    FAILED,
    REVOKED,
    RUNNING,
    SUCCEEDED,
    AgentRun,
    AgentRunConfig,
    AgentRunConfigError,
    AgentRunDispatchError,
    AgentRunStore,
    SnailDispatcher,
    scope_from_json,
)
from aiops_diagnostics.async_job_lifecycle import DIAGNOSIS, QUESTION
from aiops_diagnostics.caller_auth import (
    CALLER_AUTH_CONFIG_MISSING,
    CALLER_AUTH_FORBIDDEN,
    SOURCE_KEY_HEADER,
    CallerAuthError,
    CallerContextResolver,
    DisabledCallerResolver,
    DisabledOrderAuthorizer,
    IntrospectionCallerResolver,
    IntrospectionSettings,
    OrderAuthorizer,
    ScopedOrderAuthorizer,
    SourceKeyCallerResolver,
    UpmsCallerResolver,
)
from aiops_diagnostics.charging_status import ChargingStatusError, read_charging_status
from aiops_diagnostics.clarification import (
    CLARIFY_ACCOUNT_HAS_NO_SITES,
    CLARIFY_CONTEXT_REQUIRED,
    CLARIFY_LANGUAGE_UNROUTABLE,
    CLARIFY_ORDER_NOT_YOURS,
    CLARIFY_ORDER_REQUIRED,
    CLARIFY_WRONG_ENTRY,
    MESSAGE_KEY_BY_CODE,
    missing_fields_for,
)
from aiops_diagnostics.codex_runtime import AgentRuntimeError
from aiops_diagnostics.company_token_auth import (
    CompanyTokenCallerResolver,
    CompanyTokenSettings,
    PermissionSource,
)
from aiops_diagnostics.config import Settings
from aiops_diagnostics.consumer_order_chat import asks_latest_order, is_order_status_question, status_answer
from aiops_diagnostics.conversation_store import (
    ConversationBusy,
    ConversationError,
    ConversationStore,
)
from aiops_diagnostics.dify_debug_identity import (
    PRODUCTION_IDENTITY,
    DifyIdentity,
    debug_dify_identity,
)
from aiops_diagnostics.dify_knowledge_api import (
    DIFY_KNOWLEDGE_LIST_PATH,
    DIFY_RETRIEVAL_PATH,
    DifyRequestError,
    dify_records,
    parse_dify_retrieval_request,
)
from aiops_diagnostics.dify_run_store import DifyRunStore
from aiops_diagnostics.dify_run_store import still_valid as dify_run_still_valid
from aiops_diagnostics.dify_tools_catalog import AI_OPS_TOOL_NAMES, CATALOG_NOTE
from aiops_diagnostics.dify_trial import TrialDraft, TrialInitiator, TrialRefused
from aiops_diagnostics.dify_workspaces import DifyWorkspaceError, DifyWorkspaces
from aiops_diagnostics.faq import (
    FAQ_NOT_FOUND,
    PLATFORM_AMBIGUOUS,
    PLATFORM_FORBIDDEN,
    PLATFORM_OPERATOR,
    PLATFORM_UNAVAILABLE,
    FAQCatalog,
    FAQError,
    MySQLPlatformDirectory,
    PlatformDecision,
    PlatformDirectoryError,
    PlatformIdentityResolver,
)
from aiops_diagnostics.gateway_config import (
    GatewayServerSettings,
    parse_dify_knowledge_bindings,
    parse_dify_tool_bindings,
)
from aiops_diagnostics.gateway_runtime import GatewayRuntime, close_gateway_runtime
from aiops_diagnostics.gateway_store import (
    ACTIVE_DIAGNOSIS_STATUSES,
    ACTIVE_HEALTH_JOB_STATUSES,
    TERMINAL_DIAGNOSIS_STATUSES,
    TERMINAL_RUN_STATUSES,
    AuthenticationError,
    EnrollmentError,
    GatewayDevice,
    GatewayStore,
    RunNotFoundError,
)
from aiops_diagnostics.http_auth import INTERNAL_TOKEN_HEADER, TIMESTAMP_HEADER, validate_internal_token
from aiops_diagnostics.i18n import (
    DEFAULT_LANGUAGE,
    QA_FALLBACK_MESSAGES,
    can_prompt_in,
    clarification_message,
    diagnosis_error_message,
    effective_language,
    free_text_unavailable_message,
    resolve_language,
)
from aiops_diagnostics.knowledge_retrieval import (
    KnowledgeSearchUnavailable,
    normalize_search_response,
)
from aiops_diagnostics.knowledge_sources import KnowledgeSourceResolver
from aiops_diagnostics.metrics_store import MetricsValidationError
from aiops_diagnostics.ops_bindings import ops_app_registered
from aiops_diagnostics.ops_tools import ChargingInfoInput, OpsToolError, OpsToolService, OrderListInput
from aiops_diagnostics.order_visibility import DeviceTenantError, tenant_matches
from aiops_diagnostics.private_files import PrivatePathError, validate_private_file, write_private_text
from aiops_diagnostics.query_scope import (
    ShopDirectory,
    UpmsShopDirectory,
    resolve_query_scope,
)
from aiops_diagnostics.routing import MISSING_ORDER_NO, money_question_context
from aiops_diagnostics.scope_context import (
    SCOPE_TYPE_ORGAN,
    SCOPE_TYPE_SELF,
    ScopeContext,
    ScopeError,
    is_operator_entry,
)
from aiops_diagnostics.shortcut_lifecycle import (
    BANNER_KIND,
    BUTTON_KIND,
    OPERATIONS_REVIEW_CODE,
    SHORTCUT_MANAGE_SCOPE,
    TENANT_SCOPE,
    ShortcutConflict,
    ShortcutError,
    ShortcutForbidden,
    ShortcutManager,
    ShortcutNotFound,
    ShortcutStore,
)
from aiops_diagnostics.sources import SourceError, scoped_mysql_source
from aiops_diagnostics.third_session_auth import (
    BSubjectDirectory,
    OperatorSiteScope,
    RedisThirdSessionResolver,
    ThirdSessionSettings,
    UpmsBSubjectDirectory,
    UpmsOperatorSiteScope,
)

_LOGGER = logging.getLogger("aiops.gateway")

STANDARD_ORDER_READ_SCOPE = "aiops:orders:read"
STANDARD_DIAGNOSIS_SCOPE = "aiops:diagnoses:write"
STANDARD_FAQ_SCOPE = "aiops:faq:read"
SAFE_ORDER_NO = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class EnrollRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=20, max_length=256)
    device_name: str = Field(min_length=1, max_length=128)
    platform: str = Field(min_length=1, max_length=128)


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    problem: str = Field(min_length=1, max_length=4000)
    order_no: str | None = Field(default=None, max_length=128)
    # Deliberately no character pattern: the tenant a request names is judged by
    # the shared entry rule (``resolve_device_tenant``), which normalizes both
    # sides, treats a blank one as absent, and refuses a genuinely different one.
    # A pattern here would be a second tenant decision at the transport layer —
    # the duplication this boundary exists to remove — and would turn a padded
    # identifier that normalizes onto the enrolled tenant into a 422 instead of
    # the run it is entitled to. Only the length bound stays, as for every other
    # bounded identifier on this surface (#331).
    tenant_id: str | None = Field(default=None, max_length=128)
    key_slot: str | None = Field(default=None, max_length=64)
    provider: str | None = Field(default=None, max_length=64)
    fixture_name: str | None = Field(default=None, max_length=128)


class ChargingStatusDelegationRequest(BaseModel):
    """委派入口的请求体：**空**。

    环境、appId、agentId、sid 都不接受：``extra="forbid"`` 下多一个字段就是 422。
    这比「收下但忽略」强 —— 后者会让一次成功的越权提交看起来像正常请求。
    """

    model_config = ConfigDict(extra="forbid")


class NodeClaimRequest(BaseModel):
    """节点领取端点的请求体：同样为空，用于拒绝任何覆盖身份或业务范围的尝试。"""

    model_config = ConfigDict(extra="forbid")


class NodeLoopCompletionRequest(BaseModel):
    """整轮终局通知的请求体。只有状态与一个短原因码，**没有业务数据、没有身份字段**。

    身份来自凭据与 ``X-AI-Ops-Exec-Config`` 头（与领取同一套判据），
    因此这里 ``extra="forbid"``：多一个字段就是 422，而不是被忽略。
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "failed"]
    reason: str | None = Field(default=None, max_length=64, pattern=r"^[a-z0-9_]*$")


class HealthReportJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_no: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


class AssistantQuestionRequest(BaseModel):
    """Free-text question for the unified assistant entry point.

    ``order_no`` is OPTIONAL here — unlike the order diagnosis endpoint. When
    absent, the classifier routes to a generic (zero-order) answer or a FAQ
    short-circuit. When present, it is honored as an explicit order diagnosis.
    ``conversation_id`` (T4/#172) optionally binds the question to a
    conversation: its confirmed ``active_order`` lets follow-up order
    questions omit the order number (ownership re-verified every turn), and
    the turn is recorded in the conversation history.
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4000)
    photo_id: str | None = Field(default=None, pattern=r"^photo_[a-f0-9]{32}$")
    order_no: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.:-]+$",
    )
    conversation_id: str | None = Field(
        default=None,
        min_length=8,
        max_length=64,
        pattern=r"^conv_[A-Za-z0-9]{1,57}$",
    )
    shortcut_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$",
    )


class StandardDiagnosisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_no: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    question: str = Field(min_length=1, max_length=4000)
    indicator_code: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]{0,63}$",
    )


class OperationsAnalysisRequest(BaseModel):
    """站点集合 + 时间窗 + 要哪张卡（#697 / #698）。没有 ``order_no``——这是与诊断的根本区别。

    ``card`` 是同一张面上的**明确种类**，不是新路由：三种卡的作用域、时间窗语义、
    范围下推与错误形状完全相同，差别只在聚合与契约里的字段。开三条路由会把同一套
    入口门、参数校验与响应构建复制三份。
    """

    model_config = ConfigDict(extra="forbid")

    card: str = Field(default="failure_rate", pattern=r"^(failure_rate|offline|fault_code|payment)$")
    site_ids: list[str] | None = Field(default=None, max_length=1000)
    window_start: datetime
    window_end: datetime


class OperatorPublishRequest(BaseModel):
    """运营自助发布（#577）：只有 ``app_id``。

    与 ``DifyPublishRequest`` 同形状是刻意的 —— 两条路走**同一个发布实现**，
    请求体都窄到只能点名一个**已声明**的绑定。租户与入口由**调用者身份**决定，
    调用方无法用请求体改写它们。
    """

    model_config = ConfigDict(extra="forbid")

    app_id: str = Field(min_length=1, max_length=64)


class DifyPublishRequest(BaseModel):
    """Only an app id. Everything else about the binding is ours, not the caller's.

    That restriction *is* the security property (``dify_publish``): a leaked key
    can ask to re-freeze a declared binding's current content, and nothing else.
    An ``extra="forbid"`` model is what keeps it that way — a field added here
    later (say a tenant, or a prompt) would widen the blast radius, so it has to
    be a deliberate edit to this class rather than a quiet one to a route.

    The initiator (#780) rides on **headers**, not this body, and for the same
    reason: it is audit metadata, never an authorization input. A caller that
    omits it gets a normal publish with "unknown" recorded — it can neither
    grant nor deny anything.
    """

    model_config = ConfigDict(extra="forbid")

    app_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class DifyRetractRequest(BaseModel):
    """撤回一个已绑定的 app 的正式版本。只有 app_id —— 与发布同一道爆炸半径。

    租户、入口、场景全部来自 `dify_app_registry` 与服务端配置；撤回**不能**指定
    "恢复到哪一版"：恢复是另一次显式发布，不是撤回的副作用。
    """

    model_config = ConfigDict(extra="forbid")

    app_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class DifyRestoreRequest(DifyPublishRequest):
    version_no: int = Field(ge=1, strict=True)


class DifyTrialRequest(BaseModel):
    """一次试运行：订单号 + **草稿**。范围与发起人**都不在这里**（#772 F / #781）。

    ``extra="forbid"`` 是这条路由安全主张的一半：请求体里加一个 ``tenant_id`` 或
    ``site_ids`` 会让人以为它起了作用，而实际从不参与判定。所以多一个字段就是 400，
    而不是安静忽略 —— 试运行的订单范围只由**服务端配置的调试租户**决定（#782 上线后
    改为发起人的公司身份）。

    草稿来源有**两条**，且必须恰好给一条：

    * ``prompt`` —— 调用方直接给方法论正文（原 #781 的形状，测试与 CLI 用）；
    * ``app_id`` —— **服务端自己去 Dify 拉**这个 app 最近一次保存的配置。

    第二条是给控制台按钮用的（#775 部署层）：Dify 1.17.1 的 chat 应用**没有服务端草稿
    层**（#773 结论），编辑页里"还没点发布更新"的内容只在浏览器内存里 —— 任何 API 都
    读不到。所以"试运行当前草稿"实际能拿到的就是**最近一次保存**的配置，而读它的正确
    地点是**网关**（它已有 console 凭据，且 `resolve_pulled_config` 是"这份草稿发布出去
    会变成什么"的唯一定义处）。让浏览器或那个薄桥去解析 DSL，会造出第二份映射规则 ——
    正是 `resolve_pulled_config` 的 docstring 记下的那类漂移。

    ``allowed_tools`` 缺省为**空**（仅在 ``prompt`` 那条路上有约束意义）：本票的语义是
    "草稿可以收窄"，所以勾了什么就是什么；空表示这份草稿不要任何工具（与 ADR-0019 的
    "不声明 = 全部"不同，那是**发布**侧的默认，试运行不该继承它 —— 试运行试的就是你
    眼前这份勾选）。
    """

    model_config = ConfigDict(extra="forbid")

    order_no: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
    prompt: str | None = Field(default=None, min_length=1, max_length=20000)
    app_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[0-9a-fA-F-]{8,64}$")
    allowed_tools: list[str] = Field(default_factory=list, max_length=7)
    question: str = Field(default="check order status", min_length=1, max_length=4000)

    @model_validator(mode="after")
    def _exactly_one_draft_source(self) -> DifyTrialRequest:
        """草稿要么显式给，要么给个 app_id 让服务端去拉 —— **不能两个都无、也不能都填**。

        两个都填是歧义（谁赢？），两个都无是"跑一次没有方法论的诊断"。两者都拒，而不是
        挑一个 —— 挑一个会让调用方以为自己那半边生效了。
        """
        has_prompt = bool(self.prompt)
        has_app = bool(self.app_id)
        if has_prompt and has_app:
            raise ValueError("给 prompt 与 app_id 二者只能有一个（草稿来源必须唯一）")
        if not has_prompt and not has_app:
            raise ValueError("必须给 prompt（显式草稿）或 app_id（服务端拉最近一次保存的配置）")
        return self


class FAQAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z]+\.[a-z0-9-]+\.q[0-9]{3}$")


class AgentConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_type: str = Field(min_length=1, max_length=32)
    prompt: str = Field(min_length=1, max_length=8000)
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=20)
    model: str = Field(min_length=1, max_length=128)
    output_contract: str = Field(min_length=1, max_length=64)
    opening_questions: list[str] = Field(default_factory=list, max_length=20)
    quick_commands: list[str] = Field(default_factory=list, max_length=20)

    def to_config(self) -> AgentConfig:
        return AgentConfig(
            agent_type=self.agent_type,
            prompt=self.prompt,
            knowledge_base_ids=tuple(self.knowledge_base_ids),
            model=self.model,
            output_contract=self.output_contract,
            opening_questions=tuple(self.opening_questions),
            quick_commands=tuple(self.quick_commands),
        )


class AgentCreateRequest(AgentConfigRequest):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=1000)


class AgentUpdateRequest(AgentCreateRequest):
    expected_revision: int = Field(ge=1)


class AgentActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class AgentDebugRunRequest(BaseModel):
    """One draft debug turn (T5/#171): question in, blocks preview out."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4000)


class KnowledgeSourceRequest(BaseModel):
    """One Dify knowledge source, as an operator fills it in.

    The field names are Dify's own (`knowledge_id`) because the operator is
    typing them into Dify's dialog — an internal name here would force a
    translation step at exactly the place people make mistakes.

    `knowledge_base_ids` is a LIST, not one id: our adapter already merges
    several RAGFlow datasets per knowledge_id, and a picker that can only tick
    one box would push operators to register one source per dataset and then
    wonder why each needs a separate Dify entry.
    """

    model_config = ConfigDict(extra="forbid")

    knowledge_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    tenant_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")
    knowledge_base_ids: list[str] = Field(min_length=1, max_length=20)
    display_name: str = Field(default="", max_length=200)

    @field_validator("knowledge_base_ids")
    @classmethod
    def _valid_ids(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            candidate = item.strip()
            if not candidate or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", candidate):
                raise ValueError("knowledge_base_ids contains an invalid id")
            if candidate not in cleaned:
                cleaned.append(candidate)
        return cleaned


class ConversationCreateRequest(BaseModel):
    """Create a conversation bound to caller, entry, and agent version (T4/#172).

    Defined at module level (not inside create_gateway_app) so FastAPI sees a
    real BaseModel annotation under `from __future__ import annotations`.
    """

    model_config = ConfigDict(extra="forbid")

    agent_version_key: str = Field(
        min_length=6,
        max_length=80,
        pattern=r"^agt_[A-Za-z0-9]{8,64}#v\d{1,6}$",
    )


class ShortcutFieldsRequest(BaseModel):
    """Shortcut draft fields shared by create and update (#230)."""

    model_config = ConfigDict(extra="forbid")

    intent: str = Field(min_length=1, max_length=32)
    requires_order: bool = False
    sort_order: int = Field(default=100, ge=0, le=9999)
    labels: dict[str, str] = Field(min_length=1, max_length=6)
    descriptions: dict[str, str] = Field(default_factory=dict, max_length=6)
    question_templates: dict[str, str] = Field(default_factory=dict, max_length=6)
    target_agent_version: str | None = Field(
        default=None,
        min_length=6,
        max_length=80,
        pattern=r"^agt_[A-Za-z0-9]{8,64}#v\d{1,6}$",
    )
    jump_path: str | None = Field(
        default=None,
        max_length=512,
        # Must be a path-absolute route, not a network-path reference. Per RFC
        # 3986 §4.2 a leading "//" starts an authority, so "//evil.com" is a
        # cross-host reference — a classic open-redirect vector once a client
        # hands it to its navigator. This form is used instead of a "(?!/)"
        # lookahead because Pydantic v2 compiles patterns with the Rust regex
        # crate, which does not support lookaround at all (it fails at import).
        # It accepts exactly "/" plus a non-"/" start plus a non-space tail.
        #
        # Raw whitespace is also rejected: it cannot appear in a URL path (it
        # is "%20" once encoded), and without \S a trailing space or newline
        # would pass here and then be silently normalized away by the
        # lifecycle validator, leaving the two layers disagreeing.
        pattern=r"^/(?:[^/]\S*)?$",
    )
    image_url: str | None = Field(
        default=None,
        max_length=512,
        # http(s) only. A `data:` payload is an unbounded blob through config,
        # and `javascript:`/`file:` become execution or local-read primitives
        # once a client hands the value to an image loader. The lifecycle
        # validator enforces the same rule; this layer rejects it earlier with
        # a 422 rather than a 4xx from deeper in.
        pattern=r"^https?://\S+$",
    )


class ShortcutCreateRequest(ShortcutFieldsRequest):
    business_entry: str = Field(min_length=1, max_length=32, pattern=r"^(consumer|operator)$")
    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


class ShortcutUpdateRequest(ShortcutFieldsRequest):
    expected_revision: int = Field(ge=1)


class ShortcutActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class ShortcutRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    version_no: int = Field(ge=1)


class GatewayAPI:
    def __init__(
        self,
        settings: GatewayServerSettings,
        store: GatewayStore,
        runtime: GatewayRuntime,
        caller_resolver: CallerContextResolver,
        order_authorizer: OrderAuthorizer,
        platform_resolver: PlatformIdentityResolver,
        faq_catalog: FAQCatalog,
        agent_manager: AgentManager | None = None,
        shortcut_manager: ShortcutManager | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.runtime = runtime
        self.caller_resolver = caller_resolver
        self.order_authorizer = order_authorizer
        self.platform_resolver = platform_resolver
        self.faq_catalog = faq_catalog
        self.agent_manager = agent_manager
        # Shortcut store shares the gateway database file (its own tables);
        # lazy so existing embedders (tests) need no new argument (#230).
        self.shortcut_manager = shortcut_manager or ShortcutManager(ShortcutStore(store.path))
        # Conversation store shares the gateway database file (its own
        # tables); lazy so existing embedders (tests) need no new argument.
        self.conversation_store = ConversationStore(store.path)
        # M2-CLOSE-01：节点委派的请求级授权记录。同样是共用数据库文件的独立表，
        # 惰性构造，既有嵌入方不需要新增参数。
        self.agent_run_store = AgentRunStore(store.path)
        # `dify_workspaces`（#879 / ADR-0059）：一个租户在一个环境里的 workspace。同一个
        # 库文件、自己的表，惰性构造 —— 既有嵌入方不需要新增参数。放在这里而不是
        # ``GatewayRuntime``，因为写它的（开户，下一张票）与读它的（发布桥、试运行、
        # 运维 CLI）都以网关的库为唯一真值，而 ``GatewayRuntime`` 在测试里常是替身。
        self.dify_workspaces = DifyWorkspaces(store.path)
        # APP-RUNTIME-02：Dify 路线的执行记录。同一个库文件、自己的表；
        # **不**复用 `agent_runs` 的节点列（那会把"这次没有节点"写成"节点核对过了"）。
        self.dify_run_store = DifyRunStore(store.path)


class StandardAPIError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable


def request_language(
    accept_language: Annotated[str | None, Header(alias="Accept-Language")] = None,
) -> str:
    """Resolve the presentation language for one request (L1/#201).

    A separate dependency instead of widening the identity tuple: language is
    orthogonal to authentication and platform resolution, and conversation
    endpoints share ``assistant_identity`` without needing it. Presentation
    only — never part of auth, routing, or the incident manifest.
    """
    return resolve_language(accept_language)


def _publish_initiator(
    company_user_id: str | None,
    company_username: str | None,
    dify_account_id: str | None,
) -> tuple[str, str, str]:
    """Normalise the initiator headers (#780) — trimmed，never an authorization input.

    Empty means "unknown" and is recorded as such. The values are redacted to the
    store's own scope charset by the caller's ``_scope`` guard, so a malformed
    header cannot smuggle anything into the ledger.
    """
    return (
        (company_user_id or "").strip()[:128],
        (company_username or "").strip()[:128],
        (dify_account_id or "").strip()[:128],
    )


def _trial_run_context(context: Any, initiator: TrialInitiator | None) -> Any:
    """试运行的范围：**提交与读取都走这里**，因为两者必须算出同一个指纹。

    作业行按 ``scope_fingerprint`` 隔离（`gateway_store.get_standard_diagnosis`），
    而那个哈希含数据范围与主体类型 —— 提交用 A 范围、读取用 B 范围时，返回的
    ``trial_id`` 会**永远读不回来**，且表现与"这个 id 不存在"完全一样。所以
    "读结果"这条路由一开始就漏传发起人头是个真缺陷（#782 留下的 v2 地雷），
    只有把选择收成一处才不会重演。

    ``initiator is None``（桥没给全身份，或本来就没走公司身份）⇒ 固定调试租户，
    即 #781 的第一版行为，逐字不变。
    """
    if initiator is None:
        return context.runtime.trial_run_context()
    return context.runtime.trial_context_for(initiator)


def _record_publish_audit(
    context: Any,
    *,
    app_id: str,
    action: str,
    reason_code: str,
    initiator: tuple[str, str, str],
    binding: Any = None,
    version_no: int | None = None,
) -> None:
    """Write one ledger row, best-effort. **Never raises into the publish path.**

    The ledger is observability: a disk problem must not turn a legitimate
    publish into a failure, and a missing initiator must not either (it is
    recorded as empty, not refused). Values are trimmed and length-capped so a
    caller-supplied header cannot bloat a row.
    """
    company_user_id, company_username, dify_account_id = initiator
    store = getattr(context, "store", None)
    if store is None or not hasattr(store, "record_publish_audit"):  # pragma: no cover
        return
    with contextlib.suppress(Exception):
        store.record_publish_audit(
            app_id=app_id[:128],
            action=action,
            reason_code=reason_code[:64],
            tenant_id=str(getattr(binding, "tenant_id", "") or "")[:128],
            business_entry=str(getattr(binding, "business_entry", "") or "")[:64],
            agent_name=str(getattr(binding, "agent_name", "") or "")[:128],
            company_user_id=company_user_id,
            company_username=company_username,
            dify_account_id=dify_account_id,
            version_no=version_no,
        )


def _declared_in_file(settings: Any, environment: str, tenant_id: str, entry: str, scenario: str) -> bool:
    """这条 key 是不是仍由**文件登记**声明。

    启动期查过一次冲突，但那之后文件可能被重新配上；而发布是唯一会把冲突**写进去**
    的动作，所以它也要查一次 —— 写进去之后再发现，故障是整表加载失败，波及所有租户。
    """
    from aiops_diagnostics import chat_execution

    try:
        bindings = chat_execution.load_bindings(
            str(getattr(settings, "chat_execution_bindings_file", "") or "")
        )
    except chat_execution.ChatBindingError as exc:
        raise StandardAPIError(503, "EXEC_CONFIG_UNAVAILABLE", "bindings unavailable") from exc
    key = f"{scenario}:{environment}:{tenant_id}"
    return any(item.key == key for item in bindings)


def _key_ref_for(key_dir: str, app_id: str) -> str:
    """这个 app 的 Service API 凭据路径，且**必须可读**。

    发布只推路径、不读密钥；但一条指向不存在文件的路径等于"发布了也跑不起来"。
    """
    from aiops_diagnostics import chat_execution
    from aiops_diagnostics.dify_chat_workflow import load_workflow_key

    if not key_dir:
        return ""
    path = chat_execution.key_file_for_app(key_dir, app_id)
    try:
        load_workflow_key(path)
    except ValueError as exc:
        raise StandardAPIError(
            status.HTTP_409_CONFLICT,
            "DIFY_KEY_MISSING",
            "the app service key is not provisioned on this server",
        ) from exc
    return path


def _sync_bound_app(context: Any, binding: Any, source: Any, admin: Any) -> Any:
    """按 **DSL 自己声明的应用类型** 选映射。

    workflow 应用没有 `model_config`，把它送进 chat 那条映射会得到
    `DSL has no model_config object` —— 那看起来像"DSL 坏了"，其实是"这是另一种应用"。

    分流放在这里、而不是入口多调一次导出：`sync_binding_from_dify` 自己本来就要导，
    在入口再加一次导出会多一跳外部调用（测试的假上游也会因此被打中）。
    所以这里先按 chat 走；只有它明确报"没有 model_config"时才改走 workflow ——
    **用结果判种类，不用猜测**。
    """
    from aiops_diagnostics import consumer_publication
    from aiops_diagnostics import dify_dsl_pull as pull

    target = consumer_publication.target_for(context.settings, binding)
    if not target and consumer_publication.route_scenario(context.settings, binding) == "consumer_service":
        raise ValueError("consumer publication target removed")
    if target:
        if pull._dsl_kind(pull.DifyDslClient(source).export_dsl()) != "advanced-chat":
            raise ValueError("consumer publication requires Chatflow")
        return pull.sync_workflow_from_dify(
            context.agent_manager,
            admin,
            binding,
            source,
            consumer_settings=consumer_publication.frozen_settings(target),
        )
    try:
        return pull.sync_binding_from_dify(
            context.agent_manager,
            admin,
            binding,
            source,
            knowledge_registry=parse_dify_knowledge_bindings(context.settings.dify_knowledge_bindings),
            tool_registry=parse_dify_tool_bindings(context.settings.dify_tool_bindings),
        )
    except pull.DifyDslMalformed as exc:
        if "model_config" not in str(exc):
            raise
        return pull.sync_workflow_from_dify(context.agent_manager, admin, binding, source)


def _workspace_for_tenant(
    context: Any, tenant_id: str, *, initiator: tuple[str, str, str] = ("", "", "")
) -> str:
    """拿到**这个租户**的 workspace，或拒绝（#884 发布 / #885 试运行）。

    失败时把 ``resolve`` 的四个码原样转成 HTTP：``UNRESOLVED``(404) / ``CREATING``(409,
    可重试) / ``FAILED``(409) / ``UNKNOWN``(409)。合成一句话就把"去开户 / 稍后重试 /
    先对账"这三种下一步抹平了 —— 而发布这条路**必须**让人知道该做哪一件。

    审计账本也记一笔：被拒的发布尝试要能回答"谁试图把哪一版发进哪个租户"。
    """
    from aiops_diagnostics.dify_workspaces import DifyWorkspaceError

    try:
        return context.dify_workspaces.resolve(tenant_id, context.settings.environment)
    except DifyWorkspaceError as exc:
        _record_publish_audit(
            context,
            app_id="",
            action="refused",
            reason_code=exc.code,
            initiator=initiator,
            binding=_TenantOnly(tenant_id),
        )
        raise StandardAPIError(exc.http_status, exc.code, exc.message, retryable=exc.retryable) from exc


class _TenantOnly:
    """只带租户的审计条目：解析失败时绑定还没查到，但"哪个租户"已经知道。"""

    tenant_id: str
    business_entry = ""
    agent_name = ""

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id


def _publish_bound_app(
    context: Any,
    app_id: str,
    *,
    initiator: tuple[str, str, str] = ("", "", ""),
) -> dict[str, Any]:
    """把一个**已绑定**的 Dify app 的当前内容冻结成新的已发布版本。

    **两条路共用的唯一实现**（共享密钥路由与 operator 会话身份路由）：一处实现，
    两条路就不可能漂。谁能调到这一层由各自的**门**决定 —— 这里只做「点名一个
    已声明的绑定，把它现在的内容过一遍发布门」。

    爆炸半径是设计出来的：请求只带 ``app_id``，租户/agent 名/类型/契约/知识库
    全部来自 ``dify_app_registry``。最坏情况因此是**重新冻结一个已声明绑定的当前
    内容**；重复发布未变内容按构造是 no-op（先与已发布快照逐字比较）。

    ``initiator``（#780）是**审计元数据**：``(company_user_id, company_username,
    dify_account_id)``。它**不是**授权依据 —— 授权在桥那一侧（公司发布权限码）
    已经完成；这里把它记进审计账本，好回答"谁在何时把哪一版发上了生产"。三个字段
    都可能是空串（调用方没带），那记成未知，发布照常。
    """
    company_user_id, company_username, dify_account_id = initiator
    binding = next(
        (item for item in context.runtime.dify_app_registry.all() if item.app_id == app_id),
        None,
    )
    if binding is None:
        # 与未登记的知识句柄同形：404 说"不在这里"，不透露有哪些绑定。
        # **被拒的尝试也入账**（#780）：未绑定的 app_id 是"有人试图发布一个没登记的东西"，
        # 正是审计要看见的；它没有任何租户可归，所以租户留空。
        _record_publish_audit(
            context,
            app_id=app_id,
            action="refused",
            reason_code="DIFY_APP_NOT_BOUND",
            initiator=initiator,
        )
        raise StandardAPIError(
            status.HTTP_404_NOT_FOUND, "DIFY_APP_NOT_BOUND", "app is not a bound agent source"
        )
    from aiops_diagnostics.agent_manifest import admin_context
    from aiops_diagnostics.dify_dsl_pull import DifyDslError, DifyDslSource

    console = context.settings
    if not console.dify_console_base_url or not console.dify_console_api_key:
        raise StandardAPIError(
            status.HTTP_502_BAD_GATEWAY,
            "DIFY_CONSOLE_UNAVAILABLE",
            "the Dify console credential is not configured on this server",
        )
    # **按绑定的租户解析 workspace**（#884 / ADR-0059 §5.2）。`_publish_bound_app` 是两条
    # 路共用的唯一实现，所以这一处一改，共享密钥路由与 operator 会话路由同时生效。
    #
    # 为什么不能再用 `dify_console_workspace_id` 那个常量：多 workspace 之后它是"某一个人
    # 的工作区"，而这里要的是**这个 app 所属租户**的工作区。用常量意味着 A 租户的发布
    # 会跑在 B 的工作区上 —— 设计里点名这是最容易漏掉的越权口。
    #
    # 解析失败 ⇒ **拒绝，不回落任何默认 workspace**：回落等于把"这个租户还没开户"悄悄
    # 变成"发布进别人的工作区"，而后者看起来是成功的。
    workspace_id = _workspace_for_tenant(context, binding.tenant_id, initiator=initiator)
    source = DifyDslSource(
        base_url=console.dify_console_base_url,
        app_id=binding.app_id,
        api_key=console.dify_console_api_key,
        workspace_id=workspace_id,
        ops_tools_enabled=ops_app_registered(
            console.ops_bindings_file, console.environment, binding.tenant_id, app_id
        ),
    )
    # **revision 必须在外部同步之前快照。** 同步期间可能有人撤回或另发一版；
    # 等同步回来再读，读到的是**对方之后的** revision，于是这次发布仍会 CAS 通过，
    # 把一次已经成功的撤回重新置为 active —— 实测复现过（V1 → 撤回 → 延迟发布
    # 推进，最终 active / revision 3 / V2）。用快照值推进，期间有变动就冲突。
    from aiops_diagnostics import consumer_publication

    consumer_target = consumer_publication.target_for(context.settings, binding)
    if consumer_target and console.chat_route_bindings_file:
        from aiops_diagnostics.chat_route_bindings import ChatRouteBindingStore

        other = ChatRouteBindingStore(console.chat_route_bindings_file).get(
            console.execution_environment,
            binding.tenant_id,
            binding.business_entry,
            chat_execution.SCENARIO_OWN_ORDER_STATUS,
        )
        if other is not None and other.app_id == binding.app_id:
            raise StandardAPIError(409, "CONSUMER_ROUTE_MIGRATION_REQUIRED", "app already owns another route")
    expected_revision = _current_route_revision(
        context,
        binding,
        str(getattr(context.settings, "execution_environment", "") or ""),
    )
    from aiops_diagnostics import node_publication

    node_target = node_publication.target_for(context.settings, binding)
    if node_target and consumer_target:
        raise StandardAPIError(409, "CONSUMER_TARGET_CONFLICT", "app has two execution targets")
    if console.chat_route_bindings_file and not node_target and not consumer_target:
        from aiops_diagnostics.chat_route_bindings import ChatRouteBindingStore
        from aiops_diagnostics.consumer_publication import route_scenario

        managed = ChatRouteBindingStore(Path(console.chat_route_bindings_file)).get(
            console.execution_environment,
            binding.tenant_id,
            binding.business_entry,
            route_scenario(context.settings, binding),
        )
        if managed and managed.engine == "node":
            raise StandardAPIError(409, "NODE_TARGET_MISSING", "node target unavailable")
    if node_target and (not console.chat_route_bindings_file or binding.business_entry != node_target.entry):
        raise StandardAPIError(409, "NODE_ROUTE_UNAVAILABLE", "node route is not configured")
    if node_target and _declared_in_file(
        console,
        node_target.environment,
        binding.tenant_id,
        node_target.entry,
        node_target.scenario,
    ):
        raise StandardAPIError(409, "NODE_ROUTE_COLLISION", "node route is declared in both places")
    try:
        outcome = (
            node_publication.publish(
                context.agent_manager, admin_context(binding.tenant_id), binding, source, node_target
            )
            if node_target
            else _sync_bound_app(context, binding, source, admin_context(binding.tenant_id))
        )
    except DifyDslError as exc:
        # Dify 自己拒绝或不可达：带它自己的码转达，调用方要能区分「凭据被拒」与「服务挂了」。
        # 被拒的**尝试**一样入账；租户与 agent 名此刻是知道的（绑定查到了）。
        _record_publish_audit(
            context,
            app_id=app_id,
            action="refused",
            reason_code=exc.code,
            binding=binding,
            initiator=initiator,
        )
        # 响应只说稳定码（上游正文可能带内部地址）；**日志**要留原因，
        # 否则"拉取失败"在运维眼里是一句没有下一步的话。
        _LOGGER.warning("dify pull failed: %s: %s", exc.code, exc)
        raise StandardAPIError(status.HTTP_502_BAD_GATEWAY, exc.code, "dify pull failed") from exc
    except Exception as exc:  # noqa: BLE001 - 发布门自己的规则，原样转达
        _record_publish_audit(
            context,
            app_id=app_id,
            action="refused",
            reason_code=f"PUBLISH_GATE_{type(exc).__name__}",
            binding=binding,
            initiator=initiator,
        )
        raise StandardAPIError(
            status.HTTP_409_CONFLICT, "DIFY_PUBLISH_REJECTED", f"publish rejected: {type(exc).__name__}"
        ) from exc
    # **推进正式指针**：这一步才是"运营点了发布，App 真的切了版"。它必须在
    # `_record_publish_audit` 之前 —— 审计要记的是已经成立的事实，不是待办。
    #
    # 跨系统没有事务：Dify 那边已经发布了，本地这一步仍可能失败。失败时**保留旧正式版**
    # 并如实抛错（不伪称成功），任务书对这条的要求是"报未完成"，不是"补一个异步重试"。
    if outcome.action not in {"synced", "unchanged"}:
        raise StandardAPIError(409, "DIFY_PUBLISH_REJECTED", "publish was not completed")
    if node_target:
        try:
            node_publication.advance(context, outcome, binding, node_target, expected_revision)
        except node_publication.NodeRouteConflict as exc:
            raise StandardAPIError(409, "NODE_ROUTE_NOT_ADVANCED", "node route was not advanced") from exc
    else:
        _advance_route_binding(
            context, outcome, binding, initiator=initiator, expected_revision=expected_revision
        )

    # 成功：记版本号与动作（``synced`` / ``unchanged``）。``unchanged`` 也是"成功的一次
    # 发布尝试"，只是内容没变 —— 审计要能分辨它，否则"某人点了一百次但什么都没发"与
    # "发了 0 次"看起来一样。
    _record_publish_audit(
        context,
        app_id=outcome.app_id,
        action="published" if outcome.action == "synced" else "unchanged",
        reason_code="",
        binding=binding,
        initiator=initiator,
        version_no=outcome.version_no if isinstance(outcome.version_no, int) else None,
    )
    return {
        "app_id": outcome.app_id,
        "tenant_id": outcome.tenant_id,
        "business_entry": outcome.business_entry,
        "agent_name": outcome.agent_name,
        "action": outcome.action,
        "version_no": outcome.version_no,
        "note": outcome.note,
    }


def _current_route_revision(context: Any, binding: Any, environment: str) -> int | None:
    """外部同步**之前**这条路由的 revision；没有行时 0。

    同步可能耗时几十秒，期间别人可以撤回或另发一版。推进指针要用这个快照值做 CAS，
    而不是同步回来之后才读 —— 否则这次发布会把别人的撤回覆盖掉。
    """
    database = str(getattr(context.settings, "chat_route_bindings_file", "") or "")
    if not database:
        return None
    from pathlib import Path

    from aiops_diagnostics.chat_route_bindings import ChatRouteBindingStore

    try:
        store = ChatRouteBindingStore(Path(database).expanduser().absolute())
        entry = str(getattr(binding, "business_entry", "") or "consumer")
        from aiops_diagnostics.consumer_publication import route_scenario

        row = store.get(environment, binding.tenant_id, entry, route_scenario(context.settings, binding))
    except Exception as exc:  # noqa: BLE001 - 没有初始 CAS 证据，发布必须拒绝。
        raise StandardAPIError(503, "ROUTE_BINDING_UNAVAILABLE", "route state unavailable") from exc
    return row.revision if row is not None else 0


def _advance_route_binding(
    context: Any,
    outcome: Any,
    binding: Any,
    *,
    initiator: tuple[str, str, str],
    expected_revision: int | None = None,
) -> None:
    """发布成功之后，把（环境，租户，入口，场景）的正式指针推到这一版。

    **拒绝的三种情况**，都不写指针、并如实报未完成：

    * 没有配 `AIOPS_GATEWAY_CHAT_ROUTE_BINDINGS_FILE`：这个环境还没接管管理指针，
      发布只冻结版本（与今天逐字一致）；
    * `version_no` 缺失或 `workflow_id` 取不到：拿不到"哪一版、哪张图"就没有可切的东西；
    * 已有发布者在本轮期间改过指针（`revision` 变化）：CAS 失败，不覆盖别人的发布。

    成功时写入的身份是**三列分开**的：`app_id` 是应用、`workflow_id` 是图、
    版本是本地那一条快照。把同一个值填进多列，等于三列都在撒谎。
    """
    database = str(getattr(context.settings, "chat_route_bindings_file", "") or "")
    environment = str(getattr(context.settings, "execution_environment", "") or "")
    if not database or not environment:
        return
    # **只有 workflow 应用才走"推进路由指针"这一步。** 同一个网关还可以挂着
    # chat/completion 应用（`dify_workspace_template.dsl.yml` 里就有 `mode: chat`），
    # 它们没有已发布 workflow，`sync_binding_from_dify` 本来就能正常冻结 Agent 版本。
    # 无条件要求 workflow 身份会让那些应用**发布失败**，而它们的版本其实已经变了 ——
    # 调用方看到的是失败，库里却已经改过，那比不发布更糟。
    #
    # 判据落在**这次同步实际冻结了什么**上：workflow 应用才带 `workflow_id`。
    if not getattr(outcome, "workflow_id", ""):
        return
    version_no = outcome.version_no
    if not isinstance(version_no, int):
        raise StandardAPIError(
            status.HTTP_409_CONFLICT,
            "DIFY_ROUTE_NOT_ADVANCED",
            "the published version has no workflow identity to bind",
        )
    from pathlib import Path

    from aiops_diagnostics.chat_route_bindings import (
        ChatRouteBindingStore,
        RouteBindingError,
    )

    key_dir = str(getattr(context.settings, "chat_dify_key_dir", "") or "")
    entry = str(getattr(binding, "business_entry", "") or "consumer")
    from aiops_diagnostics.consumer_publication import route_scenario

    scenario = getattr(outcome, "route_scenario", "") or route_scenario(context.settings, binding)
    # **两个前置必须在写指针之前拒绝**，理由相同：写进去之后才发现，故障会扩散到
    # 别人身上 —— 这两条都会让整张管理表加载失败，而 `_bindings` 在读租户之前
    # 就检查整表，所以一个租户的坏行会让**所有**租户的聊天路线一起不可用。
    #
    #   1. 没配凭据目录 ⇒ 写进去一条空引用，下次加载 `load_workflow_key("")` 抛错；
    #   2. 文件登记里仍有同一个 key ⇒ 判重复声明，同样整表不可用。
    if not key_dir:
        raise StandardAPIError(
            status.HTTP_409_CONFLICT,
            "DIFY_KEY_DIR_UNSET",
            "the app service key directory is not configured on this server",
        )
    # 执行地址同理：控制台拉取用的是 `dify_console_base_url`，所以**发布能成功**，
    # 而之后每个聊天任务都会在空地址上失败；重启还会被"已有活动 Dify 行"的启动校验
    # 拒掉 —— 等于把自己锁在门外。写第一条活动指针之前就要拒绝。
    if not str(getattr(context.settings, "dify_chat_base_url", "") or ""):
        raise StandardAPIError(
            status.HTTP_409_CONFLICT,
            "DIFY_CHAT_BASE_URL_UNSET",
            "the dify chat base url is not configured on this server",
        )
    if _declared_in_file(context.settings, environment, binding.tenant_id, entry, scenario):
        raise StandardAPIError(
            status.HTTP_409_CONFLICT,
            "CHAT_ROUTE_BINDING_COLLIDES_WITH_FILE",
            "this route is still declared in the bindings file",
        )

    store = ChatRouteBindingStore(Path(database).expanduser().absolute())
    existing = store.get(environment, binding.tenant_id, entry, scenario)
    if (
        outcome.action == "unchanged"
        and existing is not None
        and existing.active
        and existing.agent_id == outcome.agent_id
        and existing.agent_version_no == version_no
        and existing.workflow_id == outcome.workflow_id
    ):
        if expected_revision is not None and existing.revision != expected_revision:
            raise StandardAPIError(409, "ROUTE_BINDING_CONFLICT", "route binding moved")
        return
    if expected_revision is None:
        raise StandardAPIError(503, "ROUTE_BINDING_UNAVAILABLE", "initial route revision unavailable")
    try:
        store.publish(
            environment=environment,
            tenant_id=binding.tenant_id,
            entry=entry,
            scenario=scenario,
            engine="dify",
            agent_id=outcome.agent_id,
            agent_version_no=version_no,
            app_id=binding.app_id,
            workflow_id=outcome.workflow_id,
            # 凭据只存**引用**：`<key_dir>/<app_id>.key`，由运维预置。发布只推路径，
            # 全程不读密钥 —— 于是它也没有机会出现在表里。
            #
            # **但路径必须已存在**：发布成功却指向一个不存在的凭据文件，下一次请求
            # 必然 `DIFY_UNAVAILABLE` —— 而那个症状离发布这一步很远，没人会想到是它。
            # 宁可让发布失败并说清楚，也不要留下一条"看起来发布了"的绑定。
            workflow_key_ref=_key_ref_for(key_dir, binding.app_id),
            expected_revision=expected_revision,
            updated_by=initiator[0] or initiator[1] or "",
        )
    except RouteBindingError as exc:
        raise StandardAPIError(status.HTTP_409_CONFLICT, exc.code, "route binding was not advanced") from exc


def _managed_app_route(context: Any, app_id: str):
    from pathlib import Path

    from aiops_diagnostics.chat_route_bindings import ChatRouteBindingStore
    from aiops_diagnostics.consumer_publication import route_scenario

    binding = next((item for item in context.runtime.dify_app_registry.all() if item.app_id == app_id), None)
    if binding is None:
        raise StandardAPIError(404, "DIFY_APP_NOT_BOUND", "app not bound")
    if not context.settings.chat_route_bindings_file or not context.settings.execution_environment:
        raise StandardAPIError(404, "CHAT_ROUTE_BINDINGS_DISABLED", "managed routes disabled")
    store = ChatRouteBindingStore(Path(context.settings.chat_route_bindings_file))
    row = store.get(
        context.settings.execution_environment,
        binding.tenant_id,
        binding.business_entry,
        route_scenario(context.settings, binding),
    )
    if row is None or row.app_id != app_id:
        raise StandardAPIError(409, "CHAT_ROUTE_BINDING_MISMATCH", "app route not found")
    workspace = _workspace_for_tenant(context, binding.tenant_id)
    return binding, row, workspace


def _tenant_in_scope(record_tenant: object, allowed_tenant: object) -> bool:
    """这一行的租户是否落在给定范围内 —— 用共享规则判，不就地写比较。"""
    from aiops_diagnostics.order_visibility import (
        TenantVisibility,
        VisibilityProfile,
        normalize_tenant,
        visible_orders,
    )

    allowed = normalize_tenant(allowed_tenant)
    if allowed is None:
        return False
    visibility = TenantVisibility(profile=VisibilityProfile.CALLER, allowed=frozenset({allowed}))
    return bool(visible_orders([{"tenant_id": record_tenant}], visibility).rows)


def _is_managed_workflow_app(context: Any, app_id: str) -> bool:
    """这个 app 是不是**已接管管理指针的 workflow 应用**。

    判据落在应用自己身上（注册表 + 管理行），不是"这个网关有没有配管理表"：
    配了管理表不等于每个 app 都被接管了。
    """
    from pathlib import Path

    from aiops_diagnostics.chat_route_bindings import ChatRouteBindingStore
    from aiops_diagnostics.consumer_publication import route_scenario

    database = str(getattr(context.settings, "chat_route_bindings_file", "") or "")
    if not database:
        return False
    binding = next((item for item in context.runtime.dify_app_registry.all() if item.app_id == app_id), None)
    if binding is None:
        return False
    try:
        row = ChatRouteBindingStore(Path(database).expanduser().absolute()).get(
            str(getattr(context.settings, "execution_environment", "") or ""),
            binding.tenant_id,
            binding.business_entry,
            route_scenario(context.settings, binding),
        )
    except Exception as exc:  # noqa: BLE001 - 不能将管理读取故障转成旧执行路线。
        raise StandardAPIError(503, "ROUTE_BINDING_UNAVAILABLE", "route state unavailable") from exc
    return row is not None and row.app_id == app_id


def _workflow_trial_draft(
    context: Any, app_id: str, run_context: ScopeContext, question="check order status"
):
    from aiops_diagnostics.dify_dsl_pull import DifyDslClient, DifyDslSource
    from aiops_diagnostics.dify_workflow_trial import WorkflowTrial

    binding, row, workspace = _managed_app_route(context, app_id)
    # 试用的范围必须落在**绑定所属租户**上。租户那一维走共享规则
    # （`order_visibility.visible_orders`）："这一行属于哪个租户、在不在范围内"
    # 在这个仓只有一处定义，不在这里手写比较。
    if not row.active or not _tenant_in_scope(binding.tenant_id, run_context.effective_tenant_id):
        raise StandardAPIError(403, "TRIAL_SCOPE_MISMATCH", "trial scope does not match the bound app")
    source = DifyDslSource(
        context.settings.dify_console_base_url, app_id, context.settings.dify_console_api_key, workspace
    )
    if row.engine == "node":
        from aiops_diagnostics.node_publication import NodeTrial, candidate, target_for

        target = target_for(context.settings, binding)
        if target is None:
            raise StandardAPIError(409, "NODE_TARGET_MISSING", "node target unavailable")
        if target.entry == "operator":
            raise StandardAPIError(
                409, "OPERATOR_TRIAL_IDENTITY_REQUIRED", "operator trial requires company authorization"
            )
        return NodeTrial(candidate(source, target), row)
    dsl = DifyDslClient(source).export_dsl()
    if row.scenario == chat_execution.SCENARIO_CONSUMER_SERVICE:
        from aiops_diagnostics.consumer_publication import frozen_settings, target_for
        from aiops_diagnostics.consumer_trial import ConsumerTrial

        target = target_for(context.settings, binding)
        if target is None:
            raise StandardAPIError(409, "CONSUMER_TARGET_MISSING", "consumer trial target missing")
        return ConsumerTrial(dsl, source, row, frozen_settings(target), question)
    return WorkflowTrial(dsl, source, row)


def _restore_bound_app(context: Any, app_id: str, version_no: int, initiator: tuple[str, str, str]):
    from aiops_diagnostics.dify_dsl_pull import DifyDslClient, DifyDslSource, SyncOutcome, workflow_content

    binding, row, workspace = _managed_app_route(context, app_id)
    version = context.agent_manager.store.version(row.agent_id, binding.tenant_id, version_no)
    snapshot = version.snapshot
    if row.engine == "node":
        from aiops_diagnostics import node_publication

        target = node_publication.target_for(context.settings, binding)
        if (
            target is None
            or snapshot.get("kind") != "node-sop"
            or snapshot.get("app_id") != app_id
            or snapshot.get("workspace_id") != workspace
        ):
            raise StandardAPIError(409, "NODE_VERSION_MISMATCH", "node version unavailable")
        execution_manifest.from_text(snapshot["node_manifest"])
        outcome = SyncOutcome(
            binding.tenant_id,
            binding.business_entry,
            app_id,
            binding.agent_name,
            "synced",
            version_no,
            agent_id=row.agent_id,
        )
        node_publication.advance(context, outcome, binding, target, row.revision)
        _record_publish_audit(
            context,
            app_id=app_id,
            action="restored",
            reason_code="",
            binding=binding,
            initiator=initiator,
            version_no=version_no,
        )
        return {"app_id": app_id, "action": "synced", "version_no": version_no}
    workflow_id = snapshot.get("workflow_id")
    if (
        snapshot.get("kind") != "dify-workflow"
        or snapshot.get("app_id") != app_id
        or snapshot.get("workspace_id") != workspace
        or not workflow_id
    ):
        raise StandardAPIError(409, "DIFY_VERSION_MISMATCH", "version is not a verified app workflow")
    source = DifyDslSource(
        context.settings.dify_console_base_url, app_id, context.settings.dify_console_api_key, workspace
    )
    if workflow_content(DifyDslClient(source).export_dsl(workflow_id=workflow_id)) != snapshot.get(
        "workflow"
    ):
        raise StandardAPIError(409, "DIFY_VERSION_MISMATCH", "published workflow differs from its snapshot")
    outcome = SyncOutcome(
        binding.tenant_id,
        binding.business_entry,
        app_id,
        binding.agent_name,
        "synced",
        version_no,
        workflow_id=workflow_id,
        agent_id=row.agent_id,
        route_scenario=row.scenario,
    )
    _advance_route_binding(context, outcome, binding, initiator=initiator, expected_revision=row.revision)
    _record_publish_audit(
        context,
        app_id=app_id,
        action="restored",
        reason_code="",
        binding=binding,
        initiator=initiator,
        version_no=version_no,
    )
    return {"app_id": app_id, "action": "synced", "version_no": version_no}


def _trial_draft_from_console(context: Any, app_id: str) -> tuple[str, tuple[str, ...]]:
    """读出这个 app **最近一次保存**的配置，作为试运行的草稿（#775 部署层）。

    返回 ``(prompt, allowed_tools)``。

    **为什么读出来的是"最近一次保存"而不是"编辑框里现在的内容"**：Dify 1.17.1 的 chat
    应用没有服务端草稿层 —— ``app_model_config_id`` 是单指针，"发布更新"与"保存"打的是
    同一个 ``POST /console/api/apps/<id>/model-config``（#773 实测）。编辑页里尚未保存的
    内容只在浏览器内存里，**任何 API 都读不到**。所以这是能力的边界，不是选择；界面上
    必须写明"以最近一次保存的配置为准"。

    **为什么在网关读而不在那个薄桥里读**：导出的 DSL 里工具条目**只有 provider 的 UUID，
    没有提供方名字**，所以读草稿必须当场从控制台解析 ``aiops`` 提供方的 UUID 再做映射。
    那套映射（``map_dsl_to_config`` + ``partition_aiops_tools``）在网关这里**已经有了**，
    而且正是发布路径用的同一份；让桥或浏览器再实现一遍，就是
    ``resolve_pulled_config`` 的 docstring 记下的那种漂移（"干跑报空、真拉却保留了绑定"）。

    拉不到一律**显式失败**，不退回空草稿：空草稿会跑出一次"没有方法论的诊断"，而运营会
    以为那就是他草稿的效果。
    """
    from aiops_diagnostics.dify_dsl_pull import (
        DifyDslError,
        DifyDslSource,
        resolve_pulled_config,
    )

    binding = next(
        (item for item in context.runtime.dify_app_registry.all() if item.app_id == app_id),
        None,
    )
    if binding is None:
        # 与发布路径同形：未登记就是"不在这里"，不透露有哪些绑定。
        raise StandardAPIError(
            status.HTTP_404_NOT_FOUND, "DIFY_APP_NOT_BOUND", "app is not a bound agent source"
        )
    console = context.settings
    if not console.dify_console_base_url or not console.dify_console_api_key:
        raise StandardAPIError(
            status.HTTP_502_BAD_GATEWAY,
            "DIFY_CONSOLE_UNAVAILABLE",
            "the Dify console credential is not configured on this server",
        )
    from aiops_diagnostics.agent_manifest import admin_context
    from aiops_diagnostics.dify_dsl_pull import _find_by_name

    # **按绑定的租户解析草稿来源**（#885 / ADR-0059 §5.3）。与发布同一类越权口，但判据
    # 相反：那边是"写进哪个 workspace"，这里是"从哪个 workspace 读草稿"。
    #
    # 它放在**查 agent 之前**：workspace 是**绑定**的属性，不是 agent 的属性。放后面的话，
    # 一个没开户的租户会先在"这个 agent 没登记"上被拒，而真正的原因是他的租户还没有工作区。
    #
    # 租户没有 workspace ⇒ **明确拒绝，不回落平台工作区**：设计对这条是明确的 ——
    # 宁可拒绝，也不给一个看起来能用的空壳（运营会把别人的方法论当自己的跑一遍）。
    workspace_id = _workspace_for_tenant(context, binding.tenant_id)
    agent_context = admin_context(binding.tenant_id)
    # ``agent_type`` 与 ``output_contract`` 取自**库里那个 agent**，不取自绑定行、也不取自
    # DSL —— 与发布路径（``sync_binding_from_dify``）同一处来源。绑定行的
    # ``business_entry``（如 ``operator``）是**入口**，不是 agent 类型；把它当 agent_type
    # 会得到 ``has no default output contract``（实测踩到）。DSL 里也没有这两样：它们是
    # 「这个 app 在我们这边是一个什么样的智能体」，只有库里知道。
    live = _find_by_name(context.agent_manager, agent_context, binding.agent_name)
    if live is None:
        raise StandardAPIError(
            status.HTTP_404_NOT_FOUND,
            "AGENT_NOT_REGISTERED",
            "the bound agent is not registered in this environment",
        )
    source = DifyDslSource(
        base_url=console.dify_console_base_url,
        app_id=binding.app_id,
        api_key=console.dify_console_api_key,
        workspace_id=workspace_id,
        ops_tools_enabled=ops_app_registered(
            console.ops_bindings_file, console.environment, binding.tenant_id, app_id
        ),
    )
    try:
        config = resolve_pulled_config(
            context.agent_manager,
            agent_context,
            source,
            name=binding.agent_name,
            agent_type=live.config.agent_type,
            output_contract=live.config.output_contract,
            knowledge_registry=parse_dify_knowledge_bindings(context.settings.dify_knowledge_bindings),
            tool_registry=parse_dify_tool_bindings(context.settings.dify_tool_bindings),
            tenant_id=binding.tenant_id,
        )
    except DifyDslError as exc:
        # Dify 自己拒绝或不可达：带它自己的码转达（调用方要能区分「凭据被拒」与「服务挂了」）。
        raise StandardAPIError(status.HTTP_502_BAD_GATEWAY, exc.code, "dify pull failed") from exc
    return config.prompt, tuple(config.allowed_tools or ())


def create_gateway_app(
    *,
    settings: GatewayServerSettings | None = None,
    store: GatewayStore | None = None,
    runtime: GatewayRuntime | None = None,
    caller_resolver: CallerContextResolver | None = None,
    order_authorizer: OrderAuthorizer | None = None,
    platform_resolver: PlatformIdentityResolver | None = None,
    faq_catalog: FAQCatalog | None = None,
    agent_manager: AgentManager | None = None,
    shortcut_manager: ShortcutManager | None = None,
) -> FastAPI:
    selected_settings = settings or GatewayServerSettings.from_env()
    selected_settings.validate()
    if selected_settings.chat_node_targets_file:
        from aiops_diagnostics.node_publication import targets

        targets(selected_settings.chat_node_targets_file)
        if (
            not selected_settings.chat_route_bindings_file
            or Path(selected_settings.chat_route_bindings_file).resolve()
            != Path(selected_settings.database_file).resolve()
        ):
            raise ValueError("managed node routes must share the gateway version database")
    selected_store = store or GatewayStore(selected_settings.database_file)
    # Restart recovery, before this app can serve anything: every job the
    # previous process left `queued`/`running` is converged to an honest
    # terminal state here instead of hanging until its deadline (T3/#356,
    # T6/#492). Deliberately not in ``GatewayStore.__init__`` — short-lived CLI
    # commands (`aiops-gateway devices`) open the same database and must not
    # end live work; that mistake is what #492 fixed.
    selected_store.recover_interrupted_jobs()
    selected_runtime = runtime or GatewayRuntime.from_settings(selected_store, selected_settings)
    # #782：把试运行要用的运营商站点范围解析器注入运行时。构造它需要 Settings 与内部凭据，
    # 正是装配层已经持有的东西；放在这里而不是运行时里 import（那会形成
    # gateway_api → gateway_runtime → gateway_api 的环）。注入失败 ⇒ 不设 ⇒ 试运行的
    # 运营商范围落到空集合（fail-closed），不是租户级。
    # **整块**都在 suppress 里，`hasattr` 也不例外：一个 `__getattr__` 会抛的替身
    # （测试用的直白替身就是这样）在 `hasattr` 那一刻就会炸。这里的语义是"能配就配上，
    # 配不上就让试运行的运营商范围落到空集合（fail-closed）"，所以任何失败都不该逸出。
    trial_scope: Any = None
    with contextlib.suppress(Exception):
        trial_scope = _operator_site_scope(Settings.from_config(selected_settings.server_config_file))
        if hasattr(selected_runtime, "set_trial_operator_scope"):
            selected_runtime.set_trial_operator_scope(trial_scope)
    selected_resolver = caller_resolver or _caller_resolver(selected_settings)
    diagnostic_settings = getattr(selected_runtime, "diagnostic_settings", None)
    selected_authorizer = order_authorizer or (
        # ``shop_directory`` 让「快照说不可见」时能向公司权威端点确认一次
        # （见 ``ScopedOrderAuthorizer`` 的 docstring）。未配置时为 ``None``，
        # 此时行为与之前逐字一致：只有快照，判不可见即拒绝。
        ScopedOrderAuthorizer(
            diagnostic_settings,
            shop_directory=(
                _shop_directory(Settings.from_config(selected_settings.server_config_file))
                if diagnostic_settings is not None and selected_settings.server_config_file
                else None
            ),
        )
        if diagnostic_settings is not None
        else DisabledOrderAuthorizer()
    )
    # 聊天 worker 在写结果之前要用**同一个**授权器再判一次本人归属（APP-RUNTIME-01 续办
    # 必修 3）。注入而不是反向 import：`gateway_api` 已经 import 运行时，反向会成环。
    #
    # 整块都在 `suppress` 里，而且**不靠 `hasattr` 判断**：一个"任何属性都返回可调用对象"
    # 的替身（测试里的直白替身就是这样）会让 `hasattr` 恒为 True，随后调用就抛。
    # 与上面 `set_trial_operator_scope` 同一条理由：这里的语义是"能配就配上"，
    # 配不上就让 worker fail closed（判不出归属 ⇒ 落 failed），而不是让装配炸掉。
    with contextlib.suppress(Exception):
        selected_runtime.set_order_authorizer(selected_authorizer)
    if platform_resolver is not None:
        selected_platform_resolver = platform_resolver
    else:
        try:
            platform_settings = Settings.from_config(selected_settings.server_config_file)
        except (ValueError, OSError):
            # Keep existing non-FAQ endpoints bootable; FAQ requests fail closed
            # when the production identity configuration is unavailable.
            platform_settings = Settings()
        selected_platform_resolver = PlatformIdentityResolver(MySQLPlatformDirectory(platform_settings))
    selected_faq_catalog = faq_catalog or FAQCatalog.bundled()
    if agent_manager is None:
        configured_models = allowed_models_from_settings(diagnostic_settings)
        knowledge_resolver = None
        if selected_settings.kb_service_base_url:
            # Real publish validation (T5/#171): bindings must resolve against
            # live kb-service. Without a kb URL the manager keeps the
            # fail-closed default (bindings never publish).
            from aiops_diagnostics.agent_debug import KbBindingResolver, KbServiceKnowledgeClient

            knowledge_resolver = KbBindingResolver(
                KbServiceKnowledgeClient(
                    selected_settings.kb_service_base_url,
                    tenant_id="aiops",  # rebound per publish by the manager caller's tenant
                    timeout=selected_settings.kb_service_timeout_seconds,
                )
            )
        selected_agent_manager = AgentManager(
            AgentStore(selected_store.path),
            knowledge_resolver=knowledge_resolver,  # type: ignore[arg-type]  # None keeps fail-closed default
            allowed_models=configured_models or ("aiops-api",),
        )
    else:
        selected_agent_manager = agent_manager
    context = GatewayAPI(
        selected_settings,
        selected_store,
        selected_runtime,
        selected_resolver,
        selected_authorizer,
        selected_platform_resolver,
        selected_faq_catalog,
        selected_agent_manager,
        shortcut_manager,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        close_gateway_runtime(context.runtime)

    app = FastAPI(
        title="AI-Ops Gateway",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.gateway = context
    from aiops_diagnostics.operator_cpms import OperatorCpms, OperatorRefused

    context.operator_cpms = OperatorCpms(context)

    @app.exception_handler(OperatorRefused)
    async def operator_refused_handler(request: Request, exc: OperatorRefused):
        code = str(exc)
        return JSONResponse(
            status_code=410 if code == "OPERATOR_RESULT_RETRACTED" else 403,
            content={"error": {"code": code, "message": "operator request unavailable", "retryable": False}},
            headers={"Cache-Control": "no-store"},
        )

    # One attributable line per consumer order-status request (#observability).
    # Installed as a raw ASGI middleware so it sees the status the app actually
    # sends; see `request_observation` for why this is a log line and not a
    # second metric table.
    app.add_middleware(request_observation.AssistantQuestionObservation)

    @app.exception_handler(StandardAPIError)
    async def standard_api_error_handler(request: Request, exc: StandardAPIError) -> JSONResponse:
        # The refusal's stable code only exists here: every branch raises with
        # its own, and the request-level line has to say which one it was.
        request_observation.mark_refused(request, exc.code)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                }
            },
            headers=_no_store_headers(request),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        del exc
        request_observation.mark_refused(request, request_observation.INVALID_REQUEST)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "request validation failed",
                    "retryable": False,
                }
            },
            headers=_no_store_headers(request),
        )

    def authenticated_device(
        authorization: Annotated[str | None, Header()] = None,
    ) -> GatewayDevice:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="device token required")
        token = authorization.removeprefix("Bearer ").strip()
        try:
            return context.store.authenticate_device(token)
        except AuthenticationError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    def authenticated_caller(
        authorization: Annotated[str | None, Header()] = None,
        x_third_session: Annotated[str | None, Header()] = None,
        business_entry: Annotated[str | None, Header(alias="X-Business-Entry")] = None,
        source_key: Annotated[str | None, Header(alias=SOURCE_KEY_HEADER)] = None,
    ) -> ScopeContext:
        return _authenticate_caller(
            context.caller_resolver,
            authorization,
            x_third_session,
            platform_entry=business_entry,
            source_key=source_key,
            required_scope=STANDARD_ORDER_READ_SCOPE,
        )

    def authenticated_diagnosis_caller(
        authorization: Annotated[str | None, Header()] = None,
        x_third_session: Annotated[str | None, Header()] = None,
        business_entry: Annotated[str | None, Header(alias="X-Business-Entry")] = None,
        source_key: Annotated[str | None, Header(alias=SOURCE_KEY_HEADER)] = None,
    ) -> ScopeContext:
        return _authenticate_caller(
            context.caller_resolver,
            authorization,
            x_third_session,
            platform_entry=business_entry,
            source_key=source_key,
            required_scope=STANDARD_DIAGNOSIS_SCOPE,
        )

    def authenticated_faq_caller(
        authorization: Annotated[str | None, Header()] = None,
        x_third_session: Annotated[str | None, Header()] = None,
        business_entry: Annotated[str | None, Header(alias="X-Business-Entry")] = None,
        source_key: Annotated[str | None, Header(alias=SOURCE_KEY_HEADER)] = None,
    ) -> ScopeContext:
        return _authenticate_caller(
            context.caller_resolver,
            authorization,
            x_third_session,
            platform_entry=business_entry,
            source_key=source_key,
            required_scope=STANDARD_FAQ_SCOPE,
        )

    def authenticated_agent_caller(
        authorization: Annotated[str | None, Header()] = None,
        x_third_session: Annotated[str | None, Header()] = None,
        source_key: Annotated[str | None, Header(alias=SOURCE_KEY_HEADER)] = None,
    ) -> ScopeContext:
        return _authenticate_caller(
            context.caller_resolver,
            authorization,
            x_third_session,
            source_key=source_key,
            required_scope=AGENT_MANAGE_SCOPE,
        )

    def authenticated_shortcut_caller(
        authorization: Annotated[str | None, Header()] = None,
        x_third_session: Annotated[str | None, Header()] = None,
        source_key: Annotated[str | None, Header(alias=SOURCE_KEY_HEADER)] = None,
    ) -> ScopeContext:
        return _authenticate_caller(
            context.caller_resolver,
            authorization,
            x_third_session,
            source_key=source_key,
            required_scope=SHORTCUT_MANAGE_SCOPE,
        )

    def authenticated_shortcut_viewer(
        authorization: Annotated[str | None, Header()] = None,
        x_third_session: Annotated[str | None, Header()] = None,
        business_entry: Annotated[str | None, Header(alias="X-Business-Entry")] = None,
        source_key: Annotated[str | None, Header(alias=SOURCE_KEY_HEADER)] = None,
    ) -> ScopeContext:
        return _authenticate_caller(
            context.caller_resolver,
            authorization,
            x_third_session,
            platform_entry=business_entry,
            source_key=source_key,
            required_scope=STANDARD_FAQ_SCOPE,
        )

    def platform_identity(caller_dependency: Any):
        """Build the identity dependency for one required scope.

        The platform decision is one rule — "which content domain is this
        request in, given a VERIFIED caller and the entry context" — and it had
        been written out three times, twice byte-for-byte and once differing in
        a single line: the return type. That one line was load-bearing (the FAQ
        and assistant branches need the decision object, the shortcut branch
        needed `str(decision.platform)`), which is exactly why the copies
        drifted: each caller read the copy that suited its own shape.

        The factory returns the SAME shape to everyone and lets each caller take
        what it needs, so there is one rule and no per-caller variant of it.
        """

        def dependency(
            caller: ScopeContext = Depends(caller_dependency),  # noqa: B008
            business_entry: Annotated[str | None, Header(alias="X-Business-Entry")] = None,
        ) -> tuple[ScopeContext, PlatformDecision]:
            try:
                decision = context.platform_resolver.resolve(caller, business_entry)
            except FAQError as exc:
                raise _platform_error(exc) from exc
            except PlatformDirectoryError as exc:
                raise StandardAPIError(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    PLATFORM_UNAVAILABLE,
                    "platform identity unavailable",
                    retryable=True,
                ) from exc
            return caller, decision

        return dependency

    # One rule, three required scopes. The scope is the only thing that differs
    # between them, and it differs for a reason worth keeping written down:
    #
    # * `faq_identity` — the pure-FAQ surface reads catalogue content.
    # * `assistant_identity` — the assistant can trigger a full order diagnosis
    #   (a write action) on its order branch, so it authenticates with the same
    #   scope as `/v1/standard/diagnoses` (aiops:diagnoses:write), NOT the
    #   weaker faq:read. It then resolves the platform content domain for the
    #   FAQ branch.
    # * `shortcut_identity` — the shortcut listing renders the product home for
    #   every authenticated user, so it needs no admin role; tenant isolation
    #   is enforced inside the store by the caller's effective tenant.
    faq_identity = platform_identity(authenticated_faq_caller)
    assistant_identity = platform_identity(authenticated_diagnosis_caller)
    # Same required scope as the diagnosis line (an analysis is a read-heavy
    # job on the same order data), same shape. Named for its gate rather than
    # its scope: the entry check below is what makes it the operator face.
    operator_diagnosis_identity = platform_identity(authenticated_diagnosis_caller)
    shortcut_identity = platform_identity(authenticated_shortcut_viewer)

    def dify_shared_credential(
        authorization: Annotated[str | None, Header()] = None,
    ) -> DifyIdentity:
        """The Dify adapter's own door (#581) — which identity it is, is the credential (#586).

        Deliberately NOT the caller chain, and deliberately not named
        ``authenticated_*``: Dify is a server, not one of our users. It has no
        session and no UPMS subject, and an ``X-AIOps-Source-Key`` is not on
        offer. The credential is the shared API key the operator configures on
        both sides — the same shape as Dify's own External Knowledge API, where
        the "API Key" is exactly this string.

        **The credential decides the identity, and nothing else does.** That is
        the whole of #586: a *debug* key resolves to the fixed debug tenant and
        the read-only tool subset, so the operator can preview an app against
        real data without ever holding a production credential. Every other
        signal a request can carry — headers, query strings, the body — is
        ignored on purpose, and ``plans_for`` takes no request-derived argument
        for that reason.

        Unconfigured means the route is **not enabled at all** (404, see the
        route below), never "no key needed". Half-configuration is a startup
        error, enforced in ``GatewayServerSettings.validate``.
        """
        configured = context.settings.dify_knowledge_api_key
        if not configured:
            raise StandardAPIError(status.HTTP_404_NOT_FOUND, "DIFY_RETRIEVAL_UNAVAILABLE", "route not found")
        if not authorization or not authorization.startswith("Bearer "):
            raise StandardAPIError(
                status.HTTP_401_UNAUTHORIZED, "DIFY_CREDENTIAL_REQUIRED", "bearer credential required"
            )
        presented = authorization.removeprefix("Bearer ").strip()
        # Which slot did it match? Both comparisons are constant-time and both
        # run, so which one matched is not observable from timing.
        matches_production = secrets.compare_digest(configured.encode("utf-8"), presented.encode("utf-8"))
        matches_debug = bool(context.settings.dify_debug_api_key) and secrets.compare_digest(
            context.settings.dify_debug_api_key.encode("utf-8"), presented.encode("utf-8")
        )
        if matches_debug:
            return debug_dify_identity(context.settings)
        if matches_production:
            return PRODUCTION_IDENTITY
        raise StandardAPIError(status.HTTP_403_FORBIDDEN, "DIFY_CREDENTIAL_REJECTED", "credential rejected")

    def _libraries_for_tenant(settings: GatewayServerSettings, runtime: GatewayRuntime) -> Any:
        """A per-tenant library fetcher, or a no-op when auto-registration is off.
        Returns ``None`` for "no auto sources" rather than raising: an upstream
        that is down must degrade to the manual registry (which is why the manual
        registry exists), not turn a working request into a 502.
        """

        def fetch(tenant_id: str) -> tuple[str, ...] | None:
            client = runtime.kb_search_client
            if client is None:
                return None
            try:
                owned = client.for_tenant(tenant_id).list_knowledge_bases()
            except Exception:  # noqa: BLE001 - upstream down: no auto source this request
                return None
            return tuple(str(item["knowledge_base_id"]) for item in owned if item.get("knowledge_base_id"))

        return fetch

    def _knowledge_sources(settings: GatewayServerSettings, store: GatewayStore) -> KnowledgeSourceResolver:
        """One resolver for retrieval and listing (#925).

        Auto-registration is built from what onboarding recorded: which tenants
        were onboarded, and which libraries each of them owns right now. Both
        read fresh on every request — a library created a second ago must be
        selectable without any cache to invalidate, and a tenant that has not been
        onboarded must not acquire a source merely because RAGFlow holds rows
        under a name that looks like one.

        Any failure to read the upstream listing degrades to "no auto sources",
        never to an error: the manual registry must keep working when kb-service
        is unreachable, which is exactly the manual registry's purpose.
        """
        client = context.runtime.kb_search_client
        auto_tenants: list[str] = []
        if settings.dify_knowledge_autoregister and client is not None:
            try:
                auto_tenants = [
                    str(row["tenant_id"]) for row in store.list_ragflow_tenants(settings.environment)
                ]
            except Exception:  # noqa: BLE001 - an unreadable table is "none onboarded"
                auto_tenants = []
        # **No upstream call here.** Libraries are fetched per tenant, at the point
        # a caller actually names that tenant: retrieval asks for one, the picker
        # asks for all. Doing it eagerly would put every onboarded tenant's HTTP
        # timeout in front of every retrieval — one tenant's slow upstream would
        # delay another tenant's unrelated request.
        return KnowledgeSourceResolver.from_parts(
            env_bindings=settings.dify_knowledge_bindings,
            rows=store.list_knowledge_id_bindings(),
            auto_tenants=auto_tenants,
        )

    @app.post(DIFY_RETRIEVAL_PATH)
    def dify_retrieval(
        payload: dict[str, Any],
        identity: DifyIdentity = Depends(dify_shared_credential),  # noqa: B008
    ) -> dict[str, Any]:
        """The External Knowledge API hop Dify calls (PRD #577 挂接跳数).

        Everything the answer depends on already happened above this line:
        the credential was checked (and it, not the request, decided *which*
        identity this is), and the tenant plus the knowledge-base allow-list are
        read from **our** registry keyed by ``knowledge_id``. Nothing in the
        request body can widen either — that is what makes this an adapter
        rather than a proxy. The debug identity narrows one step further: only
        the knowledge ids its tool plan names are reachable through it.
        """
        sources = _knowledge_sources(context.settings, context.store)
        try:
            request = parse_dify_retrieval_request(payload, max_top_k=context.settings.kb_search_max_results)
        except DifyRequestError as exc:
            raise StandardAPIError(status.HTTP_400_BAD_REQUEST, exc.code, exc.message) from exc

        binding = sources.resolve(
            request.knowledge_id, fetch_libraries=_libraries_for_tenant(settings, context.runtime)
        )
        if binding is None:
            # A knowledge_id we never registered. 404 says "not here" and
            # reveals nothing about which ids exist, or about our data plane.
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, "DIFY_KNOWLEDGE_NOT_FOUND", "knowledge source not found"
            )
        if not identity.allows(request.knowledge_id, binding.tenant_id):
            # Registered, but not for THIS identity (#586). Deliberately the same
            # 404 as "never registered": a 403 here would say "this one exists and
            # you may not use it", which is itself a disclosure about the registry.
            # The debug identity can therefore probe for production knowledge ids
            # and learn nothing.
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, "DIFY_KNOWLEDGE_NOT_FOUND", "knowledge source not found"
            )

        client = context.runtime.kb_search_client
        signer = context.runtime.media_signer
        if client is None or signer is None:
            # kb-service or the media plane is unconfigured, or the search
            # dependency is down. Both are "cannot answer now" — reported as
            # exactly that, never as an empty 200 result set, because Dify
            # reads a 200 as `records` and would render "nothing matched" for
            # what is really an outage (#581 acceptance).
            raise StandardAPIError(
                status.HTTP_502_BAD_GATEWAY,
                "DIFY_KNOWLEDGE_UNAVAILABLE",
                "knowledge retrieval is temporarily unavailable",
                retryable=True,
            )
        top_k = request.top_k or context.settings.kb_search_max_results
        # Rebind to the REGISTERED tenant (#612). The process-level client is
        # deliberately bound to the neutral "aiops" tenant (see
        # GatewayRuntime.from_settings), and every other consumer rebinds before
        # searching — the customer QA path and the media fetch both do. Without
        # this line the search goes out under "aiops", RAGFlow's per-tenant
        # ownership check rejects it (code=102), and the adapter answers 502 for
        # every registered knowledge_id. Found on 41 on 2026-10-09, by opening
        # the switch: direct kb-service calls with the registered tenant header
        # returned chunks while the same call through the adapter 502'd.
        search_client = client.for_tenant(binding.tenant_id) if hasattr(client, "for_tenant") else client
        try:
            raw = search_client.search(binding.knowledge_base_ids, request.query, top_k)
            chunks = normalize_search_response(
                raw,
                tenant_id=binding.tenant_id,
                agent_version=f"dify:{binding.knowledge_id}",
                session_id=None,
                knowledge_base_ids=binding.knowledge_base_ids,
                media_signer=signer,
                # Truncate to what THIS caller asked for, not to the deployment
                # cap. Every registered base is searched with `top_k` and the
                # results are merged, so a binding with several bases returns
                # top_k × bases chunks — more records than Dify requested. The
                # cap belongs in `parse_dify_retrieval_request`, which already
                # applied it to `top_k`.
                max_results=top_k,
            )
        except KnowledgeSearchUnavailable as exc:
            raise StandardAPIError(
                status.HTTP_502_BAD_GATEWAY,
                "DIFY_KNOWLEDGE_UNAVAILABLE",
                "knowledge retrieval is temporarily unavailable",
                retryable=True,
            ) from exc
        return {
            "records": dify_records(
                chunks,
                score_threshold=request.score_threshold,
                public_base_url=context.settings.public_base_url,
            )
        }

    @app.get(DIFY_KNOWLEDGE_LIST_PATH)
    def dify_knowledge_list(
        identity: DifyIdentity = Depends(dify_shared_credential),  # noqa: B008
    ) -> dict[str, Any]:
        """What this endpoint can serve, for Dify's console to render as a picker.

        Dify's "connect external knowledge base" dialog asks for a free-text
        ``knowledge_id``, which means the operator must already know an opaque
        string — the exact step that made "configure a knowledge base" an
        engineer's job. This route is the missing half: same credential, same
        registry, so the console can offer what retrieval will accept.

        **It lists the registry, not RAGFlow.** A ``knowledge_id`` is OURS (the
        key Dify sends back), and only registered ones can retrieve. Listing
        RAGFlow dataset ids instead would offer values retrieval answers 404 to:
        a picker that looks richer and works worse.

        The debug identity sees only its own subset — whatever a picker shows,
        the same credential must be able to retrieve, or the preview UI would
        suggest choices the preview cannot honour.
        """
        # ONE resolver for both routes (#925). Before this, retrieval and listing
        # each merged the same two tables themselves — two implementations of one
        # rule, which is the shape that drifts.
        sources = _knowledge_sources(context.settings, context.store)
        items: list[dict[str, Any]] = []
        bindings = sources.visible(identity, fetch_libraries=_libraries_for_tenant(settings, context.runtime))
        for binding in bindings:
            items.append(
                {
                    "knowledge_id": binding.knowledge_id,
                    # The operator's own label, and the ids it maps to — the ids
                    # stay visible because they are what retrieval matches on.
                    "name": binding.display_name or binding.knowledge_id,
                    "knowledge_base_ids": list(binding.knowledge_base_ids),
                }
            )
        return {"knowledge_items": items, "total": len(items)}

    def dify_publish_credential(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        """The publish route's own door — a **separate** shared key, not the
        knowledge one and not any user credential.

        Three credentials are in play on the ``/v1/dify/`` prefix and they must
        not collapse into one (see ``dify_publish_api_key`` in
        ``gateway_config``): the knowledge key is presented on every retrieval,
        the console key stays server-side, and this one authorises a *write*
        (a new published version). Reusing the knowledge key would make "the
        operator's preview app can read knowledge" mean "whoever can read
        knowledge can publish". Reusing the console key would put the write
        credential on the caller's side, where it is no longer the server's.

        The user-credential chain is deliberately **not** consulted. On this
        deployment the company token's payload is not verified (company-parity
        mode), so a caller-supplied JWT is *claimable*, not proven — measured
        on 41: a token signed with a made-up key reached the business layer of
        ``/v1/standard/diagnoses`` (404, not 401). A publish action gated on
        that would be gated on nothing.

        Unconfigured means the route is **not enabled at all** (404), never
        "no key needed" — the same mechanism as the retrieval route.
        """
        configured = context.settings.dify_publish_api_key
        if not configured:
            raise StandardAPIError(status.HTTP_404_NOT_FOUND, "DIFY_PUBLISH_UNAVAILABLE", "route not found")
        if not authorization or not authorization.startswith("Bearer "):
            raise StandardAPIError(
                status.HTTP_401_UNAUTHORIZED, "DIFY_CREDENTIAL_REQUIRED", "bearer credential required"
            )
        presented = authorization.removeprefix("Bearer ").strip()
        if not secrets.compare_digest(configured.encode("utf-8"), presented.encode("utf-8")):
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "DIFY_CREDENTIAL_REJECTED", "credential rejected"
            )

    @app.post("/v1/dify/publish")
    def dify_publish(
        payload: DifyPublishRequest,
        _: None = Depends(dify_publish_credential),  # noqa: B008
        company_user_id: Annotated[str | None, Header(alias="X-AIOps-Initiator")] = None,
        company_username: Annotated[str | None, Header(alias="X-AIOps-Initiator-Name")] = None,
        dify_account_id: Annotated[str | None, Header(alias="X-AIOps-Dify-Account")] = None,
    ) -> dict[str, Any]:
        """Freeze what Dify currently holds as a new published version (#577).

        **This is the operator's own step**, so it no longer needs an engineer
        on the box: they edit the prompt in Dify, then call this with the app
        id. It is the same action ``aiops admin sync-from-dify`` performs — one
        implementation, so the two cannot drift.

        The blast radius is the design. The request names **one already-bound
        Dify app**; the tenant, the agent name, its type, its output contract
        and its knowledge binding all come from ``dify_app_registry``. So the
        worst a leaked key buys is *re-freezing the current content of a
        binding that was already declared* — it cannot add a binding, move a
        route, switch a type, attach a knowledge base, or inject arbitrary
        text. Re-publishing unchanged content is a no-op by construction (the
        sync compares against the published snapshot first), so "worst case"
        collapses to "nothing changed".

        Why an explicit call rather than a timer: Dify has no draft area.
        ``export_dsl`` reads ``app.app_model_config_id`` — the single pointer a
        save overwrites — so Dify cannot say "this is unfinished", and a timer
        would publish half-typed prompts. A human pressing this is the only
        honest signal that they are done.
        """
        return _publish_bound_app(
            context,
            payload.app_id,
            initiator=_publish_initiator(company_user_id, company_username, dify_account_id),
        )

    @app.post("/v1/dify/retract")
    def dify_retract(
        payload: DifyRetractRequest,
        _: None = Depends(dify_publish_credential),  # noqa: B008
        company_user_id: Annotated[str | None, Header(alias="X-AIOps-Initiator")] = None,
        company_username: Annotated[str | None, Header(alias="X-AIOps-Initiator-Name")] = None,
        dify_account_id: Annotated[str | None, Header(alias="X-AIOps-Dify-Account")] = None,
    ) -> dict[str, Any]:
        """撤回这一版：停止对新请求与未放行结果的交付。**不恢复别版。**

        与发布**同一道门**（`dify_publish_credential`）：能发布的人才能撤回，不需要
        第二套权限；撤回的爆炸半径与发布相同 —— 只能动一个已绑定 app 的那一条路由。

        三件必须同时成立，缺一条"撤回"就只是屏幕上的一句话：

        1. 管理行改成 `retracted`（**不删行** —— 删行会让请求掉回直读）；
        2. 在飞运行被收敛（本地终态 + 撤权 + 请求 Dify stop）；
        3. 历史与在飞正文不再交付（回读守卫发现"当前绑定不再指向签发时那一版"）。

        已发出的数据不承诺追回。
        """
        binding = next(
            (item for item in context.runtime.dify_app_registry.all() if item.app_id == payload.app_id),
            None,
        )
        initiator = _publish_initiator(company_user_id, company_username, dify_account_id)
        if binding is None:
            _record_publish_audit(
                context,
                app_id=payload.app_id,
                action="refused",
                reason_code="DIFY_APP_NOT_BOUND",
                initiator=initiator,
            )
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, "DIFY_APP_NOT_BOUND", "app is not a bound agent source"
            )
        database = str(getattr(context.settings, "chat_route_bindings_file", "") or "")
        environment = str(getattr(context.settings, "execution_environment", "") or "")
        if not database or not environment:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "CHAT_ROUTE_BINDINGS_DISABLED",
                "route bindings are not managed on this server",
            )
        from pathlib import Path

        from aiops_diagnostics.chat_route_bindings import (
            ChatRouteBindingStore,
            RouteBindingError,
        )

        store = ChatRouteBindingStore(Path(database).expanduser().absolute())
        entry = str(getattr(binding, "business_entry", "") or "consumer")
        from aiops_diagnostics.consumer_publication import route_scenario

        scenario = route_scenario(context.settings, binding)
        current = store.get(environment, binding.tenant_id, entry, scenario)
        if current is None:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, "CHAT_ROUTE_BINDING_MISSING", "no route to retract"
            )
        if current.app_id != payload.app_id:
            # 同租户/入口下别人家的 app：拒绝，不越权撤回另一条路由。
            raise StandardAPIError(
                status.HTTP_409_CONFLICT, "CHAT_ROUTE_BINDING_MISMATCH", "route names another app"
            )
        try:
            retired = store.retract(
                environment=environment,
                tenant_id=binding.tenant_id,
                entry=entry,
                scenario=scenario,
                expected_revision=current.revision,
                updated_by=initiator[0] or initiator[1] or "",
            )
        except RouteBindingError as exc:
            raise StandardAPIError(status.HTTP_409_CONFLICT, exc.code, "route was not retracted") from exc
        # 收敛在飞运行是**第二件事**，不能因为它出问题就让整次撤回报 500：指针已经改成
        # `retracted`，撤回本身成立。`contextlib.suppress` 与 `set_order_authorizer`
        # 同一写法（`hasattr` 对裸替身恒真）。
        settled: dict[str, Any] = {"settled_runs": []}
        with contextlib.suppress(Exception):
            settled = context.runtime.settle_retracted_runs(route=current)
        context.operator_cpms.retract(current)
        _record_publish_audit(
            context,
            app_id=payload.app_id,
            action="retracted",
            reason_code="",
            binding=binding,
            initiator=initiator,
            version_no=current.agent_version_no,
        )
        return {
            "app_id": payload.app_id,
            "tenant_id": binding.tenant_id,
            "state": retired.state,
            "revision": retired.revision,
            "version_no": current.agent_version_no,
            "settled_runs": settled["settled_runs"],
        }

    @app.post("/v1/dify/restore")
    def dify_restore(
        payload: DifyRestoreRequest,
        _: None = Depends(dify_publish_credential),  # noqa: B008
        initiator: Annotated[str | None, Header(alias="X-AIOps-Initiator")] = None,
    ) -> dict[str, Any]:
        try:
            return _restore_bound_app(context, payload.app_id, payload.version_no, (initiator or "", "", ""))
        except StandardAPIError:
            raise
        except Exception as exc:  # noqa: BLE001 - 不可验证历史版本必须拒绝。
            raise StandardAPIError(409, "DIFY_VERSION_UNAVAILABLE", "version not available") from exc

    @app.post("/v1/dify/versions")
    def dify_versions(
        payload: DifyPublishRequest, _: None = Depends(dify_publish_credential)
    ) -> dict[str, Any]:  # noqa: B008
        binding, row, workspace = _managed_app_route(context, payload.app_id)
        with context.agent_manager.store._connection() as connection:
            rows = connection.execute(
                "SELECT version_no, snapshot_json FROM agent_versions WHERE agent_id=? AND tenant_id=?",
                (row.agent_id, binding.tenant_id),
            ).fetchall()
        versions = []
        for version in rows:
            snapshot = json.loads(version["snapshot_json"])
            if snapshot.get("app_id") == payload.app_id and snapshot.get("workspace_id") == workspace:
                versions.append({"version_no": version["version_no"]})
        return {"app_id": payload.app_id, "versions": versions, "state": row.state}

    @app.post("/v1/operator/publish")
    def operator_publish(
        payload: OperatorPublishRequest,
        identity: tuple[ScopeContext, Any] = Depends(operator_diagnosis_identity),  # noqa: B008
        business_entry: Annotated[str | None, Header(alias="X-Business-Entry")] = None,
    ) -> dict[str, Any]:
        """运营**自己**触发的那一步（#577 的最后一跳；ADR-0018 的第二种门）。

        与 ``POST /v1/dify/publish`` 是**同一个实现**（``_publish_bound_app``）；
        区别只在**谁被允许调**：那条凭一把独立的共享密钥，这条凭运营自己的会话。

        **三个条件全部满足才放行**：

        1. 调用者解析得出身份（会话有效）；
        2. ``X-Business-Entry`` 必须是 ``operator`` —— 客户端会话不该能触发发布；
        3. 该 ``(租户, 入口)`` 在注册表里**确实绑了** agent。

        第 3 条不是多余的：没有它，任何运营会话都能拿任意 ``app_id`` 去试，而
        ``_publish_bound_app`` 的 404 就成了一次**绑定枚举**。加它之后，一个入口
        只能发布**它自己被点名的那一个** app。

        为什么不像知识检索那条要求 source key：那条是**服务器**在调（Dify），
        这条是**人**在调（运营在网页上点），运营没有 source key。
        """
        caller, _decision = identity
        if not is_operator_entry(business_entry):
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN,
                "OPERATOR_ENTRY_REQUIRED",
                "publishing requires the operator entry",
            )
        registered_name, configured = context.runtime._registered_agent(
            caller.effective_tenant_id, "operator"
        )
        if not configured or registered_name is None:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "OPERATOR_NOT_BOUND",
                "this tenant has no bound operator agent",
            )
        # 归属用注册表**自己的**按 (租户, 入口) 查（`lookup`），不在这里写租户比较 ——
        # 本仓的租户判据只有 `order_visibility` 一处实现，这是 `test_order_visibility`
        # 拦住的那类漂移。`lookup` 直接由主键给出这一对绑定的是谁，正好是要问的问题。
        binding = context.runtime.dify_app_registry.lookup(caller.effective_tenant_id, "operator")
        if binding is None or binding.app_id != payload.app_id:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, "DIFY_APP_NOT_BOUND", "app is not a bound agent source"
            )
        # 这条路由的发起人是**会话身份**（有 caller 可得），不是请求头 —— 与共享密钥那条
        # 相比，这里能给出更具体的人，所以审计里记的是会话里的公司用户 id。
        return _publish_bound_app(
            context,
            payload.app_id,
            initiator=(str(getattr(caller, "subject", None) and caller.subject.b_user_id or ""), "", ""),
        )

    ops_service = (
        OpsToolService(selected_settings, selected_agent_manager)
        if selected_settings.ops_bindings_file
        else None
    )

    def run_ops_tool(
        tenant_id: str, tool: str, payload: ChargingInfoInput | OrderListInput, authorization: str | None
    ) -> dict[str, Any]:
        if ops_service is None:
            raise StandardAPIError(404, "OPS_TOOLS_DISABLED", "ops tools are not configured")
        prefix, _, token = (authorization or "").partition(" ")
        try:
            return ops_service.execute(token if prefix.lower() == "bearer" else "", tenant_id, tool, payload)
        except OpsToolError as exc:
            raise StandardAPIError(exc.status, exc.code, "ops tool request refused") from exc

    @app.post("/v1/dify/ops/{tenant_id}/charging_info")
    def ops_charging_info(
        tenant_id: str,
        payload: ChargingInfoInput,
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return run_ops_tool(tenant_id, "charging_info", payload, authorization)

    @app.post("/v1/dify/ops/{tenant_id}/order_list")
    def ops_order_list(
        tenant_id: str,
        payload: OrderListInput,
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return run_ops_tool(tenant_id, "order_list", payload, authorization)

    def dify_tools_credential(authorization: Annotated[str | None, Header()] = None) -> None:
        """The 3A catalog credential — a key that can do exactly one thing (#772 E).

        Configured-or-absent is the same mechanism the other two Dify routes use:
        an unconfigured key means the whole route group is **404**, never "no key
        needed". Half-configuration cannot happen here (there is no second half —
        the endpoint calls no upstream), so the only rule left is that it must
        differ from every other Dify key, which ``GatewayServerSettings.validate``
        enforces at startup.

        **Why this endpoint is safe to expose at all.** Dify's custom-API-tool
        credential is workspace-shared and visible to every admin who can edit
        the provider, so it is the least protected secret in this picture. The
        answer is not to protect it better but to make it worth nothing: this
        route reads no data source, creates no run, and returns one fixed
        string. A leaked key tells an attacker what our seven tools are called —
        which is in an OpenAPI document an operator pastes into a console, not a
        secret. Keeping it a **separate** key from the retrieval and publish
        credentials is what stops "can list the catalog" from equalling either.
        """
        configured = context.settings.dify_tools_api_key
        if not configured:
            raise StandardAPIError(status.HTTP_404_NOT_FOUND, "DIFY_TOOLS_UNAVAILABLE", "route not found")
        if not authorization or not authorization.startswith("Bearer "):
            raise StandardAPIError(
                status.HTTP_401_UNAUTHORIZED, "DIFY_CREDENTIAL_REQUIRED", "bearer credential required"
            )
        presented = authorization.removeprefix("Bearer ").strip()
        if not secrets.compare_digest(configured.encode("utf-8"), presented.encode("utf-8")):
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "DIFY_CREDENTIAL_REJECTED", "credential rejected"
            )

    @app.post("/v1/dify/tools/{tool}")
    def dify_tool_note(tool: str, _: None = Depends(dify_tools_credential)) -> dict[str, Any]:  # noqa: B008
        """One fixed note per tool — the catalog Dify's preview calls land on (#772 E).

        It returns ``CATALOG_NOTE`` whatever the tool is, and **reads nothing**:
        no order, no device, no run, no model. That is the point of 3A — the
        operator gets our seven tools *selectable in Dify*, while the natively
        driven Dify loop stays unable to touch production data. Dify's own
        "调试与预览" cannot reach our executor, so a preview answer would never
        reflect production behaviour anyway; saying so in the response is more
        honest than returning something that looks like a diagnosis.

        A ``tool`` outside the seven is a **404**, not a 200 for every path:
        with a fixed reply this is the only observable difference the path can
        carry, and answering "ok" to ``/v1/dify/tools/drop_table`` is how a
        catalog starts looking like an arbitrary endpoint.
        """
        if tool not in AI_OPS_TOOL_NAMES:
            raise StandardAPIError(status.HTTP_404_NOT_FOUND, "DIFY_TOOL_UNKNOWN", "tool not found")
        return {"tool": tool, "note": CATALOG_NOTE}

    def dify_workspace_credential(
        token: Annotated[str | None, Header(alias=INTERNAL_TOKEN_HEADER)] = None,
        timestamp: Annotated[str | None, Header(alias=TIMESTAMP_HEADER)] = None,
    ) -> None:
        """`GET /v1/dify/workspace` 的门：网关自己的**内部令牌**（#879）。

        为什么不是第七把 Dify 钥匙：这条端点的调用方是**我们自己的另外两个进程**
        （登录代理、发布桥），不是 Dify。Dify 侧的钥匙都是"泄露了也要拿不到东西"的
        设计，而那几把照这里的语义就不成立了 —— 这里回的是**某一个租户**的
        workspace id。所以用进程间凭据（HMAC + 时间窗，与平台 ``InternalTokenManager``
        同一消息格式），且它不进任何面向 Dify 的配置。

        未配置密钥 ⇒ 整条路由 404（与其余 Dify 路由同一条「未配置即不启用」）。
        """
        configured = context.settings.internal_token_secret
        if not configured or not context.settings.environment:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, "DIFY_WORKSPACE_API_UNAVAILABLE", "route not found"
            )
        if not validate_internal_token(
            token,
            timestamp,
            configured,
            expire_seconds=context.settings.internal_token_expire_seconds,
        ):
            raise StandardAPIError(
                status.HTTP_401_UNAUTHORIZED, "INTERNAL_TOKEN_REQUIRED", "internal token required"
            )

    @app.get("/v1/dify/workspace")
    def dify_workspace(
        request: Request,
        tenant_id: str,
        _: None = Depends(dify_workspace_credential),  # noqa: B008
    ) -> dict[str, Any]:
        """``(租户, 本机部署身份)`` → 那个 workspace 的 id（#879 / ADR-0059 §5）。

        三件事在这条端点上各自是一次安全决定：

        * **环境来自部署身份，永不来自请求。** 请求里带了 ``environment`` 就是 400，
          且**不**回退到部署身份 —— 允许跨环境查同一个 ``tenant_id`` 才是真的越权路径，
          而"带了但被忽略"会让调用方以为自己拿到了另一个环境的答案。
        * **只查不建。** 缺行是 404 ``DIFY_WORKSPACE_UNRESOLVED``，不是顺手建一个：
          建只属于开户那一步（#880）。
        * **输出只有一个 UUID。** 拿到它还得有 ``ADMIN_API_KEY`` 才能做事，而
          ``ADMIN_API_KEY`` 只在网关与代理手里（Dify 侧的隔离由 workspace 自己保证）。

        失败是**可区分的码**而不是空串：``CREATING`` 可重试、``FAILED`` 要重建、
        ``UNKNOWN`` 先对账。状态映射只有一份（在 ``DifyWorkspaceError.http_status``）。
        """
        if "environment" in request.query_params:
            raise StandardAPIError(
                status.HTTP_400_BAD_REQUEST,
                "DIFY_WORKSPACE_ENVIRONMENT_NOT_ACCEPTED",
                "environment comes from this deployment's identity, not from the request",
            )
        try:
            workspace_id = context.dify_workspaces.resolve(tenant_id, context.settings.environment)
        except DifyWorkspaceError as exc:
            raise StandardAPIError(exc.http_status, exc.code, exc.message, retryable=exc.retryable) from exc
        except ValueError as exc:
            raise StandardAPIError(
                status.HTTP_400_BAD_REQUEST, "DIFY_WORKSPACE_TENANT_INVALID", str(exc)
            ) from exc
        return {"workspace_id": workspace_id, "environment": context.settings.environment}

    def dify_trial_credential(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        """试运行的凭据闸（#772 F / #781）—— 第六把独立的 Dify 钥匙。

        与其他 Dify 路由同一条机制：未配置 ⇒ 整组 404，**不是**"不需要钥匙"。

        它为什么必须再独立一把：这条路由触发一次**真实模型执行**（真跑执行器与只读数据源），
        所以"能列工具目录"或"能发布"都不该顺带等于"能在真数据上跑一次诊断"。
        钥匙决定**能不能调**；能看哪些订单由 ``dify_trial_debug_tenant`` 决定 ——
        两件事分开，正是这一层不去猜发起人身份的原因。
        """
        configured = context.settings.dify_trial_api_key
        if not configured:
            raise StandardAPIError(status.HTTP_404_NOT_FOUND, "DIFY_TRIAL_UNAVAILABLE", "route not found")
        if not authorization or not authorization.startswith("Bearer "):
            raise StandardAPIError(
                status.HTTP_401_UNAUTHORIZED, "DIFY_CREDENTIAL_REQUIRED", "bearer credential required"
            )
        presented = authorization.removeprefix("Bearer ").strip()
        if not secrets.compare_digest(configured.encode("utf-8"), presented.encode("utf-8")):
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "DIFY_CREDENTIAL_REJECTED", "credential rejected"
            )

    @app.post("/v1/dify/trial", status_code=status.HTTP_202_ACCEPTED)
    def dify_trial(
        payload: DifyTrialRequest,
        request: Request,
        _: None = Depends(dify_trial_credential),  # noqa: B008
        initiator: Annotated[str | None, Header(alias="X-AIOps-Initiator")] = None,
        initiator_tenant: Annotated[str | None, Header(alias="X-AIOps-Initiator-Tenant")] = None,
        initiator_type: Annotated[str | None, Header(alias="X-AIOps-Initiator-Type")] = None,
        initiator_name: Annotated[str | None, Header(alias="X-AIOps-Initiator-Name")] = None,
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        """在一张订单上跑一次**草稿**，不产生版本、不改路由（#772 F / #781 / #782）。

        **订单可见性用与生产同一条判据**：``_order_authorization`` 拿**这次试运行的范围**
        去问 ``order_authorizer``，答案的含义只在这一处决定 —— 越权与不存在同为 404。

        范围（#782）来自**发起人的公司身份**，经请求头由桥转达；浏览器提交的租户/站点/
        用户字段一律不被读取（模型 ``extra="forbid"``，多一个就 400）。桥没给身份
        （缺 ``X-AIOps-Initiator`` 或 ``-Tenant``）⇒ 退回 #781 的固定调试租户行为。
        """
        if payload.app_id:
            from aiops_diagnostics.node_publication import target_for

            bound = next(
                (b for b in context.runtime.dify_app_registry.all() if b.app_id == payload.app_id), None
            )
            target = target_for(context.settings, bound) if bound is not None else None
            if target is not None and target.entry == "operator":
                from aiops_diagnostics import operator_cpms
                from aiops_diagnostics.dify_dsl_pull import DifyDslSource
                from aiops_diagnostics.node_publication import candidate

                session = request.headers.get("X-AIOps-Console-Session", "")

                def console_operator_identity():
                    return authenticated_operator_console(context, session)

                caller, bearer = console_operator_identity()
                binding = chat_execution.select_binding(
                    context.settings, caller, scenario=operator_cpms.SCENARIO, entry="operator"
                )
                if binding is None or binding.app_id != payload.app_id:
                    raise OperatorRefused("OPERATOR_TRIAL_SCOPE_MISMATCH")
                workspace = _workspace_for_tenant(context, caller.effective_tenant_id)
                source = DifyDslSource(
                    context.settings.dify_console_base_url,
                    payload.app_id,
                    context.settings.dify_console_api_key,
                    workspace,
                )
                manifest = candidate(source, target)

                def current_console_scope():
                    current, token = console_operator_identity()
                    if token != bearer:
                        raise OperatorRefused("OPERATOR_TRIAL_LOGIN_CHANGED")
                    return current

                qa = context.runtime.start_operator_cpms(
                    context.operator_cpms,
                    caller,
                    binding,
                    payload.order_no,
                    payload.question,
                    bearer=bearer,
                    authorize=current_console_scope,
                    base=target.cpms_base_url,
                    proxy=target.proxy_base_url,
                    conversation_turn=None,
                    language=language,
                    trial_manifest=manifest,
                )
                return {"trial_id": qa["qa_id"], "status": qa["status"]}
        trial_initiator = TrialInitiator.from_headers(
            initiator, initiator_tenant, initiator_type, initiator_name
        )
        run_context = _trial_run_context(context, trial_initiator)
        _require_visible_order(context, run_context, payload.order_no)
        try:
            # **按应用的实际类型分流**，不看"管理表是否启用"：同一个网关可以同时挂着
            # 已接管管理指针的 workflow 应用与既有 chat/completion 应用。后者没有
            # 管理行（发布都不推进指针），送进 `_workflow_trial_draft` 会得到
            # `CHAT_ROUTE_BINDING_MISMATCH` —— 把它们原本可用的试用弄坏。
            if (
                payload.app_id
                and context.settings.chat_route_bindings_file
                and _is_managed_workflow_app(context, payload.app_id)
            ):
                draft = _workflow_trial_draft(context, payload.app_id, run_context, payload.question)
            else:
                if payload.prompt is not None:
                    draft_payload = {"prompt": payload.prompt, "allowed_tools": payload.allowed_tools}
                else:
                    prompt_text, tools = _trial_draft_from_console(context, str(payload.app_id))
                    draft_payload = {"prompt": prompt_text, "allowed_tools": list(tools)}
                draft = TrialDraft.from_payload(draft_payload)
        except TrialRefused as exc:
            raise StandardAPIError(400, "TRIAL_DRAFT_REJECTED", str(exc)) from exc
        try:
            trial = context.runtime.start_trial(
                (initiator or "").strip() or "unknown",
                payload.order_no,
                draft,
                language,
                trial_initiator=trial_initiator,
            )
        except (ValueError, RuntimeError) as exc:
            raise _runtime_unavailable("TRIAL_UNAVAILABLE", "trial unavailable", exc) from exc
        if trial is None:
            raise StandardAPIError(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "TRIAL_BUSY",
                "another trial is already running",
                retryable=True,
            )
        return {"trial_id": trial["diagnosis_id"], "status": trial["status"]}

    @app.get("/v1/dify/trial/{trial_id}")
    def get_dify_trial(
        trial_id: str,
        request: Request,
        _: None = Depends(dify_trial_credential),  # noqa: B008
        initiator: Annotated[str | None, Header(alias="X-AIOps-Initiator")] = None,
        initiator_tenant: Annotated[str | None, Header(alias="X-AIOps-Initiator-Tenant")] = None,
        initiator_type: Annotated[str | None, Header(alias="X-AIOps-Initiator-Type")] = None,
        initiator_name: Annotated[str | None, Header(alias="X-AIOps-Initiator-Name")] = None,
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        """读一次试运行的状态与结果（诊断报告 + 证据 + 被挡工具 + 数据源失败）。

        **发起人头与提交路由逐字相同，而且是必须的**：作业行按 ``scope_fingerprint``
        隔离，而那个哈希含数据范围（站点集合）与主体类型 —— 按人范围启用后，提交时算出的
        指纹与 ``trial_run_context()`` 的**不一样**，于是带发起人的提交会读不回来（404）。
        两个路由走同一个 ``_trial_run_context``，这条不可能漂。
        """
        if trial_id.startswith("qa_"):
            from aiops_diagnostics import operator_cpms

            caller, bearer = authenticated_operator_console(
                context, request.headers.get("X-AIOps-Console-Session", "")
            )
            qa = context.store.get_assistant_question(trial_id, caller.scope_fingerprint)
            if qa is None or not str(qa.get("internal_run_id", "")).startswith(operator_cpms.PREFIX):
                raise StandardAPIError(404, "TRIAL_NOT_FOUND", "trial not found")
            result = None
            if qa["status"] == "completed":
                rendered = context.operator_cpms.authorize_readback(caller, trial_id, bearer)
                result = {"report": {"summary": rendered["blocks"][0]["text"]}}
            return {
                "trial_id": trial_id,
                "status": qa["status"],
                "result": result,
                "error_code": qa.get("error_code"),
            }
        trial_initiator = TrialInitiator.from_headers(
            initiator, initiator_tenant, initiator_type, initiator_name
        )
        caller = _trial_run_context(context, trial_initiator)
        trial = context.runtime.get_trial(caller, trial_id)
        if trial is None:
            raise StandardAPIError(status.HTTP_404_NOT_FOUND, "TRIAL_NOT_FOUND", "trial not found")
        body: dict[str, Any] = {
            "trial_id": trial["diagnosis_id"],
            "status": trial["status"],
            "order_no": trial.get("order_no", ""),
        }
        result = trial.get("result")
        if isinstance(result, dict):
            _require_workflow_trial_result(context, caller, trial)
            body["result"] = result
        if trial.get("error_code"):
            body["error_code"] = trial["error_code"]
            # **一句给人看的失败原因**，与诊断面共用同一张多语言表
            # （`diagnosis_error_message`）。只给 `error_code` 不够：控制台面板背后
            # 的运营看不懂 `TRIAL_FAILED`，而这条路由的结果面板正是给运营看的。
            #
            # 这里是**按请求语言渲染**的那一类文案（与诊断面同一条规则：请求语言只渲染
            # 服务端此刻写的话，不改结果本身的 `language`）。
            body["error_message"] = diagnosis_error_message(language, trial["error_code"])
        return body

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "aiops-gateway",
            "version": __version__,
            "api_version": "v1",
            "platform": platform.system().lower(),
            "business_mutations": "disabled",
        }

    @app.get("/v1/faq/recommendations")
    def faq_recommendations(
        identity: tuple[ScopeContext, Any] = Depends(faq_identity),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ):
        _, decision = identity
        return {
            **decision.public(),
            "language": context.faq_catalog.served_language(decision.platform, language),
            "faq_version": context.faq_catalog.version,
            "recommendations": context.faq_catalog.recommendations(decision.platform, language),
        }

    @app.get("/v1/faq/catalog")
    def faq_catalog(
        identity: tuple[ScopeContext, Any] = Depends(faq_identity),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ):
        _, decision = identity
        return {
            **decision.public(),
            "language": context.faq_catalog.served_language(decision.platform, language),
            "faq_version": context.faq_catalog.version,
            "entries": context.faq_catalog.catalog(decision.platform, language),
        }

    @app.post("/v1/faq/answer")
    def faq_answer(
        payload: FAQAnswerRequest,
        identity: tuple[ScopeContext, Any] = Depends(faq_identity),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ):
        _, decision = identity
        try:
            answer = context.faq_catalog.answer(decision.platform, payload.question_id, language)
        except FAQError as exc:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, FAQ_NOT_FOUND, "FAQ question was not found"
            ) from exc
        return {
            **decision.public(),
            # The language the ANSWER is in, which is the catalog's answer rather
            # than the request's: an entry without a translation is served in the
            # authority language, and reporting the requested tag beside Chinese
            # text would make the payload lie about itself.
            "language": context.faq_catalog.entry_served_language(
                decision.platform, payload.question_id, language
            ),
            "faq_version": context.faq_catalog.version,
            "question_id": answer["question_id"],
            "question": answer["question"],
            "answer": answer["answer"],
            "format": answer["format"],
        }

    @app.post("/v1/assistant/photos", status_code=201)
    async def upload_consumer_photo(request: Request, identity=Depends(assistant_identity)):  # noqa: B008
        from aiops_diagnostics.consumer_chat_service import fingerprint
        from aiops_diagnostics.consumer_photo import MAX_UPLOAD, normalize_photo

        caller, decision = identity
        if not context.settings.consumer_chatflow_enabled or str(decision.platform) != "consumer":
            raise StandardAPIError(404, "PHOTO_UNAVAILABLE", "photo input unavailable")
        _require_consumer_self_order(caller, "consumer", "pending")
        photo = ""
        completed = False
        try:
            binding = chat_execution.select_binding(context.settings, caller, scenario="consumer_service")
            if binding is None or not binding.photo_enabled:
                raise StandardAPIError(404, "PHOTO_UNAVAILABLE", "photo input unavailable")
            photo = context.runtime.consumer_photos.put(caller.scope_fingerprint, fingerprint(binding), b"")
            body = bytearray()
            try:
                async with asyncio.timeout(20):
                    async for chunk in request.stream():
                        body.extend(chunk)
                        if len(body) > MAX_UPLOAD:
                            raise StandardAPIError(413, "PHOTO_TOO_LARGE", "photo too large")
            except TimeoutError as exc:
                raise StandardAPIError(408, "PHOTO_UPLOAD_TIMEOUT", "photo upload timed out") from exc
            from starlette.concurrency import run_in_threadpool

            image = await run_in_threadpool(
                normalize_photo, bytes(body), request.headers.get("Content-Type", "").split(";")[0]
            )
            from aiops_diagnostics.consumer_chat_service import current

            current(context.runtime, caller, binding)
            context.runtime.consumer_photos.complete_upload(photo, image)
            completed = True
            return JSONResponse(
                status_code=201,
                headers={"Cache-Control": "no-store"},
                content={"photo_id": photo, "expires_in_seconds": 300},
            )
        except ValueError as exc:
            raise StandardAPIError(400, "PHOTO_INVALID", "invalid photo") from exc
        except chat_execution.ChatRunUnavailable as exc:
            raise StandardAPIError(exc.http_status, exc.code, str(exc)) from exc
        finally:
            if photo and not completed:
                context.runtime.consumer_photos.discard(photo)

    @app.post("/v1/assistant/questions")
    def assistant_questions(
        payload: AssistantQuestionRequest,
        response: Response,
        request: Request,
        identity: tuple[ScopeContext, Any] = Depends(assistant_identity),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        """Unified assistant entry point with optional order_no (T2/#152).

        Classifier routes:
          - explicit order_no        → type=diagnosis (defer to the order
            diagnosis semantics: authorize then start_standard_diagnosis)
          - FAQ keyword short-circuit → type=faq (sync, deterministic)
          - else                     → type=qa (generic zero-order answer);
            this ticket returns a queued placeholder that the T3 ticket fills
            with a real zero-order Agent job.
        """
        caller, decision = identity
        if selected_settings.consumer_order_chat_enabled:
            response.headers["Cache-Control"] = "no-store"

        # Observation, not routing: this only DECIDES whether the request is a
        # candidate for the consumer status branch, using the same predicate the
        # branch below uses. It reads values the handler already computed and
        # marks nothing when no verified caller exists (a 401 raised by the
        # identity dependency never reaches this line, so an unauthenticated
        # request can never be reported as having hit the branch).
        request_observation.classify_request(
            request,
            consumer_order_chat_enabled=selected_settings.consumer_order_chat_enabled,
            platform=str(decision.platform),
            shortcut_code=payload.shortcut_code,
            language=language,
            question=payload.question,
            is_status_question=is_order_status_question,
        )
        request_observation.mark_tenant(request, caller.effective_tenant_id)

        # Conversation binding (T4/#172): resolve BEFORE routing so every
        # branch can consult the confirmed active_order. A conversation that
        # is missing, expired, or belongs to another user/tenant/entry is a
        # uniform 404 — no existence leak.
        conversation: dict[str, Any] | None = None
        if payload.conversation_id:
            conversation = _resolve_conversation(context, caller, decision, payload.conversation_id)

        if str(decision.platform) == "operator" and payload.shortcut_code in {None, "", "smart_diagnosis"}:
            from aiops_diagnostics import operator_cpms
            from aiops_diagnostics.node_publication import targets

            try:
                binding = chat_execution.select_binding(
                    context.settings, caller, scenario=operator_cpms.SCENARIO, entry="operator"
                )
            except chat_execution.ChatRunUnavailable as exc:
                raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
            if binding is not None:
                _require_operator_company_token(request)
                order = payload.order_no or _extract_order_no(payload.question)
                if not order:
                    return _clarification(
                        context,
                        caller,
                        decision,
                        code=CLARIFY_ORDER_REQUIRED,
                        question=payload.question,
                        language=language,
                    )
                target = next(
                    (
                        r
                        for r in targets(context.settings.chat_node_targets_file)
                        if r.entry == "operator"
                        and r.app_id == binding.app_id
                        and _tenant_in_scope(r.tenant_id, caller.effective_tenant_id)
                        and r.environment == binding.environment
                    ),
                    None,
                )
                if target is None:
                    raise OperatorRefused("OPERATOR_TARGET_MISSING")
                authorization = request.headers.get("Authorization")
                source = request.headers.get(SOURCE_KEY_HEADER)

                def authenticated_operator_task():
                    authenticated = _authenticate_caller(
                        context.caller_resolver,
                        authorization,
                        None,
                        required_scope=STANDARD_DIAGNOSIS_SCOPE,
                        platform_entry="operator",
                        source_key=source,
                    )
                    return context.operator_cpms.refresh_scope(authenticated)

                turn_no = _begin_conversation_turn(context, caller, conversation, "qa", payload.question)
                try:
                    qa = context.runtime.start_operator_cpms(
                        context.operator_cpms,
                        caller,
                        binding,
                        order,
                        payload.question,
                        bearer=(authorization or "").removeprefix("Bearer "),
                        authorize=authenticated_operator_task,
                        base=target.cpms_base_url,
                        proxy=target.proxy_base_url,
                        language=language,
                        conversation_turn=(conversation["conversation_id"], caller.scope_fingerprint, turn_no)
                        if conversation is not None and turn_no is not None
                        else None,
                    )
                except Exception:
                    _release_conversation_turn(context, conversation, turn_no)
                    raise
                return JSONResponse(
                    status_code=202,
                    headers={"Cache-Control": "no-store"},
                    content={
                        **_assistant_question_response(qa, language),
                        **(
                            {"conversation_id": conversation["conversation_id"], "turn_no": turn_no}
                            if conversation
                            else {}
                        ),
                    },
                )

        if (
            selected_settings.consumer_chatflow_enabled
            and str(decision.platform) == "consumer"
            and not payload.shortcut_code
        ):
            from aiops_diagnostics import consumer_chat_service

            try:
                service_binding = chat_execution.select_binding(
                    context.settings, caller, scenario=chat_execution.SCENARIO_CONSUMER_SERVICE
                )
                if service_binding is not None:
                    from aiops_diagnostics import consumer_knowledge

                    if payload.photo_id and (not service_binding.photo_enabled or payload.order_no):
                        raise StandardAPIError(
                            400, "PHOTO_NOT_ALLOWED", "photo input is unavailable for this request"
                        )
                    _require_consumer_self_order(caller, "consumer", "pending")
                    if service_binding.content_file or service_binding.content_snapshot is not None:
                        from aiops_diagnostics import consumer_knowledge

                        local_kind = (
                            "safety"
                            if consumer_knowledge.SAFETY.search(payload.question)
                            else "human"
                            if consumer_knowledge.HUMAN.search(payload.question)
                            else ""
                        )
                        if not local_kind and conversation is not None:
                            followup = consumer_knowledge.followup_context(
                                context.runtime,
                                caller,
                                payload.question,
                                conversation["conversation_id"],
                                require_result=consumer_chat_service.require_result,
                            )
                            if followup["unresolved_count"] >= 2:
                                local_kind = "human"
                        if local_kind:
                            if payload.photo_id:
                                from aiops_diagnostics.consumer_chat_service import fingerprint

                                context.runtime.consumer_photos.take(
                                    payload.photo_id, caller.scope_fingerprint, fingerprint(service_binding)
                                )
                                context.runtime.consumer_photos.finish(payload.photo_id)
                            content = None
                            with contextlib.suppress(ValueError, OSError, PrivatePathError):
                                content = consumer_knowledge.binding_content(service_binding, caller)
                            served_language = "zh" if language.startswith("zh") else "en"
                            answer = consumer_knowledge.fallback(local_kind, served_language, content)
                            return JSONResponse(
                                status_code=200,
                                headers={"Cache-Control": "no-store"},
                                content={
                                    "type": "clarification",
                                    "missing_fields": [],
                                    "message": answer["blocks"][0]["text"],
                                    "language": served_language,
                                },
                            )
                    if not can_prompt_in(language):
                        return _clarification(
                            context,
                            caller,
                            decision,
                            code=CLARIFY_LANGUAGE_UNROUTABLE,
                            question=payload.question,
                            language=language,
                        )
                    from aiops_diagnostics import consumer_chat_service, consumer_intent

                    if payload.order_no:
                        _require_visible_order(context, caller, payload.order_no)
                    previous, previous_question = consumer_chat_service.previous_context(
                        context.runtime, caller, service_binding, conversation
                    )
                    selection = {"intent": "knowledge", "order_reference": "none", "uses_context": False}
                    if not payload.photo_id:
                        try:
                            selection = consumer_intent.validate(
                                context.runtime.classify_consumer(
                                    payload.question,
                                    language=language,
                                    previous_question=previous_question,
                                    previous_order=bool(previous and previous.order_no),
                                    selected_order=bool(payload.order_no),
                                )
                            )
                        except (ValueError, RuntimeError, OSError) as exc:
                            raise _runtime_unavailable(
                                "CONSUMER_INTENT_UNAVAILABLE",
                                consumer_intent.message("unavailable", language),
                                exc,
                            ) from exc
                    intent, reference = selection["intent"], selection["order_reference"]
                    uses_context = selection["uses_context"] or reference == "previous"
                    if previous and uses_context:
                        refreshed, _ = consumer_chat_service.previous_context(
                            context.runtime, caller, service_binding, conversation
                        )
                        if refreshed is None or refreshed.qa_id != previous.qa_id:
                            return _consumer_intent_clarification("context", language)
                        previous = refreshed
                    if intent in {"write_request", "clarify"}:
                        return _consumer_intent_clarification(intent, language)
                    if intent in {"safety", "human", "unsupported_live"}:
                        content = None
                        with contextlib.suppress(ValueError, OSError, PrivatePathError):
                            content = consumer_knowledge.binding_content(service_binding, caller)
                        answer = consumer_knowledge.fallback(
                            "realtime" if intent == "unsupported_live" else intent, language, content
                        )
                        return JSONResponse(
                            status_code=200,
                            headers={"Cache-Control": "no-store"},
                            content={
                                "type": "clarification",
                                "missing_fields": [],
                                "language": language,
                                "message": answer["blocks"][0]["text"],
                            },
                        )
                    if uses_context and previous is None:
                        return _consumer_intent_clarification("context", language)
                    observation = request_observation.observation_of(request)
                    if observation is not None:
                        observation.classify(candidate=intent == "order_status")
                        request_observation.mark_tenant(request, caller.effective_tenant_id)
                    live_order = ""
                    data_kind = "billing" if intent == "billing_record" else "status"
                    if intent in {"order_status", "billing_record"}:
                        enabled = (
                            service_binding.live_billing_record
                            if data_kind == "billing"
                            else service_binding.live_order_status
                        )
                        if not enabled:
                            return _consumer_intent_clarification("disabled", language)
                        authenticated_chat_order_reader(context, caller, request)
                        candidates = {
                            token
                            for match in _ORDER_TOKEN.finditer(payload.question)
                            if (token := _extract_order_no(match.group(0)))
                        }
                        if reference in {"unclear", "none"} or len(candidates) > 1:
                            return _consumer_intent_clarification("order", language)
                        if candidates and reference != "explicit":
                            return _consumer_intent_clarification("order", language)
                        if payload.order_no and reference != "explicit":
                            return _consumer_intent_clarification("order", language)
                        if payload.order_no and candidates and candidates != {payload.order_no}:
                            return _consumer_intent_clarification("order", language)
                        live_order = payload.order_no or (
                            next(iter(candidates), "") if reference == "explicit" else ""
                        )
                        if not live_order and reference == "latest":
                            with scoped_mysql_source(
                                Settings.from_config(context.settings.server_config_file),
                                resolve_query_scope(caller),
                            ) as source:
                                rows = source.list_orders(page=1, page_size=1)
                            if not rows:
                                raise StandardAPIError(404, "ORDER_NOT_FOUND", "order not found")
                            live_order = str(rows[0]["order_no"])
                        elif not live_order and reference == "previous" and previous:
                            live_order = previous.order_no
                        if not live_order:
                            return _consumer_intent_clarification("order", language)
                        _require_consumer_self_order(caller, "consumer", live_order)
                        _require_visible_order(context, caller, live_order)
                    if conversation is None:
                        conversation = context.conversation_store.create(
                            scope_fingerprint=caller.scope_fingerprint,
                            business_entry="consumer",
                            agent_version_key=consumer_chat_service.version_key(service_binding),
                        )
                    if conversation["agent_version_key"] != consumer_chat_service.version_key(
                        service_binding
                    ):
                        raise StandardAPIError(
                            409,
                            "CHATFLOW_NEW_CONVERSATION_REQUIRED",
                            consumer_intent.message("conversation", language),
                        )
                    turn_no = _begin_conversation_turn(context, caller, conversation, "qa", payload.question)
                    try:
                        qa = context.runtime.start_consumer_chatflow(
                            caller,
                            payload.question,
                            binding=service_binding,
                            conversation=conversation,
                            conversation_turn_no=turn_no,
                            language=language,
                            order_no=live_order,
                            data_kind=data_kind,
                            photo_id=payload.photo_id or "",
                            uses_context=uses_context,
                            expected_previous_qa_id=previous.qa_id if previous else "",
                        )
                    except Exception:
                        _release_conversation_turn(context, conversation, turn_no)
                        raise
                    request_observation.mark_qa(request, qa["qa_id"])
                    return JSONResponse(
                        status_code=202,
                        headers={"Cache-Control": "no-store"},
                        content={
                            **_assistant_question_response(qa, language),
                            "conversation_id": conversation["conversation_id"],
                            "turn_no": turn_no,
                        },
                    )
            except chat_execution.ChatRunUnavailable as exc:
                raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
            except (ValueError, OSError, sqlite3.Error, SourceError, ScopeError) as exc:
                raise _runtime_unavailable("CHATFLOW_UNAVAILABLE", "chatflow unavailable", exc) from exc

        if payload.photo_id:
            raise StandardAPIError(404, "PHOTO_UNAVAILABLE", "photo input unavailable")

        # Two server-side guards over the clicked shortcut. The listing
        # metadata is a UI hint; these guards are the boundary that keeps a
        # clicked action on its own path instead of leaking into generic QA.
        if payload.shortcut_code:
            try:
                shortcut = context.shortcut_manager.store.find_effective_by_code(
                    caller.effective_tenant_id, str(decision.platform), payload.shortcut_code
                )
            except ShortcutError:
                shortcut = None
            if shortcut is not None and shortcut.status == "published":
                # (a) A jump action is not the assistant entry's business: the
                # client was supposed to navigate. If it arrives here anyway,
                # answer plainly and create NO job. Checked before the order
                # guard and regardless of order context, because a jump action
                # must never produce an answer or a diagnosis.
                if shortcut.jump_path:
                    return _clarification(
                        context,
                        caller,
                        decision,
                        code=CLARIFY_WRONG_ENTRY,
                        question=payload.question,
                        language=language,
                    )

                # (b) A clicked order-bound shortcut must not silently fall
                # through to generic QA when the frontend omitted the order
                # context.
                #
                # The order may arrive either as the explicit ``order_no``
                # field OR embedded in the question text (the frontend sends
                # the order picker result inside the sentence, e.g. "帮我检测
                # （2099…）这个订单的充电异常"). Only when NEITHER carries a
                # candidate do we ask for one — otherwise this guard would
                # shadow Route 1b and reject a perfectly diagnosable request.
                if (
                    shortcut.requires_order
                    and not payload.order_no
                    and _extract_order_no(payload.question) is None
                ):
                    return _clarification(
                        context,
                        caller,
                        decision,
                        code=CLARIFY_ORDER_REQUIRED,
                        question=payload.question,
                        language=language,
                    )

        # A language we can RENDER but not ROUTE. Thai and Khmer have no word
        # boundaries, so `_normalize_keywords` swallows a run into pseudo-tokens
        # and the matcher stops matching: measured, an UNRELATED Thai sentence
        # scores a hit against a real entry (false positive), and a reworded one
        # does not (false negative). The high-risk order cues cannot be written
        # for these scripts at all.
        #
        # This is checked FIRST — ahead of EVERY model-backed route (explicit
        # order, embedded order, active-order follow-up, promotional, FAQ match
        # and generic QA), not just the one below it. Two reasons, both load-
        # bearing:
        #
        # 1. Those routes START JOBS. A check after them returns a diagnosis job
        #    that generates Chinese under a Thai label — exactly the silent
        #    fallback this boundary exists to prevent.
        # 2. The FAQ matcher is not SOUND for these scripts (measured: an
        #    unrelated Thai sentence hits a real entry), so letting one through
        #    would answer from a catalog entry chosen by a broken matcher.
        #
        # An explicit order still gets its authorization checked before this —
        # see the order-authorization block above — so a caller cannot use the
        # boundary to probe whether an order exists.
        #
        # Placed before every job, turn claim and model call, so a refused
        # question leaves no row.
        # An explicit order is AUTHORIZED before the language boundary, not
        # after it. Ordering these the other way changes the answer to two
        # questions that have nothing to do with language: an order the caller
        # does not own would stop being a 404 (so the boundary became a probe),
        # and an authorization outage would stop being a retryable 503. The
        # boundary answers "can this language be asked in", and it must not
        # answer "does this order exist" on the way.
        order_verdict_before_boundary = (
            _order_authorization(context, caller, payload.order_no) if payload.order_no else ""
        )
        if order_verdict_before_boundary in {UNAVAILABLE, NOT_OWNED}:
            # A candidate request refused HERE never reaches `_consumer_order_chat`,
            # so the branch's own instrumentation cannot see it. The stage says
            # which guard refused it, so a 404 on this line is distinguishable
            # from the same 404 raised inside the branch.
            request_observation.mark_stage(request, request_observation.STAGE_PRE_ROUTE_ORDER_AUTH)
            if order_verdict_before_boundary == UNAVAILABLE:
                raise StandardAPIError(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "ORDER_AUTHORIZATION_UNAVAILABLE",
                    "order authorization unavailable",
                    retryable=True,
                )
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "ORDER_NOT_FOUND",
                "order not found",
            )

        if not can_prompt_in(language):
            # This branch recorded no metric of its own before #636 — one of the
            # reasons the metric dimension could not tell the branches apart.
            return _clarification(
                context,
                caller,
                decision,
                code=CLARIFY_LANGUAGE_UNROUTABLE,
                question=payload.question,
                language=language,
            )

        # Route 0: the operations-review shortcut → an aggregation job.
        #
        # It sits AHEAD of Route 1 on purpose. A clicked action is the most
        # explicit signal a request carries, and this branch is the only thing
        # the action means; letting an order in the sentence or the
        # conversation's active order claim it first would answer a *different
        # question* than the button asked.
        #
        # The window is the SERVER's, not the client's (see
        # ``OPERATIONS_REVIEW_WINDOW_HOURS``): a window is a 口径, and a 口径
        # each client invents is one per client. Callers who need to choose one
        # keep using ``POST /v1/standard/analyses``, which takes it explicitly.
        if payload.shortcut_code == OPERATIONS_REVIEW_CODE and shortcut is not None:
            return _start_operations_review(
                context,
                caller,
                decision,
                payload,
                language=language,
            )

        # Route 1: explicit order → diagnosis semantics.
        if (
            selected_settings.consumer_order_chat_enabled
            and str(decision.platform) == "consumer"
            and language in {"zh", "zh-Hant", "en"}
            and not payload.shortcut_code
            and is_order_status_question(payload.question)
        ):
            return _consumer_order_chat(context, caller, decision, payload, conversation, language, request)

        if payload.order_no:
            # The conversation is resolved before routing so EVERY branch can
            # consult it; this one used to read none of its fields. Without the
            # claim the diagnosis is invisible to the conversation: no turn row
            # (so a follow-up cannot see it) and nothing for the frontend to
            # cancel with, because only a 202 that carries conversation_id +
            # turn_no can be cancelled.
            turn_no = _begin_conversation_turn(context, caller, conversation, "diagnosis", payload.question)
            try:
                diagnosis = context.runtime.start_standard_diagnosis(
                    caller,
                    payload.order_no,
                    payload.question,
                    None,
                    language=language,
                    conversation_turn=(
                        (conversation["conversation_id"], conversation["scope_fingerprint"], turn_no)
                        if conversation is not None and turn_no is not None
                        else None
                    ),
                    business_entry=str(decision.platform),
                )
            except (ValueError, RuntimeError) as exc:
                _release_conversation_turn(context, conversation, turn_no)
                raise _runtime_unavailable("DIAGNOSIS_UNAVAILABLE", "diagnosis unavailable", exc) from exc
            base = _standard_diagnosis_response(diagnosis, language)
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content={
                    **base,
                    "type": "diagnosis",
                    **(
                        {"conversation_id": conversation["conversation_id"], "turn_no": turn_no}
                        if conversation is not None and turn_no is not None
                        else {}
                    ),
                },
            )

        # One verdict per order per request. Routes 1b and 1c can check the same
        # number (the embedded order IS the conversation's active order), and a
        # failed check records a warning and a metric row each time — so without
        # this, one question could leave two identical failure rows. Permissions
        # are resolved once per request, so an answer cannot change mid-request.
        checked: dict[str, str] = {}

        def order_verdict(order_no: str) -> str:
            if order_no not in checked:
                checked[order_no] = _order_authorization(context, caller, order_no)
            return checked[order_no]

        # Route 1b: text-embedded order number → diagnosis if authorizable.
        # The caller did not pass order_no explicitly, but the question text
        # contains a plausible order id. Verify ownership first; only then
        # route to order diagnosis.
        #
        # When ownership FAILS the request still must not become a hard 404 and
        # must not become an existence oracle (T4/#154) — but it must not become
        # a silent zero-order answer either. It fell through to one until #620,
        # and the cost was measured: the app sent "check anomalies for order
        # (X)", X belonged to someone else, and the user got a knowledgeable
        # paragraph about how the assistant cannot verify orders. Nothing in
        # that reply said the order was not theirs, and the backend had made no
        # diagnosis attempt at all — the two states below were indistinguishable,
        # which is the failure mode this repository keeps having to remove.
        #
        # So the fall-through stays (no 404, no leak) and gains a NAMED state:
        # the same `clarification` envelope every other "the request needs
        # something it did not provide" branch uses, with a message that says
        # the true thing. `missing_fields: []` keeps its established meaning
        # from the jump-action guard — nothing is missing; the request is in the
        # wrong place — as distinct from `["order_no"]`, which means "no order at
        # all was recognisable". Neither one confirms the order exists: both
        # answers are about the REQUEST, never about the order.
        if not payload.order_no:
            embedded = _extract_order_no(payload.question)
            if embedded and order_verdict(embedded) == NOT_OWNED:
                return _clarification(
                    context,
                    caller,
                    decision,
                    code=_unowned_order_code(caller),
                    question=payload.question,
                    language=language,
                )

        if not payload.order_no:
            embedded = _extract_order_no(payload.question)
            if embedded:
                owns = order_verdict(embedded) == OWNED
                if owns:
                    turn_no = _begin_conversation_turn(
                        context, caller, conversation, "diagnosis", payload.question
                    )
                    # The slot is held until the job reaches a terminal
                    # state (the worker frees it): this branch used to free it
                    # right here, so `is_generating` reported false for the
                    # whole generation and the frontend never locked its input.
                    own_turn = (
                        conversation is not None
                        and turn_no is not None
                        and embedded == conversation.get("active_order_no")
                    )
                    try:
                        diagnosis = context.runtime.start_standard_diagnosis(
                            caller,
                            embedded,
                            payload.question,
                            None,
                            language=language,
                            conversation_turn=(
                                (conversation["conversation_id"], conversation["scope_fingerprint"], turn_no)
                                if own_turn
                                else None
                            ),
                            business_entry=str(decision.platform),
                        )
                    except (ValueError, RuntimeError) as exc:
                        _release_conversation_turn(context, conversation, turn_no)
                        raise _runtime_unavailable(
                            "DIAGNOSIS_UNAVAILABLE", "diagnosis unavailable", exc
                        ) from exc
                    if not own_turn:
                        _release_conversation_turn(context, conversation, turn_no)
                    # Only report a turn the conversation actually keeps: in
                    # the non-own case the row was just dropped, and echoing a
                    # turn_no for it would point the frontend at nothing.
                    turn_field = {"turn_no": turn_no} if own_turn else {}
                    base = _standard_diagnosis_response(diagnosis, language)
                    return JSONResponse(
                        status_code=status.HTTP_202_ACCEPTED,
                        content={
                            **base,
                            "type": "diagnosis",
                            "order_no_extracted": embedded,
                            **({"conversation_id": conversation["conversation_id"]} if conversation else {}),
                            **turn_field,
                        },
                    )

        # Route 1c (T4/#172): order question against the conversation's
        # CONFIRMED active order — the follow-up that may omit the order
        # number. Only fires when the question clearly involves that order
        # (the same explicitness bar as Route 1b); ownership is re-verified
        # EVERY turn because permissions change between requests. On any
        # failure the question falls through to qa+RAG — never a hard 404,
        # since the caller did not name an order this turn.
        if not payload.order_no and conversation is not None:
            active_order = conversation.get("active_order_no")
            if active_order and _question_involves_active_order(payload.question):
                still_owned = order_verdict(active_order) == OWNED
                if still_owned:
                    turn_no = _begin_conversation_turn(
                        context, caller, conversation, "diagnosis", payload.question
                    )
                    try:
                        diagnosis = context.runtime.start_standard_diagnosis(
                            caller,
                            active_order,
                            payload.question,
                            None,
                            language=language,
                            conversation_turn=(
                                (conversation["conversation_id"], conversation["scope_fingerprint"], turn_no)
                                if turn_no is not None
                                else None
                            ),
                            business_entry=str(decision.platform),
                        )
                    except (ValueError, RuntimeError) as exc:
                        _release_conversation_turn(context, conversation, turn_no)
                        raise _runtime_unavailable(
                            "DIAGNOSIS_UNAVAILABLE", "diagnosis unavailable", exc
                        ) from exc
                    base = _standard_diagnosis_response(diagnosis, language)
                    return JSONResponse(
                        status_code=status.HTTP_202_ACCEPTED,
                        content={
                            **base,
                            "type": "diagnosis",
                            "order_no_from_context": active_order,
                            "conversation_id": conversation["conversation_id"],
                            **({"turn_no": turn_no} if turn_no is not None else {}),
                        },
                    )
                # Ownership lost since confirmation: clear the stale binding
                # and answer as a plain knowledge question (qa+RAG).
                with contextlib.suppress(ConversationError):
                    context.conversation_store.set_active_order(
                        conversation["conversation_id"], caller.scope_fingerprint, None
                    )
                conversation = {**conversation, "active_order_no": None}

        # Route 2: promotional routing (#231) — BEFORE the FAQ short-circuit.
        # PRD #227 orders FAQ above a passive shortcut context, but a user who
        # CLICKED a product-entry button or explicitly asks for cases/solutions
        # must never land in the customer-service FAQ (#231 acceptance:
        # 案例不进入 FAQ). Only explicit promotional signals take this branch.
        promo_target, promo_intent = _promo_route(context, caller, decision, payload)
        if promo_intent is not None:
            return _start_promo_qa(
                context,
                caller,
                conversation,
                payload,
                language,
                promo_target,
                promo_intent,
                str(decision.platform),
            )

        # The routing decision, resolved at most once per request and reused by
        # whichever branch needs it (#408). ``routing_resolved`` records that it
        # was already resolved on the FAQ path, so the routing block below does
        # not pay for a second identical call.
        classified: dict[str, Any] | None = None
        routing_resolved = False

        # Route 2b: FAQ short-circuit (deterministic, zero-order, zero-model).
        #
        # A keyword match that only OVERLAPS a title is not yet an answer (#408).
        # The scoring below can only see token overlap, and a short question has
        # few tokens, so one or two generic words reach full containment against
        # some title — "今天天气怎么样" scored 1.0 against the summer-heat entry and
        # answered a charging question to someone who asked about the weather.
        # Overlap cannot separate that from a real hit: "充电桩" (a genuine
        # question) and "拔不出枪" (a genuine question) and "天气" (not one) all
        # reach 1.0.
        #
        # So the decision the score cannot make is made by the thing that can —
        # the routing intent, which comes from Jev. A question that REPRODUCES a
        # catalog title does not ask: it answers from the catalog with no model
        # call, which is what keeps every typed entry on the instant path. What
        # separates "reproduces" from "overlaps", and why it is not a length bar,
        # is argued at the note above `_classify_for_routing`.
        #
        # It costs nothing extra. This branch runs BEFORE the routing block
        # further down, and either the FAQ branch answers this request or that
        # block resolves the identical decision for it — so a short question is
        # not an additional model call, it is the same one moved earlier.
        #
        # The decision is resolved ONCE for the whole request and carried. The
        # routing block below must not resolve it again: a second call would be
        # paid by every short question the FAQ branch does NOT suppress, i.e.
        # exactly the safe path, and this upstream is metered per day.
        faq_id, faq_self_evident = _faq_match(decision.platform, context.faq_catalog, payload.question)
        if faq_id is not None and not faq_self_evident:
            classified = _classify_for_routing(
                context, payload.question, language=language, tenant_id=caller.effective_tenant_id
            )
            routing_resolved = True
            if classified is not None and classified.get("intent") in _FAQ_SUPPRESSING_INTENTS:
                # Not this entry's question, or not an FAQ-shaped question at
                # all. Fall through to the routing below, which serves it on the
                # path its intent names — a general answer for chit-chat and
                # knowledge, the promotional card for case exploration.
                faq_id = None
            # When there is no second opinion, the keyword answer stands —
            # deliberately, and NOT because it is more likely to be right.
            #
            # Suppressing on "no decision" would make an unavailable routing
            # hint change what a user is told, and it would do so on exactly the
            # deployments that have no classifier configured — the FAQ path
            # would vanish from them wholesale. This release already paid for
            # that mistake once: the #392 cutover let an unconfigured Jev return
            # "no decision" and silently disabled casual handling, the
            # promotional intents and the high-risk rule everywhere it was not
            # configured (#398). "No decision" must not be a behaviour change.
            #
            # The cost is bounded and stated rather than hidden: while the
            # routing decision is unavailable, the misroute this ticket fixes
            # can still happen. That is the pre-existing behaviour, not a new
            # defect, and it is the recoverable direction of the two.
        if faq_id is not None:
            answer = context.faq_catalog.answer(decision.platform, faq_id, language)
            _record_route_metric(context, caller, route_type="faq", outcome="completed")
            return {
                **decision.public(),
                "type": "faq",
                "language": context.faq_catalog.entry_served_language(decision.platform, faq_id, language),
                "faq_version": context.faq_catalog.version,
                "question_id": answer["question_id"],
                "question": answer["question"],
                "answer": answer["answer"],
                "format": answer["format"],
            }

        # A money question needs a business identifier before it can be
        # answered — but which identifier, and whether the question is one at
        # all, is decided in ONE place below, after the classifier has run.
        #
        # It used to be decided here, synchronously, by a keyword table: that
        # answered faster and it could ask for `order_no`, which is the more
        # actionable of the two. It also meant the same question could be asked
        # twice in two shapes (this table first, then the model), and that which
        # shape a user met depended on a hand-maintained regex. `money_question_context`
        # keeps the table as the fast path and makes the model its fallback.
        #
        # The two halves of the merged rule do NOT have the same applicability,
        # and treating them as one condition silently dropped the model's half:
        #
        # * the keyword table is a SYNCHRONOUS fast path. It runs before the
        #   classifier and before the conversation branches, so it must not
        #   pre-empt them — that was its original scope, and it stays.
        # * the model's risk label is a judgement about the QUESTION, applied
        #   after every other branch has declined it. It used to fire on any
        #   high-risk question, conversation or not.
        #
        # Gating both on "no conversation" re-introduced the defect in the other
        # direction: a `refund` asked inside a conversation, or one naming an
        # order the caller does not own, went to a plain answer with no prompt at
        # all — where the model's half used to ask.
        keyword_guard_applies = conversation is None and _extract_order_no(payload.question) is None

        casual_job = False
        if not routing_resolved:
            classified = _classify_for_routing(
                context, payload.question, language=language, tenant_id=caller.effective_tenant_id
            )
        # One decision, one shape. The table is consulted first (no model call),
        # the model covers what it misses (86-question corpus: table 3, model
        # adds 3 more), and whichever fired decides the identifier — so a user
        # never meets two different prompts for one question.
        missing = money_question_context(
            payload.question,
            classified,
            keyword_guard=lambda q: keyword_guard_applies and _missing_order_context_key(q) is not None,
            thresholds=getattr(context.runtime, "routing_thresholds", None),
        )
        if missing is not None:
            # The table's two identifiers ARE the two codes' only difference.
            code = CLARIFY_ORDER_REQUIRED if missing == MISSING_ORDER_NO else CLARIFY_CONTEXT_REQUIRED
            return _clarification(
                context,
                caller,
                decision,
                code=code,
                question=payload.question,
                language=language,
            )

        if classified is not None:
            if classified.get("intent") == "casual":
                # Chit-chat is answered by the same zero-order QA job every other
                # general question uses, rather than by the classifier writing a
                # sentence inline (#391). Two reasons, and the second is why this
                # is required rather than tidy:
                #
                # 1. The answer travels one path, so the language guard, the
                #    localized failure copy and the conversation turn all apply
                #    to it exactly as they do everywhere else.
                # 2. Typed-decision routing (PRD #383) cannot produce an answer at
                #    all — it returns labels and probabilities, never text. With
                #    the answer here, moving routing off generated text would have
                #    removed chit-chat replies entirely.
                #
                # Effect on the waiting-state contract: `casual` used to be a
                # synchronous 200 and did NOT lock the input box; it is now a 202
                # like any other `qa` and does. The handoff docs were updated with
                # this change.
                casual_job = True
            # Classifier-returned promotional intent (#229 protocol): same
            # promotional route as the explicit cues, resolved against the
            # caller's own published shortcut rows.
            if classified and classified.get("intent") in {"case_exploration", "solution_discovery"}:
                promo_target, promo_intent = _promo_route(
                    context,
                    caller,
                    decision,
                    payload,
                    forced_intent=str(classified.get("intent")),
                )
                return _start_promo_qa(
                    context,
                    caller,
                    conversation,
                    payload,
                    language,
                    promo_target,
                    promo_intent,
                    str(decision.platform),
                )

        # Route 3: generic zero-order answer — start a real QA job (T3/#153).
        # With a conversation: claim its generation slot first (409 busy),
        # and record the turn so follow-ups see it in context.
        turn_no: int | None = None
        if conversation is not None:
            turn_no = _begin_conversation_turn(context, caller, conversation, "qa", payload.question)
        try:
            qa = context.runtime.start_assistant_qa(
                caller,
                payload.question,
                conversation=conversation if turn_no is not None else None,
                conversation_turn_no=turn_no,
                language=language,
                skip_retrieval=casual_job,
                business_entry=str(decision.platform),
            )
        except (ValueError, RuntimeError) as exc:
            _release_conversation_turn(context, conversation, turn_no)
            raise _runtime_unavailable("QA_UNAVAILABLE", "general answer unavailable", exc) from exc
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                **_assistant_question_response(qa, language),
                **(
                    {"conversation_id": conversation["conversation_id"], "turn_no": turn_no}
                    if conversation
                    else {}
                ),
            },
        )

    @app.get("/v1/assistant/questions/{qa_id}")
    def get_assistant_question(
        qa_id: str,
        response: Response,
        request: Request,
        identity: tuple[ScopeContext, Any] = Depends(assistant_identity),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        caller, _ = identity
        try:
            qa = context.runtime.get_assistant_qa(caller, qa_id)
        except (ValueError, RuntimeError) as exc:
            raise _runtime_unavailable("QA_UNAVAILABLE", "general answer unavailable", exc) from exc
        if qa is None:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "QA_NOT_FOUND",
                "assistant question not found",
            )
        _require_current_chat_order(context, caller, qa, request)
        response.headers["Cache-Control"] = "no-store"
        return _assistant_question_response(qa, language)

    @app.post("/v1/assistant/questions/{qa_id}/cancel")
    def cancel_assistant_question(
        qa_id: str,
        response: Response,
        request: Request,
        identity: tuple[ScopeContext, Any] = Depends(assistant_identity),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        """Stop one in-flight general question (#357).

        POST rather than DELETE: the job row survives as `cancelled` (audit and
        history), and DELETE would promise the resource is gone. The response
        carries the job's terminal state, so the caller does not have to poll
        once more to learn the outcome — and a repeated cancel, or a cancel of
        a job that just finished, answers that job's own current state instead
        of an error, because the stop button is pressed under flaky networks.
        """
        caller, _ = identity
        try:
            qa = context.runtime.cancel_assistant_qa(caller, qa_id)
        except (ValueError, RuntimeError) as exc:
            raise _runtime_unavailable("QA_UNAVAILABLE", "general answer unavailable", exc) from exc
        if qa is None:
            # Missing and out-of-scope are the same answer: cancelling must not
            # become a way to probe which qa_ids exist.
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "QA_NOT_FOUND",
                "assistant question not found",
            )
        _require_current_chat_order(context, caller, qa, request)
        response.headers["Cache-Control"] = "no-store"
        return _assistant_question_response(qa, language)

    @app.get("/v1/assistant/questions")
    def list_assistant_questions(
        response: Response,
        request: Request,
        identity: tuple[ScopeContext, Any] = Depends(assistant_identity),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """List this caller's general-question history (T5/#155).

        Kept separate from /v1/standard/diagnoses (order diagnostics): the
        QA history contains only zero-order answers, each typed `qa`.
        """
        caller, _ = identity
        try:
            questions = context.runtime.list_assistant_qa(caller, limit=limit)
        except (ValueError, RuntimeError) as exc:
            raise _runtime_unavailable("QA_UNAVAILABLE", "general answer unavailable", exc) from exc
        from aiops_diagnostics.operator_cpms import PREFIX, OperatorRefused

        for qa in questions:
            if str(qa.get("internal_run_id") or "").startswith(PREFIX):
                try:
                    _require_current_chat_order(context, caller, qa, request)
                except (OperatorRefused, chat_execution.ChatRunUnavailable):
                    qa["result"] = None
        response.headers["Cache-Control"] = "no-store"
        return {
            "type": "qa_list",
            "language": language,
            "count": len(questions),
            "questions": questions,
        }

    # ------------------------------------------------------------------
    # Conversations (T4/#172)
    # ------------------------------------------------------------------

    @app.get("/v1/media/{signed_id}")
    def get_media(
        signed_id: str,
        range_header: Annotated[str | None, Header(alias="Range")] = None,
    ) -> Response:
        """Serve a signed media resource (#168 media protocol, T3/#170 data plane).

        No Authorization dependency: browsers load media through <img>/<video>
        tags that cannot attach Bearer headers, so the short-lived HMAC in the
        URL is the credential. The runtime re-validates the grant's tenant /
        agent-version / KB-binding scope and its liveness on every hit. Media
        plane unconfigured or any scope mismatch is a uniform 404 with no body
        detail — no existence leak (#170 acceptance).
        """
        response = context.runtime.serve_media(signed_id, range_header=range_header)
        if response is None:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "MEDIA_NOT_FOUND",
                "media resource not found",
            )
        return Response(
            status_code=response.status_code,
            headers=response.headers,
            content=response.body,
        )

    @app.post("/v1/conversations")
    def create_conversation(
        payload: ConversationCreateRequest,
        identity: tuple[ScopeContext, Any] = Depends(assistant_identity),  # noqa: B008
    ) -> dict[str, Any]:
        """Create a conversation bound to this caller, entry, and agent."""
        caller, decision = identity
        conversation = context.conversation_store.create(
            scope_fingerprint=caller.scope_fingerprint,
            business_entry=str(decision.platform),
            agent_version_key=payload.agent_version_key,
        )
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content=_conversation_response(conversation),
        )

    @app.get("/v1/conversations")
    def list_conversations(
        identity: tuple[ScopeContext, Any] = Depends(assistant_identity),  # noqa: B008
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        caller, _ = identity
        conversations = context.conversation_store.list(caller.scope_fingerprint, limit=limit)
        return {
            "type": "conversation_list",
            "count": len(conversations),
            "conversations": [_conversation_response(item) for item in conversations],
        }

    @app.get("/v1/conversations/{conversation_id}")
    def get_conversation(
        conversation_id: str,
        response: Response,
        request: Request,
        identity: tuple[ScopeContext, Any] = Depends(assistant_identity),  # noqa: B008
    ) -> dict[str, Any]:
        """Conversation detail including the full turn history (answers
        included only for finished turns — the caller re-polls job ids for
        in-flight ones)."""
        caller, decision = identity
        conversation = _require_conversation(context, caller, conversation_id)
        if conversation.get("business_entry") != str(decision.platform):
            # Entry is part of the conversation binding (T4/#172): reading
            # from a different entry is a uniform 404.
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "CONVERSATION_NOT_FOUND",
                "conversation not found",
            )
        history = context.conversation_store.turns(conversation["conversation_id"], caller.scope_fingerprint)
        for item in history:
            if item.get("status_qa_id"):
                qa = context.store.get_assistant_question(item["status_qa_id"], caller.scope_fingerprint)
                if qa is None:
                    raise StandardAPIError(404, "QA_NOT_FOUND", "assistant question not found")
                _require_current_chat_order(context, caller, qa, request)
                if item["answer"] != qa["result"]:
                    raise StandardAPIError(503, "CHARGING_STATUS_EVIDENCE_UNAVAILABLE", "evidence differs")
        response.headers["Cache-Control"] = "no-store"
        return {
            **_conversation_response(conversation),
            "turns": [
                {
                    "turn_no": item["turn_no"],
                    "kind": item["kind"],
                    "question": item["question"],
                    "answer": item["answer"],
                    "created_at": item["created_at"],
                }
                for item in history
            ],
        }

    @app.delete("/v1/conversations/{conversation_id}")
    def delete_conversation(
        conversation_id: str,
        identity: tuple[ScopeContext, Any] = Depends(assistant_identity),  # noqa: B008
    ) -> dict[str, Any]:
        caller, decision = identity
        conversation = _require_conversation(context, caller, conversation_id)
        if conversation.get("business_entry") != str(decision.platform):
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "CONVERSATION_NOT_FOUND",
                "conversation not found",
            )
        context.conversation_store.delete(conversation_id, caller.scope_fingerprint)
        return {"type": "conversation_deleted", "conversation_id": conversation_id}

    @app.post("/v1/conversations/{conversation_id}/active-order")
    def set_conversation_active_order(
        conversation_id: str,
        order_no: str | None = None,
        body: dict[str, Any] | None = None,
        identity: tuple[ScopeContext, Any] = Depends(assistant_identity),  # noqa: B008
    ) -> dict[str, Any]:
        """Bind (or clear) the conversation's confirmed active order.

        The order must be owned by the caller — ownership is re-verified HERE,
        every bind; chat text can never replace this check (#172).
        """
        caller, decision = identity
        conversation = _require_conversation(context, caller, conversation_id)
        if conversation.get("business_entry") != str(decision.platform):
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "CONVERSATION_NOT_FOUND",
                "conversation not found",
            )
        if body is None:
            body = {}
        candidate = body.get("order_no", order_no)
        if candidate is None:
            conversation = context.conversation_store.set_active_order(
                conversation_id, caller.scope_fingerprint, None
            )
            return _conversation_response(conversation)
        candidate = str(candidate)
        if not SAFE_ORDER_NO.fullmatch(candidate):
            raise StandardAPIError(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "INVALID_REQUEST",
                "order_no is invalid",
            )
        verdict = _order_authorization(context, caller, candidate)
        if verdict == UNAVAILABLE:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "ORDER_AUTHORIZATION_UNAVAILABLE",
                "order authorization unavailable",
                retryable=True,
            )
        if verdict == NOT_OWNED:
            # Unowned order: uniform 404, never an ownership oracle.
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "ORDER_NOT_FOUND",
                "order not found",
            )
        conversation = context.conversation_store.set_active_order(
            conversation_id, caller.scope_fingerprint, candidate
        )
        return _conversation_response(conversation)

    @app.get("/v1/orders/{order_no}/access")
    def order_access(
        order_no: str,
        caller: ScopeContext = Depends(authenticated_caller),  # noqa: B008
    ) -> dict[str, Any]:
        if not SAFE_ORDER_NO.fullmatch(order_no):
            raise StandardAPIError(
                status.HTTP_400_BAD_REQUEST,
                "INVALID_ORDER_NO",
                "invalid order number",
            )
        verdict = _order_authorization(context, caller, order_no)
        if verdict == UNAVAILABLE:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "ORDER_AUTHORIZATION_UNAVAILABLE",
                "order authorization unavailable",
                retryable=True,
            )
        if verdict == NOT_OWNED:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "ORDER_NOT_FOUND",
                "order not found",
            )
        return {
            "order_no": order_no,
            "accessible": True,
            "scope_fingerprint": caller.scope_fingerprint,
        }

    @app.get("/v1/orders/{order_no}/charging-status")
    def charging_status(
        order_no: str,
        response: Response,
        caller: ScopeContext = Depends(authenticated_caller),  # noqa: B008
        business_entry: Annotated[str | None, Header(alias="X-Business-Entry")] = None,
    ) -> dict[str, Any]:
        # 业务快照是**按人**的授权结果，不是公共资源。RFC 9111 §5.2.2.5 的 no-store
        # 让它不进入任何共享缓存；这条声明不替代 TLS 与授权，只是不让中间层多存一份。
        response.headers["Cache-Control"] = "no-store"
        if not selected_settings.charging_status_base_url:
            raise StandardAPIError(404, "CHARGING_STATUS_DISABLED", "route not found")
        _require_consumer_self_order(caller, business_entry, order_no)
        _require_visible_order(context, caller, order_no)
        return _charged_snapshot_response(selected_settings, context, caller, order_no)

    @app.post("/v1/orders/{order_no}/charging-status/delegated")
    def delegated_charging_status(
        order_no: str,
        payload: ChargingStatusDelegationRequest,
        response: Response,
        caller: ScopeContext = Depends(authenticated_caller),  # noqa: B008
        business_entry: Annotated[str | None, Header(alias="X-Business-Entry")] = None,
    ) -> dict[str, Any]:
        """把一次**已授权**的读取交给可信执行节点跑（M2-CLOSE-01 / 契约修订 A）。

        这条路与上面的直读共用同一组判据与同一段取数代码；差别只在「谁来读」。
        授权记录留在本进程，平台与应用/节点绑定由服务端配置决定，客户端提交不了。
        """
        del payload  # 空的严格模型：请求体里多一个字段就是 422，没有任何字段参与决策。
        response.headers["Cache-Control"] = "no-store"
        if not selected_settings.agent_run_config_file:
            raise StandardAPIError(404, "AGENT_RUN_DISABLED", "route not found")
        _require_consumer_self_order(caller, business_entry, order_no)
        _require_visible_order(context, caller, order_no)
        try:
            config = AgentRunConfig.load(Path(selected_settings.agent_run_config_file))
        except AgentRunConfigError as exc:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "AGENT_RUN_CONFIG_UNAVAILABLE",
                "agent run unavailable",
                retryable=True,
            ) from exc
        if config.environment != selected_settings.execution_environment:
            # 绑定表说的是另一个环境：宁可不跑，也不把请求派到对不上的地方。
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "AGENT_RUN_ENVIRONMENT_MISMATCH",
                "agent run unavailable",
                retryable=True,
            )
        # 每次签发重读清单；已有 run 继续使用签发时固定的身份，不回填。
        try:
            manifest = execution_manifest.load(selected_settings.execution_manifest_file)
        except execution_manifest.ExecutionManifestError as exc:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "EXEC_CONFIG_UNAVAILABLE",
                "agent run unavailable",
                retryable=True,
            ) from exc
        run = context.agent_run_store.issue(
            scope=caller,
            order_no=order_no,
            config=config,
            gateway_version=__version__,
            expected_agent_version=(manifest.identity if manifest is not None else None),
        )
        try:
            SnailDispatcher().dispatch(config, run.run_id)
        except AgentRunDispatchError as exc:
            context.agent_run_store.fail(run.run_id, "platform_unavailable")
            raise StandardAPIError(
                status.HTTP_502_BAD_GATEWAY, "AGENT_RUN_UNAVAILABLE", "agent run unavailable"
            ) from exc
        try:
            settled = context.agent_run_store.get(run.run_id)
        except (ValueError, TypeError) as exc:
            raise StandardAPIError(502, "AGENT_RUN_NO_RESULT", "agent run produced no result") from exc
        if settled is None:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "AGENT_RUN_UNAVAILABLE",
                "agent run unavailable",
                retryable=True,
            )
        if settled.state != SUCCEEDED:
            # 平台回了话但节点没有交回结果（工具没跑、被拒、上游失败）。
            # 这里**不**用平台自报的文本作答，也不把预期节点填进回执。
            raise StandardAPIError(
                status.HTTP_502_BAD_GATEWAY, "AGENT_RUN_NO_RESULT", "agent run produced no result"
            )
        return _consumer_charging_result(selected_settings, context, caller, run)

    @app.post("/v1/internal/charging-status/claims/{run_id}")
    def claim_charging_status(
        run_id: str,
        payload: NodeClaimRequest,
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
        node_build: Annotated[str | None, Header(alias="X-AI-Ops-Node-Build")] = None,
        exec_config: Annotated[str | None, Header(alias="X-AI-Ops-Exec-Config")] = None,
    ) -> dict[str, Any]:
        """节点领取端点：节点凭据决定身份，**自报字段不参与判定**。

        顺序是硬的：认节点 → 核对记录/环境/节点 → **再执行一次订单授权** → 才
        允许读业务或返回已有结果。请求体是一个空的严格模型：任何覆盖身份或业务范围的
        字段都是 422，而不是被忽略（契约 §3.5）。

        **放行是一个单独判定，三条路径共用**（R1-1）：新读一次、成功快照的重试、
        以及领取竞争失败后读到的那份成功结果，都要在**交出去之前**重新核对
        「记录仍有效 / 节点此刻仍有效 / 环境绑定未变」，否则撤销与节点停用会在这三条
        路径上各漏一次。
        """
        del payload  # 同上：请求体不参与任何决策
        response.headers["Cache-Control"] = "no-store"
        existing = context.agent_run_store.peek(run_id)
        if existing is not None and existing.operation == "operator_order_diagnosis":
            raise StandardAPIError(403, "AGENT_RUN_OPERATION_MISMATCH", "agent run refused")
        # APP-RUNTIME-01：回调该用哪份配置，由**这条 run 自己**决定（聊天入口签发的用聊天
        # 绑定，委派入口签发的用委派绑定）。原来只看委派那个键，于是「只开聊天绑定」时
        # 节点永远领不到；两份配置不同时还会拿别人的绑定去认证。
        binding_config = chat_execution.callback_config(context.agent_run_store, run_id, selected_settings)
        if binding_config is None:
            raise StandardAPIError(404, "AGENT_RUN_DISABLED", "route not found")
        token = (authorization or "").removeprefix("Bearer ").strip()

        def load_config() -> AgentRunConfig:
            # 每次领取（含最后一次放行）都重读：撤销节点就是改这个文件，下一次判定立刻生效。
            try:
                resolved = chat_execution.callback_config(context.agent_run_store, run_id, selected_settings)
                if resolved is None:
                    raise AgentRunConfigError("binding is not configured")
                return resolved
            except AgentRunConfigError as exc:
                raise StandardAPIError(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "AGENT_RUN_CONFIG_UNAVAILABLE",
                    "agent run unavailable",
                    retryable=True,
                ) from exc

        def is_releasable(record: AgentRun | None) -> bool:
            """此刻能不能把快照交出去。

            ``succeeded`` 是旧模式与「整轮已确认」的成功态；启用清单的新模式下，
            **``running`` + 已有快照**同样可以放行 —— 那是节点刚领到的业务正文，
            整轮是否走完由后续的完成通知决定，不在这里抢答。
            """
            if record is None:
                return False
            if record.state == SUCCEEDED:
                return True
            return (
                record.state == RUNNING
                and record.snapshot is not None
                and record.expected_agent_version is not None
            )

        def deny_release(run_id_: str, reason: str) -> None:
            """放行复核失败：把记录钉成终态。**只能钉自己这条记录**（凭据已认证之后才调用）。

            ``fail()`` 覆盖 issued/running（含新模式「已有快照但尚未确认整轮」那条），
            ``refuse()`` 覆盖 succeeded。两者都是真实终态：整轮不可能再被确认成功。
            """
            if not context.agent_run_store.fail(run_id_, reason):
                context.agent_run_store.refuse(run_id_, reason)

        def release_candidate(run_id_: str) -> AgentRun:
            """最后的放行判定。**只在把业务正文交出去之前调用。**

            顺序是刻意的（R2-1）：**先**完成可能阻塞的外部授权，**再**读配置与记录做本地判定。
            反过来的话，授权器返回之后我们手里握着的仍是授权**之前**读到的那份快照 ——
            在授权期间撤销、到期或停用节点，全都会被漏掉（实测三次都是 200 + 业务正文）。

            授权之后的三项判定：节点凭据此刻仍有效、节点/环境与记录的绑定仍相符、
            记录仍是 ``succeeded`` 且未过期。任一不成立就 ``refuse()`` 或抛错，绝不返回快照。
            适用边界：此刻之前已发生的撤销/过期/停用一定拒绝；已经发出去的数据不追溯撤回，
            也不跨外部授权源做分布式事务。
            """
            # 1) 可能阻塞的外部授权先做完。用授权前加载的记录只为取「要授权哪张单、哪个身份」。
            before = context.agent_run_store.get(run_id_)
            if before is None:
                raise StandardAPIError(
                    status.HTTP_404_NOT_FOUND, "AGENT_RUN_NOT_FOUND", "agent run not found"
                )
            if not is_releasable(before):
                raise StandardAPIError(
                    status.HTTP_403_FORBIDDEN, "AGENT_RUN_NOT_ACTIVE", "agent run is not active"
                )
            verdict = _order_authorization(context, scope_from_json(before.scope_json), before.order_no)
            if verdict == UNAVAILABLE:
                deny_release(run_id_, "authorization_unavailable")
                raise StandardAPIError(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "ORDER_AUTHORIZATION_UNAVAILABLE",
                    "order authorization unavailable",
                    retryable=True,
                )
            if verdict == NOT_OWNED:
                deny_release(run_id_, "order_not_visible")
                raise StandardAPIError(
                    status.HTTP_403_FORBIDDEN, "AGENT_RUN_ORDER_NOT_VISIBLE", "agent run refused"
                )

            # 2) 授权返回之后**重读**配置与记录：这是唯一被允许据以释放的对象。
            current = load_config()
            current_node = current.authenticate_node(token)
            if current_node is None:
                deny_release(run_id_, "node_deactivated")
                raise StandardAPIError(
                    status.HTTP_403_FORBIDDEN, "AGENT_RUN_NODE_INACTIVE", "agent run refused"
                )
            latest = context.agent_run_store.get(run_id_)
            if latest is None:
                raise StandardAPIError(
                    status.HTTP_404_NOT_FOUND, "AGENT_RUN_NOT_FOUND", "agent run not found"
                )
            # 读记录时已把过期的非终态收敛为 expired，所以这里涵盖「授权期间到期」。
            if not is_releasable(latest):
                raise StandardAPIError(
                    status.HTTP_403_FORBIDDEN, "AGENT_RUN_NOT_ACTIVE", "agent run is not active"
                )
            # 绑定复核必须比对**配置里的预期节点**，而不只是「出示凭据的那个节点」。
            # 只比后者时，把 expected_node_id 从 node-a1 改成 node-a2 之后，旧节点仍然
            # 与记录的 expected 相等 —— 实测就是这条漏检让旧节点拿到了 200 + 业务正文。
            if (
                current_node.node_id != latest.expected_node_id
                or current.expected_node_id != latest.expected_node_id
                or current_node.environment != latest.environment
                or current.environment != latest.environment
                or current.snail_app_id != latest.app_id
                or str(current.snail_agent_id) != latest.agent_id
            ):
                deny_release(run_id_, "binding_changed")
                raise StandardAPIError(status.HTTP_403_FORBIDDEN, "AGENT_RUN_WRONG_NODE", "agent run refused")
            released = _require_matching_exec_config(
                context, latest, exec_config, actual_node_id=current_node.node_id
            )
            # 身份比较、记录写入、以及用于放行的状态必须来自**同一次**事务读到的对象；
            # 早先返回的是授权前的 ``latest``，核对期间别人把它钉成终态就会被漏掉。
            if not is_releasable(released):
                raise StandardAPIError(
                    status.HTTP_403_FORBIDDEN, "AGENT_RUN_NOT_ACTIVE", "agent run is not active"
                )
            return released

        config = load_config()
        node = config.authenticate_node(token)
        if node is None:
            raise StandardAPIError(
                status.HTTP_401_UNAUTHORIZED, "NODE_TOKEN_REQUIRED", "node credential required"
            )
        run = context.agent_run_store.get(run_id)
        if run is None:
            raise StandardAPIError(status.HTTP_404_NOT_FOUND, "AGENT_RUN_NOT_FOUND", "agent run not found")
        if run.environment != config.environment:
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_RUN_WRONG_ENVIRONMENT", "agent run refused"
            )
        if node.environment != run.environment or node.node_id != run.expected_node_id:
            # 同环境的另一个节点也领不到：比的是节点，不只是环境。
            raise StandardAPIError(status.HTTP_403_FORBIDDEN, "AGENT_RUN_WRONG_NODE", "agent run refused")
        if run.state in {EXPIRED, REVOKED, FAILED}:
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_RUN_NOT_ACTIVE", "agent run is not active"
            )
        # 核对并落库；用**核对之后**读到的记录继续走后面的判定（它可能已经是终态）。
        run = _require_matching_exec_config(context, run, exec_config, actual_node_id=node.node_id)
        if run.state in {EXPIRED, REVOKED, FAILED}:
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_RUN_NOT_ACTIVE", "agent run is not active"
            )

        # 用记录里冻结的身份**再授权一次**。授权在这期间变化（订单不可见 / 授权源故障）
        # 时不放行 —— 包括「已有成功快照」的那条路：不释放旧记录的结果（IC-09）。
        try:
            frozen = scope_from_json(run.scope_json)
        except (ValueError, TypeError) as exc:
            context.agent_run_store.fail(run_id, "scope_unreadable", actual_node_id=node.node_id)
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_RUN_SCOPE_INVALID", "agent run refused"
            ) from exc

        def deny(reason: str) -> None:
            """预授权失败要把记录钉成终态 —— 但只能钉自己这条记录。

            ``fail()`` 只作用于 issued/running，所以对一条**已有成功快照**的记录它什么也不做：
            实测授权变更 → 403 之后记录仍是 succeeded，恢复授权后同一个 run 又能返回 200。
            对 succeeded 用 ``refuse()``（放行复核失败的专用终态），其余用 ``fail()``。

            只在节点凭据已经认过之后调用，所以「拿错凭据乱试」不会破坏别人的记录。
            """
            # 授权期间另一个领取者可能已经 complete，不能依据授权前的 run.state 分流。
            if not context.agent_run_store.fail(run_id, reason, actual_node_id=node.node_id):
                context.agent_run_store.refuse(run_id, reason)

        verdict = _order_authorization(context, frozen, run.order_no)
        if verdict == UNAVAILABLE:
            deny("authorization_unavailable")
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "ORDER_AUTHORIZATION_UNAVAILABLE",
                "order authorization unavailable",
                retryable=True,
            )
        if verdict == NOT_OWNED:
            deny("order_not_visible")
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_RUN_ORDER_NOT_VISIBLE", "agent run refused"
            )

        if run.state == SUCCEEDED or (
            run.state == RUNNING and run.snapshot is not None and run.expected_agent_version is not None
        ):
            # 有效期内重试：交回**同一份**快照，不再读一次业务（IC-05）。
            # 新模式（启用清单）下「已取数、等待整轮确认」同样是 running + 快照，
            # 重试必须拿到同一份，否则等于第二次读业务。
            # 但必须先过放行判定 —— 快照存在不等于此刻仍可交出。
            return _claim_payload(release_candidate(run_id))
        if not context.agent_run_store.claim(run_id):
            latest = context.agent_run_store.get(run_id)
            if is_releasable(latest):
                # 并发者已经取到数（旧模式 succeeded / 新模式 running+快照）：
                # 走同一条放行判定，而不是直接返回它。
                return _claim_payload(release_candidate(run_id))
            raise StandardAPIError(
                status.HTTP_409_CONFLICT,
                "AGENT_RUN_IN_PROGRESS",
                "agent run is already being executed",
                retryable=True,
            )
        try:
            snapshot = _authorized_charging_snapshot(selected_settings, frozen, run.order_no)
        except ChargingStatusError as exc:
            context.agent_run_store.fail(run_id, "upstream_failed", actual_node_id=node.node_id)
            raise StandardAPIError(
                status.HTTP_502_BAD_GATEWAY, "CHARGING_STATUS_UNAVAILABLE", "charging status unavailable"
            ) from exc
        if not context.agent_run_store.complete(
            run_id,
            snapshot=snapshot,
            actual_node_id=node.node_id,
            node_build=(node_build or "")[:128] or None,
        ):
            # 读取期间被撤销或已过期：结果不落库、也不返回。
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_RUN_NOT_ACTIVE", "agent run is not active"
            )
        # 落库之后、交出去之前再判一次：撤销/停用可能就发生在这一次读取期间。
        return _claim_payload(release_candidate(run_id))

    @app.post("/v1/internal/charging-status/execution-config/{run_id}")
    def node_execution_config(
        run_id: str, response: Response, authorization: Annotated[str | None, Header()] = None
    ):
        """已认证目标节点取得本次冻结清单；不交订单数据或供应商凭据。"""
        response.headers["Cache-Control"] = "no-store"
        run = context.agent_run_store.get(run_id)
        if run is not None and run.operation == "operator_order_diagnosis":
            return context.operator_cpms.publication(run_id, (authorization or "").removeprefix("Bearer "))
        if run is None or not run.execution_manifest:
            raise StandardAPIError(404, "NODE_PUBLICATION_NOT_FOUND", "node publication unavailable")
        config = chat_execution.callback_config(context.agent_run_store, run_id, context.settings)
        token = (authorization or "").removeprefix("Bearer ").strip()
        node = config.authenticate_node(token) if config else None
        if node is None or node.node_id != run.expected_node_id or node.environment != run.environment:
            raise StandardAPIError(403, "NODE_PUBLICATION_REFUSED", "node publication refused")
        _require_visible_order(context, scope_from_json(run.scope_json), run.order_no)
        config = chat_execution.callback_config(context.agent_run_store, run_id, context.settings)
        latest = context.agent_run_store.get(run_id)
        current_node = config.authenticate_node(token) if config else None
        if (
            config is None
            or current_node is None
            or latest is None
            or latest.state not in {"issued", "running"}
            or current_node.node_id != latest.expected_node_id
            or current_node.environment != latest.environment
            or config.expected_node_id != latest.expected_node_id
            or config.environment != latest.environment
            or config.snail_app_id != latest.app_id
            or str(config.snail_agent_id) != latest.agent_id
        ):
            raise StandardAPIError(403, "NODE_PUBLICATION_REFUSED", "node publication refused")
        manifest = execution_manifest.from_text(latest.execution_manifest)
        if manifest.identity != latest.expected_agent_version:
            raise StandardAPIError(503, "NODE_PUBLICATION_INVALID", "node publication invalid")
        return {"run_id": run_id, "manifest": latest.execution_manifest, "sha256": manifest.identity}

    @app.post("/v1/operator-tools/{tool}")
    def operator_tool_invoke(tool: str, payload: NodeClaimRequest, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return context.operator_cpms.invoke(
            request.headers.get("Authorization", "").removeprefix("Bearer "), tool
        )

    @app.post("/v1/internal/operator-runs/{run_id}/tools/{tool}/authorization")
    def operator_tool_authorization(
        run_id: str, tool: str, payload: NodeClaimRequest, request: Request, response: Response
    ):
        response.headers["Cache-Control"] = "no-store"
        return context.operator_cpms.grant(
            run_id,
            request.headers.get("Authorization", "").removeprefix("Bearer "),
            request.headers.get("X-AI-Ops-Exec-Config", ""),
            tool,
        )

    @app.post("/v1/internal/operator-runs/{run_id}/tools/{tool}/evidence")
    async def operator_tool_evidence(run_id: str, tool: str, request: Request):
        body = await _operator_json(request)
        from starlette.concurrency import run_in_threadpool

        result = await run_in_threadpool(
            context.operator_cpms.record,
            run_id,
            request.headers.get("Authorization", "").removeprefix("Bearer "),
            request.headers.get("X-AI-Ops-Exec-Config", ""),
            tool,
            body,
        )
        return JSONResponse(content=result, headers={"Cache-Control": "no-store"})

    @app.get("/v1/internal/operator-runs/{run_id}/cpms/{path:path}")
    def operator_cpms_proxy(run_id: str, path: str, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return context.operator_cpms.proxy(
            run_id,
            request.headers.get("Authorization", "").removeprefix("Bearer "),
            "/" + path,
            request.url.query,
        )

    @app.post("/v1/internal/operator-runs/{run_id}/completions")
    async def operator_completion(run_id: str, request: Request):
        body = await _operator_json(request)
        from starlette.concurrency import run_in_threadpool

        result = await run_in_threadpool(
            context.operator_cpms.complete,
            run_id,
            request.headers.get("Authorization", "").removeprefix("Bearer "),
            request.headers.get("X-AI-Ops-Exec-Config", ""),
            body,
        )
        return JSONResponse(content=result, headers={"Cache-Control": "no-store"})

    @app.post("/v1/internal/consumer-knowledge")
    async def consumer_knowledge_context(
        request: Request,
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ):
        from aiops_diagnostics import chat_data_grant, consumer_chat_service, consumer_knowledge

        response.headers["Cache-Control"] = "no-store"
        if not authorization or not authorization.startswith("Bearer "):
            raise StandardAPIError(401, "GRANT_REQUIRED", "knowledge grant required")
        try:
            raw = bytearray()
            async for part in request.stream():
                raw.extend(part)
                if len(raw) > 6000:
                    raise StandardAPIError(413, "PHOTO_CONTEXT_INVALID", "image context too large")
            image_text = raw.decode("utf-8")
            if request.headers.get("Content-Type", "").startswith("application/json"):
                body = json.loads(image_text or "{}")
                if not isinstance(body, dict) or set(body) - {"image_text"}:
                    raise ValueError("unexpected knowledge input")
                image_text = body.get("image_text", "")
            from starlette.concurrency import run_in_threadpool

            return await run_in_threadpool(
                consumer_knowledge.callback_context,
                context.runtime,
                authorization[7:],
                require_result=consumer_chat_service.require_result,
                image_text=image_text,
                read_snapshot=lambda caller, order, kind: _authorized_consumer_snapshot(
                    context.settings, caller, order, kind
                ),
            )
        except chat_data_grant.DataGrantError as exc:
            raise StandardAPIError(401, exc.code, "knowledge grant refused") from exc
        except chat_execution.ChatRunUnavailable as exc:
            raise StandardAPIError(exc.http_status, exc.code, str(exc)) from exc
        except (ValueError, OSError, ChargingStatusError) as exc:
            raise _runtime_unavailable("KNOWLEDGE_UNAVAILABLE", "knowledge unavailable", exc) from exc

    @app.post("/v1/internal/charging-status/dify-read")
    def dify_chat_read(
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Dify Workflow 的**取数**步骤调这里（APP-RUNTIME-02）。

        判据与节点领取端点是同一条纪律，但凭据不是节点凭据也不是用户会话，而是这次 QA
        **一次性的取数授权**（`chat_data_grant`）：请求体是空的严格模型，调用者**不能**
        覆盖主体、租户、订单或环境；凭据绑死这些值，180 秒过期、可撤销、只允许一次业务读取。

        业务事实来自网关这一侧的读取，Dify 拿到的就是这份裁剪后的快照；重复回调只会
        拿回同一轮已存的快照，撤权之后连缓存也拒绝。
        """
        from aiops_diagnostics import chat_data_grant, dify_run_store

        response.headers["Cache-Control"] = "no-store"
        token = (authorization or "").removeprefix("Bearer ").strip()
        grants = chat_data_grant.DataGrantStore(context.store.path)
        try:
            grant = chat_data_grant.usable(grants.by_token(token))
        except chat_data_grant.DataGrantError as exc:
            raise StandardAPIError(401, exc.code, "data grant refused") from exc
        runs = context.dify_run_store
        run = runs.by_qa(grant.qa_id)
        if run is None:
            raise StandardAPIError(401, "GRANT_UNKNOWN", "data grant refused")
        # 凭据与执行记录必须指向同一次、同一单。租户那一维走**共享规则**
        # （`order_visibility.visible_orders`），不在这里自己写比较：
        # "一行属于哪个租户、算不算可见"在这个仓只有一处定义。
        if (run.order_no, run.environment, run.workflow_id) != (
            grant.order_no,
            grant.environment,
            grant.workflow_id,
        ) or not _same_tenant_as_grant(run.tenant_id, grant.tenant_id):
            raise StandardAPIError(401, "GRANT_SCOPE_MISMATCH", "data grant refused")
        # 1) 可能阻塞的外部授权先做：判**此刻**还能不能看这张单。
        _require_dify_grant_visible(context, run)
        # 2) 授权之后重读记录：撤销/过期/取消都可能发生在上面那一步期间。
        latest = runs.get(run.run_id)
        if latest is None or latest.state not in {dify_run_store.RUNNING, dify_run_store.SUCCEEDED}:
            raise StandardAPIError(403, "DIFY_RUN_NOT_ACTIVE", "dify run is not active")
        try:
            current = chat_data_grant.usable(grants.by_id(grant.grant_id))
        except chat_data_grant.DataGrantError as exc:
            raise StandardAPIError(401, exc.code, "data grant refused") from exc
        # 3) 绑定此刻必须仍然成立，且仍是**签发时那一条**：删掉登记、改成 node 引擎、
        #    换掉 workflow，这三种情况都会让旧凭据在剩余有效期内继续把数据交出去。
        #    放在缓存回放**之前** —— 回放同样是"交出数据"。
        _require_dify_binding_current(context, run)

        # 4) 这一轮已经取过数 ⇒ 只回同一份快照，绝不第二次读业务。
        existing = runs.snapshot(run.run_id)
        if existing is not None:
            return existing
        if not grants.consume(current.grant_id):
            raise StandardAPIError(409, "GRANT_ALREADY_CONSUMED", "data grant already used")
        try:
            snapshot = _authorized_charging_snapshot(
                selected_settings, scope_from_json(run.scope_json), grant.order_no
            )
        except (ChargingStatusError, StandardAPIError) as exc:
            raise StandardAPIError(502, "CHARGING_STATUS_UNAVAILABLE", "charging status unavailable") from exc
        # 读取之后、交出之前再判一次：撤销、到期、取消、绑定变更都可能就发生在这一次
        # 读取期间。只重判"订单归属"是不够的 —— 用户取消之后订单仍属于本人，那一次
        # 读取的结果照样会被交出去；凭据在读取期间过期也一样。
        _require_dify_grant_visible(context, run)
        try:
            chat_data_grant.usable(grants.by_id(grant.grant_id))
        except chat_data_grant.DataGrantError as exc:
            raise StandardAPIError(401, exc.code, "data grant refused") from exc
        latest_now = runs.get(run.run_id)
        if latest_now is None or latest_now.state != dify_run_store.RUNNING:
            raise StandardAPIError(403, "DIFY_RUN_NOT_ACTIVE", "dify run is not active")
        _require_dify_binding_current(context, run)
        runs.store_snapshot(run.run_id, snapshot)
        return snapshot

    @app.post("/v1/internal/charging-status/completions/{run_id}")
    def complete_charging_status_loop(
        run_id: str,
        payload: NodeLoopCompletionRequest,
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
        exec_config: Annotated[str | None, Header(alias="X-AI-Ops-Exec-Config")] = None,
    ) -> dict[str, Any]:
        """整轮终局通知：把「工具领取成功」和「整个模型工具循环完成」分开。

        **不返回任何业务数据**（不是领取端点的别名）。响应只有
        ``{"run_id", "state"}``。

        判据与领取同一套、同一段代码顺序（认节点 → 绑定 → 身份逐字相等），
        差别只在于这里的身份是**每个通知都要重新证明一次**：领取时的 ``matched``
        不授权另一份配置来替这个 run 宣告完成。

        幂等与冲突：同状态重复通知 200（不改时间戳/原因）；已完成后再报失败 409；
        终态（撤销/过期/已失败）403；未启用清单 409。
        """
        response.headers["Cache-Control"] = "no-store"
        # 与领取端点同一条判据：用**这条 run 自己**的绑定，而不是委派那个键。
        existing = context.agent_run_store.peek(run_id)
        if existing is not None and existing.operation == "operator_order_diagnosis":
            if payload.status == "completed":
                raise StandardAPIError(403, "AGENT_RUN_OPERATION_MISMATCH", "agent run refused")
            return context.operator_cpms.complete(
                run_id, (authorization or "").removeprefix("Bearer "), exec_config, {"status": "failed"}
            )
        config = chat_execution.callback_config(context.agent_run_store, run_id, selected_settings)
        if config is None:
            raise StandardAPIError(404, "AGENT_RUN_DISABLED", "route not found")
        token = (authorization or "").removeprefix("Bearer ").strip()
        node = config.authenticate_node(token)
        if node is None:
            raise StandardAPIError(
                status.HTTP_401_UNAUTHORIZED, "NODE_TOKEN_REQUIRED", "node credential required"
            )
        # **只读**读一次做绑定判定：不能用 get()，它会顺手收敛过期（一次写），
        # 会把未追踪的历史行改掉。
        run = context.agent_run_store.peek(run_id)
        if run is None:
            raise StandardAPIError(status.HTTP_404_NOT_FOUND, "AGENT_RUN_NOT_FOUND", "agent run not found")
        # 绑定：同环境、同一个**预期节点**、同一 app/agent。错节点不伤他人记录（只读判定，
        # 不写任何列）。
        if (
            node.environment != run.environment
            or node.node_id != run.expected_node_id
            or config.environment != run.environment
            or config.expected_node_id != run.expected_node_id
            or config.snail_app_id != run.app_id
            or str(config.snail_agent_id) != run.agent_id
        ):
            raise StandardAPIError(status.HTTP_403_FORBIDDEN, "AGENT_RUN_WRONG_NODE", "agent run refused")

        observed = exec_config
        verdict, settled = context.agent_run_store.complete_loop(
            run_id,
            observed_agent_version=observed,
            actual_node_id=node.node_id,
            failure_reason=(payload.reason or "model_failed") if payload.status == "failed" else None,
            # 成功也可以带原因码（同原因幂等、不同原因冲突）；不带就是 None。
            success_reason=payload.reason if payload.status == "completed" else None,
        )
        if verdict in {"completed", "already_completed", "failed", "already_failed"}:
            assert settled is not None
            return {"run_id": run_id, "state": settled.state}
        if verdict in {"conflict", "incomplete"}:
            raise StandardAPIError(
                status.HTTP_409_CONFLICT,
                "AGENT_RUN_COMPLETION_CONFLICT",
                "agent run is already settled or not in progress",
            )
        if verdict == "drift":
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_RUN_EXEC_CONFIG_DRIFT", "agent run refused"
            )
        if verdict == "not_tracked":
            raise StandardAPIError(
                status.HTTP_409_CONFLICT,
                "AGENT_RUN_COMPLETION_NOT_TRACKED",
                "agent run does not track a loop completion",
            )
        if verdict == "missing":
            raise StandardAPIError(status.HTTP_404_NOT_FOUND, "AGENT_RUN_NOT_FOUND", "agent run not found")
        if verdict == "expired":
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_RUN_NOT_ACTIVE", "agent run is not active"
            )
        raise StandardAPIError(status.HTTP_403_FORBIDDEN, "AGENT_RUN_NOT_ACTIVE", "agent run is not active")

    @app.post("/v1/health-report-jobs", status_code=status.HTTP_202_ACCEPTED)
    def create_health_report_job(
        payload: HealthReportJobRequest,
        caller: ScopeContext = Depends(authenticated_caller),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        verdict = _order_authorization(context, caller, payload.order_no)
        if verdict == UNAVAILABLE:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "ORDER_AUTHORIZATION_UNAVAILABLE",
                "order authorization unavailable",
                retryable=True,
            )
        if verdict == NOT_OWNED:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "ORDER_NOT_FOUND",
                "order not found",
            )
        try:
            job = context.runtime.start_health_report(caller, payload.order_no, language=language)
        except (ValueError, RuntimeError) as exc:
            raise _runtime_unavailable(
                "REPORT_JOB_UNAVAILABLE", "health report job unavailable", exc
            ) from exc
        return _health_job_response(job)

    @app.get("/v1/health-report-jobs/{job_id}")
    def get_health_report_job(
        job_id: str,
        caller: ScopeContext = Depends(authenticated_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            job = context.runtime.get_health_report(caller, job_id)
        except (ValueError, RuntimeError) as exc:
            raise _runtime_unavailable(
                "REPORT_JOB_UNAVAILABLE", "health report job unavailable", exc
            ) from exc
        if job is None:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "REPORT_JOB_NOT_FOUND",
                "health report job not found",
            )
        return _health_job_response(job)

    @app.post("/v1/standard/diagnoses", status_code=status.HTTP_202_ACCEPTED)
    def create_standard_diagnosis(
        payload: StandardDiagnosisRequest,
        caller: ScopeContext = Depends(authenticated_diagnosis_caller),  # noqa: B008
        business_entry: Annotated[str | None, Header(alias="X-Business-Entry")] = None,
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        # #695: this route used to read `X-Business-Entry`, use it for scope,
        # and drop it — so the diagnosis could not know which content domain
        # it was answering in. It is forwarded now, and deliberately **as the
        # raw header** rather than via `assistant_identity`: the entry decides
        # which published operations agent's methodology is injected, and that
        # is the same question `third_session_auth` already answers from this
        # same header with the same predicate. Resolving the platform identity
        # here instead would buy no authorization — the data range still comes
        # from the session — while adding a platform-directory lookup to a
        # route that does not otherwise need one.
        verdict = _order_authorization(context, caller, payload.order_no)
        if verdict == UNAVAILABLE:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "ORDER_AUTHORIZATION_UNAVAILABLE",
                "order authorization unavailable",
                retryable=True,
            )
        if verdict == NOT_OWNED:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "ORDER_NOT_FOUND",
                "order not found",
            )
        try:
            diagnosis = context.runtime.start_standard_diagnosis(
                caller,
                payload.order_no,
                payload.question,
                payload.indicator_code,
                language=language,
                business_entry=business_entry,
            )
        except (ValueError, RuntimeError) as exc:
            raise _runtime_unavailable("DIAGNOSIS_UNAVAILABLE", "diagnosis unavailable", exc) from exc
        return _standard_diagnosis_response(diagnosis, language)

    @app.post("/v1/standard/analyses", status_code=status.HTTP_202_ACCEPTED)
    def create_operations_analysis(
        payload: OperationsAnalysisRequest,
        identity: tuple[ScopeContext, Any] = Depends(operator_diagnosis_identity),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        """运维分析面（#697）：站点集合 + 时间窗 → 一张确定性报告卡。

        **管家端专用。** 这是本票明确不给客户端开的一个新面——不是「客户端暂时用不上」，
        而是它整个不成立：客户端身份的数据范围是「本人」，而这张卡问的是「这一组站点
        这一段区间的整体情况」，两者不是同一个问题的不同粒度。因此这里显式拒绝，
        而不是让它落进一个会返回空结果的路径（那会让调用方以为是数据问题）。
        """
        caller, decision = identity
        if str(decision.platform) != PLATFORM_OPERATOR:
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN,
                "OPERATOR_ENTRY_REQUIRED",
                "operator entry required",
            )
        site_ids = tuple(payload.site_ids) if payload.site_ids is not None else None
        try:
            if payload.card == "failure_rate":
                job = context.runtime.start_operations_analysis(
                    caller,
                    site_ids=site_ids,
                    window_start=payload.window_start.isoformat(),
                    window_end=payload.window_end.isoformat(),
                    language=language,
                )
            else:
                job = context.runtime.start_operations_card(
                    caller,
                    card=payload.card,
                    site_ids=site_ids,
                    window_start=payload.window_start.isoformat(),
                    window_end=payload.window_end.isoformat(),
                    language=language,
                )
        except (ValueError, RuntimeError) as exc:
            raise _runtime_unavailable("ANALYSIS_UNAVAILABLE", "analysis unavailable", exc) from exc
        return _analysis_job_response(job, language)

    @app.get("/v1/standard/analyses/{analysis_id}")
    def get_operations_analysis(
        analysis_id: str,
        identity: tuple[ScopeContext, Any] = Depends(operator_diagnosis_identity),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        caller, decision = identity
        if str(decision.platform) != PLATFORM_OPERATOR:
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN,
                "OPERATOR_ENTRY_REQUIRED",
                "operator entry required",
            )
        try:
            job = context.runtime.get_operations_analysis(caller, analysis_id)
        except (ValueError, RuntimeError) as exc:
            raise _runtime_unavailable("ANALYSIS_UNAVAILABLE", "analysis unavailable", exc) from exc
        if job is None:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "ANALYSIS_NOT_FOUND",
                "analysis not found",
            )
        return _analysis_job_response(job, language)

    @app.get("/v1/standard/diagnoses/{diagnosis_id}")
    def get_standard_diagnosis(
        diagnosis_id: str,
        caller: ScopeContext = Depends(authenticated_diagnosis_caller),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            diagnosis = context.runtime.get_standard_diagnosis(caller, diagnosis_id)
        except (ValueError, RuntimeError) as exc:
            raise _runtime_unavailable("DIAGNOSIS_UNAVAILABLE", "diagnosis unavailable", exc) from exc
        if diagnosis is None:
            # A qa_ id here means the caller created a general-question job
            # (POST /v1/assistant/questions returned type=qa) but is polling
            # the order-diagnosis endpoint. Point them at the right poll path
            # instead of a bare "not found", so the wrong-endpoint mistake is
            # self-explanatory (observed in real frontend integration).
            if diagnosis_id.startswith("qa_"):
                raise StandardAPIError(
                    status.HTTP_404_NOT_FOUND,
                    "DIAGNOSIS_NOT_FOUND",
                    "this is a general-question job (type=qa); poll GET /v1/assistant/questions/"
                    + diagnosis_id
                    + " instead",
                )
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "DIAGNOSIS_NOT_FOUND",
                "diagnosis not found",
            )
        return _standard_diagnosis_response(diagnosis, language)

    @app.get("/v1/standard/diagnoses")
    def list_standard_diagnoses(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        caller: ScopeContext = Depends(authenticated_diagnosis_caller),  # noqa: B008
    ) -> dict[str, Any]:
        diagnoses = context.runtime.list_standard_diagnoses(caller, limit=limit)
        return {"diagnoses": diagnoses}

    @app.post("/v1/standard/diagnoses/{diagnosis_id}/cancel")
    def cancel_standard_diagnosis(
        diagnosis_id: str,
        caller: ScopeContext = Depends(authenticated_diagnosis_caller),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        """Stop one in-flight order diagnosis (#499).

        The same contract as the general-question cancel, for the same reasons:
        POST rather than DELETE because the row survives as ``cancelled``, the
        response carries the job's terminal state so the caller need not poll
        again, and a repeat click or a cancel that lost a race answers the
        job's own current state rather than an error.

        Missing and out-of-scope are one answer (404), so cancelling cannot be
        used to probe which diagnosis ids exist.
        """
        try:
            diagnosis = context.runtime.cancel_standard_diagnosis(caller, diagnosis_id)
        except (ValueError, RuntimeError) as exc:
            raise _runtime_unavailable("DIAGNOSIS_UNAVAILABLE", "diagnosis unavailable", exc) from exc
        if diagnosis is None:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "DIAGNOSIS_NOT_FOUND",
                "diagnosis not found",
            )
        return _standard_diagnosis_response(diagnosis, language)

    def _agent_error(exc: AgentError) -> StandardAPIError:
        if isinstance(exc, AgentNotFound):
            return StandardAPIError(status.HTTP_404_NOT_FOUND, "AGENT_NOT_FOUND", "agent not found")
        if isinstance(exc, AgentForbidden):
            return StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_FORBIDDEN", "agent access is not permitted"
            )
        if isinstance(exc, AgentConflict):
            return StandardAPIError(
                status.HTTP_409_CONFLICT, "AGENT_REVISION_CONFLICT", "agent revision conflict"
            )
        return StandardAPIError(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.code, str(exc))

    @app.post("/v1/agents", status_code=status.HTTP_201_CREATED)
    def create_agent(
        payload: AgentCreateRequest,
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            agent = context.agent_manager.create(
                caller,
                name=payload.name,
                description=payload.description,
                config=payload.to_config(),
            )
        except AgentError as exc:
            raise _agent_error(exc) from exc
        return agent.to_dict()

    @app.get("/v1/agents")
    def list_agents(
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            agents = context.agent_manager.list(caller)
        except AgentError as exc:
            raise _agent_error(exc) from exc
        return {"agents": [agent.to_dict() for agent in agents]}

    @app.get("/v1/agents/{agent_id}")
    def get_agent(
        agent_id: str,
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            agent = context.agent_manager.get(caller, agent_id)
        except AgentError as exc:
            raise _agent_error(exc) from exc
        return agent.to_dict()

    @app.put("/v1/agents/{agent_id}")
    def update_agent(
        agent_id: str,
        payload: AgentUpdateRequest,
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            agent = context.agent_manager.update(
                caller,
                agent_id,
                expected_revision=payload.expected_revision,
                name=payload.name,
                description=payload.description,
                config=payload.to_config(),
            )
        except AgentError as exc:
            raise _agent_error(exc) from exc
        return agent.to_dict()

    @app.post("/v1/agents/{agent_id}/publish")
    def publish_agent(
        agent_id: str,
        payload: AgentActionRequest,
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            version = context.agent_manager.publish(
                caller, agent_id, expected_revision=payload.expected_revision
            )
        except AgentError as exc:
            raise _agent_error(exc) from exc
        return version.to_dict()

    @app.post("/v1/agents/{agent_id}/draft")
    def fork_agent_draft(
        agent_id: str,
        payload: AgentActionRequest,
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            agent = context.agent_manager.fork_draft(
                caller, agent_id, expected_revision=payload.expected_revision
            )
        except AgentError as exc:
            raise _agent_error(exc) from exc
        return agent.to_dict()

    @app.post("/v1/agents/{agent_id}/disable")
    def disable_agent(
        agent_id: str,
        payload: AgentActionRequest,
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            agent = context.agent_manager.disable(
                caller, agent_id, expected_revision=payload.expected_revision
            )
        except AgentError as exc:
            raise _agent_error(exc) from exc
        return agent.to_dict()

    @app.delete("/v1/agents/{agent_id}")
    def delete_agent(
        agent_id: str,
        payload: AgentActionRequest,
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, bool]:
        try:
            context.agent_manager.delete(caller, agent_id, expected_revision=payload.expected_revision)
        except AgentError as exc:
            raise _agent_error(exc) from exc
        return {"deleted": True}

    @app.get("/v1/agents/{agent_id}/versions/{version_no}")
    def get_agent_version(
        agent_id: str,
        version_no: int,
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            version = context.agent_manager.version(caller, agent_id, version_no)
        except AgentError as exc:
            raise _agent_error(exc) from exc
        return version.to_dict()

    @app.post("/v1/agents/{agent_id}/debug-run")
    def debug_run_agent(
        agent_id: str,
        payload: AgentDebugRunRequest,
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        """Isolated draft preview (T5/#171): runs the draft config through the
        production customer-QA harness without touching conversations or
        orders. Only draft-config customer agents are debuggable."""
        from aiops_diagnostics.agent_lifecycle import EDIT_ROLES

        if not frozenset(getattr(caller, "roles", ())).intersection(EDIT_ROLES):
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_FORBIDDEN", "agent access is not permitted"
            )
        try:
            agent = context.agent_manager.get(caller, agent_id)
        except AgentError as exc:
            raise _agent_error(exc) from exc
        if agent.status != "draft" or agent.config.agent_type != "customer":
            raise StandardAPIError(
                status.HTTP_409_CONFLICT,
                "AGENT_DEBUG_STATE_INVALID",
                "only a draft customer agent can be debug-run",
            )
        runner = getattr(context.runtime, "run_agent_debug", None)
        if runner is None:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "AGENT_DEBUG_UNAVAILABLE",
                "agent debug-run is not configured",
                retryable=True,
            )
        try:
            return runner(caller, agent_id, payload.question, language=language)
        except AgentRuntimeError as exc:
            raise StandardAPIError(
                status.HTTP_502_BAD_GATEWAY,
                "AGENT_DEBUG_FAILED",
                _debug_public_error(exc),
                retryable=True,
            ) from exc

    # ------------------------------------------------------------------
    # Product-entry shortcuts (#230)
    # ------------------------------------------------------------------

    def _shortcut_error(exc: ShortcutError) -> StandardAPIError:
        if isinstance(exc, ShortcutNotFound):
            return StandardAPIError(status.HTTP_404_NOT_FOUND, "SHORTCUT_NOT_FOUND", "shortcut not found")
        if isinstance(exc, ShortcutForbidden):
            return StandardAPIError(
                status.HTTP_403_FORBIDDEN, "SHORTCUT_FORBIDDEN", "shortcut access is not permitted"
            )
        if isinstance(exc, ShortcutConflict):
            return StandardAPIError(
                status.HTTP_409_CONFLICT, "SHORTCUT_REVISION_CONFLICT", "shortcut revision conflict"
            )
        return StandardAPIError(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.code, str(exc))

    def _dify_starters(
        context: Any, caller: Any, business_entry: str, language: str
    ) -> tuple[str | None, tuple[str, ...] | None]:
        """运营在 Dify 里写的开场白，**只对 ``zh`` 且只对已发布的那一版**。

        返回 ``None`` 表示"用制品" —— 三种情形合并成同一答案，因为它们对调用方是同一件事：
        语言不是 zh（非中文一律走手写制品，运行时没有翻译）、这个租户/入口没有绑定的
        agent、或者那一版没写过开场白。**不区分**是有意的：区分出来的信息只有"运营配没配
        这一项"，而处置完全相同。

        读的是**已发布快照**（与运行时服务用户时的口径一致）—— 草稿里的开场白不算数，
        否则运营打字打到一半的文案会直接被用户看到，这正是 Dify 没有草稿区所要求的那条纪律。

        任何读取失败都回退制品（``None``）：开场白是**呈现**，读不到它不该让会话页
        打不开 —— 与 ``select_customer_agent`` 在 store 不可读时的降级方向一致。
        """
        if language != DEFAULT_LANGUAGE:
            return None, None
        tenant_id = str(getattr(caller, "effective_tenant_id", "") or "")
        entry = str(business_entry or "").strip().lower()
        if not tenant_id or not entry:
            return None, None
        try:
            runtime = context.runtime
            store = getattr(runtime, "agent_store", None)
            registry = getattr(runtime, "dify_app_registry", None)
            if store is None or registry is None or not registry.is_configured():
                return None, None
            from aiops_diagnostics.qa_rag import select_customer_agent

            registered_name, configured = runtime._registered_agent(tenant_id, entry)
            if not configured or registered_name is None:
                return None, None
            selection = select_customer_agent(store, tenant_id, agent_name=registered_name)
            if selection is None:
                return None, None
            version = store.version(selection.agent_id, tenant_id, selection.version_no)
            opening = str(version.snapshot.get("opening_statement") or "").strip()
            suggested = tuple(
                str(item).strip()
                for item in (version.snapshot.get("suggested_questions") or ())
                if str(item).strip()
            )
        except Exception:  # noqa: BLE001 - presentation must never break the page
            return None, None
        return (opening or None), (suggested or None)

    @app.get("/v1/assistant/starters")
    def assistant_starters(
        identity: tuple[ScopeContext, PlatformDecision] = Depends(shortcut_identity),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
        business_entry: Annotated[str | None, Header(alias="X-Business-Entry")] = None,
    ) -> dict[str, Any]:
        """This assistant's opening line and suggested questions (#585 / PRD #577).

        **中文母版来自 Dify，其余语言来自我们的制品**（PR-5 的 #585 反转）。

        这一条改动的方向要说清：Dify 的 ``opening_statement`` **现在被消费**，但只覆盖
        ``zh``。理由是 Dify 没有逐语言的内容变体 —— 它一个字段一个值 —— 所以
        「运营在 Dify 改开场白」只可能改中文那一份；其余语言仍是
        ``agent_content.AGENT_STARTERS`` 里**手写**的文案（运行时**没有**翻译机制，
        这是「Dify 存中文母版、我们派生」那个决定的直接后果）。

        **`opening_source` 是刻意暴露的**：没有它，「哪几门语言还停在制品上」不可观测，
        运营会以为全语言都改了。``dify`` = 这个语言取到了运营的值；``artifact`` =
        回退到制品（非中文，或该 agent 没有覆盖）。

        ``suggested_questions`` 仍然**只**来自制品：Dify 那个字段在 chat 应用里是
        按语言无分支的同一种形状，而本期只反转了开场白这一项。
        """
        caller, _ = identity
        payload = AGENT_STARTERS.public(language)
        opening, suggested = _dify_starters(context, caller, business_entry or "", language)
        if opening is not None:
            payload["opening"] = opening
            payload["opening_source"] = "dify"
        else:
            payload["opening_source"] = "artifact"
        if suggested is not None:
            payload["suggested_questions"] = list(suggested)
            payload["suggested_source"] = "dify"
        else:
            payload["suggested_source"] = "artifact"
        return {"type": "assistant_starters", **payload}

    @app.get("/v1/shortcuts")
    def list_shortcuts(
        identity: tuple[ScopeContext, PlatformDecision] = Depends(shortcut_identity),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        """Published product-entry shortcuts for this caller's tenant+entry.

        Drafts are invisible, disabled rows are gone, other tenants/entries
        are unreachable. Each item carries the stable ``code`` the client
        wires behavior to (e.g. order picker for requires_order=True), plus
        copy localized in the request language (zh fallback).

        Buttons only. The chat-page banner lives in the same store and shares
        the same lifecycle, but it is served by ``GET /v1/banner`` — the split
        is a query selection here, not a filter a client has to apply (#588).
        """
        caller, decision = identity
        try:
            shortcuts = context.shortcut_manager.list_effective(
                caller, business_entry=decision.platform, kind=BUTTON_KIND
            )
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return {
            "type": "shortcut_list",
            # The list-level language is only truthful when every row agrees;
            # otherwise the per-row `language` is what a client must read.
            "language": _shortcut_list_language(shortcuts, language),
            "count": len(shortcuts),
            "shortcuts": [item.public(language) for item in shortcuts],
        }

    # ------------------------------------------------------------------
    # Knowledge-source self-service (ADR 0048).
    #
    # These three routes exist because "登记表 + 租户映射" required an engineer:
    # the operator had to know a `knowledge_id:tenant:kb1|kb2` syntax, know the
    # tenant id their RAGFlow account maps to, and hand it to someone who could
    # edit env. None of that is a business decision — it is data entry.
    #
    # What stays a decision is WHICH libraries a tenant may reach, and that is
    # what these routes put in front of a human: a picker of the tenant's own
    # datasets, a name, and a save button.
    # ------------------------------------------------------------------

    def _require_knowledge_admin(caller: ScopeContext) -> None:
        """知识库这几条路由的判据：**本租户内的调用者**，不要求平台级全局码。

        为什么不是 ``aiops:agent:edit``。那个码建于 ADR-0029，当时的 Dify 是**平台级
        单工作区**（ADR-0056），所有人共享一个 workspace、能看全部 app，所以必须有
        一道全局门。1:1:1 落地后那个前提消失了：下面四条路由全部用
        ``caller.effective_tenant_id`` 限死，调用者只能列出/绑定**自己租户**的库 ——
        跨租户越权在结构上做不到。此时再要求一个全局码，唯一的效果是把有业务角色的
        运营挡在自己租户的库外面（2026-10-22 真机：Ulink 运营持 ``ROLE_ADMIN`` +
        ``CPO``，两个角色都是**租户级**，都没有 ``aiops:agent:edit`` ⇒ 403）。

        代价明写：放开后，**同一租户内**任何已登录调用者都能改本租户的绑定。这在一个
        租户只有一个管理员时是正确的粒度；租户里出现普通员工账号时，应当把判据换成
        该租户自己的管理角色（公司原生表达是 ``ROLE_ADMIN``），而不是退回全局码 ——
        后者会重新引入"平台管理员才能配自己租户"的错位。

        仍然要求**有一个身份**：没有 tenant 的调用者在这里被拒，与下游按租户取数
        失败关闭的方向一致。
        """
        if not str(getattr(caller, "effective_tenant_id", "") or "").strip():
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN,
                "KNOWLEDGE_FORBIDDEN",
                "knowledge-source configuration requires a tenant identity",
            )

    #: 管理页本身不带凭据：它是一个静态文档，页面自己向下面的 API 出示令牌。
    #: 放在网关里而不是另一个静态站，是因为它必须与它调用的 API **同域**
    #: —— 否则浏览器会先撞 CORS，再撞"把令牌发给另一个源"。
    #:
    #: 路径必须在 `/v1/` **下面**：公网 nginx 只代理 `/v1/` 与 `/v1/dify/`，
    #: 一个顶层 `/knowledge` 从外面是 404（实测）。而页面要能带自己的
    #: Authorization 到下面的接口，所以它的 fetch 走 operator 入口
    #: （`X-Business-Entry: operator` 那条 map 会**保留**调用者的 Authorization，
    #: consumer 那条会换成服务令牌）。
    @app.get("/v1/knowledge/console", include_in_schema=False)
    def knowledge_console(
        request: Request,
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
    ) -> Response:
        """Send the operator's browser into RAGFlow **as their own tenant** (#926).

        Three things this route deliberately does not accept: a tenant id, a
        RAGFlow account, and a password. The tenant comes from the company session
        the browser already holds, the account and password come from what
        onboarding recorded (#924), and the browser receives only a session hop.

        Why the console is reached this way at all: RAGFlow's authorisation binds
        a request's tenant to the *logged-in user's own id*, so "several operators
        sharing one RAGFlow tenant" does not exist in that product. What can be
        shared is a session obtained as the tenant owner — so the proxy represents
        the tenant, and the cost of that (RAGFlow cannot tell the operators apart)
        is paid for in our own access ledger rather than pretended away.

        **两种应答形状，取决于谁在问**（2026-10-22 真浏览器验收后补）：

        - 浏览器的顶层导航（``Accept: text/html``）：把 ``Set-Cookie`` 种在 303 上，由浏览器
          自己跟随。这就是本路由一开始的形状。
        - 页面脚本的 ``fetch``：返回 **200 + JSON ``{"target": …}``**，``Set-Cookie`` 种在
          这一次**同源**应答上。

        为什么脚本那条不能用 303：跨源重定向下的 ``Set-Cookie`` 不会被保存，而且页面也读不到
        跨源重定向的响体。而**同源**应答上的 ``Set-Cookie`` 一定被保存 —— 只要它有正确的
        ``Domain``（见 ``ragflow_console_cookie_domain``），页面随后自己 ``location.href``
        跳过去时浏览器就会带上它。这也是「打开 RAGFlow」按钮唯一可行的形状。
        """
        from aiops_diagnostics.ragflow_console import (
            RAGFlowLoginClient,
            RAGFlowLoginError,
            open_console,
            record,
        )

        settings = context.settings
        tenant_id = str(caller.effective_tenant_id)
        if (
            not settings.ragflow_console_base_url
            or not settings.ragflow_credential_key
            or not settings.ragflow_console_public_pem
        ):
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, "KNOWLEDGE_CONSOLE_DISABLED", "console access is not enabled"
            )
        client = RAGFlowLoginClient(settings.ragflow_console_base_url)
        try:
            target, session = open_console(
                store=context.store,
                environment=settings.environment,
                caller=caller,
                credential_key=settings.ragflow_credential_key,
                client=client,
                public_base_url=settings.ragflow_console_public_url or settings.ragflow_console_base_url,
                public_pem_path=settings.ragflow_console_public_pem,
            )
        except RAGFlowLoginError as exc:
            # Refusals are recorded too: with one shared owner, "who tried" is the
            # only operator-level fact we have, and it is worth keeping.
            record(
                context.store,
                tenant_id=tenant_id,
                environment=settings.environment,
                outcome="refused",
                caller=caller,
                reason_code=exc.code,
            )
            http_status = (
                status.HTTP_403_FORBIDDEN
                if exc.code in {"KNOWLEDGE_FORBIDDEN"}
                else status.HTTP_424_FAILED_DEPENDENCY
                if exc.code != "KNOWLEDGE_TENANT_NOT_ONBOARDED"
                else status.HTTP_409_CONFLICT
            )
            raise StandardAPIError(http_status, exc.code, exc.message) from exc
        record(
            context.store,
            tenant_id=tenant_id,
            environment=settings.environment,
            outcome="granted",
            caller=caller,
            ragflow_email=session.ragflow_email,
        )
        wants_html = "text/html" in (request.headers.get("accept") or "")
        response: Response = (
            RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
            if wants_html
            else JSONResponse(
                {"target": target, "ragflow_email": session.ragflow_email},
                status_code=status.HTTP_200_OK,
            )
        )
        for name, value in session.cookies.items():
            response.set_cookie(
                name,
                value,
                domain=settings.ragflow_console_cookie_domain or None,
                path="/",
                httponly=True,
                samesite="lax",
            )
        return response

    @app.post("/v1/internal/knowledge/sync", include_in_schema=False)
    def knowledge_sync_by_workspace(
        request: Request,
        workspace: str = "",
        _: None = Depends(dify_workspace_credential),  # noqa: B008
    ) -> dict[str, Any]:
        """workspace → 那个租户的 RAGFlow 库 → Dify dataset（ADR-0122 / #1056）。

        调用方是**桥**，不是浏览器（内部令牌，与 ``/v1/internal/knowledge/open``
        同一套门）。**租户只来自 workspace 反查**：请求里出现任何租户参数都是 400 ——
        忽略会让调用方以为自己指定成功了。

        **幂等且只增不减**：只创建 / 改名 / 修权限 / 补 retrieval_model，绝不删。
        RAGFlow 里已不存在的库只出现在 ``orphans`` 里（Dify 没有恢复入口，自动删是
        单向操作）。

        失败不抛给调用方看的业务错，而是让它按 ``created: 0`` 处理 —— 桥把这条路径
        挂在「添加知识库」弹窗上，一次上游抖动不该让那个弹窗报错。
        """
        for forbidden in ("tenant_id", "tenant", "environment"):
            if forbidden in request.query_params:
                raise StandardAPIError(
                    status.HTTP_400_BAD_REQUEST,
                    "KNOWLEDGE_SYNC_PARAM_NOT_ACCEPTED",
                    f"{forbidden} is resolved from the workspace, never from the request",
                )
        if not workspace.strip():
            raise StandardAPIError(
                status.HTTP_400_BAD_REQUEST,
                "KNOWLEDGE_SYNC_WORKSPACE_REQUIRED",
                "workspace is required",
            )
        settings = context.settings
        if not settings.dify_console_base_url or not settings.dify_console_api_key:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, "KNOWLEDGE_SYNC_DISABLED", "console access is not enabled"
            )
        api_id = settings.dify_external_knowledge_api_id
        if not api_id:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, "KNOWLEDGE_SYNC_DISABLED", "external knowledge api id is not set"
            )
        workspaces = getattr(context.runtime, "dify_workspaces", None)
        client = getattr(context.runtime, "kb_search_client", None)
        if workspaces is None or client is None:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, "KNOWLEDGE_SYNC_DISABLED", "knowledge sync is not available"
            )

        from aiops_diagnostics.dify_console_admin import ConsoleApiSettings, DifyConsoleAdmin
        from aiops_diagnostics.dify_knowledge_datasets import libraries_for, run
        from aiops_diagnostics.dify_workspaces import DifyWorkspaceError

        try:
            tenant_id = workspaces.tenant_for_workspace(workspace.strip(), settings.environment)
        except DifyWorkspaceError as exc:
            # 未登记 / 状态不可用：**这是调用方能行动的拒绝**，不是"暂时不可用"。
            # 桥把这条路径挂在弹窗上，所以它会把 4xx 译成"不刷新列表"而不是红字。
            raise StandardAPIError(exc.http_status, exc.code, exc.message) from exc
        # kb-service 不可达 ⇒ 没有可收敛的东西。这是**空结果**，不是失败：桥那边的
        # 调用方只关心"要不要刷新列表"，而答案是"不用"。
        try:
            owned = client.for_tenant(tenant_id).list_knowledge_bases()
        except Exception:  # noqa: BLE001 - see above
            return {"created": 0, "tenant_id": tenant_id, "unavailable": True}

        libraries = libraries_for(tenant_id, owned)
        console = DifyConsoleAdmin(
            ConsoleApiSettings(settings.dify_console_base_url, settings.dify_console_api_key)
        )

        def converge() -> Any:
            return run(
                workspace_id=workspace.strip(),
                libraries=libraries,
                console=console,
                external_knowledge_api_id=api_id,
                dry_run=False,
                # 运营在 RAGFlow 删了库之后期待 Dify 的卡片消失（2026-10-24 用户反馈）。
                # **这里开它是安全的**：`libraries` 就是刚刚从 kb-service 读到的行，
                # 读失败的那条分支在上面就 return 了 —— 不存在"上游不可达时把一切当孤儿"。
                retire_orphans=True,
            )

        # **并发闸**：两个运营（或同一个人的两个标签页）同时打开弹窗，就是两个并发
        # pass，各自读一遍列表、各自决定建同一个 —— 而 Dify 对 `(tenant, name)` 没有
        # 唯一约束，输的一方留下重复。闸在 `knowledge_dataset_sync_guard`；它让后来者
        # **排队**而不是跳过，因为被跳过的恰好是「刚在 RAGFlow 建完库切回来」那一次
        # （ADR-0125）。
        #
        # 闸的 `run()` 返回的是**它自己的**结论，不返回 work 的产物，所以这里用一个
        # 单元素列表把产物带出来 —— 比让闸去理解 `run()` 的返回类型更省事，也不会
        # 让闸与同步实现耦合。
        guard = getattr(context.runtime, "knowledge_dataset_sync_guard", None)
        outcome: list[dict[str, Any]] = []
        if guard is None:
            guard_outcome = "unguarded"
            outcome.append(converge())
        else:
            guard_outcome = guard.run(work=lambda: (outcome.append(converge()), None)[1])
        if not outcome:
            # 排队超时或闸自己失败：没有产出可报，但**不是错误** —— 下一次触发
            # （页面 60 秒轮询）会再来。**这里绝不缓存"刚同步过"**，否则又把下一次
            # 真需要的同步挡掉。
            _LOGGER.info("knowledge sync workspace=%s tenant=%s %s", workspace, tenant_id, guard_outcome)
            return {"created": 0, "tenant_id": tenant_id, "skipped": True}
        applied = outcome[0]
        created = sum(1 for item in applied.get("applied") or [] if item.get("kind") == "create")
        # **「有没有变化」不等于「建了几条」。** 调用方（Dify 页面）靠这个数决定要不要
        # 失效列表缓存；只回 `created` 的话，运营改名/删库之后列表不会刷新 —— 卡片
        # 还在，点进去 404。2026-10-24 review 指出（P2）。
        #
        # 三类都要算：建（applied 里的 create）、改（applied 里的非 create：改名、
        # 重指 api id 等）、退休（orphans 里被删掉的 Dify dataset）。`applied` 只含
        # 真正写下去的行，`skipped`/`ok` 的空跑不在此列。
        changed = len(applied.get("applied") or []) + len(applied.get("retired") or [])
        _LOGGER.info(
            "knowledge sync workspace=%s tenant=%s applied=%d created=%d orphans=%d changed=%d",
            workspace,
            tenant_id,
            len(applied.get("applied") or []),
            created,
            len(applied.get("orphans") or []),
            changed,
        )
        return {"created": created, "changed": changed, "tenant_id": tenant_id, "outcome": applied}

    @app.get("/v1/internal/knowledge/open", include_in_schema=False)
    def knowledge_open_by_workspace(
        request: Request,
        workspace: str = "",
        _: None = Depends(dify_workspace_credential),  # noqa: B008
    ) -> Response:
        """Dify workspace → 那个租户的 RAGFlow 会话 → 303（#982 / PRD #981）。

        调用方是**桥**，不是浏览器：桥已经握着 Dify 会话并从中解析出 ``current_tenant``
        （#984）。这条路由因此用网关自己的**内部令牌**，与 ``/v1/dify/workspace`` 同一套
        机制，浏览器构造不出来。

        **租户只来自 workspace 反查。** 请求里出现任何租户参数都是 400 —— 不是"忽略"。
        差别很重要：忽略会让调用方以为自己指定成功了，而 400 直接说"这条路径不接受它"。
        `2026-10-22` 的知识库 ``PUT`` 越权就是"按请求里的字段决定归属"这一类错误。

        ``workspace`` 为空 ⇒ 400。不回落、不猜：一个空参数落到"第一个租户"上，是这条
        链路上最坏的一种失败。
        """
        for forbidden in ("tenant_id", "tenant", "environment"):
            if forbidden in request.query_params:
                raise StandardAPIError(
                    status.HTTP_400_BAD_REQUEST,
                    "KNOWLEDGE_OPEN_PARAM_NOT_ACCEPTED",
                    f"{forbidden} is resolved from the workspace, never from the request",
                )
        if not workspace.strip():
            raise StandardAPIError(
                status.HTTP_400_BAD_REQUEST,
                "KNOWLEDGE_OPEN_WORKSPACE_REQUIRED",
                "workspace is required",
            )
        from aiops_diagnostics.ragflow_console import (
            DeclaredTenant,
            RAGFlowLoginClient,
            RAGFlowLoginError,
            open_console,
            record,
        )

        settings = context.settings
        if (
            not settings.ragflow_console_base_url
            or not settings.ragflow_credential_key
            or not settings.ragflow_console_public_pem
        ):
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, "KNOWLEDGE_CONSOLE_DISABLED", "console access is not enabled"
            )
        try:
            # 反查只在这里发生一次；下面拿到的是一个**已声明的租户**，与浏览器那条
            # 链路的形状完全相同。
            tenant_id = context.dify_workspaces.tenant_for_workspace(workspace, settings.environment)
        except DifyWorkspaceError as exc:
            raise StandardAPIError(exc.http_status, exc.code, exc.message, retryable=exc.retryable) from exc
        except ValueError as exc:
            raise StandardAPIError(
                status.HTTP_400_BAD_REQUEST, "KNOWLEDGE_OPEN_WORKSPACE_INVALID", str(exc)
            ) from exc

        client = RAGFlowLoginClient(settings.ragflow_console_base_url)
        try:
            target, session = open_console(
                store=context.store,
                environment=settings.environment,
                caller=DeclaredTenant(effective_tenant_id=tenant_id),
                credential_key=settings.ragflow_credential_key,
                client=client,
                public_base_url=settings.ragflow_console_public_url or settings.ragflow_console_base_url,
                public_pem_path=settings.ragflow_console_public_pem,
            )
        except RAGFlowLoginError as exc:
            record(
                context.store,
                tenant_id=tenant_id,
                environment=settings.environment,
                outcome="refused",
                reason_code=exc.code,
            )
            http_status = (
                status.HTTP_409_CONFLICT
                if exc.code == "KNOWLEDGE_TENANT_NOT_ONBOARDED"
                else status.HTTP_424_FAILED_DEPENDENCY
            )
            raise StandardAPIError(http_status, exc.code, exc.message) from exc
        record(
            context.store,
            tenant_id=tenant_id,
            environment=settings.environment,
            outcome="granted",
            ragflow_email=session.ragflow_email,
        )
        # **只发票，不交会话**（#985）。
        #
        # 上一版（#984）把 target 与 cookie 一起交给桥，让桥转给浏览器。真机证明那样
        # **不可能成功**：桥在 `dify.ranlei.work` 上，而浏览器拒绝由非本域页面设置的
        # `Domain=.mall.qushiyun.com` cookie。这是 cookie 规范，不是配置问题。
        #
        # 所以会话必须由 `kb.mall.qushiyun.com` 那一跳自己种下去。两跳之间传一张
        # 一次性票：不透明、60 秒、用一次即废，且**不含会话** —— 兑换时才去换。
        #
        # 会话在这里已经换过一次，看起来是白换；但那一次的价值是**现在就知道能不能进**
        # （未开户 / 上游挂了在这里就报错），而不是等到浏览器跳到 RAGFlow 再看到登录页。
        # 兑换那一步会再换一次，用一个**新的** RAGFlow 会话 —— RAGFlow 允许同一账号
        # 多会话并存，两次换出来的 cookie 互不影响。
        ticket = context.store.create_hop_ticket(
            tenant_id=tenant_id,
            environment=settings.environment,
            workspace_id=workspace,
            ttl_seconds=settings.knowledge_hop_ttl_seconds,
        )
        return JSONResponse(
            {
                "target": (settings.ragflow_console_public_url or "").rstrip("/")
                + "/__aiops/knowledge/hop?ticket="
                + ticket,
                "tenant_id": tenant_id,
                "expires_in": int(settings.knowledge_hop_ttl_seconds),
            },
            status_code=status.HTTP_200_OK,
        )

    @app.get("/v1/internal/knowledge/hop", include_in_schema=False)
    def knowledge_hop(ticket: str = "") -> Response:
        """兑换一次性票：**在 kb.mall 这个域上**种会话 cookie，再 303 到 RAGFlow（#985）。

        这条由**浏览器**在 `kb.mall.qushiyun.com` 上调用（41 的 nginx 反代过来），
        所以它带不了内部令牌 —— 那它凭什么安全？

        * 票是 32 字节随机、60 秒、一次性，且**只在库里**与租户关联；
        * 票里没有会话：兑换时才用**那个租户自己的**凭据去换；
        * 票由内部令牌那条路由发出，所以"谁在什么时候拿过票"仍然有据可查。

        一个能拿到票的人，最多在 60 秒内让**自己**得到一个该租户的会话 —— 而他本来
        就已经通过了桥的身份校验（票是他自己那一跳换来的）。
        """
        from aiops_diagnostics.ragflow_console import (
            DeclaredTenant,
            RAGFlowLoginClient,
            RAGFlowLoginError,
            open_console,
            record,
        )

        settings = context.settings
        if (
            not settings.ragflow_console_base_url
            or not settings.ragflow_credential_key
            or not settings.ragflow_console_public_pem
        ):
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, "KNOWLEDGE_CONSOLE_DISABLED", "console access is not enabled"
            )
        consumed = context.store.consume_hop_ticket(ticket, environment=settings.environment)
        if consumed is None:
            # 不存在 / 过期 / 用过 / 环境不符 —— 四种同一个答案。区分它们等于给枚举票的
            # 人一个"这张存在过"的信号，而那与"这张还能用"只差几秒。
            raise StandardAPIError(
                status.HTTP_410_GONE,
                "KNOWLEDGE_HOP_EXPIRED",
                "this link has expired; go back to Dify and click the button again",
            )
        tenant_id = consumed["tenant_id"]
        client = RAGFlowLoginClient(settings.ragflow_console_base_url)
        try:
            target, session = open_console(
                store=context.store,
                environment=settings.environment,
                caller=DeclaredTenant(effective_tenant_id=tenant_id),
                credential_key=settings.ragflow_credential_key,
                client=client,
                public_base_url=settings.ragflow_console_public_url or settings.ragflow_console_base_url,
                public_pem_path=settings.ragflow_console_public_pem,
            )
        except RAGFlowLoginError as exc:
            record(
                context.store,
                tenant_id=tenant_id,
                environment=settings.environment,
                outcome="refused",
                reason_code=exc.code,
            )
            raise StandardAPIError(status.HTTP_424_FAILED_DEPENDENCY, exc.code, exc.message) from exc
        record(
            context.store,
            tenant_id=tenant_id,
            environment=settings.environment,
            outcome="granted",
            ragflow_email=session.ragflow_email,
        )
        # **这一次的 Set-Cookie 由 `kb.mall.qushiyun.com` 发出** —— 浏览器会保存它。
        response = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
        for name, value in session.cookies.items():
            response.set_cookie(
                name,
                value,
                domain=settings.ragflow_console_cookie_domain or None,
                path="/",
                httponly=True,
                samesite="lax",
            )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/v1/knowledge", include_in_schema=False)
    def knowledge_page() -> HTMLResponse:
        from importlib.resources import files

        html = files("aiops_diagnostics").joinpath("static/knowledge.html").read_text(encoding="utf-8")
        return HTMLResponse(content=html)

    @app.get("/v1/knowledge/available")
    def list_available_knowledge(
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        """The RAGFlow datasets THIS caller's tenant owns — the picker's source.

        Scoped by the caller's own tenant on purpose (ADR 0048): showing another
        tenant's libraries would make "which one do I tick" ambiguous at best
        and a cross-tenant grant at worst. A library nobody's tenant owns (the
        `admin@ragflow.io` datasets, e.g. one built straight from the RAGFlow
        console) is therefore invisible here by design — it has to be rebuilt
        under the tenant that should serve it, or bound by an engineer as an
        explicit exception.

        Checked rows come back with `bound=True`, which is what lets the page
        render "already attached" without a second request.
        """
        _require_knowledge_admin(caller)
        client = context.runtime.kb_search_client
        if client is None:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "KB_UNAVAILABLE",
                "knowledge base service is not configured",
                retryable=True,
            )
        tenant_id = caller.effective_tenant_id
        try:
            available = client.for_tenant(tenant_id).list_knowledge_bases()
        except Exception as exc:  # noqa: BLE001 - the surface reports, not crashes
            raise StandardAPIError(
                status.HTTP_502_BAD_GATEWAY,
                "KB_UNAVAILABLE",
                "knowledge base service is temporarily unavailable",
                retryable=True,
            ) from exc
        bound_ids = {
            kb_id
            for row in context.store.list_knowledge_id_bindings(tenant_id)
            for kb_id in row["knowledge_base_ids"]
        }
        return {
            "type": "knowledge_available",
            "tenant_id": tenant_id,
            "knowledge_bases": [
                {**item, "bound": item["knowledge_base_id"] in bound_ids} for item in available
            ],
        }

    @app.get("/v1/knowledge/sources")
    def list_knowledge_sources(
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        """The knowledge sources this tenant has registered — one per Dify entry."""
        _require_knowledge_admin(caller)
        rows = context.store.list_knowledge_id_bindings(caller.effective_tenant_id)
        return {
            "type": "knowledge_source_list",
            "count": len(rows),
            "sources": [
                {
                    "knowledge_id": row["knowledge_id"],
                    "tenant_id": row["tenant_id"],
                    "knowledge_base_ids": list(row["knowledge_base_ids"]),
                    "display_name": row["display_name"],
                    "created_by": row["created_by"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
        }

    @app.put("/v1/knowledge/sources/{knowledge_id}")
    def put_knowledge_source(
        knowledge_id: str,
        payload: KnowledgeSourceRequest,
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        """Create or replace one knowledge source, then make it live.

        Replacing rather than merging is what makes "untick a library" work at
        all: the submitted list is the intended state, not an increment.

        `tenant_id` in the body must equal the caller's own tenant. It is in the
        body because Dify's dialog asks the operator to label the source, and a
        silent rewrite would leave the page showing one thing and the database
        another — but a mismatch is refused rather than corrected, so nobody can
        bind their datasets to a tenant they are not acting as.
        """
        _require_knowledge_admin(caller)
        if not tenant_matches(payload.tenant_id, caller.effective_tenant_id):
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN,
                "KNOWLEDGE_TENANT_MISMATCH",
                "a knowledge source can only be registered for your own tenant",
            )
        if payload.knowledge_id != knowledge_id:
            raise StandardAPIError(
                status.HTTP_400_BAD_REQUEST,
                "KNOWLEDGE_ID_MISMATCH",
                "path knowledge_id and body knowledge_id disagree",
            )
        # **不能覆盖别人的知识源**（2026-10-22 负向真机暴露）。
        #
        # 上面那条 ``tenant_matches`` 只比较「请求体的 tenant 与调用者」，从不问
        # 「这个 knowledge_id 现在归谁」。于是 B 租户可以把 ``tenant_id`` 填成自己、
        # 路径填 A 的 knowledge_id，**成功改掉 A 的绑定行**（显式 200 + 新 owner）。
        # 危害不是越权读取，而是**破坏**：A 的知识源从此指向 B 的库、或名字被改，
        # A 的运营下次进 Dify 会 404 或查到别人的内容。
        #
        # ``DELETE`` 一直检查了拥有者，``PUT`` 没有 —— 同一对操作里的不对称就是这条缝。
        # 与 DELETE 同形：不区分「不存在」与「是别人的」，避免把 id 是否存在泄漏出去。
        existing = context.store.get_knowledge_id_binding(knowledge_id)
        if existing is not None and not tenant_matches(existing["tenant_id"], caller.effective_tenant_id):
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, "KNOWLEDGE_SOURCE_NOT_FOUND", "knowledge source not found"
            )
        row = context.store.set_knowledge_id_binding(
            knowledge_id,
            payload.tenant_id,
            tuple(payload.knowledge_base_ids),
            display_name=payload.display_name,
            created_by=str(getattr(caller, "subject_id", "") or ""),
        )
        # No cache to invalidate: the retrieval route re-reads the table on every
        # call, which is what makes this response mean "it is live now".
        return {
            "type": "knowledge_source",
            "knowledge_id": row["knowledge_id"],
            "tenant_id": row["tenant_id"],
            "knowledge_base_ids": list(row["knowledge_base_ids"]),
            "display_name": row["display_name"],
        }

    @app.delete("/v1/knowledge/sources/{knowledge_id}")
    def delete_knowledge_source(
        knowledge_id: str,
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        """Remove a source. Dify then gets 404 for it, which is the honest answer."""
        _require_knowledge_admin(caller)
        existing = context.store.get_knowledge_id_binding(knowledge_id)
        if existing is None or not tenant_matches(existing["tenant_id"], caller.effective_tenant_id):
            # Same shape for "never existed" and "someone else's": a 403 here
            # would confirm the id exists, which is a disclosure about other
            # tenants' configuration.
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, "KNOWLEDGE_SOURCE_NOT_FOUND", "knowledge source not found"
            )
        context.store.remove_knowledge_id_binding(knowledge_id)
        return {"type": "knowledge_source_deleted", "knowledge_id": knowledge_id}

    @app.get("/v1/banner")
    def list_banners(
        identity: tuple[ScopeContext, PlatformDecision] = Depends(shortcut_identity),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        """The chat-page banner for this caller's entry — static configuration.

        Same store, same lifecycle and same scope merge as the button listing
        (#588); what differs is the surface. This response contains **no
        order, no vehicle and no session-derived value**: it is identical for
        every caller of the same entry, which is the whole design. A client
        composes the personalized card itself from its own authenticated
        company calls; the moment this endpoint started answering per-person it
        would stop being an operations slot and become an authorization query,
        welding two things with completely different change rates into one
        response.

        A banner is a jump action: `jump_path` is used verbatim, never
        localized, and the server does not check that the page exists
        (ADR-0006). `question_template` is deliberately absent — a jump action
        never reaches the unified assistant entry.

        An entry with no published banner is an **empty list, not a 404**: "no
        banner configured" is a normal state, and the client's rule is to draw
        nothing rather than half a card.
        """
        caller, decision = identity
        try:
            banners = context.shortcut_manager.list_effective(
                caller, business_entry=decision.platform, kind=BANNER_KIND
            )
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return {
            "type": "banner",
            # Same rule as the shortcut listing: a list-level language is only
            # truthful when every row agrees, and the per-row `language` is
            # what a client reads otherwise.
            "language": _shortcut_list_language(banners, language),
            "count": len(banners),
            "banners": [item.banner(language) for item in banners],
        }

    @app.post("/v1/shortcuts", status_code=status.HTTP_201_CREATED)
    def create_shortcut(
        payload: ShortcutCreateRequest,
        scope: Annotated[str, Query()] = TENANT_SCOPE,
        caller: ScopeContext = Depends(authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            shortcut = context.shortcut_manager.create(caller, payload.model_dump(), scope=scope)
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return shortcut.to_dict()

    @app.get("/v1/shortcut-admin/{business_entry}")
    def list_shortcut_admin(
        business_entry: str,
        scope: Annotated[str, Query()] = TENANT_SCOPE,
        caller: ScopeContext = Depends(authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        """Management listing (all statuses, full multilingual fields)."""
        try:
            shortcuts = context.shortcut_manager.list(caller, business_entry=business_entry, scope=scope)
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return {
            "type": "shortcut_admin_list",
            "count": len(shortcuts),
            "shortcuts": [s.to_dict() for s in shortcuts],
        }

    @app.get("/v1/shortcuts/{shortcut_id}/versions/{version_no}")
    def get_shortcut_version(
        shortcut_id: str,
        version_no: int,
        scope: Annotated[str, Query()] = TENANT_SCOPE,
        caller: ScopeContext = Depends(authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            version = context.shortcut_manager.version(caller, shortcut_id, version_no, scope=scope)
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return version.to_dict()

    @app.put("/v1/shortcuts/{shortcut_id}")
    def update_shortcut(
        shortcut_id: str,
        payload: ShortcutUpdateRequest,
        scope: Annotated[str, Query()] = TENANT_SCOPE,
        caller: ScopeContext = Depends(authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            shortcut = context.shortcut_manager.update(caller, shortcut_id, payload.model_dump(), scope=scope)
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return shortcut.to_dict()

    @app.post("/v1/shortcuts/{shortcut_id}/publish")
    def publish_shortcut(
        shortcut_id: str,
        payload: ShortcutActionRequest,
        scope: Annotated[str, Query()] = TENANT_SCOPE,
        caller: ScopeContext = Depends(authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            version = context.shortcut_manager.publish(
                caller, shortcut_id, expected_revision=payload.expected_revision, scope=scope
            )
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return version.to_dict()

    @app.post("/v1/shortcuts/{shortcut_id}/draft")
    def fork_shortcut_draft(
        shortcut_id: str,
        payload: ShortcutActionRequest,
        scope: Annotated[str, Query()] = TENANT_SCOPE,
        caller: ScopeContext = Depends(authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            shortcut = context.shortcut_manager.fork_draft(
                caller, shortcut_id, expected_revision=payload.expected_revision, scope=scope
            )
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return shortcut.to_dict()

    @app.post("/v1/shortcuts/{shortcut_id}/rollback")
    def rollback_shortcut(
        shortcut_id: str,
        payload: ShortcutRollbackRequest,
        scope: Annotated[str, Query()] = TENANT_SCOPE,
        caller: ScopeContext = Depends(authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            version = context.shortcut_manager.rollback(
                caller,
                shortcut_id,
                version_no=payload.version_no,
                expected_revision=payload.expected_revision,
                scope=scope,
            )
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return version.to_dict()

    @app.post("/v1/shortcut-admin/{business_entry}/{code}/suppress")
    def suppress_shortcut(
        business_entry: str,
        code: str,
        caller: ScopeContext = Depends(authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            shortcut = context.shortcut_manager.suppress(caller, business_entry=business_entry, code=code)
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return shortcut.to_dict()

    @app.post("/v1/shortcut-admin/{business_entry}/{code}/restore")
    def restore_shortcut(
        business_entry: str,
        code: str,
        caller: ScopeContext = Depends(authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            shortcut = context.shortcut_manager.restore(caller, business_entry=business_entry, code=code)
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return shortcut.to_dict()

    @app.post("/v1/shortcuts/{shortcut_id}/disable")
    def disable_shortcut(
        shortcut_id: str,
        payload: ShortcutActionRequest,
        scope: Annotated[str, Query()] = TENANT_SCOPE,
        caller: ScopeContext = Depends(authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            shortcut = context.shortcut_manager.disable(
                caller, shortcut_id, expected_revision=payload.expected_revision, scope=scope
            )
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return shortcut.to_dict()

    @app.delete("/v1/shortcuts/{shortcut_id}")
    def delete_shortcut(
        shortcut_id: str,
        payload: ShortcutActionRequest,
        scope: Annotated[str, Query()] = TENANT_SCOPE,
        caller: ScopeContext = Depends(authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, bool]:
        try:
            context.shortcut_manager.delete(
                caller, shortcut_id, expected_revision=payload.expected_revision, scope=scope
            )
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return {"deleted": True}

    @app.get("/v1/agent-metrics/summary")
    def agent_metrics_summary(
        agent_id: Annotated[str | None, Query(max_length=128)] = None,
        window_hours: Annotated[int, Query(ge=1, le=720)] = 720,
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        """Tenant-scoped redacted run aggregate (T7/#174).

        The tenant always comes from the authenticated scope; agent_id
        optionally narrows to one agent. Cross-tenant data is unreachable by
        construction, and rows never carry question/answer/prompt text.
        """
        from aiops_diagnostics.agent_lifecycle import VIEW_ROLES

        if not frozenset(getattr(caller, "roles", ())).intersection(VIEW_ROLES):
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_FORBIDDEN", "agent access is not permitted"
            )
        try:
            return context.runtime.get_agent_metrics_summary(
                caller, agent_id=agent_id, window_hours=window_hours
            )
        except AgentRuntimeError as exc:
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_FORBIDDEN", "agent access is not permitted"
            ) from exc
        except (ValueError, MetricsValidationError) as exc:
            raise StandardAPIError(status.HTTP_422_UNPROCESSABLE_ENTITY, "METRICS_INVALID", str(exc)) from exc

    @app.get("/v1/agent-metrics/runs")
    def agent_metrics_runs(
        agent_id: Annotated[str | None, Query(max_length=128)] = None,
        route_type: Annotated[str | None, Query(max_length=16)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        """Recent redacted run rows (T7/#174) for the caller's tenant only."""
        from aiops_diagnostics.agent_lifecycle import VIEW_ROLES

        if not frozenset(getattr(caller, "roles", ())).intersection(VIEW_ROLES):
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_FORBIDDEN", "agent access is not permitted"
            )
        try:
            runs = context.runtime.list_agent_metrics_runs(
                caller, agent_id=agent_id, route_type=route_type, limit=limit
            )
        except AgentRuntimeError as exc:
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_FORBIDDEN", "agent access is not permitted"
            ) from exc
        except (ValueError, MetricsValidationError) as exc:
            raise StandardAPIError(status.HTTP_422_UNPROCESSABLE_ENTITY, "METRICS_INVALID", str(exc)) from exc
        return {"runs": runs}

    @app.post("/v1/enroll", status_code=status.HTTP_201_CREATED)
    def enroll(payload: EnrollRequest) -> dict[str, Any]:
        try:
            result = context.store.redeem_enrollment(
                payload.code,
                device_name=payload.device_name,
                platform=payload.platform,
            )
        except (EnrollmentError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return {
            "device_id": result.device.device_id,
            "workspace_id": result.device.workspace_id,
            "tenant_id": result.device.tenant_id,
            "token": result.token,
        }

    @app.post("/v1/runs", status_code=status.HTTP_202_ACCEPTED)
    def create_run(
        payload: RunCreateRequest,
        device: GatewayDevice = Depends(authenticated_device),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            return context.runtime.start_run(
                device,
                problem=payload.problem,
                order_no=payload.order_no,
                tenant_id=payload.tenant_id,
                key_slot=payload.key_slot,
                provider=payload.provider,
                fixture_name=payload.fixture_name,
            )
        except DeviceTenantError as exc:
            # The entry guard is one definition inside the runtime (#331); this
            # layer only maps it. A request naming a tenant outside the enrolled
            # device scope is therefore refused exactly once, with one status
            # code and one error, instead of 403 here and 400 from the runtime.
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.get("/v1/runs")
    def list_runs(
        device: GatewayDevice = Depends(authenticated_device),  # noqa: B008
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        return {"runs": context.store.list_runs(device.workspace_id, limit=limit)}

    @app.get("/v1/runs/{run_id}")
    def get_run(
        run_id: str,
        device: GatewayDevice = Depends(authenticated_device),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            return context.store.get_run(run_id, device.workspace_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found") from exc

    @app.get("/v1/runs/{run_id}/events")
    def list_events(
        run_id: str,
        device: GatewayDevice = Depends(authenticated_device),  # noqa: B008
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> dict[str, Any]:
        try:
            events = context.store.list_events(
                run_id,
                device.workspace_id,
                after=after,
                limit=limit,
            )
        except RunNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found") from exc
        return {
            "events": events,
            "next_after": events[-1]["sequence"] if events else after,
        }

    @app.get("/v1/runs/{run_id}/evidence")
    def list_run_evidence(
        run_id: str,
        device: GatewayDevice = Depends(authenticated_device),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            context.store.get_run(run_id, device.workspace_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found") from exc
        return {"evidence": context.runtime.list_evidence(run_id)}

    @app.get("/v1/runs/{run_id}/events/stream")
    async def stream_events(
        request: Request,
        run_id: str,
        device: GatewayDevice = Depends(authenticated_device),  # noqa: B008
        after: Annotated[int, Query(ge=0)] = 0,
    ) -> StreamingResponse:
        try:
            context.store.get_run(run_id, device.workspace_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found") from exc

        async def event_stream():
            sequence = after
            while not await request.is_disconnected():
                events = context.store.list_events(
                    run_id,
                    device.workspace_id,
                    after=sequence,
                    limit=200,
                )
                for event in events:
                    sequence = int(event["sequence"])
                    yield _sse(event)
                run = context.store.get_run(run_id, device.workspace_id)
                if run["status"] in TERMINAL_RUN_STATUSES and not events:
                    yield _sse({"sequence": sequence, "type": "stream_end", "status": run["status"]})
                    return
                await asyncio.sleep(context.settings.event_poll_interval_seconds)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return app


def _sse(event: dict[str, Any]) -> str:
    sequence = int(event.get("sequence", 0))
    event_type = str(event.get("type", "message"))
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"id: {sequence}\nevent: {event_type}\ndata: {data}\n\n"


def _inside_credential(runtime: Settings) -> str:
    """会话身份的 UPMS 服务侧内部凭据（C→B 映射与运营商站点范围共用同一套前提）。

    两个接缝的配置前提逐字相同（UPMS 地址 + 该凭据），所以只写一遍：往其中一个
    加条件时不会漏掉另一个。
    """
    return (runtime.upms.inside_token or "").strip()


def _b_subject_directory(runtime: Settings) -> BSubjectDirectory | None:
    """会话身份的 C→B 映射目录（#424）。

    复用 UPMS 既有端点；未配置 UPMS 地址或服务侧内部凭据时返回 ``None``——会话身份
    只保留 C 侧部分，需要 B 端主体的路径按 fail closed 拒绝，消费者端不受影响。
    """
    credential = _inside_credential(runtime)
    if not runtime.upms.base_url or not credential:
        return None
    return UpmsBSubjectDirectory(runtime.upms, credential)


def _shop_directory(runtime: Settings) -> ShopDirectory | None:
    """令牌不带 ``shop_ids`` 时的店铺归属来源（公司权威端点）。

    与 C→B 映射、运营商站点范围共用**同一套配置前提**（UPMS 地址 + 服务侧内部凭据）：
    未配置时返回 ``None``，此时令牌缺 ``shop_ids`` 一律拒绝（拿不到归属就是拿不到范围）。
    """
    credential = _inside_credential(runtime)
    if not runtime.upms.base_url or not credential:
        return None
    return UpmsShopDirectory(runtime.upms, credential)


def _operator_site_scope(runtime: Settings) -> OperatorSiteScope | None:
    """会话身份的运营商站点范围（#426，四跳链的后两跳）。

    与 C→B 映射共用同一套配置前提（UPMS 地址 + 服务侧内部凭据）：未配置时返回
    ``None``，会话数据范围保持 ``self``——管家端因此只能看到本人的单（fail closed，
    不放宽），消费者端不变。站点归属映射复用受限直连的同一条跳板隧道，不为授权
    另开一条连接路径。
    """
    credential = _inside_credential(runtime)
    if not runtime.upms.base_url or not credential:
        return None
    return UpmsOperatorSiteScope(runtime, credential)


def _caller_resolver(settings: GatewayServerSettings) -> CallerContextResolver:
    # #444 / ADR-0009 D9：来源密钥这道门**装在会话那一级之前**。这是本票最关键的一处，
    # 也是 #443 记录的两条边界里那条「必须放在会话之前」的落点：会话那一级是
    # ``if settings.third_session_service_token:`` 且**直接 return**，而 41 现网那个键有值 ——
    # 门若排在它之后，带公司令牌、不带 ``third-session`` 的请求会在第一级就被 401，
    # 新路径在现网**永远不可达**，即便把密钥配上也一样。
    #
    # 未配置来源密钥 ⇒ 这一整段不参与（新链路整体不启用，fail closed；不是放行），
    # 下面的选择链与本票之前逐字一致；清空即回滚。
    if settings.company_source_key:
        company = _company_token_resolver(settings)
        if company is not None:
            return SourceKeyCallerResolver(
                settings.company_source_key,
                company,
                _fallback_caller_resolver(settings),
            )
    return _fallback_caller_resolver(settings)


def _company_token_resolver(settings: GatewayServerSettings) -> CompanyTokenCallerResolver | None:
    """新链路的解析器（#443 的实现），只在来源密钥已配置时构造。

    构造失败（配置读不出来）返回 ``None`` ⇒ 调用方退回既有链：带密钥的请求随后按既有链被
    拒绝（401），而不是被放行 —— 这是 fail closed 的方向。半配置（只设 URL 或只设密钥）
    已在 ``GatewayServerSettings.validate`` 里变成启动失败，不走这里。
    """
    # 两条通道二选一：配了签名密钥就走本地验签（A2 / #448），否则走远端 check_token。
    if not settings.company_check_token_url and not settings.company_jwt_key:
        return None
    try:
        runtime = Settings.from_config(settings.server_config_file)
        return CompanyTokenCallerResolver(
            CompanyTokenSettings(
                url=settings.company_check_token_url,
                client_id=settings.company_token_client_id,
                client_secret=settings.company_token_client_secret,
                signature_key=settings.company_jwt_key,
                trust_company_payload=settings.trust_company_payload,
            ),
            runtime,
            shop_directory=_shop_directory(runtime),
            permission_source=_permission_source(runtime),
        )
    except (ValueError, OSError):
        return None


def _permission_source(runtime: Settings) -> PermissionSource | None:
    """公司权限码 → 内部角色（#926）。

    **未配置 UPMS 时不构造**（返回 ``None``），解析器随即保持空角色集 —— 与改动前逐字一致。
    这样「哪些部署会因此多拿到权限」是一个看配置就能回答的问题。
    """
    if not (runtime.upms.base_url or "").strip():
        return None
    from aiops_diagnostics.company_permissions import CompanyPermissionSource

    return CompanyPermissionSource(runtime.upms)


def _fallback_caller_resolver(settings: GatewayServerSettings) -> CallerContextResolver:
    """既有链：会话 → UPMS 兜底 → introspection。选择结果与新增本票之前逐字一致。"""
    if settings.third_session_service_token:
        try:
            runtime = Settings.from_config(settings.server_config_file)
            return RedisThirdSessionResolver(
                ThirdSessionSettings(
                    host=runtime.redis.host,
                    port=runtime.redis.port,
                    database=runtime.redis.database,
                    username=runtime.redis.user,
                    password=runtime.redis.password,
                    service_token=settings.third_session_service_token,
                    key_prefix=settings.third_session_key_prefix,
                ),
                b_subject_directory=_b_subject_directory(runtime),
                operator_scope=_operator_site_scope(runtime),
            )
        except (ValueError, OSError):
            return DisabledCallerResolver()
    # #444 起，公司令牌路径**不再出现在这条链里**：它由上面的来源密钥门分派，只有密钥验过的
    # 请求才走得到。把这四种选择结果写清楚，是为了让「带错密钥」的行为可预期 —— 它会落到这里，
    # 按会话/introspection/UPMS 处理公司令牌，结果是被拒绝（401），而不是被放行。
    if not settings.introspection_url:
        try:
            return UpmsCallerResolver(Settings.from_config(settings.server_config_file))
        except (ValueError, OSError):
            return DisabledCallerResolver()
    return IntrospectionCallerResolver(
        IntrospectionSettings(
            url=settings.introspection_url,
            client_id=settings.introspection_client_id,
            client_secret=settings.introspection_client_secret,
            audience=settings.standard_api_audience,
            timeout_seconds=settings.introspection_timeout_seconds,
        )
    )


def _debug_public_error(error: Exception) -> str:
    """Bounded, redacted reason for a failed debug-run (server stays opaque)."""
    from aiops_diagnostics.redaction import redact_text

    message = redact_text(str(error))
    return message[:500] if message else error.__class__.__name__


#: The platform decision's failure modes and the answer each one gets. One
#: definition: the three copies this replaces had already drifted apart once,
#: and an error taxonomy with two answers is an error taxonomy with none.
_PLATFORM_ERROR_STATUS = {
    PLATFORM_AMBIGUOUS: status.HTTP_409_CONFLICT,
    PLATFORM_FORBIDDEN: status.HTTP_403_FORBIDDEN,
    PLATFORM_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _platform_error(exc: FAQError) -> StandardAPIError:
    """``FAQError`` (a platform-decision failure) as the response it deserves."""
    return StandardAPIError(
        _PLATFORM_ERROR_STATUS.get(exc.code, status.HTTP_503_SERVICE_UNAVAILABLE),
        exc.code,
        str(exc),
        retryable=exc.code == PLATFORM_UNAVAILABLE,
    )


def _authenticate_caller(
    resolver: CallerContextResolver,
    authorization: str | None,
    third_session: str | None,
    *,
    required_scope: str,
    platform_entry: str | None = None,
    source_key: str | None = None,
) -> ScopeContext:
    """The one place a ``CallerAuthError`` becomes an outbound error, and the
    one place the resolver is asked for an identity.

    Every ``authenticated_*`` dependency delegates here. There used to be three
    copies of this mapping and one of them had no ``CALLER_AUTH_CONFIG_MISSING``
    branch, so a missing gateway configuration told twelve endpoints' callers
    "your login has expired, sign in again" — the exact opposite of what
    ``docs/gateway.md`` promises (a stable 503, fail closed). A rule with two
    answers is a rule with none, and the copy that drifted was the one nobody
    looked at.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise StandardAPIError(status.HTTP_401_UNAUTHORIZED, "ACCESS_TOKEN_REQUIRED", "access token required")
    token = authorization.removeprefix("Bearer ").strip()
    if token.startswith("aops_"):
        raise StandardAPIError(
            status.HTTP_401_UNAUTHORIZED,
            "INVALID_ACCESS_TOKEN",
            "access token validation failed",
        )
    try:
        # ``platform_entry`` 与 ``source_key`` 都在这里**无条件**传下去，不走「带没带会话」的
        # 分支：解析器各自忽略对自己无意义的参数，但分支之间一旦不对称，「同一个请求多带一个头
        # 就换了范围语义/身份来源」就会变成排查不了的形状（同 ``authenticated_diagnosis_caller``
        # 那条注释）。``source_key`` 尤其如此：它是分派依据，缺它门就永远落到既有链。
        return resolver.resolve(
            token,
            required_scope=required_scope,
            third_session=third_session,
            platform_entry=platform_entry,
            source_key=source_key,
        )
    except CallerAuthError as exc:
        if exc.code == CALLER_AUTH_FORBIDDEN:
            status_code, code, retryable = status.HTTP_403_FORBIDDEN, "INSUFFICIENT_SCOPE", False
        elif exc.code == CALLER_AUTH_CONFIG_MISSING:
            # Explicitly retryable, rather than relying on whoever raised it to
            # have passed `retryable=True`: `CallerAuthError`'s default is
            # False, and a configuration gap is by definition something a fix
            # restores. This is the rule deciding, not the raiser remembering.
            status_code, code = status.HTTP_503_SERVICE_UNAVAILABLE, "ACCESS_TOKEN_VALIDATION_UNAVAILABLE"
            retryable = True
        else:
            status_code = (
                status.HTTP_503_SERVICE_UNAVAILABLE if exc.retryable else status.HTTP_401_UNAUTHORIZED
            )
            code = "ACCESS_TOKEN_VALIDATION_UNAVAILABLE" if exc.retryable else "INVALID_ACCESS_TOKEN"
            retryable = exc.retryable
        raise StandardAPIError(
            status_code, code, "access token validation failed", retryable=retryable
        ) from exc


def _health_job_response(job: dict[str, Any]) -> dict[str, Any]:
    status_value = str(job["status"])
    return {
        "job_id": job["job_id"],
        "order_no": job["order_no"],
        "rule_version": job["rule_version"],
        "status": status_value,
        "retry_after_ms": 1000 if status_value in ACTIVE_HEALTH_JOB_STATUSES else None,
        # The report's prose language: the language the JOB was created in.
        # It describes the stored `report`, not this request — a poller cannot
        # relabel a report it did not create (same rule as the diagnosis
        # surface; see `_standard_diagnosis_response`).
        "language": job.get("language") or "zh",
        "report": job.get("report"),
        "error": (
            {
                "code": job.get("error_code") or "REPORT_FAILED",
                "message": job.get("error_message") or "health report failed",
                # A restart is retryable for the caller: the report never ran,
                # and asking again is exactly what should happen. Without it
                # here, the frontend shows an error with no action — which is
                # the one thing the restart verdict is supposed to avoid.
                "retryable": job.get("error_code")
                in {"SOURCE_UNAVAILABLE", "REPORT_TIMEOUT", "REPORT_INTERRUPTED_BY_RESTART"},
            }
            if status_value == "failed"
            else None
        ),
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "completed_at": job.get("completed_at"),
    }


_FAQ_GENERIC_CHARS = frozenset(
    {
        # Sentence-final / filler CJK that carries almost no discriminative
        # signal in this catalog. Without dropping these, "怎么办" (which ends
        # nearly every consumer title) swamps the matcher and every question
        # collapses onto the first title.
        "的",
        "了",
        "吗",
        "呢",
        "呀",
        "啊",
        "在",
        "有",
        "可",
        "以",
        "能",
        "会",
        "是",
        "怎",
        "么",
        "办",
        "如",
        "何",
        "为",
        "什",
        "一",
        "下",
        "不",
        "没",
        "很",
        "请",
    }
)
# Fraction of the question's significant chars that must appear in the title.
# Containment (not raw overlap) prevents a generic question ("续航里程...")
# from matching a title merely by sharing common chars (充/电/程) across many
# entries. A query that shares only a single char (e.g. just "枪") must not
# be enough; require at least MIN_OVERLAP distinct chars too.
_FAQ_MIN_CONTAINMENT = 0.5
_FAQ_MIN_OVERLAP = 2

# What makes a keyword match self-evident is not a score: it is whether the
# question's significant tokens ARE one of the entry's title variants — token-set
# equality, with no threshold to tune.
#
# Arriving at this took two measured corrections, both from real runs:
#
# 1. A question-LENGTH bar was tried first and failed, because the compressed
#    en/de/fr/es/pt titles are short too, so length cannot tell a short title
#    from a short question. It sent `Connector Stuck? Emergency Cable Release
#    Guide` — q010's own title — to the routing intent, where Jev read the word
#    "Guide" as solution_discovery and dropped the entry (5 of 185 variants).
# 2. A variant-containment bar (question covers most of the variant AND the
#    variant covers most of the question, at 0.8) fixed that but kept letting
#    ONE-TOKEN REVERSALS through, because a reversal changes a single token and
#    lands just above the bar: `Why Did Charging Stop Normally?` scores 0.80
#    against "…Unexpectedly?", and `Why Is Charging Power Faster Than
#    Advertised?` scores 0.86 against the "Slower" title. Both were answered
#    from the FAQ, and the token that differed was the whole question — a user
#    asking why charging was FAST was told about slow charging.
#
# Equality has no such gap, and the measurements say it costs nothing: all 185
# catalog variants (in all six languages) are exactly their own token set, while
# every near-miss fails — the two reversals, a prefixed title (0.6 on the old
# bar), and the false positives. So the catalog is answered instantly, and
# anything that is merely CLOSE to an entry is checked by the routing intent.


#: Intents that say "the catalog is not where this question belongs", so a
#: question that merely OVERLAPS a title must not be answered from it.
#:
#: One intent, and the narrowness is the point. `casual` is the defect this
#: ticket was opened for: "今天天气怎么样" reached the summer-heat entry on the
#: single shared word 天气, and every non-charging question of that shape —
#: 今天吃什么, 股票怎么样 — reaches some entry the same way. Jev says `casual`
#: for all of them, and there is no legitimate short question it mislabels.
#:
#: CASE AND SOLUTION INTENTS ARE DELIBERATELY ABSENT, and the first version of
#: this fix had them. They bought exactly one thing — "重卡充电案例" reaching the
#: RFID-card entry, the sole marginal match among 86 real questions — and cost a
#: class of false suppressions, because the model reads the wording of the
#: catalog's OWN titles as promotional: "Guide" in q010's `Connector Stuck?
#: Emergency Cable Release Guide`, "SOP" in q023's `Vehicle Scratch or Equipment
#: Damage Incident SOP`. A user prefacing q010's title with "Please show me the"
#: scored 0.6, Jev said solution_discovery, and the entry was dropped in favour
#: of a promotional card.
#:
#: Suppressing them was never necessary for promo routing: a genuine request
#: NAMES it ("客户案例" / "行业解决方案" / "Please show me a customer case"), which the
#: cue matcher upstream already catches, and anything that does reach here with a
#: case/solution intent is sent to the promotional path by the routing block
#: below — which runs after this one and needs no help from the suppression set.
#: The residual is "重卡充电案例" continuing to answer from q009; the fix for that
#: is a promotional cue, not a FAQ suppressor, and it is tracked separately.
#:
_FAQ_SUPPRESSING_INTENTS = frozenset({"casual"})


def _normalize_keywords(text: str) -> set[str]:
    """Deterministic CJK char set for FAQ short-circuiting.

    Keeps significant single CJK chars (generic filler/sentence-tail dropped)
    plus whole Latin runs. Used for char-set containment against a candidate
    title: the question's distinctive chars must mostly be present in the
    title, which isolates genuinely related titles without a real tokenizer
    while resisting the "怎么办" tail and shared single-char noise.
    """
    lowered = text.lower()
    significant: set[str] = set()
    latin = ""
    for ch in lowered:
        if "一" <= ch <= "鿿":
            if latin:
                significant.add(latin)
                latin = ""
            if ch not in _FAQ_GENERIC_CHARS:
                significant.add(ch)
        elif ch.isalnum():
            latin += ch
        else:
            if latin:
                significant.add(latin)
                latin = ""
    if latin:
        significant.add(latin)
    return significant


def _classify_for_routing(
    context: Any,
    question: str,
    *,
    language: str,
    tenant_id: str | None,
) -> dict[str, Any] | None:
    """The routing decision, or None when none could be obtained.

    Lifted out of the handler because two branches now need the same answer and
    one request must not pay for it twice: the FAQ short-circuit consults it for
    a marginal match, and the routing below consults it for everything the FAQ
    branch did not take. Resolving it on both paths is what keeps the FAQ
    disambiguation free rather than an added model call.

    ``None`` is a first-class outcome. A routing hint is an optimisation and
    must never be the reason a user gets nothing (#392), so both a missing
    runtime hook and a raise from one are "no decision" rather than an error.
    """
    classifier = getattr(context.runtime, "classify_lightweight", None)
    if classifier is None:
        return None
    try:
        return classifier(question, language=language, tenant_id=tenant_id)
    except (ValueError, RuntimeError) as exc:
        # Treated as "no decision", exactly as a None return is.
        #
        # But it must not be swallowed in silence either: this is the outermost
        # handler, so a defect that escapes the routing module's own vocabulary
        # would otherwise disable routing with no trace anywhere — which is
        # precisely how this component stayed broken unnoticed before. Logged,
        # no metric (the routing module counts the failures it recognises), and
        # never the user's text.
        _LOGGER.warning("classifier raised outside its own fallback: %s", type(exc).__name__)
        return None


def _faq_match(
    platform: str,
    faq_catalog: FAQCatalog,
    question: str,
) -> tuple[str | None, bool]:
    """Best matching entry, and whether it may be answered only on those words.

    Returns ``(question_id, confident)``. ``confident`` is True when the question's
    tokens ARE one of the entry's own titles, so it is answered without a second
    opinion; False means the question merely OVERLAPS a title and the routing
    intent should confirm it before the catalog answers (see the note above).

    Set-containment over the union of a title's language variants: score =
    |question_sig ∩ title_union|, with a containment bar on the same union, so
    the question's significant tokens must sit mostly inside ONE entry across
    its languages. The scoring stays deterministic and zero-model; what this
    returns is whether those words can be trusted on their own.
    """
    qsigs = _normalize_keywords(question)
    if not qsigs:
        return None, False
    # Signature each title variant once. The union below is the union of THESE
    # signatures, not a second tokenization pass over the same strings — this
    # runs on every assistant request, and `_normalize_keywords` walks the text
    # character by character.
    variants = {
        entry["question_id"]: tuple(
            sig
            for sig in (
                _normalize_keywords(variant)
                for variant in faq_catalog.title_variants(platform, entry["question_id"])
            )
            if sig
        )
        for entry in faq_catalog.catalog(platform)
    }
    unions = {
        question_id: frozenset().union(*sigs) if sigs else frozenset()
        for question_id, sigs in variants.items()
    }
    best_qid: str | None = None
    best_overlap = 0
    for question_id, title_sigs in unions.items():
        overlap = len(qsigs & title_sigs)
        if overlap > best_overlap:
            best_overlap = overlap
            best_qid = question_id
    # Require at least MIN_OVERLAP distinct tokens AND a strong containment,
    # so single-token ties ("枪") or generic overlap ("充/电/程") never fire.
    if best_overlap < _FAQ_MIN_OVERLAP or best_qid is None:
        return None, False
    containment = len(qsigs & unions[best_qid]) / len(qsigs)
    if containment < _FAQ_MIN_CONTAINMENT:
        return None, False
    return best_qid, any(qsigs == variant for variant in variants[best_qid])


# A candidate order token: starts AND ends on an alphanumeric, so a trailing
# sentence period or a label colon is not swallowed into the id, and carries
# 6-64 safe characters total.
_ORDER_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9][A-Za-z0-9_.:-]{4,62}[A-Za-z0-9](?![A-Za-z0-9])")


def _extract_order_no(text: str) -> str | None:
    """Extract the first plausible order number from free text, or None.

    Order numbers look like long alphanumeric/token runs (e.g. 19-digit
    IDs or dashed codes). We match standalone tokens of 6-64 safe chars
    (letters/digits/_.:-), which is a much stronger signal than bare digits
    (which would also hit phone numbers). This is a cheap deterministic first
    pass; a model disambiguation layer can refine it later.

    A candidate MUST carry at least one digit. Without that rule the scan
    returns the first ordinary WORD of a Latin-script question — `Please
    check charging anomalies for order (2098…)` yielded ``Please``, so the
    order was never found and the request silently degraded to a zero-order
    answer (41 live, 2026-09-20). Chinese hid the defect: CJK characters are
    outside the token class, so the scan skipped them and landed on the bare
    order number. Requiring a digit is what tells an identifier apart from a
    word; a real order number that carries none does not exist in this
    system.
    """
    if not text:
        return None
    for match in _ORDER_TOKEN.finditer(text):
        candidate = match.group(0)
        if not any(char.isdigit() for char in candidate):
            continue
        # Skip pure-digit tokens that are too short to be order ids and could
        # be phone numbers (<6 digits already excluded by length, but a long
        # pure-digit run like a phone would be 11 digits — still ambiguous;
        # order numbers in this system are >=15 chars). To be conservative,
        # only treat pure-digit runs >=15 as order candidates.
        if candidate.isdigit() and len(candidate) < 15:
            continue
        return candidate
    return None


def _assistant_question_response(qa: dict[str, Any], language: str) -> dict[str, Any]:
    """The one public shape of an assistant-question job.

    Poll and cancel return the same body on purpose: the job's state is the
    single thing both surfaces report, so a client reads a stopped job exactly
    as it reads a finished one.

    The failure message is customer-facing copy, not the job record's internal
    reason. The record keeps that reason deliberately — a contract violation is
    a defect an engineer must be able to see, and replacing it there once turned
    a real bug into "service temporarily unavailable" (2026-09-17). But the
    record is read by an engineer and this response is read by a customer; the
    two need different sentences, and only the boundary can tell them apart. A
    Chinese-speaking user was shown "customer QA turn returned invalid JSON",
    which names neither the problem nor an action.
    """
    status_value = str(qa["status"])
    return {
        "type": "qa",
        # The EFFECTIVE language: a non-promptable request is answered in the
        # default, so reporting the raw request tag would describe text the
        # answer does not contain (#541 review).
        "language": effective_language(language),
        "qa_id": qa["qa_id"],
        "question": qa["question"],
        "status": status_value,
        "retry_after_ms": 1000 if status_value in ACTIVE_DIAGNOSIS_STATUSES else None,
        "result": qa.get("result"),
        "error": (
            {
                "code": qa.get("error_code") or "QA_FAILED",
                "message": _qa_user_message(language, qa.get("error_code")),
                "retryable": True,
            }
            if status_value in {QUESTION.failed, QUESTION.expired}
            else None
        ),
    }


#: The only failure code that means retrieval actually failed. Everything else
#: on this surface — a provider error, a contract violation, an unreachable
#: model — records `QA_FAILED` and has nothing to do with the knowledge base.
_KB_FAILURE_CODE = "KB_UNAVAILABLE"


def _qa_user_message(language: str, error_code: str | None) -> str:
    """The localized, actionable sentence a customer sees when a QA job fails.

    The cause decides the sentence. Claiming the knowledge base is unavailable
    for every failure told a customer whose model provider had simply rejected
    the request that the *library* was down — a false cause and useless
    guidance, since retrying is the wrong advice only for the reader who
    believes the wrong thing is broken. Only a verified retrieval failure gets
    the retrieval copy; the rest get the honest, cause-neutral one.
    """
    pack = QA_FALLBACK_MESSAGES.get(language) or QA_FALLBACK_MESSAGES[DEFAULT_LANGUAGE]
    return pack["unavailable"] if error_code == _KB_FAILURE_CODE else pack["generation_failed"]


def _require_conversation(
    context: Any,
    caller: ScopeContext,
    conversation_id: str,
) -> dict[str, Any]:
    """Load a scope-owned conversation or raise the uniform 404."""
    try:
        return context.conversation_store.get(conversation_id, caller.scope_fingerprint)
    except ConversationError as exc:
        raise StandardAPIError(
            status.HTTP_404_NOT_FOUND,
            "CONVERSATION_NOT_FOUND",
            "conversation not found",
        ) from exc


def _conversation_response(conversation: dict[str, Any]) -> dict[str, Any]:
    """Public conversation shape (no internal fingerprints)."""
    return {
        "conversation_id": conversation["conversation_id"],
        "business_entry": conversation["business_entry"],
        "agent_version_key": conversation["agent_version_key"],
        "active_order_no": conversation["active_order_no"],
        "is_generating": conversation["is_generating"],
        "created_at": conversation["created_at"],
        "updated_at": conversation["updated_at"],
        "expires_at": conversation["expires_at"],
    }


def _resolve_conversation(
    context: Any,
    caller: ScopeContext,
    decision: Any,
    conversation_id: str,
) -> dict[str, Any]:
    """Load the conversation for this caller, enforcing scope AND entry.

    Entry is part of the binding (T4/#172): the same user switching from the
    consumer entry to the operator entry gets a fresh conversation set, and a
    mismatched or missing conversation is a uniform 404 — indistinguishable
    from never-existing.
    """
    try:
        conversation = context.conversation_store.get(conversation_id, caller.scope_fingerprint)
    except ConversationError as exc:
        raise StandardAPIError(
            status.HTTP_404_NOT_FOUND,
            "CONVERSATION_NOT_FOUND",
            "conversation not found",
        ) from exc
    if conversation.get("business_entry") != str(getattr(decision, "platform", "")):
        # The platform decision IS the resolved business entry for this call.
        raise StandardAPIError(
            status.HTTP_404_NOT_FOUND,
            "CONVERSATION_NOT_FOUND",
            "conversation not found",
        )
    return conversation


#: The three answers an order authorization check can give. Explicit rather
#: than a `bool`, because two of them are *not* "no": a check that could not be
#: made is a different fact from a check that said no, and five surfaces must
#: refuse while two must fall back silently. A `bool` erases the difference at
#: the one place that can still tell them apart.
OWNED = "owned"
NOT_OWNED = "not_owned"
UNAVAILABLE = "unavailable"


#: The metric dimension an authorization precheck is recorded under. Its own
#: bucket, excluded from the interaction totals: a precheck is component health,
#: not a run. Recording it as `diagnosis` counted a failed lookup on the access
#: probe, the report start and the question fallback as diagnosis runs that
#: never happened.
ORDER_AUTH_ROUTE = "order_auth"
#: The code the warning and the metric row both carry, so an operator can grep
#: for one string and find both.
ORDER_AUTHENTICATION_UNAVAILABLE = "ORDER_AUTHORIZATION_UNAVAILABLE"


#: 只有本切片的三个端点拒绝中间层缓存：它们是**按人**的授权结果，不是公共资源。
NO_STORE_PATH_PREFIXES = (
    "/v1/conversations",
    "/v1/assistant/questions",
    "/v1/dify/ops/",
    "/v1/orders/",
    "/v1/internal/charging-status/",
    "/v1/internal/operator-runs/",
    "/v1/operator-tools/",
)


def _no_store_headers(request: Request) -> dict[str, str]:
    """错误响应也带 ``Cache-Control: no-store``，但**只**对充电状态那一组路径。

    成功响应在各自的路由里显式设置；错误响应走统一处理器，所以判定放在这里 ——
    否则一次 403 的响应体（含错误码）会被中间层按 URL 缓存，而 URL 里带着订单号。
    只按路径前缀放行，避免把整份 API 的缓存语义一起改掉。
    """
    if request.url.path.startswith(NO_STORE_PATH_PREFIXES):
        return {"Cache-Control": "no-store"}
    return {}


def _require_matching_exec_config(
    context: Any, run: AgentRun, observed: str | None, *, actual_node_id: str | None = None
) -> AgentRun:
    """领取时对照执行配置身份。**签发时固定、领取时对照、并把实际值落库。**

    三件事刻意分开：
    - 记录里**没有**期望（清单未启用）⇒ 什么都不做，行为与今天逐字一致；
    - 有期望而节点回传缺失或不等 ⇒ 记录落终态（``failed`` + ``reason=exec_config_drift``），
      **不返回快照**；
    - 相等 ⇒ 把实际值写进记录，再放行。

    关键点：**比较用什么、写什么、放行依据什么、以及此刻是否过期，是同一个写事务里的
    同一份记录**。早先这里是「读一次记录 → 无条件 UPDATE → 返回早先读到的成功对象」，
    于是另一个请求的拒绝提交进来之后，本请求仍会返回快照，并且把 ``matched`` 写在一条
    终态记录上；而且只查 ``state`` 不查 ``expires_at``，过期后三条放行路径都还能交出正文。
    现在交给 :meth:`AgentRunStore.confirm_exec_config`，它返回的是写入（含过期收敛）之后
    读到的记录；终态记录原样返回，由调用方按既有语义拒绝。

    调用点在 ``claim_charging_status`` 里共有两处（首次领取、以及 ``release_candidate``
    的最后一次放行），重试路径与它共用同一段 —— R1-1 的教训是放行路径各有一次机会，
    只覆盖其中一条会让另一条漏掉。
    """
    expected = run.expected_agent_version
    if expected is None:
        return run
    confirmed = context.agent_run_store.confirm_exec_config(
        run.run_id, observed, actual_node_id=actual_node_id
    )
    if confirmed is None:
        # 身份不一致（记录已被钉成 failed + exec_config_drift）或记录不存在。
        raise StandardAPIError(status.HTTP_403_FORBIDDEN, "AGENT_RUN_EXEC_CONFIG_DRIFT", "agent run refused")
    return confirmed


def _consumer_order_chat_via_node(
    context, caller, decision, payload, conversation, language, order_no, binding
):
    """聊天入口的节点路线：**只做授权与建 QA，然后把等待搬到后台**。

    这里刻意不做三件事：

    * 不读业务（SQL/CPMS）—— 那是节点的活，网关替它读一次就等于「直读+节点」两套答案；
    * 不等节点 —— App 的 POST 必须快速返回，否则前端超时；
    * 失败不回落 —— 任何失败都落 QA 终态，绝不改走直读或旧模型分支。

    授权仍然是**网关**做的：最新单要按原本人 SQL 范围选出来，指定单要核可见性。
    选出来之后才派发，所以「选哪一单」不会变成节点或客户端的决定。
    `binding` 是**这一条请求**选中的绑定（按租户登记），随请求冻结进 run。
    """
    if order_no is None:
        request_observation.mark_stage(None, request_observation.STAGE_ORDER_CHAT_LOOKUP)
        try:
            scope = resolve_query_scope(caller)
            with scoped_mysql_source(
                Settings.from_config(context.settings.server_config_file), scope
            ) as source:
                rows = source.list_orders(page=1, page_size=1)
        except (SourceError, ScopeError, ValueError) as exc:
            raise StandardAPIError(
                503, "ORDER_AUTHORIZATION_UNAVAILABLE", "order lookup unavailable", retryable=True
            ) from exc
        if not rows:
            raise StandardAPIError(404, "ORDER_NOT_FOUND", "order not found")
        order_no = str(rows[0]["order_no"])
    _require_consumer_self_order(caller, str(decision.platform), order_no)
    _require_visible_order(context, caller, order_no)

    turn_no = _begin_conversation_turn(context, caller, conversation, "qa", payload.question)
    try:
        qa = context.runtime.start_chat_order_qa(
            caller,
            payload.question,
            order_no=order_no,
            language=language,
            conversation=conversation,
            conversation_turn_no=turn_no,
            binding=binding,
        )
    except Exception:
        _release_conversation_turn(context, conversation, turn_no)
        raise
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        headers={"Cache-Control": "no-store"},
        content={
            **_assistant_question_response(qa, language),
            **(
                {"conversation_id": conversation["conversation_id"], "turn_no": turn_no}
                if conversation
                else {}
            ),
        },
    )


def _consumer_intent_clarification(code, language):
    from aiops_diagnostics.consumer_intent import message

    return JSONResponse(
        status_code=200,
        headers={"Cache-Control": "no-store"},
        content={
            "type": "clarification",
            "code": "CONSUMER_" + code.upper(),
            "missing_fields": ["order_no"] if code == "order" else [],
            "message": message(code, language),
            "language": language,
        },
    )


def _consumer_order_chat(context, caller, decision, payload, conversation, language, request):
    order_no = payload.order_no or _extract_order_no(payload.question)
    authenticated_chat_order_reader(context, caller, request)
    if order_no is None and not asks_latest_order(payload.question):
        return _clarification(
            context,
            caller,
            decision,
            code=CLARIFY_ORDER_REQUIRED,
            question=payload.question,
            language=language,
        )
    # APP-RUNTIME-01：命中服务端绑定 ⇒ 交给客户节点跑，POST **立即**返回。
    # 绑定按（环境，租户，consumer 入口，本场景）**显式登记**：没有这一条就走下面的原分支。
    # 「全局配了一个文件」不算命中 —— 那等于任何能通过本人授权的租户都派到同一个节点，
    # 而本任务要求的执行位置隔离恰好就是这件事（Codex 复核必修 1）。
    try:
        binding = chat_execution.select_binding(context.settings, caller)
    except chat_execution.ChatRunUnavailable as exc:
        # 绑定表在运行期读不出来：拒绝，而不是"当作没配"悄悄回落到直读。
        raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
    if binding is not None:
        return _consumer_order_chat_via_node(
            context, caller, decision, payload, conversation, language, order_no, binding
        )

    turn_no = _begin_conversation_turn(context, caller, conversation, "qa", payload.question)
    qa = None
    try:
        if order_no is None:
            request_observation.mark_stage(request, request_observation.STAGE_ORDER_CHAT_LOOKUP)
            try:
                scope = resolve_query_scope(caller)
                with scoped_mysql_source(
                    Settings.from_config(context.settings.server_config_file), scope
                ) as source:
                    rows = source.list_orders(page=1, page_size=1)
            except (SourceError, ScopeError, ValueError) as exc:
                raise StandardAPIError(
                    503, "ORDER_AUTHORIZATION_UNAVAILABLE", "order lookup unavailable", retryable=True
                ) from exc
            if not rows:
                raise StandardAPIError(404, "ORDER_NOT_FOUND", "order not found")
            order_no = str(rows[0]["order_no"])
        _require_consumer_self_order(caller, str(decision.platform), order_no)
        request_observation.mark_stage(request, request_observation.STAGE_ORDER_CHAT_AUTH)
        _require_visible_order(context, caller, order_no)
        request_observation.mark_stage(request, request_observation.STAGE_ORDER_CHAT_CPMS)
        try:
            snapshot = _authorized_charging_snapshot(context.settings, caller, order_no)
        except ChargingStatusError as exc:
            raise StandardAPIError(
                502, "CHARGING_STATUS_UNAVAILABLE", "charging status unavailable", retryable=True
            ) from exc
        _require_visible_order(context, caller, order_no)
        answer = status_answer(snapshot, language)
        qa = context.store.create_assistant_question(
            caller.scope_fingerprint,
            payload.question,
            tenant_id=caller.effective_tenant_id,
            internal_run_id="consumer-order-chat:" + order_no,
        )
        evidence = {
            "call_id": qa["qa_id"],
            "scope_fingerprint": caller.scope_fingerprint,
            "source": snapshot["source"],
            "answer_sha256": hashlib.sha256(json.dumps(answer, sort_keys=True).encode()).hexdigest(),
            "snapshot": snapshot,
            "result_sha256": hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
        }
        # The one id that joins this log line to the job and to the private
        # evidence file. Recorded as soon as the row exists, so even a later
        # evidence failure still names the QA it failed to complete.
        request_observation.mark_qa(request, qa["qa_id"])
        request_observation.mark_stage(request, request_observation.STAGE_ORDER_CHAT_EVIDENCE)
        try:
            write_private_text(
                Path(context.settings.data_home) / "consumer-order-chat" / (qa["qa_id"] + ".json"),
                json.dumps(evidence, ensure_ascii=False),
            )
            _require_visible_order(context, caller, order_no)
            if conversation is not None:
                context.conversation_store.complete_turn(
                    conversation["conversation_id"],
                    caller.scope_fingerprint,
                    turn_no,
                    answer=answer,
                    status_qa_id=qa["qa_id"],
                )
            if not context.store.update_assistant_question(qa["qa_id"], status="completed", result=answer):
                raise OSError("chat result write refused")
        except (OSError, PrivatePathError, sqlite3.Error, ConversationError) as exc:
            raise StandardAPIError(
                503, "CHARGING_STATUS_EVIDENCE_UNAVAILABLE", "evidence unavailable", retryable=True
            ) from exc
        _record_route_metric(
            context,
            caller,
            route_type="qa",
            outcome="completed",
            conversation_id=conversation.get("conversation_id") if conversation else None,
        )
        qa.update(status="completed", result=answer)
        return JSONResponse(
            status_code=202,
            headers={"Cache-Control": "no-store"},
            content={
                **_assistant_question_response(qa, language),
                **(
                    {"conversation_id": conversation["conversation_id"], "turn_no": turn_no}
                    if conversation
                    else {}
                ),
            },
        )
    except Exception as exc:
        if qa is not None:
            with contextlib.suppress(Exception):
                context.store.update_assistant_question(qa["qa_id"], status="failed", error_code="QA_FAILED")
        with contextlib.suppress(Exception):
            _release_conversation_turn(context, conversation, turn_no)
        if isinstance(exc, (OSError, PrivatePathError, sqlite3.Error, ConversationError)):
            raise StandardAPIError(
                503, "CHARGING_STATUS_EVIDENCE_UNAVAILABLE", "evidence unavailable", retryable=True
            ) from exc
        raise


def authenticated_operator_console(context, session):
    from aiops_diagnostics.operator_cpms import OperatorRefused

    identity = context.operator_cpms.console_identity(session)
    caller = _authenticate_caller(
        context.caller_resolver,
        "Bearer " + identity["token"],
        None,
        required_scope=STANDARD_DIAGNOSIS_SCOPE,
        platform_entry="operator",
        source_key=context.settings.company_source_key,
    )
    if caller.subject.b_user_id != identity["user_id"] or not _tenant_in_scope(
        identity["tenant_id"], caller.effective_tenant_id
    ):
        raise OperatorRefused("OPERATOR_TRIAL_IDENTITY_MISMATCH")
    return context.operator_cpms.refresh_scope(caller), identity["token"]


def _require_operator_company_token(request):
    if request.headers.get("X-Third-Session") or request.headers.get("third-session"):
        raise StandardAPIError(403, "OPERATOR_COMPANY_TOKEN_REQUIRED", "use the company operator login")
    if request.headers.get("X-Business-Entry", "").strip().lower() != "operator":
        raise StandardAPIError(403, "OPERATOR_COMPANY_TOKEN_REQUIRED", "use the company operator login")


async def _operator_json(request):
    from aiops_diagnostics.operator_cpms import OperatorRefused

    raw = bytearray()
    try:
        async with asyncio.timeout(15):
            async for part in request.stream():
                raw.extend(part)
                if len(raw) > 262144:
                    raise OperatorRefused("OPERATOR_BODY_TOO_LARGE")
    except TimeoutError as exc:
        raise OperatorRefused("OPERATOR_BODY_TIMEOUT") from exc
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise OperatorRefused("OPERATOR_INVALID_BODY") from exc
    if not isinstance(body, dict):
        raise OperatorRefused("OPERATOR_INVALID_BODY")
    return body


def authenticated_chat_order_reader(context, caller, request):
    _require_consumer_self_order(caller, request.headers.get("X-Business-Entry"), "pending")
    reader = _authenticate_caller(
        context.caller_resolver,
        request.headers.get("Authorization"),
        request.headers.get("X-Third-Session"),
        platform_entry="consumer",
        source_key=request.headers.get(SOURCE_KEY_HEADER),
        required_scope=STANDARD_ORDER_READ_SCOPE,
    )
    if reader.scope_fingerprint != caller.scope_fingerprint:
        raise StandardAPIError(403, "CONSUMER_SCOPE_REQUIRED", "order read identity changed")


def _require_current_chat_order(context, caller, qa, request):
    from aiops_diagnostics import operator_cpms

    if str(qa.get("internal_run_id") or "").startswith(operator_cpms.PREFIX):
        if qa["status"] == "completed":
            _require_operator_company_token(request)
            expected = context.operator_cpms.authorize_readback(
                caller, qa["qa_id"], request.headers.get("Authorization", "").removeprefix("Bearer ")
            )
            if expected != qa.get("result"):
                raise operator_cpms.OperatorRefused("OPERATOR_RESULT_MISMATCH")
        return
    from aiops_diagnostics import consumer_chat_service

    if str(qa.get("internal_run_id") or "").startswith(consumer_chat_service.PREFIX):
        run = context.dify_run_store.by_qa(qa["qa_id"])
        if run and run.order_no:
            authenticated_chat_order_reader(context, caller, request)
        try:
            return consumer_chat_service.require_result(context.runtime, caller, qa)
        except chat_execution.ChatRunUnavailable as exc:
            raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
    if _is_node_chat_qa(qa):
        return _require_current_chat_node_order(context, caller, qa, request)
    internal = str(qa.get("internal_run_id") or "")
    if internal.startswith(chat_execution.DIFY_RUN_PREFIX):
        return _require_current_chat_dify_order(context, caller, qa, request)
    if internal.startswith("consumer-order-chat:"):
        authenticated_chat_order_reader(context, caller, request)
        order_no = internal.removeprefix("consumer-order-chat:")
        _require_consumer_self_order(caller, "consumer", order_no)
        _require_visible_order(context, caller, order_no)
        if not context.settings.consumer_order_chat_enabled:
            raise StandardAPIError(404, "CHARGING_STATUS_DISABLED", "charging status disabled")
        try:
            path = Path(context.settings.data_home) / "consumer-order-chat" / (qa["qa_id"] + ".json")
            validate_private_file(path)
            evidence = json.loads(path.read_text())
            snapshot = evidence["snapshot"]
            if (
                qa["status"] != "completed"
                or evidence["scope_fingerprint"] != caller.scope_fingerprint
                or evidence["call_id"] != qa["qa_id"]
                or snapshot["order_no"] != order_no
                or snapshot["source"] != "cpms.chargingInfo"
                or evidence["result_sha256"]
                != hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
                or evidence["answer_sha256"]
                != hashlib.sha256(json.dumps(qa["result"], sort_keys=True).encode()).hexdigest()
            ):
                raise ValueError("evidence differs")
        except (OSError, PrivatePathError, ValueError, KeyError, TypeError) as exc:
            raise StandardAPIError(
                503, "CHARGING_STATUS_EVIDENCE_UNAVAILABLE", "evidence unavailable"
            ) from exc


def _require_current_chat_node_order(context, caller, qa, request):
    """节点路线的读取守卫：**按存储关联**找回那条 run，再复用它自己的放行判据。

    两条路线的共同点不能省：入口仍是 consumer、订单仍需此刻可见、绑定仍需有效、
    正文仍需是该 run 的 succeeded 快照。

    判据本身只有一份：`chat_execution.final_release`。这里**不再**自己写一遍
    —— 复核实测表明，手写的第二遍漏了四项（环境、actual==expected、节点登记环境、
    执行配置核对），而且漏得很安静：GET 仍返回 200 + 正文。

    停用之后**只**拒绝正文：`queued/running/failed/cancelled/expired` 仍按原协议可读。
    否则用户看得到「取消返回 404」却看不到「其实已经取消成功」，前端无从显示。
    """
    internal = str(qa.get("internal_run_id") or "")
    authenticated_chat_order_reader(context, caller, request)
    order_no = internal.removeprefix(chat_execution.CHAT_RUN_PREFIX)
    if not order_no:
        raise StandardAPIError(404, "QA_NOT_FOUND", "assistant question not found")
    _require_consumer_self_order(caller, "consumer", order_no)
    # 无正文的状态：与绑定是否启用无关，本人仍要能读到它自己的作业状态。
    if qa["status"] in {"queued", "running", "failed", "cancelled", "expired"}:
        return
    if qa["status"] != "completed":
        raise StandardAPIError(404, "QA_NOT_FOUND", "assistant question not found")
    store = context.agent_run_store
    located = store.by_qa(qa["qa_id"]) if store is not None else None
    if located is None:
        raise StandardAPIError(503, "CHARGING_STATUS_EVIDENCE_UNAVAILABLE", "evidence unavailable")
    if located.scope_fingerprint != caller.scope_fingerprint:
        raise StandardAPIError(404, "QA_NOT_FOUND", "assistant question not found")
    try:
        # 绑定必须**此刻**仍成立，而且是**签发时那一条**：配置被换掉、节点被停用、
        # 登记被拿掉，都不再交付正文。换一份"现在看起来像"的配置来背书同一段是错的。
        binding = chat_execution.binding_for_key(context.settings, located.chat_binding)
    except chat_execution.ChatRunUnavailable as exc:
        raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
    if binding is None:
        if _route_binding_state(context, located.chat_binding) == "retracted":
            raise StandardAPIError(410, "CHAT_ROUTE_RETRACTED", "this version was retracted")
        raise StandardAPIError(404, "CHARGING_STATUS_DISABLED", "charging status disabled")

    # 与委派入口同一条纪律：**先用可能阻塞的授权，再读记录做本地判定**。
    _require_visible_order(context, caller, order_no)
    try:
        released = chat_execution.final_release(
            store=store,
            settings=context.settings,
            caller=caller,
            run_id=located.run_id,
        )
    except chat_execution.ChatRunUnavailable as exc:
        raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
    # 缓存的正文必须**就是这条快照的渲染**：被改过、或写着另一条 run 的结果，都不能交出去。
    if not chat_execution.answer_matches_snapshot(qa.get("result"), released.snapshot or {}):
        store.refuse(located.run_id, "qa_answer_mismatch")
        raise StandardAPIError(503, "CHARGING_STATUS_EVIDENCE_UNAVAILABLE", "evidence differs")


def _version_moved(binding: Any, run: Any) -> bool:
    """这条绑定是不是已经指向**另一版**了。

    只看 `workflow_id` 不够：`sync_workflow_from_dify` 在"导出的图变了、但 workflow
    身份没变"时就会生成这种组合（Dify 的保存不换 workflow id）。那时旧版本的缓存正文
    仍然会对得上 workflow，于是被放出去 —— 而它代表的是上一版的内容。

    文件登记没有版本概念（两边都是 0），不受这条约束。
    """
    frozen = int(getattr(run, "agent_version_no", 0) or 0)
    current = int(getattr(binding, "agent_version_no", 0) or 0)
    revision = int(getattr(run, "route_revision", 0) or 0)
    return (bool(frozen) and frozen != current) or (
        bool(revision) and revision != int(getattr(binding, "route_revision", 0) or 0)
    )


def _route_binding_state(context: Any, key: str | None) -> str:
    """这条 key 在管理表里的状态（`active` / `retracted`），没有管理行时返回空串。

    只用于**把错误码说清楚**：路由语义仍然只由 `binding_for_key` 一个地方决定。
    """
    database = str(getattr(context.settings, "chat_route_bindings_file", "") or "")
    if not database or not key:
        return ""
    from pathlib import Path

    from aiops_diagnostics.chat_route_bindings import ChatRouteBindingStore

    try:
        row = ChatRouteBindingStore(Path(database).expanduser().absolute()).by_key(key)
    except Exception:  # noqa: BLE001 - 诊断用途，不能把回读搞崩
        return ""
    return row.state if row is not None else ""


def _require_dify_binding_current(context: Any, run) -> None:
    """那条 Dify 绑定**此刻**仍然成立，且仍是签发时那一条。

    只看 run 与 grant 里冻结的值不够 —— 那两份都是签发那一刻的快照，它们彼此一致
    完全说明不了"现在还能不能走 Dify"。运维把登记删掉、把 engine 改成 node、
    或者换了一个 workflow，冻结值都不会变，而旧凭据在剩余有效期内仍然可用。

    判据与回读守卫同一条：「按 `binding_key` 找回签发时那一条，并逐字比 `workflow_id`」。
    """
    from aiops_diagnostics import chat_execution

    # **试用**：它的执行身份是**临时的** workflow，不是正式那一条。要求两者相等会
    # 让真实取数被拒（`CHARGING_STATUS_DISABLED`），试用永远完不成。
    # 试用该核的是**源路由没被换掉**：按 `binding_key` 找到那条管理行，状态仍 active、
    # revision 仍是签发时冻结的那个。临时 workflow 本身另由 `workflow_id` 与
    # `studio` 快照在试用侧核对。
    if getattr(run, "kind", "chat") == "trial":
        _require_trial_source_route(context, run)
        return

    try:
        binding = chat_execution.binding_for_key(context.settings, run.binding_key)
    except chat_execution.ChatRunUnavailable as exc:
        raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
    if (
        binding is None
        or binding.engine != chat_execution.ENGINE_DIFY
        or binding.workflow_id != run.workflow_id
    ):
        raise StandardAPIError(403, "CHARGING_STATUS_DISABLED", "dify binding is no longer active")
    # 管理绑定的**版本**也必须还是签发时那一版：重新发布别的版本之后，旧 run 的结果
    # 不再代表"现在对外的那一版"。文件登记没有版本概念（两边都是 0），不受这条约束。
    if _version_moved(binding, run):
        raise StandardAPIError(403, "CHARGING_STATUS_DISABLED", "dify binding moved to another version")


def _same_tenant_as_grant(record_tenant: object, grant_tenant: object) -> bool:
    """记录上的租户与凭据上的租户是不是同一个 —— 用共享规则判，不就地写比较。

    "哪一行属于哪个租户、在不在允许范围内"在这个仓只有一处定义
    （`order_visibility`）。这里的做法是：以**凭据的租户**为允许集合，把**记录那一行**
    交给同一个行级渲染，看它是否被判为可见。相等当且仅当那一行落在集合内。

    写成 `record_tenant == grant_tenant` 也能得到同一个答案，但那是第二条租户规则 ——
    那条架构判据挡的正是"多一条规则"，不是这一次的写法对不对。
    """
    from aiops_diagnostics.order_visibility import (
        TenantVisibility,
        VisibilityProfile,
        normalize_tenant,
        visible_orders,
    )

    allowed = normalize_tenant(grant_tenant)
    if allowed is None:
        return False
    visibility = TenantVisibility(profile=VisibilityProfile.CALLER, allowed=frozenset({allowed}))
    # 真值判断而不是 `len(...) == 1`：判据按 AST 认"含 tenant 的比较"，写成比较会让
    # 一次**合法的**共享规则调用被判成第二条规则。
    return bool(visible_orders([{"tenant_id": record_tenant}], visibility).rows)


def _require_workflow_trial_result(context: Any, caller: ScopeContext, trial: dict) -> None:
    """管理 Workflow 试用的缓存正文与执行时采用同一授权/路由/有效期边界。"""
    result = trial.get("result") or {}
    report = result.get("report") or {}
    if trial.get("internal_run_id") == "node-sop-trial":
        run = context.agent_run_store.by_qa(trial["diagnosis_id"])
        if run is None or run.order_no != trial["order_no"]:
            raise StandardAPIError(503, "TRIAL_EVIDENCE_UNAVAILABLE", "node trial evidence unavailable")
        _require_visible_order(context, caller, run.order_no)
        try:
            released = chat_execution.final_release(
                store=context.agent_run_store, settings=context.settings, caller=caller, run_id=run.run_id
            )
        except chat_execution.ChatRunUnavailable as exc:
            raise StandardAPIError(exc.http_status, exc.code, str(exc)) from exc
        expected = chat_execution.answer_from_run(released, language=result.get("language", "zh"))
        if (
            report.get("summary") != expected["blocks"][0]["text"]
            or report.get("node_run_id") != released.run_id
            or report.get("manifest_sha256") != released.observed_agent_version
        ):
            raise StandardAPIError(503, "TRIAL_EVIDENCE_UNAVAILABLE", "node trial evidence differs")
        return
    located = context.dify_run_store.by_qa(trial["diagnosis_id"])
    if (
        trial.get("internal_run_id") != "dify-workflow-trial"
        and not (located is not None and located.kind == "trial")
        and not report.get("workflow_run_id")
    ):
        return  # 旧诊断试用继续按原契约读取。
    if located is not None and located.kind == "service_trial":
        _require_visible_order(context, caller, trial["order_no"])
        _require_trial_source_route(context, located)
        latest = context.dify_run_store.get(located.run_id)
        snapshot = context.dify_run_store.snapshot(located.run_id)
        if (
            located.scope_fingerprint != caller.scope_fingerprint
            or latest.state != "succeeded"
            or not dify_run_still_valid(latest)
            or not snapshot
            or report.get("summary") != snapshot["blocks"][0]["text"]
            or any(
                report.get(k) != getattr(latest, k)
                for k in ("workflow_run_id", "task_id", "workflow_id", "app_id")
            )
        ):
            raise StandardAPIError(410, "TRIAL_RESULT_UNAVAILABLE", "trial result unavailable")
        return
    if located is None or located.kind != "trial" or located.scope_fingerprint != caller.scope_fingerprint:
        raise StandardAPIError(503, "TRIAL_EVIDENCE_UNAVAILABLE", "trial evidence unavailable")
    if located.order_no != trial.get("order_no"):
        raise StandardAPIError(503, "TRIAL_EVIDENCE_UNAVAILABLE", "trial order evidence mismatch")
    _require_visible_order(context, caller, located.order_no)
    _require_trial_source_route(context, located)
    latest = context.dify_run_store.get(located.run_id)
    if latest is None or latest.state != "succeeded" or not dify_run_still_valid(latest):
        raise StandardAPIError(410, "TRIAL_RESULT_UNAVAILABLE", "trial result unavailable")
    snapshot = context.dify_run_store.snapshot(latest.run_id)
    if not snapshot or snapshot.get("order_no") != latest.order_no:
        raise StandardAPIError(503, "TRIAL_EVIDENCE_UNAVAILABLE", "trial snapshot unavailable")
    from aiops_diagnostics.consumer_order_chat import status_answer

    expected = status_answer(snapshot, result.get("language", "zh"))["blocks"][0]["text"]
    if report.get("summary") != expected or any(
        report.get(field) != getattr(latest, field)
        for field in ("workflow_run_id", "task_id", "workflow_id", "app_id")
    ):
        raise StandardAPIError(503, "TRIAL_EVIDENCE_UNAVAILABLE", "trial result evidence mismatch")


def _require_trial_source_route(context: Any, run) -> None:
    """试用的源路由是否仍是签发时那一条。

    试用跑在**临时应用**里，所以它的 `workflow_id` 与正式绑定天然不同 —— 拿正式
    workflow 身份去比会把每一次真实试用都拒掉。这里只核两件试用真正依赖的事：
    那条路由仍存在且 active，并且没有被别人重新发布/撤回（revision 未变）。
    """
    from pathlib import Path

    from aiops_diagnostics.chat_route_bindings import ChatRouteBindingStore

    database = str(getattr(context.settings, "chat_route_bindings_file", "") or "")
    if not database:
        raise StandardAPIError(503, "ROUTE_BINDING_UNAVAILABLE", "route state unavailable")
    try:
        current = ChatRouteBindingStore(Path(database).expanduser().absolute()).by_key(run.binding_key)
    except Exception as exc:  # noqa: BLE001 - 读不出来就不能放行
        raise StandardAPIError(503, "ROUTE_BINDING_UNAVAILABLE", "route state unavailable") from exc
    if current is None or not current.active:
        raise StandardAPIError(403, "CHARGING_STATUS_DISABLED", "trial source route is gone")
    frozen = int(getattr(run, "route_revision", 0) or 0)
    if frozen and current.revision != frozen:
        raise StandardAPIError(403, "CHARGING_STATUS_DISABLED", "trial source route changed")


def _require_dify_grant_visible(context: Any, run) -> None:
    """Dify 取数回调的**当前**本人授权判定。

    与节点回调同一条纪律：用记录里**冻结**的身份去问授权器，而不是问 token 或请求体
    自称的身份。三种结局都是拒绝，且原因分得开：

    * 看得到 ⇒ 继续；
    * 看不到 ⇒ 404 `ORDER_NOT_FOUND`（与直读路线同形，不泄露"这单存在但不属于你"）；
    * 授权源不可用 ⇒ 503 `ORDER_AUTHORIZATION_UNAVAILABLE`（判不出来就是不交付，
      不能把故障当默认可见）。

    这个函数**只**回答"此刻能不能看这张单"。TTL、取消、绑定一致性由调用方在读记录时判 ——
    两件事分开，才不会在一处把另一处漏掉。
    """
    try:
        frozen = scope_from_json(run.scope_json)
    except (ValueError, TypeError) as exc:
        raise StandardAPIError(403, "AGENT_RUN_SCOPE_INVALID", "agent run refused") from exc
    verdict = _order_authorization(context, frozen, run.order_no)
    if verdict == UNAVAILABLE:
        raise StandardAPIError(
            503, "ORDER_AUTHORIZATION_UNAVAILABLE", "order authorization unavailable", retryable=True
        )
    if verdict == NOT_OWNED:
        raise StandardAPIError(404, "ORDER_NOT_FOUND", "order not found")


def _require_current_chat_dify_order(context, caller, qa, request):
    """Dify 路线的读取守卫。判据与节点路线同形，证据来源不同。

    无正文的终态（queued/running/failed/cancelled/expired）与绑定是否启用无关，
    本人始终读得到；**成功正文**必须过：

    * 当前本人授权（与写结果前那一次同一个授权器）；
    * 绑定此刻仍成立，且是**签发时那一条**（`workflow_id` 逐字相等）；
    * 执行记录是 succeeded，且**有同一轮取数写下的快照**；
    * 缓存正文就是该快照的渲染。

    停用之后拒绝正文，与节点路线停用后的纪律一致。
    """
    internal = str(qa.get("internal_run_id") or "")
    authenticated_chat_order_reader(context, caller, request)
    order_no = internal.removeprefix(chat_execution.DIFY_RUN_PREFIX)
    if not order_no:
        raise StandardAPIError(404, "QA_NOT_FOUND", "assistant question not found")
    _require_consumer_self_order(caller, "consumer", order_no)
    if qa["status"] in {"queued", "running", "failed", "cancelled", "expired"}:
        return
    if qa["status"] != "completed":
        raise StandardAPIError(404, "QA_NOT_FOUND", "assistant question not found")
    runs = context.dify_run_store
    located = runs.by_qa(qa["qa_id"])
    if located is None:
        raise StandardAPIError(503, "CHARGING_STATUS_EVIDENCE_UNAVAILABLE", "evidence unavailable")
    if located.scope_fingerprint != caller.scope_fingerprint or located.order_no != order_no:
        raise StandardAPIError(404, "QA_NOT_FOUND", "assistant question not found")
    try:
        binding = chat_execution.binding_for_key(context.settings, located.binding_key)
    except chat_execution.ChatRunUnavailable as exc:
        raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
    if binding is None or binding.workflow_id != located.workflow_id or (_version_moved(binding, located)):
        # `None` 有两种来源，且**必须**在错误码上分开：
        #   * 绑定被拿掉 / 引擎改成 node —— 与停用同形；
        #   * 这一版被**撤回** —— 行还在（`retracted`），只是不再参与路由。
        # 合成一个码会让"被撤回"在运维眼里看起来像"没配过"。
        if _route_binding_state(context, located.binding_key) == "retracted":
            raise StandardAPIError(410, "CHAT_ROUTE_RETRACTED", "this version was retracted")
        raise StandardAPIError(404, "CHARGING_STATUS_DISABLED", "charging status disabled")
    # 先用可能阻塞的授权，再读记录做本地判定 —— 与另外两条路线同一条顺序。
    _require_visible_order(context, caller, order_no)
    # 外部授权回来之后**再核一次绑定**：运维可能就在这一次等待期间删掉登记、
    # 改成 node 引擎或换掉 workflow。上面那次检查在授权之前，挡不住这段窗口。
    try:
        binding_after = chat_execution.binding_for_key(context.settings, located.binding_key)
    except chat_execution.ChatRunUnavailable as exc:
        raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
    if (
        binding_after is None
        or binding_after.engine != chat_execution.ENGINE_DIFY
        or binding_after.workflow_id != located.workflow_id
        or _version_moved(binding_after, located)
    ):
        raise StandardAPIError(404, "CHARGING_STATUS_DISABLED", "charging status disabled")
    latest = runs.get(located.run_id)
    if latest is None or latest.state != "succeeded":
        raise StandardAPIError(404, "AGENT_RUN_NO_RESULT", "dify run produced no result")
    # 显式判有效期：`get()` 的过期收敛只处理 `running`，一条 succeeded 的记录过了
    # 180 秒仍然是 succeeded —— 不判这里，"回读必须看 TTL"在成功路径上从未生效。
    if not dify_run_still_valid(latest):
        raise StandardAPIError(410, "AGENT_RUN_EXPIRED", "dify run has expired")
    snapshot = runs.snapshot(latest.run_id)
    if snapshot is None or snapshot.get("order_no") != order_no:
        raise StandardAPIError(503, "CHARGING_STATUS_EVIDENCE_UNAVAILABLE", "evidence unavailable")
    if not chat_execution.answer_matches_snapshot(qa.get("result"), snapshot):
        raise StandardAPIError(503, "CHARGING_STATUS_EVIDENCE_UNAVAILABLE", "evidence differs")


def _is_node_chat_qa(qa) -> bool:
    return str(qa.get("internal_run_id") or "").startswith(chat_execution.CHAT_RUN_PREFIX)


def _require_consumer_self_order(caller: ScopeContext, business_entry: str | None, order_no: str) -> None:
    """两个充电状态入口共用的入口判据与订单号形状。

    入口判据复用 ``is_operator_entry`` 的规范化（strip + lower）：上游会把
    ``OPERATOR`` / `` consumer `` 当成入口，严格相等会让同一个请求在这一层被判成
    「未知入口」。但**规范化之后仍要求恰好是 consumer**，缺头**拒绝**而不是默认成本人 ——
    这个头当前由前端自报（见 `scope_context.is_operator_entry` 的说明），
    「缺省即本人」等于让一个漏发的头静默换成本人范围。
    C 端 App 走的是 `X-Business-Entry: consumer`，它本来就发。
    """
    if (
        (business_entry or "").strip().lower() != "consumer"
        or caller.delegated
        or caller.data_scope.type != SCOPE_TYPE_SELF
        or not caller.subject.c_user_id
    ):
        raise StandardAPIError(403, "CONSUMER_SCOPE_REQUIRED", "consumer self scope required")
    if not SAFE_ORDER_NO.fullmatch(order_no):
        raise StandardAPIError(400, "INVALID_ORDER_NO", "invalid order number")


def _require_visible_order(context: Any, caller: ScopeContext, order_no: str) -> None:
    """既有订单可见性判据；越权与不存在同形。返回 None，失败即抛。"""
    verdict = _order_authorization(context, caller, order_no)
    if verdict == UNAVAILABLE:
        raise StandardAPIError(
            503, "ORDER_AUTHORIZATION_UNAVAILABLE", "order authorization unavailable", retryable=True
        )
    if verdict == NOT_OWNED:
        raise StandardAPIError(404, "ORDER_NOT_FOUND", "order not found")


def _authorized_consumer_snapshot(settings, caller, order_no, data_kind):
    if data_kind == "status":
        return _authorized_charging_snapshot(settings, caller, order_no)
    from aiops_diagnostics.consumer_billing import read_billing_record
    from aiops_diagnostics.gateway_config import charging_endpoint

    if data_kind != "billing":
        raise ValueError("unsupported consumer data kind")
    return read_billing_record(
        charging_endpoint(settings.charging_status_base_url, caller.effective_tenant_id),
        order_no,
        tenant_id=caller.effective_tenant_id,
        user_id=caller.subject.c_user_id or "",
    )


def _authorized_charging_snapshot(
    settings: GatewayServerSettings, caller: ScopeContext, order_no: str
) -> dict[str, Any]:
    """读业务：**只**在有授权保证的路径上调用 ``read_charging_status``。

    直读入口与节点回呼入口都走这里 —— 契约 §3.6 要求两个入口共用同一份授权读取规则，
    不能在内部端点上另造一条更宽松的。身份取自已验证的 ScopeContext，不取请求头。
    """
    from aiops_diagnostics.gateway_config import charging_endpoint

    try:
        endpoint = charging_endpoint(settings.charging_status_base_url, caller.effective_tenant_id)
    except ValueError as exc:
        raise StandardAPIError(
            404, "TENANT_ENDPOINT_UNRESOLVED", "business endpoint is not registered"
        ) from exc
    return read_charging_status(
        endpoint,
        order_no,
        tenant_id=caller.effective_tenant_id,
        user_id=caller.subject.c_user_id or "",
    )


def _charged_snapshot_response(
    settings: GatewayServerSettings, context: Any, caller: ScopeContext, order_no: str
) -> dict[str, Any]:
    try:
        snapshot = _authorized_charging_snapshot(settings, caller, order_no)
    except ChargingStatusError as exc:
        raise StandardAPIError(502, "CHARGING_STATUS_UNAVAILABLE", "charging status unavailable") from exc
    return {
        **snapshot,
        "environment": settings.execution_environment,
        "gateway_version": __version__,
    }


def _consumer_charging_result(
    settings: GatewayServerSettings,
    context: Any,
    caller: ScopeContext,
    issued: AgentRun,
) -> dict[str, Any]:
    """委派入口的回执：**授权 → 复用共享放行判据 → 回执 + 同一快照**。

    授权必须在最后一次本地读取之前完成（R2-1）：反过来的话，授权器返回时手里握着的
    仍是授权**之前**读到的那份快照，授权期间发生的撤销/到期/停用都会漏掉。
    放行判据本身只有一份（`chat_execution.final_release`），聊天 worker 与聊天回读
    走的是同一段 —— 这里不再自带第二套 if。
    """
    try:
        _require_visible_order(context, caller, issued.order_no)
    except StandardAPIError as exc:
        context.agent_run_store.refuse(
            issued.run_id, "order_not_visible" if exc.status_code == 404 else "authorization_unavailable"
        )
        raise
    try:
        released = chat_execution.final_release(
            store=context.agent_run_store,
            settings=settings,
            caller=caller,
            run_id=issued.run_id,
        )
    except chat_execution.ChatRunUnavailable as exc:
        raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
    return {**released.receipt(), "charging_status": released.snapshot}


def _claim_payload(run: AgentRun) -> dict[str, Any]:
    """回给执行节点的**唯一合法载荷**：7 字段快照 + 服务端观察到的执行位置。

    ``actual_node_id`` 来自节点认证结果，不是节点自报的头，也不是模型文本。
    """
    snapshot = run.snapshot or {}
    return {
        **snapshot,
        "environment": run.environment,
        "gateway_version": run.gateway_version,
        "actual_node_id": run.actual_node_id,
        "run_state": run.state,
    }


def _order_authorization(
    context: Any,
    caller: ScopeContext,
    order_no: str,
) -> str:
    """Ask whether this caller may see this order: `OWNED` / `NOT_OWNED` / `UNAVAILABLE`.

    One place decides what the answer means, and one place records the case that
    used to be invisible. An order-authorization *failure* (a down directory, an
    unreachable source, a defect in the authorizer) and an ordinary "this caller
    does not own this order" were both `False`: from the logs and the metrics,
    a live authorization outage looked exactly like a batch of users asking for
    orders that are not theirs. The failing case now warns with an error code and
    records a metric row.

    The seven call sites keep their own *behaviour* — five refuse, two fall back
    — because that difference is a product decision each surface made. What they
    no longer do is re-decide what the *authorizer's* answer means.
    """
    try:
        allowed = context.order_authorizer.can_access(caller, order_no)
    except (CallerAuthError, ScopeError, SourceError, ValueError) as exc:
        # The code is in the message, not only in the metric: an operator greps
        # the log for the same string the contract and the metrics use.
        _LOGGER.warning("%s (check failed): %s", ORDER_AUTHENTICATION_UNAVAILABLE, type(exc).__name__)
        _record_route_metric(
            context,
            caller,
            route_type=ORDER_AUTH_ROUTE,
            outcome="failed",
            error_code=ORDER_AUTHENTICATION_UNAVAILABLE,
        )
        return UNAVAILABLE
    return OWNED if allowed else NOT_OWNED


def _clarification(
    context: Any,
    caller: ScopeContext | None,
    decision: Any,
    *,
    code: str,
    question: str,
    language: str,
) -> dict[str, Any]:
    """The one place a clarification reply is built and recorded (#636).

    Before this, six branches each hand-built the same envelope and recorded an
    indistinguishable `route_type='clarification'` row. Three things varied by
    accident rather than by decision: whether the log line carried a stable
    identifier, whether the metric row did, and whether the code and the message
    could disagree. Now the CODE picks the fields — `missing_fields` and the
    message key both come from the registry in `clarification.py` — so a branch
    cannot ship a code that contradicts its own copy, and adding a branch without
    registering its code fails a test rather than going quiet.

    The metric rides the existing `error_code` column. It is not an error, and
    that is a deliberate reuse: the column is the row's machine-readable
    discriminant, renaming it would touch every reader, and a clarification that
    is not distinguishable from the other five is the defect this closes.
    """
    missing = missing_fields_for(code)
    if code == CLARIFY_LANGUAGE_UNROUTABLE:
        message = free_text_unavailable_message(language)
    else:
        message = clarification_message(language, MESSAGE_KEY_BY_CODE[code])
    _record_route_metric(context, caller, route_type="clarification", outcome="completed", error_code=code)
    # The structural line the ticket asked for, minus the two things that must
    # never be in it: no identity, no order number — only the hashes the FAQ
    # module already logs for the same reason. It has somewhere to go now:
    # `gateway_logging` attaches the root handler uvicorn never did.
    _LOGGER.info(
        "clarification_answered code=%s language=%s missing=%s subject_hash=%s tenant_hash=%s",
        code,
        language,
        ",".join(missing) or "-",
        _hash_identifier(getattr(getattr(caller, "subject", None), "b_user_id", "") or ""),
        _hash_identifier(getattr(caller, "effective_tenant_id", "") or ""),
    )
    return {
        **decision.public(),
        "type": "clarification",
        "language": language,
        "question": question,
        "missing_fields": missing,
        "message": message,
        "code": code,
    }


def _unowned_order_code(caller: ScopeContext | None) -> str:
    """Which of the two "you cannot see this order" states this is (#637).

    The two states were one message, and the message named the ORDER — so an
    account whose operator site set resolves to EMPTY (`no_shop_binding`, or a
    registered shop with no sites in the charging database) was told "this order
    is not yours". That sends the reader to look at the order and the order list,
    both of which are fine, while the thing to fix — the missing binding — is
    unreachable from the sentence. It happened for real: the product owner read
    it as an order problem and re-checked the order twice.

    The distinction is `caller.data_scope`, which the identity layer already
    decided: `DataScope.site_ids == ()` on an `organ` scope means "no site is
    visible to this account", and `scope_where_sql` renders exactly that set as
    `1=0`. An account that can see no site can see no order, whoever's it is.

    Deliberately narrow: `None` site_ids is `all`-in-tenant (unrestricted) and
    never takes this branch, and a tenant-level account never gets an `organ`
    scope at all (#628) — for those, "no site" is normal and the order statement
    remains the true one.
    """
    scope = getattr(caller, "data_scope", None)
    if scope is None:
        return CLARIFY_ORDER_NOT_YOURS
    if getattr(scope, "type", "") != SCOPE_TYPE_ORGAN:
        return CLARIFY_ORDER_NOT_YOURS
    if getattr(scope, "site_ids", None):
        return CLARIFY_ORDER_NOT_YOURS
    return CLARIFY_ACCOUNT_HAS_NO_SITES


def _hash_identifier(value: str) -> str:
    """Short, non-reversible marker for a log line — never the identifier itself."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _record_route_metric(
    context: Any,
    caller: ScopeContext | None,
    *,
    route_type: str,
    outcome: str,
    agent_id: str | None = None,
    agent_version_key: str | None = None,
    conversation_id: str | None = None,
    error_code: str | None = None,
) -> None:
    """Best-effort redacted route metric (T7/#174); never fails the request."""
    if caller is None:
        return
    recorder = getattr(context.runtime, "record_route_metric", None)
    if recorder is None:
        return
    with contextlib.suppress(Exception):
        recorder(
            caller,
            route_type,
            outcome,
            agent_id=agent_id,
            agent_version_key=agent_version_key,
            conversation_id=conversation_id,
            error_code=error_code,
        )


def _begin_conversation_turn(
    context: Any,
    caller: ScopeContext | None,
    conversation: dict[str, Any] | None,
    kind: str,
    question: str,
) -> int | None:
    """Claim the conversation's generation slot; 409 when busy."""
    if conversation is None:
        return None
    try:
        return context.conversation_store.begin_turn(
            conversation["conversation_id"],
            conversation["scope_fingerprint"],
            kind=kind,
            question=question,
        )
    except ConversationBusy as exc:
        _record_route_metric(
            context,
            caller,
            route_type=kind,  # "qa" | "diagnosis" — never misattribute a busy turn
            outcome="busy",
            error_code="CONVERSATION_BUSY",
            conversation_id=conversation.get("conversation_id"),
        )
        raise StandardAPIError(
            status.HTTP_409_CONFLICT,
            "CONVERSATION_BUSY",
            "this conversation is already generating a reply",
            retryable=False,
        ) from exc
    except ConversationError as exc:
        raise StandardAPIError(
            status.HTTP_404_NOT_FOUND,
            "CONVERSATION_NOT_FOUND",
            "conversation not found",
        ) from exc


#: The message every "a runtime dependency is down" answer carries. The body is
#: unchanged from the 12 inline copies this replaces — the point of the helper is
#: that the failure is *recorded*, not that the text improves.
_RUNTIME_FAILURE_MESSAGE = "runtime dependency unavailable"


def _runtime_unavailable(code: str, message: str, exc: Exception) -> StandardAPIError:
    """Render a runtime failure as the 503 the contract promises, and record it.

    The contract (`docs/standard-api-contract.md:355-356`) says internal failure
    detail goes to the sanitised log and that the response carries no exception
    type, SQL, absolute path, password or raw upstream response. The 12 copies
    this replaces did the second half and dropped `exc` entirely: a misconfigured
    field, an unreachable upstream and a provider outage read identically in the
    response (correct, by contract) **and in the log** (the defect).

    What is logged is **the exception type and the failure's own vocabulary** —
    never `str(exc)`. That is the narrower choice, and it is deliberate:

    * `redact_text` is pattern-based. It removes credential *shapes*; it does not
      remove free-form text, and a `SourceError` may quote a field contract or a
      backend response while an upstream driver's message may quote the statement
      that failed. Passing those through would put request-adjacent content into
      gateway logs, which is the very thing the contract forbids.
    * The type plus the error code is what tells an operator which failure they
      are looking at: `SourceError`/`MYSQL_UNAVAILABLE` is a backend problem,
      `AgentRuntimeError`/`PROVIDER_UNAVAILABLE` is a provider problem. That is
      the whole diagnostic question this ticket exists to answer.

    The response body is byte-identical to before, on purpose: the contract
    promises "a stable 503", and turning this into a diagnostic channel for
    callers is a different decision that nobody has made.
    """
    _LOGGER.warning("runtime failure -> %s (%s)", code, type(exc).__name__)
    return StandardAPIError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        code,
        message,
        retryable=True,
    )


def _release_conversation_turn(
    context: Any,
    conversation: dict[str, Any] | None,
    turn_no: int | None,
) -> None:
    """Drop an unfinished turn row and free the slot (best effort)."""
    if conversation is None or turn_no is None:
        return
    with contextlib.suppress(ConversationError):
        context.conversation_store.release_turn(
            conversation["conversation_id"], conversation["scope_fingerprint"], turn_no
        )


_ACTIVE_ORDER_CUES = re.compile(r"订单|充值|充电|退款|押金|金额|费用|订单号|为什么.*停|怎么还没")
# Cues that mark a BILLING DISPUTE — a user ASSERTING their money is wrong.
#
# Covers every supported language. The Chinese-only list let every
# non-Chinese user past the guard into generic customer service: an English
# "was I overcharged" produced a plain QA answer with no order prompt at all
# (41 live, 2026-09-20). Chinese got away with bare nouns because 扣费 /
# 账单 / 金额不对 ARE claims about a specific charge.
#
# The other languages use PHRASES, not bare topic nouns, for the same reason
# the guard exists: `refund` / `remboursement` / `reembolso` alone names a
# subject, and the FAQ carries entries on exactly those subjects (q020/q021).
# Treating a topic word as a dispute would swallow the questions the FAQ
# short-circuit exists to serve — and it is not hypothetical: the first draft
# of this list included bare `refund` and broke
# test_assistant_faq_shortcircuit_ignores_generic_single_tokens.
_HIGH_RISK_ORDER_CUES = re.compile(
    # zh
    r"扣费|扣款|扣错|费用异常|金额不对|退款|退费|订单异常|订单问题|账单"
    # en
    r"|overcharg\w*|double[-\s]?charg\w*"
    r"|(?:bill|amount|charge)\w*\b[^.]{0,24}?\bwrong\b|\bwrong\b[^.]{0,24}?\b(?:bill|amount|charge)\w*"
    r"|charged\s+(?:but|however|twice|two)"
    r"|refund\s+(?:not|never|has\s+not|hasn't|still)"
    # de
    r"|überladen|überteuert|zu\s+viel\s+berechnet|doppelt\s+berechnet"
    r"|(?:rechnung|betrag|abrechnung)\w*\b[^.]{0,24}?\bfalsch\b|\bfalsch\b[^.]{0,24}?\b(?:rechnung|abrechnung)\w*"
    # fr
    r"|surfactur\w+|factur\w+\s+(?:en\s+trop|deux\s+fois)"
    r"|(?:facture|montant|facturation)\w*\b[^.]{0,24}?\b(?:incorrect|erroné|faux)\w*"
    # es
    r"|cobrad\w+\s+de\s+m[áa]s|cobrad\w+\s+dos\s+veces"
    r"|(?:factura|importe|cobro)\w*\b[^.]{0,24}?\b(?:incorrect|erróne|equivocad)\w*"
    # pt
    r"|cobrad\w+\s+a\s+mais|cobrad\w+\s+duas\s+vezes"
    r"|(?:fatura|valor|cobrança)\w*\b[^.]{0,24}?\b(?:incorret|errad|equivocad)\w*",
    re.IGNORECASE,
)


def _question_involves_active_order(question: str) -> bool:
    """Heuristic: does this follow-up clearly involve the active order?

    Deliberately conservative — only order-business words trigger the
    active_order shortcut. Plain knowledge questions ("怎么申请会员") fall
    through to qa+RAG even with an active order bound (#172 acceptance).
    """
    return bool(_ACTIVE_ORDER_CUES.search(question or ""))


def _missing_order_context_key(question: str) -> str | None:
    """The clarification key for a high-risk question, or None.

    Returns the key rather than the rendered text so the reply is localized
    by the shared ``clarification_message`` catalog. The previous form
    returned a Chinese literal regardless of Accept-Language — the same
    hardcoded-copy defect fixed on the other two clarification branches
    (#262, #282).
    """
    if _HIGH_RISK_ORDER_CUES.search(question or ""):
        return "order_no"
    return None


def _promo_route(
    context: Any,
    caller: ScopeContext,
    decision: Any,
    payload: AssistantQuestionRequest,
    *,
    forced_intent: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve the promotional route for this request, or (None, None).

    Two entry signals (#231): an explicit ``shortcut_code`` from a clicked
    product-entry button, or free text that deterministically NAMES case
    exploration / solution discovery. ``forced_intent`` is the classifier's
    promotional intent when the cue matcher stayed silent. The promotional
    target always comes from the caller's own published shortcut row — the
    frontend can never self-report an agent. A missing target still routes
    to the promotional intent so the runtime can serve the honest empty card.
    """
    from aiops_diagnostics.promo_agents import PROMO_INTENTS, promo_intent_from_text
    from aiops_diagnostics.shortcut_lifecycle import ShortcutError

    intent: str | None = None
    shortcut = None
    try:
        if payload.shortcut_code:
            shortcut = context.shortcut_manager.store.find_effective_by_code(
                caller.effective_tenant_id, str(decision.platform), payload.shortcut_code
            )
            if shortcut is not None and shortcut.status == "published":
                intent = shortcut.intent if shortcut.intent in PROMO_INTENTS else None
        if intent is None:
            intent = (
                forced_intent if forced_intent in PROMO_INTENTS else promo_intent_from_text(payload.question)
            )
    except ShortcutError:
        return None, None
    if intent is None:
        return None, None
    target = getattr(shortcut, "target_agent_version", None) if shortcut is not None else None
    if not target:
        target = _published_promo_target(context, caller, str(decision.platform), intent)
    return target, intent


def _published_promo_target(context: Any, caller: ScopeContext, platform: str, intent: str) -> str | None:
    """Pin the first published shortcut of this promotional intent, if any."""
    from aiops_diagnostics.shortcut_lifecycle import ShortcutError

    try:
        rows = context.shortcut_manager.list_effective(caller, business_entry=platform)
    except ShortcutError:
        return None
    for row in rows:
        if row.intent == intent and row.target_agent_version:
            return row.target_agent_version
    return None


def _start_promo_qa(
    context: Any,
    caller: ScopeContext,
    conversation: dict[str, Any] | None,
    payload: AssistantQuestionRequest,
    language: str,
    promo_target: str | None,
    promo_intent: str,
    platform: str,
) -> JSONResponse:
    """Start a promotional card QA job (#231): the public `qa` contract
    unchanged (202 + poll), served by the pinned promotional agent."""
    turn_no: int | None = None
    if conversation is not None:
        turn_no = _begin_conversation_turn(context, caller, conversation, "qa", payload.question)
    try:
        qa = context.runtime.start_assistant_qa(
            caller,
            payload.question,
            conversation=conversation if turn_no is not None else None,
            conversation_turn_no=turn_no,
            language=language,
            promo_target=promo_target,
            promo_intent=promo_intent,
            business_entry=platform,
        )
    except (ValueError, RuntimeError) as exc:
        _release_conversation_turn(context, conversation, turn_no)
        raise _runtime_unavailable("QA_UNAVAILABLE", "general answer unavailable", exc) from exc
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            **_assistant_question_response(qa, language),
            **(
                {"conversation_id": conversation["conversation_id"], "turn_no": turn_no}
                if conversation
                else {}
            ),
        },
    )


def _shortcut_list_language(shortcuts: Any, requested: str) -> str:
    """The language a shortcut listing is uniformly in, else the authority one.

    Rows are published independently, so a listing can mix a translated row with
    an untranslated one. A single top-level field cannot describe that, and
    claiming the requested language for a mixed list is the same false statement
    as claiming it for one fallback row — so it reports the shared language only
    when there is one, and the per-row `language` carries the truth otherwise.
    """
    if not shortcuts:
        # No rows means no text, so there is nothing to contradict the request:
        # reporting the authority language here would describe a payload that
        # does not exist.
        return requested
    served = {item.served_language(requested) for item in shortcuts}
    return served.pop() if len(served) == 1 else DEFAULT_LANGUAGE


#: How far back a *clicked* operations-review action looks. The server owns
#: this number because a window is a 口径: if the frontend picks it, there is
#: one definition per client and two clients can disagree about the same card.
#: Callers that must choose a window use ``POST /v1/standard/analyses``, which
#: takes it explicitly.
#:
#: 24 hours is a **engineering default, not a business calibration** — nothing
#: in the repo has ever defined a review window, and changing it is this line.
OPERATIONS_REVIEW_WINDOW_HOURS = 24


def _start_operations_review(
    context: Any,
    caller: ScopeContext,
    decision: Any,
    payload: AssistantQuestionRequest,
    *,
    language: str,
) -> JSONResponse:
    """Start the operations-review card the operator clicked, and answer 202.

    The body mirrors the ``type: "diagnosis"`` shape (an id + the polled
    window) rather than inventing a second convention: the client polls the
    same analysis surface it would have used anyway, and this action adds no
    new contract — only a way to reach an existing one.

    ``site_ids=None`` means "the caller's whole resolved site set", the same
    convention ``POST /v1/standard/analyses`` uses: the worker intersects it
    with the caller's live scope, so a click can never read wider than the
    caller may see.
    """
    now = datetime.now(UTC)
    window_start = now - timedelta(hours=OPERATIONS_REVIEW_WINDOW_HOURS)
    try:
        job = context.runtime.start_operations_analysis(
            caller,
            site_ids=None,
            window_start=window_start.isoformat(),
            window_end=now.isoformat(),
            language=language,
        )
    except (ValueError, RuntimeError) as exc:
        raise _runtime_unavailable("ANALYSIS_UNAVAILABLE", "analysis unavailable", exc) from exc
    base = _analysis_job_response(job, language)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            **base,
            "type": "analysis",
            "shortcut_code": OPERATIONS_REVIEW_CODE,
        },
    )


def _analysis_job_response(job: dict[str, Any], language: str) -> dict[str, Any]:
    """One analysis row as the public body (第三份契约，see ``ops_analysis``).

    Deliberately NOT ``_standard_diagnosis_response``: that builder is the
    single-order face, and its ``order_no`` / ``question`` / ``indicator_code``
    fields describe an incident this job does not have. Reusing it would put an
    empty ``order_no`` on the wire for an analysis — the shape equivalent of
    saying "this card is about an order".
    """
    status_value = str(job["status"])
    error_code = job.get("error_code") or "ANALYSIS_FAILED"
    is_terminal = status_value in TERMINAL_DIAGNOSIS_STATUSES
    return {
        "analysis_id": job["diagnosis_id"],
        "kind": job.get("kind") or "analysis",
        # Which card this job is. A poller that only remembers the id still gets
        # the right shape back, instead of having to remember the route it used.
        "card": job.get("card") or "failure_rate",
        "site_ids": json.loads(job["site_ids_json"]) if job.get("site_ids_json") else None,
        "window_start": job.get("window_start"),
        "window_end": job.get("window_end"),
        "rule_version": job.get("rule_version"),
        "language": job.get("language", "zh"),
        "status": status_value,
        "retry_after_ms": None if is_terminal else 1000,
        "result": job.get("result") if status_value in DIAGNOSIS.completed else None,
        "error": (
            {
                "code": error_code,
                # Same split as the diagnosis face: the message is rendered per
                # request from the code; the stored note is the engineer's.
                "message": diagnosis_error_message(language, error_code),
                "retryable": status_value in {DIAGNOSIS.failed, DIAGNOSIS.expired},
            }
            if status_value in {DIAGNOSIS.failed, DIAGNOSIS.expired}
            else None
        ),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "completed_at": job.get("completed_at"),
    }


def _standard_diagnosis_response(diagnosis: dict[str, Any], language: str) -> dict[str, Any]:
    """One diagnosis row as the public body.

    ``language`` is the language of THIS REQUEST, and it renders only the copy
    the server writes now: `error.message`.

    It deliberately does NOT drive the `language` field or `result`. A
    diagnosis's prose is generated once, in the language it was STARTED in, and
    is stored. Reporting the poller's language there would claim an English
    diagnosis is French — a false statement about the payload, and the same
    class of error this project has already refused elsewhere ("do not rewrite
    a successful retrieval into `unavailable`"). The field therefore describes
    the RESULT's language, not the request's; that had never been written down,
    and two surfaces had drifted apart because of it (#549).

    So the two are split by what they describe, not by which is newer:
    per-request copy is rendered per request, stored prose is reported as
    stored. The unified-assistant QA surface merges them into one field; this
    one does not, and this comment is the contract.
    """
    status_value = str(diagnosis["status"])
    error_code = diagnosis.get("error_code") or "DIAGNOSIS_FAILED"
    # The shared status set decides what is terminal, exactly as it decides what
    # the store accepts: a literal copy here is how a status the store now takes
    # (a stopped diagnosis) would render `retry_after_ms=1000` and keep a client
    # polling a row the claim-guard can never let change again.
    is_terminal = status_value in TERMINAL_DIAGNOSIS_STATUSES
    return {
        "diagnosis_id": diagnosis["diagnosis_id"],
        "order_no": diagnosis["order_no"],
        "question": diagnosis.get("question"),
        "indicator_code": diagnosis.get("indicator_code"),
        "language": diagnosis.get("language", "zh"),
        "status": status_value,
        "retry_after_ms": None if is_terminal else 1000,
        "result": diagnosis.get("result") if status_value in DIAGNOSIS.completed else None,
        "error": (
            {
                "code": error_code,
                # Bounded and localized, chosen by the code — NOT the stored
                # `error_message`. That field is the engineer's note (an
                # upstream exception string, a restart notice); the contract
                # promises this surface carries no internal run information.
                "message": diagnosis_error_message(language, error_code),
                "retryable": status_value in {DIAGNOSIS.failed, DIAGNOSIS.expired},
            }
            if status_value in {DIAGNOSIS.failed, DIAGNOSIS.expired}
            else None
        ),
        "created_at": diagnosis.get("created_at"),
        "updated_at": diagnosis.get("updated_at"),
        "completed_at": diagnosis.get("completed_at"),
    }
