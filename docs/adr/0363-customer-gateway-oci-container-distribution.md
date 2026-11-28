# 363. 客户机 Gateway 正规 OCI 容器镜像分发与无损切换（阿里云 ACR）

状态：已采纳；演进并强化 ADR-0358（自助开通）与 ADR-0361（Day-2 升级回滚），不改变 41 控制面 GitHub CD、只读诊断边界（`business_mutations: disabled`）及客户业务服务隔离原则。

## 上下文

ADR-0361 收口了已上线客户环境的版本选择、受控 Nginx 入口对比同步与原子回滚闭环，但制品交付仍采用「美国开发机构建 24 MiB 整包 `application-3.12.tar.gz` → 经 SSH/SCP 跨海推往中国客户机 → 目标机离线 `venv` 全量安装」的模式，存在四项结构性缺陷：

1. **跨海推送全量包脆弱且缓慢**：常规小版本迭代实际仅改动 ~400 KB 自研 Python 代码，却每次重复跨海传输 ~24 MiB 第三方依赖轮子包，晚高峰跨国链路丢包即拖慢或阻断升级。
2. **缺少中心化不可变镜像仓（Registry）**：未在 `main` 发布正式标签（`vX.Y.Z`）时即由 CI 一次性构建并推送不可变制品，无法实现「构建一次、各客户机国内就近秒拉（Build Once, Pull Locally）」。
3. **运行形态未与公司容器体系对齐**：公司自身业务组件（`cloud-*`、`tsdata`、`tdengine` 等）已统一基于 Docker 与阿里云容器镜像服务（ACR）分发；实测 `41` 与客户机（如 `jiejia`）均已运行 Docker。
4. **冷装与日常升级链路未同源统一**：新客户自助冷装与已上线客户 Day-2 升级仍分走整包 `installer.tar.gz + application.tar.gz` 两套推送路径，增加维护与故障面。

## 决定

1. **采用 OCI 标准容器镜像分发 + 不可变 Digest 锁定（`@sha256:...`）**
   - 官方发布工作流（`.github/workflows/release-gateway-image.yml`）在推送或触发 `origin/main` 注释标签 `vX.Y.Z` 时自动构建 `linux/amd64` Docker 镜像，执行非 root 只读冒烟核验（确认 `aiops_diagnostics.__version__` 与提交一致且可执行），推送至国内阿里云 ACR，并将不可变 `@sha256:...` 摘要清单 `gateway-image.json` 挂载至对应 GitHub Release。
   - 控制面 `release-catalog.json` 与客户机执行引擎严格校验 `@sha256:[0-9a-f]{64}` 摘要引用，禁止 `:latest` 或仅凭漂移 tag 名拉取部署。

2. **独立阿里云 ACR 先行，凭单配置无缝切入公司 ACR**
   - 通过配置抽象解耦镜像仓地址与拉取凭据：CI 侧读取仓库变量 `AIOPS_GATEWAY_IMAGE_REPO` 与机密 `AIOPS_ACR_USERNAME` / `AIOPS_ACR_PASSWORD`；控制面 `41` 侧读取 `0600` 私有账本 `/var/lib/aiops-41/onboarding-assets/registry-config.json`（由 `ops/environments/gateway/configure-registry.sh` 安全写入，明文密码永不进入仓库、命令行参数或管理面 API）。
   - 直接启用公司企业版 ACR `qushi-registry.cn-hangzhou.cr.aliyuncs.com/release/aiops-gateway`（依托已开通的受控 RAM 推送账号及客户机既有公司 ACR 预登录态），并保留后续凭单配置切换到任意合规国内 ACR 的零代码迁移能力。
   - **隔离客户机既有 Docker 认证**：当 AI-Ops 需要独立拉取凭据时，仅写入 `/etc/aiops-access-<env>/docker-config/config.json`（目录 `0700`、文件 `0600`、`root` 拥有），通过 `DOCKER_CONFIG` 环境变量注入；**严禁覆盖客户机 `/root/.docker/config.json`**（保留客户既有公司 ACR 认证）。

