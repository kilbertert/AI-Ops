# 95 环境事实

> **用途**：回答「95 是什么、在哪、怎么连、里面有什么」—— 不依赖会话记忆，也不需要
> 去别的文档里拼三处半句。
> **最后核验**：2026-11-13（受管 SSH、进程健康与配置位置）；旧入口与数据盘点注明原日期。
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

## 2. 怎么连（区分目标与中继故障）

`dev-host read 95 <path>` / `dev-host ls 95 <path>` / `dev-host find 95 ...` 即可用；
它是服务类主机，`exec` 需显式 `--allow-service-exec`，写入需产物身份。

2026-11-13：开发机直连仍在 6 秒内超时，但**现有受管中继路径连续三次成功**，
耗时 6.17 / 6.38 / 5.71 秒，使用原有公钥；没有使用口令、修改 SSH 配置或重启服务。
同日较早失败的错误指向中继连接超时，不能据此判断 95 宕机或密码错误。
中继后来在线这一事实不能确定历史故障由休眠、重启还是网络中断造成。

当前路径以库外主机清单和 SSH 配置为准，不再按旧 HK 中继记录推断。
`dev-host` 到达前仍校验目标主机公钥；中继地址、密钥与历史操作记录保留在私有运维清单。
2026-10-20 的直连抓包零 SYN 是当时的观测，不足以确定当前网络丢弃位置。

```bash
# 原受管路径的最小复验：必须返回 SSH_OK，不能只用直连 TCP 判断主机状态。
dev-host exec 95 --allow-service-exec --timeout 35 -- printf SSH_OK
dev-host exec 95 --allow-service-exec -- systemctl is-active aiops-gateway.service
dev-host exec 95 --allow-service-exec -- curl -fsS --max-time 5 http://127.0.0.1:8788/health
```

若超时，先在本机查看有效 SSH 路径并区分中继和目标；敏感输出只保存在私有证据目录。
路径恢复后重新核对进程与健康，不以外部域名的重定向或 403 代替本机健康检查。

## 3. 95 上到底有什么

| 项 | 值（注明核验时点） |
|---|---|
| 服务单元 | 2026-11-13：`aiops-gateway.service` active，`User=aiops` |
| 运行目录 | 2026-11-13：`/home/aiops/AI-Ops`；2026-10-20 检查无 `.git` |
| venv | 2026-10-20：`/home/aiops/AI-Ops/.venv`；旧 CLI 记录不是现行运维入口 |
| 进程环境文件 | 2026-10-20：`/home/aiops/gateway.env`；本轮只读取进程配置项名称 |
| 服务端配置 | 2026-11-13：`/home/aiops/.config/aiops-diagnostics/acceptance.env` 存在 |
| Gateway DB | 2026-11-13：`/home/aiops/.local/share/aiops-diagnostics/gateway/gateway.db` 存在 |
| 版本 | 2026-11-13：本机 `/health` 报 `0.1.0`、`business_mutations=disabled`，无提交来源标记 |

**声明态与历史空白**：

- 仓库已有 [`ops/environments/env-95.toml`](../../ops/environments/env-95.toml)，
  是历史部署演练清单，**不是已部署生产配置**。旧“从来没有过”的描述已失效。
- 2026-11-13：目标运行目录仍无 `ops/environments/`；不能因仓库有模板就断言生产已有声明态。
- 2026-11-13：以 SQLite `mode=ro`、`query_only=ON` 重查，`agents` / `agent_versions` /
  `conversations` / `conversation_turns` / `runs` / `devices` 六张表仍各 0 行，
  `user_version=0`，没有 `dify_workspaces` 表；只证明旧 Gateway 库，不证明公司业务库为空。
- 2026-11-13：服务端文件有 UPMS、Redis、MySQL、TDengine 非空配置项；文件和进程配置项
  均无显式 CPMS、Dify、环境身份键。本轮只输出名称与计数，**未验证上游可达或配置有效**。
- 2026-10-20：带服务身份的 `GET /v1/faq/catalog` 返回 `401 INVALID_ACCESS_TOKEN`。
  本轮没有真实用户令牌验证，不能据此判定当前认证或业务链可用。

当前可确认的是**受管 SSH 可用、旧版 Gateway 运行**。第二环境的认证、CPMS、时序与离线源、
Dify 工作区和真实用户使用情况还需核对；管理访问成功不代表跨环境业务已接通。
未观察到实际流量前继续按“在用”处理，不迁移入口或退役进程。

后续盘点须在目标侧处理配置，只输出键名、是否非空和验证结果，不打印文件或进程环境全文。
Gateway 数据库用 `file:<path>?mode=ro` 及 `PRAGMA query_only=ON`，仅查表结构与必要计数；
不启动现行 store 构造器，以免自动迁移旧库。

## 4. 不要把 95 和 120 混为一谈的地方

- **`120` 有两个含义**：既是这台机自己的代号（地址末段），又是「`healthcare` 的 dev
  网关」在散文里的写法 —— **两者是同一台机**。`hosts.toml` 里 `qy-gw-120` 的 `aka`
  已同时登记 `["95", "120"]`。
- 95 上的 `aiops-120-world-tunnels` 一类 tunnel 单元属于公司既有栈，不是我们的。

## 5. 新实例重建

用户已指定95清理旧AI-Ops并验证平台自维护环境登记。构建、安装、退休与恢复方法见[重建手册](../../ops/environments/env95/README.md)。状态以本轮验收记录为准，资产存在不表示已执行。

2026-11-14：新Gateway已从合并制品重建为1.10.2+b1e509be1de7，旧服务停止但尚未退休；真实租户准入通过，完整业务仍待验。[本轮验收](../validation/2026-11-14-env95-live-acceptance.md)、[共享服务接入](env95-shared-services.md)。
