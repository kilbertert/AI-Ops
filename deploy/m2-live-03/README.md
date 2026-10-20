# M2-LIVE-03 受控试运行栈（41 同宿主，隔离）

一次**真实只读链路**的验证环境：公司身份 → Gateway → 编排平台 → 指定节点 → 唯一受控工具
→ 真实业务状态。它是**部署件**，不是实验脚本，也不是长期控制面布局。

## 它是什么 / 不是什么

| 是 | 不是 |
|---|---|
| 41 上的一组容器，独立网络、独立数据目录、显式运行 UID | 生产 Gateway 的一部分（生产 `aiops-gateway-41.service` 不改、不重启） |
| 隔离 Gateway，持有既有只读业务依赖 | 带业务库凭据给平台或节点 |
| 平台与节点只拿自己的凭据 | 新增公网端口（全部绑定回环或容器内网） |
| 真实模型经已批准的渠道 | 公司会话、令牌、完整订单进入平台或模型 |
| 构建好、按摘要锁定的 Gateway 镜像 | 启动时下载或解析依赖 |

## 逻辑拓扑

```
                ┌────────── 41 宿主（本文件所在的部署件） ──────────┐
  真实 App ──?──▶ nginx :8789 ──▶ 生产 Gateway :8788（不动）        │
                                        │                          │
  受控测试 ──▶ 隔离 Gateway :8794 ◀─────┘（同一份代码，独立状态库） │
                    │  OpenAPI（经回环中继）                          │
                    ▼                                                 │
              平台（编排服务端） :8900                                  │
                    │  gRPC 调度（:18888）                            │
                    ▼                                                 │
              执行节点 :18889  ──回呼──▶ 隔离 Gateway :8794            │
                    │                                                 │
                    └── 唯一工具：charging_status_snapshot            │
                                                                      │
  平台存储：MySQL 8（容器内，独立数据卷）                              │
  CPMS：真实 HTTPS（只由隔离 Gateway 访问）                            │
  会话库：宿主回环 :6379 ──gateway-host-relay──▶ 仅 Gateway 可见       │
                └────────────────────────────────────────────────────┘
```

**只有隔离 Gateway 访问业务系统与会话库**。平台与节点不挂载生产 env、不挂载生产
Gateway 库、不持有业务库凭据；节点只接内部网络，没有出网。

**模型调用在节点进程里发生**（`ClientChatExecutor` 在节点构造 ChatClient 并直接请求
模型端点），因此节点需要一条到模型的通路。部署件给它的是**固定目标的受控出口**
`model-proxy`（`modellink` 内部网络，只有 node 与它相连）：出口只转发到部署件钉住的
一个上游，接受 `/chat/completions` 与 `/v1/chat/completions`（适配器两种拼法）以及
`/v1/models`，不实现 CONNECT，不接受请求里的 URL。供应商 key 只在出口，节点只拿到出口
专用令牌。

## 为什么同宿主

Gateway 与业务依赖已在 41；同宿主先避免跨主机 gRPC、隧道与新增公网暴露。首轮只做一条
真实只读链路。36 保留为后续控制面候选。

## 正式身份依赖怎么接上

生产 `production.env` 里 `AIOPS_REDIS_HOST=127.0.0.1`、端口 6379 —— 那是**宿主**回环，
而隔离 Gateway 在 bridge 网络里。部署件用两种最小通路接上，都不改生产监听、不新增公网口：

| 依赖 | 通路 | 谁能访问 |
|---|---|---|
| 会话库（Redis，宿主回环 6379） | `gateway-host-relay`：只接 `hostloop` 网络，目标钉死 `host-gateway:6379` | 只有隔离 Gateway |
| 编排平台（HTTPS） | 平台用本地 CA 签发的证书讲 HTTPS；Gateway 用 `SSL_CERT_FILE` 信任该 CA | 只有隔离 Gateway |
| 模型（HTTPS，供应商端点） | `model-proxy`：只转发到一个固定目标，路径白名单；节点只接 `modellink` | 节点经出口；供应商 key 只在出口 |
| 供应商基路径 | `M2LIVE_MODEL_UPSTREAM_BASE_PATH`（默认 `/v1`）：适配器只追加 `/chat/completions` | 部署配置，不是运行时猜测 |

`canonical_gateway_url` 仍然要求远端是 HTTPS，**校验没有被放宽**：不再用回环中继把
「地址是回环」凑出来（那是审查否掉的形状）。证书由 `make-certs.sh` 生成，SAN 含
`platform`；CA 带 `keyUsage=keyCertSign,cRLSign`（缺它 OpenSSL 3 会在链校验时拒绝）。
`checks.sh deps` 从隔离 Gateway 的**同一网络空间**验证这两条通路，是真实请求之前的门。