3. **两层缓存友好镜像（常规升级增量 ~400 KB）**
   - `ops/environments/gateway/Dockerfile` 基于 `python:3.12-slim` 在 `/opt/aiops/venv` 构建两层产物：
     - **依赖底座层**：由 `uv export --frozen --no-dev --no-editable --no-emit-project` 安装锁定的第三方 wheel；`uv.lock` 不变时跨版本完全复用。
     - **自研应用层**：安装带 `<VERSION>+<rev12>` 的 `aiops_diagnostics` wheel（~400 KB），升级时客户机仅拉取此小层与 Nginx 模板（`< 3 KB`）。
   - 统一执行 `chmod -R a+rX /opt/aiops`，确保容器以项目专属非 root 数字 UID/GID 运行在只读根文件系统下。

4. **宿主兼容容器运行契约（`systemd` 托管 + 零配置改动）**
   - 保留 `aiops-access-<env>.service` 作为宿主唯一起停控制边界，托管 `/opt/aiops-access-<env>/run-container.sh` 启动的单容器 `aiops-access-<env>`：
     - `--network host`（与原裸进程完全一致：继续监听 `127.0.0.1:<port>` 并直连宿主 Nginx、`127.0.0.1` Redis/`tsdata` 与内网 MySQL，无需修改 `production.env`）；
     - `--user <uid>:<gid>`（使用既有 `aiops-access-<env>` 数字 UID/GID，延续原有 `0600`/`0700` 文件属主与权限边界）；
     - 只读加固：`--read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m --security-opt no-new-privileges:true --cap-drop ALL --memory 384m --cpus 0.5 --pids-limit 64`；
     - 挂载只读配置 `/etc/aiops-access-<env>:/etc/aiops-access-<env>:ro` 与可写数据卷 `/var/lib/aiops-access-<env>:/var/lib/aiops-access-<env>:rw`。
   - 新客户 `infra_provision` 冷装在目标机存在 Docker 且最新官方版已发布 OCI 镜像时，直接走容器原生冷装（不再跨海传 24 MiB 包）；存量 `venv` 客户机（如 `jiejia`）升级到含 OCI 镜像的新版本时原地平滑切换为容器态。

5. **双向兼容的原子回滚与零触碰核验**
   - 升级继续执行 `production.env`、`registry/environment.json` 与 `nginx-user-ingress-auth.conf` 前后 SHA-256 零触碰校验（`untouched_config_verified: true`）以及受控 Nginx 入口对比同步。
   - 若升级后本地 `/health`（`ok: true`、精确版本、`business_mutations: disabled`）或零触碰检查失败，自动恢复原容器 `container.env`（`docker -> docker`）或原 `venv` systemd 单元（`venv -> docker`）及 Nginx 备份。
   - 手动一键 `environment_rollback` 支持 `docker -> docker`、`docker -> venv` 与存量 `venv -> venv`。

## 替代方案

- **继续跨海 SCP 24 MiB tarball 并拆 `deps + wheel`**：仍需开发机常驻打包传输、客户机保留编译态/安装脚本，且未能与公司 ACR + Docker 运行体系对齐。
- **使用 GitHub Releases / GHCR 作为国内客户机主拉取源**：跨境网络直连晚高峰丢包率高，不适合生产交付。
- **把 AI-Ops 凭据直接合并进客户机 `/root/.docker/config.json`**：存在破坏客户原有公司 ACR 凭证的风险，违背最小隔离原则，拒绝。

## 后果

- 客户机常规小版本升级耗时由分钟级跨海传输缩短为国内秒级镜像拉取，Dify「版本与发版」可清晰观测容器运行态与 `@sha256` 镜像摘要。
- 若目标历史版本尚未发布 OCI 镜像或目标机未安装 Docker，引擎自动回退至 ADR-0361 既有隔离 `venv` 路径，保证向后兼容。
