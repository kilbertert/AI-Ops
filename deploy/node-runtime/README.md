# 正式节点运行配置

沿用生产 Gateway，平台、节点、MySQL 与固定模型出口组成常驻栈。配置默认不会部署或启用；
仅在已批准的正式启用任务内执行。公司 App 保持原 POST/GET/blocks。

## 配置与启动

制品从已合并修订构建；平台、节点和镜像身份沿用 `../m2-live-03/images.lock.json`。
运行时不拉镜像、不装依赖。设置分别放在 `.dev-host.toml` 声明的三个节点目录中。
`runtime.env` 是私有部署记录，0600，必须提供以下值：

| 变量 | 来源/用途 |
|---|---|
| `AIOPS_NODE_CONFIG` / `AIOPS_NODE_STATE` / `AIOPS_NODE_DIST` | 配置、状态、不可变制品根，不能指向旧试验目录 |
| `AIOPS_NODE_UID` / `AIOPS_NODE_GID` | 已存在的项目服务身份；MySQL 沿用镜像入口初始化后降权 |
| `AIOPS_NODE_PROXY_IMAGE` / `AIOPS_NODE_MYSQL_IMAGE` / `AIOPS_NODE_JAVA_IMAGE` | 已核对摘要的本地镜像 ID |
| `AIOPS_NODE_BUILD` | 节点构建身份 |
| `AIOPS_NODE_SUBNET` / `AIOPS_NODE_BRIDGE` | 在目标主机路由及 Docker 网络中核对无重叠后登记的内部网段/宿主网桥 |
| `AIOPS_NODE_GATEWAY_URL` | 上述网桥的回调入口；不能使用节点不可达的其他网桥地址 |
| `AIOPS_NODE_PLATFORM_ADDRESS` | 内部网段中的固定平台地址；宿主直接用 HTTPS 访问 |
| `AIOPS_MODEL_UPSTREAM` / `AIOPS_MODEL_BASE_PATH` | 已批准的固定 HTTPS 供应商及 API 前缀 |

最小目录：`platform/application.properties`、`node/node.env`、`gateway/exec-manifest.json`、
`model-proxy/{upstream_api_key,proxy_token}`、`secrets/mysql_root`、`tls/{platform,proxy,public}`。
仅复制所需配置与停稳后的平台数据库；不复制试验 Gateway、身份 fixture 或旧备份密钥。
平台/节点无业务库凭据，模型出口只挂自己的凭据目录。

把 `gateway-callback.conf.template` 中 **具名部署占位符**替换为本次配置：
`AIOPS_NODE_BRIDGE`、`AIOPS_NODE_STATE`、`AIOPS_PRODUCTION_GATEWAY`（共三种名字），
其中 STATE 出现多次。**不能无参数 envsubst**，否则会抹掉 nginx 的认证头变量。
渲染到配置根的 `gateway-callback.conf`。创建项目身份可写的临时目录；入口仅监听内部网桥。
nginx 二进制位置按服务宿主现有安装核对；本 unit 使用 `/usr/bin/nginx`。
含 Lua 模块的宿主构建须沿用现有 `lua_package_path`（必要时 `lua_package_cpath`）加入渲染配置；
仅 `nginx -t` 不够，还要确认进程持续运行。普通 nginx 不添加不支持的 Lua 指令。

通过 `dev-host cp ... --artifact-sha256 ...` 同步合并制品，再执行已批准的安装步骤：

```bash
# 下列是服务宿主操作，由 dev-host exec 的 --allow-service-exec 边界执行。
install -m 644 <合并制品>/deploy/node-runtime/aiops-node-*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now aiops-node-runtime.service aiops-node-callback.service
systemctl is-active aiops-node-runtime.service aiops-node-callback.service
```

systemd 先起栈再起回调；回调随栈重启，使用项目身份。`compose --wait` 只证明容器/存储启动，
**必须另验**平台 health、真实节点在线注册、无凭据回调被拒及节点网络内的可达性。
Docker 网络通过 `compose stop` 保留，正常停机不删除持久状态。

## Gateway 与发布

按[节点发布手册](../../docs/agents/m3-node-publication.md)配置目标表，逻辑环境三方一致。
宿主 Gateway 直接访问平台固定内部地址的 HTTPS 8900；证书 SAN 必须含该地址。
用原 CA 签发平台叶子证书并更新 `tls/platform/platform.p12`，CA 合并系统信任库，不能关闭校验。
Docker internal 网络的端口映射在本环境不生成宿主监听，不能依靠 `ports` 声明判断可达。
登记个人状态业务端点和路由库，先保持用户查询关闭；通过真实发布 API 从 Dify SOP 生成版本，
确认路由 active 后再启用 `CONSUMER_ORDER_CHAT_ENABLED`。更改前保存原配置与源 SOP，
确认没有在飞工作再重启 Gateway。禁止另建生产 app 对象来“检查”状态库。

开关覆盖 Gateway 的状态问法分流：未登记端点的其他租户仍可能收到 404。
这不提供按人限流或自动证据清理。模型重试次数没有硬上限；失败不回落直读。

## 验收、退役和恢复

执行 [qa-plan.md](qa-plan.md)。真实 H5 用 `status_probe.py --execution node`，
页面完成之后仍须按 qa_id 核对唯一 run、succeeded/matched、发布版本及同轮快照，并独立对照 SQL/CPMS。
默认直读判据保持严格，不因新增节点路径放宽。该浏览器驱动使用正常登录取得的已有会话。

新链通过后才退役试验配置：检查容器挂载、systemd、定时任务及进程引用；无引用的本项目旧根
移动到私有归档，写源/目的/摘要清单。历史证据不删，其他入口不因“监听可疑”顺手关闭。
恢复先禁用/撤回新路线，保留所有新 QA/run，恢复原配置与 SOP 后重启 Gateway，再停本栈。
不以旧数据库覆盖生产期间新增数据。复核 Gateway 健康、原功能和归档可读后才完成恢复。

边界遵循 Docker Compose 的 internal 网络、restart/depends_on 与 systemd 的 After/PartOf 语义；
回调沿用 nginx 的精确路径白名单及既有 Gateway 身份校验，不新增权限协议。
