# Dify 多租户：grilling 结论

> **状态**：grilling 已完成，用户已确认（2026-10-20）。设计已定型，**尚未实施**。
> **读者**：要把它拆票或实施的人。本页是**决定**，不是实施计划。
> 现状（为什么只有一个工作区）见 `dify-tenant-integration.md` 与 ADR-0056。

---

## 0. 一句话

**Dify 只有一套，在 36 上，是平台级共享的。41 与 95 是两个客户环境，各自有一套 UPMS + 一个
aiops-gateway + 一个登录代理，各自服务自己的租户。租户的隔离发生在「一个公司租户一个
Dify workspace」这一层 —— 与它在哪个环境无关。**

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

**三个容易混的层，各自的范围：**

| 层 | 实例数 | 它决定什么 |
|---|---|---|
| **Dify** | **一套**（36） | 所有租户的 workspace 都在这一个 Dify 里 |
| **环境** | 每客户一套（41、95） | 一套 UPMS + 一个网关 + 一个登录代理 + 一个业务库 |
| **租户** | 每客户环境里若干 | 公司 `sys_organ` 一行；在同一套 UPMS 里 |

所以：**"环境"不是 Dify 的维度，是部署的维度。** 一个租户属于哪个环境，由"它的账号存在于哪套
UPMS 里"决定 —— 41 的租户换不到 95 去登录，因为 95 的 UPMS 里没有这个人。

`dify_workspaces` 表因此仍然要带 `environment` 列，但它的用途是**防止两套环境的 `tenant_id`
撞号**，而不是"给每个环境各建一套 workspace"。两个环境的同名租户 id 如果撞上，
`(tenant_id, environment)` 才能分辨它们其实是两个不同的租户。

---

## 1. 领域语言（这次新增/修正的）

| 词 | 定义 |
|---|---|
| **环境（environment）** | 一套独立部署的 **UPMS + aiops-gateway + 登录代理 + 业务库**。**是部署身份**，**不含 Dify**。41 与 95 是两台机器、两套库、两个域名。 |
| **租户工作区** | 一个公司租户在**某个环境**里独占的 Dify workspace。标识是 Dify 的 `tenants.id`（UUID）。 |
| **平台工作区** | 平台账号（`type=-1`）自己的 workspace；**不**含别人的编辑面。 |

已写入 `GLOSSARY.md`。

### 「环境」不是公司 JWT 的字段 —— 这是个被推翻的前提

用户最初转述后端同事的说法：JWT 里除 `tenant_id` 外还有一个 `domain`，据它能判断租户属于哪个区域。
**2026-10-20 全量核实后否定**：

```
cloud-upms:release  TokenController.issueLoginToken 的完整 payload：
  id, user_id, username, organ_id, type, tenant_id, system_id, shop_id,
  role_ids, tenant_ids(＜10 时), shop_ids(＜10 时)
```

`cloud-auth`（8 个分支）、`cloud-gateway`（11 个分支）、`qumall-common`（18 个分支）的 claim 集合相同，
**没有任何 `domain`**。`grep domain` 在三套 Tech stack 里的命中：Node 遗留 ORM 的 `blade_tenant.domain`（零引用）、
`DeviceProperties.domain`（死配置）、`sys_organ.second_domains`（"二级域名列表"，Java 侧零引用）。

也没有按区域选路的代码：网关不做环境路由，动态数据源设施存在但**零使用者**，
环境区分发生在部署层（Nacos namespace、独立数据库、镜像 tag `dev/` vs `release/`）。

⇒ **"环境"必须由我们的部署身份声明，不能从令牌读。**

---

## 2. 已定型的决定

