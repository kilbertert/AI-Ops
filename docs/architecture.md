# 系统架构

## 确定性快速路径

```mermaid
flowchart LR
    U[工程师反馈] --> P[输入解析]
    P --> E[诊断引擎]
    E --> H[/diag/* HTTP 只读接口]
    E --> T[TDengine 有界查询]
    H --> Q[证据与规则]
    T --> Q
    Q --> O[终端与 JSON 报告]
```

面向自然语言的层只提取订单号和问题意图，不能提交 SQL、选择任意表或调用业务动作。诊断引擎掌握确定性 runbook，并调用固定的只读适配器。

## Codex-native Thin Harness

第一版 agent 路径反转了决策边界：持久 Codex thread 请求受限的证据工具，读取运行目录中暂存的 SOP 和业务参考，负责选择假设并给出因果解释。harness 只保留不可变身份、只读上限、证据哈希、脱敏、置信度上限、结果验证和恢复状态。详见 [thin-harness.md](thin-harness.md)。

运行过程由统一事件 sink 记录到私有 `events.jsonl`，并可通过进度回调实时送给
终端或上层服务。事件 sink 不携带证据 payload 或数据库凭据；证据正文只进入
带哈希的 evidence artifact。JSON 调用的 stdout 保持机器合同不变，进度走 stderr。

## 基于后端的业务规则

初始规则来自用户提供的后端快照：

- 订单状态：`CommonConstant` 和 `ChOrderInfo` 定义状态 2 为不可控异常、3 为异常已处理、5 为设备已上报但异常结束。
- 交易匹配：OCPP 使用 `transaction_id`；AYK、YKC 和其他非 OCPP 协议使用订单号，符合后端事件查询路径。
- 计费侧：`FourPriceComputeComponent` 对 OCPP 和 AYK 使用服务端计费，其他协议使用桩端计费。
- operator 订单：`launch_type=operator` 绕过标准停止充电计费流程，直接用 `tx_data.totalFee` 检查订单总额，不套用费率模板和标准结算检查。
- 桩端计费：平台电费根据分时电量和费率模板计算；服务费为 `max(tx_data.totalFee - electricity_fee, 0)`。
- 订单总额：`ChOrderInfo.collectFee()` 汇总电费、服务费、启动费、停车费、电损电费和电损服务费。
- YKC 异常结束：64-73 视为正常结束，其他设备上报码通常标记为状态 5。
- 交易完整性：可解析的 `tx_data` 视为已收到证据，即使独立接收标志不一致；充电订单结束前不强制要求交易结束数据。
- 两轮车订单：当前版本标记为不支持并降低置信度，不套用四轮车计费和枪时序规则。
- 置信度：证据源失败、跳过或金额快照缺失都会降低置信度，不能用残缺证据链报告高确定性。

## 部署边界

项目可以直接运行在权限受限的诊断主机上，也可以通过 OpenSSH 本地转发访问 TDengine 严格只读代理。刻意不支持 SSH 密码自动化；生产环境必须使用专用账号和 key，并限制可转发目标。

**两个源集合并存，各自服务一条入口路径**：设备运行路径（`POST /v1/runs`）不携带范围对象，订单、费用、设备和 Redis 队列证据经充电桩 `/diag/*` HTTP 接口读取（`HybridSources`），租户由工具层按行后过滤；标准 API 面（`/v1/standard/diagnoses`，调用者身份已解析出租户）携带冻结的 `QueryScope`，走受限直连（`ScopedSources`），租户以参数绑定的 SQL 谓词下推，本仓为此配置最小只读的 MySQL / Redis 账号。并存的根因是设备路径没有 SQL 可下推，而不是两套规则：租户可见性只在 `order_visibility.py` 定义一次，行级过滤与 SQL 谓词是同一定义的两种渲染，两个入口因此必须给出一致结论。

多端便携部署时，推荐将上述数据库访问边界放入固定服务器上的 `AI-Ops Gateway`，客户端只通过 HTTPS 设备令牌提交诊断请求和同步事件。Gateway 方案、注册、秘密管理和跨设备 run 同步见 [gateway.md](gateway.md)。

AFK 开发自动化使用独立的可信控制面：`pull_request_target` 只接受仓库所有者创建的同仓库 PR；宿主依赖、Sandcastle controller 和 policy checker 来自当前 `main`，候选 checkout 只挂载到带只读 GitHub token 的 Docker 沙箱。变更结果通过 Git bundle 交给干净 delivery checkout，并在推送前校验远端 head 未发生竞态。`AGENT_PAT` 只出现在标签、评论和最终推送步骤，缺失或失败时进入 `agent:blocked`。

TDengine Community Edition 3.4 不支持 `GRANT READ`，非超级用户仍可能写入已有数据库。因此生产请求必须经过 `ops/` 中的 loopback-only 代理。代理只识别 `TDengineSource` 发出的精确有界查询，SSH 账号不能直接转发到 TDengine 原生 REST 端口。

## 必需的生产身份

- `/diag/*` HTTP 内部令牌：只允许访问已实现的白名单诊断端点，并在 300 秒窗口内完成验签。
- TDengine 代理后端账号：仅限 localhost，`CREATEDB 0`、`SYSINFO 0`；由于 Community Edition 缺少数据库级只读授权，具备写能力的凭据只能由严格只读代理持有。
- SSH 账号：无 shell 管理权限，Phase 3a 后只能转发到 TDengine 只读代理端点。

## 参考资料边界

开发环境可以从被 `.gitignore` 忽略的后端快照读取原始 Java 依据；公开便携包不包含该私有源码，只携带维护后的 SOP、架构说明、`engine.py`、`rules.py` 和合成 fixture。
