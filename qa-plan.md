# AFK 可信交付 QA 计划

## AFK-B10 可信工作流静态门

- 环境：AI-Ops AFK 任务分支。
- 前置：模板 1.1.1 已部署。
- 数据：六条 AFK 变更 workflow。
- 动作：运行 `node .sandcastle/policy-check.mjs workflows`、actionlint、ShellCheck，并与 afk-bootstrap 的受管文件逐字节比较。
- 预期：同仓库 owner gate、可信 controller、候选只读 token、干净 delivery checkout 和 AGENT_PAT fail-closed 全部通过，受管文件无漂移。
- 清理：无。

## AFK-B11 Bundle 状态机回归

- 环境：afk-bootstrap 临时 Git 仓库测试。
- 前置：模板测试 checkout 可用。
- 数据：落后的本地 main、前进的 origin main、合并结果和远端竞态。
- 动作：运行 afk-bootstrap 的 `test/trusted-pr-delivery.sh`。
- 预期：基线被重置、bundle 原样保留提交、远端竞态被拒绝。
- 清理：测试 trap 删除临时仓库。

## AFK-B12 Live canary

- 环境：AI-Ops self-hosted runner。
- 前置：加固 workflow 已合并，runner 与只读 token、AGENT_PAT 在线。
- 数据：仓库所有者创建的一次性 PR。
- 动作：添加 `agent:review` 并检查 workflow、review、标签和交付分支。
- 预期：使用当前 main，通过 controller/candidate/delivery 隔离完成审核且没有 blocked 标签。
- 清理：关闭一次性 PR，删除临时分支和标签。

AFK-B10：已通过，时间 `2026-08-30T03:31:15+08:00`，提交
`7380fe8c12c738c4f365db8b13c758d204ebed90`，Linux x86_64，Python
3.13.13、Node v24.15.0、actionlint 1.7.12、ShellCheck 0.11.0。证据：`uv run
ruff check .`、`uv run ruff format --check .`、`uv run pytest -q`、compileall、
`uv pip check`、policy checker、actionlint、ShellCheck、`git diff --check` 全部通过；
受管文件与模板逐字节一致。

AFK-B11：已通过，复用模板提交 `84e9537c661f676f68951eb3e7480472b91ff728` 的
`bash test/trusted-pr-delivery.sh`，覆盖 stale main、bundle 提交保留和远端竞态拒绝。

AFK-B12：待模板合并后在 AI-Ops 在线 self-hosted runner 执行 owner-authored
`agent:review` canary，保留 workflow URL、review、标签和清理证据后再标记通过。
workflow YAML 不适用复杂度或 mutation 工具；安全状态机由模板动态测试覆盖，本仓负责静态门和部署一致性。

## 受限诊断运行时 QA 计划

## SCP-01 权限上下文解析（有效/无效凭证）

- 环境：AI-Ops 本地开发环境，不透传生产凭据。
- 前置：`--scope-json` 选项可用；UPMS/Dis 为离线 fake。
- 数据：有效平台凭证、空凭证、伪造凭证。
- 动作：对每种凭证构造 `ScopeContext`（`ScopeResolver->ScopeRequest`），断言解析成功或 `ScopeError` 失败闭锁。
- 预期：有效凭证得到 `caller/subject/effective_tenant_id/scope_fingerprint`；空/伪造凭证抛 `SCOPE_ERROR_AUTH_FAILED` 且无数据库调用。
- 清理：无。

## SCP-02 目标主体越权与租户切换

- 环境：AI-Ops 本地，UPMS/Dis 离线 fake。
- 前置：`ScopePolicy` 配置代查权限码与管理角色码。
- 数据：无权限调用者、有代查权限调用者、平台管理员、跨租户目标主体。
- 动作：分别请求 `target_b_user_id`、跨租户 `tenant_id`，断言拒绝或解析。
- 预期：无权限代查 → `delegation_denied`；普通调用者指定他人租户 → `tenant_forbidden`；管理员显式切换租户 → 采用请求租户。
- 清理：无。

## SCP-03 三类数据库受限范围查询

- 环境：离线 fixture + 伪造 MySQL/TDengine/Redis 适配器。
- 前置：`QueryScope` 冻结。
- 数据：允许站点/设备/租户内外的订单、设备、报文与 Stream 消息。
- 动作：执行 MySQL 订单/占位费/设备、TDengine 枪属性/报文、Redis Stream 检查，断言过滤条件与结果。
- 预期：范围外数据不返回；空站点短路；越权设备拒绝且不发 TDengine 请求；Redis 只统计租户归属可验证消息；无法验证归属不返回原始正文。
- 清理：无。