| # | 决定 |
|---|---|
| 1 | 智能体是**租户资产**，不是平台资产 |
| 2 | 需要自行编辑智能体的租户**各有一个 workspace**；不需要的走平台工作区 |
| 3 | 隔离深度：**app + dataset 跟租户走**；provider credentials **跟 workspace 走**（Dify 数据模型决定的，见 §3） |
| 3b | 改造面是**四条**：开户 / 发布桥 / 试运行 / **运维 CLI（pull-dify、sync-from-dify）**，共用同一个解析函数 |
| 4 | 已有映射账号：迁移进各自租户的 workspace；**不保留**旧的共享 workspace 访问权 |
| 5 | 建 workspace 走 **`POST /inner/api/enterprise/workspace`**（`is_from_dashboard=True` 绕过 `ALLOW_CREATE_WORKSPACE`）。**`INNER_API` 已于 2026-10-20 在 36 上打开** —— 见 §8 的「−0」 |
| 6 | 模板 app：**模板 DSL 进仓库**，开户时导入。模板**只含结构与工具白名单**，`model_config` 留空（避免绑死模型） |
| 7 | 发布权：`aiops:agent:publish` 仍然按公司权限码裁定；**平台租户（`type=-1`）视为可见全部** —— 但见 §4 的边界 |
| 8 | 试运行草稿来源：租户没有 editor 环境就**明确拒绝**（"你的租户没有编辑环境"），不回落 |
| 9 | 映射存在 **新表 `dify_workspaces(tenant_id, environment, workspace_id, setup_state, lease_owner, lease_expires_at, last_error, created_at)`**，落在**网关的 `gateway.db`**；代理与桥经网关的只读端点查。`dify_app_registry` 不动 |
| 9b | `dify_workspaces.environment` 的作用是**防止两套环境的 `tenant_id` 撞号**，不是「给每个环境各建一套 workspace」。**保留** —— 撞号时会静默串租户，fail-closed 值得一列 |
| 9c | workspace 的 **owner 是服务身份**（`dify-admin@aiops.local`），不是每个租户一个人；以它的身份在某 workspace 做事走 `ADMIN_API_KEY + X-WORKSPACE-ID` |
| 9d | **`account_email_for` 加环境前缀**（`{environment}-{user_id}@…`）。这是既有代码里的真实缺陷：两套 UPMS 撞 `user_id` 会让两个人映射到同一个 Dify 账号。属**迁移**，要在多 workspace 之前或同批完成 |
| 9e | workspace 查询端点**不接受 `environment` 参数**，环境由网关自己的部署身份补上；`tenant_id` 可传，但只返回一个 UUID |
| 9f | 开户用**先占位再建**（`UNIQUE(tenant_id, environment)` + `setup_state='creating'`）串行化并发；`CREATING` 与 `UNRESOLVED` 是两个不同的错误码 |
| 10 | 知识库**继续用 RAGFlow 外接**（用户明确否决"改用 Dify 自带库"） |
| 11 | 工具白名单**仍走我们的 harness** |
| 12 | 一人一租户：**接受**，并写进文档（Dify 的 `current` 只有一个，跨 workspace 会互相踩） |
| 13 | **95 与 41 是环境**（各一套 UPMS + 网关 + 登录代理）；**Dify 只有一套**（36），平台级共享 |
| 13b | **登录代理按环境分开**：41 一个实例打 41 的 UPMS，95 一个实例打 95 的 UPMS。代理不按请求挑 UPMS |
| 14 | 环境写在哪：**`AIOPS_GATEWAY_ENVIRONMENT` 部署身份**（每台机器一个值），并落进 `dify_workspaces.environment` |
| 15 | `platform_code` **不能**当环境（默认值全局 `QUSHIYUN`，无区分度）；读不到就 fail-closed |
| 16 | 解析失败统一 **`DIFY_WORKSPACE_UNRESOLVED`**，三处（开户/发布/试运行）**共用同一个解析函数** |
| 17 | 模板导入失败：**只记录状态**，不阻塞登录（登录仍成功，编辑面为空且明确告知） |
| 18 | 迁移分两步：先建表+解析函数（行为不变），再切三条路径 |
| 19 | `AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL` / `_API_KEY` 保持**单份**（Dify 是平台级的）；只有 `_WORKSPACE_ID` 改为查表 |
| 20 | 旧共享 workspace：**先按租户分派 app 副本到各新 workspace，再撤旧访问权**；不删旧 workspace |
| 21 | 一人一租户的判据：`sys_user.tenant_id` **非空且单值**（`sys_user` 一行只属于一个 `sys_organ`）；跨租户的人在公司侧本就存在多个账号 |

---

## 3. 为什么 provider 凭据必须每 workspace 各配一份

Dify 1.17.1 的数据模型里**没有**跨租户共享凭据：

- `provider_credentials.tenant_id` 与 `providers.tenant_id` 都是 `NOT NULL`，无外键、无共享表；
- 凭据用**每租户一把 RSA 密钥**加解密（`tenant.encrypt_public_key` + `privkeys/{tenant_id}/private.pem`）
  ⇒ 即使把行复制到别的 `tenant_id` 下也解不开；
- 跨租户同步只在 `DEPLOYMENT_EDITION == ENTERPRISE` 才入队，本机是 `COMMUNITY`。

而且**新建 workspace 是空的**：只产出 `tenants` 一行、6 行插件升级策略、RSA 密钥对、credit pool、
一条 `current=false` 的 owner membership。**没有 app、没有 dataset、没有 provider、没有插件**。
里面建得出来 app（回落到模板的 `openai/gpt-4o`），但跑不起来（`ProviderTokenDoesNotExist` 或 500）。

⇒ 开户流程里必须包含：装插件 + 用目标 workspace 的身份写一次凭据。