## 资源预算（整栈，不是每进程）

| 组件 | 内存上限 | CPU |
|---|---:|---:|
| 平台存储（MySQL 8） | 700 MiB | 0.5 |
| 平台服务端 | 1200 MiB | 0.40 |
| 执行节点 | 500 MiB | 0.22 |
| 隔离 Gateway | 400 MiB | 0.22 |
| 宿主回环中继 | 64 MiB | 0.05 |
| **受控模型出口** | **128 MiB** | **0.10** |
| **合计** | **2992 MiB** | **1.49** |

上限按指令的 3 GiB / 1.5 CPU 之内分配；**不是峰值保证**。上线前复测宿主余量，
预算不足就报告差额，不用 swap 顶。辅助中继也计入预算。

## 文件

| 文件 | 用途 |
|---|---|
| `compose.yaml` | 六个容器、四张网络、资源上限、显式 UID、只读挂载 |
| `gateway.Dockerfile` + `gateway-requirements.txt` | Gateway 镜像：依赖由 `uv.lock` 导出并带哈希，启动不下载 |
| `build-image.sh` | 构建镜像并记录精确身份（镜像 ID + 内容摘要标签）；`--update-lock` 写回锁文件 |
| `bootstrap.sh` / `bootstrap_platform.py` | 平台初始化：换默认管理员口令、禁用 demo 应用、注册应用/模型/智能体、回填绑定表（幂等） |
| `compose.synthetic.yaml` | **只给开发机**：模型/CPMS/调用者身份替身，用于部署件本身的验收 |
| `images.lock.json` | 平台与节点制品摘要、基础镜像身份（按 ref）、Gateway 构建身份 |
| `.env.template` | 非敏感参数与**键名**；真实值来自宿主上的 0600 文件 |
| `make-certs.sh` | 生成本地 CA 与 platform 的服务端证书（幂等） |
| `build-runtime-config.sh` | 从生产配置抽取**只读依赖白名单**；不完整就拒绝 |
| `checks.sh` | preflight / up / ready / verify / deps / status / down |
| `HOST-CHANGE.md` | 41 上的精确变更清单（不是授权） |

## 数据与凭据边界

模型出口使用独立的 `model-proxy/` 配置目录，只挂载自己的供应商 key 与代理令牌。
它不挂 `gateway/`，因此看不到业务库、会话库及订单绑定配置；Gateway 也不挂出口的
供应商 key。`M2LIVE_MODEL_API_KEY_FILE` 只供 `prepare-secrets.sh` 读取供应商凭据；
`bootstrap.sh` 固定读取 `model-proxy/proxy_token` 下发给节点，两者不得共用变量覆盖。
正式出口只接受 HTTPS 上游；HTTP 仅供显式 `M2LIVE_SYNTH_MODE=1` 的合成上游。
复跑时使用新配置根，旧合成根内 `gateway/upstream_api_key` 不应带入新部署。

- 宿主目录：`/opt/aiops-m2-live/`（制品）与 `/var/lib/aiops-m2-live/`（状态）。
- 隔离 Gateway 的状态库、运行目录、key 目录都在 `/var/lib/aiops-m2-live/gateway/`，
  与生产 `/var/lib/aiops-41` **不交叉**。`aiops_diagnostics.isolation_guard` 在
  `create_gateway_app()` 打开任何库**之前**判定；它就是 `gateway_server serve` 的启动路径，
  不是另写一份检查。
- 隔离 Gateway 读 `build-runtime-config.sh` 生成的**白名单子集**
  （`/etc/m2live/runtime.env`：会话库、业务库、UPMS、TDengine、thirdSession 令牌），
  不挂生产 `keys/`：业务只读路径不需要模型密钥。只读挂载也会泄露目录内的凭据。
  不挂整份 `production.env` 的理由：设置读取是「环境变量 or 配置文件」，整份文件会让
  试运行实例继承生产里所有未覆盖的开关与凭据。
- 平台与节点只挂自己的配置目录（`/etc/aiops-m2-live/{platform,node}`），不挂 `/etc/aiops-41`。
- 绑定表（`agent-runs.json`）在 Gateway 的只读配置里，0600；节点凭据只以 SHA-256 出现在其中。
- 平台 schema 自带的 `admin/admin123` 与 demo 应用 token 在导入任何真实模型凭据或业务结果
  之前由 `bootstrap.sh` 替换/禁用；`bootstrap.sh --check` 验证默认凭据已不可用。

## 运行身份

