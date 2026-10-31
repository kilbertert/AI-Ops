# 41 变更单（M2-LIVE-03 受控试运行栈）

**这份文件是变更清单，不是授权。** 在取得对 41 的明确授权之前不执行任何一条 ——
不 `up`、不 `--purge`、不发真实请求。地址、指纹、凭据路径都在私有运维记录里。

## 0. 先决条件（部署前必须成立）

| 项 | 要求 |
|---|---|
| 合并状态 | 部署件已合入受保护默认分支；本节变更只针对合并后的制品 |
| 制品来源 | Gateway 镜像由 `build-image.sh` 在开发机/runner 构建并 `docker save`，**宿主不下载依赖** |
| 基础镜像 | 41 上 docker.io 直连不可达（实测 `i/o timeout`）；用宿主既有加速前缀 `docker.1ms.run` / `26agah86.mirror.aliyuncs.com`。**不改** `/etc/docker/daemon.json`；已拉取的镜像保留，不盲目 `rmi` |
| 余量 | `checks.sh preflight` 报 ≥3892 MiB 可用（2992 MiB 栈预算 + 900 MiB 余量） |

## 1. 新增宿主身份与目录

| 项 | 值 | 说明 |
|---|---|---|
| 服务身份 | **复用 `aiops41`**（uid 999 / gid 987） | 与生产 Gateway 同属 AI-Ops 项目；两者不共享任何可写路径。5 个非 store 服务容器**以 999:987 运行**，不是 root |
| 分发目录 | `/opt/aiops-m2-live/{platform,node}` | `aiops41:aiops41`，`0750`；只放只读制品 |
| 配置目录 | `/etc/aiops-m2-live/{gateway,node,platform,model-proxy,secrets}` | `aiops41:aiops41`，目录 `0750`；文件 `0600` |
| 状态目录 | `/var/lib/aiops-m2-live/{store,gateway,node,platform}` | `aiops41:aiops41`，`0700`（`store` 由 MySQL 入口点初始化为 `999`） |
| 所有权标记 | `/var/lib/aiops-m2-live/.owner` | `checks.sh up` 写入项目名；`down --purge` 只认它 |

隔离 Gateway 只读挂载另行生成的业务依赖白名单 `runtime.env`；不挂生产配置目录或
生产 `keys/`（其中可能含供应商凭据）。生产状态根 `/var/lib/aiops-41` 不挂入任何试运行容器。

## 2. 端口与网络

| 端口 | 绑定 | 用途 | 公网 |
|---|---|---|---|
| 8794/tcp | `127.0.0.1` | 隔离 Gateway（宿主唯一映射端口） | 否 |
| 8900/tcp | 容器网络内 | 平台 HTTPS | 否 |
| 18888/tcp | 容器网络内 | 平台 gRPC | 否 |
| 18889/tcp | 容器网络内 | 节点 gRPC | 否 |
| 6379/tcp | `hostloop` 网络内 | `gateway-host-relay` → 宿主回环 Redis | 否 |
| 9101/tcp | `modellink` 网络内 | 受控模型出口 HTTPS | 否 |

四张网络：`trial`（`internal: true`，无出站 NAT）、`modellink`（`internal: true`，
**只有** node 与 model-proxy）、`egress`（平台、模型出口、Gateway 外联）、`hostloop`
（**只有** gateway 与 host-relay）。节点只接两张 internal 网络。

**生产 Redis 监听不变**（仍是宿主 `0.0.0.0:6379` 的既有容器）。本栈不打开、不代理它的
公网访问；中继只把宿主回环映射进一张仅 Gateway 可见的网络。

## 3. 容器与资源

| 容器 | 镜像 | 内存 | CPU | 网络 | 运行 UID |
|---|---|---:|---:|---|---|
| `m2live03-store` | mysql:8.0 | 700 MiB | 0.5 | trial | 入口点 root → mysqld 999 |
| `m2live03-platform` | eclipse-temurin:21-jdk | 1200 MiB | 0.40 | trial + egress | 999:987 |
| `m2live03-node` | eclipse-temurin:21-jdk | 500 MiB | 0.22 | trial + modellink | 999:987 |
| `m2live03-gateway` | `aiops-m2-live-03-gateway`（构建） | 400 MiB | 0.22 | trial + egress + hostloop | 999:987 |
| `m2live03-host-relay` | 同一个构建镜像 | 64 MiB | 0.05 | hostloop | 999:987 |
| `m2live03-model-proxy` | 同一个构建镜像 | 128 MiB | 0.10 | modellink + egress | 999:987 |

合计 **2992 MiB / 1.49 CPU**，含辅助中继与受控模型出口。41 当前 MemAvailable 约 6.4 GiB。
生产 Gateway 延迟/错误或宿主内存压力是停机条件，不用 swap 顶。

## 4. 制品身份