**写凭据的正确姿势**：`ADMIN_API_KEY` + `X-WORKSPACE-ID` 头。
这是唯一"按请求选 workspace、不碰 DB `current`"的机制（`ext_login.py:74-90`，且 admin key 请求跳过 CSRF）。
`POST /workspaces/switch` 会改 DB 的 `current`，对同一 owner 账号的并发请求可见 —— 不能用它做后台自动化。

---

## 4. 一处必须写清的边界：平台租户"可见全部"到什么程度

grilling 里 Q7 与 Q19 的答案看起来冲突，实际是两个层：

| 层 | 平台账号（`type=-1`） | 依据 |
|---|---|---|
| 试运行的**订单范围** | 看整个租户 | `build_trial_scope`：`type ∈ {-1,1}` → `SCOPE_TYPE_ALL` |
| 发布的**审计与归属** | 可以发布，审计记他的公司身份 | `publish_audit_ledger` 的 initiator 字段 |
| **编辑面** | **只在平台工作区**，不进租户 workspace | Q19 (a) |

**为什么编辑面不能"看全部"**：Dify 的 `current` 只有一个，一个账号在多个 workspace 里都是 owner 时，
来回切会互相踩；而且"能改所有租户的智能体"是一个比"能看所有订单"大得多的权力。
平台运维要把某个租户的智能体改坏，需要**显式地以该租户的身份**去做 —— 那是一次可审计的动作，
不是默认能力。

---

## 5. 四条路径怎么改

### 5.1 开户（`provision_editor`）

现在：无 tenant 入参，邀请打到 `workspaces/current`（owner 的 current 决定）。
改为：

```
resolve_workspace(tenant_id, environment) -> workspace_id | DIFY_WORKSPACE_UNRESOLVED
  1. 查 dify_workspaces
  2. 没有 → 建 workspace（inner_api，owner_email = 平台服务身份）
     ★ 建成后**立刻**写 dify_workspaces 一行（见下「幂等」）
  3. 装插件 + 写 provider 凭据（admin key + X-WORKSPACE-ID）
  4. 导入模板 DSL（失败只记状态，不删 workspace）
  5. 把这个人 join 进 workspace（member 端点，不是邀请）
```

**幂等与并发（评审指出两件事）**：

原设计把写表放在第 5 步，于是"建成功但第 3/4 步失败"会让下一次登录又建一个 workspace，
前一个成为孤儿。改为**先占位、再建**：

```
1. INSERT INTO dify_workspaces(tenant_id, environment, workspace_id, setup_state)
   VALUES (?, ?, '', 'creating')          -- UNIQUE(tenant_id, environment)
   ↑ 并发时第二个 INSERT 撞唯一键 ⇒ 读回第一行，等它变成 ready 或直接报 creating
2. 用 inner_api 建 workspace → 拿到 tenant.id
3. UPDATE ... SET workspace_id = ?, setup_state = 'created'
4. 装插件 / 写凭据 / 导模板（可重试）→ 全部成功后 setup_state = 'ready'
```

**为什么先占位**：代理是 `ThreadingHTTPServer`，同一租户的两次首次登录可以同时越过
"表里没有行"这个检查。只有**唯一键 + 先插入**才能把它们串行化；反过来（先建后写）
两个请求各建一个 workspace，后写的赢，前一个成为**永久孤儿**（Dify 没有删 tenant 的 API）。

`workspace_id` 为空是 `setup_state = 'creating'` 的合法状态 —— 解析函数见到它不是"没有行"，
而是"有人在建"，返回一个**可重试**的 `DIFY_WORKSPACE_CREATING`，不是 `UNRESOLVED`。
这两个错误码必须分开：前者让调用方稍后再试，后者让人去建。

#### `creating` 不能是死胡同（评审指出）

原设计有一个真缺陷：Dify 建 workspace 失败后，那行 `creating` 会**永久挡住**下一次开户 ——
每次登录都读到它、都等不到 `ready`，这个租户再也自动建不出 workspace。

所以 `creating` 必须带**租约与三种结局**：

```
dify_workspaces(
  tenant_id, environment, workspace_id, setup_state,
  lease_owner,      -- 谁在创建（进程 + 请求 id）
  lease_expires_at, -- 租约到期时刻
  last_error,       -- 最近一次失败的分类
  UNIQUE(tenant_id, environment)
)
```

| 结局 | 判据 | 动作 |
|---|---|---|
| **明确失败** | 收到错误响应体 / 连接被拒 | `setup_state='failed'`，`workspace_id` 保持空 ⇒ 下一次开户**可以重建** |
| **结果未知** | 超时 / 连接中断 / 建成了但响应没回来 | `setup_state='unknown'` ⇒ **不重建**，先核实：用 `GET /console/api/all-workspaces`（admin key）按名字查有没有这个租户的 workspace；有就补写 UUID，没有才重建 |
| **建成但写回失败** | 建成功、UPDATE 失败 | 同"结果未知" |