`platform` / `node` / `gateway` / `gateway-host-relay` / `model-proxy` 都显式给出数字 UID/GID
（宿主上是项目身份 `aiops41`，合成验收时是开发机当前用户），渲染后再核对一次。
`store` 是唯一例外：官方 mysql 镜像的入口点先以 root 做一次 initdb，随后长期以
`mysql(999)` 运行；这一条短暂初始化不泛化到其它服务。

## M3：打开执行配置守卫的开关（真实链路前必做）

四个开关里最后一个是**执行配置清单**。它两侧读同一份字节，身份就是该文件的 sha256。
顺序不能颠倒 —— 配了却读不到时，Gateway **启动即拒**（`validate()` 直接失败）。

```bash
# 1) 写清单（在开发机/runner 上跑；身份 = 原始字节 sha256）
uv run python tools/m2_close/m3live/write_exec_manifest.py \
  --out /tmp/exec-manifest.json \
  --model-id "$M2LIVE_MODEL_ID" \
  --endpoint "$M2LIVE_MODEL_ENDPOINT" \
  --system-message "<平台智能体的 instruction，逐字>"

# 2) 落到宿主的 gateway 配置目录（0600），节点容器只读挂载**这一个文件**
install -m 600 /tmp/exec-manifest.json "$M2LIVE_CONFIG/gateway/exec-manifest.json"

# 3) 两个开关（模板里已声明）
#    gateway.env: AIOPS_GATEWAY_EXECUTION_MANIFEST_FILE=/etc/m2live/exec-manifest.json
#    node.env:    AIOPS_EXEC_MANIFEST=/etc/m2live/exec-manifest.json

# 4) 起栈（preflight 会核对：文件存在、权限 0600、两侧指向同一个文件）
bash deploy/m2-live-03/checks.sh up
```

**清单里每一个值的来源**（`write_exec_manifest.py` 逐条核对，缺一项就拒绝写）：

| 字段 | 来源 |
|---|---|
| `model.provider_key` / `adapter_key` | `bootstrap_platform.py` 建平台模型配置时写的那两个值 |
| `model.model_key` | `M2LIVE_MODEL_ID` |
| `model.endpoint` | `M2LIVE_MODEL_ENDPOINT`（节点经受控出口的那个地址） |
| `model_options` | 平台下发给节点的选项。本栈不传 `configJson` ⇒ **实测全为空**（`{}`）。写成非空值会被节点以 `OPTIONS` 拒绝 —— 2026-10-20 的真实请求就是这样发现的 |
| `system_messages` | 平台智能体的 `instruction`（`SystemPromptHandler` 直接把它当 system 消息） |
| `tools[0]` | 节点源码里 `ControlledChargingStatusTool` 的三个常量 |

⚠️ **`bootstrap.sh` 会重写 `node.env`**：它只覆盖三行凭据，其余键（含
`AIOPS_EXEC_MANIFEST`）原样保留 —— 这条有回归钉住，因为整份覆盖会让守卫在 bootstrap
之后静默消失。

## 合成验收（开发机）

```bash
M2LIVE_SYNTH_TOKEN=$(openssl rand -hex 16) \
M2LIVE_PROJECT=m2-live-03-syn \
M2LIVE_COMPOSE_EXTRA=deploy/m2-live-03/compose.synthetic.yaml \
M2LIVE_CONFIG=/tmp/m2live03-local/etc M2LIVE_STATE=/tmp/m2live03-local/state \
M2LIVE_DIST=/tmp/m2live03-local/dist \
M2LIVE_UID=$(id -u) M2LIVE_GID=$(id -g) M2LIVE_SYNTH_MODE=1 \
bash deploy/m2-live-03/checks.sh up
```

它验证的是**部署件**：冷启动、运行身份、只读挂载、无出网、节点注册、平台调度、
sid 到节点、领取端点按凭据拒绝。正例（真实身份 → 本人订单）只能到 41 上跑；
结果记在 `docs/validation/m2-live-03-synthetic.json`。

## 不在这份部署件里的

真实业务验收、模型回答质量、App 客户端、发布版本冻结、公网全量启用。
`agent_version` 仍是 `null` + `not_wired`。

## 初始化文件传输

`bootstrap.sh` 通过 `docker exec` 标准输入/输出传输初始化脚本与凭据，不使用
`docker cp`：后者在部分 daemon/嵌套只读挂载组合下会拒绝复制。输入由容器的服务 UID
创建为 0600；绑定/节点输出全部成功、非空后才以正确 UID/0600 逐文件原子替换宿主配置。
传输失败或空输出返回非零并保留宿主原文件；不把凭据放入命令行或日志。

管理员轮换目标在改变平台前先保存到宿主 0600 文件；后续传输失败并重建 Gateway
仍可重跑恢复。绑定/节点两份输出齐全后才逐文件发布；bootstrap 退出时清理容器临时凭据。
