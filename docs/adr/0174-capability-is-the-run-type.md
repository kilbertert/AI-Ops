# 174. 能力是 run 的类型，订单只是其中一种输入

日期：2026-10-30
状态：已采纳（设计定型，已实施；**本条曾经写错两个子项，2026-10-30 修订，见「修订」**）

## 决定

运行类型由 `agent_runs.operation` 承担 —— 它本来就承担着这件事；**不新增 `capability` 列**，
也**不改 `order_no` 的可空性**。`operation` 是选择回答协议、工具面与范围推导的唯一判据，
`order_no` 降级为该类型的一项输入。

```text
operation = operator_order_diagnosis   inputs={order_no}      scope=order_site
operation = operator_conversation      inputs={}              scope=caller_sites
operation = <今后的能力>                inputs={...}            scope=<该能力的推导>
```

现有九处「按有没有订单分支」的判断（`operator_cpms.py` 的 `refresh_scope` 调用点、
`authorize_readback`、`publication` 的领取、`_current` 与 `result` 各一份的清单选择、
`complete` 的协议选择与快照、`_worker` 的派发提示、`result` 的渲染）收敛到
一个谓词 `diagnoses_an_order(run)` 上。

`operation` 本来就已经承担这件事（`operator_cpms.py` 签发时在两条场景之间选择，
`gateway/gateway_api.py` 已经拿它当分支条件）。缺的不是新概念，是把判据收到一处、
把「有没有订单」从判据降级为输入。

## 依据

今天「管家」这一个应用绑定下混着两种任务，它们的差别远不止订单：回答协议不同
（五字段 `direct_answer/summary/...` 对四字段 `kind/text/name/address_terms`）、工具面不同
（两个受控工具对零个工具）、授权范围来源不同（订单所在站点对账号站点集合）。
这些差别现在全部由「`run.order_no` 是否为空」间接表达。

代价是可测的：**「无订单、但要调用工具」这种组合在类型上写不出来**。它是站点查询
进不了工具面的直接原因，也是下一个不需要订单的能力（设备故障记录、站点负荷）会撞上的
同一堵墙。要么承认工具面永久只能在订单诊断里存在，要么把类型补上。

## 被否掉的替代形态

**保持 `order_no NOT NULL`，用哨兵值承载无订单 run。** 最强的地方是它最省：不改 schema、
不做回填、不动任何一处判据，一天之内就能让站点带上工具。它输在哨兵值会顺着授权一路走下去——
节点会拿它去请求 `getOrderInfoDetail/<哨兵>`，网关会逐行核对 `siteId`，证据里会写上这个单号。
整条证据链于是变成一份格式正确、内容为假的取证。这正好是 ADR-0165 明确否掉的
「随便选一张订单承载普通问题」的同一个形状，换个实现方式不改变它的性质。

**`order_no` 改可空，但保留九处 `if run.order_no` 分支。** 最强的地方是迁移面最小：一次
`ALTER TABLE` 就够，其余代码一行不动。它输在两套判据并存——某处改了、另一处没改，
结果是协议与工具面错配：一份按四字段协议校验的报告撞上要求五字段的校验器。这类错配不会
在写入时报错，会以「模型输出不合规」的形状出现在业务侧。

**每个 capability 一张新表。** 最强的地方是类型完全显式，谁也不会把两种任务混起来读。
它输在 run 的生命周期只有一份实现——签发、TTL、`claim`、`complete`、`complete_loop`、
回读、取消、`_current` 的复核，拆表等于把这些状态机复制 N 份，而它们的正确性靠的是
「只有一处能改终态」这条性质。

## 修订（2026-10-30，实施时发现两处写错）

初版写的「增加 `capability` 列，`order_no` 改为可空」在落地时被两条证据推翻：

1. **`operation` 已经是运行类型，再加一列就是同一事实的第二份真源。** 它已经被
   `gateway_api` 用作分支条件、被 `operator_cpms` 用作两处成员校验，签发时也是按它
   二选一。新增一个与它平行的列，只会让「哪一个是判据」重新变成问题。
2. **`order_no` 不改为可空。** SQLite 放松 `NOT NULL` 要重建整张表，而这是现役运行的
   记录。仓库里已经对**同一个形状**做过这个判断：标准诊断表的 `order_no` 同样绑空串、
   用 `kind` 区分类别，理由写在 `gateway_store.start_analysis_job` 里——「重建会把每一行
   既有诊断记录置于风险中，换来的只是一个『看起来更对』」。那张分析作业单就是活证。

改后的做法比初版**更小**：不增列、不改 schema，只把九处判据收成一个谓词。
「无订单」这个状态仍然被完整表达——由 `operation` 表达，不再由订单号是否为空兼职。

## 后果与验证

- **没有迁移，也没有回填**：不增列、不改约束，历史行一个字节不动。这是修订后最直接的
  收益——初版要回填 9 处判据对应的历史语义，现在一行数据都不用碰。
- **命名要单独决定**：`SCENARIO_OPERATOR_DIAGNOSIS = "operator_order_diagnosis"` 现在同时
  被当作 scenario（`node_publication.py:43`、`chat_route_bindings.py:168` 的字符串比较）
  和 operation。改名会同时动绑定键与路由判据，因此**不在本决定内随手改**，留给独立的一次决定。
- **清单身份不受影响**：`operation` 是 run 上的列，不在 `operator-exec-config/1` 清单里，
  清单 identity（原始字节 sha256）不变，已发布版本不需要重发。
- 验收：行为逐字不变，用现有 `tests/operations/test_operator_cpms.py` 与
  `tests/operations/test_operator_conversation.py` 证明等价；另一条测试专杀「拿订单号当判据」
  这个变异——运行类型是无订单回合但订单号非空、以及反过来，两行都必须变红。

方案与分期见 [工具架构](../agents/operator-cpms/tool-architecture.md)。
