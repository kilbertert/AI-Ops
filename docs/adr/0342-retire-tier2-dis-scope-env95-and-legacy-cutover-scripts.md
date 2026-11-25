# 342. 退役零生产调用的 DIS 代查栈、已撤回 env95 资产、历史切流脚本与旧诊断列表存储方法

状态：已采纳（2026-11-25）

## 背景

在完成 Tier 1 清理（ADR-0341）后，继续处理架构考古识别出的四组 Tier 2 历史遗留：

1. **Era 1 DIS 点位代查栈**（起源 `#78`）：`Settings.dis` / `DisSettings`、`DisDirectory` / `DisHttpDirectory` 与 `MySQLSource.site_ids_by_points()` 在全仓生产代码 15 处 `resolve_query_scope(...)` 调用中零处传参、零处实例化，且 41 生产配置中 `AIOPS_DIS_BASE_URL` 为空、`AIOPS_DIS_TOKEN` 未配置。
2. **已撤回的 `ops/environments/env95/` 目录**（13 文件 + 2 个专属单测）：已于 2026-11-14 随 ADR-0260 / ADR-0263 正式撤回，被正式受管安装器 `ops/environments/gateway/` 替代，仅留存可能被误当新环境模板的历史脚本。
3. **已完成的一次性生产切流脚本**：`deploy/d4-cutover.py`（2026-10-10 已执行完毕的一次性 D-4 Nginx 切流脚本）。同属已失效脚本的 `deploy/routing-window.sh` 因关联测试 `tests/test_cd_deploy_scripts.py` 正被并行工作树 `fix/release-by-dispatch` 占用，按多工作树协作边界延后至该分支合入后单独清理。
4. **零生产调用的旧诊断列表存储方法**：`GatewayStore.list_standard_diagnoses()` 与 `count_standard_diagnoses()` 随 `/v1/standard/diagnoses` 列表面退役后无任何生产调用，仅在 3 个测试文件中残存桩调用。

## 决定

1. 移除 `common/config.py` 中的 `DisSettings` / `Settings.dis`、`.env.example` 中的 `AIOPS_DIS_*`（并在 `tests/test_env_example.py` 纳入退役前缀守卫）、`identity/query_scope.py` 中的 `DisDirectory` / `DisHttpDirectory` / `site_ids_by_points` 与 `data/sources.py` 中的 `site_ids_by_points()`。
   - **安全不变量保持**：`resolve_query_scope()` 对非 `self` 范围的 `context.delegated and context.subject.c_user_id` 仍显式以 `ScopeError(..., code=SCOPE_ERROR_EMPTY_SCOPE)` fail closed，绝不静默扩大查询范围。
2. 删除已撤回的 `ops/environments/env95/` 全目录及其专属单测 `tests/tooling/test_env95_{node_runtime,upgrade}.py`，同步清理 `llmdoc/deployment/delivery-and-hosts.mdx` 中的失效文件列表（撤回决策与历史事实保留在 Git 历史与 ADR-0260/0263）。
3. 删除 `deploy/d4-cutover.py` 与 `tests/dify/test_dify_entry_cutover.py`，同步更新 `tests/test_repository_layout.py` 与 `deploy/README.md`（不触碰并行分支占用的 `tests/test_cd_deploy_scripts.py` 与 `deploy/routing-window.sh`）。
4. 删除 `GatewayStore.list_standard_diagnoses()` 与 `count_standard_diagnoses()`，清理 3 个测试文件中的冗余桩调用。

## 备选方案

- **在 `resolve_query_scope()` 中移除 `context.delegated` 分支而不再 fail closed**：被否决。若未来或测试路径传入 `delegated=True`，直接忽略会导致查询范围静默放宽为调用者自身范围；保留显式 `ScopeError` fail closed 既零成本又守住安全边界。
- **保留 `ops/environments/env95/` 作为历史恢复目录**：被否决。该试验已于 2026-11-14 撤回且 95 本身不在编队内；Git 历史与 ADR-0260/0263 已完整留档，继续留在 `ops/environments/` 下只会与现役 `ops/environments/gateway/` 混淆。
- **同 PR 一并移除 Tier 3（`consumer/legacy_callbacks.py`、`dify-console-bridge` 旧环境变量兜底、`environment_manifest_file` 等）**：被否决。经 41/36 现网只读核查，36 `bridge.env` 仍在使用 `AIOPS_GATEWAY_DIFY_{PUBLISH,TRIAL}_API_KEY`，41 `gateway.env` 仍配置 `AIOPS_GATEWAY_ENVIRONMENT_MANIFEST_FILE`，且 41 `agent_versions` 仍留存 18 条 `callback_version` 2–5 历史版本供显式 `/v1/dify/restore` 回滚，暂不强行拆除。
