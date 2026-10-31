# 生产草稿迁移：已写入（2026-10-31）

**只写草稿，未发布。** 已发布版本始终保持 `e7501f0a`（2026-10-31 12:15:18）。

## 写入方式

控制台 API `POST /console/api/apps/<id>/workflows/draft`（`SyncDraftWorkflowPayload`）：
`graph` + `features` + `hash`（并发校验）+ `environment_variable_patch`。
写入脚本先做 dry-run（打印逐节点差异），确认后才 `--apply`。

## 两步与各自的回滚点

| 步骤 | 改动 | 草稿 hash | 回滚备份 |
|---|---|---|---|
| 起点 | — | `6d939223…` | `/tmp/backup-before-step-a.json` |
| **3a 选库** | `knowledge` 节点：`188c54f4…`（空库）→ `3064598c…`（61 文档） | `6dc1c8d9…` | `/tmp/backup-before-step-b.json` |
| **3b 协议+标记** | `answer` 拆成 2 条 SYSTEM（运营文字 888 + 协议 1607）；新增 `answer_fact_contract=slots` | `83bc0699…` | 上述两份 |

两步的 dry-run 与写入均确认：**其余 13 个节点逐字节不变、连线不变、既有环境项不变**。

## 迁移后的核对

```
system_prompts = 2
env = cpms_status_enabled, cpms_billing_enabled, knowledge_answer_mode, answer_fact_contract
published = 2026-10-31 12:15:18 / e7501f0a   ← 未动
```

本地 `publication_settings` 对这份候选的推导：`fact_slots=True`、**`callback_version=5`**、
协议项与 `_protocol_block()` **精确相等**。

## 与操作者手改版的差别

上一轮操作者把协议**追加进同一条 SYSTEM**（2496 字符一条）。本轮按 Codex 验证拆成
**独立的第二条 SYSTEM** —— `callback_version=5` 要求协议项精确独立，否则系统输出协议校验失败。

## 仍待完成

- 真实模型预览（受管试运行）与固定样本
- 发布 A/B、正式新会话、恢复
- 41 原应用 / 原 H5 验收

**业务验收未完成。**
