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
from aiops_diagnostics.deployment_environment import canonical_environment
from aiops_diagnostics.gateway_logging import DEFAULT_LOG_LEVEL, parse_log_level
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
    charging_status_base_url: str = ""
    execution_environment: str = ""
    #: M2-CLOSE-01：执行环境 → 编排平台应用/智能体/节点的**可信绑定**（契约 §3.1）。
    #: 缺省为空 ⇒ 委派入口整体不启用（fail closed，与 ``third_session_service_token``
    #: 同一条「全空即与今天逐字一致」的形状）。路径必须指向 0600 的 JSON：它持有
    #: Gateway 自己的平台应用令牌（明文），而节点凭据只存 SHA-256。
    agent_run_config_file: str = ""
    #: M3：受控**执行配置清单**（期望值的唯一来源）。缺省为空 ⇒ 守卫不启用，行为与今天
    #: 逐字一致；配了就必须读得到且权限合规，否则**启动即拒**（不降级）。
    execution_manifest_file: str = ""
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
    #: 每回合向知识库请求的 chunk 数上限 —— **两条检索路径共用的同一个口径**
    #: （客户问答/宣传卡片的 ``KnowledgeSearchGuard.max_results``，以及 Dify 适配路由
    #: 对 Dify 所发 ``top_k`` 的封顶值）。
    #:
    #: 默认 10 而不是 5，是实测定的：`chunk_method=picture` 的知识库把图片块按相似度
    #: 排在中段（实测全部 chunk 挤在 0.185~0.201），k=5 时 16 种真实 query 形状里只有
    #: 5 种能取到媒体块，k=10 是 12 种，k=15 以上不再增加。**取多少个**归这里管；
    #: **怎么排**（分数分布、rerank）不归它管。
    kb_search_max_results: int = 10
    media_signing_secret: str = ""
    media_ttl_seconds: int = 600
    #: 本网关**对外**的基地址（``https://host``，无路径）。媒体 URL 默认是相对的
    #: ``/v1/media/...``，因为 APP 与网关同源；但 Dify 的记录会离开本网关 —— Dify 自己的
    #: 控制台、以及它转交给模型的上下文，都会拿到这个字符串。相对路径在那里会解析到
    #: **Dify 的主机**上（36 的 nginx 把 ``/v1`` 转给 ``api:5001``），结果是每张图都 404，
    #: 而且看起来像"我们的媒体面坏了"。留空 = 保持相对路径（同源调用方的原有行为）。
    public_base_url: str = ""
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
    #: Dify 工具身份 → 我们的 ``ToolName`` 的登记表（``provider_id/tool_name:our_tool``，
    #: 多行/分号分隔）。Dify 的工具名与我们的 ``ToolName`` **不是同一个命名空间**
    #: （实测：``provider_id=time`` / ``tool_name=timezone_conversion``），所以只能靠
    #: 一张**显式登记表**翻译 —— 与知识库那条桥同形。空表 = 不认任何 Dify 工具声明
    #: （声明了就拒，绝不按字面搬运）。
    dify_tool_bindings: str = ""
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
    #: **试运行**（`POST /v1/dify/trial`，#772 F / #781）的专用凭据。留空 ⇒ 整组路由 404，
    #: 与其他 Dify 路由同一条「未配置即不启用」的机制。
    #:
    #: 第六把独立钥匙。它触发一次**真实模型执行**（真跑执行器与只读数据源），所以不能
    #: 与检索面（`dify_knowledge_api_key`）、调试面、发布面（`dify_publish_api_key`）、
    #: 工具目录面、以及我们调 Dify 用的那把（`dify_console_api_key`）同值 —— 同值等于
    #: 把"能列目录"或"能发布"绑成了"能在真数据上跑一次诊断"。启动期校验它们互不相同。
    dify_trial_api_key: str = field(repr=False, default="")
    #: **试运行**的固定调试租户（#772 F / #586）。它**不可由请求改写**：试运行的订单范围
    #: 由它（经 :func:`admin_context`）决定，浏览器提交的任何租户/站点/用户字段一律忽略。
    #: 留空 ⇒ 试运行整体不可用（与凭据成对，半配置是启动错误）—— 没有固定租户就等于
    #: "按请求给的租户跑"，那正是 #782 要接管的按人范围，不是本票的语义。
    dify_trial_debug_tenant: str = ""
    #: Dify 控制台导出端点的**只读凭据**（#625）。它服务的是「我方发布动作」这一跳：
    #: `aiops admin pull-dify` 用它把运营在 Dify 里改好的配置拉回来。**与服务运行时的
    #: 任何凭据都不同** —— 上面那几把是 Dify 调我们时用的，这把是我们调 Dify 时用的。
    #: 留空 ⇒ 这个动作整体不可用（不半配置：地址有值而凭据为空时是启动错误）。
    dify_console_base_url: str = ""
    dify_console_api_key: str = field(repr=False, default="")
    #: 导出请求要带的 workspace（Dify 的 admin-key 路径从它解析租户 owner）。留空 ⇒ 不带该头。
    dify_console_workspace_id: str = ""
    #: **运营自助发布**的共享凭据（`POST /v1/dify/publish`）。留空 ⇒ 该路由整体 404，
    #: 与 ``dify_knowledge_api_key`` 同一条「未配置即不启用」的机制。
    #:
    #: 它为什么**必须**与 ``dify_knowledge_api_key`` 分开：那把是 Dify 调**知识检索**用的，
    #: 会在每一个检索请求上出现；这把能触发一次**发布**。合成一把会让"运营的预览 app 能查
    #: 知识库"顺带等于"谁能查知识库就能发布" —— 那是把两件不同的事绑成一个权限。
    #: 另一半是它必须与 ``dify_console_api_key`` 分开：那把在**服务端**，这把要给到
    #: **触发方**，一把钥匙两处持有就不再是"服务端凭据"了。
    dify_publish_api_key: str = field(repr=False, default="")
    #: **3A 工具说明端点**（`POST /v1/dify/tools/<tool>`）的共享凭据。留空 ⇒ 整组路由 404，
    #: 与 `dify_knowledge_api_key` 同一条「未配置即不启用」的机制。
    #:
    #: 五把钥匙各自的理由，为什么这一把必须**再新开一把**（#772 E）：它是 Dify 侧
    #: **每一个**能编辑该 API 工具提供方的人都看得见的那把（Dify 的提供方凭据是工作区
    #: 共享的），所以它必须是**只能读到一段固定说明文本**的钥匙 —— 不能查知识库
    #: （`dify_knowledge_api_key`），不能查调试面（`dify_debug_api_key`），不能发布
    #: （`dify_publish_api_key`），也不是我们调 Dify 用的那把（`dify_console_api_key`）。
    #: 用法相同（登记表里的每一对都各有理由）而**权限最小**，这正是 3A 端点除了说明
    #: 文本什么都不返回的原因：泄露这把钥匙拿不到任何业务数据。
    dify_tools_api_key: str = field(repr=False, default="")
    #: **本部署的环境标识**（ADR-0059 / #879）。每台机器一个值，落进
    #: ``dify_workspaces.environment``，并决定"这个租户的工作区是哪一个"。
    #:
    #: **必填、无默认值。** 给默认值会让漏配的部署静默退化成一个所有机器共用的环境 ——
    #: 那正好把 ``environment`` 列挡的东西（两套 UPMS 的 ``tenant_id`` 撞号）放回来，
    #: 而且不报错。所以漏配或写成 ``prod.41`` 这类非规范形式都是**启动失败**。
    #:
    #: 判据只有一处实现（``deployment_environment.canonical_environment``），登录代理
    #: 的 ``AIOPS_LOGIN_PROXY_ENVIRONMENT`` 用的是同一份 —— 两处各写一份会漂开，
    #: 一处拒绝的值另一处照收。
    environment: str = ""
    #: 网关自己的**内部令牌密钥**（HMAC-SHA256，与平台 ``InternalTokenManager`` 同一
    #: 消息格式）。目前唯一的消费者是 ``GET /v1/dify/workspace`` —— 登录代理与发布桥
    #: 都不在网关进程里，它们要问"这个租户的工作区是哪一个"就必须有一把**进程间**凭据。
    #:
    #: 留空 ⇒ 该端点整体 404（与其余 Dify 路由同一条「未配置即不启用」的机制）。
    #: 它**不复用**任何 Dify 侧钥匙：那几把是 Dify 调我们时用的，这把是我们自己的
    #: 进程之间用的，且不暴露给 Dify。
    internal_token_secret: str = field(repr=False, default="")
    #: 内部令牌的有效窗口（秒），与平台 ``InternalTokenManager`` 缺省一致（300）。
    internal_token_expire_seconds: int = 300
    #: **建 workspace 的唯一 HTTP 入口**（``inner_api``，#880）。地址**必须是配置项**：
    #: 今天是 api 容器的内网 ``IP:5001``（36 本机可用），转发器上线后换成
    #: ``http://127.0.0.1:15001`` —— 代码一行不改，只改这一行值。
    #:
    #: 留空 ⇒ ``aiops admin ensure-workspace`` 拒绝运行（它没有别的建法）。它**不是**
    #: 网关运行时要用的东西：只有开户那一条 CLI 路径读它。
    dify_inner_api_base_url: str = ""
    dify_inner_api_key: str = field(repr=False, default="")
    #: 一租户一 RAGFlow 租户的凭据加密密钥（#924 / PRD #923）。开户把 RAGFlow 的 owner
    #: 密码写进 `ragflow_tenants` 时用它加密，登录代理（#926）读出来时用它解密。
    #:
    #: 16/24/32 字节。**留空 ⇒ 开户与轮换拒绝运行**，而不是"存个明文凑合" —— 一个
    #: 没有密钥的凭据保管就是明文保管。它不是网关运行时读的东西：只有那两条 CLI 路径读它。
    ragflow_credential_key: str = field(repr=False, default="")
    #: RAGFlow 账号的邮箱域（派生 `sha1(tenant_id)[:16]@domain`）。与 kb-service 懒注册
    #: 用的是同一个形状，所以一个在此命令之前懒注册过的租户不会因为域名不同被当成两个人。
    ragflow_tenant_domain: str = "kb.internal"
    #: 开户时做**一次真实 embedding 调用**要用的端点与密钥（#924）。校验存在的理由很直白：
    #: 41 上出现过一个配置齐全但密钥被供应商禁用的租户，每一份文档都解析失败，而界面上
    #: 只写"解析失败"。只读配置的检查会把那种租户判成健康。
    ragflow_embedding_base_url: str = ""
    ragflow_embedding_api_key: str = field(repr=False, default="")
    ragflow_embedding_model: str = "text-embedding-3-large"
    #: Conversation context window (#482). The defaults ARE the contract's
    #: numbers (8 turns / 8k tokens, ``conversation_store`` owns the constants);
    #: they are configurable because a RAG or promotional turn also spends the
    #: model's budget on retrieved chunks, and the only honest way to tune that
    #: trade-off is a knob rather than a second hardcoded copy.
    context_max_turns: int = CONTEXT_MAX_TURNS
    context_max_tokens: int = CONTEXT_MAX_TOKENS
    #: 网关自身日志的最低级别（#636）。**默认是代码一直假定的那一级**：`INFO`。
    #: 在这一项存在之前，网关根本没配过 logging —— uvicorn 只给 `uvicorn*` 挂 handler，
    #: root 停在 WARNING 且无 handler，于是 `aiops.*` 的 INFO 行**一行都没出去过**
    #: （41 的 journal 实测 0 行）。级别做成设置是因为调它是运维动作，
    #: 而为了改一个日志级别重新部署是错的代价。非法值在 `validate()` 里是**启动错误**。
    log_level: str = DEFAULT_LOG_LEVEL

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
            charging_status_base_url=_env("AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL")
            or _file_value(file_values, "AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL"),
            execution_environment=_env("AIOPS_GATEWAY_EXECUTION_ENVIRONMENT")
            or _file_value(file_values, "AIOPS_GATEWAY_EXECUTION_ENVIRONMENT"),
            agent_run_config_file=_env("AIOPS_GATEWAY_AGENT_RUN_CONFIG_FILE")
            or _file_value(file_values, "AIOPS_GATEWAY_AGENT_RUN_CONFIG_FILE"),
            execution_manifest_file=_env("AIOPS_GATEWAY_EXECUTION_MANIFEST_FILE")
            or _file_value(file_values, "AIOPS_GATEWAY_EXECUTION_MANIFEST_FILE"),
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
            # Reads the private server config too, not just the process env: the
            # depth lives beside the KB URL it governs, and `kb_service_base_url`
            # two lines up already accepts either source. An operator who put it
            # in the file beside that URL would otherwise get a startup that
            # succeeds and a setting that silently does nothing.
            kb_search_max_results=_int_value(file_values, "AIOPS_GATEWAY_KB_SEARCH_MAX_RESULTS", 10),
            media_signing_secret=_env("AIOPS_GATEWAY_MEDIA_SIGNING_SECRET")
            or _file_value(file_values, "AIOPS_GATEWAY_MEDIA_SIGNING_SECRET"),
            media_ttl_seconds=_env_int("AIOPS_GATEWAY_MEDIA_TTL_SECONDS", 600),
            public_base_url=_env("AIOPS_GATEWAY_PUBLIC_BASE_URL")
            or _file_value(file_values, "AIOPS_GATEWAY_PUBLIC_BASE_URL"),
            dify_knowledge_api_key=_env("AIOPS_GATEWAY_DIFY_KNOWLEDGE_API_KEY")
            or _file_value(file_values, "AIOPS_GATEWAY_DIFY_KNOWLEDGE_API_KEY"),
            dify_knowledge_bindings=_env("AIOPS_GATEWAY_DIFY_KNOWLEDGE_BINDINGS")
            or _file_value(file_values, "AIOPS_GATEWAY_DIFY_KNOWLEDGE_BINDINGS"),
            dify_tool_bindings=_env("AIOPS_GATEWAY_DIFY_TOOL_BINDINGS")
            or _file_value(file_values, "AIOPS_GATEWAY_DIFY_TOOL_BINDINGS"),
            dify_debug_api_key=_env("AIOPS_GATEWAY_DIFY_DEBUG_API_KEY")
            or _file_value(file_values, "AIOPS_GATEWAY_DIFY_DEBUG_API_KEY"),
            dify_debug_tenant=_env("AIOPS_GATEWAY_DIFY_DEBUG_TENANT")
            or _file_value(file_values, "AIOPS_GATEWAY_DIFY_DEBUG_TENANT"),
            dify_debug_knowledge_ids=_env("AIOPS_GATEWAY_DIFY_DEBUG_KNOWLEDGE_IDS")
            or _file_value(file_values, "AIOPS_GATEWAY_DIFY_DEBUG_KNOWLEDGE_IDS"),
            dify_trial_api_key=_env("AIOPS_GATEWAY_DIFY_TRIAL_API_KEY")
            or _file_value(file_values, "AIOPS_GATEWAY_DIFY_TRIAL_API_KEY"),
            dify_trial_debug_tenant=_env("AIOPS_GATEWAY_DIFY_TRIAL_DEBUG_TENANT")
            or _file_value(file_values, "AIOPS_GATEWAY_DIFY_TRIAL_DEBUG_TENANT"),
            dify_console_base_url=_env("AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL")
            or _file_value(file_values, "AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL"),
            dify_console_api_key=_env("AIOPS_GATEWAY_DIFY_CONSOLE_API_KEY")
            or _file_value(file_values, "AIOPS_GATEWAY_DIFY_CONSOLE_API_KEY"),
            dify_console_workspace_id=_env("AIOPS_GATEWAY_DIFY_CONSOLE_WORKSPACE_ID")
            or _file_value(file_values, "AIOPS_GATEWAY_DIFY_CONSOLE_WORKSPACE_ID"),
            dify_publish_api_key=_env("AIOPS_GATEWAY_DIFY_PUBLISH_API_KEY")
            or _file_value(file_values, "AIOPS_GATEWAY_DIFY_PUBLISH_API_KEY"),
            dify_tools_api_key=_env("AIOPS_GATEWAY_DIFY_TOOLS_API_KEY")
            or _file_value(file_values, "AIOPS_GATEWAY_DIFY_TOOLS_API_KEY"),
            environment=_env("AIOPS_GATEWAY_ENVIRONMENT")
            or _file_value(file_values, "AIOPS_GATEWAY_ENVIRONMENT"),
            internal_token_secret=_env("AIOPS_GATEWAY_INTERNAL_TOKEN_SECRET")
            or _file_value(file_values, "AIOPS_GATEWAY_INTERNAL_TOKEN_SECRET"),
            # 与 `kb_search_max_results` 同一条规则：密钥在私有配置里，窗口也该能在它旁边调。
            # 只读进程 env 时，运维改了 `production.env` 里的这一行会**静默不生效** ——
            # 而它旁边那把密钥是从文件里读到的，读者有充分理由认为两者同源。
            internal_token_expire_seconds=_int_value(
                file_values, "AIOPS_GATEWAY_INTERNAL_TOKEN_EXPIRE_SECONDS", 300
            ),
            dify_inner_api_base_url=_env("AIOPS_GATEWAY_DIFY_INNER_API_BASE_URL")
            or _file_value(file_values, "AIOPS_GATEWAY_DIFY_INNER_API_BASE_URL"),
            dify_inner_api_key=_env("AIOPS_GATEWAY_DIFY_INNER_API_KEY")
            or _file_value(file_values, "AIOPS_GATEWAY_DIFY_INNER_API_KEY"),
            ragflow_credential_key=_env("AIOPS_GATEWAY_RAGFLOW_CREDENTIAL_KEY")
            or _file_value(file_values, "AIOPS_GATEWAY_RAGFLOW_CREDENTIAL_KEY"),
            ragflow_tenant_domain=_env("AIOPS_GATEWAY_RAGFLOW_TENANT_DOMAIN", "kb.internal"),
            ragflow_embedding_base_url=_env("AIOPS_GATEWAY_RAGFLOW_EMBEDDING_BASE_URL")
            or _file_value(file_values, "AIOPS_GATEWAY_RAGFLOW_EMBEDDING_BASE_URL"),
            ragflow_embedding_api_key=_env("AIOPS_GATEWAY_RAGFLOW_EMBEDDING_API_KEY")
            or _file_value(file_values, "AIOPS_GATEWAY_RAGFLOW_EMBEDDING_API_KEY"),
            ragflow_embedding_model=_env("AIOPS_GATEWAY_RAGFLOW_EMBEDDING_MODEL", "text-embedding-3-large"),
            jev_base_url=_env("AIOPS_GATEWAY_JEV_BASE_URL")
            or _file_value(file_values, "AIOPS_GATEWAY_JEV_BASE_URL"),
            jev_api_key=_env("AIOPS_GATEWAY_JEV_API_KEY")
            or _file_value(file_values, "AIOPS_GATEWAY_JEV_API_KEY"),
            jev_model=_env("AIOPS_GATEWAY_JEV_MODEL", "typesafe/jev"),
            jev_timeout_seconds=_env_float("AIOPS_GATEWAY_JEV_TIMEOUT_SECONDS", 20.0),
            routing_risk_at_least=_env_float("AIOPS_GATEWAY_ROUTING_RISK_AT_LEAST", 0.5),
            routing_confidence_at_least=_env_float("AIOPS_GATEWAY_ROUTING_CONFIDENCE_AT_LEAST", 0.8),
            routing_risk_always_asks=_env_bool("AIOPS_GATEWAY_ROUTING_RISK_ALWAYS_ASKS", True),
            log_level=_env("AIOPS_GATEWAY_LOG_LEVEL", DEFAULT_LOG_LEVEL),
        )

    def validate(self) -> None:
        # 部署身份在**启动时**校验（#879）：一个格式不对的环境标识如果活到请求路径上，
        # 它的表现是"某个租户的工作区有时找得到、有时找不到"，而不是"配置写错了"。
        #
        # 判据本身只有一处（`canonical_environment`），登录代理用的是同一份。
        # **必备性**不在这里判：`from_env()` 是读部署身份的那一处，缺值在那里就是启动
        # 失败；而一个直接构造出来的 settings 不是一次部署（测试就是这么构造的）。
        # 空值在这里只意味着"这个进程没有部署身份"，用到它的那个端点因此不启用。
        if self.environment:
            self.environment = canonical_environment(self.environment)
        if not 1 <= self.internal_token_expire_seconds <= 3600:
            raise ValueError("AIOPS_GATEWAY_INTERNAL_TOKEN_EXPIRE_SECONDS must be between 1 and 3600")
        if self.charging_status_base_url:
            canonical_gateway_url(self.charging_status_base_url)
            if not SAFE_PROFILE_NAME.fullmatch(self.execution_environment):
                raise ValueError("charging status requires a logical execution environment")
        # 委派入口要求业务接口与逻辑环境都已配置：这两项是领取时「再用记录里的身份
        # 做一次授权读取」的前提，缺一项就不是 fail closed 而是「配了一半就开门」。
        if self.agent_run_config_file and not (self.charging_status_base_url and self.execution_environment):
            raise ValueError("agent run delegation requires charging status settings")
        # 执行配置清单只在**已启用委派**时有意义：没有委派就没有 run 可固定身份。
        if self.execution_manifest_file and not self.agent_run_config_file:
            raise ValueError("execution manifest requires agent run delegation")
        if self.execution_manifest_file:
            # 启动时就完整校验一次：坏清单必须在起监听之前失败，而不是等第一次签发
            # 才变成运行期不可用（那就是「配一半就开门」）。
            from aiops_diagnostics.execution_manifest import (  # noqa: PLC0415 - 避免循环导入
                ExecutionManifestError,
            )
            from aiops_diagnostics.execution_manifest import (
                load as load_execution_manifest,
            )

            try:
                load_execution_manifest(self.execution_manifest_file)
            except ExecutionManifestError as exc:
                raise ValueError(f"execution manifest is invalid: {exc}") from exc
        if not self.bind_host or any(character.isspace() for character in self.bind_host):
            raise ValueError("AIOPS_GATEWAY_BIND is invalid")
        # 解析在这里做（结果丢弃）：级别名不对就该在起监听之前失败，而不是让服务起不来
        # 日志却声称"已经打开了"。取用是 `serve()` 的事 —— 这里只负责判它与拒不拒。
        parse_log_level(self.log_level)
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
        if not 1 <= self.kb_search_max_results <= 20:
            raise ValueError("AIOPS_GATEWAY_KB_SEARCH_MAX_RESULTS must be between 1 and 20")
        # #625：控制台凭据的两半 —— 半配置同样是启动错误。只给地址会让运维以为
        # 「发布动作配好了」，而拉取会在第一次运行时因缺凭据失败 —— 与「没配」不可区分。
        if self.dify_console_base_url and not self.dify_console_api_key:
            raise ValueError(
                "AIOPS_GATEWAY_DIFY_CONSOLE_API_KEY is required when a Dify console base URL is set"
            )
        if self.dify_console_api_key and not self.dify_console_base_url:
            raise ValueError(
                "AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL is required when a Dify console API key is set"
            )
        # 发布路由要用**服务端**的控制台凭据去拉 Dify。只给发布密钥而不给控制台凭据，
        # 会得到一个"门开着、但每次调用都 502"的部署 —— 那正是 #586 那条「半配置是启动
        # 错误」的同一种形状：看起来装好了，实际整体不可用。
        if self.dify_publish_api_key and not (self.dify_console_base_url and self.dify_console_api_key):
            raise ValueError(
                "AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL/API_KEY are required when the Dify publish "
                "route is enabled (the route pulls from that console)"
            )
        # #772 E：3A 工具说明端点只用一把钥匙，没有第二个半（它不调任何上游）。这里要判的
        # 是它**不能**与任何既有钥匙同值 —— 同值等于把五件不同的事绑成一个权限，而那把
        # 钥匙是暴露给 Dify 侧的。
        for other_name, other in (
            ("AIOPS_GATEWAY_DIFY_KNOWLEDGE_API_KEY", self.dify_knowledge_api_key),
            ("AIOPS_GATEWAY_DIFY_DEBUG_API_KEY", self.dify_debug_api_key),
            ("AIOPS_GATEWAY_DIFY_PUBLISH_API_KEY", self.dify_publish_api_key),
            ("AIOPS_GATEWAY_DIFY_CONSOLE_API_KEY", self.dify_console_api_key),
        ):
            if self.dify_tools_api_key and other and secrets.compare_digest(self.dify_tools_api_key, other):
                raise ValueError(
                    f"AIOPS_GATEWAY_DIFY_TOOLS_API_KEY must differ from {other_name} "
                    "(one key must not authorise both reading the tool catalog and another surface)"
                )
        # 三把钥匙必须互不相同。同值等于把两件事绑成一个权限 —— #586 对调试凭据已经立过
        # 这条，这里的每一对都有各自的理由（见字段注释）。
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
        # #781：试运行的两半 —— 凭据与固定调试租户 —— 必须成对。只给凭据会让运维以为
        # 「试运行建好了」，而它连该在哪个租户上跑都不知道；那会退化成"按请求给的租户跑"，
        # 正是 #782 才要接管的按人范围。交给启动失败。
        if self.dify_trial_api_key and not self.dify_trial_debug_tenant:
            raise ValueError(
                "AIOPS_GATEWAY_DIFY_TRIAL_DEBUG_TENANT is required when the Dify trial route is enabled"
            )
        if self.dify_trial_debug_tenant and not self.dify_trial_api_key:
            raise ValueError(
                "AIOPS_GATEWAY_DIFY_TRIAL_API_KEY is required when a Dify trial debug tenant is set"
            )
        # 试运行触发一次**真实模型执行**，所以它的钥匙必须与其余每一把都不同 ——
        # 同值等于把"能读知识库 / 能发布 / 能列目录"悄悄绑成"能在真数据上跑一次诊断"。
        for other_name, other in (
            ("AIOPS_GATEWAY_DIFY_KNOWLEDGE_API_KEY", self.dify_knowledge_api_key),
            ("AIOPS_GATEWAY_DIFY_DEBUG_API_KEY", self.dify_debug_api_key),
            ("AIOPS_GATEWAY_DIFY_PUBLISH_API_KEY", self.dify_publish_api_key),
            ("AIOPS_GATEWAY_DIFY_TOOLS_API_KEY", self.dify_tools_api_key),
            ("AIOPS_GATEWAY_DIFY_CONSOLE_API_KEY", self.dify_console_api_key),
        ):
            if self.dify_trial_api_key and other and secrets.compare_digest(self.dify_trial_api_key, other):
                raise ValueError(
                    f"AIOPS_GATEWAY_DIFY_TRIAL_API_KEY must differ from {other_name} "
                    "(one key must not authorise both running a trial and another Dify surface)"
                )
        # 登记表在这里解析一次：解析失败是**启动错误**，而不是第一个请求到达时才发现 ——
        # 后者会把一个配置错误伪装成一次运行时故障。
        parse_dify_knowledge_bindings(self.dify_knowledge_bindings)
        parse_dify_tool_bindings(self.dify_tool_bindings)
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
    #: The operator's own label, when the row came from the console (ADR 0048).
    #: The env registry has no such field — there the id doubles as the label —
    #: so this defaults to empty instead of growing the env syntax a fourth
    #: segment.
    display_name: str = ""


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


