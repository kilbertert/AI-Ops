# Snail AI 远端配置与发布机制研究

日期：2026-10-29。范围：以第二租户管家开户仍需服务器命令这一实际问题，研究 Snail AI 的
业务配置入口、节点分发与发布语义。本文是研究结论与建议，不表示新流程已实施或验收。

## 结论

Snail AI 值得继续借鉴的是**在管理页面选择执行应用、模型和工具，保存后由平台自动组装请求，
已注册节点接收并执行**。其公开版的“发布”主要是配置保存和市场可用状态，未发现对应我们
不可变版本、受控正式路由、执行摘要确认的完整发布事务。它也不负责 Dify 工作区开户或公司订单权限。

因此我们应把现有开户、依赖准备和发布操作收进 Dify 的一个业务动作，并保留已有公司身份和
单订单授权；无需让运营进入 Snail AI 再配置一次，也无需重新做远程执行框架。

## 证据基线与获取方式

| 基线 | 本次证据 |
|---|---|
| AI-Ops 平台制品 | [锁文件](../../deploy/m2-live-03/images.lock.json)固定 Gitee `10c1d3e50b701be62da23a3403d79a41187ab8cd`；本地留存平台JAR的SHA-256与锁文件完全一致 |
| SDK | [节点POM](../../services/execution-node/pom.xml)使用 `snail-ai-agent-executor-starter:1.1.1` |
| 官方仓库分支 | `git ls-remote`：Gitee master为`10c1d3e…`，1.1.1为`31c52e8277f012cfa5cfbafc53c2baaf654afa3d`；本次GitHub官方镜像master也是后者 |
| 源码核对 | 读取官方GitHub固定修订，再与Gitee固定提交的Git树比较：122个已读取文件blob相同，README与AgentService不同；不把两个分支直接说成相同 |
| AgentService差异补证 | Gitee该blob下载遇到TLS失败；使用摘要匹配的现役平台制品，以断网JDK容器`javap -c`静态核查create/update调用，与下述原地写入语义一致 |
| 官方文档 | 读取官方站点的配置、客户端、应用、市场、模型、MCP及API说明；发现与源码不符处以固定源码为准 |

官方入口：[Gitee仓库][repo]、[官方文档][guide]。下列源码引用使用可访问的官方镜像固定修订；
除单列AgentService外，所引用文件已核对与Gitee锁定版本的blob一致。未启动上游服务、未操作生产
平台管理API、未测试上游企业版。源码/树/制品检查输出保留在本次研究的gitignored临时目录。

## 用户理解的“远端发布”实际包含什么

### 1. 配置面是统一表单，执行面是预先接入的应用

官方配置页把名称、提示词、问候语、模型、RAG、MCP、Skill、记忆、执行应用放在一个编辑页面，
旁边有对话预览。保存动作一次提交这些引用，页面展示保存完成。执行应用下拉来自已启用的应用。
这能直接借鉴到 Dify：业务人员选择“在哪个业务环境使用”，内部地址、node ID、模型key和文件路径
由平台解析，不让业务人员拼接。[配置说明][configure]、[应用控制器][app-controller]

Snail 的 App 是**一组执行节点的接入身份与路由配置**，不是公司租户，也不是 Dify App。
`sai_agent.app_id`指向它；`sai_app`保存应用token/路由策略，`sai_client_node`以
`(app_id, host_id)`区分节点。把这三个层次合并成一个“租户ID”会再次制造配置歧义。[结构][schema]

### 2. “发布按钮”不等于不可变部署版本

- `POST /agent`创建；`PUT /agent/{id}`更新。创建时`AgentAssembler`默认ACTIVE；
  `AgentService.update`先更新当前agent行，再调整MCP/Skill关联。[控制器][agent-controller]、[装配器][assembler]
- `sai_agent`只有当前配置和状态，没有`published_version`、`revision`或执行节点确认摘要；
  `sai_skill.version`是技能缓存一致性字段，不能拿来证明智能体版本发布。[结构][schema]
- 对话初始化按ID读取当前agent，后续解析当前模型配置；下次请求携带当前配置分发。
  这属于“配置集中管理、逐次请求下发”，不需要把每份提示词重新打包为JAR。[初始化][init]、[模型解析][model-resolve]
