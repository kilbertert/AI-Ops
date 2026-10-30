# 正式节点运行配置

现役Java源码与构建入口位于[services/execution-node](../../services/execution-node/README.md)。
仓内源码迁移不改变宿主jar安装目录、systemd和容器挂载。
管家JSON输出契约修复位于节点jar；仅更新Gateway或Dify提示词不会生效。按原流程从合并
修订构建/核对节点制品，变更窗口切换后分别验原生预览、AI-Ops试用与正式入口。
既有`operator-exec-config/1`清单无需重发；恢复时切回原jar并核对节点在线。
本地候选的验证与线上限制见[输出契约QA](../../docs/agents/operator-cpms/output-contract-qa.md)。
模型出口源码位于[services/model-proxy](../../services/model-proxy/README.md)，新Compose与包含新路径的镜像必须成对发布。

当前产品路径与交付状态见[当前说明](../../docs/agents/current-delivery-state.md)。本文件是部署/维护方法，配置默认关闭不代表现役全部关闭；
新旧执行器和场景分别验收，不按一个服务进程推断全系统已迁移。

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
| `AIOPS_NODE_GATEWAY_URL` | 上述网桥的 HTTPS 回调入口；证书 SAN 必须匹配该地址，不能关闭验证 |
| `AIOPS_NODE_PLATFORM_ADDRESS` | 内部网段中的固定平台地址；宿主直接用 HTTPS 访问 |
| `AIOPS_MODEL_UPSTREAM` / `AIOPS_MODEL_BASE_PATH` | 已批准的固定 HTTPS 供应商及 API 前缀 |

最小目录：`platform/application.properties`、`node/node.env`、`gateway/exec-manifest.json`、
`node/operator-tools.json`、`model-proxy/{upstream_api_key,proxy_token}`、`secrets/mysql_root`、
`tls/{platform,proxy,public}`，以及 `tls/callback/{server.crt,server.key}`（回调叶子证书和私钥）。

**`node/operator-tools.json` 是工具定义本身**（ADR-0181）：从仓库的
`src/aiops_diagnostics/operator/operator_tools.json` 原样复制，节点用
Compose将它只读挂载到`/etc/m2live/operator-tools.json`，并在`-jar`之前传入
`-Daiops.node.tools-file=/etc/m2live/operator-tools.json`。文件不存在时Compose拒绝创建容器，
不会自动生成一个同名空目录。声明由节点首次读取后缓存，更新文件后须定点重建节点使新声明生效，
**不需要重建 JAR**；声明缺失或非法时不会被当成「没有工具」。
仅复制所需配置与停稳后的平台数据库；不复制试验 Gateway、身份 fixture 或旧备份密钥。
平台/节点无业务库凭据，模型出口只挂自己的凭据目录。

把 `gateway-callback.conf.template` 中 **具名部署占位符**替换为本次配置：
`AIOPS_NODE_BRIDGE`、`AIOPS_NODE_STATE`、`AIOPS_NODE_CONFIG`、`AIOPS_PRODUCTION_GATEWAY`（共四种名字），
其中 STATE 出现多次。**不能无参数 envsubst**，否则会抹掉 nginx 的认证头变量。
渲染到配置根的 `gateway-callback.conf`。创建项目身份可写的临时目录；入口仅监听内部网桥。
回调使用 TLS 1.2/1.3；由节点已信任的项目 CA 签发叶子证书，SAN 包含实际网桥 IP，
用途为 serverAuth。证书644、私钥600，所属身份与回调服务一致；不向节点挂载私钥。
同一端口不再接受明文；项目运行配置和管家目标的 `proxy_base_url` 同步使用 HTTPS。
若宿主防火墙默认拒绝 INPUT，须登记并允许**仅节点内部网段 → 回调网桥:8788/TCP**；
使用现有防火墙管理器持久保存，禁止开放 `0.0.0.0/0`。先从节点网络空间做真实请求，
无凭据必须被 Gateway 拒绝（不能只有宿主 curl 通）。恢复时删除本次精确规则。
例如既有管理器是 ufw 时：