**租约**：`lease_expires_at` 到期后别的请求可以**抢占**。没有租约的话，一个创建进程崩在
中间会让这行永远卡住。

**但租约本身不是足够的（第二轮评审指出）**：租约保护的是**我们那行映射**，不是 Dify 的创建
请求。丢失响应、UPDATE 失败、或请求仍在 Dify 里跑，都会在租约到期后让下一个请求**再建一个** ——
而孤儿不可删。

⇒ **抢占之前必须先对账**，把抢占做成"先核实、后接管"：

```
lease 到期，请求 B 想接管
  1. 用 admin key 查 GET /console/api/all-workspaces，按 workspace **名字**精确匹配
  2. 命中 0 个  → 安全，接管并重建
  3. 命中 1 个  → 不重建，把它的 UUID 补写进映射，state='created'，再做后续步骤
  4. 命中 >1 个 → 人工介入（不自动选，选错就是串租户）
```

**名字必须确定性且唯一**，否则第 2 步无从匹配：

```
workspace_name = f"aiops-{environment}-{tenant_id}"
```

`tenants.name` 在 Dify 里**没有唯一约束**（实测：`tenants` 上只有 `tenant_pkey`），
所以唯一性是**我们命名约定**给的，不是 Dify 保证的。因此：

- 每次创建都用这个派生名，绝不人工起名；
- 第 4 种情况（命中 >1）说明有人手工建过同名 workspace —— 那时**必须人工**，机器不该猜。

**已知限制（据实）**：Dify 允许改 workspace 名，而改名后我们按名字就对不上了。
这类情况会落到"命中 0 个 ⇒ 重建 ⇒ 造孤儿"。缓解是**不给运营改名的入口**，
并在 `unknown` 状态里记 `created_at` 时间窗口，人工核实时可对照。

**测试**：三类都要有 ——
「明确失败后重试能建成」、「建成但写回失败 → 按名字补 UUID」、
「租约到期但已有同名 workspace → **不**重建」。

**"解析不到就拒绝"与"缺行就建"不冲突**（评审指出）——两者是不同的调用方：

| 调用方 | 缺行时 |
|---|---|
| **开户**（`provision_editor`） | **建**（这是唯一该建的时机） |
| **发布桥 / 试运行 / 运维命令** | **拒绝**（`DIFY_WORKSPACE_UNRESOLVED`），绝不隐式建 |

所以解析函数本身**只查不建**；建是开户那一步的显式动作。这样"拒绝"与"创建"就不是同一个
函数的两种行为，而是两个调用方各自的策略。

**注意**：`RBAC_ENABLED=False` 时，邀请一个**已激活**的账号会失败（`AccountAlreadyInTenantError`）。
新 workspace 里的人是全新的 Dify 账号（我们按公司用户 id 确定性生成邮箱），所以不受影响。

### 5.1b 旧的共享 workspace 怎么退（已决：先复制、后撤权、不删）

**⚠️ 评审时发现这条路的原版走不通**：我写的是"移除成员 + 在新 workspace 重新邀请"，
但 `RBAC_ENABLED=False` 时邀请一个**已激活**的账号会静默失败（`invite_new_member` 只在
`status == PENDING or RBAC_ENABLED` 时建 join，否则抛 `AccountAlreadyInTenantError`）。
移出旧 workspace 不会把 Dify 账号变回 pending —— 它是全局账号，状态与 workspace 无关。
**结果是这个人既进不了旧的，也进不了新的。**

**正确的迁移入口是 inner_api 的 member 端点，不是邀请**：

```
POST /inner/api/enterprise/workspace/member
  {workspace_id, account_id, email, role}
  → TenantService.join_enterprise_workspace_member
  → TenantService.create_tenant_member   ← 直接建 join，不经过 invite/activate
```

`create_tenant_member`（`account_service.py:1122-1149`）**没有** `RBAC_ENABLED` 门槛：
已有 join 就改角色，没有就新建一行。它才是"把一个已存在账号加进另一个 workspace"的正路。
（前提：目标 workspace 已有 owner，否则 owner 冲突检查会拦；我们的新 workspace 有服务身份 owner。）

`remove_member_from_tenant` 会把该成员的 app/dataset 转给 owner 且**没有反向操作**
（ADR-0023 已记）。所以**不能**用"移除再邀请"来迁移。

#### 顺序（六步，前四步都是"只加不减"）

```
1. 建目标 workspace（§5.1 的 resolve + create 流程）
2. 把该租户的人 join 进目标 workspace（member 端点，不是邀请）
3. 在目标 workspace 里建**它自己要用的 dataset**，拿到新 dataset UUID
4. 导出旧 app 的 DSL → 把 DSL 里的 dataset UUID 换成第 3 步的新 UUID → import 到目标 workspace
5. 更新 dify_app_registry 的 app_id（新 UUID）+ 更新清单文件 ops/environments/*.toml
6. 全租户切换完成、且新 workspace 跑过一轮真实发布与试运行之后，才撤旧 workspace 的成员
```

