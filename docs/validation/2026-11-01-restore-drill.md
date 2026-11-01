# A/B 恢复演练完成（2026-11-01）

## 双向验证

| 步骤 | 发布版本 | 正式会话结果 |
|---|---|---|
| slots 上线 | v11 | `fact_contract=slots`、`found`、来源 `KB-C-007.md` |
| **恢复 A** | **v12** | **`fact_contract=legacy`、`not_found`** |
| **再恢复 slots** | **v13** | **`fact_contract=slots`、`found`、来源 `KB-C-007.md`** |

每一步都用**产品端点** `POST /v1/dify/publish` 推进绑定（`action=synced`），
不是 Dify 原生 publish —— 后者不更新绑定，`callback_version` 不会前进。

## 回滚点（全部实测可用）

| 对象 | 位置 |
|---|---|
| slots 草稿 | `/tmp/slots-final.json` |
| A 版图 | `/tmp/A-graph-clean.json`（取自 v1 `e7501f0a`） |
| 各步草稿备份 | `/tmp/backup-before-{slots-final,restore-A}.json` |
| 环境清单 | `/tmp/env-41.toml.bak-20261101T070554` |

## 订单链路：未验，原因是数据而非机制

用两个真实会话问 `2115290621004312577`，均返回 `404 ORDER_NOT_FOUND`。

查 `chat_dify_runs`：这些订单历史上由 `service:java-bff`（服务身份）查询，
**没有对应的真实用户会话**。订单可见性按"本人订单"判定，真实用户看不到别人的单 ——
`ORDER_NOT_FOUND` 是**正确行为**。

因此订单链路（restricted→slots 的 realtime 分支）**尚未在生产验证**。
它已在试运行里验过（`fact_contract=slots` + `status=live` + 事实段渲染），
但那不等于正式入口。

## 未完成

- 有可见订单的真实会话上的订单链路验证
- 41 原 H5 验收
- 固定样本批次（Codex 任务书第 4 点）
