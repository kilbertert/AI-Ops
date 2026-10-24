# 2026-10-24 · APP-RUNTIME-02：原 App 聊天接入 Dify（本人权限贯穿取数）

## 目标

在已交付的客户节点路线**旁边**再加一条真实可运行的 Dify 路线：App 仍用原聊天
`POST`/`GET` 与 blocks；Gateway 负责身份、选单、授权；Dify 负责本轮工作流与模型步骤。
**默认关闭**，一条登记只指向一个引擎，没有故障自动跨路线回落。

## 交付

| 层 | 改动 |
|---|---|
| 绑定 | `chat_execution` 的登记表加 `engine`（`node`/`dify`）。缺省=节点（旧登记原样可读）；未知值、半配、同场景重复登记 ⇒ **启动失败** |
| 取数凭据 | 新模块 `chat_data_grant`：≥256 bit、库存哈希、绑主体/订单/环境/租户/QA/workflow、180 秒、一次性、可撤销 |
| Dify 调用 | 新模块 `dify_chat_workflow`：Service API `/v1/workflows/run` 与 `/v1/workflows/tasks/{task_id}/stop`；app key 从 0600 文件读，不进日志/prompt/响应 |
| 执行记录 | 新表 `chat_dify_runs`：`workflow_run_id`/`task_id`/`app_id`/`workflow_id`/`grant_id`/`snapshot`/`scope_json`/`binding_key`；**不伪造 node_id 或 matched** |
| worker | `_execute_chat_dify_qa`：签发凭据 → 建记录 → 调 Dify → 核身份与快照 → **写前再授权** → 落终态；失败不回落 |
| 回调 | `POST /v1/internal/charging-status/dify-read`：凭据校验 + 当前授权 + 一次业务读取 + 同轮快照回放 |
| 回读 | `_require_current_chat_dify_order`：Dify 前缀分流；成功正文过授权/绑定/TTL/快照一致性 |
| DSL | `deploy/dify-chat/consumer-order-status.dsl.yml`：固定 URL 的 HTTP 步骤，凭据**直接绑定** `Authorization` 头 |
| 隔离栈 | `deploy/dify-chat/docker-compose.acceptance.yaml`（真 Dify 1.17.1，一次性根，仅回环） |
| 验收 | `tools/m2_close/m3node/verify_dify_chat_entry.py` |

## 配置

```dotenv
AIOPS_GATEWAY_CHAT_EXECUTION_BINDINGS_FILE=
AIOPS_GATEWAY_DIFY_CHAT_BASE_URL=
```

绑定表里 `engine: dify` 的登记需要 `workflow_id` 与 `workflow_key_file`；有 Dify 登记却没有
`AIOPS_GATEWAY_DIFY_CHAT_BASE_URL` ⇒ **启动失败**。只配地址不登记任何 Dify 路线是无害的。

## 已知限制（不当作已消除）

- Dify 的 inputs 与运行记录会保存本次 input，取数凭据对**能打开该应用记录的人**可见；
  完成/失败/取消后立即撤销，但不声称能追回此前已披露的数据。
- 本片只支持「固定 HTTP 取数 → 模型 → 结束」这一种图，不承诺任意 Function Calling 图。
- SOP 的编辑/试用/发布/撤回属 M3；这里只固定并核对**实际执行的已发布 workflow 身份**。

## 真实整链结果

`tools/m2_close/m3node/verify_dify_chat_entry.py`：**27/27 PASS**（2026-10-24）。
真 Dify 1.17.1（隔离栈、一次性数据根、仅回环）+ 真 Gateway 进程；模型与 CPMS 为合成上游。
判据与逐条结果见 [`dify-chat-real-chain-evidence.md`](../agents/consumer-order-chat/dify-chat-real-chain-evidence.md)。

## 仍未完成

真实外部模型供应商与真实业务库、真实公司 UPMS 目录、登录 App 真机验收、生产启用。
本片**默认关闭**：没有绑定登记就没有 Dify 路线；41 与 36 都没有改动。
