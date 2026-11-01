# B 版回滚完成，暴露两套登记不一致（2026-11-01）

## 回滚结果

| 项 | 值 |
|---|---|
| Dify 已发布版本 | v3 `2026-11-01 06:43:22`（内容 = v1 的图） |
| 已发布 SYSTEM | **1 条 906 字符**，无协议块 |
| `environment_variables` | 3 项，**已删除 `answer_fact_contract`** |
| 选库 | `188c54f4…`（空库，与 v1 一致） |
| 线上真实会话 | `completed`，`fact_contract: legacy` |

回滚方法：从 Dify 库取 v1 的 `graph`，写回草稿（`environment_variable_patch` 删除 slots 项），
再调 `POST /workflows/publish`。全程 dry-run 先行。

## 本轮真正的阻塞：两套登记不一致

这是 B 版发布后正式会话仍走 legacy 的根因。

| 表 | app_id | 更新时间 |
|---|---|---|
| `chat_route_bindings`（**正式会话读它**） | `07d15c42…` revision 9 | 2026-10-31 12:15:19 |
| `dify_app_registry`（**产品发布端点读它**） | `394b5399…` | 2026-10-25 08:56 |

后果，两条路都断：

1. **正式会话**从 `chat_route_bindings` 取绑定，其 `callback_version` 快照仍是 4 →
   即使 Dify 已发布带 slots 的版本，运行时仍是 `legacy`。
2. **产品发布端点** `POST /v1/dify/publish` 从 `dify_app_registry` 查 `07d15c42` →
   查不到 → `404 DIFY_APP_NOT_BOUND`。

**所以：直接调 Dify 原生 `/workflows/publish` 只改 Dify，不更新网关绑定。**
我第一次发布就是这么做的，导致"发了但没生效"。这不是 Dify 的缺陷，是我用了绕开产品的路径。

## 我的错误（本轮）

1. 用 Dify 原生 publish 代替产品发布端点 → 绑定的 `callback_version` 没前进。
2. 误把"发布前的草稿快照"当作 A 版回滚材料 —— 那份草稿已含 slots。
   真正的 A 只能从 Dify 版本历史（v1 `e7501f0a`）取。

## 待决断（Codex 第 2 点的一致性核查结论）

这不只是"两套登记不同"，而是**产品发布路径当前不可用**。三条路：

- **A** 把 `dify_app_registry` 的 consumer 行更新到 `07d15c42`，让发布端点可用；
- **B** 查清 10-31 12:15 那次切换为何没同步登记（漏了还是有意）；
- **C** 维持现状，slots 迁移继续用 Dify 原生路径，但那样 `callback_version` 永远不会前进。

我倾向 B → A：先弄清原因再改，避免把一次漏更新当成设计。

## 现状

**线上是 A 版（legacy + 空库），与回滚前一致。** 本轮未留下不一致中间态。
业务验收仍未完成。
