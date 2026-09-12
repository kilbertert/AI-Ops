# AFK 可信交付 QA 计划

## 41 切换 QA

| ID | 环境 | 操作 | 预期结果 | 清理 |
|---|---|---|---|---|
| CUTOVER-41-01 | 41 | `curl http://127.0.0.1:8788/health` | 200，`business_mutations=disabled` | 保留服务 |
| CUTOVER-41-02 | 120→41 | `curl http://127.0.0.1:28789/health` | 200 | 保留隧道 |
| CUTOVER-41-03 | 公网 | 无效会话调用 `/v1/faq/recommendations` | 401 `INVALID_ACCESS_TOKEN` | 不产生业务数据 |
| CUTOVER-41-04 | 41 | MySQL、Redis、TDengine 只读连接探针 | 三项连接成功 | 不执行写操作 |

真实 41 会话、演示订单和成功路径待业务授权后执行，当前不标记为通过。

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
- 前置：diagnosis deadline 15 分钟（真实 Agent 链路实测约 7 分钟，2026-09-05 联调发现 30 秒 deadline 会把运行中的诊断提前判 expired）、完成保留 15 分钟、失败保留 5 分钟。
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

## 智能体生命周期 QA 计划（T2）

### AGENT-LIFE-01 草稿租户与角色隔离

- 环境：AI-Ops 本地 Python 3.13，临时 SQLite，`AgentStore` 生命周期服务。
- 前置条件：准备 tenant-a 的智能体管理员、发布管理员、普通角色和 tenant-b 管理员。
- 测试数据：客服与运维草稿各一份。
- 有序动作：管理员创建、读取、编辑、列出；普通角色和 tenant-b 读取同一 ID。
- 预期结果：管理员成功且 revision 增加；其他调用者得到统一拒绝，不泄露资源存在性。
- 清理：删除临时 SQLite。
- 结果：PASS（2026-09-09，本地 pytest `tests/test_agent_lifecycle.py`）。

### AGENT-LIFE-02 发布依赖校验与不可变快照

- 环境：同 AGENT-LIFE-01，fake 知识库状态 resolver。
- 前置条件：草稿绑定 ready、parsing、missing 知识库；模型/输出合同包含 allowlist 与非法值。
- 测试数据：带 Prompt、开场问题、快捷指令、媒体输出合同的客服草稿。
- 有序动作：尝试非法依赖发布；修正依赖后按 revision 发布；再编辑草稿并读取旧版本。
- 预期结果：非法依赖不生成版本；成功发布 version_no=1；旧快照字段不随草稿编辑变化；过期 revision 返回冲突。
- 补充动作：从 version_no=1 派生新草稿、修改配置并发布。
- 补充预期：旧版本字段保持不变，新发布版本为 version_no=2。
- 清理：删除临时 SQLite。
- 结果：PASS（2026-09-09，本地 pytest `tests/test_agent_lifecycle.py`）。

### AGENT-LIFE-03 停用与删除边界

- 环境：同 AGENT-LIFE-01。
- 前置条件：一个未发布草稿和一个已有 version_no=1 的智能体。
- 有序动作：删除未发布草稿；停用已发布智能体；查询状态和版本；尝试删除已发布智能体。
- 预期结果：未发布草稿删除后统一未找到；停用后状态为 disabled，历史快照仍可读，已发布智能体不能删除。
- 清理：删除临时 SQLite。
- 结果：PASS（2026-09-09，本地 pytest `tests/test_agent_lifecycle.py`）。

证据边界：AGENT-LIFE-01..03 验证 AI-Ops 生命周期协议和权限边界；后台 Java BFF、真实 UPMS 角色、知识库生产状态与浏览器页面由 #171/#170/#173 接入，未完成业务验收。
## 受限知识检索与媒体协议 QA 计划

### RAG-MEDIA-01 检索白名单与调用上限

