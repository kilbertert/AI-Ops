# 59. 一个公司租户一个 Dify workspace，按 `(租户, 环境)` 解析

日期：2026-10-20
状态：已采纳（设计定型，**尚未实施**）
影响版本：`src/aiops_diagnostics/dify_login_provision.py`（接 `tenant_id`）、
`src/aiops_diagnostics/dify_login_service.py`（映射表加 workspace 维度）、
`src/aiops_diagnostics/dify_console_publish.py`（workspace 按发起人解析）、
`src/aiops_diagnostics/gateway_api.py`（试运行的草稿来源）、
网关新增 `dify_workspaces` 表与 `AIOPS_GATEWAY_ENVIRONMENT` 配置

## 背景

ADR-0056 记录了 V1 的单工作区暂定，并写明触发条件是「出现第二个需要自行编辑智能体的租户」。
2026-10-20 用户确认该条件已满足，并指定 41 与 95（`prod-bugtrack-120`）作为多租户环境。

实测证明这个缺口是**真实的越权**，不是理论风险：用 `provision_editor` 新开一个账号，
它一开局就能看到并导出全部 3 个 app 的完整可编辑配置（`/export` 200，4975 字节）——
`editor` 在单一 workspace 内是平权的。

### 一个被推翻的前提：公司 JWT 里没有 `domain`

用户最初转述后端同事的说法：JWT 里除 `tenant_id` 外还有一个 `domain`，据它能判断租户属于哪个区域。

全量核实后否定：`cloud-upms:release` 的 `issueLoginToken` 写出的 claim 是
`id, user_id, username, organ_id, type, tenant_id, system_id, shop_id, role_ids, tenant_ids, shop_ids`。
`cloud-auth`（8 分支）、`cloud-gateway`（11 分支）、`qumall-common`（18 分支）同一集合，**没有 `domain`**。
全仓 `grep domain` 的三个命中都与环境无关（Node 遗留 ORM、死配置 `DeviceProperties.domain`、
`sys_organ.second_domains` 是"二级域名列表"且 Java 侧零引用）。
也没有按区域选路的代码：动态数据源设施存在但零使用者；环境区分发生在部署层
（Nacos namespace、独立数据库、镜像 tag `dev/` vs `release/`）。

⇒ **环境只能由我们的部署身份声明。**

## 决策

**Dify 只有一套，是平台级共享的。41 与 95 是两个客户环境，各自有一套 UPMS + 一个网关 +
一个登录代理。租户隔离发生在一个公司租户一个 Dify workspace 这一层 —— 与它在哪个环境无关。**

```
        ┌─────────────────────────────────┐
        │  CLM / SaaS / Dify / AI APP     │   ← 平台层，一套（Dify 在 36）
        └───────────────┬─────────────────┘
                        │
            ┌───────────┴───────────┐
            ▼                       ▼
        41 (AI-Ops)              Si (95)     ← 两个客户环境，各一套 UPMS + 网关 + 登录代理
            ▲                       ▲
            │                       │
          APP (前端)              APP (前端)
```

**每个需要自行编辑智能体的公司租户，在这套 Dify 里有一个独占的 workspace。**
开户、发布、试运行三条路径都按 `(tenant_id, environment)` 解析出 `workspace_id`，
解析不出就 fail-closed。`environment` 列的作用是**防止两套环境的 `tenant_id` 撞号**，
不是"每个环境各建一套 workspace"。

**登录代理按环境分开部署**：一个实例只打一套 UPMS。不按请求挑 —— 公司登录响应里没有
环境标识，猜错的表现是"用户名或密码错误"，排查成本极高。

配套的九条：

