# 95 环境事实

> **用途**：回答「95 是什么、在哪、怎么连、里面有什么」—— 不依赖会话记忆，也不需要
> 去别的文档里拼三处半句。
> **最后核验**：2026-10-20（从开发机与 runner 主机两侧实测）。
>
> **纪据纪律**：本文件只写方法与位置。地址、指纹、凭据的真值在库外主机清单
> （`dev-host show 95`），不在这里；也不写任何口令/令牌/thirdSession 明文。

## 1. 一句话

**95 是公司杭州那台机的内部代号，就是 `qy-gw-120`（`~/.ssh/config` 别名
`prod-bugtrack-120`，SSH 端口 2134）。** 它承载 `api.qumall.qushiyun.com` 的 `/v1/*`，
即「另一套 AI-Ops 环境」。

| 项 | 值 |
|---|---|
| 内部代号 | **95**（AI-Ops 散文里的「95 环境」「95 本机」「95 入口」都指它） |
| 主机侧清单键 | `qy-gw-120`；`dev-host` 里的目标名也是 `95` |
| 角色 / 归属 | `service-host` / `qushiyun`；**公司环境，未纳入本编队** |
| 公网入口 | `https://api.qumall.qushiyun.com` |
| 与 41 的区别 | 41 = `api.mall.qushiyun.com`；95 = `api.qumall.qushiyun.com`。**两套独立数据面**，不得把任一域名反代到另一环境的会话库或诊断数据源 |

判别口诀：**41 是 mall，95 是 qumall，36 是移动云 KB 栈，124 是新加坡。**

证据（2026-10-20 实测）：
- `api.qumall.qushiyun.com` 解析到 95；`api.mall.qushiyun.com` 解析到 41；
  `xyh5.xyseeker.com` 解析到 36（`dig +short`，地址本体见库外清单）。
- 95 本机 nginx vhost `api.qumall.qushiyun.com` 的 `location /v1/` → `127.0.0.1:8788`，
  并注入服务身份；该 `proxy_pass` 与 41 的反代是两个独立配置。
- 95 本机 `aiops-gateway` active，绑 `127.0.0.1:8788`，`GET /health` = 200。

## 2. 怎么连（以及为什么直连会失败）

`dev-host read 95 <path>` / `dev-host ls 95 <path>` / `dev-host find 95 ...` 即可用；
它是服务类主机，`exec` 需显式 `--allow-service-exec`，写入需产物身份。

**网络事实**：开发机（`dev-us-01`）到 95 所在云厂商那一段地址的**入站**被网络侧丢弃，
不是主机防火墙 —— 在 95 上抓包，从 `dev-us-01` 发起的 SYN **一个都没到**
（`tcpdump` 0 packets；同一时刻对 41 抓也是 0）。41 早已是同一现象，并因此有了一条
临时跳板（`FLEET.md` §7.9）。95 的 2134 端口同理，且 `known_hosts` 里此前根本没有它的记录。

三个反证，说明这不是"美国与该公司网络不通"：
- 95 **自己**能主动连到开发机（反方向通）；
- 同一云厂商的另一个地域主机（`svc-gz-117`）从 `dev-us-01` 可达；
- 从 36 和 124 都能直连 95 的 2134。

现状：`dev-us-01` 的 `~/.ssh/config` 给 `prod-bugtrack-120` 加了 `ProxyCommand`，
经 HK 开发机一条**受限通道**转发 —— 该 key 在 HK 的 `authorized_keys` 里绑定
`command="nc <95 地址> <95 SSH 端口>"` + `restrict`，**开不了 shell，也只能连这一个
地址端口**（实测：拿该 key 要 shell，拿回来的是 95 的 SSH banner）。
`dev-host` 的主机公钥断言照样生效。改法与回滚见 `FLEET.md` §7.30。

## 3. 95 上到底有什么

| 项 | 值（2026-10-20 实测） |
|---|---|
| 服务单元 | `aiops-gateway.service`，`User=aiops`，enabled，自 2026-09-14 起常驻 |
| 运行目录 | `/home/aiops/AI-Ops`（**无 `.git`**，不是 checkout） |
| venv | `/home/aiops/AI-Ops/.venv`（`aiops` CLI 可用） |
| 进程环境 | `/home/aiops/gateway.env`（`AIOPS_GATEWAY_BIND=127.0.0.1`、`PORT=8788`） |
| 服务端配置 | `/home/aiops/.config/aiops-diagnostics/acceptance.env` |
| Gateway DB | `/home/aiops/.local/share/aiops-diagnostics/gateway/gateway.db` |
| 版本 | `/health` 报 `0.1.0` —— 纯版本号，**没有** 41 那种 `0.1.0+<sha>` 来源标记 |

**已知空白（如实记录，不是推测）**：

- **没有 `ops/environments/env-95.toml`，而且从来没有过** ——
  `git log --all -- ops/environments/env-95.toml` 为空。41 那份之所以存在，是因为它要走
  `aiops admin reconcile`；95 上没有这条流程。
- **95 上连 `ops/environments/` 目录都没有**，且 `/home/aiops/AI-Ops` 不是 git 仓库 ——
  95 的 agent 清单**没有声明态**，只有库里的运行态。
- **库是空的**：`agents` / `agent_versions` / `conversations` / `conversation_turns` /
  `runs` / `devices` 六张表**各 0 行**（以 `aiops` 身份读）。
- 带服务身份调 `GET /v1/faq/catalog` 返回 `401 INVALID_ACCESS_TOKEN` —— 与 41 相同的
  认证合同，但对**这个**环境而言，此刻没有可用的会话世界。

所以：**95 是「入口与进程在跑、数据面为空」的状态**，不是 41 的镜像，也不是它的备份。
把它当 41 的对等环境来读会得出错误结论；把它当"已退役"也是错的 —— 进程与入口都活着。
它的真实归属（现在还被谁用、用来做什么）需要业务方给一句；在此之前按"在用"处理。

## 4. 不要把 95 和 120 混为一谈的地方

- **`120` 有两个含义**：既是这台机自己的代号（地址末段），又是「`healthcare` 的 dev
  网关」在散文里的写法 —— **两者是同一台机**。`hosts.toml` 里 `qy-gw-120` 的 `aka`
  已同时登记 `["95", "120"]`。
- 95 上的 `aiops-120-world-tunnels` 一类 tunnel 单元属于公司既有栈，不是我们的。
