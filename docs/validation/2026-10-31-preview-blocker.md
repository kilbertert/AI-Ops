# 真实预览入口：advanced-chat 可用（2026-10-31）

## 结论

**不存在预览阻塞。** 上一版本文档把 404 误判为"能力边界"，那是打错了路由。

## 正确入口

目标应用 `394b5399-…` 的 mode 是 `advanced-chat`。可用的就地草稿运行接口是：

```
POST /console/api/apps/<app>/advanced-chat/workflows/draft/run
```

## 三次误判的经过（保留，避免有人重犯）

| 尝试的路径 | 响应 | 我当时的结论 | 实际含义 |
|---|---|---|---|
| `/apps/<id>/workflows/draft/run` | 404 `App mode is not in the supported list: {'workflow'}` | "advanced-chat 不支持草稿运行" | 该路由只服务 `workflow` 模式 |
| `/apps/<id>/chat-messages` | 404 `{chat, agent, agent-chat}` | 同上 | 该路由不服务 `advanced-chat` |
| `/apps/<id>/advanced-chat/workflows/draft/run` | **200，返回 SSE 事件** | —— | **正确入口** |

误判根源：我先看到两个 404 就下了能力边界的结论，**没有把 `/advanced-chat/` 这个模式前缀试全**。
404 的报文里其实已经点名了"支持哪些模式"，但我把它当成了"没有这个能力"，
而不是"这个路径服务的是别的模式"。

## 该接口的性质

`POST .../advanced-chat/workflows/draft/run` 执行的是**草稿图**，返回 `workflow_started` /
`workflow_finished` 等 SSE 事件。它**不修改草稿、不改正式指针**，属于只读预览。

因此本轮不需要：
- 在生产 workspace 创建临时应用（`WorkflowTrial`）；
- 跳过预览直接发布。

两条原本列为"备选"的路都不必走。