## SCP-04 UPMS/Dis 不可用与失败语义

- 环境：离线 fake 目录与 Dis 传输。
- 前置：UPMS/Dis 抛 `URLError` / HTTP 401 / 畸形响应。
- 数据：三种失败输入。
- 动作：解析 `ScopeContext` / 代查目标站点归属，断言错误码。
- 预期：均 fail closed 且错误码区分 `upms_unavailable` / `auth_failed` / `dis_config_missing`；不误报为“空业务数据”。
- 清理：无。

## SCP-05 CLI 受权限诊断入口

- 环境：AI-Ops CLI，Fake `_ssh_tunnel`。
- 前置：`--scope-json` 可用。
- 数据：合法 `QueryScope` JSON、非法 tenant_id。
- 动作：`aiops diagnose --mode deterministic --scope-json '{"tenant_id":"T","site_ids":["S1"],"user_id":null}' ...`；对非法 JSON 断言 `BadParameter`。
- 预期：合法 scope 走 `scoped_live_sources`（受限 MySQL/TDengine/Redis）；非法 scope 被拒绝；无需真实数据库即可验证接线。
- 清理：无。

证据要求：每项记录提交/构建身份、环境、时间戳与日志或报告制品；未连接真实 UPMS/Dis/生产库，fixture 与 fake 不能冒充真实环境验收。

## 标准调用者认证 QA 计划

## SAPI-01 有效 token 与订单范围

- 环境：AI-Ops 本地 TestClient，caller resolver 与订单授权器使用离线 fake。
- 前置：标准订单授权接口启用，existing device API 保持可用。
- 数据：有效 Bearer token、SELF 范围调用者、范围内订单 O-ALLOW。
- 动作：调用订单授权探针。
- 预期：HTTP 200，返回 O-ALLOW 与 scope fingerprint，不返回 token、VIN、权限副本或内部凭据。
- 清理：删除临时 Gateway SQLite。

## SAPI-02 token 失败与注入

- 环境：AI-Ops 本地 TestClient。
- 前置：fake resolver 可记录是否被调用，fake order authorizer 可记录查询次数。
- 数据：缺失 Bearer、错误 scheme、无效/过期 token、缺 scope token、裸 user_id/tenant_id 注入、aops_ device token。
- 动作：逐一调用标准订单授权探针。
- 预期：稳定 401/403；token 解析失败时不查询订单；裸身份字段不改变调用者上下文；device token 不获得标准接口权限。
- 清理：无。

## SAPI-03 跨主体与跨租户订单

- 环境：AI-Ops 本地 TestClient，离线授权 fake。
- 前置：调用者上下文已验证。
- 数据：同租户其他主体订单、跨租户订单、不存在订单。
- 动作：逐一调用订单授权探针。
- 预期：全部统一返回 HTTP 404 + ORDER_NOT_FOUND，不泄露资源存在性。
- 清理：无。

## SAPI-04 Introspection 合同

- 环境：AI-Ops 本地，HTTP transport fake。
- 前置：配置 HTTPS introspection endpoint、client credentials、AI-Ops audience 与 required scope。
- 数据：active 响应、inactive、错误 audience、过期、缺 subject/tenant/scope、HTTP 401、网络失败和畸形 JSON。
- 动作：解析 access token。
- 预期：有效响应生成不可变 ScopeContext；所有无效响应 fail closed，错误不包含 token/client secret。
- 清理：无。

## SAPI-05 兼容性回归

- 环境：AI-Ops 本地 TestClient。
- 前置：既有 enroll/run 测试数据。
- 数据：合法和非法 aops_ device token。
- 动作：执行 enroll、create/list/get run 与标准订单授权探针。
- 预期：既有接口行为不变；device token 不能访问标准接口。
- 清理：删除临时 Gateway SQLite。

证据要求：SAPI-01..05 当前只证明离线合同和权限失败语义；真实 issuer/introspection、真实订单范围与生产调用方尚未完成业务验收。

## 公司 cloud-auth Bearer 适配 QA 计划

## CLOUD-AUTH-01 现有 token 解析

