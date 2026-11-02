# 消费者端四项验收结果（2026-11-02 最终）

> 本文是 [2026-11-02-consumer-three-acceptance.md](2026-11-02-consumer-three-acceptance.md)
> 的**后继结果**：那份记录的是修复前的状态（账单未通过），本文记录修复并重发之后的结果。
> 两份都要保留——失败样本与修复证据同样重要。

# 消费者端三项补充验收（2026-11-02 晚）

设备：rl-1 USB → NAM-AL00 / Android 12，`com.trend.ulink` 1.2.15，验收账号 phone+fixed-code 正常登录。

调用者主体（从 scope_json 读）：`c_user_id=2041451906399821826`，tenant `1942105476598861824`，
`data_scope.type=self`，`delegated=false`。

## 1. 知识问答回归 —— 通过

问：`how can I top charging safely`（`%s` 转义把 "stop" 吃成 "top"，语义仍是知识类）
- `qa_62988e91a6574a58a622e0434a156d48` **completed**
- `retrieval_status=found`、`status=found`、`sources=5`
- 引用真实来源：`新能源汽车充电服务平台_RAG知识库FAQ问答库_纯文本版.docx`、
  `KB-C-047.md`、`KB-C-051.md`、`KB-C-048.md`、`KB-C-049.md`
- 正文含具体操作步骤（结束充电按钮、继电器断开等），不是通用套话

判据：不只是 status=completed，而是**必须有 reference 块且 sources 非空** ——
只有 completed 不能排除"模型自己编了一段"。

## 2. 越权订单反例 —— 通过

先确认归属（直连生产 `ch_order_info`，只读）：

| 订单 | user_id | 是否本人 |
|---|---|---|
| 2115290621004312577 | 2041451906399821826 | 是（本人） |
| 2109928861763776513 | 2041451906399821826 | 是（我第一次误当反例，实际是本人单） |
| **2107546575906942977** | **2107546528991350785** | **否（真反例）** |

问：`what is the status of order 2107546575906942977`
- 页面：`order not found`，**不返回任何字段**
- 服务端：`POST /v1/assistant/questions` → **404（82 字节）**，
  `assistant_questions` 里**没有新记录** ⇒ 拦截发生在建立作业之前，不是"查到了不渲染"
- 判据正确性：`_require_visible_order` → `NOT_OWNED` → 404 `ORDER_NOT_FOUND`；
  越权与不存在同形，不泄露订单是否存在

**这一步我先做错了一次**：第一次拿 `2109928861763776513` 当反例，它其实是本人订单，
答出来是正确行为。查库确认归属后才找到真反例。

## 3. 账单链路 billing_line —— **失败（新增缺陷，已开 PR #1312）**

问：`show the billing of order 2115290621004312577`（两次不同措辞）
- `qa_9f2c32a8c7254b688c6e30be6b6caf08`、`qa_3ad4e49ff2924439b8e47b04d6b1c7f7`
  均 **failed / CHATFLOW_FACT_UNKNOWN**
- `reject_detail = required evidence segment missing`
- 两次模型原文（Dify `workflow_node_executions.outputs`）都是：
  `{{fact:billing_line}}\n{{fact:status_line}}`
- 快照字段齐全：`order_amount=1.13, recorded_payment_amount=1.15,
  recorded_refund_amount=0.0, refund_status=0`，`facts_for()` 确实产出
  `order_line / billing_line / observed_line`，**但没有 status_line**（账单轮无 status_role）

根因：协议块第 81 行「账单必须引用billing_line，状态必须引用status_line」被模型读成
"两个都要写"。判据正确（双向都拦得住、只引对应那段放行），所以改的是措辞。

修复：PR #1312（改措辞 + 回归测试）。合并部署后还需重新发布 Dify app `394b5399`
（协议块是发布时冻结的）。

## 结论

- 知识问答：通过
- 跨租户/越权反例：通过
- **账单链路：未通过**，根因已定位、修复已提交待合并


---

# 修复后复验（同日 22:39，网关 `1.0.0+c54735dbaa6a`）

PR #1312 合并 `c54735db` → CD 成功（网关 `1.0.0+c54735dbaa6a`）→
Dify 原生 publish 生成中间版本 `17d89f00`（**未生效**）→
`/v1/dify/publish` 冻结并推进到 **agent v18 / revision 19 / workflow `9c2cb04a`**。

**生效的 workflow 是 `9c2cb04a`**：`chat_dify_runs` 里四次账单/状态复验（14:39、14:41）
全部记录的是它；`17d89f00` 只是原生 publish 的中间产物，没有任何请求跑过它。

## 账单轮 —— 通过

问：`show billing for order 2115290621004312577`
- `qa_19aa411dcbe8498bb2d79397324b972a` **completed**，`data_kind=billing`，`state=succeeded`
- `fact_contract=slots`，快照 `order_amount=1.13`、`recorded_payment_amount=1.15`、
  `recorded_refund_amount=0.0`、`refund_status=0`
- 页面正文：

```
这单的账单合计 1.13，系统记录的支付金额是 1.15，没有退款记录。
· 订单金额 1.13 —— 这单的总计费；币种接口未提供，金额单位请与原订单页面核对
· 支付金额 1.15 —— 账务系统里记的数，不等于银行已经扣款
· 退款：没有退款记录 —— 有这条记录也不代表银行已经到账，已退金额 0.0
我没有发起任何付款或退款，只能读取记录。
订单号：2115290621004312577
来源：订单账务记录 · 2026-11-02 14:39:04 UTC
```

金额与库内逐字一致；免责语义完整（「不等于银行已经扣款」「也不代表银行已经到账」）；
未出现状态断言。

## 状态轮回归 —— 通过（改措辞没有打坏原来的路）

问：`what is the status of my last order`
- `qa_3dcf4b5f7c1a481ea7053bafe1d0d8a7` **completed**，`data_kind=status`，`state=succeeded`
- 正文含订单号、查询时间、「不代表故障原因」、来源

## 结论（本轮全部四项）

| 项 | 结论 |
|---|---|
| 知识问答回归 | 通过 |
| 跨租户/越权反例 | 通过 |
| 账单链路 | **通过**（修复后） |
| 状态轮回归 | 通过 |

截图未入库（含真实手机号与订单号，属于私有运行证据），存放于操作者私有目录
`~/.cache/aiops-consumer-acceptance3-20261102/`：`w4.jpg`（账单页正文）、`v8.jpg`（越权反例 `order not found`）。