def parse_dify_tool_bindings(value: str) -> dict[str, str]:
    """解析 ``dify_provider/tool:our_tool`` 的多行/分号登记表。

    与知识库那张表**同一条纪律**：非法行抛错而不是跳过。键是 Dify 的工具身份
    （``provider_id/tool_name``，实测形状见 ADR-0019），值必须是合法的 ``ToolName``
    —— 后半句在发布门里兜底，这里只做结构校验（``gateway_config`` 不 import
    ``agent_contracts``，那条依赖方向要保住）。
    """
    from aiops_diagnostics.agent_contracts import ALL_TOOL_NAMES

    bindings: dict[str, str] = {}
    for row in value.replace(";", "\n").splitlines():
        row = row.strip()
        if not row or row.startswith("#"):
            continue
        dify_id, sep, our_tool = row.partition(":")
        dify_id, our_tool = dify_id.strip(), our_tool.strip()
        if not sep or not dify_id or not our_tool:
            raise ValueError(f"dify tool binding is malformed: {row!r}")
        if our_tool not in ALL_TOOL_NAMES:
            raise ValueError(
                f"dify tool binding maps to an unknown tool: {our_tool!r} "
                f"(known: {', '.join(ALL_TOOL_NAMES)})"
            )
        if dify_id in bindings:
            raise ValueError(f"dify tool binding is duplicated: {dify_id!r}")
        bindings[dify_id] = our_tool
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


def _int_value(values: dict[str, str | None], name: str, default: int) -> int:
    """An int setting that may come from the process env or the private config.

    Same two-source rule as ``_file_value``, but parsed: the env wins when set,
    the private file is the fallback, and a value that is not an integer fails
    loudly at construction rather than being dropped back to the default —
    "the depth was raised" must not be a statement the config cannot support.
    """
    raw = _env(name) or _file_value(values, name)
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
