# 363. 客户机 Gateway 正规 OCI 容器镜像分发与无损切换（阿里云 ACR）

状态：已采纳；演进并强化 ADR-0358（自助开通）与 ADR-0361（Day-2 升级回滚），不改变 41 控制面 GitHub CD、只读诊断边界（`business_mutations: disabled`）及客户业务服务隔离原则。

## 上下文

ADR-0361 收口了已上线客户环境的版本选择、受控 Nginx 入口对比同步与原子回滚闭环，但制品交付仍采用「美国开发机构建 24 MiB 整包 `application-3.12.tar.gz` → 经 SSH/SCP 跨海推往中国客户机 → 目标机离线 `venv` 全量安装」的模式，存在四项结构性缺陷：

1. **跨海推送全量包脆弱且缓慢**：常规小版本迭代实际仅改动 ~400 KB 自研 Python 代码，却每次重复跨海传输 ~24 MiB 第三方依赖轮子包，晚高峰跨国链路丢包即拖慢或阻断升级。
2. **缺少中心化不可变镜像仓（Registry）**：未在 `main` 发布正式标签（`vX.Y.Z`）时即由 CI 一次性构建并推送不可变制品，无法实现「构建一次、各客户机国内就近秒拉（Build Once, Pull Locally）」。
3. **运行形态未与公司容器体系对齐**：公司自身业务组件（`cloud-*`、`tsdata`、`tdengine` 等）已统一基于 Docker 与阿里云容器镜像服务（ACR）分发；实测 `41` 与客户机（如 `jiejia`）均已运行 Docker。
4. **冷装与日常升级链路未同源统一**：新客户自助冷装与已上线客户 Day-2 升级仍分走整包 `installer.tar.gz + application.tar.gz` 两套推送路径，增加维护与故障面。

## 决定

1. **采用 OCI 标准容器镜像分发 + 不可变 Digest 锁定（`@sha256:...`）+ 单一发布入口**
   - 统一由 [`ops/environments/gateway/publish-release-image.sh`](../../ops/environments/gateway/publish-release-image.sh) 负责构建、非 root 只读冒烟校验、推送至公司阿里云 ACR 并挂载 `gateway-image.json` 至 GitHub Release；日常 [`.github/workflows/cd.yml`](../../.github/workflows/cd.yml) 与人工补发 [`.github/workflows/release-gateway-image.yml`](../../.github/workflows/release-gateway-image.yml) 共用同一脚本。
   - 镜像元数据唯一真源为 GitHub Release 附件 `gateway-image.json`（同步写入 `41` 的 `release-catalog.json`），严格校验 `@sha256:[0-9a-f]{64}`。

2. **直接启用公司 ACR，凭单配置保留迁移能力**
   - 通过配置抽象解耦镜像仓地址与拉取凭据：CI 侧读取仓库变量 `AIOPS_GATEWAY_IMAGE_REPO` 与机密 `AIOPS_ACR_USERNAME` / `AIOPS_ACR_PASSWORD`；控制面 `41` 侧读取 `0600` 私有账本 `/var/lib/aiops-41/onboarding-assets/registry-config.json`（由 `ops/environments/gateway/configure-registry.sh` 安全写入，明文密码永不进入仓库、命令行参数或管理面 API）。
   - 直接启用公司企业版 ACR `qushi-registry.cn-hangzhou.cr.aliyuncs.com/release/aiops-gateway`（依托受控 RAM 推送账号及客户机既有公司 ACR 预登录态），若配置了独立拉取凭证则隔离写入 `/etc/aiops-access-<env>/docker-config/config.json`（绝不触碰 `/root/.docker/config.json`）。

3. **两层缓存友好镜像（常规升级仅拉自研小层）**
   - `ops/environments/gateway/Dockerfile` 基于 `python:3.12-slim` 在 `/opt/aiops/venv` 构建两层产物：依赖底座层（锁定第三方 wheel）与自研 `aiops_diagnostics` 应用层。

4. **平铺直跑的宿主容器契约（去掉中间壳脚本 + 零配置改动）**
   - `aiops-access-<env>.service` 通过 `EnvironmentFile=/opt/aiops-access-<env>/container.env` 直接调用 `/usr/bin/docker run` 启动单容器（去掉 `run-container.sh` 转接层，运维执行 `systemctl cat` 一屏可见完整参数）：
     - `--network host`、`--user <uid>:<gid>`、`--read-only`、`--tmpfs /tmp:rw,noexec,nosuid,size=64m`、`--security-opt no-new-privileges:true`、`--cap-drop ALL`、`--memory 384m --cpus 0.5 --pids-limit 64`；
     - 挂载只读配置 `/etc/aiops-access-<env>:/etc/aiops-access-<env>:ro` 与可写数据卷 `/var/lib/aiops-access-<env>:/var/lib/aiops-access-<env>:rw`。
   - 客户机操作抽离至纯标准库脚本 [`ops/environments/gateway/host_release_agent.py`](../../ops/environments/gateway/host_release_agent.py)，经 SSH stdin 投递执行，控制面编排独立收口在 [`src/aiops_diagnostics/onboarding/release_worker.py`](../../src/aiops_diagnostics/onboarding/release_worker.py)。

5. **全线单轨容器冷装、升级与回滚（正式退役 `venv` 跨海整包分支）**
   - 在 `jiejia` 完成 `v1.43.22` 容器切换后，新客户冷装、`environment_upgrade` 与 `environment_rollback` 均只支持 OCI `@sha256` 容器轨道，不再保留 24 MiB 跨海 `tar.gz` 与 `venv ↔ docker` 双轨分支。
   - 升级继续执行 `production.env`、`registry/environment.json` 与 `nginx-user-ingress-auth.conf` 前后 SHA-256 零触碰校验与 Nginx 同步，任何异常自动原子回退至上一容器镜像摘要与 Nginx 备份。

## 替代方案

- **继续跨海 SCP 24 MiB tarball 并拆 `deps + wheel`**：仍需开发机常驻打包传输、客户机保留编译态/安装脚本，且未能与公司 ACR + Docker 运行体系对齐。
- **使用 GitHub Releases / GHCR 作为国内客户机主拉取源**：跨境网络直连晚高峰丢包率高，不适合生产交付。
- **把 AI-Ops 凭据直接合并进客户机 `/root/.docker/config.json`**：存在破坏客户原有公司 ACR 凭证的风险，违背最小隔离原则，拒绝。

## 后果

- 客户机常规小版本升级耗时由分钟级跨海传输缩短为国内秒级镜像拉取，Dify「版本与发版」可清晰观测容器运行态与 `@sha256` 镜像摘要。
- 若目标历史版本尚未发布 OCI 镜像或目标机未安装 Docker，引擎自动回退至 ADR-0361 既有隔离 `venv` 路径，保证向后兼容。