- 官方市场按ACTIVE筛选，订阅是`user↔agent`关系；市场分发不是把应用复制进另一个Dify工作区。
  文档把`/agent/my`说成创建的应用，源码实际按订阅表返回，也说明不能仅靠文案推断权限语义。[市场服务][market-service]

对AI-Ops的判断：现有冻结清单、发布/撤回/恢复及任务摘要校验不应撤掉。要减少的是业务人员
操作的步骤，让一次“创建并启用”在后台完成依赖与登记，而不是把“保存成功”包装成“正式用户已可用”。

### 3. 节点不是由发布按钮远程安装的

Client先安装SDK并配置应用ID、token及Server地址。运行中每10秒发送心跳，服务端校验应用token，
更新节点地址、并发容量和负载；40秒过期窗口、定时落库及缓存回源支撑在线发现。
应用页面可以查看在线节点、负载与踢出节点。[心跳客户端][heartbeat]、[心跳接收][beat]、[节点目录][instances]

每个对话从agent的appId找到启用应用，再查存活节点并按策略选择。已有最少负载、轮询、一致性哈希
等策略；这解决“发给哪个已在线节点”，没有替公司部署一个新节点或审批网络可达性。[路由选择][route]

**文档和源码存在一处直接影响接入体验的差异**：配置文档说未选执行应用使用默认路由，数据库注释
又写NULL=本地执行；实际`ContextCollectorHandler:51–65`遇到空appId、停用应用或无可用节点都会
终止。因此我们的业务向导应在创建前检测执行环境可用性，不沿用“先保存、第一次提问才失败”。

### 4. 请求承载提示词、模型和工具配置，节点执行并回传

`ChatDispatchRequest`携带agent配置、模型配置、MCP描述、技能、历史和systemPrompt。
`LlmCallHandler`把当前上下文序列化后用gRPC流式调用节点；客户端解析请求，运行
`ChatSessionRuntime`并返回文本、完成或错误事件。[分发DTO][dispatch-dto]、[服务端分发][llm-call]、[客户端接收][dispatch-handler]

这与我们现有 Java 节点路线相容，但我们增加了更具体的业务约束：节点收到run后向Gateway读取
该run冻结的manifest、校验run和摘要，再安装本轮提示词/工具/模型守卫。
这部分已经存在于[ReportingChatSessionRuntime](../../services/execution-node/src/main/java/com/trendpower/aiops/node/ReportingChatSessionRuntime.java)。
本次多租户障碍主要在进入该链路前的配置生命周期，不在“Java不会远端执行”。

### 5. MCP有目录和连通性检查，但没有Dify资源映射

管理面提供MCP创建、编辑、列表、绑定及`test-connection`。连通测试执行initialize，服务声明tools
能力时再listTools；编辑后清客户端缓存，删除被引用的服务会拒绝。一次聊天按agent关联查询描述并
下发；MCP配置有headers、command、args、envVars，不能把这些通用能力直接开放给普通租户运营。
[MCP管理][mcp-service]、[每次请求加载][mcp-handler]

可借鉴的是**已有连接资源供选择、配置后有可观察校验、被引用资源不随意删除**。
本次优链充仍要重建cpms-node UUID、配置Dify workspace模型凭据的原因，是两个平台资源空间不同；
Snail MCP目录不会自动修复Dify的provider UUID或公司订单授权。工具连通也不代表当前账号能查指定订单。

文档列出的三种MCP传输不能原样承诺：固定源码`McpServerService:396–413`对SSE明确报弃用，
实际接入应按Streamable HTTP等已核查路径判断。[MCP管理][mcp-service]

## 不能照搬的部分

| 主题 | 已观察机制 | AI-Ops需要保留/补齐 |
|---|---|---|
| 租户 | 公共版业务表没有公司tenant维度；管理角色是ADMIN/USER，App是执行组，OpenAPI用户以app+openId映射 | 公司身份→租户工作区的服务端校验；成员、提示词、工具、订单分别隔离 |
| 发布 | 当前配置原地保存，默认ACTIVE；没有找到跨依赖的持久创建任务 | 一次业务动作的幂等键、阶段状态、失败续跑、最终路由回读；失败不留“成功”假象 |
| 凭据 | 模型分发DTO含apiKey；MCP描述可含headers/envVars | 保持平台凭据引用和已批准模型出口，Dify/业务用户不接触公司任务凭据 |
| 权限 | App token证明接入应用，普通用户订阅证明平台使用关系 | 公司账号当前站点/订单权限不能被App token或订阅关系代替 |
| 可用性 | 节点心跳与负载可见；模型/MCP有独立检测 | 页面区分配置完成、依赖可用、本人权限不足、真实试用通过四种结果 |
| 恢复 | 心跳重连、存活节点剔除与缓存重建 | 不宣称具备跨Dify/网关/节点配置事务；恢复本次操作并保留旧正式路由 |