- 环境：AI-Ops 本地 Python 3.13，pytest，fake `kb-service` client。
- 前置条件：已发布智能体版本绑定 `KB-A`；fake 响应包含 `KB-A` 与其他知识库结果。
- 测试数据：一条带 `image_id` 的图片分段、一条视频分段、一个越权知识库分段。
- 有序动作：调用 `knowledge_search`；检查返回分段和请求参数；重复调用至第三次。
- 预期结果：只返回绑定知识库；Codex 不能指定 `kb_id/top_k`；第三次返回 `limited` 且不再访问 fake client。
- 清理：释放内存 fake，无持久化数据。
- 结果：PASS（2026-09-09，源提交 `efcaf29`，PR #176 merge commit `c8be858`）。

### RAG-MEDIA-02 检索结果规范化

- 环境：同 RAG-MEDIA-01。
- 前置条件：fake RAGFlow 响应使用 `content_with_weight`、`similarity`、`image_id`、`doc_type_kwd` 原始字段。
- 测试数据：PNG 图片分段、MP4 视频分段、无内容分段、无效 MIME 分段。
- 有序动作：执行结果规范化并读取 `blocks` 资源字段。
- 预期结果：文本脱敏；返回不透明资源 ID、相对媒体地址、引用 ID 和允许 MIME；不暴露内部 image/object/token；无效分段不产生媒体。
- 清理：释放内存 fake。
- 结果：PASS（2026-09-09，pytest `tests/test_knowledge_retrieval.py`）。

### RAG-MEDIA-03 媒体授权、TTL 与 Range

- 环境：AI-Ops 本地媒体代理，内存资源签发器和 bytes fetcher。
- 前置条件：签发租户 A、智能体版本和会话绑定的 MP4 资源，TTL 60 秒。
- 测试数据：10 字节媒体对象；租户 B、过期时间和主动失效资源各一组。
- 有序动作：完整读取；请求 `bytes=2-5`；跨租户读取；过期读取；失效后读取。
- 预期结果：完整读取 200；Range 返回 206、`video/mp4`、`Content-Range`、`Accept-Ranges` 和对应字节；越权/过期/失效返回 403 且 fetcher 不读取对象。
- 清理：清空内存签发器。
- 结果：PASS（2026-09-09，pytest `tests/test_knowledge_retrieval.py`）。

### RAG-MEDIA-04 依赖不可用降级

- 环境：同 RAG-MEDIA-01。
- 前置条件：fake client 抛出超时/上游不可用异常。
- 测试数据：一条普通问题。
- 有序动作：调用 `knowledge_search`。
- 预期结果：返回 `retrieval_status=unavailable`、空分段和空媒体；异常不泄露上游 token、URL 或堆栈。
- 清理：释放 fake。
- 结果：PASS（2026-09-09，pytest `tests/test_knowledge_retrieval.py`）。

证据边界：RAG-MEDIA-01..04 是 AI-Ops 协议和安全边界的离线自动化验证；未连接生产
`kb-service`、RAGFlow、真实租户或真实浏览器，因此**未完成业务媒体验收**。

## 客服 QA RAG 单轮运行 QA 计划（T3/#170）

### QA-RAG-01 blocks 合同与媒体授权

- 环境：AI-Ops 本地 Python 3.13，pytest，脚本化 Codex session + fake kb-service client + 内存媒体签发器。
- 前置条件：租户有已发布客服智能体（绑定 KB-A）；检索命中含 PNG 图片分段和 MP4 视频分段。
- 测试数据：图片分段（`image_id`/`image/png`）、视频分段（`doc_type_kwd=video`/`video/mp4`）、伪造媒体 ID、伪造引用 ID 各一组。
- 有序动作：发起业务问题；模型请求 `knowledge_search`；harness 注入检索结果；模型返回引用媒体的回答。
- 预期结果：`blocks[]` 含 text/image/video/reference 块；image 块带本轮签发的媒体描述（`media_/…` URL、MIME），不含 RAGFlow 标识或对象路径；伪造媒体/引用块被丢弃且文本保留。
- 清理：释放内存 fake 与临时 run 目录。
- 结果：PASS（2026-09-10，pytest `tests/test_qa_rag.py`）。

### QA-RAG-02 检索状态三态与降级

- 环境：同 QA-RAG-01。
- 前置条件：fake client 分别返回空分段、抛上游不可用异常、模型声称 found 但零检索。
- 测试数据：一条普通业务问题。
- 有序动作：执行三组独立运行并读取 `retrieval_status`。
- 预期结果：空命中 → `not_found`；kb 不可用 → `unavailable`（文本仍交付）；零检索声称 found → 被修正为 `not_found`。
- 清理：释放 fake。
- 结果：PASS（2026-09-10，pytest `tests/test_qa_rag.py`）。