- 环境：本机现有 cloud-auth/UPMS 隧道，使用短期测试 token。
- 前置：`AIOPS_UPMS_BASE_URL` 指向已批准的认证/UPMS 入口。
- 数据：有效 cloud-auth Bearer token、过期 token、伪造 token。
- 动作：调用标准订单授权接口。
- 预期：有效 token 经 UPMS 生成 ScopeContext；无效 token fail closed；不复制 HS256 密钥。
- 清理：删除临时 token 和日志。

## CLOUD-AUTH-02 user_id 注入

- 环境：同上。
- 数据：无 Bearer、`X-User-Id`、`user_id`、`tenant_id` 裸字段。
- 动作：调用标准订单接口。
- 预期：统一 401/403；不触发订单数据源查询。
- 清理：无。

## 最小异步健康报告 QA 计划

## HRJ-01 创建、轮询与完成

- 环境：AI-Ops 本地 TestClient，标准 caller/order 授权 fake，报告 sources 使用离线订单。
- 前置：有效 Bearer token 与 `aiops:orders:read` scope。
- 数据：已结束订单 O-DONE，含有效设备、created_time/stop_time 和停止原因。
- 动作：POST 创建报告作业，GET 轮询直到终态。
- 预期：202；状态只经过 queued/running/completed；报告含最小指标、确定性摘要、rule_version、data_as_of、completeness。
- 清理：删除临时 Gateway SQLite。

## HRJ-02 复用与保留

- 环境：AI-Ops 本地，GatewayStore 可控时钟边界。
- 前置：同一 scope fingerprint、order_no、rule_version。
- 数据：queued、running、completed、failed、expired 作业。
- 动作：重复创建和查询。
- 预期：queued/running 与 15 分钟内 completed 复用；failed/expired 创建新作业；failed 保留 5 分钟；过期资源返回 expired。
- 清理：删除临时 Gateway SQLite。

## HRJ-03 订单准入与部分数据

- 环境：离线报告 sources。
- 前置：受限 QueryScope 已建立。
- 数据：充电中订单、缺设备、缺时间、超长窗口、缺停止原因的已结束订单。
- 动作：生成最小报告。
- 预期：核心准入错误使作业 failed；缺停止原因仍 completed，指标 unavailable 并带 reason_code。
- 清理：无。

## HRJ-04 超时与重启

- 环境：GatewayRuntime/GatewayStore 离线测试。
- 前置：作业 deadline 30 秒。
- 数据：执行超过 deadline 的 builder；数据库中遗留 queued/running 作业。
- 动作：完成执行或重新初始化 store。
- 预期：超时作业不写回 completed；启动时遗留作业变 expired；随后可创建新作业。
- 清理：删除临时 Gateway SQLite。

## HRJ-05 权限与隐私

- 环境：AI-Ops 本地 TestClient。
- 前置：两个不同 scope fingerprint 的 caller。
- 数据：同一 job_id、越权订单、随机 job_id。
- 动作：创建和查询作业。
- 预期：越权订单不创建；其他 caller 与随机 ID 统一 404；响应不含 scope fingerprint、VIN、SQL、原始报文或内部路径。
- 清理：删除临时 Gateway SQLite。

证据要求：HRJ-01..05 仅使用离线订单与 fake caller；未连接真实订单/遥测，未完成业务验收。

## 标准单问诊断 QA 计划

## DX-01 自由文本诊断

- 环境：AI-Ops 本地 TestClient，caller resolver/order authorizer fake，Agent 接缝 fake。
- 前置：有效 Bearer token 与诊断写入 scope。
- 数据：授权订单、自由文本问题。
- 动作：POST 标准诊断，GET 诊断直到终态。
- 预期：202 + opaque diagnosis_id + retry_after_ms；后台调用受限 Agent 接缝；终态为 completed 或 inconclusive，结果不含内部资源。
- 清理：删除临时 Gateway SQLite 和私有 run workspace。

## DX-02 指标上下文与输入拒绝

- 环境：AI-Ops 本地 TestClient。
- 前置：标准诊断请求模型启用 extra=forbid。
- 数据：合法 indicator_code、score、curve、health_report、裸 user_id/tenant_id。
- 动作：提交指标提问与伪造字段。
- 预期：合法指标作为上下文；伪造字段返回 422 或不进入 Agent prompt；裸身份字段不改变 ScopeContext。
- 清理：无。

## DX-03 历史诊断主体隔离

