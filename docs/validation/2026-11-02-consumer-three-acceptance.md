# 消费者端三项补充验收与账单轮修复（2026-11-02）

环境：41 生产网关 `1.0.0+2f38f4dc1aaa`。设备：rl-1 USB → NAM-AL00 / Android 12，
`com.trend.ulink` 1.2.15，验收账号 `phone+fixed-code` 正常登录（未注入凭据）。

调用者主体（从 `chat_dify_runs.scope_json.subject` 读，不是 `caller`）：
`c_user_id=2041451906399821826`、tenant `1942105476598861824`、
`data_scope.type=self`、`delegated=false`。

## 一、知识问答回归 —— 通过

问：`how can I top charging safely`

- `qa_62988e91a6574a58a622e0434a156d48` **completed**
- `retrieval_status=found`、`status=found`、`sources=5`
- 引用真实来源：`新能源汽车充电服务平台_RAG知识库FAQ问答库_纯文本版.docx`、
  `KB-C-047.md`、`KB-C-051.md`、`KB-C-048.md`、`KB-C-049.md`
- 正文是具体操作步骤（结束充电按钮、继电器断开等），不是通用套话

**判据说明**：只判 `completed` 不足以排除"模型自己编一段"，
必须有 `reference` 块且 `sources` 非空。

## 二、越权订单反例 —— 通过

先直连生产 `ch_order_info`（只读）确认归属：

| 订单 | user_id | 是否本人 |
|---|---|---|
| 2115290621004312577 | 2041451906399821826 | 是 |
| 2109928861763776513 | 2041451906399821826 | 是 |
| **2107546575906942977** | **2107546528991350785** | **否** |

问：`what is the status of order 2107546575906942977`

- 页面：`order not found`，**不返回任何字段**
- 服务端：`POST /v1/assistant/questions` → **404（82 字节）**，
  `assistant_questions` **没有新记录** ⇒ 拦截在建立作业之前，
  不是"查到了但不渲染"

**本轮的一次误判（如实记录）**：最初拿 `2109928861763776513` 当"他人订单"，
它其实属于本人，答出来是正确行为。查库确认归属后才找到真反例。
教训：反例必须先证明归属，不能靠"看起来不像我的"。

## 三、账单链路 —— **未通过**（缺陷已定位并修复，待合并与重发）

问：`show the billing of order 2115290621004312577`（两种措辞各一次）

- `qa_9f2c32a8c7254b688c6e30be6b6caf08`、`qa_3ad4e49ff2924439b8e47b04d6b1c7f7`
  均 **failed / CHATFLOW_FACT_UNKNOWN**，`reject_detail = required evidence segment missing`
- 两次模型原文（Dify `workflow_node_executions.outputs`，
  `node_type='answer'`）都是 `{{fact:billing_line}}\n{{fact:status_line}}`
- 快照字段齐全（`order_amount=1.13`、`recorded_payment_amount=1.15`、
  `recorded_refund_amount=0.0`、`refund_status=0`），`facts_for()` 产出
  `order_line / billing_line / observed_line`，**没有 status_line**（账单轮无 `status_role`）

### 根因

协议块第 81 行「账单必须引用 billing_line，状态必须引用 status_line」被模型读成
"两个都要写"。

### 判据是对的（本地双向验证）

| 场景 | 结果 |
|---|---|
| 账单轮误引 `status_line` | 拒绝 `CHATFLOW_FACT_UNKNOWN` |
| 状态轮误引 `billing_line` | 拒绝 `CHATFLOW_FACT_UNKNOWN` |
| 账单轮只引 `billing_line` | 放行 |
| 状态轮只引 `status_line` | 放行 |

所以修的是措辞，不是守卫。

### 失败样例（供后续回归使用）

正确的账单轮 reply 形状（本地渲染验证通过）：

```
{{fact:billing_line}}
{{fact:observed_line}}
```

渲染结果含「total 1.13 / recorded payment 1.15 / no refund recorded」+
「recorded payment 不证明银行已扣款」+ 订单号与取证时间。

## 自动化检查

- 新增 `tests/consumer/test_prompt_fact_segments.py::test_protocol_does_not_demand_both_segments_at_once`
- `tests/consumer` 全量 **558 passed**

## 未完成业务验收

账单轮的真机复验必须等 PR #1312 合并部署 + Dify app `394b5399` 重新发布之后才能做。
在那之前，**账单链路仍判未通过**。