| 制品 | 身份 |
|---|---|
| `snail-ai-server-exec.jar` | `8dd6b63504eae7cc479e1c3a7d674aa1529069d44e7967e4ed2158ef645d5cb0` |
| `aiops-execution-node.jar` | `6ffad7414d2be9e9d7b38664efbf3091b726b98891373f7ee7c85f37dec6ea53`（管家CPMS工具；源码 `ae290a0d`，固定构建时间戳；源码摘要见 `images.lock.json` 的 `node.source_sha256`） |
| 平台源码 commit | `10c1d3e50b701be62da23a3403d79a41187ab8cd` |
| Gateway 镜像 | 构建/分发件 `gateway-image/identity.json` 中的 Image ID；`images.lock.json` 中的内容摘要与镜像 `m2live.source` 标签一致 |
| 依赖 | `gateway-requirements.txt`，由 `uv export --frozen` 从 `uv.lock` 生成，每条带 `--hash` |

preflight 对**镜像 ID**、`m2live.source` 标签、基础镜像 ref→image ID 三项比对。
不匹配即拒绝，tag 名相同不算；镜像覆盖参数绕不开锁文件。

## 5. 配置来源

| 文件 | 内容 | 来源 |
|---|---|---|
| `secrets/mysql_root` | 平台 MySQL root 口令 | 新生成 |
| `secrets/platform_admin` | 平台管理员口令（已替换默认 `admin123`） | `bootstrap.sh` 生成 |
| `platform/application.properties` | 数据源指向 `store`；SM4 key/iv（不与生产共用） | 新生成 |
| `gateway/gateway.env` | 隔离 Gateway 的键 | `gateway.env.template`；业务接口与逻辑环境名来自私有记录 |
| `gateway/runtime.env` | **只读业务依赖白名单**（会话库/业务库/UPMS/TDengine/thirdSession） | `build-runtime-config.sh` 从生产配置文件抽取；不完整即拒绝 |
| `tls/{ca.crt,ca.key,platform.crt,platform.key,platform.p12,password,model-proxy.*}` | 本地 CA、平台与受控出口的叶子证书 | `make-certs.sh`；幂等 |
| `model-proxy/{proxy_token,upstream_api_key}` | 节点→出口的令牌 / 出口→供应商的凭据 | `prepare-secrets.sh`（0600；后者来自 `M2LIVE_MODEL_API_KEY_FILE`）；只有出口挂载此目录，bootstrap 只传前者 |
| `gateway/agent-runs.json` | 环境 → appId/agentId/预期节点 + 节点凭据摘要 | `bootstrap.sh` 回填 |
| `node/node.env` | 节点自己的 appId/token 与回呼凭据 | 同上 |

平台侧注册走平台自己的管理接口（登录 → **换掉默认管理员口令、禁用 demo 应用** →
建应用/模型配置/智能体 → 注册 OpenAPI 用户并订阅），不直接写 MySQL。
`bootstrap.sh` 可重复执行；`bootstrap.sh --check` 验证默认凭据已不可用。

## 6. 回滚

1. `checks.sh down` —— 六个容器与四张网络移除，宿主端口释放，数据保留。
2. 本表的试运行步骤不改、不重启生产 `aiops-gateway-41.service`。PR 合并命中 `src/**`
   会另行触发生产 CD 与重启；先完成该次 CD/health 验收，再开始隔离试运行。
3. 恢复变更前状态：`checks.sh down --purge`（先过所有权与路径门禁；停栈失败就不删），
   再删除 `/etc/aiops-m2-live`、`/opt/aiops-m2-live`。
4. 卸载镜像：`docker rmi aiops-m2-live-03-gateway:<构建标签>`；基础镜像缓存**保留**
   （它们可能被别的用途复用，且删除不影响生产的既有容器）。

## 7. 观测

| 看什么 | 怎么看 |
|---|---|
| 预检 | `checks.sh preflight`（隔离门禁、挂载边界、UID、镜像/制品摘要） |
| 就绪 | `checks.sh ready`（store 健康 / platform UP / 节点注册 / Gateway /health 四项全过） |
| 依赖 | `checks.sh deps`（从隔离 Gateway 网络空间验证会话库与平台） |
| 结构 | `checks.sh verify`（/health 200、无会话 401、运行 UID、节点无出网） |
| 占用 | `checks.sh status` |
| 调度链路 | `docker logs m2live03-node`：`Chat dispatch received ... sid=<run_id>` |
| 记录落库 | 隔离 Gateway 的 `agent_runs` 表 |
| 生产未被碰 | `systemctl status aiops-gateway-41`、`/var/lib/aiops-41` mtime、生产 env 的 mtime |

## 8. 不在这张单里的

- 公网入口、nginx、TLS、防火墙：一条都不改。
- 生产 Gateway 的三个开关：保持现状；新入口只在隔离 Gateway 打开。
- App 发布、模型供应商新增、正式平台商业采用、真实试运行的启动时刻与数据范围
  （技术审查通过后单独确认）。
