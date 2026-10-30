# 工具架构：能力、授权与参数

**状态**：提案（2026-10-30）。本文只描述方案，不含代码改动。四项决定分别记录在
[ADR-0174](../../adr/0174-capability-is-the-run-type.md)、
[ADR-0175](../../adr/0175-tool-grant-carries-the-request.md)、
[ADR-0176](../../adr/0176-tool-arguments-gated-against-the-question.md)、
[ADR-0177](../../adr/0177-site-query-has-one-implementation-three-entries.md)。

## 0. 要解决的问题

站点查询（`GET /chSite/page`）在管家对话里已经可用，但：

1. 它不是 `cpms_order_detail` / `cpms_charging_samples` 那样的工具；
2. 没有对用户端（`consumer` 入口）开放的只读接口。

上一轮的解释把原因归到「ADR-0165 有意不给工具」。那个解释不完整。真正的原因在工具机制
本身：**「这次 run 有订单」被当作「这次 run 是什么任务」**。只要这条等式存在，任何不需要
订单的能力都进不了工具面 —— 站点只是第一个撞上它的能力，设备故障记录、站点负荷、
电表读数会依次撞上同一堵墙。

本文记录：耦合点在哪（§2）、根因是什么（§3）、目标形态（§4）、分期与每期的等价证明（§5）。

## 1. 现状：一次工具调用经过八层

| # | 层 | 代码 | 现在的职责 |
| --- | --- | --- | --- |
| 1 | 发布 | `execution/node_publication.py:176-206` | 从 Dify DSL 的勾选结果生成 `operator-exec-config/1` 清单；工具白名单在这里；**强制 `input_schema` 为空对象** |
| 2 | 登记 | `operator/ops_provider.py:14-55` | 把工具注册成 Dify `cpms-node` provider；已存在且 schema 不同时抛错，不自动修改 |
| 3 | 签发 | `execution/agent_runs.py:462-505` | 建 run 行：`order_no`、`scope_json`、`chat_binding`、`execution_manifest`、TTL |
| 4 | 派发 | 平台（Snail）→ 节点 | `sid` 即 `run_id`，`GovernedToolRuntime.resolve()` 按清单装配工具 |
| 5 | 授权 | `operator/operator_cpms.py:384-399` | 给节点发 Grant：`site_ids=[order_site_id]`、`order_no`、`base_url`、`bearer` |
| 6 | 执行 | `CpmsReadTool.java:88-103` | 节点**自己**把 CPMS 路径从 `grant.orderNo()` 拼出来并发请求 |
| 7 | 代理 | `operator/operator_cpms.py:401-431` | 只放行 `path == 前缀 + run.order_no`，逐行核对租户与站点 |
| 8 | 收证 | `operator/operator_cpms.py:449-490` | 证据字段集合必须**精确等于**给定集合，且 `order_no` 与 run 相同 |

## 2. 耦合清单

| # | 载荷点 | 位置 | 现在依赖什么 | 目标 |
| --- | --- | --- | --- | --- |
| C1 | `agent_runs.order_no TEXT NOT NULL` | `agent_runs.py:411` | 无订单的 run 没有地方存放 | 可空 + 新增 `capability` 列 |
| C2 | 九处 `if run.order_no` | `operator_cpms.py:129,150,355,375,521,548,572,619,643` | 用「有没有订单」选协议、选范围、选渲染 | 收成 `run.operation`（`capability`）单一判据 |
| C3 | Grant 的 `site_ids=[order_site_id]` | `operator_cpms.py:394` | 授权范围恒等于「订单所在的那一个站点」 | scope 推导策略：`order_site` / `caller_sites` |
| C4 | Grant 的 `order_no` 必填 | `CpmsReadTool.java:40-60` | 授权载荷的形状 | `request{method,path,query}` + `inputs` |
| C5 | 节点拼 CPMS 路径 | `CpmsReadTool.java:95-96` | 工具名→路径的映射在 Java 里 | 授权给什么请求就发什么请求 |
| C6 | 代理只认 `前缀+orderNo` | `operator_cpms.py:408`、`operator_cpms.py:446` | 请求白名单以订单号为核心 | 比对完整请求目标 |
| C7 | 证据字段集精确相等且含 `order_no` | `operator_cpms.py:463-467`、`CpmsReadTool.java:100` | 双端必须同版部署 | 改成 `inputs`（仍是精确相等，仍需同版） |
| C8 | 工具白名单写了五遍 | `chat_execution.py:73`、`operator_cpms.py:27`、`CpmsReadTool.java:23`、`ops_provider.py:14`、`node_publication.py:176` | 加一个工具要改五处，其中一处是 Java | 单一声明源 + CI 漂移检查 |
| C9 | 强制 `input_schema` 为空对象 | `node_publication.py:205-210` | 模型不能给工具任何参数 | 带参数的工具需要一条新的参数放行判据 |