#### 四处评审指出的坑，各自的原因与修法

**（a）`dify_app_registry` 的 `app_id` 必须跟着换，而且清单文件也要改。**
import 产生**新 app UUID**；`DifyDslClient` 是按 `binding.app_id` 向指定 workspace 导出的。
更麻烦的是 `app_id` 也写在 `ops/environments/env-41.toml` 的 `[[dify_apps]]` 里，
而 `aiops admin reconcile` 会用它**覆盖**库里的值 —— 只改库不改清单，下次 reconcile 就把旧
`app_id` 写回去了。⇒ **第 5 步必须是"清单 + 库"一起改**，且顺序是"先改清单、再 reconcile"。

**（b）dataset 也要在新 workspace 里重建，不能只复制 app。**
`DifyDslClient.dataset_knowledge_id` 会在**目标 workspace** 里查 DSL 引用的 dataset UUID；
跨 workspace 查不到 ⇒ 带知识库的 app 同步失败。
但这里有个**我们已经有的出口**：那两个生产 dataset 是 `provider=external`，
`external_knowledge_id` 指向我们网关的 `/v1/dify/retrieval`（`kb-canary` / `kb-canary-2`）。
⇒ 新 workspace 里只需**重建两个 external dataset 记录**（同样的 `external_knowledge_id` +
同样的 endpoint + 同样的 api_key），**不需要搬任何文档** —— 真正的内容在 RAGFlow。
这是本设计里最省事的一处，但**必须显式做**：external dataset 的 UUID 也是新建的，
DSL 里要换。

**（c）"先加成员"与"后撤权限"是两步，不冲突 —— 但原文写得像一步。**
第 2 步加的是**新** workspace 的成员；第 6 步撤的是**旧** workspace 的成员。
中间那几步（3-5）里这个人**同时在两个 workspace 里**，这是**故意**的：
在切换完成前，他还是旧 workspace 的成员，旧链路照常工作 —— 那是回滚路径。
**这一步的代价写清楚**：切换窗口内，这个人在旧 workspace 里仍能看到旧的 app（包括别人的）。
所以窗口要短，且按租户逐个做，不要一次铺开。

**（d）为什么第 4 步用成员自己的会话而不是 owner 服务身份**：
`/apps/imports` 要一个在该 workspace 里能建 app 的会话。用 `ADMIN_API_KEY + X-WORKSPACE-ID`
也能建，但那会把"谁建的"记成 owner 服务身份，审计上不好看；用成员自己的会话，
Dify 的 `created_by` 就是那个人。两者都可行，**选后者**。

### 5.2 发布桥

`AIOPS_CONSOLE_BRIDGE_WORKSPACE_ID` 从单一常量改为按发起人解析：

- 桥拿浏览器会话 → 调代理 `/__aiops/publish/check` → 拿到 `company_tenant_id`
- 用 `(tenant_id, environment)` 查 `dify_workspaces`
- **钉住的判据要改成"角色来自发起人所在的那个 workspace"** —— 这是最容易被漏掉的越权口

### 5.2b 两个运维入口也要改（评审补充）

`admin pull-dify` 与 `admin sync-from-dify` 各自构造 `DifyDslSource(workspace_id=...)`，
用的是同一个单一配置：

```
admin_cli.py:234-239   pull-dify      source = DifyDslSource(..., workspace_id=console.dify_console_workspace_id)
admin_cli.py:492-497   sync-from-dify 对每个 binding 各建一个 source，但 workspace_id 仍是同一个
```

`sync-from-dify` **会遍历多个租户的绑定**，所以它是"一个 workspace_id 被套用到 N 个租户"
的最明显例子 —— 多 workspace 之后它必须按 `binding.tenant_id` 解析。

⇒ 改造面从三条变**四条**（开户 / 发布 / 试运行 / 运维 CLI），
四处**共用同一个 `resolve_workspace`**。CLI 在解析失败时**拒绝并退非零**，
不能"跳过这个租户继续跑"，否则一次 sync 会静默漏掉半个租户。

### 5.3 试运行

`DifyDslSource(workspace_id=console.dify_console_workspace_id)` 同样改为按发起人解析。
租户没有 workspace ⇒ **明确拒绝**（Q8），不读平台工作区的草稿。

### 5.4 登录代理：按环境分开，不按请求挑 UPMS

代理不是"一套服务同时认两套 UPMS"。**每个环境一个代理实例**，它的
`AIOPS_LOGIN_PROXY_COMPANY_URL` 写死到那个环境的 UPMS：

