# 361. 已上线客户环境 Day-2 零命令行版本升级与安全回滚（版本与发版页）

状态：已采纳；补充 ADR-0358（V2.1 自助开通），不改变 41 控制面 GitHub CD 发布流程及已有单问/健康报告只读边界。

## 上下文

ADR-0358 已实现新客户「5 步自助开通向导」，使 0 到 1 部署不再依赖手工 SSH 与脚本；但对于已上线并启用服务的客户环境（例如 `jiejia`），日常迭代仍存在四项断裂：

1. **发版与升级入口未收口进 Dify 管理面**：`main` 分支合入新能力并发布不可变标签（例如 `v1.43.19`）后，已上线客户机无法在「环境与租户」页直接发起升级或回滚，只能回退到开发机手工执行构建、投递离线包、覆盖 `venv`、`nginx -t && nginx -s reload` 与逐项验证。
2. **冷装编排不可直接用于原地升级**：`infra_provision` 会重写 `production.env`、`registry/environment.json`、`runtime/`、`dify-keys` 并重播 `gateway.db`，若误用于已上线客户机将破坏既有租户运行态或凭据。
3. **版本制品与客户侧 Nginx 入口规则脱节**：如 ADR-0360 引入 `/v1/health-report-jobs*` 等用户入口放行变更时，仅升级 Python wheelhouse 仍会令客户域名返回 `404`。
4. **缺少原地版本可观测与一键安全回滚面**：「环境与租户」总览看不到各客户环境实时运行版本、`nginx` 入口差异与可回滚版本目标。

## 决定

1. **版本来源严格收敛为 `main` 已发版不可变注释标签**
   - `release_governance` 仅接纳满足 SemVer（`vX.Y.Z`）、类型为 Git `tag` 对象（annotated tag）且其指向提交为 `origin/main` 祖先的正式发布版本；不开放任意未打标 commit SHA 的自助输入。
   - 由 `dev-host worker` 空闲周期同步最新正式版本清单及配套离线制品池信息至控制面 `release-catalog.json`（`0600` 私有账本，零路径与密钥泄露）。

2. **独立于冷装的无损隔离目录切换与自动回滚**
   - 在 `/v1/dify/management/environment-wizard` 及同源控制台桥新增 4 项受控动作：`release_overview`、`release_probe`、`environment_upgrade`、`environment_rollback`。
   - 控制面 `41` 拒绝经由该页面自升级或自回滚（稳定错误码 `ENVIRONMENT_CONTROL_PLANE_USE_CD`），`41` 继续走受保护 GitHub `cd.yml` 工作流。
   - 目标客户机升级采用隔离版本目录 `/opt/aiops-access-<env>/releases/<rev12>-<ts>/venv` 离线安装与预检，待 `aiops_diagnostics.__version__` 核对通过后，再经临时符号链接原子替换 `/opt/aiops-access-<env>/current`，并在 `installed.json` 中持久化 `previous_release` 供秒级 `environment_rollback`。
   - 升级前后强制计算并比对 `/etc/aiops-access-<env>/production.env`、`/etc/aiops-access-<env>/registry/environment.json` 与 `/etc/aiops-access-<env>/nginx-user-ingress-auth.conf` 的 SHA-256，确保客户配置、业务目录、运行态、`gateway.db` 与密钥零改动（`untouched_config_verified: true`）；任何切换失败均自动回退旧 `current`、`installed.json` 与 Nginx 备份并拉起原版本服务。

3. **受控 Nginx 入口按目标修订自动对比同步**
   - 直接从目标发布修订 `git show <revision>:ops/nginx/environment-user-ingress*` 渲染客户侧 `aiops-user-ingress.conf` 与 `environment-user-ingress-proxy.inc`（不碰客户主站点配置及 `nginx-user-ingress-auth.conf`）。
   - 前端升级前自动对比并预览路由差异（无变化 / 新增放行路由列表），服务端升级先备份原文件、覆盖并运行 `nginx -t`，待新版本进程 `/health` 返回 `ok: true`、目标 `version` 与 `business_mutations: disabled` 后再执行 `nginx -s reload`；最后强制运行公网 HTTPS 匿名 `401` + 客户原业务路由 `200` 冒烟校验。

4. **「环境与租户」新增「版本与发版」工作台**
   - 展示实时在线版本探测（「刷新实时版本」）、官方正式版本选择与 Nginx 路由差异预览、「一键无损滚动升级」（含 5 步流水线实时进度与零触碰/冒烟回执）以及「一键回滚至上一版本」（二次确认 + 3 步回滚进度）；所有回执继续诚实标明 `business_accuracy: unconfirmed`。

## 替代方案

- **复用 `infra_provision` 做线上客户升级**：会触碰 `production.env`、`registry/environment.json`、`runtime/` 与 `gateway.db`，风险过大，拒绝。
- **仅原地 `pip install --upgrade --force-reinstall` 覆盖单一 `venv`**：一旦新依赖或配置不兼容难以秒级恢复，缺少原子目录回滚能力，拒绝。
- **允许运维在页面手填任意 Git commit SHA 升级**：绕开正式版本发布与标签审批语义，容易把未定型提交推向客户机，按决策拒绝。

## 后果

- 已上线客户环境（如 `jiejia`）的日常小版本升级与回滚完全收口至 Dify「环境与租户 → 版本与发版」页面，零 SSH 命令行操作。
- 升级过程与回滚历史均进入异步任务账本及 `environment-releases.json` 审计记录，且受控 Nginx 入口扩展与代码升级版本保持一致。