依据：[表结构][schema]、[App管理][app-service]、[OpenAPI鉴权][openapi-auth]、[模型分发][model-dispatch]。
这些是适配边界，不是上游全产品安全审计；没有以缺少某字段证明整个项目绝无其他机制。

## 最小可落地的借鉴建议

1. **一个业务入口。** Dify提供“新建/复制到租户”，填名称并选有管理权限的目标租户；
   环境从可信平台配置确定，当前仅一个环境时不让用户选。输入是业务对象，不是UUID/路径。
2. **一次后台操作。** 后台依次调用已有workspace/provider/import/registry/publish能力，
   显示“正在准备工作区、正在连接工具、正在启用”等结果；可重试同一任务，不另建副本。
3. **一个模型事实来源。** 业务模板预选获准模型，页面明确实际执行模型；Dify选择器与Java模板
   应映射到同一受控配置，避免再次出现一个选v4.1、实际执行v4的无感差异。
4. **创建前检测，创建后证明。** 预检已有模型、工具、执行节点与租户管理权；完成判据是新app绑定
   正确且新路由可用。正常登录后再测账号业务权限，空站点提示管理员绑定，不能报告“授权系统坏了”。
5. **维护入口留后台。** 节点首次安装、公司环境接入和密钥轮换属于平台维护；普通租户开新智能体
   不应改TOML、节点JSON、登录服务环境变量，也不应为每个租户走一次代码PR。

建议验收以“一个新的真实租户，全程Dify页面创建→公司账号登录→明确订单报告”为正例，
同时测连续点击、半途断网重试、工具失败、节点离线、未授权租户和无订单权限。
“按钮可点/保存成功/路由active”只算对应阶段通过，不代替最终账号与订单报告。

## 研究边界

本轮没有实施新UI/API、部署新服务或修改业务权限。没有真人操作Snail后台，页面流程来自官方文档，
关键语义由源码与摘要一致制品补证。Gitee README与官方镜像README还存在授权描述差异；若后续直接
复制上游代码或面向第三方交付，应固定使用修订并单独核对该修订的LICENSE/NOTICE及附加说明，
本研究不作法律判断，也不据宣传页面承诺企业版能力。