C1–C3 是「订单绑在能力上」，C4–C7 是「订单绑在授权载荷上」，C8–C9 是「工具定义没有单一事实源」。
三组是三个独立的决定，因此分成三条 ADR，而不是一条大重构。

## 3. 根因：三个词被当成一个

现在有三个概念，代码里只用到两个：

| 概念 | 含义 | 今天的存储位置 |
| --- | --- | --- |
| **scenario** | 哪个应用绑定到哪个节点（`环境:租户:入口:场景`） | `ChatBinding.scenario`，由 `node_publication.py:43` 按入口推出 |
| **operation** | 这一次 run 是什么任务 | `agent_runs.operation`，由 `operator_cpms.py:230` 按**有没有订单**在两条之间选 |
| **capability** | 这一次 run 允许做哪几件事、用哪个回答协议 | **不存在** |

后果是「管家」这一个 scenario 下混着两种 operation（订单诊断 / 无订单对话），而它们的
差别远不止订单：回答协议不同（五字段 vs 四字段）、工具面不同（两个工具 vs 零个工具）、
授权范围来源不同（订单站点 vs 账号站点集合）。这些差别现在是**用「有没有订单」间接表达的**，
所以「无订单但要用工具」这种组合在类型上根本写不出来。

`operation` 已经是 capability 的存储位置 —— 它已经在 `gateway/gateway_api.py:3663` 被当作判据使用。
缺的不是新概念，是把这个判据收敛到一处、并把「有没有订单」从判据降级为输入。

## 4. 目标形态

### 4.1 run 有显式能力，订单是其中一种输入（ADR-0174）

```text
agent_runs:  operation(=capability)  inputs_json  order_no NULLABLE
```

```text
capability = cpms_order_diagnosis   inputs={order_no}  scope=order_site     answer=cpms-diagnosis
capability = cpms_site_roster       inputs={}          scope=caller_sites   answer=conversation
capability = cpms_site_search       inputs={name,address}  scope=caller_sites  answer=conversation
```

规则只有两条：**协议由 capability 选**；**`if run.order_no` 不再出现在判据里**。

### 4.2 授权装请求目标，不装订单号（ADR-0175）

```text
Grant = { tenant_id, site_ids, request:{method,path,query}, inputs, base_url, bearer, expires_at }
```

节点不再拼路径，只执行授权给的那个请求；代理比对完整请求目标。做完这条，**新增一个
CPMS 只读工具不再需要改 Java** —— 这是「工具架构调整机制」该有的形状，否则每次调整都
伴随一次节点发版。

### 4.3 一份工具声明（ADR-0175 的产物）

现在同一件事写了五遍（C8）。目标是一份进仓的声明，Python 侧的 `PATHS` / `DETAIL_FIELDS` /
`node_publication` 白名单 / `ops_provider` 的 openapi，与 Java 侧的工具装配，都从它生成或
被它校验。仓库里已有 `tools/experiments/m2_close/m3node/manifest_parity.py` 用同一组样本比对两端解析，
把工具定义纳入同一套比对是现成模式。

### 4.4 参数由网关对照提问原文放行（ADR-0176）

