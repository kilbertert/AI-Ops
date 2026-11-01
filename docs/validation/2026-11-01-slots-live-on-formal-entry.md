# slots 契约在正式入口生效（2026-11-01）

## 证据（生产正式入口，真实用户会话）

```
POST /v1/assistant/questions  （公网 api.mall.qushiyun.com，真实 thirdSession）
qa_00b828cfa31643dabe9b88835be63680 → completed
```

```json
{"blocks": [
   {"kind":"text","text":"请先将车辆停在对应车位…请优先遵循充电桩屏幕和页面指引。"},
   {"kind":"reference","reference_id":"f4bbabeb4813710f","title":"KB-C-007.md"}],
 "retrieval_status":"found"}
```

库内核对（`chat_dify_runs`，2026-11-01T07:20:33）：

```
state=succeeded  data_kind=status
fact_contract: slots        ← 新契约在正式入口生效
status: found   sources: 5  ← 检索返回真实来源
```

## 走通它的三步（顺序是本次才澄清的）

1. **写草稿**：基于 A 版构造 slots 候选 —— 选库换成 `3064598c…`（61 文档）、
   `answer` 拆成 2 条 SYSTEM（运营 837 + 协议 1607）、加 `answer_fact_contract=slots`。
   逐节点 dry-run 确认：只改 `knowledge` 与 `answer`，连线与既有环境项不变。
2. **用产品端点发布** `POST /v1/dify/publish` → `{"action":"synced","version_no":11}`。
3. **验证正式会话** → `fact_contract: slots`。

## 关键：`callback_version` 在发布时冻结

它在发布那一刻从 Dify 的 workflow 计算并写进绑定。所以必须
**先改 Dify → 再用产品端点发布**。我第一轮反了（先原生 publish 再用产品端点），
那时草稿已回滚成 A，冻结的仍是 4。

## 订单链路

用另一会话问 `2115290621004312577` → `404 ORDER_NOT_FOUND`。
**这是正确行为**：该会话的用户看不到这张单，订单归属校验按设计拒绝。
知识链路已证明 slots 生效；订单链路要在有可见订单的会话上验。

## 回滚点

- 草稿写前备份：`/tmp/backup-before-slots-final.json`
- 上一发布版：v10（A 版内容，`fact_contract=legacy`）
- 清单备份：`/tmp/env-41.toml.bak-20261101T070554`

## 尚未完成

- 有可见订单的会话上的订单链路验证
- A/B 恢复演练（当前线上是 slots 版；需验证恢复回 A）
- 41 原 H5 验收

**业务验收未完成。**