| 项 | 决定 |
|---|---|
| 环境的权威来源 | `AIOPS_GATEWAY_ENVIRONMENT`（每台机器一个值）+ 登录代理侧同名的部署身份；落进 `dify_workspaces.environment` |
| 映射 | 新表 `dify_workspaces(tenant_id, environment, workspace_id, setup_state, lease_owner, lease_expires_at, last_error, created_at)`，`UNIQUE(tenant_id, environment)`；`dify_app_registry` **不动** |
| 建 workspace | `POST /inner/api/enterprise/workspace`（`is_from_dashboard=True` 绕过 `ALLOW_CREATE_WORKSPACE`），需开 `INNER_API` + 一把只绑回环的 key |
| 模型凭据 | **每个 workspace 各配一份**。Dify 数据模型里没有共享层（见下），用 `ADMIN_API_KEY + X-WORKSPACE-ID` 写 |
| 模板 app | 模板 DSL 进仓库；**只含结构与工具白名单，`model_config` 留空**。导入失败只记状态，不阻塞登录 |
| 发布权 | 仍由公司权限码裁定；钉住的 workspace 判据改为「**发起人所在**的那一个」 |
| 试运行 | 草稿来源按发起人解析；租户没有 workspace ⇒ **明确拒绝**，不回落平台工作区 |
| 平台租户 | 编辑面**只在自己的平台 workspace**；"看全部"只体现在试运行订单范围与发布审计 |
| 一人一租户 | 接受并写进文档（Dify 的 `current` 只有一个）；判据是 `sys_user.tenant_id` 非空且单值 |
| `DIFY_CONSOLE_*` 配置 | `BASE_URL` / `API_KEY` 保持单份（Dify 平台级）；只有 `WORKSPACE_ID` 改为查表 |
| 旧共享 workspace 的退出 | 先按租户复制 app 副本 → 逐个租户切换 → 最后撤旧访问权；**不删**旧 workspace |

### 为什么模型凭据必须每个 workspace 一份

- `provider_credentials.tenant_id` / `providers.tenant_id` 都是 `NOT NULL`，无外键、无共享表；
- 凭据用**每租户一把 RSA 密钥**加解密（`tenant.encrypt_public_key` + `privkeys/{tenant_id}/private.pem`）
  ⇒ 复制行到别的 `tenant_id` 下也解不开；
- 跨租户同步只在 `DEPLOYMENT_EDITION == ENTERPRISE` 才入队；本机是 `COMMUNITY`。

且新建 workspace 是**完全空白**的：只有 `tenants` 一行、6 行插件升级策略、RSA 密钥对、
credit pool、一条 `current=false` 的 owner membership —— 没有 app / dataset / provider / 插件。
里面建得出来 app（模板回落 `openai/gpt-4o`），但跑不起来。

### 为什么用 `X-WORKSPACE-ID` 而不是 `POST /workspaces/switch`

`switch_tenant` 会把该账号其它 join 的 `current` 全置 false 再 commit —— `load_user` 每个请求都重读
`current=true`，所以切换对同一账号的**并发请求可见**。`ADMIN_API_KEY + X-WORKSPACE-ID`
只在请求上下文里注入 `current_tenant`，不写库（`ext_login.py:74-90`，且 admin key 请求跳过 CSRF）。
后台自动化必须用后者。

## 被否掉的替代形态

- **等公司提供 `domain` claim / 从 `/upms/user/info` 读一个环境字段**。核实结果是公司侧
  根本没有这个维度；把不存在的字段写进契约，只会让下一个人以为读不到是配错了。
- **一个 workspace + 按租户分 app**。Dify 1.17.1 的 `App.tenant_id` 是 workspace 级，
  没有"app 属于哪个租户"的字段。靠命名约定做隔离 = 没有强制力的隔离。
- **每个租户一个独立 Dify 实例**。运维成本与版本漂移放大一个数量级，而 workspace 模型够用。
- **平台账号能进所有租户 workspace**（"平台可见全部"的最简读法）。`current` 只有一个，
  跨 workspace 会互相踩；而且"能改所有租户智能体"比"能看所有订单"大得多。
  要以某租户身份操作就走显式、可审计的动作。
- **给 95 也部署一套 Dify**（我最初把"环境"读成"Dify 也按环境分"）。Dify 是平台级的，
  只有一套；95 缺的是它自己的登录代理与控制台入口，不是 Dify。把 Dify 按环境复制，
  会让"同一租户的智能体在哪"变成一个必须跨环境同步的问题 —— 而那正是这次要消掉的东西。
