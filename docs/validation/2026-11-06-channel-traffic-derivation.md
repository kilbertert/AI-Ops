# 回执：渠道状态「声明优先、真实流量推导兜底」（#1441）

日期：2026-11-06 ・ 候选：fix/channel-traffic-derivation（94a37e4b）

## 背景与判据来源

用户提出产品化要求：新租户、新服务器不能再逐台手工编辑 `env-<env>.toml`
`[[channels]]`。选定方向「流量推导 + 声明兜底」。判据（ADR-0189 §修订，不
从实现输出反推）：

1. `[[channels]]` 声明行是显式裁决——人工 `pending` 压回不被流量推翻；
2. 无声明看真实流量：consumer 查 `chat_dify_runs`（`tenant_id` +
   `binding_key LIKE 'consumer_service:%'`），operator 查 `agent_runs`
   （`json_extract(scope_json,'$.caller.tenant_id')`），一条 `succeeded` 即 connected；
3. 都没有 ⇒ `pending`（fail closed）；观测查询失败按未接入显示。

## 确定性检查

- `uv run pytest tests/dify/test_dual_entry.py tests/execution/test_agent_run_records.py
  tests/execution/test_app_runtime_dify_entry.py tests/execution/test_chat_route_bindings.py`
  —— **137 全绿**。新增 3 用例：纯函数三档优先级、`_channel_state` 集成
  （running 不算证据 / succeeded 推导 connected / 声明 pending 压回）、
  `has_succeeded_for_tenant` 租户归属。
- `uv run ruff check` + `ruff format --check`：绿。
- `verify-aiops-gateway`（`.claude/skills/.../observe.py`，一次性数据根真进程）：
  `/health` 只读、匿名 401、OpenAPI 200、退役端点 404 —— **全过**。
  证据 `var/verify-evidence/channel-derivation/aiops-gateway.json`。

## 一次性数据根探针（真 settings → 真 service → 真 sqlite）

`/tmp/probe-channel-derive.py`：清掉继承的 `AIOPS_*`，指向临时
`AIOPS_GATEWAY_DATA_HOME`，经 `GatewayServerSettings.from_env()` →
`SelfService(SimpleNamespace(settings=...))` 走到 `_channel_state`。
种子行用真实 `DifyRunStore.issue/bind_workflow/succeed` 与 `AgentRunStore`
直插。**11/11 全过**：

| 场景 | 期望 | 实测 |
|---|---|---|
| 无声明无流量 | pending + 「该端尚未接入使用渠道」 | PASS |
| 仅 running（未跑完） | pending | PASS |
| consumer 一条 succeeded | connected | PASS |
| 他租户不受影响 | pending | PASS |
| operator 无 agent_runs 行 | pending | PASS |
| operator 一条 succeeded | connected | PASS |
| 声明 pending 压过流量 | pending + note 保留 | PASS |
| operator 未被该声明影响 | 仍 connected | PASS |
| 声明 connected 无需流量 | connected | PASS |

## 部署后核对计划（41）

41 现行三行声明（ulink consumer/operator、优链充 operator）仍是显式裁决 ⇒
合并部署后页面**应无可见变化**（回归判据：三条仍 connected、优链充 consumer
仍 pending）。推导路径的生产自然触发点是第一个未声明新租户。

## 未验范围

- 不证真实 Dify 控制台会话路径下的端到端（`/​__aiops/manage` 三门头链路未在本轮重跑；
  `_channel_state` 是服务端判定，前端只读 `status.channel`）。
- 不证其他环境（env-probe 只跑本机一次性根）。
- 同文件在途分支 `refactor/capability-source-tiers`、`refactor/consumer-agent-conversation`
  与本 PR 重叠，后合并者需 rebase——已作为评审注意项记录。
