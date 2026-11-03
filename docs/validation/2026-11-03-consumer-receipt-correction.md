# 消费者回执订正的验证记录（#1316）

日期：2026-11-03　分支：`docs/consumer-receipt-correction`　PR：#1316

纯文档订正，**未修改任何运行代码、测试或生产状态**，也未重跑真实模型或真实问答。
本文件记录的是"文档陈述是否与源码/已有证据一致"的核对过程。

## 1. 越权样本没有 workflow 运行

**源码**：

- `src/aiops_diagnostics/gateway/gateway_api.py:6888` `_require_visible_order`：
  `NOT_OWNED` ⇒ `raise StandardAPIError(404, "ORDER_NOT_FOUND", "order not found")`。
- `src/aiops_diagnostics/gateway/gateway_api.py:3152-3166`：`/v1/assistant/questions` 的
  consumer 分支先解析 `live_order`，再 `_require_visible_order(...)`，**之后**才走到
  建立作业/发起 workflow。

**已有证据**（2026-11-02 当时的记录，本轮未重测）：

- `POST /v1/assistant/questions` → 404（82 字节）
- `assistant_questions` 无新行

两侧一致 ⇒ 该请求停在入口判据，没有作业、没有运行。**结论**：它不是 v17 的运行，
也不是 v18 的；不能计入任何版本回归。

## 2. 恢复步骤与 `change_version`

**源码**：

- `src/aiops_diagnostics/dify/self_service.py:885` `change_version(caller, app_id, revision, version=None)`：
  无 `version` ⇒ 撤回（`routes.retract`）；有 `version` ⇒ 从 `agent_versions` 取该版快照，
  经 `node_publication.advance`（operator）或 `gateway_api._advance_route_binding`（consumer）
  推指针，动作为 `restored`。
- `src/aiops_diagnostics/execution/chat_route_bindings.py:223`（`publish`）：`next_revision = actual + 1`，
  写语句带 `revision = ?` 谓词做 CAS。**没有"写回旧 revision"的路径**。
- `src/aiops_diagnostics/execution/chat_route_bindings.py:336`（`retract`）：`revision = revision + 1`，同样单调。

**结论**：恢复产生**递增**的新 revision；把 17 直接写回 16 与实现冲突，已从文档中移除。

## 3. 「保存 ≠ 恢复」

- Dify agent-chat 的模型配置导出读取当前保存内容；消费者 Chatflow 另有草稿与已发布
  workflow。不能将前者的描述推广成「Dify 没有草稿区」。保存影响候选内容，
  不改变网关已经冻结的发布快照。
- 正式入口用的是 `chat_route_bindings` 里冻结的 `workflow_id` / `agent_version_no`
  （`consumer_chat_service.py:140` 把 `binding.workflow_id` 带进运行），
  不读控制台当前内容。⇒ 保存之后必须再走一次发布（或受管恢复）才改变线上行为。

## 4. 配置优先级

`src/aiops_diagnostics/gateway/gateway_config.py:411-419`：

```python
dify_console_base_url = _env("AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL")
    or _file_value(file_values, "AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL"),
# 同形状的还有 _API_KEY / _WORKSPACE_ID 两项
```

`_private_config_values` / `_file_value`（同文件 861-872）从 `AIOPS_GATEWAY_SERVER_CONFIG_FILE`
指向的文件读取。⇒ 进程环境优先，缺省回落配置文件；"只在 `production.env`"不等于读不到。

**本轮没有执行的操作**：未删 `gateway.env` 里的任何键，未重启服务，未改任何配置。
原失败的真实成因仍未知，文档里已按"待查"记录。

## 5. 业务提示词基线

本机核对 `aiops_diagnostics.consumer.consumer_documents.PROMPT`：长度 **139**，
与记录中的"重置后 139 字符"一致（不是从生产回读，而是源码侧确认这是被贴进去的那份标准文案）。

## 未完成业务验收

本 PR 是记录订正，**不构成任何业务验收**。v18 上知识与越权样本仍未补跑，
跨租户反例仍未做，费用字段与结算状态仍未闭环。
