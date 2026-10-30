# C端答复先说结论：本地验证记录

日期：2026-10-30（UTC）。分支`feat/consumer-plain-answers`，基线`5de086c3`。
本记录是**提交前**的本地验证；远端 CI、合并与部署不在本记录内。
目标与缺口见[开发进度记录](../开发进度/2026-10-30-consumer-plain-answers.md)，
设计取舍见 [ADR-0182](../adr/0182-consumer-answers-lead-with-the-conclusion.md)。

## 环境与范围

开发机、本任务 worktree、本地 Docker（只用于重建内容锁）。
未访问 41、未连真实 Dify、未调用真实模型、未改任何生产数据或配置；镜像未推送任何注册表。

## 自动化检查

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 静态检查 | `uv run ruff check .` | 通过 |
| 格式检查 | `uv run ruff format --check .` | 通过，1120 files already formatted |
| 全量测试 | `uv run pytest -n 8` | **3482 passed / 9 skipped**，95.05s |
| 本片专项 | `uv run pytest tests/consumer/test_consumer_plain_answers.py` | 19 passed |
| 内容锁 | `deploy/m2-live-03/build-image.sh --update-lock` | `source_sha256=b44ef1d5f511…`，`built_at=2026-10-30T05:31:13Z` |

九条 skip 全部是既有环境跳过（`AIOPS_GL_CA` 未配置、playwright 解释器缺失等），与本片无关。

## 变异验证（先红后绿）

| 变异 | 被拦住的用例 |
| --- | --- |
| `billing_text` 的结论句换成固定串「这单的账单如下。」 | `test_billing_answer_opens_with_a_conclusion_not_a_field_list`、`test_zero_is_a_value_not_a_missing_field` |
| 删掉支付行的「不等于银行已经扣款」 | `test_payment_line_denies_debit_and_refund_line_denies_arrival` |

两次变异后恢复源码，19 条全绿。

## 与既有断言的冲突及处理

本片改的是"屏幕上出现哪些字"，所以有一批用例原本断言的字面值必须跟着改。逐条说明
**改的是指针还是意图**：

| 用例 | 原断言 | 现断言 | 意图是否变了 |
| --- | --- | --- | --- |
| `test_consumer_multilingual.py` 11 语种事实渲染 | `cpms.orderDetail` 出现在正文 | 该语种的「订单账务记录」出现在正文，且原标识**不**出现 | 不变：读者要认得出数据来自哪条记录 |
| `test_consumer_multilingual.py` 占位符集合 | `status` 含 `status_code` | 同上但去掉 `status_code` | 不变：逐语种一致才不出现键缺失 |
| `test_consumer_billing.py` 免责 | 逐字匹配「不证明已经扣款」等 | 支付行必须否认扣款、退款行必须否认到账 | 不变且更贴意图：拦的是"免责被简化成听起来钱已到账" |
| `test_consumer_order_chat.py` / `test_consumer_live_context.py` / `test_consumer_semantic_routing.py` / `test_app_runtime_node_entry.py` | 正文含 `cpms.chargingInfo` | 正文含「充电状态记录」且不含 `cpms.` | 不变 |
| `test_consumer_order_chat.py` 未知状态 | 正文含 `status_role=unknown` | 正文含「未知」且不含 `status_role=` | 不变：未知状态要如实说，但说人话 |

## 实际输出（合并后代码生成）

```
这单当前的状态是「充电结束」。

订单号：2113549588485718017
查询于：2026-10-29 12:34:45 UTC

这只是我查到的业务状态，不代表故障原因。
来源：充电状态记录
```

```
这单的账单合计 27.36，系统记录的支付金额是 0.00，没有退款记录。

· 订单金额 27.36 —— 这单的总计费；币种接口未提供，金额单位请与原订单页面核对
· 支付金额 0.00 —— 账务系统里记的数，不等于银行已经扣款
· 退款：没有退款记录 —— 有这条记录也不代表银行已经到账

我没有发起任何付款或退款，只能读取记录。

订单号：2113549588485718017
来源：订单账务记录 · 2026-10-29 12:34:45 UTC
```

## 制品身份

| 项 | 值 |
| --- | --- |
| 镜像 | `aiops-m2-live-03-gateway:plain-answers`（仅本地） |
| `image_id` | `sha256:adfb88f8db242ec62cc5bb000a940dcaae30b373c7a46161e68b0056538fb55c` |
| `source_sha256` | `b44ef1d5f5111846bb9191d900f4dee2c380f346b5fda4f272200269e7f149e2` |
| 基础镜像 | `python@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f` |

## 未完成项（不得读作通过）

- **未完成业务验收。** 没有真实账号、真实订单或 App/H5 上的回读证据；本地绿色只说明
  渲染与断言成立，不说明我的措辞能让业务同事看懂——那需要人看。
- 九种非中文译文由本轮补写，**未经母语者复核**。句子结构沿用了各语言包里已有的句式，
  但"读起来是否自然"没有证据。
- 部署瞬间的回读窗口未消除（ADR-0182「代价」），本片只是在 180 秒内限定影响面。
- 远端 CI、合并、部署与灰度均未开始。
