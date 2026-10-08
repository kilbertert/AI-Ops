# Dify 1.17.1 @ yidong-36（#579 / PRD #577）

运营在 Dify 控制台里编排智能体的**物理前提**。这一票只做"实例活着、能登进去、能新建
app"；把配置拉回我们侧是 #580 的事。

- **上游版本**：`langgenius/dify` tag `1.17.1`，commit `8387590ace4a094de812b7847fc6a4c3a27cd52b`。
- **实例位置**：移动云 36（`yidong-36`，服务宿主），`/opt/dify`。
- **入口**：`http://36.156.159.175:10008`（会话 JWT 的 console + web）。**无 TLS，
  临时暴露**，见「暴露与收口」。

## 三个决定，以及为什么

本票有三处"必须显式选一次"的地方。都记录在这里，不留默认。

### 1. 容器集：8 + nginx = 9，向量库 0

`init_permissions`、`api`、`worker`、`web`、`db_postgres`、`redis`、`plugin_daemon`、
`ssrf_proxy`，加 `nginx` 作单一入口。

**nginx 需要吗？** 需要，而且理由是运营可用性：不加它就要把 `api`(5001) 和 `web`(3000)
各发布一个端口。两个口就是两个对外面、两条暴露决定、两个收口点；而且 console 与 web
是**同一个同源会话**，分居两个端口会让 cookie/`NEXT_PUBLIC_SOCKET_URL` 都变成跨源。
nginx 是上游自带的，不是自造组件（对应 PRD 里"`nginx` 仅作为单一入口加"）。代价是多一个
容器，换来"只有一个对外面"。

**zero 向量库**成立，因为向量初始化是惰性的：只有**内部数据集**操作才构造 `Vector`，
而本实例的知识面全走 External Knowledge API（#581）。这是 PRD 已核实的事实，不是猜的。

**被删掉的服务**，连同理由：

| 删掉的 | 理由 |
|---|---|
| `worker_beat` | 只跑定时清理与插件升级检查。**唯一一次外呼是 marketplace**（见下）。本实例不装市场插件，删 |
| `sandbox` | Code 节点才用；本实例不需要 |
| `local_sandbox` / `agent_backend` / `agent_ssrf_proxy` | Dify 自带的 Agent runtime。我们的智能体定义在**我们侧**，运行也在我们侧（PRD 的核心边界）。留着它等于把可执行的智能体面开在 36 上，与"只有登记端点可达"的判据直接冲突 |
| 全部向量库 profile | 同上，惰性 |
| `api_websocket` | 协作编辑才用。`ENABLE_COLLABORATION_MODE=false` ⇒ 不需要它 |
| `db_mysql` / `oceanbase` / `seekdb` / `certbot` | 不用 |

`api` 与 `worker` 对 `agent_backend` 是**硬依赖**（`depends_on: required: true`），
所以删它不是注释掉一行 —— `docker-compose.yaml` 里两处的 `depends_on` 已一并去掉，
`AGENT_BACKEND_*` 环境变量也不再注入。这不是可选裁剪，是配套改动。

### 2. SSRF 处置：**保留过滤 + 显式白名单**（不清空）

PRD #577 要求二选一并记录。选的是"保留过滤 + 加白名单"。

机制核实到源码：`ssrf_proxy/squid.conf.template` 里
`acl allowed_domains dstdomain .marketplace.dify.ai`，然后
`http_access deny to_private_networks` 排在 `allow allowed_domains / client_localnet /
localhost` **之前**，最后 `http_access deny all` 兜底。而 `docker-entrypoint.sh` 会把
`SSRF_PROXY_ALLOW_PRIVATE_IPS`（acl 类型 `dst`）与 `SSRF_PROXY_ALLOW_PRIVATE_DOMAINS`
（`dstdomain`）渲染成

```
acl <name> dst|dstdomain <值...>
http_access allow client_localnet <name>
```

插在 `include /etc/squid/dify_allow_private.conf` 那一位 —— 也就是 **deny 之前**。
这是唯一被支持的加白口子。

**当前两个变量都留空**：Dify 侧还没有任何需要打私有地址的配置（要等运营在控制台里把
#581 的 External Knowledge API 挂上才有流量）。留空 = 过滤全开，且与上游默认逐字一致。

**挂知识库时**按目标二选一填，只填那一个：
- 目标是网关域名（走公网）→ `SSRF_PROXY_ALLOW_PRIVATE_DOMAINS=api.mall.qushiyun.com`
- 目标是 36 本机/内网地址 → `SSRF_PROXY_ALLOW_PRIVATE_IPS=<CIDR>`
  —— 注意 squid 在容器里，`127.0.0.1` 指它自己，不是宿主。

**清空 `SSRF_PROXY_*` 这条路没有被选**，代价是失去 SSRF 过滤：Dify 里任何能发
HTTP 请求的节点都能打内网，包括 `169.254.169.254` 这类云元数据地址。不做。

`deploy/dify-36/check.sh` 把这套顺序**钉成断言**：它会失败在白名单 include 掉到
deny 之后的配置上。沉默的失败态（配了却不生效）值得一条自检。

### 3. 镜像来源：compose 里写镜像源，不动 docker daemon

Docker Hub 在 36 **不可达**（`registry-1.docker.io` 连接被重置）。可用的镜像源是
`docker.1panel.live`（实测 `/v2/langgenius/dify-api/manifests/1.17.1` → 200）。

**为什么不改 `/etc/docker/daemon.json`**：配 `registry-mirrors` 要重启 dockerd，
而 36 上跑着 aiops-gateway / kb-service / RAGFlow / healthcare **四个生产栈** ——
为了一个新栈去重启所有栈的守护进程，是把爆炸半径从一栈放大到全机。所以镜像源前缀
写在 compose 的 `image:` 里（`DIFY_REGISTRY`），**换一行即整体回退**，半径只有本栈。

