# 174. 能力是 run 的类型，订单只是其中一种输入

日期：2026-10-30
状态：已采纳（设计定型，尚未实施）

## 决定

`agent_runs` 增加 `capability` 列，`order_no` 改为可空；**`capability` 是选择回答协议、
工具面与范围推导的唯一判据**，`order_no` 降级为 capability 的一项输入。

```text
capability = cpms_order_diagnosis   inputs={order_no}      scope=order_site
capability = cpms_site_roster       inputs={}              scope=caller_sites
capability = cpms_site_search       inputs={name,address}  scope=caller_sites
```

现有九处「按有没有订单分支」的判断——`operator_cpms.py:129,150,355,375,521,548,572,619,643`——
收敛到这一个判据上。

`operation` 本来就已经承担这件事（`operator_cpms.py:230` 在两条场景之间选择，
`gateway/gateway_api.py:3663` 已经拿它当分支条件）。缺的不是新概念，是把判据收到一处、
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

## 后果与验证

- **迁移需要回填**：`order_no` 非空的行回填 `cpms_order_diagnosis`，空串的行回填
  `operator_conversation`。回填规则必须与今天 `operator_cpms.py:230` 的选择逐行一致，
  这是本决定唯一可机械验证的等价条件。
- **命名要单独决定**：`SCENARIO_OPERATOR_DIAGNOSIS = "operator_order_diagnosis"` 现在同时
  被当作 scenario（`node_publication.py:43`、`chat_route_bindings.py:168` 的字符串比较）
  和 operation（`operator_cpms.py:230`）。改名会同时动绑定键与路由判据，因此**不在本决定内
  顺手改**；P0 只加 `capability` 列，命名留给独立的一次决定。
- **清单身份不受影响**：`operation` 是 run 上的列，不在 `operator-exec-config/1` 清单里，
  清单 identity（原始字节 sha256）不变，已发布版本不需要重发。
- 验收：行为逐字不变，用现有 `tests/operations/test_operator_cpms.py` 与
  `tests/operations/test_operator_conversation.py` 证明等价；另加一条针对回填规则的迁移测试
  （同一批历史行，回填前后走出的协议分支相同）。

方案与分期见 [工具架构](../agents/operator-cpms/tool-architecture.md)。