### QA-RAG-03 补检索与调用上限

- 环境：同 QA-RAG-01。
- 前置条件：模型首轮直接回答不检索（业务问题）；另一组模型连续请求三次检索。
- 测试数据：业务问题"怎么拔出充电枪"；寒暄"你好"。
- 有序动作：业务问题观察 harness 是否拒绝首轮无检索回答并要求补检索；连续三次请求观察第三次行为；寒暄观察是否免检索。
- 预期结果：业务问题触发一次补检索后完成（总检索 ≤ 2）；第三次检索被 guard 拒绝且状态为 limited；寒暄直接完成且检索次数为 0。
- 清理：释放 fake。
- 结果：PASS（2026-09-10，pytest `tests/test_qa_rag.py`）。

### QA-RAG-04 智能体选择边界

- 环境：本地 SQLite AgentStore + 内存知识绑定 resolver。
- 前置条件：草稿、已发布客服、已发布运维、已停用客服智能体各一。
- 有序动作：依次调用选择器并交叉用其他租户调用。
- 预期结果：只有已发布客服智能体被选中；草稿/运维/停用不服务；跨租户统一不选中（无存在性泄露）。
- 清理：删除临时数据库。
- 结果：PASS（2026-09-10，pytest `tests/test_qa_rag.py`）。

### QA-RAG-05 统一入口回归

- 环境：本地 FastAPI TestClient，fake runtime。
- 有序动作：跑既有 assistant API 测试套件（FAQ 短路同步答案、显式订单 202 诊断、qa 202+轮询、422/401/409 行为）。
- 预期结果：全部既有行为不变；新增 RAG 路径不改变 FAQ 与 diagnosis 分流。
- 清理：临时目录自动回收。
- 结果：PASS（2026-09-10，pytest `tests/test_assistant_api.py` 全量 568 项含回归通过）。

证据边界：QA-RAG-01..05 为 mock-first 协议级验证。真实 kb-service/RAGFlow canary、
Java BFF 透传与前端 blocks[] 渲染属 #171/#173 及真实媒体验收范围（见 P0-E2E-REAL），
本票不把 fake 结果记为业务验收。

## 会话与活跃订单上下文 QA 计划（T4/#172）

### CONV-01 会话隔离与生命周期

- 环境：AI-Ops 本地 Python 3.13，FastAPI TestClient，临时 SQLite，fake caller/目录/授权器。
- 前置条件：用户 A（租户 T-1，consumer 入口）创建会话。
- 测试数据：另一租户 token、operator 入口头、faq:read-only token 各一组。
- 有序动作：创建/列表/详情/删除；跨租户 GET；同用户 operator 入口 GET；重复 DELETE；faq:read-only 创建。
- 预期结果：创建 201 且绑定入口与智能体版本；跨租户在身份层被拒（不可见）；跨入口统一 404；删除后 GET/重复 DELETE 均 404；faq:read-only 401/403。
- 清理：临时目录自动回收。
- 结果：PASS（2026-09-10，pytest `tests/test_conversation_api.py`）。

### CONV-02 活跃订单绑定与省略订单号

- 环境：同 CONV-01。
- 前置条件：授权器允许订单 2096164064667852801。
- 测试数据：订单 follow-up（"我刚才那笔充电订单为什么突然停了"）、知识问题（"会员积分商城什么时候上线呀"）、未授权订单。
- 有序动作：绑定活跃订单（校验归属）；绑定未授权订单；带 conversation_id 提 follow-up 不带订单号；授权器撤销后再提 follow-up；带 conversation_id 提知识问题。
- 预期结果：绑定需归属校验（未授权统一 404）；follow-up 复用活跃订单走 diagnosis（响应带 order_no_from_context）；撤销后清绑定走 qa 且响应 type=qa；知识问题始终 qa 且不触发诊断。
- 清理：同上。
- 结果：PASS（2026-09-10，pytest `tests/test_conversation_api.py`）。

### CONV-03 上下文窗口与保留期