| 环境 | UPMS | 代理实例 |
|---|---|---|
| 41 | `http://192.168.1.44:30899`（内网） | 36 上那个 `aiops-dify-login-proxy`（**今天只有这一个**） |
| 95 | `http://127.0.0.1:5999` | **尚未部署**（95 今天没有 Dify 控制台入口） |

为什么不按请求挑：公司 `/upms/token/login` 的响应里**没有任何环境标识**，两套 UPMS
的租户 id 空间又互相独立。让一个代理实例去猜"这个账号该打哪套"，等于在运行时做一次
不可验证的路由 —— 而它猜错的表现是"用户名或密码错误"（另一套 UPMS 里没这个人），
排查成本极高。**部署身份是唯一可靠的判据。**

一个后果：今天 41 的用户能进 Dify，95 的用户不能 —— 因为 95 还没有控制台入口。
这不是缺陷，是"环境先存在、Dify 入口后补"的必然状态。

---

## 6. 本次核对顺带发现的三件事（都不在本设计的范围，但要知道）

1. **Dify 的 `VECTOR_STORE` 未配置**：唯一一个 vendor dataset 的 1564 个 chunk 全停在 `indexing`，
   `embeddings` 表 0 行。worker 日志 `ValueError: Vector store must be specified.`
2. **`/v1/datasets/{id}/retrieve` 不支持 external dataset**：`HitTestingService` 收了
   `external_retrieval_model` 却不用。external 库只有 console 端点的 `external-hit-testing` 一条路。
3. **6 条孤儿 `external_knowledge_bindings`** 指向已删除的 dataset；且有 2 条 `external_knowledge_apis.settings`
   里含**明文 api_key**。

---

## 7. 关于 95（`prod-bugtrack-120`）

用户说"95 上跑的是网关"，实地核实（经 HK 跳板）：

```
systemd：aiops-gateway.service（active，/home/aiops/AI-Ops，0.1.0）
        aiops-41-tunnel.service（到 41 的回环隧道）
        aiops-tdengine-readonly-proxy.service
监听：  仅 127.0.0.1:8788
公开入口：api.qumall.qushiyun.com → /v1/ → 127.0.0.1:8788
容器：  公司业务栈（cloud-tsdata-biz、bugtrack-postgres、node-exporter…），**没有 Dify**
/root/dify/docker：只有 nginx/ssrf_proxy/volumes 三个空目录，无 compose 文件
网关 env 里 **没有任何 DIFY_* 键**
```

⇒ **95 今天是一个纯网关环境，没有 Dify、没有登录代理。这不是缺口 —— Dify 本来就不该在
95 上。** Dify 只有一套，在 36；95 缺的是那个环境自己的**登录代理**（以及控制台入口），
因为登录代理绑的是那一套 UPMS。

两个环境的 UPMS 是两套：

| 环境 | 网关 UPMS | 业务库 | 网关 |
|---|---|---|---|
| 41 | `http://192.168.1.44:30899` | `192.168.1.45` | `127.0.0.1:8788`（41） |
| 95 | `http://127.0.0.1:5999` | `192.168.0.39` | `127.0.0.1:8788`（95） |

41 的 Dify 在 **36**：`AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL -> http://36.156.159.175:10008`，
知识库经 `127.0.0.1:29380` 隧道到 36。**95 将来接的是同一个 Dify（同一个 36）**，
只是它自己的代理实例去读 95 的 UPMS 完成登录交换。

---

## 8. 原「未决项」全部关闭

grilling 结束时列了四条。前三条在 §2 的 19/20/21 与 §5.1b 已定；第四条**根本不是未决项**
（Dify 只有一套，95 不需要自己的）。剩下三条在这一节按第一性原理定掉。

| 原「未决项」 | 结论 |
|---|---|
| `AIOPS_GATEWAY_DIFY_CONSOLE_*` 的配置形状 | **定了**：`BASE_URL` 与 `API_KEY` 保持单份（Dify 是平台级的，36 上只有一套）；只有 `WORKSPACE_ID` 改为查 `dify_workspaces` |
| 旧共享 workspace 的迁移顺序 | **定了**：先按租户复制 app 副本到新 workspace → 逐个租户切换 → 最后撤旧访问权；**不删**旧 workspace。见 §5.1b |
| 一人一租户怎么检查 | **定了**：`sys_user.tenant_id` 非空且单值就是判据 —— `sys_user` 一行只属于一个 `sys_organ`（`SysUserMapper.getByUserName` 是 `su.tenant_id = sr.id` 的内连接）。同一个自然人跨租户时，公司侧本来就是**多个 `sys_user` 行（多个账号）**，不是一个账号挂多个租户 |
| 95 的 Dify 何时上 | **不是问题**。Dify 只有一套（36），95 不需要自己的 Dify。这也是本轮被纠正的最后一处误解 |

