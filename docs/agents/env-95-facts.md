# 95 环境事实

> **用途**：回答「95 是什么、在哪、怎么连、里面有什么」—— 不依赖会话记忆，也不需要
> 去别的文档里拼三处半句。
> **当前状态**：2026-11-14已撤回AI-Ops部署，见第8节。前七节是试验与恢复资料，不代表当前在运行。
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

以上为2026-11-13重建前结论；2026-11-14已安装独立新实例，当前结果见第5节。
历史访问资料仍保留；停止旧进程与退休旧文件是分开的步骤。

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

2026-11-14：首次重建1.10.2之后已升级到`1.10.3+ccd9d8a37c2c`；旧服务停止但尚未退休。
独立节点部署资产为`6c06e0bd`，真实准入、知识与部分模型业务链通过；完整业务仍待验。
[最新验证](../validation/2026-11-14-env95-business-acceptance.md)、[共享服务接入](env95-shared-services.md)。


## 6. 新实例接入与复验方法

实际版本及通过范围见[本轮业务验证](../validation/2026-11-14-env95-business-acceptance.md)，
新路径/恢复方法见[重建与升级](../../ops/environments/env95/README.md)。不要复用第3节旧配置启动新代码。

1. 先用`dev-host exec 95 --allow-service-exec`核对`aiops-gateway-95`、`aiops95-node-runtime`、
   `aiops95-node-callback`、`aiops95-shared-services-tunnel`状态与本机`/health`版本。
2. 正常管家手机登录采用公司当前`/token/loginByPhone`（配置的UPMS本机入口），随后`/user/info`核身份；
   原商城消费者使用`/mallapi/userinfo/phone/login`取得thirdSession。凭据从加密资产清单运行时读取，
   只经进程内存/标准输入传递，不记录完整令牌。正常登录不能证明原H5页已通过。
3. 对同一消费者先直达Gateway再走原API域名：本机通过而公网401时，核对原代理注入的服务凭据是否与
   `THIRD_SESSION_SERVICE_TOKEN`一致，只报告是否匹配。备份具体rewrite文件，修改原/v1块的该项值，
   `nginx -t`通过后reload；失败恢复原文件。不得顺带开放管理路径。
4. 订单过程工具需要`AIOPS_GATEWAY_OPERATOR_TSDATA_URL`指向本环境公司tsdata门面，代码追加
   `/tdadmin/data/query`。先做有界只读查询核实际响应，再备份单项配置、零在飞检查、重启并复验。
   无需为此修复旧TDengine用户或扩展数据库权限。
5. 客服知识运行需KB地址和**本环境独立**`AIOPS_GATEWAY_MEDIA_SIGNING_SECRET`；开户、能列库不代表
   运行时检索已装配。模型provider沿用已批准配置，按现役工作流关闭思考输出，正常保存/发布后验真实正文。
6. 新工作区通过现有`self_service_prepare`准备模型、插件、工具及应用，`service_keys.ensure`持久化应用密钥，
   `SelfServiceStore.register`登记目标。发布使用正式发布服务或同源`sync_workflow_from_dify`、版本与路由推进接口，
   不直接改版本/路由SQL；不得在真实库构造一次性Gateway app来验收，因为启动会恢复未完成作业。
7. 节点能力必须来自真实Java定时回调，读取`execution_node_capabilities`的环境、节点、摘要与更新时间；
   不手工插入心跳。需先配置本环境profile和受管目标，单配agent_run_config_file不足以授权能力回报。
8. 初装模型模板的model_options应与平台实际生效值一致。Dify编辑模型选项不自动改平台模型配置；
   不能用临时猜测参数填清单。保存原模板、正常发布并重跑真实请求，不关闭节点校验。

运行前后通过只读账本关联同一个QA/run和发布版本，分别记录订单取证、离线统计、客服知识与本人订单。
上游接口系统错误不是模型不理解问题；先只读定位具体公司进程日志与内存限制，不通过替换身份或直读越权数据绕过。
公司业务进程重启/参数变更需要该项授权；原AI-Ops重建授权不自动覆盖公司业务维护。

## 7. 仍待验收的入口

现有公网API知识问答通过，不代表管家H5、消费者H5或Dify管理页均已接通。
已观察到原管家H5登录停在提示页；公司接口恢复后仍须从正常登录驱动原聊天页面。
本环境登录代理和管理桥尚未完成原页面开通/发布/恢复验证。旧运行文件保持可恢复，待入口验收后再退休。

节点资源验证使用容器统计与轻量TCP探针；不要在320MiB运行节点内另起Java源码编译/执行器。
本轮此做法触发过OOM，节点自动重启不能把探针失败记为无影响；保留计数并复核能力回执恢复。

## 8. 用户要求撤回后的状态与核对方法

95不再是后续AI-Ops部署目标。新旧运行目录、配置、数据及systemd单元已移入私有归档；
新增容器/网络/精确防火墙规则、AI代理块和共享宿主独立公钥已移除。公司业务库、既有索引、其他服务和共享镜像保留。
共享Dify/知识资源保留为试验记录，不能因资源仍存在把95标成在线。

撤回前核零在飞与原入口实际访问样本，备份确切配置；仅移除本次AI配置，nginx语法校验后生效。
恢复是独立部署决定，不能直接照第6节重启。归档清单、摘要与授权记录在私有运维证据中。

```bash
# 只读复核；not-found/inactive是撤回后的预期，不据此启动服务。
dev-host exec 95 --allow-service-exec --timeout 40 -- systemctl show aiops-gateway-95.service -p LoadState -p ActiveState
dev-host exec 95 --allow-service-exec --timeout 40 -- 'docker ps -a --format "{{.Names}}"'
dev-host exec 95 --allow-service-exec --timeout 40 -- 'ss -ltnH'
dev-host exec 95 --allow-service-exec --timeout 40 -- 'free -m'
```

输出仅在私有证据目录保留；核对本轮AI容器/端口缺席与归档根，不把其他服务当清理对象。
公司进程核对仅返回PID、启动时间与预先选定的JAR名称，不输出完整命令行或环境变量。
2026-11-14 13:10–13:16 UTC再次访问受中继超时阻塞；早先公司PID已变化，当前健康和变更来源未知。
内存与因果范围见[撤回验收](../validation/2026-11-14-z95-withdrawal-and-gateway-plan.md)。