- 环境：本地 ConversationStore（直接驱动）。
- 测试数据：12 个完成轮次（token=100）+ 8 个大轮次（token=2500）+ 会话过期 31 天。
- 有序动作：context_turns 读取；backdate expires_at 后 GET/LIST。
- 预期结果：轮次窗口 ≤8；大轮次下 token 预算先生效（窗口 <8，总数 ≤8k+单轮）；无答案轮次不进入窗口；过期会话不可见。
- 清理：删除临时数据库。
- 结果：PASS（2026-09-10，pytest `tests/test_conversation_api.py`）。

### CONV-04 并发忙与取消语义

- 环境：同 CONV-01。
- 有序动作：begin_turn 占用生成槽后同会话再提问；构造过期 BUSY 锁后新提问；完成轮次后取消一轮。
- 预期结果：占用时同会话 409 CONVERSATION_BUSY（无会话的提问不受影响）；崩溃锁 120s 自过期可重新提问；取消/无答案轮次不保留且释放槽位，不进入后续上下文。
- 清理：同上。
- 结果：PASS（2026-09-10，pytest `tests/test_conversation_api.py`）。

证据边界：CONV-01..04 为 mock-first 协议级验证。真实多设备续聊（前端轮询/刷新）、
BFF 会话透传、停止生成的真实取消链路由 #173 前端票与 P0-E2E-REAL 覆盖；真实
ScopeContext（UPMS/Dis）行为由 T1-T3 既有真实验收线覆盖。

## P0-E2E 本地集成与真实链路状态

### P0-E2E-LOCAL 本地 AI-Ops 集成

- 环境：本地 Python 3.13、FastAPI `TestClient`、临时 SQLite、fake caller、fake 知识库和内存媒体对象。
- 前置条件：T1 媒体协议和 T2 智能体生命周期已合并到同一任务分支。
- 有序动作：运行 T1 媒体协议、T2 生命周期、统一问答 API、QA store、Gateway API 专项测试；再运行全量 pytest、Ruff、格式、compileall、锁文件和依赖检查。
- 预期结果：专项 31 项通过；全量 pytest 554 项通过；所有确定性检查通过。
- 清理：测试使用临时目录，进程退出后删除。
- 结果：PASS（2026-09-10，T1 merge `c8be858` + T2 commit `f564832`/同步提交 `1b73f96`）。

### P0-E2E-REAL 真实客服媒体闭环

- 环境（2026-09-10 业务方范围决定后口径）：公网同域反代 `api.qumall.qushiyun.com/v1/*`（协议侧等价 BFF 透传）+ 接口工具（apifox/curl）+ AI-Ops Gateway + 真实 `kb-service/RAGFlow` + `aiops-canary` 租户 + 含 PNG/MP4 数据集。执行序列见 `docs/agents/p0-media-canary.md`（C1-C7）。
- 前置条件：KB 栈重启与 kb-service 图片端点（人工决策点，见手册 §0）；AI-Ops 网关部署含媒体端点版本并配置 kb/媒体密钥。
- 有序动作：发布客服智能体（真实校验拒绝原因）→ 草稿调试 → 提问 → blocks[] → 图片 200 / 视频 Range 206/416 → 多轮与忙碌 → 无命中/不可用/媒体失效降级 → 监控计数核对。
- 预期结果：QA 返回 `blocks[]`，图片可渲染、视频可播放（Range 生效），文本/引用在媒体失败时保留，越权/TTL/失效拒绝，监控计数吻合且无原文。
- 清理：删临时知识库、停用 canary 智能体、删会话；租户映射复用不删（canary 纪律）。
- 结果：PARTIAL（2026-09-12）。C1/C3/C5 PASS；C4 图片 PASS（新鲜 image QA，GET 200、`image/jpeg`、11469 字节、JPEG 魔数），视频 PASS（新鲜 QA `qa_7ef0906d4f49448d852ba94a366820b1`：整段 `200 video/mp4`、82,348,365 字节，`Range: bytes=0-1023` 为 `206`/1024 字节，超范围为 `416`，伪造签名为 `403`）。C6 无命中与 KB 不可用均 PASS（真实 QA 分别为 `not_found` 与 `unavailable`，均保留文本；停止 `kb-service` 后已恢复 `/healthz=200` 且单元 active）。C7 仍 BLOCKED（缺少真实 `VIEW_ROLES` 管理身份）。视频 Range 证据包含 120 与 36 两层 Nginx 显式透传 `Range` 的热修，配置备份留在主机；不得把 C7 管理成功路径写成已验收。