### 三条也已定掉（2026-10-20，第一性原理）

**1. workspace 的 owner 用同一个平台账号，不用每个租户一个 owner。**

`POST /inner/api/enterprise/workspace` 的契约是 `{name, owner_email}`，且**要求该账号已存在**
（`select(Account).where(email == owner_email)`，查不到 404）。所以 owner 必须是现成账号。

一个账号在 N 个 workspace 里都是 owner 是允许的（`create_owner_tenant` 不检查已有 membership），
而它唯一会出问题的地方是"以该账号身份在某个 workspace 里做事"——
而那条路我们**本来就不用账号会话**，用的是 `ADMIN_API_KEY + X-WORKSPACE-ID`：

```python
# ext_login.py:74-90
if is_admin_api_key_request(request):
    workspace_id = request.headers.get("X-WORKSPACE-ID")
    ... 找该 tenant 里 role == "owner" 的那个 join → 取它的 account_id
    account.set_current_tenant_with_session(...)   # 只在请求上下文
```

这条分支**按 workspace 找 owner 行**，与 `current` 无关、不写库。
⇒ `dify-admin@aiops.local` 作为每个 workspace 的 owner 是对的：它是**服务身份**，不是人；
它不需要在浏览器里切 workspace，所以"current 只有一个"这条限制对它不适用。

**−1. `account_email_for` 必须带环境，否则两套 UPMS 撞 `user_id` 时会串账号。**

评审给的这条是**真的安全缺陷**，而且它在**今天的代码里就已经存在**：

```python
# dify_login_provision.py:65-74
def account_email_for(company_user_id: str) -> str:
    safe = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in company_user_id).strip("-")
    return f"{safe}@{ACCOUNT_EMAIL_DOMAIN}"  # ← 只有公司用户 id，没有环境
```

两套 UPMS 是两套独立数据库，`sys_user.id` 各自生成。一旦 41 与 95 出现同一个 `user_id`，
两个人就会映射到**同一个 Dify 账号**（同一个邮箱），而 Dify 账号是全局的（不属于任何 workspace）：
第二个环境的代理能为它签会话，访问第一个环境那个人的 workspace。

**修法**：邮箱里带环境标识 —— `f"{environment}-{safe}@{DOMAIN}"`。

**Dify 1.17.1 没有改账号邮箱的端点**（2026-10-20 由用户确认）。所以这不是"改一个字段"，
而是一次**账号迁移**：

```
1. 用新邮箱走开户流程（invite + activate）建一个新 Dify 账号
2. 把 dify_login_mappings 那一行的 dify_account_id / dify_email 指到新账号
3. 旧账号从 workspace 移除（注意：移除会把它的 app 转给 owner，不可逆 —— 见 ADR-0023）
4. 旧账号不再被任何映射引用，停用即可（Dify 没有删账号的端点）
```

**不变量要保持**：同一公司用户二次登录落到同一个 Dify 账号 —— 迁移前后都必须成立，
否则历史编辑与发布记录会断。所以第 2 步是一次**原子替换**（一个事务里改两列），
不能在"新账号已建、映射还没切"的窗口里让登录成功。

**分票了**：这条缺陷与多 workspace 无关（它今天就成立），单独立了 **#864**，
并且它**阻塞多工作区实施**（账号是全局的，多 workspace 不会让撞号问题消失）。
**顺序**：它**阻塞多 workspace 实施**（已开票 **#864**）。理由不是"顺手做"，
而是：多 workspace 保证的是"每个租户一个 workspace"，而**账号仍是全局的** ——
账号层的撞号不会因为多 workspace 而消失，反而因为多了第二个环境而更容易发生。

**−0. `INNER_API` 已在 36 上打开（2026-10-20 实测）**

建 workspace 的唯一 HTTP 入口在 `inner_api`，而它默认关闭。已打开并验证：

```
改动：/opt/dify/deploy/.env 追加 INNER_API=true + INNER_API_KEY=<32 字节 hex>
      备份 /opt/dify/deploy/.env.bak-innerapi-20261020160425
重启：docker compose up -d api worker（api 重启后 INNER_API=True, INNER_API_KEY set=True）

三条判据（对 api 容器的内网 IP:5001 直连）：
  无 key            -> 401 {"code":"inner_api_unauthorized"}
  错 key            -> 401 同上
  正确 key + 不存在的 owner -> 404 {"message":"owner account not found."}
                              ↑ 门开了：它走到了"查 owner"那一步
```

**暴露面没有扩大**，两条都实测过：

```
经 Dify 自己的 nginx（127.0.0.1:10018/inner/api/...）      -> 404   （default.conf 只转 /console/api、/api）
经公网 TLS 入口（https://dify.ranlei.work/inner/api/...）  -> 404
```