```bash
ufw allow from "$AIOPS_NODE_SUBNET" to "$AIOPS_NODE_BRIDGE" port 8788 proto tcp comment aiops-node-callback
# 恢复：同一参数前加 delete；不得执行 ufw reset。
ufw delete allow from "$AIOPS_NODE_SUBNET" to "$AIOPS_NODE_BRIDGE" port 8788 proto tcp
```

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

无订单管家回合使用新增`operator-conversation/1`清单，先部署支持它的节点jar，再按既有CD部署
Gateway，最后从同一合并修订构建/部署Dify控制台桥。旧节点会拒绝新协议，不存在降级执行。
部署期间保留旧jar、Gateway源码及桥包；恢复先撤回新Gateway/桥，再恢复旧节点。
现有已发布订单清单和路由无需重发。站点接口访问沿用当前用户公司Bearer，不新增凭据或公网暴露。

### 仅更新Java节点jar

先在开发机从合并修订`git archive <merge> services/execution-node`解出干净源码，
按[离线构建方法](../../docs/agents/operator-cpms/output-contract-qa.md)执行Maven package。
不依赖旧target目录；缓存未预置clean插件时无需下载，使用新的归档目录即可。

1. 通过`dev-host exec 41 --allow-service-exec`读取现役`current`真实路径、节点挂载、
   `runtime.env`和jar摘要，记录其他三容器ID、全部发布路由；以只读SQLite确认
   `agent_runs`的issued/running为0。配置快照留服务宿主0700备份目录，不传公开仓库。
2. 用`dev-host cp ... --artifact-sha256 ...`传合并jar到`node_artifacts`声明的releases下。
   现场核摘要。`cp -a --reflink=auto <old-release> <new-release>`创建独立发布目录，
   只替换其中`node/aiops-execution-node.jar`，文件644、项目服务身份所有。
   其余旧发布文件按字节继承，不把当前源码树的其他变更带入运行栈。
3. 备份原`runtime.env`与`current`目标。原子替换env中的`AIOPS_NODE_DIST`和
   `AIOPS_NODE_BUILD`，保留其余字段、属主与0600权限；原子切换`current`。
   执行下列定点重建（服务宿主已有Compose），不启动或重建其他服务：

   ```bash
   docker compose --project-name aiops-node-runtime \
     --env-file /etc/aiops-node-runtime/runtime.env \
     -f <new-release>/deploy/node-runtime/compose.yaml \
     up -d --no-deps --pull never --force-recreate node
   ```

4. 轮询到节点8081实际HTTP响应，确认gRPC注册及无重启/OOM。现役0.22 CPU配额实测
   启动约62秒，首次40秒窗口误判且已自动恢复；等待上限用240秒，并给远端执行360秒硬上限。
   再从容器内核jar摘要、构建标识、服务身份；对比其他容器ID未变及路由逐字段相等。
5. 失败时保存本次节点启动日志，恢复原env字节和`current`目标，用相同定点命令重建旧node，
   等旧节点就绪。真实业务验收失败也执行该回滚路径。保留新旧目录与业务QA，不回滚数据库。

浏览器验收必须轮询作业终态，不能固定睡35秒就判失败。Dify SSE不依赖Playwright
`Response.text()`（真实返回过resource unavailable）；从页面正文与本次请求/时间关联QA取证。
普通试运行返回202表示已接收，回读completed才成功。正常登录、原H5选单和只读CPMS核对见
[运行手册](../../docs/agents/runtime-retirement-operations.md)，原始截图/会话只保存在0700私有证据目录。

### 既有 HTTP 回调迁移