### P0-FIX-01 媒体错误包、MIME 与空工具请求回归

- 环境：AI-Ops 本地 Python 3.13，临时 SQLite/TestClient，fake kb-service 与脚本化 Codex session。
- 前置条件：媒体签名器、视频/图片授权对象和 QA harness 可用。
- 有序动作：回放 HTTP 200 `code=102` 媒体错误包；回放上游忽略 Range 的完整视频；回放 JPEG 魔数但声明 PNG 的图片；回放空 `tool_requests` 的无命中与依赖不可用场景。
- 预期结果：错误包映射为 `MediaNotFound`；Range 返回正确 206/416 和完整对象范围；响应 MIME 按字节校正；空工具请求不再产生 `QA_FAILED`，而是交付 `not_found`/`unavailable` 文本。
- 清理：释放 fake 与临时 run 目录。
- 结果：PASS（本分支，pytest 相关媒体/QA 测试与全量回归）。

### P0-FIX-02 真实 36 媒体回归

- 环境：移动云 36，公网 `api.qumall.qushiyun.com/v1/*`，真实 kb-service/RAGFlow 与 `aiops-canary` 租户。
- 前置条件：修复版本已部署，视频/图片文档解析状态为 DONE。
- 有序动作：重新提问获取新签名 URL；验证视频整段、`Range: bytes=0-1023`、不可满足 Range、伪造/过期 URL；验证图片字节与 MIME；分别注入 KB 不可用和无命中。
- 预期结果：真实视频 200/206/416、图片 200 且 MIME 与字节一致；错误包不再以 200 媒体返回；无命中/不可用均保留文本并返回对应检索状态。
- 清理：恢复 kb-service、删除临时会话/知识库，保留脱敏日志。
- 结果：PASS（2026-09-12，真实公网链路）。图片真实回源 PASS；视频真实回源 PASS（整段 `200`/有效 MP4，`Range` `206`、超范围 `416`，伪造签名 `403`）；无命中真实降级 PASS；KB 不可用真实注入 QA `qa_f3d5eab75cda46ef8a8039f8bceaa7f3` 返回 `retrieval_status=unavailable`，随后 `kb-service` 恢复并 `/healthz=200` 且单元 active。120 `/v1/` 与 36 `8789` 反代均新增显式 `proxy_set_header Range $http_range`，两处均通过 `nginx -t` 后平滑 reload。C7 管理指标成功路径不在本用例范围内，仍由 METRICS-REAL 阻塞项跟踪。

### P0-FIX-03 视频回源瞬时错误重试

- 环境：AI-Ops `KbServiceClient` 与临时 HTTP 响应 fake。
- 前置条件：视频授权有效；第一次响应为 HTTP 200 + `code=102`，第二次为 MP4 字节。
- 有序动作：调用视频媒体回源一次。
- 预期结果：在重试预算内返回 MP4；永久 404 最终映射为 `MediaNotFound`；图片回源不重试。
- 清理：释放 fake。
- 结果：PASS（本分支，pytest `tests/test_media_api.py`）。

## 后台智能体管理与草稿调试 QA 计划（T5/#171，接口级验收）

> 范围决定（2026-09-10，业务方）：qumall-admin 页面/菜单/角色接线与 Java 管理
> BFF 仓库为范围外；交付口径为 AI-Ops 管理/调试 API 完整闭环 + 接口工具
> （apifox/curl）级验收，公网链路沿用同域反代 `api.qumall.qushiyun.com/v1/*`。

### ADMIN-01 发布前知识库绑定校验