api 容器只 publish 了 `5001/tcp`（未映射到宿主），宿主只能经 docker 内网 IP
（`172.21.0.x`）访问 —— 这是本设计的调用路径：**网关 / 登录代理在 36 本机，
经容器内网 IP 调 inner_api**，不经过任何公网或宿主 nginx。

控制台回归：`10008/signin` 200、`https://dify.ranlei.work/signin` 200。

**0. 映射表放哪：`dify_workspaces` 放**网关的 `gateway.db`**，但三个进程都要能读它。**

评审指出一个真实拓扑问题：代理的映射表在 36 的
`/var/lib/aiops-login-proxy/mappings.db`，网关的在 41 的 `gateway.db`，桥也在 36。
一份表放不进三个进程各自的私有库。

三条路（都不需要新服务）：

| 方案 | 判断 |
|---|---|
| **落在网关 `gateway.db`**，代理/桥经网关的一个只读端点查 | **选它**。网关已经是这三者共同的依赖（桥调 `/v1/dify/publish`、代理调 `/upms`），且 `dify_app_registry` 本来就在那 |
| 放代理的 `mappings.db` | 桥能读（同机），但**网关读不到** —— 而试运行要读 |
| 新建一个共享 SQLite | 多一个要备份、要迁移、要防护的存储 |

具体形态：网关加 `GET /v1/dify/workspace`，**但参数不是任意的 `tenant_id`**。
评审指出：只校验内部令牌、让它按查询参数返回任意租户的 workspace，等于把"哪个租户用哪个
workspace"变成调用方可选 —— 而这正是隔离本身。

**两个入参分开**：

| 入参 | 来源 | 谁能给 |
|---|---|---|
| `environment` | **服务端配置**（网关自己的 `AIOPS_GATEWAY_ENVIRONMENT`） | 不接受请求参数。请求里带了就 400 |
| `tenant_id` | 调用方要查的那个租户 | 只要令牌有效就允许 —— 但**返回的是 `workspace_id` 一个 UUID**，不是任何内容或凭据 |

为什么可以只校验令牌：这个端点的输出是"某个租户的 workspace 标识"，而**拿着它什么也做不了** ——
要用它还得有 `ADMIN_API_KEY`（只有网关与代理有）。真正的隔离在 Dify 那边：
workspace 之外的会话读不到里面的 app。**但这不等于可以放开**：环境必须服务端定，
因为"跨环境查同一个 tenant_id"才是真的越权路径。

⇒ 形状：`GET /v1/dify/workspace?tenant_id=…`（**没有 environment 参数**），
网关用它自己的部署身份补上 environment。

**代理不再自己存一份** —— 它本来就只该读，写（建 workspace）由网关侧或开户流程做。

代价：代理多一次回环调用。收益：一处存储、一份真值，且 `environment` 的校验只写一遍。

**2. `dify_workspaces.environment` 保留，且解析函数必须带上它。**

今天它只有一个值（实测：41 的 `dify_app_registry` 3 行只涉及 2 个 tenant_id；
95 的 gateway.db 里**连 `dify_app_registry` 表都没有**，也没有任何 tenant 列）。
所以现在加它是"没有区分度的"——**但这个判断的前提是"两套 UPMS 的 id 不会撞"，
而那个前提无法保证**：41 与 95 是两套独立数据库，`sys_user` / `sys_organ` 的 id 各自生成。

本仓已经因为"同名不同物"栽过（ADR-0008、`mem-20261019-ranlei-002`：平台的 `sai_agent.id`
与网关的 `agt_…#vN` 凭同名绑定，制造出比 null 更糟的语义错误）。一个**在撞号时会静默
解析到另一个租户的 workspace** 的解析函数，比多一列贵得多。

⇒ 保留，并让 `resolve_workspace(tenant_id, environment)` **同时要求两者**。
代价是一列 + 一个谓词；收益是撞号时 fail-closed 而不是静默串租户。

**3. 凭据轮换：范围是"1 把代理 key + N 个 workspace 的 provider 凭据"，靠表枚举。**

- `SECRET_KEY`（代理自签会话用）：轮换必须 **Dify 与代理一起重启**（ADR-0023），这条不变。
- provider 凭据（每个 workspace 一份）：轮换时按 `select workspace_id from dify_workspaces`
  枚举，逐个用 `ADMIN_API_KEY + X-WORKSPACE-ID` 重写。**不做自动化脚本** ——
  这是个季度级的动作，一个能枚举的 SQL 加一段手顺就够；写脚本反而多一处要维护的代码。

⇒ 结论：轮换是**运行手册**，不是代码。设计只需保证 `dify_workspaces` 能被枚举。
