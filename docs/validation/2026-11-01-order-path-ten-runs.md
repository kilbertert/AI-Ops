# 订单链路真实会话验收：10/10 通过（2026-11-01）

- 交付分支：`docs/order-path-acceptance`（记录本条目）；代码基线 `6a2c6e43`（#1298）
- 网关版本：`1.0.0+6a2c6e43b9cd`
- Dify 已发布版本：`agent_version_no=14`（#1298 的协议块经草稿迁移 → `/v1/dify/publish` 发布后才生效）

## 做了什么

1. 合并并部署 `#1298`（协议块补"只引用本轮实际给出的段"）。
2. **迁移生产 Dify 工作流**：离线用 `upgrade_consumer_flow.py --live-documents` 生成候选，
   逐项比对后只把 `answer` 节点的 `prompt_template` 写入草稿，再走 `/v1/dify/publish`。
   比对结果：环境项、`language` 选项、`memory.query_prompt_template`、`disabled.answer`
   与其余 11 个节点**全部不变**；运营文字 837 字符逐字未动；协议块只新增两句。
3. 用验收账号重新登录真实 H5，取新会话后连续查询同一订单。

## 证据

连续 10 次，同一真实用户会话、同一真实订单 `2109928861763776513`：

| # | 时间 (UTC) | run_id | 终态 | version | reply_source |
|---|---|---|---|---|---|
| 1 | 17:22:26 | `service_66be8762ca3447c0aa74bbf4c2df4dcc` | succeeded | 14 | `{{fact:status_line}}…` |
| 2 | 17:22:39 | `service_ace369b3bacb4f8883a8135d81d02f30` | succeeded | 14 | 同上 |
| 3 | 17:22:51 | `service_42908eb8c7264074bd06fe62f949f332` | succeeded | 14 | 同上 |
| 4 | 17:23:03 | `service_3639064cf83e4c0cb42fec11423a8cc7` | succeeded | 14 | 同上 |
| 5 | 17:23:15 | `service_4eab1b44cf694d968015f59cf4ebd4ca` | succeeded | 14 | 同上 |
| 6 | 17:23:28 | `service_f7e8a516ab224236bd1cc4fcb81f5dab` | succeeded | 14 | 同上 |
| 7 | 17:23:40 | `service_7b9bf3ac65e84fcf925472a463eb5524` | succeeded | 14 | 同上 |
| 8 | 17:23:51 | `service_11f48381dd4a4d1382e1aaa42cc115af` | succeeded | 14 | 同上 |
| 9 | 17:24:05 | `service_5eee2ad2ae1e47dd9ad8631447dc9a1d` | succeeded | 14 | 同上 |
| 10 | 17:24:16 | `service_7233a2a54c3a4283a702b4686c0ae354` | succeeded | 14 | 同上 |

`reason` / `reject_detail`：10/10 为空。

第 10 次（`service_7233a2a54c3a4283a702b4686c0ae354`）的答复正文，从该行的
`snapshot_json` 读出，**属于上表窗口内**：

```
这单当前的状态是「充电结束」。

订单号：2109928861763776513
查询于：2026-11-01 17:24:17 UTC

这只是我查到的业务状态，不代表故障原因。
来源：充电状态记录
```

不复现方式：`chat_dify_runs` 按 `created_at` 过滤 `2026-11-01T17:22`–`T17:24`，
取 `state` / `agent_version_no` / `reply_source` / `reason` / `reject_detail` 五列。

## 对比基线（改前）

| 版本 | 成功 | 失败 | 失败形态 |
|---|---|---|---|
| `56995d3d`（#1294） | 6/9 | 3 | 全部 `CHATFLOW_FACT_UNKNOWN`（引用了不存在的 `billing_line`） |
| `56995d3d` 更早 | — | — | `CHATFLOW_ANSWER_INVALID (json_syntax)`：JSON 后多 `"}` |

两种形态都已消失。**但 10 次不是统计意义上的稳定性证明**，只能说这段时间内未复现。

## 仍未完成

- **业务验收未完成**：41 原应用 / 原 H5 的正式验收整体仍未全部通过，
  本次只覆盖"订单实时状态查询"这一条链路。
- 账单查询（`data_kind=billing`）未在本次连续样本中覆盖。
- 知识类问答未在本次样本中覆盖。
- 本次窗口之后（17:24:44）另有一次查询，**不计入**上表。