- 环境：本地 Python 3.13，pytest，fake kb-service GET client（按 kb-service/RAGFlow v0.27.1 源码核实的 GET 契约）。
- 前置条件：租户草稿智能体绑定 kb-a。
- 测试数据：文档 run 状态 DONE / RUNNING / FAIL 组、kb 详情 502（不存在/跨租户）、kb 完全不可达各一组。
- 有序动作：分别对四组状态执行发布校验。
- 预期结果：DONE 组通过；RUNNING/UNSTART/SCHEDULE 组拒绝并含"解析中"原因；FAIL 组拒绝并含"解析失败"原因；502 组拒绝并含"不存在"原因；不可达组拒绝并含"不可用"原因（fail closed）。
- 清理：释放 fake。
- 结果：PASS（2026-09-10，pytest `tests/test_agent_debug.py`）。

### ADMIN-02 草稿隔离调试运行

- 环境：同 QA-RAG-01（脚本化 Codex session + fake 检索 + 内存媒体签发器）。
- 前置条件：租户有 customer 草稿（revision=3，绑定 kb-a）；检索命中 PNG 图片分段。
- 有序动作：调用 `run_agent_debug_answer` 提问"怎么拔枪"；读取返回。
- 预期结果：返回 `blocks[]`（text+image）、`retrieval_status`、`debug=true`、`agent_version=agt_…#draft-r3`；不创建任何会话轮次（conversation_store 无写入）；媒体块带本轮签发的 `/v1/media/` URL。
- 清理：释放 fake 与临时 run 目录。
- 结果：PASS（2026-09-10，pytest `tests/test_agent_debug.py`）。

### ADMIN-03 debug-run API 角色与隔离

- 环境：本地 FastAPI TestClient + 临时 SQLite + fake runtime。
- 前置条件：admin（ROLE_AGENT_ADMIN）、viewer（ROLE_AGENT_VIEWER）、跨租户 caller 各一。
- 有序动作：admin 创建草稿并 debug-run；viewer 对同草稿 debug-run；跨租户 caller debug-run；对已发布智能体 debug-run。
- 预期结果：admin 200（含 debug 标记与草稿版本）；viewer 403；跨租户 404（无存在性泄露）；非草稿/非客服 409 AGENT_DEBUG_STATE_INVALID。
- 清理：临时目录自动回收。
- 结果：PASS（2026-09-10，pytest `tests/test_agent_debug.py`）。

### ADMIN-04 生命周期回归

- 环境：既有 agent lifecycle / QA RAG / conversation 测试套件。
- 有序动作：全量 pytest。
- 预期结果：578 基线全过 + 新增 11 项（共 589），既有 create/publish/disable/delete/fork 行为与租户隔离零变化。
- 清理：无。
- 结果：PASS（2026-09-10，全量 pytest 589 项；Ruff、格式、compileall 通过）。

### ADMIN-REAL 真实链路 canary（120 栈恢复后）

- 环境：120 真实 kb-service/RAGFlow + 公网 `api.qumall.qushiyun.com/v1/agents/*`（同域反代）+ apifox/curl。
- 前置条件：KB 栈重启（人工决策点）、canary 租户建临时知识库并上传含图 docx + MP4（素材已在 `docs/知识库材料/`）。
- 有序动作：接口工具走 创建草稿 → 绑定知识库 → debug-run 预览（看 blocks[]/图片/视频块/引用/检索状态）→ 发布 → （发布后重复 debug-run 应 409）→ 停用 → 删除草稿；对解析中知识库发布验证拒绝原因。
- 预期结果：全链路 API 可用；发布校验返回真实原因；debug 媒体 URL 600s 内可用。
- 清理：删除 canary 知识库与智能体（租户映射固定复用 `aiops-canary`）。
- 结果：BLOCKED（2026-09-10）。原因：120 KB 栈停机中（502），重启为人工决策点（docs/agents/kb-service-test-env.md §1/§6.1）；不得把 fake 结果记为业务验收。

## 智能体运行监控与脱敏审计 QA 计划（T7/#174）

### METRICS-01 记录与聚合回读

- 环境：本地 Python 3.13，pytest，临时 SQLite MetricsStore。
- 测试数据：qa 完成（含检索命中/媒体/Token/延迟）、qa 失败、faq 完成、diagnosis 完成、会话忙碌、租户 B 记录各一。
- 有序动作：写入六类行后分别查询本租户汇总、另一租户汇总、按 agent 过滤汇总。
- 预期结果：totals 各计数正确（runs=5、completed=3、failed=1、busy=1、tokens、media）；by_route/by_retrieval/by_error 分桶正确；租户 B 只见自己的 1 条；agent 过滤后 1 条。
- 清理：临时目录自动回收。
- 结果：PASS（2026-09-10，pytest `tests/test_agent_metrics.py`）。

