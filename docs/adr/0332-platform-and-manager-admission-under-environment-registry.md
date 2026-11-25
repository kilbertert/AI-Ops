# 332. 启用运行目录后的平台账号与目录管理员准入边界

状态：已采纳；补充 ADR-0256 与目录 schema 2 授权边界（#1530）在控制环境启用 `environment_registry_file` 后的控制台登录与自助管理准入语义，不放宽业务租户或运行目录隔离。

## 背景与实证

2026-11-25 41 控制环境向唯一运行目录切换前只读盘点发现：

- 36 宿主 `aiops-dify-login-proxy.service` 的 `/var/lib/aiops-login-proxy/mappings.db` 当前活跃用户含 1 名公司平台管理员（`tenant_id='-1'`、`user_type='-1'`）及 2 个业务租户的 3 名租户账号；41 中央目录 `registry.json`（schema 2）中的唯一全局目录管理员亦为该平台主体 `(41, -1, <user_id>)`。
- 既有 `LoginService.require_registered_tenant` 与 `self_service_identity.identity(..., require_admission=True)` 对所有主体一律调用 `EnvironmentRegistry.require(tenant_id)`，要求 `tenant_id` 作为已启用项出现在 `row["tenants"]` 中。
- 业务租户目录只登记真实客户租户，绝不应将 `-1` 作为虚假业务租户写入 `tenants`。一旦为 41/36 启用 `AIOPS_GATEWAY_ENVIRONMENT_REGISTRY_FILE` 与 `AIOPS_LOGIN_PROXY_ENVIRONMENT_REGISTRY_FILE`，平台管理员将被立即锁死在 Dify 登录、会话交换和全部自助管理入口之外；同时，若控制环境在目录中被停用，显式授权的目录管理员（`managers`）也无法继续通过控制台桥访问环境管理面执行恢复。

## 决定

1. **`EnvironmentRegistry` 区分三类只读判据，均在单次原子读取与配置不变校验（`_load_entry`，含 `gateway_url`、`cpms_base_url`、`config_ref`、`company_gateway_url`）下执行**：
   - `require(tenant_id)`：要求本环境已启用且该业务租户已启用（原业务准入语义不变）。
   - `require_enabled()` / `enabled_tenant_ids()`：要求本环境已启用（供真实平台哨兵账号与自助目录交集过滤使用）。
   - `is_manager(tenant_id, user_id)`：仅在 `schema == aiops-environment-registry/2` 时核对 `(self.environment, tenant_id, user_id)` 是否显式出现在 `managers` 中；文件损坏、环境缺失或连接配置（含 `company_gateway_url`）漂移一律抛 `RegistryError` 失败关闭。
2. **平台哨兵账号准入（`user_type == "-1"` 且 `tenant_id == "-1"`）**：
   - 登录代理（`LoginService.login` / `active_session`）与自助管理身份（`self_service_identity.identity`）仅当两个条件同时满足且均来自公司 UPMS 实时核验时调用 `require_enabled()`；仅 `tenant_id == "-1"` 但非平台类型、或 `user_type == "-1"` 但持业务租户编号的账号均走 `require(tenant_id)` 拒绝。
   - 登录代理只在 `environment_registry` 已配置且 `identity.tenant_id == "-1"` 时提前调一次 `read_user_type`（并复用其结果，不增加 UPMS 往返，也不为被拒业务租户增加上游调用）。
   - `/v1/dify/management/catalogue` 在启用运行目录时将可选租户、工作区与来源列表与本环境 `enabled_tenant_ids()` 求交集，同时保留提交侧 `self_service_identity.require_target` 对具体业务租户的独立 `require(target_tenant_id)` 强校验。
3. **显式目录管理员在停用环境下的恢复通道与业务隔离**：
   - 无论单环境还是多环境模式，`LoginService.login` 一律拒绝停用环境的新登录（`allow_manager=False`），不向停用环境签发新的全权 Dify 编辑会话；仅对已登录的显式 `managers` 活动会话在 `active_session` 与 `LoginEnvironments.session`（`select(..., allow_disabled=True)`）保留目录恢复通道（仍强核 `company_gateway_url` 与 `_configuration` 漂移；无活动会话时须经受管 CLI `aiops-admin environment-registry` 恢复）。
   - `/__aiops/publish/check` 另按 `allow_manager=False` 计算 `business_admitted`：环境或业务租户停用时，`allowed` 强制为 `False`、`reason="TENANT_ENVIRONMENT_NOT_ENABLED"`、`business_admitted=False`。
   - 控制台同源桥 `_select_environment` 在 `business_admitted is False` 时仅放行 `/__aiops/manage`，对其余业务路由（`/__aiops/publish`、`/__aiops/retract`、`/__aiops/restore`、`/__aiops/versions`、`/__aiops/trial*`、`/__aiops/operator-preview*`、`/__aiops/knowledge-open`、`/__aiops/knowledge-sync`）一律返回 403。
   - `/__aiops/manage` 进入 Gateway 后，目录管理端点（`environment_management.py`）使用 `require_admission=False` + 实时公司身份与 `managers` 范围核对，而普通应用自助管理端点（`self_service_api.py`、`operator_run_logs_api.py`）继续使用 `require_admission=True`（不豁免 `managers`），在环境停用时一律返回 403 `TENANT_ENVIRONMENT_NOT_ENABLED`。

## 被否方案

- **把 `"-1"` 作为普通租户写入 `registry.json` 的 `tenants` 列表**：污染唯一业务租户目录、让 `-1` 出现在环境与租户页面和业务取址/工具准入中，且停用环境时仍无法保留目录管理员恢复通道。
- **凡 `user_type == "-1"` 一律跳过目录校验**：平台类型账号若归属具体业务租户（非哨兵 `-1`）或环境本身已停用，无条件放行会绕过停用开关，且违反「公司 `type=-1` 不自动授予目录管理权」的已批准边界（#1530）。
- **允许目录管理员在停用环境通过 `login` 签发新 Dify 会话**：Dify 原生控制台不查 AI-Ops 目录，签发新会话会直接重新开放停用环境的应用编辑面。
- **在 `self_service_identity.identity(require_admission=True)` 中也豁免 `managers`**：会使目录管理员在停用环境期间仍可操作普通应用开通、发布、试运行和知识同步，扩大停用态暴露面。
- **只在登录代理放行 `managers` 而不区分 `business_admitted`**：会导致停用环境中的目录管理员通过同源桥继续命中传统 `/__aiops/publish` 或 `/__aiops/trial` 路由，破坏「停用后业务动作关闭」的语义。

## 验证范围

- 单测与真实本机 HTTP 集成测试覆盖 `EnvironmentRegistry.require_enabled/is_manager`、单/多环境登录代理、控制台桥与 `self_service_identity` 的正反例（见 `D48`）。
- 本地替身不能证明真实 36/41 公司 UPMS 登录、Dify Cookie 交换或生产切换窗口，须在 41 切换前后的真实环境复核。
