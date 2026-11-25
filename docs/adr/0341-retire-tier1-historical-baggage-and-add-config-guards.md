# 341. 清理 Tier 1 零风险历史包袱并增加配置声明-消费闭环守卫

状态：已采纳（2026-11-25）

## 背景

在完成认知成本简化（PR #1699–#1726）后的全仓 Git 历史考古中，确认了一批解决的问题已随 Era 1/Era 2 引擎退役、生产调用面为零的残留项，以及一处清理遗漏导致的真实潜在运行期崩溃：

1. `src/aiops_diagnostics/admin/common.py` 的 `_console_settings_from()` 仍读取在 `#1125` 已删除的 `AIOPS_GATEWAY_OPS_BINDINGS_FILE` 与 `AIOPS_GATEWAY_DIFY_TOOL_BINDINGS` 两键（而 41 宿主机 `/etc/aiops-41/gateway.env` 仍留有前者），传给 `dataclasses.replace(console, ...)` 会触发 `TypeError`。
2. `GatewayServerSettings` / `SETTING_SOURCES` / `HttpServerConfig` / `.env.example` 中 4 个字段（`allow_fixtures`、`routing_risk_at_least`、`routing_confidence_at_least`、`routing_risk_always_asks`）全仓生产读取数为 0；`gateway_config.py` 中 5 个设备 CLI 遗留辅助（`GatewayClientProfile`、`gateway_profile_dir`、`gateway_profile_path`、`gateway_token_file`、`validate_profile_name`）全仓引用数为 0。
3. `GatewayStore.prune_hop_tickets()` 生产与测试引用数均为 0；`gateway_store.py` 与 `async_job_lifecycle.py` 仍残留对已退役 `runs` 表的说明注释。
4. `docs/adr/0012-adr-check-scope-and-self-application.md` 与 `0013-...` 为 2026-10-12 脚手架连续升级误生成的字节级完全重复副本（与 `0011` SHA-256 逐字节相同，仓内零引用）。
5. 4 个一次性历史脚本（`deploy/dify-36/migrate_20261020.py`、`tools/verification/mutation-audit.py`、`tools/verification/model_samples_32.py`、`tools/verification/inspect_production_prompt.py`）均无任何代码或测试依赖。

## 决定

1. 从 `admin/common.py::_console_settings_from()` 移除 `AIOPS_GATEWAY_OPS_BINDINGS_FILE` 与 `AIOPS_GATEWAY_DIFY_TOOL_BINDINGS`；从 `gateway_config.py`、`gateway_config_groups.py`、`.env.example` 与 `tests/gateway/test_setting_sources.py` 移除上述 4 个零读取配置字段及 5 个零引用设备配置辅助函数。`GatewayServerSettings.from_env()` 按 `SETTING_SOURCES` 遍历取值，自动忽略现网 `.env` 中遗留的未知旧键，保持向后兼容。
2. 删除 `GatewayStore.prune_hop_tickets()`，修正 `gateway_store.py` 与 `async_job_lifecycle.py` 中过期的 `runs` 表注释。
3. 删除字节级重复的 ADR `0012`、`0013`（保留 `0011`）及上述 4 个零测试依赖的一次性脚本。
4. 在 `tests/gateway/test_setting_sources.py` 新增两条静态闭环守卫：
   - `test_every_setting_source_has_a_runtime_consumer`：要求 `SETTING_SOURCES` 的每个字段在配置定义文件之外存在至少 1 处生产引用；
   - `test_admin_console_settings_keys_match_gateway_server_settings_fields`：要求 `_console_settings_from()` 返回的键全集属于 `GatewayServerSettings` 字段并验证旧键不引发 `TypeError`。

## 备选方案

- **仅在文档中标记 deprecated 而不删除**：被否决。僵尸配置字段和虚假范围校验持续误导运维与新工程师，且无法消除 `AIOPS_GATEWAY_OPS_BINDINGS_FILE` 的运行期 `TypeError` 隐患。
- **不加静态闭环守卫、仅做一次性删除**：被否决。历史上 `#1125` 退役时正是因为缺少双向守卫，导致删了消费代码却留下配置定义与 CLI 读键。
- **同步清理 36/41 宿主机 `.env` 或删除仍在承重的 Tier 3 兼容层**：被否决。经只读核查，36 `bridge.env` 仍使用旧 `AIOPS_GATEWAY_DIFY_*` 键、41 仍使用 `AIOPS_GATEWAY_ENVIRONMENT_MANIFEST_FILE` 且 `agent_versions` 仍保留 v2–v5 历史版本，不在本 PR 触碰。
