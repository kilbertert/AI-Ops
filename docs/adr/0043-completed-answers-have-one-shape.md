# 43. `completed` 的回答只有 blocks-v1 一种形状

日期：2026-10-19
状态：已采纳
影响版本：`src/aiops_diagnostics/gateway_runtime.py`（`as_blocks_result`）、
`docs/agents/frontend-api-brief.md` §10.8、`docs/agents/assistant-type-trigger-guide.md`

## 背景

统一助手的三条回答路径写着同一个 `completed` 状态，却落库了**两种**负载形状：

- 知识库路径（`qa_rag`）与宣传路径写 blocks-v1：
  `{"blocks": [{"kind": "text", …}], "retrieval_status": …}`；
- 降级零阶路径（`run_zero_order_answer`，T3/#153）写 `{"text": …, "reminder": …}`。

合同文档只描述了第一种：`frontend-api-brief` §10.8 场景 B 让前端解析 `blocks[]`，
并明说「不要解析 Markdown、不要从文本里提取 URL」。于是按合同实现的客户端会把每条零阶回答
渲染成自己的兜底文案 —— 用户的问题被回答了，界面上却显示成没回答。

代价是量出来的，不是推出来的：41 上 33 条 `completed` 里 9 条是旧形状；线上 UlinkEV 1.2.14
（PGYer build `be1013c3…`）的 `app-service.js` 里 `result.text` 出现 **0 次**，
把该包的渲染逻辑逐行抄出来喂真实行，`text+reminder` 全部得到「未找到」。

根因不是"某条路径写错了"，而是**没有一处规定 completed 只有一种形状**：三条路径各自决定
自己写什么，合同文档只跟其中两条对齐，而这个偏离活了很久没人发现 —— 因为它只在
「问题降级到零阶」时才可见。

## 决策

**`completed` 的 `result` 只有 blocks-v1 一种形状；零阶答案在落库前归一化。**

- 归一化点是 `_execute_assistant_qa` 里**唯一的落库调用**，没有第二条路径；
  模型正文移进 `{"kind": "text", "text": …}`。
- `reminder` **丢弃**而不是升格成块类型：合同里没有客户端读它，提醒行本就在正文里
  （#708 已确认）。保留它会多出第二个没人消费的字段。
- `retrieval_status` 分两种成因，**不合并**：没有选中客服智能体（没检索）取
  `not_found`；知识库检索**失败**取 `unavailable`。后者由 `_try_customer_rag` 返回的
  哨兵 `KB_OUTAGE` 传到落库点 —— 一个裸 `None` 会让"搜不了"与"没得搜"在数据上无法区分，
  于是故障被报成「知识库里没有匹配内容」，正是 `promo_empty_result` 一直拒绝做的那种声明。
- **存量历史行不重写**，按写入时的形状逐字回读 —— 这是一个只作用于写入侧的归一化，
  不是一次数据迁移。

## 被否掉的替代形态

- **让前端补读 `result.text`。** 一行就能修好这个客户端，但修不好合同：形状分叉仍在，
  下一个按文档实现的客户端会再踩一次。而且发版在对方手里，反馈环路最长。

- **把 `reminder` 升格成一种块（`kind: "reminder"`）。** 没有消费者，且 `QaAnswer`
  的块词汇是四类（text/image/video/reference）；加第五类要动 schema、提示词与四类渲染表，
  换来一个客户端不读的字段。

- **在 `_guard_zero_order_language` 里顺手包。** 那个函数的名字与职责是"语言守卫"。
  把形状归一化藏进去，下一个人改语言规则时会连带改形状。

- **把 `retrieval_status` 留空。** 空值在指标面（`metrics_store.RETRIEVAL_STATUSES`）
  是合法值，但它会让「零阶」与「没记录」在指标上长得一样。

- **两个成因都写 `not_found`（本记录第一版就是这样，评审指出后改）。** 少了那个哨兵，
  一次 kb-service 故障会被写成「知识库里没有匹配内容」—— 与「没选中客服智能体」
  合流成同一个声明，而它们一个读过库、一个没读。判据是 `promo_empty_result` 早就写下的
  同一条：没读过的库不能被断言为空。

## 后果

更容易：客户端只需实现一种 `result` 形状；「零阶回答看不到正文」这类缺陷不会再出现，
因为它需要形状分叉才可能存在。

更难：零阶路径的返回形状**不再由它自己决定** —— 谁要加一个零阶专属字段，必须同时回答
"它为什么不能是一个块"。这是一次刻意的收窄。

上限：归一化只保证**容器**对，不保证**内容**对 —— 模型在 text 块里写了什么、语言对不对，
是另一条线（`_guard_zero_order_language` 与多语言验收），本记录不动它。
