# 部署索引

本目录是各部署件的成组资产。「部署到哪」的主机名册在仓库根 `.dev-host.toml`（主机细节只记逻辑名，不含凭据）。

**CD 只覆盖 `deploy-41.sh` 一条路径**（rsync `src/` 到 41）。其余一切——nginx 片段、H5 静态件、Java 节点、Dify 应用、插件——各走各的手工或独立程序，并在[手工部署台账](manual-deploys.md)登记。**「CI 绿了」不等于「已部署」**，确认生产状态以台账与各件 README 为准。

## 命名约定

- 名称里的数字是**主机逻辑名**：`41`=生产网关宿主，`36`=共享 Dify 宿主，`95`/`124`=其他业务环境；映射以 `.dev-host.toml` 为准。
- `mN-*` 形如 `m2-live-03` 的是**里程碑试验代号**，不是主机；`tools/experiments/` 下的 `m2_close`、`m3node` 等同理。
- 不带数字的通用名（`node-runtime/`、`kb-service/`）是生产件。

## 部署面总表

| 资产 | 目标 | 机制 | 性质 | 说明 |
|---|---|---|---|---|
| `deploy-41.sh` | 41 `/opt/aiops-41/src` | `dev-host` CLI rsync `src/`；CI 在 `.github/workflows/cd.yml` | 生产 | 唯一 CD 入口；`dev-host` 命令不在本仓，测试用 `tests/fixtures/dev-host-stub/` 顶替 |
| `manual-deploys.md` + `record-manual-deploy.sh` | 41 | 手工动作 + 台账登记 | 生产 | CD 之外到达生产的一切变更必须留痕 |
| `check-41-editable.sh` | 41 | 部署前置检查 | 生产 | 防 `uv sync` 静默替换 editable 安装 |
| `routing-window.sh`、`classify-remote-result.sh`、`test-rollback-classifier.sh` | 41 | 观察/回滚分类辅助 | 生产 | 见各文件头注释（`routing-window.sh` 待 `tests/test_cd_deploy_scripts.py` 并发分支释放后退役；已完成的 2026-10-10 `d4-cutover.py` 已随 ADR-0342 退役，历史脚本见 Git `a4cf295e:deploy/d4-cutover.py`） |
| `operator-h5.py`、`company-gitlab-api.sh` | 41 / 公司 GitLab | 专项部署辅助 | 生产 | 见各文件头注释 |
| `node-runtime/` | 41 及各环境 | compose + systemd 单元 | 生产 | Java 执行节点栈；`environment.md` 记独立环境节点配置 |
| `m2-live-03/` | 41（隔离网络与数据目录） | 固定镜像容器栈 | 试验 | 受控试运行栈，**运行栈不是生产的一部分**；但其中的 `build-image.sh` 同时是 `node-runtime/` 生产镜像的构建入口（见节点部署手册），生产对应物是 `node-runtime/` |
| `dify-36/` | 36 `/opt/dify` | docker-compose + 对上游 Dify 1.17.1 的源码补丁 | 生产 | 共享 Dify 宿主；补丁随上游升级需逐个重打 |
| `dify-chat/` | 36 Dify | DSL 应用定义 | 生产 | 权威状态在 Dify 控制台，仓内 DSL 是声明态，经 `aiops-admin pull-dify`/`reconcile` 对账 |
| `kb-service/` | 36 `/opt/ragflow-kb` | sha 锁定补丁 + 运维脚本 | 生产 | 目标服务源码不在本仓 |
| `ops/dify-console-bridge`、`ops/dify-login-proxy` | 36 | tarball + systemd + nginx | 生产 | 部署资产在 `ops/`，服务代码在 `src/aiops_diagnostics/dify/`；改行为改 src，不是 ops |
| `ops/dify-inner-api` | 转发器在 36，SSH 隧道单元在 41 | `build-bundle.sh` 打包同目录 `aiops-inner-api-forward.py` + `aiops-inner-api-tunnel.service` | 生产 | stdlib 转发 `127.0.0.1:15001→docker api:5001`；src 侧同名模块是它的**客户端**，不是同一件东西 |
| `ops/environments/` | 95、124、gateway 等 | 环境清单驱动的安装/升级/退役 | 生产 | 见 `ops/README.md` |
| `integrations/cpms-java/` | — | 无部署 | 搁置 | 唯一消费方 `/diag/*` 已退役（见其 README）；契约测试保留防漂移 |
| `plugins/consumer_knowledge` | 36 Dify 插件守护 | `dify-plugin` CLI 打包 | 生产 | 与 `src/.../consumer/consumer_knowledge.py` 是一组的两半 |

新增部署面先在本表登记归属；跨主机的暴露面或入口变化属于单独评审事项。