### METRICS-02 枚举与字段校验

- 环境：同上。
- 有序动作：以未知 route_type/outcome/retrieval_status、空租户、小写失败码、负计数分别写入。
- 预期结果：全部拒绝（MetricsValidationError），非法值不落库。
- 结果：PASS（2026-09-10）。

### METRICS-03 脱敏边界

- 环境：同上。
- 有序动作：写入明细后检查行的字段集合与序列化文本。
- 预期结果：行字段固定为枚举/ID/计数/时间，不含 question/answer/prompt/media URL/对象路径；汇总序列化同样不含。
- 结果：PASS（2026-09-10）。

### METRICS-04 保留清理

- 环境：同上，时钟可控。
- 有序动作：写入 31 天前与 1 天前两条，再写一条新记录触发清理。
- 预期结果：31 天前记录被删除，剩 2 条。
- 结果：PASS（2026-09-10）。

### METRICS-05 API 角色与租户隔离

- 环境：FastAPI TestClient + fake runtime（metrics 接缝）。
- 有序动作：admin 查汇总/明细；viewer 查汇总；无 agent 角色查两个端点；跨租户 caller 查询；无 Authorization 查询；非法 route_type 过滤。
- 预期结果：admin/viewer 200；无角色 403；跨租户只见本租户；未认证 401；非法过滤 422。
- 结果：PASS（2026-09-10）。

### METRICS-06 真实运行时集成（失败路径）

- 环境：真实 GatewayRuntime + GatewayStore + MetricsStore（同一 SQLite），monkeypatch 模型函数抛 AgentRuntimeError。
- 有序动作：创建 qa 作业等待终态，读监控明细行。
- 预期结果：作业 failed/QA_FAILED 且指标行 (qa, failed, QA_FAILED) 带 duration_ms；行内无问题原文。
- 结果：PASS（2026-09-10）。
- 说明：取消/超时的完整真实链路（用户停止、模型超时）归 #173/P0-E2E-REAL 真实链路验收；本票在失败码枚举与记录接缝上覆盖其落点（CONVERSATION_BUSY/DIAGNOSIS_BLOCKED/QA_FAILED/KB_UNAVAILABLE）。

### METRICS-REAL 真实环境监控（部署后）

- 环境：36 生产网关 + 公网 `/v1/agent-metrics/*`（120 同域反代）+ apifox。
- 前置条件：#173 真实链路 canary 产生流量。
- 有序动作：走真实 FAQ/QA/诊断提问后查监控汇总与明细。
- 预期结果：计数与真实操作对应，跨租户隔离，脱敏字段核对。
- 结果：BLOCKED（2026-09-12）。#173 真实 canary 已产生公网 FAQ/QA/媒体流量；普通 C
  端会话访问汇总正确返回 `403 AGENT_FORBIDDEN`。管理成功路径仍缺带 `VIEW_ROLES` 的真实
  管理身份，不得以 C 端会话、fake 流量或临时放宽角色替代。

## 41 环境演示数据源评估（只读）

### DATA-41-READONLY

- 环境：41 主机 `47.97.160.153`；经临时 SSH 转发访问 RDS
  `rm-bp1130phekugksn8c8o.mysql.rds.aliyuncs.com`；本地 Python 3.13 + `pymysql`。
- 前置条件：仅使用用户提供的临时凭据；不在 41 安装依赖、不启动/停止服务、不改配置。
- 有序动作：只读检查 `cloud_charging_pile` 的目标表和字段、近期开单聚合、Redis 白名单
  Stream 元数据、TDengine `iot` 稳定表和聚合计数；随后关闭临时转发。