合并制品到位后先备份原回调配置与 `runtime.env`，准备证书并验证有效期、SAN及CA链，
确认没有在飞任务。先用 `nginx -t` 校验渲染配置，再在同一变更窗口替换回调配置，
执行 `systemctl restart aiops-node-callback.service` 并确认HTTPS实际可达；仅 `nginx -t`
不会改变运行进程。随后更新 `AIOPS_NODE_GATEWAY_URL` 并重新创建节点容器；平台、数据库和模型出口保持原制品。
节点仅挂已有公共信任库，增加叶子证书无需改它。

从节点网络内验证HTTPS可达、无凭据拒绝、未知路径拒绝、节点重新在线；不能仅验宿主curl。

**探活必须等就绪，且必须自带超时。** 2026-10-26 在 41 实测两次失败都出在这两点上：

- `systemctl restart` 返回只说明进程被拉起，不说明已在监听。重启之后**轮询**到
  HTTPS 真的应答再判断，别在返回后的第一次探活就下结论。
- 交互式命令（例如 `docker exec -it ... java`）没有自身超时，卡住会把整次切换拖成
  "超时失败"，而链路其实已经通了。

可复用的形状（Java 用**节点自己的**信任库，不是 `curl -k`）：

```bash
# 1) 等回调就绪（最多 30s），用证书校验，不用 -k
for i in $(seq 30); do
  curl -sS -o /dev/null -m 2 --cacert /etc/aiops-node-runtime/tls/callback/server.crt \
    https://172.30.41.1:8788/v1/internal/operator-runs/probe/tools/cpms_order_detail/authorization \
    && break; sleep 1
done

# 2) 节点容器内用现役 truststore 做真实握手；外部 timeout 兜底
docker cp TlsProbe.class aiops-runtime-node:/tmp/
timeout 45 docker exec aiops-runtime-node java -Xmx32m -cp /tmp TlsProbe \
  https://172.30.41.1:8788/v1/internal/operator-runs/probe/tools/cpms_order_detail/authorization 403 POST
```

退出码 0 且打印 `TLS_CALLBACK_STATUS=403` 才算"连上了且按预期拒绝无凭据请求"；
`403` 本身不是故障，`000`/超时才是。明文进同一端口应得 `400`（nginx 拒绝非 TLS 握手）。

失败先恢复原回调配置，执行 `systemctl restart aiops-node-callback.service` 并确认原HTTP可达，
再恢复URL并重新创建节点，不恢复业务数据库。此迁移会短暂停顿节点派发，
须在具体变更单中登记时间和恢复点；只合并本模板不会修改运行中的HTTP配置。