- 环境：AI-Ops 本地 TestClient，两个不同 scope fingerprint 的 resolver。
- 前置：调用者 A 已创建诊断。
- 数据：A 的 diagnosis_id、调用者 B、随机 diagnosis_id。
- 动作：B 查询详情和列表；A 查询列表。
- 预期：B 对详情统一 404、列表为空；A 只能看到自身诊断摘要；不泄露问题、结果或内部字段。
- 清理：删除临时 Gateway SQLite。

## DX-04 诊断生命周期与重启

- 环境：GatewayStore/GatewayRuntime 离线测试。
- 前置：diagnosis deadline 30 秒、完成保留 15 分钟、失败保留 5 分钟。
- 数据：queued/running/completed/inconclusive/failed/expired 诊断。
- 动作：模拟 worker、迟到 completion 和服务重启。
- 预期：终态不可覆盖；inconclusive 保留；超时/重启转 expired；失败或过期后可重新创建。
- 清理：删除临时 Gateway SQLite 和 run workspace。

证据要求：DX-01..04 仅证明标准 API、Agent 接线和隔离合同；未连接真实 access-token issuer、生产订单或真实模型，未完成业务验收。

## 标准健康报告曲线 QA 计划

## CURVE-01 降采样与极值

- 环境：AI-Ops 本地健康报告计算模块。
- 前置：构造 400 个乱序时序点，包含重复时间和显著极值。
- 数据：功率、电压、温度字段及缺失字段。
- 动作：构建曲线响应。
- 预期：每条曲线最多 300 点，时间有序，保留首末点与极值，缺失系列为空。
- 清理：无。

## CURVE-02 完整输入与来源状态

- 环境：Gateway health-report worker fake sources。
- 前置：报告计算使用完整采样，遥测源可切换成功/失败。
- 数据：大于 300 点数据、空数据、SourceError。
- 动作：执行报告作业。
- 预期：指标计算不依赖降采样；遥测失败时作业仍可部分完成，source_summary.telemetry 为 unavailable，响应不含内部字段。
- 清理：删除临时 SQLite 和 run workspace。

证据要求：CURVE-01..02 当前为离线契约验证，未连接真实 TDengine，未完成业务验收。

## 完整健康指标 QA 计划

## METRIC-01 公式边界与单位

- 环境：AI-Ops 本地确定性计算模块。
- 前置：构造各公式分段端点、SOC 百分点、能量和温度/电压输入。
- 数据：完整与异常物理值。
- 动作：计算五维 radar 和 SOH。
- 预期：分段边界、0..100 分数、SOC 单位换算和 rule_version 与规范一致；异常值不可用。
- 清理：无。

## METRIC-02 权威容量限制

- 环境：健康报告 worker fake source。
- 前置：遥测完整但 nominal capacity 缺失、模糊 VIN/车型候选存在。
- 动作：构建报告。
- 预期：SOH 与 capacity score unavailable，不执行模糊匹配；其他指标继续返回。
- 清理：无。

证据要求：METRIC-01..02 当前为离线公式验证，未完成真实业务验收。

## 标准 API 真实环境验收（#92）

## REAL-API-01 完整订单报告

- 环境：批准的集成环境，标准 API issuer/introspection、AI-Ops Gateway、受限 MySQL/TDengine/Redis 均可达。
- 前置：调用方拥有 `aiops:orders:read`，订单已结束且范围授权有效；记录构建身份和规则版本。
- 数据：一笔遥测完整且容量权威的真实订单。
- 动作：创建健康报告作业，按 `retry_after_ms` 轮询至终态，保存脱敏响应和日志制品。
- 预期：completed；指标、曲线、来源摘要和 rule_version 存在；不含 VIN、凭据、SQL 或内部路径。
- 清理：删除测试作业/临时日志，不修改业务数据。

## REAL-API-02 部分数据订单

- 环境：同 REAL-API-01。
- 前置：调用方有权访问；订单缺少一个非核心遥测或协议字段。
- 数据：一笔数据不完整的真实订单。
- 动作：创建并轮询健康报告。
- 预期：completed；可用指标继续返回，缺失项 unavailable 并带稳定原因码；不把结果写成完整健康结论。
- 清理：同上。

## REAL-API-03 越权与资源隔离

