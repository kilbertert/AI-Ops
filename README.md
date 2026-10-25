# AI-Ops

面向充电订单故障的**只读诊断系统**：把一句自然语言反馈变成一份有证据、可追溯、
不改变任何生产数据的诊断报告。

```text
订单 2079842220423700481 金额不对，帮我排查
```

> 第一次使用（安装 → 跑出第一份离线报告 → 连生产库 → 多端 Gateway）：
> 看 [快速上手](docs/快速上手.md)。
> 本 README 是**给接手这个仓库的人**看的：它解释系统为什么长这样、模块怎么分层、
> 依赖往哪个方向流、改一处代码要动哪些文件。

---

## 目录

- [一、这个仓库到底在做什么](#一这个仓库到底在做什么)
- [二、设计哲学](#二设计哲学)
- [三、系统模块与依赖关系](#三系统模块与依赖关系)
- [四、模块之间的契约（接缝）](#四模块之间的契约接缝)
- [五、运行入口](#五运行入口)
- [六、仓库地图](#六仓库地图)
- [七、改动指南：动哪里、读哪些文件](#七改动指南动哪里读哪些文件)
- [八、验证与交付纪律](#八验证与交付纪律)
- [九、当前边界与未验收事项](#九当前边界与未验收事项)
- [十、文档路由与历史遗留文件](#十文档路由与历史遗留文件)

---

## 一、这个仓库到底在做什么

仓库里有**两个互不相同的系统**，读之前必须分清，否则会把一半的文件当成无关噪音：

| 面 | 是什么 | 主要目录 |
|---|---|---|
| **产品面** | 只读诊断运行时 + 客服问答/知识库/健康报告的多端服务 | `src/aiops_diagnostics/`、`java/`、`ops/`、`packaging/` |
| **治理面** | 让 AI agent 自动完成「issue → 实现 → 审查 → PR」的可信交付流水线（AFK） | `.github/workflows/`、`.sandcastle/` |

两面共享同一套安全底线（凭据隔离、默认分支保护、确定性 CI），但**技术上没有调用关系**：
治理面产出的 PR 改的是产品面代码。本文档以产品面为主，治理面见
[第八节](#八验证与交付纪律) 与 [docs/afk-workflow.md](docs/afk-workflow.md)。

产品面的对外能力（Gateway 共 66 条路由）分四组：

- **诊断**：设备运行路径 `/v1/runs`（workspace 设备令牌）、标准 API
  `/v1/standard/diagnoses`（调用者身份委托）。
- **客服问答**：统一助手入口 `/v1/assistant/questions`（分流到固定问答 / 通用问答 /
  订单诊断 / 宣传案例）、固定问答 `/v1/faq/*`、知识库检索与媒体 `/v1/media/*`。
- **充电健康报告**：`/v1/health-report-jobs`（确定性规则实时计算，非故障根因判断）。
- **平台管理**：智能体与快捷动作的草稿/发布/停用生命周期、运行指标 `/v1/agent-metrics`。
- **运营面**：产品快捷动作 `/v1/shortcuts`（按钮）与聊天页运营横幅 `/v1/banner`（带图卡片）。
  两者同表同生命周期，只是**展示面**不同；横幅是跳转动作，个性化车图由客户端自己合成 ——
  AI-Ops 只给静态配置，不读业务表。

---

## 二、设计哲学

这一节是本文档的核心。**下文每一个模块的存在理由，都可以回溯到这里的某一条推论**；
如果你要判断一处改动是否违背设计意图，也回到这里判断。

### 起点：三个不可协商的事实

1. **被诊断的系统承载资金与设备控制。** 充电订单关联计费、退款与结算；充电枪
   关联正在进行的物理过程。任何一次误写都不是数据问题，是钱和现场问题。
2. **业务方要的是解释，不是又一个能改数据的后台。** 故障的根因判断需要跨越订单、
   费用规则、设备时序、通讯报文和消息队列五类证据，这是人做起来很慢、模型做得还行的事。
3. **模型有推理能力，但没有可靠边界。** 它可能 hallucinate、可能凭空造出一张表名，
   可能在证据不足时给出高置信度结论，可能被提示词注入。

### 推论一：系统的正确形态是「只读的证据收集 + 解释」

事实 1 与 2 直接推出只读边界，并且这个边界必须写成**明文禁令**而不是"暂时没做"：

- 不提供 `UPDATE` / `DELETE` / `INSERT` / DDL、退款、重算、补发、消息重放、服务重启。
- 未来如果确实需要动作能力，**必须独立设计审批、权限与审计账本**，
  绝不能在诊断工具里顺手加一个写接口。
  见 [ADR-0001](docs/adr/0001-read-only-diagnostic-boundary.md)。

「只读」还不够严格——读也能压垮生产库。所以只读继续推出**有界**：

- 每次查询有超时、行数上限、时间窗口上限（`SafetySettings`，`config.py:309`）。
- TDengine 查询必须带设备、时间范围、选定列与 `LIMIT`；TDengine Community Edition
  不支持数据库级只读授权，因此生产查询必须经过 `ops/` 里 loopback-only 的
  **严格只读代理**（只识别本项目发出的精确有界语句）。
- Redis 用最小权限 ACL 用户，只允许 `XLEN/XINFO/XREVRANGE/TYPE/PING/INFO`。

### 推论二：既然模型不可信，边界必须由 harness 保证，而不是由提示词保证

这是本项目最重要的一次架构反转，叫 **thin harness（细管道）**：

> **模型不是被"管着"的工具，它是决策主体；harness 不决定因果结论，
> 它只保证模型的每个动作合法、有界、限定到租户与订单、进入日志、完成脱敏、可以恢复。**
> 见 [docs/thin-harness.md](docs/thin-harness.md)。

传统做法是「代码算出答案，模型负责措辞」；本项目反过来——**Codex 选择要哪些证据、
如何组合证据、给出因果解释**，Python 侧只做它必须做的事。为什么？因为事实 3 里的
"推理能力"是真能力，而事实 2 要求的跨五类证据的因果判断恰好是它的强项；
用提示词去约束它则是无效的（提示词是请求，不是保证）。

推论二在代码里落成五道**由 harness 执行的**硬边界，全部在 `agent_engine.py` 的
turn 循环周围：

| 边界 | 载体 | 保证什么 |
|---|---|---|
| 身份不可漂移 | `IncidentManifest`（`agent_contracts.py:36`） | 订单号/租户/意图/来源哈希在运行中冻结，改不了 |
| 动作有界可枚举 | `ToolName` 枚举 + `DiagnosticToolExecutor` | 模型只能点名 7 个预定义工具，**不能提交 SQL、不能选表** |
| 上限与恢复 | `RunState` + `AgentWorkspace` | turn 数、工具调用数、校验重试数封顶；`thread_id` 落盘，可 `agent-resume` |
| 证据可追溯 | `EvidenceJournal`（`journal.py`） | 每个工具结果存不可变 artifact + SHA-256，事件日志只记元数据 |
| 输出必须过合同 | `AgentResultValidator`（`agent_validator.py`） | 拒绝身份漂移、缺证据引用、只引 runbook 的结论、来源失败后的高置信度、密钥泄露 |

模型吐出的结构化输出还要过一层**容错解析**（`agent_engine.py:40`）：不同 provider
（GLM 等）会包 markdown fence、会丢掉 `kind` 判别字段、会把内层对象拍平。
这些差异在这里被归一化，而不是让每种 provider 的分支渗进业务逻辑。

### 推论三：一条业务规则只能有一处定义

第二个起点是**漂移**。最有力的证据是 `order_visibility.py` 的模块文档：
「哪个订单可以被这次运行看见」这条规则**曾经被独立实现六次**——两个入口守卫、
agent 工具层、三个数据源——并且**已经漂移了**：同一个订单，因入口不同会得到不同的
可见性结论和不同的失败原因。

因此该规则被收敛到一处定义、三种渲染（`order_visibility.py`）：

- `resolve_device_tenant()` — 入口渲染：运行的有效租户在此决定一次，请求不能放宽它。
- `visible_orders()` — 行级渲染：给手里已经握有行的调用方（fixture、工具层）。
- `scope_where_sql()` — 参数绑定 SQL 渲染：给能把规则下推进 SQL 的调用方。

**为什么必须是三种渲染而不是一种？** 因为设备路径从 HTTP 读订单（没有 SQL 可下推），
调用者路径直连数据库（有 SQL 可下推）。收敛点在**规则**，不在执行点。

这条推论还有一条更弱的通用形式：`rules.py` 与 `order_visibility.py` 被刻意写成
**只含纯函数与冻结值**，不含类层次、不含运行时状态、不引第三方依赖。规则是可测的
数据，不是对象。

### 推论四：失败是常态，所以运行必须可恢复

模型会因为额度欠费、超时、格式错误、进程中断而失败。把"运行"做成一个不可恢复的
内存对象，等于把外部不稳定变成内部数据丢失。所以：

- 每次运行有一个私有目录（`AgentWorkspace`，POSIX `0700`），`state.json` 记录
  phase/turn 计数/下一个 prompt；`events.jsonl` 事件**先落盘再推送**。
- **事件 sink 与人类进度显示是同一个源**——终端上看到的阶段，事后就能在
  `events.jsonl` 里追溯到，不需要两套记录互相对账。
- 恢复时校验 provider endpoint 一致（拒绝把已存 key 重定向到别的上游）、
  校验 fixture 哈希（恢复不能悄悄读到变过的测试数据）。

### 推论五：凭据不能到达客户端，所以有 Gateway

便携客户端不应该携带 MySQL、Redis、SSH 或 provider key。
把凭据留在固定服务器、客户端只拿一个设备令牌——见
[docs/gateway.md](docs/gateway.md)，以及 [ADR-0004](docs/adr/0004-bff-owns-frontend-api-boundary.md)。

同一原理在**用户身份**上重复一次：设备令牌不能代表用户，裸 `user_id`/`tenant_id`
不能作为授权依据。所以小程序侧由业务后端验证会话后签发一次性身份委托句柄，
AI-Ops 用独立服务身份回查——见 [ADR-0003](docs/adr/0003-bff-delegated-user-identity.md)。

> ⚠️ **上句描述的是目标形态，不是现状。** 实际落地是共享会话 Redis 直读，
> 偏离记录见 [ADR-0008](docs/adr/0008-adr-0003-deviation-session-direct-read.md)；
> 偏离的**根因**是站点位置（AI-Ops 不在 `cloud-gateway` 之后），
> 现状、证据与目标态见
> [docs/agents/company-platform-integration-baseline.md](docs/agents/company-platform-integration-baseline.md)。

### 推论六：成本与确定性要求保留一条不调用模型的路径

模型很贵、不稳定、不可复现，但 CI 需要可复现的回归基准。因此 `diagnose` 保留
`--mode deterministic` 分支（`engine.py`），它由已知规则与离线 fixture 驱动，
不需要 provider key，是 CI 与 fixture 回归的稳定锚点。

---

## 三、系统模块与依赖关系

### 3.1 分层

**先说结论，再说例外。** 本包实测**无环**（全量 AST 导入分析，含函数内延迟导入），
可以按下面的层理解；但它**不是严格的单向栈**——有 **6 条真实的向上依赖**，
全部列在表里。README 不会假装它们是"架构违规"：它们是低层模块为了服务某个窄场景
而引用高层的既有接缝，每条都有具体理由。

```mermaid
flowchart TB
    L6["L6 服务边界与入口<br/>gateway_api · gateway_runtime · cli · gateway_server · gateway_cli<br/>gateway_client · admin_cli · gateway_config · gateway_tokens · gateway_logging<br/>request_observation · chat_execution · chat_data_grant · dify_chat_workflow · dify_run_store · tdengine_proxy · responses_adapter · codex_launcher"]
    L5["L5 持久化<br/>gateway_store · conversation_store · metrics_store<br/>agent_lifecycle · shortcut_lifecycle · dify_app_registry · dify_workspaces"]
    L4["L4 推理与产品能力<br/>agent_engine · codex_runtime · agent_runner · agent_validator · qa_rag<br/>turn_recovery · routing · faq · promo_agents · answer_language · agent_manifest · agent_debug<br/>dify_dsl_pull · dify_tools_catalog · dify_trial · dify_login_proxy · dify_login_company · dify_login_provision · dify_login_service · dify_login_server · dify_knowledge_api · dify_debug_identity · dify_ensure_workspace · dify_inner_api · kb_onboarding · knowledge_sources · knowledge_triple · dify_knowledge_migration · dify_console_admin · dify_tenant_access · ragflow_console · dify_workspace_directory · agent_content · clarification"]
    L3["L3 证据与诊断内核<br/>sources · diagnostic_tools · engine · journal<br/>agent_workspace · health_report · knowledge_retrieval"]
    L2["L2 身份与范围<br/>scope_context · query_scope · caller_auth · third_session_auth<br/>company_token_auth"]
    L1["L1 领域规则（纯函数）<br/>rules · order_visibility · health_metrics · health_curves · parsing"]
    L0["L0 基础设施与契约<br/>agent_contracts · models · config · bounded_http · redaction<br/>i18n · private_files · platform_paths · http_auth · render · console_encoding<br/>jev_decisions · deployment_environment"]

    L6 --> L5
    L6 --> L4
    L6 --> L3
    L6 --> L2
    L6 --> L1
    L6 --> L0
    L4 --> L3
    L4 --> L2
    L4 --> L1
    L4 --> L0
    L3 --> L2
    L3 --> L1
    L3 --> L0
    L2 --> L0
    L1 --> L0
```

**层内为主，向下为主。** 同层内的引用（如 `agent_runner → agent_engine`、
`diagnostic_tools → sources`）是常态，不算异常。

#### 6 条向上依赖（真实存在，逐条有理由）

| 引用方（层） | 被引用（层） | 理由 |
|---|---|---|
| `caller_auth`（L2） | `sources`（L3） | `ScopedOrderAuthorizer` 需要**对着真实数据**判断调用者能否访问某订单，因此调用 `scoped_live_sources()`。授权判定本身是需要证据的。 |
| `diagnostic_tools`（L3） | `engine`（L3） | 同层，但值得记下：这里有一个**真实的封装缺陷**——它导入的是 `engine._order_window`，一个下划线私有名。复用是对的（时间窗口规则的唯一定义），但接口没有正式化。 |
| `journal`（L3） | `agent_workspace`（L3） | 同层协作：证据日志必须落到运行目录上，两者是同一层的两个面。 |
| `qa_rag`（L4） | `agent_lifecycle`、`shortcut_lifecycle`（L5） | 运行客服 QA 需要读"该租户当前已发布的智能体版本"——这是持久化层的职责。 |
| `agent_manifest`、`agent_debug`（L4） | `agent_lifecycle`（L5） | 声明式收敛与草稿调试都直接操作已发布版本存储。 |
| `shortcut_migration`（L4） | `shortcut_lifecycle`（L5） | 迁移的目标就是快捷动作存储，方向必然朝上。 |

**两条横向依赖（与推论三一致，不要"修正"）：**

1. **L3 证据层依赖 L2 身份层**（`sources.py`、`diagnostic_tools.py` 导入 `query_scope`）。
   因为范围必须作为参数绑定的谓词**下推进 SQL**，而不是取出数据再过滤。
2. **L2 身份层依赖 L1 规则层**（`scope_context.py` → `order_visibility`）。
   因为租户可见性是业务规则，不是技术细节。

**为什么 `agent_contracts.py` 在 L0 而 `agent_engine.py` 在 L4？**
契约被所有人导入（入度 12），编排只被 runner 导入。契约是共享词汇，编排是流程。

首次只启用 Dify 运维只读工具时，CPMS 地址使用
`AIOPS_GATEWAY_OPS_CHARGING_STATUS_BASE_URL`，不会同时打开 consumer 充电状态入口；
接入及兼容规则见[首片手册](docs/agents/dify-ops-first-slice/README.md)。
客户端聊天另有默认关闭的[本人状态查询](docs/agents/consumer-order-chat/README.md)：
支持指定订单与本人最新一单，复用现有 App 请求/回答格式；不使用运维服务身份，不调用模型。
自 APP-RUNTIME-01 起，该入口可选择交给**客户执行节点**读：配置项是
`AIOPS_GATEWAY_CHAT_EXECUTION_BINDINGS_FILE`（**按租户登记**的 0600 登记表，默认空 = 关闭）。
未登记租户走原直读、一次派发都不发生；命中后任何失败都落 `failed`，不回落直读/旧模型/别的节点。
登记格式、判据与旧双键变量的迁移见 [Gateway 契约 · App 聊天交给客户执行节点
（APP-RUNTIME-01）](docs/gateway.md#app-聊天交给客户执行节点app-runtime-01)。
2026-10-22 operator 两工具已常驻启用，另次授权真实预览的模型段通过；
公司 App 与业务准确率未验收。[离线证据复核](docs/agents/dify-ops-first-slice/model-acceptance.md)
不追加模型或业务请求。

**为什么 `engine.py` 和 `agent_workspace.py` 在 L3？**
判据是**入度**而非直觉：`engine` 被 `diagnostic_tools` 与 `cli` 引用（作为 `known_runbook`
参考证据），`agent_workspace` 被 `journal` 与 `codex_runtime` 引用。把它们放低一层会让
向上依赖从 6 条增加到 8 条。分层是为理解服务的，不是为整洁服务的。

### 3.2 各层模块职责

#### L0 基础设施与契约

| 模块 | 职责 |
|---|---|
| `agent_contracts.py` | 推理面全部 Pydantic 契约：`IncidentManifest`、`AgentTurn`、`AgentDiagnosis`、`RunState`，以及 strict JSON-schema 生成 |
| `models.py` | 确定性面数据类：`DiagnosticRequest`、`DiagnosticReport`、`Evidence`、`Intent`、`Severity` |
| `config.py` | 全部配置的单一来源：`Settings` 及嵌套 `*Settings`，含 `SafetySettings`（上限）与 `AgentSettings`（provider 注册表） |
| `bounded_http.py` | 所有出站 HTTP 的**唯一**骨架：请求构造 → 认证头 → `urlopen` → 异常分类 → 信封解析。各调用点以声明式参数注入自己的错误类、认证方式与重试策略，差异显式可见 |
| `jev_decisions.py` | Jev 决策客户端（PRD #383）：发送类型化问题（`choice`/`noul`/`score`），取回**带概率的类型化判定**而非生成文本——判定路径因此不再需要先让模型生成 JSON 再解析。**必须显式设置 User-Agent**：网关 WAF 会以 403 拒绝 urllib 的默认 agent，形态酷似限流或认证失败 |
| `redaction.py` | `redact_text` / `sanitize_data` / `contains_secret` |
| `i18n.py` | `Accept-Language` 解析、语言表、`chinese_leak` 判定。**纯头部解析**，结果只用于呈现，不得改变权限或路由 |
| `private_files.py` | 私有目录/文件创建与权限加固（POSIX `0700`/`0600`，Windows 当前用户 DACL） |
| `isolation_guard.py` | **试运行实例的隔离门禁**：可写状态、只读业务依赖、运行 UID 与属主必须在声明的隔离根内，且在 `create_gateway_app()` 打开任何库**之前**判定。未设置 `M2LIVE_ISOLATION_ROOT` 时是空操作（生产启动路径逐字不变）；`python -m aiops_diagnostics.isolation_guard` 是 preflight 用的同一份判据 |
| `platform_paths.py` | 平台私有路径解析：`config_root`、`data_root`、`reference_root`（开发态指向仓库、冻结态指向 `_bundle`） |
| `http_auth.py` | HMAC-SHA256 `X-Internal-Token` 签发与校验，与 Java 侧 `InternalTokenManager` 兼容 |
| `console_encoding.py` / `render.py` | Windows stdio UTF-8 兜底 / Rich 渲染 |

#### L1 领域规则（纯函数，无状态、无依赖）

| 模块 | 职责 |
|---|---|
| `order_status.py` | 订单状态码 → **业务角色**的唯一落点（`role_of` / `is_abnormal_end` / `is_normal_end` / `is_charging`）。它回答「是什么角色」（判定用），`rules.ORDER_STATUS` 回答「叫什么」（给人读）；同一份角色不再各处拼写（PRD #732） |
| `rules.py` | 停止原因分类、服务端/桩端计费判定、状态标签 |
| `order_visibility.py` | **租户可见性规则的唯一定义**，三种渲染（见推论三） |
| `health_metrics.py` / `health_curves.py` | 充电健康指标计算与曲线降采样 |
| `health_report_copy.py` | 健康报告文案（摘要 / 曲线名 / 停因，六语）|
| `parsing.py` | `parse_request`：自然语言 → 结构化请求。**只提取订单号与意图**，不接受 SQL、不选表 |

#### L2 身份与范围

| 模块 | 职责 |
|---|---|
| `scope_context.py` | 把一次运行的授权输入收敛为不可变 `ScopeContext`：调用者、目标主体、有效租户、业务数据范围。平台依赖经 `PlatformDirectory` 单一接缝接入。**解析器不持有任何数据源**，全部 fail closed |
| `query_scope.py` | 把 `ScopeContext` 解析成可直接下推的不可变 `QueryScope`（tenant / site_ids / user_id），站点范围来自 UPMS 数据范围、Dis 点位归属，或运营商维度（B 端主体 → 店铺集合 → 站点归属，PRD #423） |
| `caller_auth.py` | 调用者身份接缝：`CallerContextResolver`（UPMS / OAuth2 introspection / fail-closed 禁用 / **来源密钥分派**）与 `OrderAuthorizer`。来源密钥（`X-AIOps-Source-Key`，由可信那一跳注入）决定走既有链还是公司令牌链；未配置即不启用（#444，ADR-0009 D8–D9） |
| `third_session_auth.py` | C 端 `thirdSession` → `ScopeContext` 的 Redis 解析路径；身份同时带 C 端 id 与经既有 C→B 映射端点补全的 B 端 `sys_user.id`，解析不出唯一主体时记录可区分原因 |
| `company_token_auth.py` | 管家端的**公司 OAuth2 令牌** → `ScopeContext`。两条互斥通道：**远端** `check_token`（默认关闭）或**本地 HS256**（`AIOPS_GATEWAY_COMPANY_JWT_KEY`）。本地通道的判据**默认与公司一致**（`trust_company_payload=True`，生产默认 = **不验签、不判 `exp`**，见 ADR-0009 与 #460 的用户裁定；设 `AIOPS_GATEWAY_COMPANY_TRUST_PAYLOAD=0` 切回验签 + 判 `exp` 的更严模式）。身份与 `shop_ids` 取自令牌，站点范围经 `operator_site_scope_from_shops` 共用同一份规则。缺字段/形状不符/上游不可用一律 fail closed（#443，ADR-0009） |
| `company_permissions.py` | 公司**权限码 → 内部角色**（`aiops:agent:edit` → `ROLE_AGENT_ADMIN`，`aiops:agent:publish` → `ROLE_AGENT_PUBLISHER`）。存在的理由：真实身份路径上 `roles` 原本恒为空集，于是知识库四条路由与 `/v1/agents` 对任何真实运营都是 403，而**构造身份**（直接给角色）能过 —— 单测照抄了那个角色所以全绿（2026-10-22 真浏览器验收暴露）。按**令牌**缓存、读不到即空角色集（fail closed） |

#### L3 证据与诊断内核

| 模块 | 职责 |
|---|---|
| `sources.py` | `DiagnosticSources` 协议 + 全部实现（见 [4.1](#41-证据源接缝)）。**最大单文件（5026 行）** |
| `diagnostic_tools.py` | `DiagnosticToolExecutor`：模型可点名的 7 个工具在此落地；**工具白名单在这里强制**（`allowed_tools`，白名单外记 `blocked` 且不取数，见 [ADR-0019](docs/adr/0019-tool-allowlist-enforced-in-executor.md)）；含 `preflight_environment` 环境预检（advisory） |
| `engine.py` | `DiagnosticEngine`：确定性规则诊断（`--mode deterministic`）。**同时被 `diagnostic_tools` 复用来提供 `known_runbook` 参考证据** |
| `journal.py` | `EvidenceJournal`：追加式证据日志，每个结果存 artifact + SHA-256 |
| `agent_workspace.py` | 运行目录的磁盘契约：manifest/state/result/events，run_id 创建 |
| `health_report.py` | 最小健康报告装配，带更窄的 `OrderSource` 协议 |
| `charging_status.py` | 本人订单**充电状态快照**的读取（`GET /v1/orders/{order_no}/charging-status` 的下游一半）：经 `bounded_http` 打一笔按订单号钉住的 CPMS `chargingInfo` 接口，**禁止重定向**（不把身份头带去别处）、断言响应里的订单号就是被授权的那一笔；状态码**不重新定义**，交 `order_status.py` 定角色。调用方必须先完成身份与订单授权 —— 本模块不回答「这单是不是你的」 |
| `agent_runs.py` | **委托执行的授权记录与派发**：一次已授权的读取可以交给可信执行节点跑。记录留在 Gateway（`issued→running→succeeded`，另有 `failed`/`revoked`/`expired`，固定 180 秒不续期）；执行节点只拿到一个高熵 `run_id`，**知道它本身不授予读取权限**，回呼时必须用自己的独立凭据认身。节点凭据只以 SHA-256 存进绑定表；`claim` 是一条带条件的 UPDATE，所以并发领取只有一个赢家、成功后重试返回同一份快照。放行前用冻结身份**再授权一次** —— 授权变了连已算好的结果也不释放。`SnailDispatcher` 是发往编排平台的那一跳（只传 agentId、`sid`、一句固定提问）。见 [ADR-0033](docs/adr/0033-consumer-charging-status.md) |
| `execution_manifest.py` | **执行配置清单的身份读取**：受控文件是「一次委派应当跑在什么配置上」的唯一期望来源，身份＝**清单原始字节的 sha256**（不做 JSON 归一化，两端可直接比）。未配置时是空操作，行为与今天逐字一致；配了就必须读得到、是普通文件、非符号链接、权限不宽于 0600，否则**启动即拒** —— 不降级、不留「配一半就开门」。清单里没有凭据，只有期望。见 [ADR-0051](docs/adr/0051-execution-config-is-verified-per-surface.md) |
| `knowledge_retrieval.py` | 有界知识检索与媒体签发：`KbServiceClient`、`KnowledgeSearchGuard`、`MediaResourceSigner`、`MediaProxy` |

#### L4 推理与产品能力

| 模块 | 职责 |
|---|---|
| `agent_engine.py` | **`AgentCoordinator`：整个 agent 路径的心脏。** turn 循环、跨 provider 容错解析、工具分发、合同修复循环、完成/阻断终态 |
| `codex_runtime.py` | `CodexSession` 协议 + `SDKCodexSession`（openai-codex SDK）、隔离 runtime home、provider key 解析、心跳 |
| `agent_validator.py` | `AgentResultValidator`：输出侧合同校验 |
| `agent_runner.py` | CLI 与 Gateway 共用的入口：`run_agent_diagnosis` / `run_zero_order_answer` / `classify_lightweight` |
| `qa_rag.py` | 客服 QA 的 RAG 运行：在 Codex harness 里跑已发布智能体 + `knowledge_search`，产出 blocks-v1 合同 |
| `consumer_order_chat.py` | 本人订单状态查询的保守措辞匹配与 blocks 回答；范围、CPMS 读取及证据放行由 Gateway 统一处理，不调用模型 |
| `turn_recovery.py` | 传输层模型轮次的宽容解析：围栏 / 最外层 `{}` / **首部丢失修复**（providers 的 SSE 首个 delta 会被上游丢弃，2026-09-22 实测 5/6） |
| `routing.py` | 路由判定（PRD #383）：把 Jev 的**类型化判定**翻成 `intent`/`risk`/`confidence`，六个 `intent` 字符串与旧模型逐字相同。取不到判定时**返回 None 而非失败**（路由只是优化），失败带错误码、可计数、日志可见（该组件曾静默失效过一次）|
| `faq.py` | 平台隔离的固定问答目录与平台身份判定（`consumer` / `operator`） |
| `promo_agents.py` | 宣传案例/方案路由：跑租户已发布的宣传智能体，与客服 FAQ 智能体隔离 |
| `ops_agents.py` | 管家端诊断的**运维配方**选择（#695 / PRD #693）：按运行时登记表点名的 agent 名取那份**已发布**的 `operations` 版本，把它的提示词作为「运维方法论」注入诊断；**只做查找、不做授权**，查不到即 `None`（调用方 fail-closed，不回退烤死提示词） |
| `ops_analysis.py` | 运维分析面的确定性装配（#697 / PRD #693）：把「站点集合 + 时间窗」的一次聚合与模型写的两段散文装成第三份对外契约（`failure_rate / main_cause / secondary / risk / recommendation`）。数字全来自聚合、模型只写叙述（按名字读两个键，没有路径让模型写的数字进入数字字段）；空范围/窗外无单 ⇒ `unavailable` 而不是 0；本面是输出语言守卫的**第 4 个模块**（第 5 个面）。规则版本 `ops-analysis-v1`。 |
| `ops_analysis_narrative.py` | 运维分析的**模型轮**（#697）：输入是已经算好的数字，输出只有 `main_cause` / `recommendation` 两段散文。结构化输出里**没有数字字段**——这是「模型写不出数字」的第一道墙（第二道在装配处按名字读两个键）。跑在零阶工作区上（那里没有订单数据可读），对话轮的容错解析与问答/诊断共用一份。 |
| `answer_language.py` | 回答面输出语言校验的**共享应用点**（挂到各回答面的定稿点） |
| `agent_manifest.py` / `agent_debug.py` | 声明式环境清单收敛 / 草稿调试与真实知识绑定校验 |
| `dify_knowledge_api.py` | 面向 Dify External Knowledge API 的知识检索适配（#581 / PRD #577）：请求/响应形状与 `records` 渲染。租户与知识库白名单由我们的登记表定，**不来自请求** |
| `ops_bindings.py` / `ops_tools.py` / `ops_provider.py` | 默认关闭的 Dify 运维服务工具：私有凭据绑定、有界订单列表、当前状态、发布撤权及自动登记。独立租户服务权限不替代个人范围；[接入与验收](docs/agents/dify-ops-first-slice/README.md) |
| `agent_content.py` | 开场白与预设问题的**版本化内容制品** + 按语言派生（#585 经 [ADR-0020](docs/adr/0020-dify-owns-the-zh-master-of-starters.md) **部分反转**）：`zh` 取运营在 Dify 写的母版、其余语言取这里手写的文案；**新增一门语言只改这里，不改 Dify 里的任何配置**。覆盖是判据（`starter_gaps()` 空元组才算完整） |
| `admin_cli.py` 的 `pull-dify` | 把运营在 Dify 里改好的配置拉回来、经**我方发布门**冻结成一个已发布版本（#625 / PRD #577）。发布走 `AgentManager.publish` —— 与 `/v1/agents`、`admin reconcile` **同一条**生产路径；控制台凭据来自服务端配置而非命令行参数；**不写运行时登记表**（那是 `[[dify_apps]]` + `reconcile` 的事，混在一起会让一次拉取顺手改掉线上路由）|
| `dify_debug_identity.py` | 面向 Dify 的**调试身份**（#586 / PRD #577）：**凭据决定身份**，请求里的任何字段都不参与判定。生产凭据 = 登记表全量；调试凭据 = 固定测试租户 + 显式列出的知识库子集。身份只**收窄**，从不扩张；子集为空即**什么都取不到**（不是『不收窄』）；越权与未登记同为 404 |
| `dify_dsl_pull.py` | Dify 侧配置的**拉取**面（#580 / PRD #577）：从控制台导出端点取已导出 DSL，映射为 `AgentConfig` 并以**草稿**落地（经 `AgentManager`，绝不发布）。只消费 `pre_prompt` / `model.name` / `dataset_configs` 数据集 id；`opening_statement` 与 `suggested_questions` **不消费**（其权威来源是按语言版本化的内容制品，见姊妹票 #585）。拉取失败（不可达 / 凭据被拒 / DSL 版本不符 / 字段不可映射）各自一个错误码，且**不留下半成品草稿** |
| `dify_login_server.py` / `dify_login_service.py` / `dify_login_company.py` / `dify_login_provision.py` / `dify_login_proxy.py` | **Dify 登录代理**（#778 / PRD #772 B，ADR-0023）：公司账号 → 公司登录交换 → 权限码 → Dify 会话。`dify_login_server` 是回环上的 HTTP 面（登录页/验证码透传/登出，TLS 未就绪即拒绝启动）；`dify_login_service` 是流程 + 映射表；`dify_login_company` 打公司 UPMS（登录体是**表单**、密码要 **AES**、失败是 200+`code:-1000`）；`dify_login_provision` 走官方邀请→激活在 Dify 开户为 **editor**（激活 token 从邀请响应取，**不经过邮件**）；`dify_login_proxy` 自签控制台会话与 AES 编码。**映射表里没有密码** |
| `dify_trial.py` | **试运行**（#781 / PRD #772 F）：拿一份**草稿**（方法论提示词 + 工具白名单）在一张订单上跑一次真实 harness。它**不是第二个执行器** —— 走 ``run_agent_diagnosis``（CLI 那一个入口），只换 methodology 与 allowed_tools，其余（执行器白名单、证据日志、预算、校验器、语言守卫）全部继承。草稿**只能收窄**工具集合；请求体里的租户/站点/用户字段一律拒绝而非忽略。不产生智能体版本、不写注册表、不改生产路由 |
| `dify_tools_catalog.py` | **七工具目录**（#774 / PRD #772 E）：一份 OpenAPI 3.0 文档（`ops/dify-tools/aiops-tools.openapi.json`，运营粘贴进 Dify 的 API 工具提供方）+ 把运营的勾选翻译成生产工具白名单的规则。规则只认两种身份：`aiops` 提供方下的七个 `operationId`（**按提供方 UUID 匹配**，UUID 每次拉取当场从控制台解析，绝不落库/落配置），或其他提供方经 `AIOPS_GATEWAY_DIFY_TOOL_BINDINGS` 显式登记。`enabled=false` 的条目与「删除」同义。目录端点只回一段固定说明、不读任何数据源 |
| `dify_console_publish.py` | Dify 控制台里那颗「发布到 AI-Ops」按钮的**后端桥**（#577 的最后一跳）：从页面 URL 取 app_id → 拿**运营自己的 Dify 会话**问 Dify「你是谁、看得见这个 app 吗」→ 再用**服务端**的发布密钥调 `/v1/operator/publish`。密钥**不进浏览器**；app_id **不由请求体给**（伪造不了发哪个）；每一个失败都收敛成同一句话，不透内部错误码。它不引入新权限 —— 能登控制台改提示词的人本来就能改这个智能体的行为 |
| `shortcut_migration.py` | 租户复制快捷动作 → 平台默认的幂等迁移 |
| `clarification.py` | 澄清类答复的**判别位**（#636）：每种成因一个稳定、非本地化的 `CLARIFY_*` 码，同一条码在**响应体、指标行、结构日志**三处是同一个串。此前六个分支共用一句本地化 `message` 与一个不分细分的 `route_type='clarification'` 指标行，排查只能靠把每条分支跑一遍量字节数（且不成立）。码决定 `missing_fields` 与文案键，所以答复不可能自相矛盾；未登记的码是错误而不是默认值 |

#### L5 持久化（同一个 SQLite 文件，各自建表）

| 模块 | 职责 |
|---|---|
| `gateway_store.py` | 设备/注册码/run/事件/提问/健康报告作业/标准诊断 |
| `async_job_lifecycle.py` | 异步作业生命周期的唯一定义：状态集/截止时间/保留期分层/claim-guard/进程死亡的三种成因（四张表按命名 profile 声明各自差异） |
| `conversation_store.py` | 会话与活跃订单上下文（最近 8 轮或 8k token，保留 30 天） |
| `conversation_context.py` | 把上面的窗口渲染成模型的输入：确定性、已脱敏、按本轮语言标注而非翻译；无历史时是空串 |
| `metrics_store.py` | 脱敏运行指标。**按构造脱敏**：只存租户/路由/计数/延迟，不存问题与答案正文 |
| `agent_lifecycle.py` | 智能体草稿与不可变已发布版本（租户隔离） |
| `shortcut_lifecycle.py` | 产品快捷动作草稿与不可变版本（平台默认 + 租户覆盖） |
| `dify_app_registry.py` | 运行时注册表（#584 / PRD #577）：`(租户, 业务入口) → Dify app` 的显式映射，同一 SQLite 文件一张表。Dify **没有租户概念**，租户判定只能在我们的运行时完成，所以这张表是那条边界的落点。查表**只返回绑定或空**，绝不推断；一旦有任何一行，未登记的组合**什么都不选**（fail closed），而表为空时保持注册表出现之前的选法不变（可安全地以惰性状态上线）|
| `dify_workspaces.py` | `(租户, 环境) → Dify workspace` 的映射与**只查不建**的解析（#879 / ADR-0059）：`UNIQUE(tenant_id, environment)`，因为两套 UPMS 的 `tenant_id` 各自生成，撞号时只有环境列能分辨。解析失败是**四个可区分的码**（`UNRESOLVED` / `CREATING` 可重试 / `FAILED` 要重建 / `UNKNOWN` 先对账），**不是空串** —— 空串在 Dify 一侧的含义是"平台根"，那会把动作打到别人的工作区。`created` 与 `ready` 都返回 id，区别只在后续步骤是否跑完（#881）|
| `dify_workspace_directory.py` | 登录代理怎么知道"这个人该进哪个 workspace"（#883 / ADR-0072）：经网关的只读端点（内部令牌）解析，**不自己存第二份**；四个失败码原样保留（缺行 / creating 可重试 / failed / unknown）。`join_account` 把**已有账号**经 inner_api 的 member 端点加进去，失败**不阻断登录** |
| `dify_ensure_workspace.py` | 开户（#880 / ADR-0068）：**先占位、再建**，并让 ``creating`` 不是死胡同。`claim` 是唯一能串行化的动作（唯一键 + 先插入）；超时/读中断归 ``unknown``（先按派生名对账）而不是 ``failed``（可重建）——两者的后果相反，而 Dify **没有删 workspace 的 API**。对账命中 1 个补 UUID、0 个才重建、>1 个拒绝并要人工 |
| `kb_onboarding.py` | 开户（#924 / PRD #923）：把 `(Dify workspace, RAGFlow 租户, 模型实例)` 收进一个**幂等**命令。完成判据不是「行存在」而是「行存在且模型校验过」—— 半成品会被补齐；校验是**一次真实 embedding 调用**，因为配置读不出来一把被供应商禁用的密钥（41 上撞到过）。凭据密文入库、明文不落盘，轮换是独立命令且**先改上游再改我们这一侧** |
| `knowledge_sources.py` | 知识源解析（#925 / PRD #923）：`knowledge_id` → `(租户, 库)` **只在一处**算，检索与清单两条路由共用。三层优先级：人工登记（数据库）> env 登记 > 自动登记 `tenant-<tenant_id>`。自动登记只在租户**开户过**且**此刻有库**时生效，两者缺一都解析不到（404，与「未登记」同答，不是 200 空结果）；上游不可达时降级为「没有自动源」，人工登记照常工作。关掉 `AIOPS_GATEWAY_DIFY_KNOWLEDGE_AUTOREGISTER` 即回滚。**另有一种派生形态 `tenant-<tenant_id>:<dataset_id>`，恰好一个库**（#1000 / ADR-0095）：Dify 一条 external dataset 只带一个 id，租户级粒度在编排页上选不出单个库。库级 id 只在 dataset 出现在**该租户自己的**清单里时可服务，否则与「未登记」同答 404（租户 id 是公开的，猜到的 dataset id 不能借别人的名字）|
| `dify_knowledge_datasets.py` | 每个 RAGFlow 库 → 一条 Dify external dataset（#1001 / ADR-0096）。**权限要靠第二次请求**：创建接口的 payload 没有 `permission` 字段，落库默认 `only_me`（只对创建者可见，编排页里选不到），所以建完必须 PATCH；而那个 PATCH 对两个 external 字段缺任一都 `ValueError`，"只改权限"也得回填。幂等键是 `(workspace, external_knowledge_id)` 不是名字（`datasets` 表没有名字唯一约束）。名字上限按**两个接口里更严的 40** 判 —— 创建允许 100、更新只允许 40，用 100 建完再授权会 422 并留下一条谁都看不见的 dataset。RAGFlow 里已删的库只报告不删（删 dataset 不可逆，且会静默破坏引用它的 app）|
| `knowledge_dataset_sync_guard.py` | 上面那条同步的**并发闸**（#1056 / ADR-0122）。运营打开「添加知识库」弹窗就会触发一次收敛，两个人（或两个标签页）同时开就是两个 pass，各自读一遍列表、各自决定建同一个 —— 而 Dify 对 `(tenant, name)` 没有唯一约束，输的一方留下重复。两件东西：非阻塞锁（同一时刻只跑一个）与租户级下限（成功 15s / **失败 5s** —— 抖动之后运营重开弹窗应该能重试，而不是被自己刚才的失败挡住）。`run()` **绝不抛**：调用方在弹窗路径上，一次失败不该让弹窗报错 |
| `knowledge_triple.py` | 1:1:1 核对（#927）：一个租户的三样东西齐不齐。三项**分开报**（缺 workspace / 缺 RAGFlow 租户 / 模型没校验过），因为三种半成品的修法不同；Dify 的 workspace 列表没给就不判断那一维 —— 没去看不等于判它坏了 |
| `ragflow_console.py` | 运营用公司账号进 RAGFlow 的代理（#926）：RAGFlow 的鉴权把租户固定在**登录者自己**，所以共享只能是"以 owner 换到的会话"——代理**代表租户**，不派发密码。租户只来自调用者身份（没有接受 tenant 参数的入口）；未开户与无权限是两个码；200 + `code!=0` 与"200 但没会话"都是拒绝，不把浏览器送去一个登出状态的页面。共用一个 owner 的代价记在我们自己的 `knowledge_access_ledger`（谁、何时、结果、原因码） |
| `dify_knowledge_migration.py` | 把 Dify 的外部知识库配置搬进租户 workspace（#927）：`plan()` 是对两棵快照的**纯函数**，三个已知坑（绑定跨 workspace / 权限行 tenant 没同批更新 / 两个 workspace 同名模板）都住在决定里，所以可以完全不碰数据库地推演；`verify()` 搬完自检，不满足就失败而不是报成功 |
| `dify_inner_api.py` | Dify ``inner_api`` 的客户端（#880）：``POST /inner/api/enterprise/workspace`` 建 workspace、member 端点把已有账号 join 进去（``create_tenant_member`` —— 邀请对已激活账号在 ``RBAC_ENABLED=False`` 下静默不生效）。地址是配置项：容器内网 ``IP:5001`` → 转发器上线后 ``127.0.0.1:15001``，代码不改 |
| `dify_console_admin.py` | Dify **控制台** API 的客户端，按 workspace 做管理动作（#881）：装插件（``/plugin/install/pkg``，不是 marketplace —— 36 上 ``MARKETPLACE_ENABLED=false``；安装是异步的，发完轮询任务到 ``success`` 才往下走）、写 provider 凭据、导 DSL。凭据是 ``ADMIN_API_KEY + X-WORKSPACE-ID`` —— **不是** ``/workspaces/switch``（那会把该账号其它 join 的 ``current`` 全置假并 commit，对并发请求可见）。错误一律带 ``[workspace <id>]``：多租户之后不带租户的报错等于没有信息 |
| `dify_tenant_access.py` | 给**运维账号**开通某个租户 workspace 的访问权（#883 之后的运维入口）：Dify 的 workspace 切换器只在一个条件下隐藏 —— 这个账号的 workspace 列表为空；成员关系一到，导航栏的 workspace 卡片里就出现切换器，点一下调 `/workspaces/switch`。所以"公司运维在 Dify 里切租户"**不需要改 Dify、不需要重建 web 镜像**。命令**不碰 `current`**（切哪个由人点），也**不批量**（25 个 ROLE_ADMIN 里 18 个半年没登录过）|
| `dify_workspace_ready.py` | 把 ``created`` 推到 ``ready``（#881 / ADR-0076）：插件 → 凭据 → **模板 DSL**，顺序不能换（模板引用 provider，provider 没凭据时"导入成功、一跑就 ``ProviderTokenDoesNotExist``"）。模板失败**只记状态**、状态停在 ``created`` —— 写成 ``failed`` 会让下一次 ``ensure-workspace`` 重建一个永久孤儿。三条动作的幂等判据都是"先看有没有"。模型 key 来自部署配置，代码里没有默认值 |
| `deployment_environment.py` | 部署身份里**环境标识**的唯一判据（ADR-0059 / ADR-0061）：网关的 `AIOPS_GATEWAY_ENVIRONMENT` 与登录代理的 `AIOPS_LOGIN_PROXY_ENVIRONMENT` 共用它。只校验、**不归一化** —— `prod.41` 与 `prod/41` 化成同一个值就会把两个部署并成一个 |

#### L6 服务边界与入口

| 模块 | 职责 |
|---|---|
| `gateway_api.py` | FastAPI 应用工厂 + **全部 66 条路由** + 请求/响应 schema。**最大单文件（5226 行）** |
| `gateway_runtime.py` | Gateway 的服务端编排核心：持有数据库凭据、Codex session、线程池，执行 run / QA / 健康报告 / 标准诊断 |
| `cli.py` | Typer 根应用 `aiops`：`init`/`key-install`/`paths`/`diagnose`/`doctor`/`shell`/`agent-resume`/`agent-doctor`，并挂载 `admin` 与 `remote` 子应用 |
| `gateway_server.py` | `aiops-gateway`：`serve`、一次性注册码签发、设备管理 |
| `gateway_cli.py` / `gateway_client.py` | `aiops remote …` 客户端子应用与它的 stdlib HTTP 客户端 |
| `gateway_config.py` / `gateway_tokens.py` | Gateway 服务端设置与客户端 profile/令牌的磁盘持久化 |
| `gateway_logging.py` | 网关自身日志的去处（#636）：**在这一模块之前，网关根本没配过 logging** —— uvicorn 只给 `uvicorn*` 挂 handler，root 停在 WARNING 且无 handler，于是 `aiops.*` 的 INFO 行一行都没出去过（41 实测 0 行），能出去的 WARNING 走 `lastResort`、形态是不带级别前缀的一行。本模块给 **root** 挂 handler、给我们的两个命名空间设级别（第三方不被带着一起变啰嗦），级别由 `AIOPS_GATEWAY_LOG_LEVEL` 定、写错是启动失败 |
| `chat_execution.py` | **App 聊天 → 客户执行节点**的接线（APP-RUNTIME-01）：把「本人订单状态」交给节点跑，POST 立即返回 202 + `qa_id`，答案由同一条 run 的成功快照适配成原 blocks 形状。绑定是**按（环境，租户，入口，场景）登记**的表（`AIOPS_GATEWAY_CHAT_EXECUTION_BINDINGS_FILE`），选中的那一条冻进 run 记录；授权、TTL、执行配置守卫与最终放行**与委派入口共用同一份实现**。命中绑定后任何失败都落 QA `failed`，**不回落直读、旧模型或别的节点**。默认不启用。 |
| `chat_data_grant.py` | **一次性取数凭据**（APP-RUNTIME-02）：Dify 的 HTTP 步骤要用它回调网关取本人那一单。≥256 bit 随机，库里**只存 sha256**；绑死 主体/订单/环境/租户/QA/workflow 与 180 秒有效期；只允许**一次**业务读取，重复回调只回同轮已存的快照；完成/失败/取消三种终态都立即撤销。它不是会话凭据、也不是整租户 key —— 那两个都会把「一个人、一次、一单」的授权放大成可长期复用的能力 |
| `dify_chat_workflow.py` | **已发布的 Dify Workflow** 的最小调用面：`/v1/workflows/{workflow_id}/run`（streaming）与 `/v1/workflows/tasks/{task_id}/stop`，app service key 从 0600 文件读。开始事件即保存 `workflow_run_id` + `task_id` 并允许取消，最终事件核同一身份且 `succeeded`；运行时**不跑草稿**、不自动追最新版本 |
| `node_publication.py` | 节点 SOP 发布与试用：从已登记编辑应用冻结提示词和可信执行清单，复用版本、正式路由、撤回与恢复；节点按 run 获取清单并实际核对，不转换任意 Dify Workflow 图 |
| `consumer_trial.py` | 在临时Chatflow执行WF01草稿，沿用源路由和试用范围，结束清理临时应用 |
| `consumer_publication.py` | WF01发布目标与路由场景，冻结已审核内容及只读能力 |
| `consumer_billing.py` | 本人订单账务记录的裁剪与保守展示，不把金额字段或退款记录当支付渠道回执 |
| `consumer_knowledge.py` | WF01知识回调、本轮检索引用与已审核答案、安全/人工兜底；短期凭据不允许跨用途取数 |
| `consumer_chat_service.py` | WF01原提问/轮询/取消与用户会话接线，固定发布绑定、同轮结果和回读守卫，默认关闭 |
| `dify_chatflow.py` | WF01 Chatflow Service API：固定发布版本和会话，核对消息与工作流双完成后返回答案；调用方负责授权和业务放行 |
| `dify_workflow_trial.py` | **真 Dify 试用**（M3-01）：把这一轮冻结的草稿导入一个**临时 Dify 应用**、发布、跑一次，取真实 `workflow_run_id`/`task_id` 与同轮取数证据，然后删掉临时应用。**不动正式指针**、不产生 `agent_versions` —— 试用不是发布。临时应用的凭据与取数凭据都按 app/这一次隔离 |
| `chat_route_bindings.py` | **聊天路由的正式绑定**（M3-01）：这个（环境、租户、入口、场景）此刻由**哪一版**执行。发布在这里推进指针，撤回把它改成 `retracted`（**不删行** —— 删行会让请求掉回直读，那是冒充停用）。行上冻结 `agent_id` + `agent_version_no` 指向不可变版本快照，`app_id` / `workflow_id` / 版本号三列分开存。`revision` 是乐观锁；文件登记与管理表**同时声明同一个 key ⇒ 启动失败**，不裁决优先级 |
| `dify_run_store.py` | Dify 路线的**执行记录**（表 `chat_dify_runs`）：`workflow_run_id`/`task_id`/`app_id`/`workflow_id`/`grant_id`/快照/scope/绑定键/状态。刻意**不复用** `agent_runs` —— 那张表要填 `node_id` 与 `matched`，会让「这次没有节点」读成「节点核对过了」 |
| `request_observation.py` | 个人状态查询的**请求级结果观察**：ASGI 中间件给每个请求生成服务端 `request_id`，每次**候选**请求写恰好一行 `assistant_question_result`（`stage`/`outcome`/`http_status`/`error_code`/`tenant_hash`/`qa_id`/`duration_ms`）。**是日志不是第二张指标表** —— `agent_run_metrics` 的保留期与形状是承重的，再开一张同义表会出现两个分母。身份未建立时不写，拒绝不伪造 QA。见 `docs/agents/consumer-order-chat/observability.md` |
| `admin_cli.py` | `aiops admin …`：声明式环境清单收敛（`reconcile`）、快捷动作迁移 |
| `tdengine_proxy.py` | `aiops-tdengine-proxy`：TDengine 前的严格只读 SQL 白名单代理 |
| `responses_adapter.py` | `aiops-responses-adapter`：修复上游 Responses API 的两个兼容缺口（缺失 item `status`、grammar 与 tools 冲突），对客户端完全透明 |
| `codex_launcher.py` | Codex 二进制 exec 包装（清洗环境变量后 `execve`） |

### 3.3 数据/配置文件

| 文件 | 内容 | 谁读 |
|---|---|---|
| `src/aiops_diagnostics/faq_catalog.json` | 固定问答正式目录（中文权威 + `en/de/fr/es/pt` 五种翻译） | `faq.FAQCatalog.bundled()` |
| `src/aiops_diagnostics/faq_recommendations.json` | 各平台/语言的推荐展示清单（不含答案） | 同上 |
| `examples/fixtures/*.json` | 三份离线合成 fixture（YKC 金额不一致 / OCPP 正常 / 交易数据缺失） | `FixtureSources` |
| `ops/environments/<env>.toml` | 某环境期望的已发布智能体清单 | `aiops admin reconcile` |

---

M3 委派的消费者成功响应交付同 run 五字段快照，并在返回前复核本人授权、绑定与有效期；
不二次直读、不改变统一助手路由。真实用户与 App 接线仍待验收。

M3 发布闭环还需单独部署[运营发布桥](ops/dify-console-bridge/README.md#m3-部署包与恢复)
与[节点运行制品](deploy/m2-live-03/README.md)，Gateway CD 不覆盖它们。
M3 正常登录用户验收与 App 接线边界见[用户入口验收手册](docs/agents/m3-user-entry-acceptance.md)；
文件预检不发请求，真实正向验收仍需要明确授权账号的本人订单。

## 四、模块之间的契约（接缝）

这一节回答第三个问题：**高层模块与底层设计决策之间靠什么连接**。
每一次"这里可以换一种实现"都是一个接缝；接缝下面是 ADR 记录的决定。

### 4.1 证据源接缝

**协议**：`DiagnosticSources`（`sources.py:64`），8 个方法。
**实现**（都是鸭子类型，结构上满足协议）：

| 实现 | 什么在变 |
|---|---|
| `MySQLSource` / `TDengineSource` / `RedisSource` | 三种直连后端，各自带范围下推或设备/Stream 白名单 |
| `HttpSources` | 改为调用远端 `/diag/*` HTTP 契约（**当前已降级为回退路径**） |
| `HybridSources` | 混合：订单/设备/Stream 走 HTTP，TDengine 直连 |
| `ScopedSources` | 全部直连且受一个 `QueryScope` 约束；TDengine 的设备集合由订单元数据经 `DeviceGate` seed，未 seed 即拒绝 |
| `LiveSources` | 全直连组合 |
| `FixtureSources` | 离线 JSON 回放 |

**工厂接缝**（都是 context manager，负责按需建立 SSH 隧道）：
`direct_sources()`、`live_sources()`、`scoped_live_sources()`。

> ⚠️ **两个源集合并存，这不是历史包袱而是推论三的必然结果。**
> 标准 API 面携带 `QueryScope`，走 `ScopedSources`，租户以参数绑定谓词下推；
> 设备运行路径不携带范围对象（没有 SQL 可下推），落到 `HybridSources`，租户由工具层按行过滤。
> 一个租户规则、两种渲染。**不要试图合并它们**——合并意味着要么给设备路径伪造一个它没有的
> 范围对象，要么放弃 SQL 下推。
> 现状注记：`.env.example` 已把 `/diag/*` HTTP 段标记为 DEPRECATED（PRD #23，2026-08-31），
> `live_sources()` 仍返回 `HybridSources`，因此**对外提供设备诊断的部署仍需 HTTP 段配置**。

### 4.2 推理接缝

| 接缝 | 协议/注入点 | 允许替换什么 |
|---|---|---|
| `CodexSession` | `codex_runtime.py:113`；经 `AgentCoordinator.session_factory` 注入 | 跑 turn 的模型 harness（生产是 `SDKCodexSession`，测试可注入假实现） |
| `KnowledgeBindingResolver` | `agent_lifecycle.py` | 发布前知识库绑定能否校验（默认 fail closed 的 `Unavailable…`） |
| `KnowledgeSearchClient` | `knowledge_retrieval.py` | 知识库检索后端 |
| `SessionFactory` / `ProgressCallback` | `agent_engine.py:34` | 会话构造与进度外送 |

### 4.3 身份与授权接缝

| 接缝 | 协议 | 实现（按配置选择） |
|---|---|---|
| `CallerContextResolver` | `caller_auth.py` | `SourceKey…`（按来源密钥在两条信任模式间分派，**装在会话那一级之前**；未配置密钥时整层不构造）/ `Disabled…`（fail closed 默认）/ `Upms…`（公司 Bearer）/ `Introspection…`（RFC 7662 令牌自省，强制 HTTPS 与 audience）/ `CompanyToken…`（`company_token_auth.py`，管家端公司 OAuth2 令牌；缺来源密钥时不构造） |
| `OrderAuthorizer` | `caller_auth.py` | `Disabled…` / `ScopedOrderAuthorizer` |
| `PlatformDirectory` | `scope_context.py` | `UpmsDirectory` |
| `SiteScopeMapper` / `DisDirectory` | `query_scope.py` | 静态映射 / `DisHttpDirectory` |
| `BSubjectDirectory` | `third_session_auth.py` | `UpmsBSubjectDirectory`（C 端用户 → B 端主体，会话身份补全） |
| `ShopDirectory` | `query_scope.py` | `UpmsShopDirectory`（B 端主体 → 店铺集合，与后端权威授权同一端点） |
| `OperatorSiteScope` | `third_session_auth.py` | `UpmsOperatorSiteScope`（运营商站点集合，管家端会话的数据范围） |

生成物是冻结的 `ScopeContext`，其 `scope_fingerprint` 是下游所有存储与授权的键。

### 4.4 Gateway 存储接缝

`GatewayStore` **没有 Protocol**，接缝是鸭子类型 + 构造注入
（`create_gateway_app(store=…)`、`GatewayRuntime(store, …)`）。
同一个 SQLite 文件上挂了五个 store，各自建表；`GatewayStore` 的类文档写明
「接口留待日后迁移到 PostgreSQL」。

### 4.5 与底层设计决策的映射

| 决策 | 落在哪些模块 |
|---|---|
| [ADR-0001](docs/adr/0001-read-only-diagnostic-boundary.md) 只读诊断边界 | `diagnostic_tools.py`（工具白名单）、`sources.py`（有界查询）、`config.SafetySettings`、`ops/` 代理 |
| [ADR-0002](docs/adr/0002-trusted-pr-control-plane.md) 可信 PR 控制面 | `.github/workflows/agent-*.yml`、`.sandcastle/policy-check.mjs`、`.sandcastle/trusted-pr-delivery.sh` |
| [ADR-0003](docs/adr/0003-bff-delegated-user-identity.md) BFF 委托用户身份（**未实现**，现行是共享会话直读：见 [ADR-0008](docs/adr/0008-adr-0003-deviation-session-direct-read.md)） | `caller_auth.py`、`scope_context.py`、`third_session_auth.py`、`gateway_api.py` 的 `authenticated_caller` |
| [ADR-0004](docs/adr/0004-bff-owns-frontend-api-boundary.md) BFF 拥有前端接口边界 | `gateway_api.py` 的标准 API 面、`docs/standard-api-contract.md` |
| [ADR-0005](docs/adr/0005-codex-mediated-knowledge-retrieval.md) Codex 中介知识检索 | `knowledge_retrieval.py`、`qa_rag.py`、`agent_contracts.qa_rag_turn_schema` |
| [ADR-0006](docs/adr/0006-shortcut-actions-extend-to-in-app-navigation.md) 快捷动作分两种形态 | `shortcut_lifecycle.py`、`gateway_api.py` 快捷动作路由 |
| [ADR-0007](docs/adr/0007-answer-surface-language-guard.md) 回答面语言校验 | `answer_language.py`、`i18n.py`（判定），挂点在各回答面定稿处 |

---

## 五、运行入口

`pyproject.toml` 声明四个 console script，对应四条独立的进程边界：

| 命令 | 入口 | 起什么 |
|---|---|---|
| `aiops` | `cli.py:app`（Typer） | 工程师本地诊断：`diagnose` / `doctor` / `shell` / `agent-resume` / `agent-doctor` / `init` / `key-install` / `paths`，并挂 `admin` 与 `remote` 子应用 |
| `aiops-gateway` | `gateway_server.py:main` | 多端服务：`serve` 起 uvicorn 承载 66 条路由；`issue-enrollment` / `devices` / `revoke-device` 管设备 |
| `aiops-tdengine-proxy` | `tdengine_proxy.py:main` | loopback-only 的 TDengine 严格只读代理 |
| `aiops-responses-adapter` | `responses_adapter.py:main` | Responses API 兼容反向代理 |

`python -m aiops_diagnostics` 走 `__main__.py`，并在 argv 含 `__codex-launcher` 时转给
`codex_launcher:main`（冻结产物里 Codex 子进程的重入点）。

### 一次诊断的调用链（agent 路径）

```text
cli.diagnose → parsing.parse_request
             → Settings.from_config → incident manifest → AgentWorkspace.create
             → agent_runner.run_agent_diagnosis
                 ├─ _agent_sources()  → FixtureSources | scoped_live_sources | live_sources
                 ├─ EvidenceJournal + DiagnosticToolExecutor + preflight_environment
                 └─ AgentCoordinator.run()
                        ├─ SDKCodexSession.run(prompt)          ← 模型决定要什么证据
                        ├─ tools.execute_many(tool_requests)    ← harness 执行有界只读查询
                        ├─ journal.record(...) + artifact SHA   ← 证据落盘
                        └─ AgentResultValidator.validate(...)   ← 输出过合同
             → render.render_agent_diagnosis
```

### 一次 Gateway 请求的调用链

```text
gateway_api 路由 → CallerContextResolver.resolve → ScopeContext
                 → GatewayRuntime.start_*()  → 落库 + ThreadPoolExecutor.submit
                 → _execute_*  → scoped_live_sources(scope) → run_agent_diagnosis
                 → store.update_*（终态） + 事件序列
```

---

## 六、仓库地图

```text
src/aiops_diagnostics/    产品面 Python 全部代码（L0–L6）
tests/                    77 个测试文件，按关注点平铺（无 conftest）
java/                     充电桩后端侧的 /diag/* 查询接口与审计切面工件 —— 见下方警告
ops/                      TDengine 只读代理的 systemd 单元与环境清单
packaging/                PyInstaller 规格与便携包构建 + 烟测
tools/                    数据生成器与校验器（合成验收数据、FAQ 目录与多语言合并/派生/校验、
                         存量快捷动作语言迁移）—— 见 tools/README.md
examples/                 离线 fixture 与合成验收数据
docs/                     架构、契约、ADR、运行手册（见第十节路由表）
.github/workflows/        确定性 CI + AFK 治理面流水线
.sandcastle/              AFK 执行层（TypeScript 编排、静态策略、投递状态机）
AGENTS.md                 仓库级开发共识（中文文档、里程碑同步、业务边界）
GLOSSARY.md                领域词汇表（术语 + 应避免的同义词）
SOP.md / 充电桩问题排查SOP.md   业务规则来源（后端行为的中文说明）
acceptance.feature        Gherkin 可执行验收约束（60 Feature / 157 Rule / 411 Scenario）
qa-plan.md                QA 用例（158 个用例 ID，含环境/前置/数据/动作/预期/清理）
```

> ⚠️ **`java/` 是待落地工件，不是本仓库构建产物。**
> 本仓库没有 JDK/Spring 工具链，这两个文件没有被编译、没有被部署。它们的生产归宿是
> 充电桩后端仓库（见 [java/README.md](java/README.md)）。仓库内只有四个 Python 契约测试
> 守护其对外形状，**这不等于真实验收**。

---

## 七、改动指南：动哪里、读哪些文件

| 我想… | 先读 | 主要改动点 | 注意 |
|---|---|---|---|
| 加一条诊断业务规则 | `rules.py`、`engine.py` | `rules.py`（纯函数）+ 必要时 `engine._finalize` 的分类优先级 | 规则来源必须是后端源码或 SOP，不能凭直觉；置信度阶梯要同步 |
| 加一个只读证据工具 | `diagnostic_tools.py`、`agent_contracts.ToolName` | `ToolName` 枚举 + `TOOL_DESCRIPTIONS`/`TOOL_SOURCES` + executor 分支 | `ALL_TOOL_NAMES` 会**自动**带上它（默认值由枚举推出）；补 fixture 与测试 |
| 改租户/站点可见性 | `order_visibility.py`（先读模块文档） | 只改这一处；三种渲染会一起生效 | **绝对不要**在入口或数据源里另写一份过滤——这正是六次重复的由来 |
| 换模型 provider 或加 key slot | `docs/thin-harness.md`、`config.AgentSettings` | 环境变量注册表（`AIOPS_PROVIDERS` 等） | 不改代码即可切换；跨 provider 的格式差异在 `agent_engine._parse_agent_turn` 归一化 |
| 加一条 Gateway 路由 | `gateway_api.py`、`gateway_runtime.py` | 路由 + 请求模型 + runtime 执行方法 + store 表 | 权限必须经 `ScopeContext`，不能从请求体取 `user_id`/`tenant_id` 当授权依据 |
| 加一项持久化 | `gateway_store.py` 等 | 在既有 SQLite 文件上建自己的表 | 迁移到 PostgreSQL 时需一并处理 |
| 改输出语言行为 | `i18n.py`（判定）、`answer_language.py`（应用） | 挂到目标回答面的**定稿点** | 判定只有一处；不要用提示词兜底（ADR-0007 明文禁止） |
| 调安全上限 | `config.SafetySettings` | 构造时校验区间 | 上限是设计的一部分，放宽需说明理由 |
| 调知识库检索深度 | `gateway_config.GatewayServerSettings` | `AIOPS_GATEWAY_KB_SEARCH_MAX_RESULTS`（1..20，默认 **10**） | **一个口径，两条路径共用**：客户问答/宣传卡片与 Dify 适配路由都读它，Dify 另对它传来的 `top_k` 封顶。默认 10 是实测定的（图片型知识库的媒体块坐落在第 5~10 名），见 [docs/gateway.md](docs/gateway.md) 与 ADR 0049 |
| 改 AFK 交付流程 | `.sandcastle/policy-check.mjs`（先读它的断言） | workflow + policy check 同步改 | policy check 是静态断言，改 workflow 不改它会让 CI 红 |

---

## 八、验证与交付纪律

### 确定性检查（唯一强制门）

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) 一个 job，GitHub-hosted：

```bash
uv sync --locked --inexact --dev
uv run ruff check . && uv run ruff format --check .
uv run pytest -rs          # -n auto 已由 addopts 提供
uv run python -m compileall -q src tests && uv pip check
```

**跑测试不需要记任何 flag —— `-n auto` 是默认**：

```bash
uv sync --locked --dev     # 唯一前提：它会装好包括调度器在内的一切
uv run pytest              # 并行，约 47 s
uv run pytest -n 0         # 显式串行，约 245 s
./tools/test.sh            # 同上，外加一次 lockfile 一致性检查
```

> **`pytest-xdist` 是声明的 dev 依赖，不是手工装的。** 这一点在 2026-10-23 改过，
> 原因是实测：一个**新 worktree** 的 venv 里没有它，于是 `-n auto` 直接
> `unrecognized arguments: -n`，唯一出路是把 `-n` 去掉 —— 静默退回串行
> （279 s vs 55 s）。只要它不在 lockfile 里，「能不能并行」就取决于**有没有人记得装**，
> 而不是取决于这份代码需要什么。
>
> 代价说清楚：`uv.lock` 变了，所以 **41 的依赖漂移门会拒绝部署**，直到按
> `docs/agents/env-41-dependency-update.md` 人工更新一次。这是一次性的，且没有改
> 那条门禁 —— 它保护的是「生产重启后不会 ModuleNotFoundError」，不该为了省一次人工
> 流程去改它。

### 持续部署（[:cd.yml](.github/workflows/cd.yml) + [:deploy/deploy-41.sh](deploy/deploy-41.sh)）

CI 保证「合并进来的东西是对的」，CD 负责「把它送到 41 上跑」。触发条件是
`main` 上命中 `cd.yml` 的 `push.paths` 的改动（纯文档合并不部署，也不该占用人审批的注意力）。

```
push main (src/** | deploy 的两个部署脚本 | 依赖清单 | 参考资料) → cd.yml → environment: production-41 → deploy-41.sh
```

`deploy/` 下**只有** `deploy-41.sh` 与 `classify-remote-result.sh` 在触发集合里（#520）；
`company-gitlab-api.sh`、`routing-window.sh` 这类只读取数脚本**不在** —— 改它们不重启生产。
判据是「这次部署是否真的执行它」，不是文件名。

> ⚠️ **2026-09-30 起这道门是自动的**：仓库所有者决定去掉人工审批（原文见
> [`docs/validation.md`](docs/validation.md) 的「决定记录：CD 的人工批准门按仓库所有者决定去掉」）。
> **任何合并到 main 且命中 `cd.yml` 的 `push.paths` 的提交都会自动部署到生产。**
> 「什么人能改生产」因此等于「**什么人能合并到 main**」—— 那道门仍在
> （Ruleset 要求 PR、必需检查、会话解决），去掉的只是"合并之后再点一次"。
> `production-41` 环境本身保留（部署账本、只允许 main 的分支策略）。
> ⚠️ **回滚就一处**：在环境上重新加回 required reviewer；`cd.yml` 不需要动 ——
> 那里**做不出**一个自动批准（environment 审批发生在 job 的步骤开始之前）。

四个设计选择，每个都有具体理由：

1. **部署逻辑只在 `deploy-41.sh` 里**。runbook §2 是同一套步骤的散文版；两处各写一遍
   必然漂移（§2 的 tar/rsync 配对 bug 就是这么来的）。脚本是自动化与手工应急的唯一共同实现。
2. **走 `dev-host`，不自造 ssh+rsync**。`dev-host` 已实现策略要求的门：主机公钥身份断言、
   写入服务主机需要 `--artifact-sha256`（载荷 sha 与声明不符即拒，退出 77）。
   自己写一套传输就等于绕开「变更只能作为已识别产物到达服务主机」。
3. **CD 用独立 ssh 别名 `aiops-41-cd`（独立密钥）**，人工仍用 `aiops-41`。
   这样自动写入可独立吊销而不影响人工路径，41 的 `authorized_keys` 也能区分来源。
   `deploy-41.sh` 不往主机清单里加第二条 41 记录（那会制造两份角色记录），
   而是**运行时从主清单派生**一份视图、只替换 `ssh_alias`，并断言其余字段一致。
4. **部署后断言 `/health` 含本次 commit**。`/health` 的 `version` 原先是个静态
   `0.1.0`，无法回答「现在跑的是哪个 commit」——回滚也就无从验证。脚本在**暂存副本**上
   把 `__init__.py` 的 `__version__` 写成 `<semver>+<short-sha>`（不动工作树），
   于是部署后可断言、回滚后可确认。

**CD 只证技术健康，不证业务验收。** 真实端到端需要业务方签发的 thirdSession，
runbook §5/§6 明确禁止 CI 自证。所以 CD 通过最多报告 `merged_waiting_deploy`；
`live` 仍须人按 §5 执行。

**部署之后还有一步：发布版本备注。** `cd.yml` 的最后一个步骤运行 `dev-changelog`，
把本次部署的 commit 范围渲染成 GitHub Release（tag `release-<sha12>`）。它**不是**部署
的一部分，失败含义也不同：

| 现象 | 含义 | 处置 |
|---|---|---|
| Deploy 步骤失败 | 生产**没有**更新 | 按本节回滚，或修因重跑 |
| Deploy 成功、`Publish release notes` 失败 | 生产**已经**更新，只是没有备注 | **不要回滚**。到 runner 上确认 `dev-changelog` 与 `git-cliff` 在 PATH，然后 `gh workflow run cd.yml` 重跑，或用 `gh release create` 手工补 |

`CHANGELOG.md` 因此**不在仓库里**（已 gitignore）：它是发布产物，由上述步骤生成，
任何 PR 都不该写它。人工撰写的发布说明在 [`RELEASE-NOTES.md`](RELEASE-NOTES.md)——那是
另一份文件，两者在 2026-10-23 的比对中**零行交集**（决策见
[docs/adr/0101](docs/adr/0101-changelog-is-generated-at-release-time.md)）。

**版本号由 [`VERSION`](VERSION) 决定，人改它来发布。** CD 只读，从不推导 —— 版本号是
「这次发布包含什么」的声明；让提交信息的措辞决定它，一次误标的 `feat!` 就会跳大版。

三处声明必须相等，由 `tests/test_release_version.py` 强制：

| 文件 | 角色 |
|---|---|
| `VERSION` | **权威**，发布时改这一份 |
| `pyproject.toml` 的 `project.version` | 副本（打包元数据） |
| `src/aiops_diagnostics/__init__.py` 的 `__version__` | 副本（`/health` 读它） |

两个 tag，含义不同：

| tag | 何时建 | 会移动吗 |
|---|---|---|
| `v<version>`（如 `v0.2.0`） | 该版本**首次**部署 | **不会**。同版本再部署只打印它指向哪个 commit |
| `release-<sha12>` | **每次**部署 | key 含 sha，各是各的 |

版本 tag 刻意不带 `+<sha>`：实测每次部署打一个带 build metadata 的 tag，会让
`dev-changelog` 一次部署开一节，changelog 变成按部署切碎的流水。

### 测试组织

`tests/` 是 77 个平铺文件的集合，按关注点聚类：
`test_assistant_*`（助手面）、`test_standard_*`（标准 API 契约）、
`test_diag_*_contract`（每个 `/diag/*` 端点的契约）、`test_*scope*`（权限栈）、
`test_agent_*`（agent 运行时）、`test_readme_architecture.py`（**本文档自身的形状守护**——见下）。
最新本地全量记录为 **1082 passed**；当前数字以
[docs/validation.md](docs/validation.md) 为准。

**README 自身有守护。** 本文件声称「每个模块都在这儿被解释」以及「包内无环」，
两条都是可机检的，所以它们由 `tests/test_readme_architecture.py` 断言：
模块必须被说明、README 引用的模块必须存在、相对链接必须可解析、包内必须无导入环。
文档漂移会让 CI 变红——而不是等下一个接手者踩坑。
（守护只查形状，不查措辞是否讲清楚了。）

> 已知缺口：`admin_cli.py` 与 `gateway_server.py` 没有专门的测试文件——
> `serve` 装配、注册码签发、设备管理、清单收敛命令未被单元测试覆盖。

### 文档与证据纪律

1. **每个可交付里程碑**必须**新增**一个 `docs/开发进度/<日期>-<短名>.md` 与一个
   `docs/validation/<日期>-<短名>.md`（不要编辑既有的单文件 —— 它们是 2026-10-23 之前的
   只读归档；一个记录一个文件是为了让并行 PR 不撞同一处），变更用户入口/配置/部署/安全
   边界时还要同步 README 与对应架构部署文档。
2. **证据分级不许混用**：自动化测试、生产只读回放、真实 provider 调用、便携包验收、
   工程师确认的真实故障结论是五件不同的事。没有后者时必须明确写「未完成业务验收」，
   **不得**把 fixture 或模型调用写成真实故障结论。
3. **契约测试守护文档形状**：例如取消契约的 7 个响应样例由
   `tests/test_assistant_cancel_handoff.py` 在真实网关栈上抓取——**文档漂移即测试失败**。

### AFK 治理面

`issue → 实现 → 审查 → PR` 的自动流水线，信任边界为
[ADR-0002](docs/adr/0002-trusted-pr-control-plane.md)：只接受仓库所有者创建的**同仓库**分支；
宿主从当前 `main` 加载可信 controller，候选代码只在 Docker 沙箱内以**只读 token** 执行；
结果经 Git bundle 导入干净 delivery checkout 后才使用短时写 token 推送；凭据缺失即 fail closed。
流程与阶段边界见 [docs/afk-workflow.md](docs/afk-workflow.md)。

---

## 九、当前边界与未验收事项

**这个系统目前没有工程师确认的真实故障结论。** 自动化测试、生产只读回放、
真实 provider 调用和便携制品验收只能证明**实现一致性**与**运行边界**，
不能替代业务准确性验收。

- 每个受支持的故障路径都需要至少 3 个工程师确认的真实故障案例，
  逐项比对系统给出的摘要、分类、证据与下一步建议。**即使建议恰好正确，
  一个错误的高置信度结论仍算失败。** 待验收路径清单见
  [docs/validation.md](docs/validation.md) 的「业务验收待办」。
- 已知未验收：41 公网停止链路与 `/v1/agent-metrics` 聚合面；
  `health_report_jobs` / `standard_diagnoses` 的重启收敛；
  五种语言的真实调用（en/de/fr/es/pt，等待可用 thirdSession）；
  真实外部贡献者的 fork PR 执行路径。
- 第一版**明确不支持**两轮车完整规则（标记 `unsupported_order_type` 并降低置信度）。
- 业务动作面**关闭**：不提供重算、退款、补发、改单、改配置、消息重放、消费游标、重启服务。
- Gateway 是**单节点 MVP**，适合内网与受控验收；公网生产前必须完成
  TLS、OIDC/mTLS、Vault/KMS、PostgreSQL、速率限制、审计与撤销——
  见 [docs/gateway.md](docs/gateway.md)。

---

## 十、文档路由与历史遗留文件

**先读这几份，它们是当前的权威来源：**

| 你要回答 | 读 |
|---|---|
| 现在到哪一步了？ | [docs/agents/current-delivery-state.md](docs/agents/current-delivery-state.md) + [docs/validation/](docs/validation/) 下按文件名排序最新的一篇 |
| 某个历史决定为什么这么做？ | [docs/adr/](docs/adr/) |
| 某个术语到底指什么？ | [GLOSSARY.md](GLOSSARY.md)（词汇表，不含规格） |
| 对外接口契约？ | 固定问答 [docs/faq-api.md](docs/faq-api.md)；健康报告与单问诊断 [docs/standard-api-contract.md](docs/standard-api-contract.md)（**冲突时以这两份为准**） |
| 怎么跑起来？ | [docs/快速上手.md](docs/快速上手.md) |
| 95 是哪台机、怎么连、里面有什么？ | [docs/agents/env-95-facts.md](docs/agents/env-95-facts.md)（另一套 AI-Ops 环境；与 41 数据面独立，当前只做只读核实） |
| 系统架构简述？ | [docs/architecture.md](docs/architecture.md) |
| agent 运行时细节？ | [docs/thin-harness.md](docs/thin-harness.md) |
| 多端部署？ | [docs/gateway.md](docs/gateway.md) |
| 里程碑历史？ | [docs/开发进度.md](docs/开发进度.md) |
| 多语言支持到什么程度了？ | [docs/reviews/2026-10-07-i18n-program-retrospective.md](docs/reviews/2026-10-07-i18n-program-retrospective.md)（完成度复盘）+ [docs/faq-api.md](docs/faq-api.md)（契约侧语言清单） |
| 怎么在生产上端到端验一次？ | `.claude/skills/verify-aiops-client-e2e/`（真用户会话驱动生产网关）+ `docs/agents/env-41-runbook.md` §5.5/§5.6 |
| 数据生成/校验工具怎么用？ | [tools/README.md](tools/README.md) |

**历史遗留文件（保留但不代表当前状态，不要据此判断）：**

- `docs/handoff-brief.md`、`docs/afk-cutover/*` — 某次交切的会话快照，相关工作已完成。
- `docs/qa-plan-real.md`、`docs/real-acceptance-results.md`、`docs/synthetic-acceptance-data.md`
  — 某一时点的验收记录，已被 `docs/validation.md` 取代。
- `docs/architecture/ai-ops-architecture.*` — 架构图快照；生成规格在同目录 `.json`。
- `docs/原型/`、`docs/知识库材料/`、`docs/用户端.docx`、`docs/管家端.docx`、`scripts/`
  — 已被 `.gitignore` 忽略，不进入仓库。

> 判断一份文档是否仍权威，看有没有**入链**：被 `AGENTS.md`、本 README 或
> `current-delivery-state.md` 指向的是权威；孤立的多半是快照。

---

## 架构图

![AI-Ops 架构](docs/architecture/ai-ops-architecture.png)

运行时架构可视化：[PNG](docs/architecture/ai-ops-architecture.png) ·
[SVG](docs/architecture/ai-ops-architecture.svg) ·
[可交互 HTML](docs/architecture/ai-ops-architecture.html)（含主题切换、平移缩放、视图聚焦、关系追溯）。
生成规格见 [`docs/architecture/ai-ops-architecture.json`](docs/architecture/ai-ops-architecture.json)。

正式节点常驻运行与旧试验配置退役见[部署手册](deploy/node-runtime/README.md)；
生产启用须按该手册完成真实页面验收，合并部署件本身不代表已启用。

2026-10-25 正式节点状态路线已启用，原 H5 本人查询经发布 V18/真实节点/baoyun 完成并与
SQL/CPMS 独立对照一致；旧试验配置已归档。范围与限制见[真实验收](docs/validation/2026-10-25-production-node-enabled.md)。

WF01发布可冻结审核内容、素材和只读能力，支持临时Chatflow试用及撤回恢复；详见[发布验收](docs/validation/2026-10-25-consumer-publication.md)。
WF01已审核FAQ可关联图片/视频，素材随QA与绑定失效；详见[媒体验收](docs/validation/2026-10-25-consumer-media.md)。
WF01可显式启用本人状态单轮读取，复用原授权与CPMS快照；实时轮次不进入后续模型记忆。
C端明确未解决反馈已接入同会话主题检索与第二次人工渠道提示，详见[验收](docs/validation/2026-10-25-consumer-followup.md)。
C端业务完善正在按[WF01](docs/agents/consumer-wf01/README.md)推进：原知识包先校验为
可追溯草稿，再接Chatflow、原App连续会话、只读业务和多媒体；编译成功不代表已上线。