语义依据：[nginx HTTPS配置](https://nginx.org/en/docs/http/configuring_https_servers.html)。

### 业务验收

执行 [qa-plan.md](qa-plan.md)。真实 H5 用 `status_probe.py --execution node`，
页面完成之后仍须按 qa_id 核对唯一 run、succeeded/matched、发布版本及同轮快照，并独立对照 SQL/CPMS。
默认直读判据保持严格，不因新增节点路径放宽。该浏览器驱动使用正常登录取得的已有会话。

新链通过后才退役试验配置：检查容器挂载、systemd、定时任务及进程引用；无引用的本项目旧根
移动到私有归档，写源/目的/摘要清单。历史证据不删，其他入口不因“监听可疑”顺手关闭。
恢复先禁用/撤回新路线，保留所有新 QA/run，恢复原配置与 SOP 后重启 Gateway，再停本栈。
不以旧数据库覆盖生产期间新增数据。复核 Gateway 健康、原功能和归档可读后才完成恢复。

边界遵循 Docker Compose 的 internal 网络、restart/depends_on 与 systemd 的 After/PartOf 语义；
回调沿用 nginx 的精确路径白名单及既有 Gateway 身份校验，不新增权限协议。

## 模型代理源码归位后的发布

合并修订通过`deploy/m2-live-03/build-image.sh --save <镜像tar>`在开发机/runner构建，
发布内容锁在任务分支构建验证时更新并随PR合并，合并后的构建不得改写清单，
制品包含`services/model-proxy/model_proxy.py`。本片只移动源码，原文件与迁移前逐字相同。
发布前核对新镜像内Compose入口的`--help`可执行，并在临时证书/合成上游下验证实际TLS请求。

服务宿主保留旧发布包、旧镜像和`runtime.env`；通过dev-host传递有摘要的镜像及同修订Compose。
先`docker load`并核镜像ID，渲染新Compose确认模型出口唯一入口为新路径，
再将`AIOPS_NODE_PROXY_IMAGE`与同修订Compose一起切换，仅对`model-proxy`执行
`docker compose ... up -d --no-deps --pull never model-proxy`。
在无在飞模型任务的窗口切换，验证节点到代理的TLS、令牌拒绝与真实单次模型请求；
失败恢复旧Compose和旧镜像ID后重建同一服务，不停止平台、节点或数据库。
只移动仓库源码不会改写运行中的旧发布包；未执行上述切换时，生产仍使用旧包中的等字节代理。

## 工具声明挂载补齐与更新

从同一已合并修订归档`deploy/node-runtime/compose.yaml`和
`src/aiops_diagnostics/operator/operator_tools.json`，通过`dev-host cp --artifact-sha256`传入。
先核现役Gateway声明与制品摘要相同、没有issued/running作业，备份当前发布目录指针、runtime.env和旧声明（包括“不存在”）。
复制现役发布目录为新目录，只替换其Compose；将声明安装到配置根`node/operator-tools.json`（0644、项目服务身份所有）。
用`docker compose config --format json`对比新旧配置：只允许节点command/volumes增加工具声明，
其他服务和节点环境、TLS、身份、网络保持原值。然后原子更新runtime.env的AIOPS_NODE_DIST和current指针，
按上节定点`up -d --no-deps --pull never --force-recreate node`并等待就绪。
核对容器内声明摘要、JVM加载参数、原jar摘要、其余容器ID与正式路由不变；正常登录原H5完成订单请求。
失败恢复原env/current/声明，定点重建原节点；只恢复发布资产，不恢复业务数据库。

挂载语义依据[Compose服务规范](https://docs.docker.com/reference/compose-file/services/#long-syntax-6)：
长语法bind的`create_host_path: false`禁止源文件缺失时自动建目录；`read_only: true`保证节点不可修改声明。

## 受管发布前的实际节点兼容性

Java每30秒使用自身节点凭据报告支持的清单schema、工具授权契约和**启动时实际加载**的工具声明
摘要到`POST /v1/internal/execution-node/capabilities`。该精确回调须随nginx模板部署；不新增公网入口。
Gateway从凭据确定节点与环境，报告120秒过期。管家开通、发布和恢复只在目标节点支持候选协议且
工具声明摘要一致时推进正式指针；缺心跳或不兼容分别返回SETUP_EXECUTOR_UNAVAILABLE/
SETUP_EXECUTOR_INCOMPATIBLE。现役路由不被后台自动撤回，旧节点升级前新发布会被明确阻止。

升级顺序：合并Gateway制品与回调模板 → 合并Java制品 → 等待实际心跳 → 正常页面发布。
先备份当前release/runtime.env/回调配置，节点重建前确保无在途任务，失败恢复旧制品与回调；
不以插入数据库假心跳代替实际节点报告。后台只读查询execution_node_capabilities中的
environment/node_id/observed_at及协议摘要即可检查；不输出节点令牌。

本地Java构建：使用既有`maven:3.9-eclipse-temurin-21`，挂载项目与`~/.m2`，以claude UID运行
`mvn -s /var/maven/settings-docker.xml -Dmaven.repo.local=/var/maven/repository -DskipTests=false package`。
验证实际Java报告：`python tools/verification/verify_node_capabilities.py dist/verification/node-capabilities`。
使用合成Gateway、随机回环端口及无效Snail目标，约4秒收到报告；关闭自己的容器，不接生产。
