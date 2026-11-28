# 367. 将 `36` 宿主双侧边车收敛为单一共享 ACR 镜像（双容器身份隔离）与统一架构入口

状态：已采纳；取代原先两套独立的 `tar.gz` / 散落源码手工同步方式，将 `dify-console-bridge` 与 `dify-login-proxy` 收敛到公司阿里云 ACR 不可变 `@sha256` 镜像 `aiops-dify-edge`。

## 上下文

在 ADR-0365（`dify-web` 走公司 ACR 发布）和 ADR-0366（退役 Nginx `sub_filter` 脚本）落地前，移动云 `36` 宿主机上的 Dify 边缘层由多套形态各异的脚本拼装而成：

1. `dify-console-bridge` 通过 `ops/dify-console-bridge/build-bundle.sh` 打 `tar.gz` 再经 `dev-host cp` 传包、宿主 systemd 跑 `/usr/bin/python3 -m dify_console_bridge`；
2. `dify-login-proxy` 依赖宿主 `/usr/bin/python3` 全局预装的 `cryptography`（曾手工拷贝 `/opt/aiops-login-proxy/aiops_diagnostics/...`，甚至与仓库内最新 `services/dify-login-proxy/dify_login_proxy` 包名错位）；
3. 交付与排障文档分散在 `deploy/dify-36/README.md`、`ops/dify-console-bridge/README.md`、`ops/dify-login-proxy/README.md`、`ops/dify-inner-api/README.md` 等多份长文，新接手工程师难以一眼判断真实运行拓扑与发布入口。

## 决定

1. **构建单一只读 OCI 镜像 `aiops-dify-edge`（公司阿里云 ACR `@sha256`）**
   - 在 [`deploy/dify-36/edge/`](../../deploy/dify-36/edge/Dockerfile) 维护 `Dockerfile`、`build-edge-image.sh`、`publish-edge-image.sh` 与 `deploy-edge-image.sh`。
   - 镜像基于 `python:3.12-slim`，仅固定安装唯一第三方运行时依赖 `cryptography==46.0.7`，并打入 `dify_console_bridge`、`dify_login_proxy`、`aiops_diagnostics` 所需最小公共/协议子包及边缘静态配置。
   - 新增 `.github/workflows/release-dify-edge-image.yml`，在 `main` 变更相关源码或配置时自动构建、非 root 离线冒烟校验并推送至 `AIOPS_DIFY_EDGE_IMAGE_REPO`，产出不可变 `@sha256` 元数据 `dify-edge-image.json`。

2. **运行时严格保持双容器、双 OS 身份、双 env 与双数据卷隔离（不合并不降格权限）**
   - `deploy-edge-image.sh` 使用同一 `@sha256` 镜像分别启动两个独立容器，延续 ADR-0023 密钥隔离不变量：
     - `aiops-dify-console-bridge`：以 `aiopsbridge` UID/GID 运行，`--read-only --cap-drop ALL --security-opt no-new-privileges:true --network host`，读取 `/etc/aiops-console-bridge/bridge.env`，只读挂载 `/var/lib/aiops-console-bridge`。
     - `aiops-dify-login-proxy`：以 `aiopslogin` UID/GID 运行（持有 `DIFY_SECRET_KEY` 与 `COMPANY_AES_KEY`），`--read-only --cap-drop ALL --security-opt no-new-privileges:true --network host`，读取 `/etc/aiops-login-proxy/login-proxy.env`，读写挂载 `/var/lib/aiops-login-proxy`。
   - `aiops-inner-api-forward.service` 因需通过宿主机 `/var/run/docker.sock` 执行 `docker inspect` 追踪 `deploy-api-1`，继续保留为宿主机 systemd 单元。
   - 切换时自动关闭/禁用对应旧 systemd 服务以避让 `10118` / `10119` 端口；若新容器启动或本地回环/同源探活失败，则自动回滚至上一版本容器镜像（或首次迁移时恢复旧 systemd 服务）。

3. **收敛唯一架构总览入口 `deploy/dify-36/ARCHITECTURE.md`**
   - 将 `36` 全部组件拓扑、身份隔离矩阵和 ACR 升级/回滚命令收敛进 [`deploy/dify-36/ARCHITECTURE.md`](../../deploy/dify-36/ARCHITECTURE.md)，并在 4 份历史 `README.md` 顶部增加统一导读指针。

## 替代方案

- **将 `console-bridge` 与 `login-proxy` 合并进同一个运行时进程/容器**：会把 `DIFY_SECRET_KEY` 与 `COMPANY_AES_KEY` 暴露给发布桥进程空间，直接违反 ADR-0023 最小权限隔离原则，拒绝。
- **为两只侧边车分别维护两份 ACR 仓库与两条 CI 流水线**：两只侧边车共享 `python:3.12-slim`、`cryptography` 与 `aiops_diagnostics.common/*`，拆两个仓库会加倍镜像构建与日常发版认知成本，拒绝。

## 后果

- **统一分发范式**：Dify 相关全部交付物（`aiops-dify-web`、`aiops-dify-edge`）与 Gateway 一致，均走 GitHub Actions → 阿里云 ACR 不可变 `@sha256` → 目标主机拉取切换与自动回滚。
- **零环境漂移**：`dify-login-proxy` 不再依赖宿主 Python 隐式站点包或历史目录名错配，本地 `build-edge-image.sh` 即可复现线上完全一致的运行容器。