- 预期结果：确认可读性、字段覆盖和数据面缺口；高权限账号不得成为运行时凭据。
- 结果：PASS（2026-09-12T15:47:28+08:00，提交前分支）。MySQL 8.0.28 的
  `cloud_charging_pile` 中订单/费率/设备/站点所需字段完整；最近 500 单覆盖 8 个租户，
  费率记录匹配 494/500；`ch_occupy_order_info` 使用 `order_id/user_id/start_time/
  end_time/refund_remark` 蛇形列，代码已增加固定候选列映射。Redis 两个白名单 Stream
  类型正确，长度分别为 1/0；TDengine `iot` 有 18 个稳定表，`charging-gun_property`
  聚合计数 3,281,797，但 `charging-pile_comm` 不存在。另以不存在的探针订单调用真实
  `MySQLSource.get_occupy_orders`，蛇形列映射成功且返回 0 行。
- 安全结论：`mall@%` 具备多个库的 `ALL PRIVILEGES`，RDS `@@read_only=0`；本轮查询虽
  显式使用只读事务，该账号仍不得写入 AI-Ops 运行配置。需要业务方提供仅对
  `cloud_charging_pile`（及必要 UPMS 库）授予
  `SELECT/SHOW VIEW` 的专用账号，并补齐或明确接受协议报文缺口后，才能评估切换。
- 清理：临时 SSH 转发已关闭；41 主机未发生服务或配置变更。

### CUTOVER-41-01 41 Gateway 与公网入口

- 环境：41 `47.97.160.153`、公网 `https://api.mall.qushiyun.com`。
- 前置条件：`aiops-gateway-41.service` enabled/active；Nginx `/v1/` 指向 41。
- 有序动作：使用无效 `third-session` 调用 FAQ，再用 H5 登录接口获取新会话调用推荐、目录、固定答案和助手问答。
- 预期结果：无效会话为 `401 INVALID_ACCESS_TOKEN`；有效会话的推荐/目录/答案为 `200`，推荐数量为 28；助手自由问答创建为 `202` 并可轮询终态。
- 清理：不保存会话；不创建订单或修改业务数据。
- 结果：PASS（2026-09-12 Asia/Shanghai）。无效会话 `401`；有效会话推荐 `200/28`、目录 `200`、固定答案 `200`；自由问答 `202` 后终态 `failed`（模型/运行依赖失败，非路由 404），订单列表 `200 total=0`。

### CUTOVER-41-02 共享 KB 受限隧道

- 环境：41 `aiops-36-kb-tunnel.service` → 36 `kb-service.service`。
- 前置条件：36 `127.0.0.1:9380/healthz=200`；41 隧道身份为 `aiops-kb-tunnel`。
- 有序动作：检查服务状态、回环监听和 `/healthz`；重启 41 Gateway 后复查。
- 预期结果：41 `127.0.0.1:29380/healthz=200`；服务 enabled/active；目标固定为 36 `127.0.0.1:9380`。
- 清理：保留开机自启隧道；保留 41 配置备份 `/etc/aiops-41/production.env.bak-kb-tunnel-20260912`。
- 结果：PASS（2026-09-12 Asia/Shanghai）。SSH 指纹已核验，健康探针 `200`，41 Gateway 重启后 active。

### CUTOVER-41-03 诊断授权边界

- 环境：41 公网 API；H5 测试账号。
- 前置条件：登录成功但订单列表 `total=0`。
- 有序动作：使用不存在/无权订单创建健康报告。
- 预期结果：`404 ORDER_NOT_FOUND`，不泄露订单存在性，不创建成功报告。
- 清理：无业务写入。
- 结果：PASS（2026-09-12 Asia/Shanghai）。返回 `404 ORDER_NOT_FOUND`；真实诊断成功路径待提供订单所有者会话或授权演示订单。

### CUTOVER-41-04 诊断数据源合规性

- 环境：41 `aiops-gateway-41.service` 的本地 MySQL、Redis、TDengine 配置。
- 前置条件：以 `direct_sources` 执行只读 doctor，不执行写入或消费游标。
- 有序动作：检查 MySQL 授权、Redis ping、TDengine stable 清单。
- 预期结果：MySQL 仅具 `SELECT/SHOW VIEW`；Redis 可达；TDengine 所需超表齐全。
- 清理：无写入。
- 结果：BLOCKED（2026-09-12 Asia/Shanghai）。Redis 可达；TDengine 缺少 `charging-pile_comm`；MySQL `read_only=false` 且含高权限 `ALL PRIVILEGES` 等，不能作为合规运行时账号。待业务方提供最小只读账号并补齐/接受报文缺口。
