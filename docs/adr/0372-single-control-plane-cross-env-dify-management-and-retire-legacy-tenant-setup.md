# 372. 单控制面跨环境 Dify 工作区与提示词受管发布，并彻底退役旧客户开通顶栏

状态：已采纳；打通 `41` 控制面对远端独立客户机（如 `jiejia`）的 Dify 工作区进入、知识库直达与双端提示词受管发布/回滚闭环，并彻底退役 Dify 首页旧「复制并开通客户 AI 服务」横幅与后端任务接口。

## 上下文

真实客户环境 `jiejia` 已通过 V2.1 开通向导部署上线并拥有独立 Dify 工作区（`aiops-6-jiejia-1990990768688599040`）与双端应用（`aiops-operator-jiejia`、`aiops-consumer-jiejia`），但运营与实施人员在 Dify 控制台完全无法定制并发布 `jiejia` 的提示词，根因有三：

1. **架构错位**：早期多环境登录设计（ADR-0276）假设每个业务环境自带全套 Dify 管理私钥；而现实落地的是「中央控制面 `41` + 轻量化客户机数据面 `jiejia`」，客户机不应也不持有 `36` Dify 管理员私钥。登录页只显示单选项 `41生产环境` 徒增困惑。
2. **跨环境工作区与受管发布未贯通**：`41` 的 `self_service.py`、`knowledge.py` 与 `dify_workspaces.py` 均按本环境硬编码，导致拥有 `managers: ["*"]` 的平台管理员无法切入 `jiejia` 工作区/知识库；且 `jiejia` 的管家端（`ENGINE_NODE`）与客户端（`ENGINE_DIFY`）运行路由位于客户机 `/var/lib/aiops-access-jiejia/gateway/gateway.db`，缺少受控远端发布与回滚闭环。
3. **旧入口与新工作台重叠**：Dify `/apps` 与首页顶部仍挂载早期「复制并开通客户 AI 服务」横幅（`TenantSetup` + `/v1/dify/management/catalogue` + `/operations`），与已投入使用的 `/aiops/environments` 5 步向导职责重叠。

## 决定

1. **单控制面跨环境工作区与知识库直达（最小权限）**：
   - `DifyWorkspaces.entry_for_workspace()` 支持按 `workspace_id` 跨环境反查唯一 `(tenant_id, environment)`；`self_service_identity.require_environment_manager()` 仅对控制面环境目录 `managers` 中显式授权的账号放行，普通租户用户仍严格隔离在本租户。
   - `/v1/dify/management/workspace`、`/workspace/member` 与 `/v1/internal/knowledge/open` 支持跨环境工作区入组、切换与 RAGFlow 知识库直达。
   - 在独立工作台 [aiops-environments.tsx](../../services/aiops-console/src/components/aiops-environments.tsx) 每个租户行直接提供「进入工作区」与「进入知识库」按钮。
2. **远端客户机受管发布/回滚以客户机 `gateway.db` 为唯一真相源**：
   - 新增 [remote_app_management.py](../../src/aiops_diagnostics/dify/remote_app_management.py) 并在 [host_release_agent.py](../../ops/environments/gateway/host_release_agent.py) 增加 5 个纯标准库 SQLite 子命令（`app-state`、`app-publish`、`app-versions`、`app-change-version`、`app-knowledge-grant`）。
   - 所有 Dify DSL 导出校验、模型/工具白名单编译与工作流发布留在 `41` 控制面，绝不向客户机扩散 Dify 私钥；客户机子命令打开 `gateway.db` 前降权至文件属主 UID/GID（避免 `root` 遗留 WAL/SHM 破坏容器写权限），以 `chat_route_bindings.revision` 乐观锁原子提交，客户机 Gateway 热读生效无需重启。
   - [wizard_provision.py](../../src/aiops_diagnostics/onboarding/wizard_provision.py) 同步将远端环境的管家端与客户端应用均登记至 `41` `operator_apps` 索引表。
   - Dify 发布条（`ManagedPublish` 与 `WorkflowManagedStrip`）显式展示 `[环境名 · 租户名]` 防呆徽标，远端发布/回滚触发二次确认，避免跨环境误发串台。
3. **彻底退役旧「复制并开通客户 AI 服务」并收敛登录页单环境展示**：
   - 移除 Dify 上游补丁中 `TenantSetup` 挂载及 `aiops-management.tsx` 中的旧横幅组件，同步删除边缘桥与控制面的 `catalogue` / `create` / `operation` / `retry` 旧接口。
   - [dify-login-page.html](../../ops/dify-login-proxy/dify-login-page.html) 在可用登录环境 `<= 1` 时自动隐藏冗余的「业务环境」单选项下拉框（保留协议兼容，未来开放多环境客户自登时自动显现）。

## 后果

- 平台管理员一次登录即可在 `/aiops/environments` 一键进入 `41`、`jiejia` 及未来所有客户环境的 Dify 工作区与 RAGFlow 知识库，修改提示词并热发布/回滚至对应客户机生产环境。
- `36` Dify 管理员私钥不向任何客户机扩散，客户机 `gateway.db` 保持单一真相源，消除双源漂移。
- Dify `/apps` 首页与上游补丁进一步瘦身，认知与长期维护成本显著下降。

## 备选方案

1. **向每台客户机下发 `36` Dify 管理员私钥并在 `/signin` 暴露多环境登录**：被否。会导致全平台 Dify 超级私钥随客户机数量线性扩散，且每次新增客户都要手工同步 `36` 凭据并重启 `login-proxy`（清空所有在线会话），运维与安全风险均不可接受。
2. **在 `41` 本地维护远端环境的 `chat_route_bindings` 镜像再异步同步到客户机**：被否。向导重跑、升级或网络中断极易造成控制面与客户机版本脑裂；直接经已钉扎 SSH 读写客户机 `gateway.db` 保持唯一真相源更简单可靠。
3. **保留 `/apps` 顶部「复制并开通客户 AI 服务」用于同环境多租户**：被否。新增客户已全面收敛至 `/aiops/environments` 5 步向导，继续保留两套平行开通代码只会增加测试、补丁与排障负担。