- **在 Dify 里做知识库**（用户考虑过又否决）。RAGFlow 那条路已经跑通且 ADR-0049 的深度修复
  作用在它上面；改过去要重建全部索引，且 Dify 的 `/v1/.../retrieve` 对 external dataset
  根本不工作，vendor dataset 的 `VECTOR_STORE` 在这台机器上还没配。
- **开户失败就回滚整个 workspace**。建 workspace 是幂等的、失败可重试；回滚要删 tenant，
  而 Dify 没有删 tenant 的 API。改成"只记状态 + 可重试"。

## 后果与代价

- **每次开户多 4 个网络往返**（建 workspace、装插件、写凭据、导模板），且每一步都可能失败。
  失败不阻塞登录，但会产生"登得进去、编辑面是空的"这个状态 —— 必须让它可见（明确的空态文案）。
- **模型凭据要按 workspace 逐个维护**。轮换 key 时是 N 个 workspace 各写一次，不是一个地方。
- **一人一租户成为硬约束**。跨租户的人要么开两个公司账号，要么这个设计不适用。
- **迁移不可逆的那一半**：把一个已有账号从旧 workspace 移除会把它的 app/dataset 转给 owner，
  且 Dify 1.17.1 没有反向操作（ADR-0023 已记）。所以旧共享 workspace 的处理必须**先冻结、再逐个搬**，
  不能"删了重建"。
- **`AIOPS_GATEWAY_DIFY_CONSOLE_*` 的形状**：`BASE_URL` 与 `API_KEY` 保持单份（Dify 是平台级的），
  只有 `WORKSPACE_ID` 改为查 `dify_workspaces`。配置项数量不变，语义变了。
- **workspace 的 owner 是一个服务身份**（`dify-admin@aiops.local`），不是每个租户一个人。
  `create_owner_tenant` 允许同一账号在 N 个 workspace 里都是 owner，而"以它的身份在某 workspace
  做事"走的是 `ADMIN_API_KEY + X-WORKSPACE-ID`（按 workspace 找 owner 行，不碰 `current`）。
- **`environment` 列保留**，尽管今天只有一个值：它防的是"两套独立 UPMS 的 `tenant_id` 撞号"，
  而撞号时解析函数会静默串到另一个租户的 workspace。本仓已有"同名不同物"的前车之鉴。
- **映射表落在网关的 `gateway.db`**，代理与桥经网关的只读端点查 —— 三个进程各自的私有库
  装不下一份共享真值。代价是代理多一次回环调用。
- **已有账号的迁移走 inner_api 的 member 端点**（`join_enterprise_workspace_member`），
  **不是**邀请：`RBAC_ENABLED=False` 时邀请已激活账号会静默失败，而移出旧 workspace 不让它变
  pending —— 结果是两头都进不去。这是评审抓到的、原设计里会**锁死存量用户**的缺陷。
- **workspace 名字是确定性派生**（`aiops-{environment}-{tenant_id}`），且租约抢占前必须
  先按名字对账 —— 租约保护的是我们的映射行，不是 Dify 的创建请求。`tenants.name` 在 Dify
  里**没有唯一约束**，唯一性靠命名约定；Dify 允许改名，所以改名会退化成"对不上 ⇒ 重建 ⇒
  孤儿"，缓解是不给运营改名的入口。
- **`account_email_for` 要加环境前缀**。这是评审从既有代码里挖出来的真实缺陷：
  它只按公司用户 id 生成邮箱，而两套 UPMS 的 `user_id` 各自生成；
  一旦撞号，两个人共用一个 Dify 账号（账号是全局的，不属于 workspace），
  第二个环境的代理就能为它签会话、进第一个环境的 workspace。这是**迁移**而非加字段。
- **workspace 查询端点不接受 `environment` 参数**，环境由网关的部署身份补上 ——
  否则"跨环境查同一个 `tenant_id`"就成了一条真实越权路径。
- **凭据轮换是运行手册，不是代码**：`SECRET_KEY` 要 Dify 与代理一起重启（ADR-0023 不变）；
  provider 凭据按 `dify_workspaces` 枚举逐个重写。

设计全貌与三条路径的改法见 `docs/agents/dify-multitenant-design.md`。
