# 209. 智能体可服务性由 published_version 指针决定，不由 status 决定

日期：2026-11-04
状态：已采纳

「这个智能体现在还有没有版本在服务」在四处各写一遍，而其中两处判的是
`status == "published"`、另外的文档与数据层写的是「草稿编辑不改变正在运行的已发布
版本」（`GLOSSARY.md`「智能体版本」词条；`agent_lifecycle.fork_draft` 的 UPDATE 只改
`status`、保留 `published_version`）。两者不能同时成立：

- 要求 `status == "published"` ⇒ 运营打开草稿编辑的那一刻，客服问答与已签发的媒体
  同时中断 —— 一次编辑变成一次静默下线，而文档说不会；
- 只看 `published_version` ⇒ 停用后指针还在，停用不再意味着停服。

## 决策与依据

判据是 `published_version` 这个指针，`status` 只用来回答「停没停」：

| status | published_version | live_version |
|---|---|---|
| `published` | 1 | 1 |
| `draft` | 1 | 1（编辑中，线上仍是这一版）|
| `disabled` | 1 | None |
| 任意 | NULL | None |

实现为 `publication/agent_serviceability.py` 的两个纯函数 `live_version` / `serves`，
客服问答选择器与媒体活性（`_is_active`）改为调用它。媒体**不直接**用 `serves`：它刻意
允许「更旧但仍绑着该知识库」的版本在剩余 TTL 内存活（ADR-0046 决策二），所以它要的是
`live_version(...) is not None` 加上自己的版本存在性与 KB 绑定检查。

这与 `#732` 把「状态码 → 业务角色」抽进 `order_status.role_of` 是同一形状：把判定
抽到一处，而不是把四个调用点合并成一个。

## 备选方案

- **B：保留 `status == "published"`，改写词条与 runbook。** 被否：那等于把「编辑即
  下线」变成正式契约，并要求 runbook 补一句「编辑期间线上答案会掉」——今天没有任何
  文档有这个警告，而且它与 `fork_draft` 自身的实现（保留指针）直接矛盾。
- **C：两条都取严（`disabled` 与 `draft` 都停服）。** 被否：等价于 B，只是把「从未
  发布」与「编辑中」分两种报错，问题没有消失。
- **不改类型过滤**：consumer 仍选不出 `operations`、operator 仍选不出 `customer`
  （ADR-0015）。改的只是「已经选对类型之后，它还在不在服务」那一句。
