# 373. 按业务环境独立登录、环境内租户/系统隔离与控制面自动目录同步

状态：已采纳；修订 [ADR-0372](0372-single-control-plane-cross-env-dify-management-and-retire-legacy-tenant-setup.md) 中「单一控制面账号跨环境混入多个客户工作区」的登录与工作区模型，保留 ADR-0372 的远端客户机 SSH 发布引擎与旧顶栏退役决策。

## 上下文

ADR-0372 上线后，真实产品验收暴露出两类高影响体验与安全边界问题：

1. **多环境工作区混排在同一个 Dify 账号下**：原方案让管理员统一登录 `41` 并在 `/signin` 隐藏环境下拉框，通过跨环境入组把 `jiejia` 与 `41` 的工作区装进同一个 `41-1454348737375174658@aiops-login.invalid` 账号。随着客户环境数量增加，不同环境、不同租户的工作区全挤在同一个 Dify 切换器里，既违背直觉也放大跨客户误操作风险；同时各客户环境自身的 `system` 与独立租户账号也无法通过 `/signin` 自主登录管理各自提示词。
2. **原有 `36` 多环境静态文件投影易漂移**：`36` 的 `login-proxy` 依赖手工维护本地 `registry.json` 与 `credentials.json`，导致通过向导新开通的 `jiejia` 未出现在 `/signin` 下拉框中；且客户业务库严守只读边界（无 `aiops:agent:*` 菜单行），需要既能放行各客户环境 `system` 与已开通租户账号，又不把 `36` Dify 管理员私钥或控制面写权限扩散给客户机。

## 决定

1. **恢复 `/signin` 业务环境选择与控制面自动目录同步**：
   - [dify-login-page.html](../../ops/dify-login-proxy/dify-login-page.html) 在多环境模式（`selection_required=true`）下始终展示「业务环境」下拉框，支持 `?environment=<id>` 预选中目标环境并兼容 `redirect_url` / `redirect` 回跳参数。
   - 复用 `41` 已暴露且受 HMAC 内部令牌保护的 `GET /v1/dify/workspace`（通过 `X-AIOps-Workspace-Environment: *` 与 `tenant_id=__catalogue__`）向 `36` `login-proxy` 同步控制面生效目录（纯元数据、零密钥），原子缓存于 `data_dir/synced-registry/registry.json`（15 秒节流，网络抖动平稳回退）。
   - `AIOPS_LOGIN_PROXY_AUTO_REGISTRY_ENVIRONMENTS=true` 与 `AIOPS_CONSOLE_BRIDGE_CONTROL_PLANE_MANAGED_ENVS=true` 启用后，凡在控制面目录登记了 `company_gateway_url` 的受管客户环境自动动态挂载登录与同源桥路由：登录验证码与 UPMS 令牌校验直连目标客户环境的 `company_gateway_url`，而工作区解析、提示词管理与远端发布统一收敛至控制面 `41`（同源桥强制校验目标地址等于本地配置的 `gateway_base_url`，绝不外泄控制面密钥）。
2. **严格按登录环境隔离 Dify 账号与工作区**：
   - Dify 账号按 `{env}-{company_user_id}@aiops-login.invalid` 与独立映射库 `data_dir/environments/<env>/mappings.db` 物理隔离；新开户加入目标业务工作区后主动调用 `/console/api/workspaces/switch` 切换活动工作区并移除不属于本环境的过渡工作区成员关系。
   - `41` 网关的 `self_service.authorize_app()` 与 `self_service.workspace_scope()` 强制同环境校验（跨环境调用直接返回 `403 SETUP_ENVIRONMENT_MISMATCH`），不再把控制面 `41` 账号加入客户环境工作区。
   - `41` 网关的 `self_service_identity.identity()` 按 `operator-token` 回传的 `environment` 路由到对应客户环境的 `company_gateway_url/upms` 核验令牌与租户清单，并对客户机 `knowledge/sync` 与 `knowledge/hop` 同步修正为按 `workspace_id` 解析目标环境，消除知识库二阶跳转与选库同步失配。
3. **环境内角色语义一致、控制面与客户环境边界清晰**：
   - **任意已启用环境 `E` 的 `system` 账号**（`username == "system"`、`user_type == "-1"`、`tenant_id == "-1"`）：在不写入客户业务库的前提下，受管客户环境仅对 `system` 超管放行编辑与发布准入（其余 `type == "-1"` 账号无显式权限码一律拒绝）；登录 `E` 时自动加入 `E` 全部已启用租户的工作区，并在 `/aiops/environments` 只读查看 `E` 及其名下全部租户（提供「进入工作区」「进入知识库」，屏蔽控制面写操作与向导）。
   - **任意已启用环境 `E` 的租户账号**：独立登录 `E`，仅进入自身租户工作区，按 ADR-0139 独立编辑/发布/回滚本租户提示词与知识库授权。
   - **控制面 `41` 全局管理员**：在 `/aiops/environments` 继续拥有全部环境的向导、版本发版与准入治理视图；查看本环境（`41`）租户行显示「进入工作区 / 进入知识库」，查看其他客户环境（如 `jiejia`）租户行改为显示「切换至此环境登录」（退出当前会话并跳转 `/signin?environment=<id>`）。

## 后果

- 不同业务环境拥有完全独立的登录入口、Dify 账号与工作区切换器，彻底消除跨环境租户混排；同时每个环境内「`system` 统筹本环境全部租户、各租户独立登录本工作区」的体验完全一致。
- 未来通过 5 步向导新上线任意客户环境并启用后，无需手工修改 `36` 的 `registry.json` 或 `credentials.json`，约 15 秒内自动出现在 `/signin` 下拉框即可用该客户 UPMS 账号登录。
- `36` Dify 管理员私钥仍只停留在 `41` 控制面，不向客户机扩散；客户业务库继续保持严格只读。

## 备选方案

1. **维持 ADR-0372 的单账号跨环境入组，仅靠前端分组折叠工作区**：被否。Dify 控制台原生工作区切换器与账号体系是全局平铺的，不仅难以彻底防呆，且无法支持各客户环境的 `system` 与独立租户账号自行登录。
2. **向客户业务库 `qumall_upms.sys_menu` 写入 `aiops:agent:*` 菜单行**：被否。违反客户业务数据库严格只读边界；改为仅在受管客户环境对 `username == "system"` 平台超管放行、对其他 `type == "-1"` 账号继续默认拒绝，兼顾最小权限与零侵入。