- 环境：同 REAL-API-01，准备两个不同主体/租户 token。
- 前置：主体 A 有订单 O 的权限，主体 B 无权限。
- 数据：O、随机 job_id、随机 diagnosis_id。
- 动作：B 创建/查询 O 的报告和诊断，A 查询自己的资源。
- 预期：B 统一 404 或稳定拒绝，不泄露资源存在性；A 正常访问；日志不含 token。
- 清理：删除测试作业/临时日志。

## REAL-API-04 诊断与性能

- 环境：同 REAL-API-01，允许测试 provider 或受控真实模型。
- 前置：诊断写入 scope；固定数据规模和并发。
- 数据：自由问题和 indicator_code 问题各一条。
- 动作：创建/轮询诊断；重复创建报告作业；模拟服务重启和超时。
- 预期：诊断状态稳定；健康报告与诊断独立；创建作业 P95 <500ms，报告 P95 <15s，30s 超时生效；重复作业复用，重启未完成作业过期。
- 清理：删除测试作业和 provider 临时资源。

### 当前状态（2026-09-02）

- REAL-API-01..04：`blocked`。
- 原因：当前工作区没有标准 API issuer/introspection 配置、批准的调用方 access token、可用于本 PRD 的真实订单和部署入口；已有真实验收资料只覆盖 PRD #23 受限直连，不能冒充本次标准 API 验收。
- 解除条件：提供批准的集成环境、短期调用凭据、测试订单/主体范围和日志保留位置后，逐项执行并记录提交/构建身份、环境、时间和制品。

## 合成数据离线验收

- 数据制品：`examples/synthetic-acceptance/synthetic_acceptance.json` 与 `manifest.json`。
- 生成：`uv run python tools/generate_synthetic_acceptance_data.py`。
- 执行：`uv run python tools/run_synthetic_acceptance.py`。
- 预期：完整订单 400 点曲线被限制为 300 点；部分订单报告保留但 telemetry unavailable；跨租户订单返回 ORDER_NOT_FOUND。
- 状态：离线仿真通过，不解除 REAL-API-01..04 的真实环境 blocked 状态。

## 统一标准 API QA 计划

## CONTRACT-01 资源与错误统一契约

- 环境：AI-Ops 本地 TestClient。
- 前置：标准 caller/order/runtime fake。
- 数据：健康报告、单问诊断、非法请求、越权资源。
- 动作：分别创建和查询两类资源。
- 预期：共享 Bearer 与稳定 error.code/message/retryable 形状；资源 ID、状态和结果互不串扰；客户端不会看到内部字段。
- 清理：删除临时 SQLite。

## 固定问答与 C/B 平台 QA 计划

## FAQ-01 目录生成

- 环境：本地 AI-Ops checkout，使用业务 DOCX 文件作为未追踪输入。
- 前置：两个 DOCX 可读；输出目录为空或已有上一版目录。
- 数据：用户端和管家端 DOCX。
- 动作：运行 `uv run python tools/generate_faq_catalog.py`，校验 JSON 语法、版本、数量、前缀和推荐字段边界。
- 预期：生成 28 条 consumer、17 条 operator；推荐项不含答案；原文换行、数字和符号保留；重复/冲突统计可见。
- 清理：删除临时输出；不把 DOCX 加入 Git。

## FAQ-02 平台判定

- 环境：本地 TestClient，fake thirdSession caller 与 fake UPMS 目录。
- 前置：C 端无 B 绑定、唯一 B 绑定、多 B 绑定和未知角色数据。
- 数据：consumer/operator 入口及缺失入口。
- 动作：调用推荐和目录接口。
- 预期：客户端入口返回 consumer；唯一管家主体返回 operator；多主体返回 409 `PLATFORM_AMBIGUOUS`；错误不泄露凭据。
- 清理：删除临时 SQLite。

## FAQ-03 答案与隔离

- 环境：本地 TestClient。
- 前置：已加载版本化 FAQ 制品。
- 数据：有效 question_id、未知 ID、跨平台 ID、带额外字段的请求。
- 动作：调用推荐、目录和答案接口。
- 预期：推荐不含答案；答案同步 200 且保留原文；跨平台/未知 ID 为 404 `FAQ_NOT_FOUND`；额外字段为 422 `INVALID_REQUEST`；不产生诊断资源。
- 清理：删除临时 SQLite。

证据要求：FAQ-01..03 记录提交、环境、时间戳和测试日志；不把目录 fixture 或离线身份 fake 写成真实业务权限验收。