这是对既有部署惯例的一次**有记录的有意偏离**（RAGFlow 与 AI-Ops 到服务端的部署
都不经由镜像源）。偏离理由如上；回退动作是"把 `image:` 改回 `langgenius/...`，
或换一个可达的 `DIFY_REGISTRY`"，不需要任何宿主级变更。

## 暴露与收口

**这是已知的、临时的暴露，不是疏忽。**

| 项 | 值 |
|---|---|
| 绑定 | `0.0.0.0:10008` → 容器 `:80` |
| 端口来源 | 36 已声明的 `10000-10999` 池；10008 是**本栈新增分配**，已登记 |
| TLS | **无**。console 会话 JWT 走明文 HTTP |
| 谁能进 | 目前**公网任何人**，靠 Dify 自己的登录拦 |
| 收口目标 | 单 workspace + 仅少数受控人员可达（PRD 二期） |

收口路径（二期执行）：把 `ports:` 改成 `127.0.0.1:10008:80`，前面放一个带 TLS 的
入口（公司二级域名 + 证书），或直接收进内网/隧道。

**为什么要运维记录里留痕**：策略要求暴露决定由**观测到的流量**决定，而不是由"哪个
端口在听"决定。10008 现在有非环回绑定，但**没有观测记录**说明它有人在用 —— 收口时
必须先测，而不是先假设。这一点在 36 的暴露清单里写清楚。

## 服务身份与隔离

- **身份**：本实例自己的 `dify` 系统账号（`nologin`、无口令、无 sudo、不加入任何共享组），
  家目录即 `/opt/dify`。**不复用** `aiops`（网关）、`health-flow`、`genesis-evidence`、
  也不加入 `docker` 组 —— `docker` 组等价于 root，加进去等于把"一个项目的缺陷"变成
  "整机 root"。
- **compose 的调用者是 root**：单元 `dify.service` 以 root 跑 `docker compose up -d`。
  这是"docker 守护进程归 root 管"的直接后果 —— `dify` 用户没有 docker socket 权限，
  而给他加权限是提权。**容器内的进程仍是各镜像自己的用户**，与宿主身份无关。
- **数据库与缓存完全分离**：本栈的 postgres / redis 是**独立的容器**、独立的卷
  （`deploy/dify-36/volumes/`），与 36 上既有的 aiops-gateway、kb-service、RAGFlow
  （`ragflow-kb-*` 容器，自带 mysql8 + valkey）、healthcare **毫无共享**。没有任何
  端口从宿主直通到这两个数据服务。
- **无编译期来源**：本文件与 compose 都不含凭据；`.env` 在 `/opt/dify/deploy/.env`（600），
  由现场生成。

## 容量决定

36 实测：30 GB 总内存 / **约 12 GB 可用**（无 swap）、`/` 余 46 GB。
本栈常驻估计 **2.5–4.5 GB**，故压了两处上游默认值（写进 `env.example`）：
`POSTGRES_MAX_CONNECTIONS=100`、`POSTGRES_EFFECTIVE_CACHE_SIZE=1024MB`、
`CELERY_WORKER_AMOUNT=2`。

**决定**：**不缩**容器集来省内存（9 个已是最小可用集），而是接受与既有栈并存。
验收时记录实际占用（`docker stats --no-stream`），作为后续收口/扩容的基线。

## 文件

```
deploy/dify-36/
├── docker-compose.yaml     上游 1.17.1 裁剪版（改动处都带注释）
├── env.example             实例级变量的模板；复制成 deploy/.env 再填
├── check.sh                零网络自检：端口集合 + squid 白名单顺序
├── nginx/                  上游 nginx 资产原样拷贝（只加 :ro）
└── ssrf_proxy/             上游 squid 资产原样拷贝（只加 :ro）
```

`nginx/` 与 `ssrf_proxy/` 是**上游文件的原样拷贝**，不是改写 —— 升级 Dify 时按
`/tmp` 里的对应 tag 重新拷一份即可，无须重新推导。compose 里给它们挂了 `:ro`。

## 首次登录

1. `INIT_PASSWORD` 是初始管理员口令（现场生成，不落仓）。首次打开控制台时用它
   建管理员账号。
2. 建完管理员后，把 `INIT_PASSWORD` 从 `.env` 清掉（它只在没有账号时用得上）。
3. 账号建完后，`ALLOW_REGISTER=false` / `ALLOW_CREATE_WORKSPACE=false` 生效 ——
   外部无法自助注册。

## 验收（本票的判据）

运营能**登进控制台、新建一个 app、看到一个模型 provider 可选**。最后一项是
`plugin_daemon` 生效的证据。

**模型 provider 需要一个插件包。** Dify 不内置模型 provider；`plugin_daemon` 空转
时控制台里一个 provider 都没有。装法二选一，取决于外呼策略：

- `MARKETPLACE_ENABLED=true` 时，控制台里直接"从市场安装"（36 → `marketplace.dify.ai`
  实测可达）；
- 关着市场时，**上传 `.difypkg`** —— 这条路与市场无关（源码里是不同的权限检查），
  所以 `MARKETPLACE_ENABLED=false` 不挡它。

**本票采用后者**（PRD 要求关市场）。验收时需要**一个 provider 插件的 `.difypkg`**
（例如 OpenAI-compatible / Tongyi / 智谱）与一把可用的供应商密钥 —— 这两样是
**运营输入**，不是本票能自造的。若两者都不到位，验收记为**待条件**，如实写，
不拿"控制台能打开"冒名顶替"provider 可选"。
