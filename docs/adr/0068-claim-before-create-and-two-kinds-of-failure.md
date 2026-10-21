# 68. 建 workspace 的顺序是"先占位、再建"，失败分两侧

日期：2026-10-21
状态：已采纳（#880 实施；ADR-0059 §5.1 的建法落成代码）
影响版本：`src/aiops_diagnostics/dify_workspaces.py`（`claim` / `mark_*` / `workspace_name`）、
`src/aiops_diagnostics/dify_inner_api.py`（新）、`src/aiops_diagnostics/dify_ensure_workspace.py`（新）、
`src/aiops_diagnostics/admin_cli.py`（`ensure-workspace`）、`gateway_config.py`（inner_api 两键）

## 背景

ADR-0059 定下了「先占位、再建」与「`creating` 要带租约与三种结局」，但没有把它们写成
代码。这一张票把它们落成，并且必须先把两件事钉死 —— 它们都是"错一次就留下永久后果"的：

- **Dify 没有删 workspace 的 API**。建出来的就是永久的。所以"建了两个"不是一次重试，
  是一个永久的孤儿。
- **`tenants.name` 在 Dify 里没有唯一约束**（只有 `tenant_pkey`）。所以唯一性只能来自
  我们自己的命名约定，而对账时"命中几个"必须当成一个需要判断的事实，不是一个循环。

## 决策

**一、`claim` 是唯一能串行化的动作，且它对每种状态的处理必须写死。**

| 起点 | `claim` | 理由 |
|---|---|---|
| 没有行 | True（插入 `creating`） | 先占位 |
| `failed` | True（接管） | 上一轮明确失败，`workspace_id` 仍是空 |
| `creating` 且租约**已过期** | True（接管） | 创建进程崩在中间；没有租约这条会永远卡住 |
| `created` / `ready` | False | 已经做好 |
| `creating` 且租约**未过期** | False | 别人正在做 |
| `unknown` | False（除非调用方声明"已对账过"） | 可能已经建成，直接抢会再建一个 |

`unknown` 的例外（`allow_unknown=True`）由开户流程在对账**命中 0 个**之后给出 ——
"能不能接管"这件事的知识在对账那一步，不在表这一层。

**二、两种失败必须分开，因为调用方的处置相反。**

| 结局 | 判据 | 写入的状态 | 调用方 |
|---|---|---|---|
| 明确失败 | 4xx/5xx，或连接被拒 | `failed`（`workspace_id` 保持空） | 可以重建 |
| 结果未知 | 超时 / 读中断 / 200 但没有 `tenant.id` | `unknown`（`workspace_id` 原样保留） | **先对账**，不重建 |

这一条是这一张票里最容易写错的地方：`bounded_http` 把"连接被拒"与"超时"都归到
`unavailable` —— 从客户端看它们确实都是"没拿到响应"，但**对建 workspace 而言后果相反**。
所以分类看的是 `HttpFailure.cause` 的类型（`Timeout` / `RemoteDisconnected` /
`IncompleteRead` / `BadStatusLine`），而不是那条粗分类。

**三、抢占之前必须先对账，且对账结果不是一个布尔。**

按**派生名**（`aiops-{environment}-{tenant_id}`）精确匹配 `GET /console/api/all-workspaces`：

| 命中 | 动作 |
|---|---|
| 0 | 安全，接管并重建 |
| 1 | **不重建**，把 UUID 补写进映射，`state='created'` |
| >1 | 拒绝并要人工（`DIFY_WORKSPACE_AMBIGUOUS`）—— 有人手工建过同名 workspace，选错就是串租户 |

**四、inner_api 的地址是配置项，不是代码。** 今天写的是 api 容器的内网 `IP:5001`
（36 本机可用）；转发器上线后换成 `http://127.0.0.1:15001`，**代码一行不改**。
这正是设计里"否则『等转发器』就变成了一次重写"那句话的落点。

## 被否掉的替代形态

- **先建后写映射**。这是最自然的写法，也是唯一会造孤儿的写法：两个并发首次登录都能越过
  "表里没有行"这个检查，各建一个，后写的赢。实测（本票的并发用例）它会稳定地发两次
  `POST`；把 `claim` 换成非原子的版本后，那条用例立刻变红。
- **超时按失败处理，重试即可。** 请求可能已经到达 Dify。重试的产物是一个永久孤儿，
  而它看起来与"正常重试成功"一模一样。
- **租约过期就直接接管。** 租约保护的是**我们那行映射**，不是 Dify 的创建请求 ——
  丢失响应、UPDATE 失败、请求仍在 Dify 里跑，都会在租约到期后让下一个运行再建一个。
- **对账命中多个时自动选一个。** Dify 允许改 workspace 名，也允许手工建；两个同名
  workspace 时机器没有依据判断哪个是这个租户的，而选错就是把两个租户并在一起。
- **在 36 上跑开户。** `dify_workspaces` 在每个环境自己的 `gateway.db` 里，36 写不到 41 的库。
  让库的属主写自己的库，就没有跨主机写，也不需要为"写失败"设计跨机对账。
- **用 `/console/api/workspaces/switch` 切工作区。** 它会把该账号其它 join 的 `current`
  全置假并 commit，对并发请求可见；后台自动化走 `ADMIN_API_KEY + X-WORKSPACE-ID`。

## 后果与代价

- **建出来的 workspace 仍是空壳**（无插件、无 provider 凭据、无 app）。这是刻意的边界：
  #881 才让它能用。`created` 与 `ready` 因此是两个状态，而解析函数对两者都返回 id。
- **每个 workspace 的 owner 是同一个平台服务身份**（`dify-admin@aiops.local`）。一个账号在
  N 个 workspace 里都是 owner 是允许的；我们从不以它的会话做事。
- **`all-workspaces` 需要 admin key**，也就是 `AIOPS_GATEWAY_DIFY_CONSOLE_API_KEY` 必须是
  Dify 的 `ADMIN_API_KEY`。41 上那把（2026-10-21 实测）打这个端点回 200，所以对账路径
  在 41 上成立；换一把普通 console 会话令牌会让对账在 401 上失败，而失败文案会说
  "查不到 workspace 列表 ⇒ 按未知处理"。
- **转发器还不存在**（#889 才交付）。在那之前 41 的 `ensure-workspace` 只能经容器内网
  IP 调用，而那只在 36 本机可达 —— 所以**本票的验收在 36 上做**，41 上做要等 #889。
