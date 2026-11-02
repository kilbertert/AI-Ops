# 费用字段与订单识别的接续验证（2026-11-02）

分支 `fix/fee-fields-followup`，基线 `cf42ae46`（#1305 + #1307 已合并）。
本轮只做三件事：**把订单提取的两个真缺陷修掉、把 #1307 在生产上的失败如实回滚、
补上失败取证**。费用字段的地基（#1305）不改。

> **本文不声称「答复成功率已修好」。** #1307 那条路在生产上被证明更差，已回滚；
> 稳定性仍未解决，下一跳需要先有真实失败证据。

## 一、订单提取：两个真缺陷（Codex 在 `099c1207` 上报）

本地复现，两条都成立：

```
'订单 A2116050516255272961'     → 2116050516255272961   ← 字母前缀被吃掉
'订单 ORDER-1001 和订单2116…'   → 2116050516255272961   ← 两张单，静默只查后者
```

两条的根因是同一个：**门槛看形状，提取取第一个含数字的 token，两处各写一份**。
所以 `订单 A…` 被当作「纯数字段」而不是标识符；而有短单号的那一张根本进不了候选集。

### 修法

`common/order_reference.order_candidates()` 成为**唯一**候选集来源，正式入口
（`gateway_api`）与原生预览桥（`dify_console_publish._native_order`）都只调它。

| 情形 | 结果 |
|---|---|
| `订单 A2116…` | `A2116050516255272961`（保留前缀） |
| `订单 ORDER-1001 和订单2116…` | 两个候选 → 正式入口返回 `CLARIFY_ORDER_REQUIRED`，**不问 CPMS** |
| `订单2116… 这张单2116…` | 同一张，去重后一个候选 |
| `订单 2115…1 2115…2` | 两个候选（第二张没有「订单」前缀，但同形状） |
| `帮我分析这笔单的电费2116…` | `2116050516255272961` |
| `TEST2026-10-29 03:00:09` / 手机号 | 空（形状判据不变） |

归并规则里有一处不显然：`订单 A2116…` 里**同时**有显式标识符和一段 15 位以上裸数字，
那是同一个号的后半截。所以并入裸数字段时按**数字内容**去重，否则同一张单会被
自己算成两张、整条请求被拒。

### 验证

```
tests/common/test_order_reference.py::test_a_letter_prefixed_identifier_keeps_its_prefix
tests/common/test_order_reference.py::test_two_different_orders_in_one_message_are_not_silently_reduced_to_one
tests/common/test_order_reference.py::test_the_same_order_repeated_is_one_order
tests/operations/test_operator_cpms.py::test_a_letter_prefixed_order_reads_that_exact_identifier
tests/operations/test_operator_cpms.py::test_two_different_orders_are_asked_back_not_silently_reduced_to_one
tests/operations/test_operator_cpms.py::test_the_same_order_written_twice_is_still_one_order
```

后三条走**真实入口** `POST /v1/assistant/questions`，断言的是「真去读了哪一张单」
（`/chOrderInfo/getOrderInfoDetail/<单号>`），不是正则返回值。

**变异验证**：把 `order_candidates` 的显式分支清空 → 7 条用例失败。

## 二、#1307：生产实测更差，已回滚

### 做了什么

按指示把输出契约真正发上生产：节点清单 v3 = `business_prompt(v2) + 当前 OPERATOR_OUTPUT`，
路由指针到 v3 / revision 14。

**产物正确性**：重新序列化后与库里 v3 **逐字节相同**，且能被 `execution_manifest.from_text`
解析。也就是说问题不在「发的是不是对的那份文本」。

### 实测

| 契约 | 次数 | 结果 |
|---|---|---|
| v2（旧） | 30 | 13 成功 / 15 失败 |
| v3（#1307） | 8 | **0 成功 / 8 失败** |

连败 8 次的概率（按旧契约 43%）约 1.1%；拒绝原因的种类也变了 —— 出现旧契约没有的
`reason=shape` 与 `OPERATOR_EVIDENCE_MISSING`。

已回滚到 v2 / revision 15（`updated_by=rollback-v3-to-v2`），同一句话立刻恢复 `completed`。

### 回滚后的基线（同一句话）

| 次 | 结果 | reason |
|---|---|---|
| 1 | failed | `json_parse` |
| 2 | failed | `json_parse` |
| 3 | failed | `unsupported_fact` |
| 4 | succeeded | — |

仍约 1/4，与 #1305 部署时的量级一致。**#1307 没有解决稳定性，production 回到了原状。**

## 三、失败取证：此前根本取不到

实测确认：**失败的 run 不保存模型原文**，`snapshot_json.answer` 恒为 `None`。
所以过去所有「`unsupported_fact` 是什么原因」的判断都只能靠猜，而这条码同时覆盖
「事实段外写了金额 / 写了状态 / 写了支付结果」三种完全不同的成因。

本轮在拒绝点补了取证：

```
operator_answer_rejected reason=unsupported_fact detail=notice line=3 len=21
```

记的是**判定事实**（哪条规则、第几行、该行长度），**不记原文、不记金额** ——
日志没有脱敏层，而那一行正是最容易带出金额的地方。用例同时断言这两面：

```
test_a_fact_rejection_says_which_line_and_which_rule
  ✅ "detail=notice" / "line=3" 在日志里
  ✅ "1.00" 与整句原文都不在日志里
```

## 四、费用字段在真实响应上的核对

从真实订单的 `agent_runs.snapshot_json.evidence.cpms_order_detail` 读**真实 CPMS 响应**：

| 字段 | 真实值 | 后台那一列 | 处理 |
|---|---|---|---|
| `vat` | `2.0` | 增值税率 2% | **本轮改为带 `%`**；`2.0` 会被读成金额 |
| `vatFee` | `0.16` | 增值税费用 | 金额，原样 |
| `launchFee` | `1.0` | 启动费用 | 金额，原样 |
| `dsElectricFee` / `dsServiceFee` | `0.0` / `0.0` | — | **零值照写 0** |
| `occupyFee` | `null` | — | **不适用，不出行** |
| `payTime` | `2026-10-30 14:12:13` | 付款时间 | 已是可读时间 |
| `marketAmount` | `0.0` | 优惠金额 | 零值照写 |

三种空值语义（缺值 / 零值 / 不适用）在这**一张真实订单**上同时出现，渲染已分别处理。

**币种没有来源**：`getOrderInfoDetail` 不返回币种，答复里不写币种，末尾保留
「币种和时长单位需与原订单核对」。不猜一个「元」。

## 五、结算状态：确认是接口缺口

真实订单的证据里共 34 个字段，**没有任何 settlement 字段**。不是我们没取 ——
`evidence_fields` 是从真实响应挑的。要覆盖得新增一次跨服务查询（充电订单 → 商城结算单），
带新的授权面与失败模式。**本轮没有做，也没有绕过授权。** 答复里不会出现结算状态，
也不会用充电状态冒充。

## 六、未完成 / 未证明

- **稳定性没修好。** #1307 的改法证伪并回滚；`unsupported_fact` 的真实成因要等下一次
  线上失败带 `detail=` 的日志才能定位。
- **20 次真实调用验收没做**：要先有稳定的契约，否则测的是噪声。
- **新契约尚未经受支持的发布链重发**：v3 是手建的（虽与发布链产物逐字节相同）。
  下一跳要补一条能走 `node_publication.publish` 的正规路径。
- 订单提取的修复**尚未部署**到 41。