[repo]: https://gitee.com/aizuda/snail-ai
[guide]: https://snailai.opensnail.com/guide/quick-start.html
[configure]: https://snailai.opensnail.com/guide/agent/configure.html
[app-controller]: https://github.com/aizuda/snail-ai/blob/31c52e8277f012cfa5cfbafc53c2baaf654afa3d/snail-ai-server/snail-ai-server-admin/src/main/java/com/aizuda/snail/ai/admin/controller/AppController.java#L29-L90
[agent-controller]: https://github.com/aizuda/snail-ai/blob/31c52e8277f012cfa5cfbafc53c2baaf654afa3d/snail-ai-server/snail-ai-server-admin/src/main/java/com/aizuda/snail/ai/admin/controller/AgentController.java#L57-L108
[assembler]: https://github.com/aizuda/snail-ai/blob/31c52e8277f012cfa5cfbafc53c2baaf654afa3d/snail-ai-server/snail-ai-server-admin/src/main/java/com/aizuda/snail/ai/admin/service/agent/AgentAssembler.java#L115-L210
[schema]: https://github.com/aizuda/snail-ai/blob/31c52e8277f012cfa5cfbafc53c2baaf654afa3d/script/sql/snail_ai_schema.sql#L133-L160
[init]: https://github.com/aizuda/snail-ai/blob/31c52e8277f012cfa5cfbafc53c2baaf654afa3d/snail-ai-server/snail-ai-server-features/snail-ai-feature-agent/src/main/java/com/aizuda/snail/ai/feature/agent/chain/InitContextHandler.java#L27-L40
[model-resolve]: https://github.com/aizuda/snail-ai/blob/31c52e8277f012cfa5cfbafc53c2baaf654afa3d/snail-ai-server/snail-ai-server-features/snail-ai-feature-agent/src/main/java/com/aizuda/snail/ai/feature/agent/chain/ModelResolveHandler.java#L33-L60
[market-service]: https://github.com/aizuda/snail-ai/blob/31c52e8277f012cfa5cfbafc53c2baaf654afa3d/snail-ai-server/snail-ai-server-admin/src/main/java/com/aizuda/snail/ai/admin/service/agent/UserAgentService.java#L39-L108
[heartbeat]: https://github.com/aizuda/snail-ai/blob/31c52e8277f012cfa5cfbafc53c2baaf654afa3d/snail-ai-agent/snail-ai-agent-common/src/main/java/com/aizuda/snail/ai/agent/common/handler/ClientHeartbeatScheduler.java#L35-L221
[beat]: https://github.com/aizuda/snail-ai/blob/31c52e8277f012cfa5cfbafc53c2baaf654afa3d/snail-ai-server/snail-ai-server-features/snail-ai-feature-common/src/main/java/com/aizuda/snail/ai/rpc/handler/BeatRequestHandler.java#L40-L86
[instances]: https://github.com/aizuda/snail-ai/blob/31c52e8277f012cfa5cfbafc53c2baaf654afa3d/snail-ai-server/snail-ai-server-features/snail-ai-feature-common/src/main/java/com/aizuda/snail/ai/ClientInstanceManager.java#L113-L194
[route]: https://github.com/aizuda/snail-ai/blob/31c52e8277f012cfa5cfbafc53c2baaf654afa3d/snail-ai-server/snail-ai-server-features/snail-ai-feature-agent/src/main/java/com/aizuda/snail/ai/feature/agent/chain/ContextCollectorHandler.java#L50-L72
[dispatch-dto]: https://github.com/aizuda/snail-ai/blob/31c52e8277f012cfa5cfbafc53c2baaf654afa3d/snail-ai-commons/snail-ai-commons-core/src/main/java/com/aizuda/snail/ai/common/dto/agent/ChatDispatchRequest.java#L23-L144
[llm-call]: https://github.com/aizuda/snail-ai/blob/31c52e8277f012cfa5cfbafc53c2baaf654afa3d/snail-ai-server/snail-ai-server-features/snail-ai-feature-agent/src/main/java/com/aizuda/snail/ai/feature/agent/chain/LlmCallHandler.java#L60-L117
[dispatch-handler]: https://github.com/aizuda/snail-ai/blob/31c52e8277f012cfa5cfbafc53c2baaf654afa3d/snail-ai-agent/snail-ai-agent-executor/snail-ai-agent-executor-core/src/main/java/com/aizuda/snail/ai/agent/core/grpc/handler/ChatDispatchStreamingHandler.java#L38-L55
[mcp-service]: https://github.com/aizuda/snail-ai/blob/31c52e8277f012cfa5cfbafc53c2baaf654afa3d/snail-ai-server/snail-ai-server-admin/src/main/java/com/aizuda/snail/ai/admin/service/mcp/McpServerService.java#L215-L294
[mcp-handler]: https://github.com/aizuda/snail-ai/blob/31c52e8277f012cfa5cfbafc53c2baaf654afa3d/snail-ai-server/snail-ai-server-features/snail-ai-feature-agent/src/main/java/com/aizuda/snail/ai/feature/agent/chain/McpHandler.java#L38-L75
[app-service]: https://github.com/aizuda/snail-ai/blob/31c52e8277f012cfa5cfbafc53c2baaf654afa3d/snail-ai-server/snail-ai-server-admin/src/main/java/com/aizuda/snail/ai/admin/service/app/AppService.java#L56-L178
[openapi-auth]: https://github.com/aizuda/snail-ai/blob/31c52e8277f012cfa5cfbafc53c2baaf654afa3d/snail-ai-server/snail-ai-server-openapi/src/main/java/com/aizuda/snail/ai/openapi/interceptor/OpenApiAuthInterceptor.java#L79-L114
[model-dispatch]: https://github.com/aizuda/snail-ai/blob/31c52e8277f012cfa5cfbafc53c2baaf654afa3d/snail-ai-server/snail-ai-server-features/snail-ai-feature-model/src/main/java/com/aizuda/snail/ai/model/adapter/server/DispatchModelConfigAssembler.java#L29-L52