带参数的工具会打破现在「`input_schema` 必须是空对象」这条边。补偿不是「相信模型」，而是
把 ADR-0165 已经验证过的那条判据搬到授权处：**每个参数必须是用户提问原文的字面子串**
（长度 1–80、不含 `%_\` 与换行）。模型可以提议参数，能不能用由网关对照原文决定。

## 5. 分期

| 阶段 | 改动面 | 等价证明 | 不做会怎样 |
| --- | --- | --- | --- |
| **P0 能力轴** | `agent_runs` 加 `capability`（回填：有单 → `cpms_order_diagnosis`，无单 → `operator_conversation`）、`order_no` 改可空、九处判据收敛 | 行为逐字不变，现有 `tests/operations/test_operator_cpms.py` 与 `tests/operations/test_operator_conversation.py` 即可证明等价 | 站点永远进不了工具面 |
| **P1 请求进授权** | Gateway + Java **一次配套发版**：Grant 换载荷、节点删路径硬编码、代理比对完整目标、证据改 `inputs` | `NodeLinkAcceptance` + `tools/experiments/m2_close/m3node/verify_operator_cpms.py`；C7 决定两端必须同版，不能灰度混跑 | 每加一个工具都要改 Java 并发版 |
| **P2 声明源** | 一份能力声明 + 两侧生成 + CI 漂移检查；Dify provider 的「schema 不同就抛错」补一条显式迁移命令 | `manifest_parity` 扩成工具级；同一份声明生成的两侧产物逐字节相同 | 五处白名单继续各自漂移 |
| **P3 站点工具** | 只加声明行，零 Java 改动 | 新增工具不改 `services/execution-node` | — |

P0–P2 都不新增业务能力，`cpms_order_detail` / `cpms_charging_samples` 的对外行为不变；
P3 才是把查站点变成工具。

## 6. 站点能力的三个入口

站点查询要用三处，共用**同一份** scope 推导（`operator_cpms.query_sites` 里逐行核对
`tenantId` 与站点集合、每页 50 最多 4 页、员工账号的 `total` 是租户口径这个已知坑）：

| 入口 | 形态 | 现状 |
| --- | --- | --- |
| 对话回合 | `kind=site_search`，四字段协议 | 已上线（ADR-0165 / 0172） |
| 节点工具 | `cpms_site_search(name,address)` | 未实现，需 P1–P3 |
| 用户端只读 API | `GET /v1/sites` | 未实现，见 ADR-0177 |

**`GET /v1/sites` 的 C 端口径尚未决定，实现不能先于这个决定。** 现状是：C 端调用者的
`DataScope` 是 `self`，`resolve_query_scope` 对 `self` 返回 `site_ids=None`
（`identity/query_scope.py:273-281`），而在 `query_sites` 里 `None` 表示**不过滤**。
直接复用等于把整租户站点名单交给任何 C 端用户。三个候选（公开目录 / 与本人订单有关联的
站点 / 不向 C 端开放）各有不同的权限含义，需要单独一条决定，不能顺手实现。

## 7. 验收

本轮（提案）**无代码改动，无自动化检查新增**，因此不存在可以声称的验证结果。各期落地时
的验收方式已在 §5 表格里逐条写明；P1 因为两端证据字段集必须精确相等，验收必须包含
「旧节点 + 新网关」与「新节点 + 旧网关」两种混跑被明确拒绝的证据，而不是只测同版通过。

## 8. 明确不做

- 不把模型给的参数当作授权（ADR-0091、契约 §3.4 的边界不变）。
- 不为无订单的 run 编造一张订单承载（ADR-0165 已否掉的形态，理由不变）。
- 不让网关代替节点持有并发送 CPMS 令牌：那会改变已验收的边界，公司令牌会更多留在
  Gateway 进程里（ADR-0175 的被否方案）。
- 不给模型任意 HTTP / SQL 工具。
- 不因为站点能查就放开距离排序、实时价格或空闲枪口：这些接口本轮没有取证。

## 9. 风险与未验证

1. **P1 是破坏性的双端变更。** 证据字段集现在是精确相等比对（`operator_cpms.py:463`），
   改字段意味着两端必须同版部署，没有灰度窗口。部署顺序与回滚点必须在 P1 的任务里
   单独写清楚，不能沿用「先网关后节点」的默认顺序。
2. **C 端口径未定**（§6），`GET /v1/sites` 的鉴权与可见范围依赖那条决定。
3. **参数放行的强度未经真实语料检验。** 「必须是提问原文的子串」在 ADR-0165 里对模型输出
   成立，但当时参数集合固定为 `name` + 最多 4 个地址词；变成工具参数后模型可以连续调用，
   多轮组合是否有新的绕过面，未验证。
4. **站点列举的 `total` 口径**是已知事实错误风险（ADR-0172 已记录），换个入口不会自动消失，
   三个入口都要带上同样的 `scope_limited` 语义。
