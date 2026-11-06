# 回执：优链充 consumer 渠道声明接线（PR #1438）

日期：2026-11-06 · 修订：`f87839d6` · 服务宿主变更：41 网关 env + 清单落地 + 重启

## 问题

受管抽屉显示「该端尚未接入使用渠道」，但 ulink 的 C 端 H5 AI 客服一直在被真实使用。

## 根因（部署缺口，非逻辑缺陷）

`channel` 为声明制（ADR-0189）：`AIOPS_GATEWAY_ENVIRONMENT_MANIFEST_FILE` → 环境清单
`[[channels]]` → `(tenant_id, entry)`。41 上 env 变量未配、清单无 `[[channels]]` 行，
全租户一律 fail-closed pending。

## 证据（声明前提）

- `chat_dify_runs`：tenant_id `1942105476598861824`（`chat_route_bindings` 口径——
  与注释中另一优链充 id `1941847786056323072` 不同）consumer 端 succeeded 170+，
  最新 2026-11-05T13:38Z；入口 = `ulink.h5.mall.qushiyun.com` AI 客服页
- operator 端查不到运行流量 → 按设计不声明，继续 pending

## 落地

- `env-41.toml` 增 `[[channels]]`（consumer/ulink-h5/connected）
- 41：`/etc/aiops-41/env-41.toml`（合后制品 sha256 `189e6ee6…105b`）+
  `gateway.env` 补 `AIOPS_GATEWAY_ENVIRONMENT_MANIFEST_FILE` + 重启 `aiops-gateway-41`

## 验证

- 服务同款代码路径（aiops41 身份）：`channel_state` → `{channel: ulink-h5, state: connected}`
- 端到端 `/__aiops/manage` status（页面同款三门头）：
  `"channel": "connected", "readiness": "ready", "version_no": 21`
