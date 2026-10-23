# 91. 保留期不回收已交付的结果

日期：2026-10-23
状态：已采纳
影响版本：`src/aiops_diagnostics/async_job_lifecycle.py`（`expire_statements`）

## 背景：同一句话，15 分钟后从「有答案」变成「没找到」

41 上的实测记录：

```
11:06:36  completed   done=11:10:26   result=YES
10:41:13  expired     done=10:44:16   result=YES   ← 答案还在库里
```

同一个订单、同一句话、同一条答案。第二条在 `completed` 之后满 15 分钟被 sweep 成
`expired`。而 `_standard_diagnosis_response` 对非 `completed` 的行一律返回
`result: null`：

```python
"result": diagnosis.get("result") if status_value in DIAGNOSIS.completed else None,
```

于是客户端看到的是「未找到」。用户的原话是「有时候可以得到回复，有时候又显示没找到，
**我一个字都没换**」。

这条 sweep 跟在**读路径**上（`get_standard_diagnosis` 先跑 `_expire_diagnoses`），
所以不是「过一会儿就好」——每次轮询都在重新确认它已经过期。

## 这条规则实际在做什么

`expire_statements` 返回两条语句：

- `claim`：把**仍在跑**且过了 `deadline_at` 的行标成 `expired` —— 语义正确，
  那些作业确实没有交付任何东西。
- `sweep`：把**已终态**且过了 `expires_at` 的行标成 `expired` —— 问题在这里。

`sweep` 的集合是 `profile.completed | profile.interrupted`，也就是**包括
`completed`**。而这个 schema **没有任何物理删除**：

```bash
$ grep -rn "DELETE FROM" src/aiops_diagnostics/gateway_store.py
# 只有 dify_knowledge_bindings，作业表一条都没有
```

所以「保留期」这个设置唯一可见的后果，就是把一个正确的答案变成一个上报的失败。
它既没有缩小存储，也没有降低任何隐私暴露 —— 行和 `result_json` 都还在原处。

## 决策

**`sweep` 只作用于 interrupted 档（`failed` / `cancelled`）。`completed` 行保持
自己的状态与结果。**

- `expired` **仍然是终态集合的成员**，客户端契约不变
  （`docs/agents/testing-api-reference.md` §2.2 的表不动）。
- `claim` **完全不变**：跑超时的作业仍然收敛为 `expired`。
- 两条规则作用在**互不相交**的行集合上：`claim` 只看 active，`sweep` 只看
  interrupted。这正是这次改动安全的理由 —— 它没有放宽「未交付」的判定。
- `completed` 行的 `expires_at` 从此不再有意义。这是诚实的状态：在这个 schema 真的
  有保留期删除之前，它本来就没有意义。

## 备选方案

1. **给 `completed` 一个独立的「结果已过期」状态。** 不采用：客户端契约里
   `expired` 已经是终态之一，再加一个值等于让每个客户端重新判断一遍；而收益只是
   换个名字，结果照样读不到。
2. **保留 sweep，但让它在 `expired` 时仍然返回 `result`。** 不采用：那会让 API
   的 `status` 与 `result` 自相矛盾 —— 说「已超时」却给出答案，客户端无法据
   `status` 决定要不要轮询。
3. **把 `completed_retention` 调到很长，例如 30 天。** 不采用：它只是把问题推远，
   而且没有解决「设置保留期却不删除任何东西」这个自相矛盾。
4. **真的实现物理删除（到点删 `result_json`）。** 暂不采用：那是真正的隐私/容量
   决策，需要单独定保留期口径与审计要求；本 ADR 只消除「不删除却谎报失败」。
   做完第 4 项后，第 2 项的取舍要重新讨论。
5. **只改客户端，让它把 `expired` 也当成有结果。** 不能采用：`result` 根本没下发。

## 没有做的事

- 没有给 `completed` 行引入物理删除或结果清理。
- 没有改客户端契约里的终态集合。
- 没有动 `claim`（运行超时）的语义。

## 验证

- 两条新用例先红（`completed` → `expired`，`result` 读不到），修后转绿。
- **变异复验**：把 `swept` 改回 `completed | interrupted`，两条立即转红。
- 相邻的「迟到完成被拒」用例仍然绿 —— 证明 `claim` 那一半没被放宽。
