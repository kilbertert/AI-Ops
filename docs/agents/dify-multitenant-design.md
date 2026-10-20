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
| **环境（environment）** | 一套独立部署的 Dify + 网关 + 数据库。**是部署身份**。41 与 95 是两台机器、两套库、两个域名。 |
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
| 4 | 已有映射账号：迁移进各自租户的 workspace；**不保留**旧的共享 workspace 访问权 |
| 5 | 建 workspace 走 **`POST /inner/api/enterprise/workspace`**（`is_from_dashboard=True` 绕过 `ALLOW_CREATE_WORKSPACE`） |
| 6 | 模板 app：**模板 DSL 进仓库**，开户时导入。模板**只含结构与工具白名单**，`model_config` 留空（避免绑死模型） |
| 7 | 发布权：`aiops:agent:publish` 仍然按公司权限码裁定；**平台租户（`type=-1`）视为可见全部** —— 但见 §4 的边界 |
| 8 | 试运行草稿来源：租户没有 editor 环境就**明确拒绝**（"你的租户没有编辑环境"），不回落 |
| 9 | 映射存在 **新表 `dify_workspaces(tenant_id, environment, workspace_id, created_at)`**；`dify_app_registry` 不动 |
| 9b | `dify_workspaces.environment` 的作用是**防止两套环境的 `tenant_id` 撞号**，不是「给每个环境各建一套 workspace」 |
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

## 5. 三条路径怎么改

### 5.1 开户（`provision_editor`）

现在：无 tenant 入参，邀请打到 `workspaces/current`（owner 的 current 决定）。
改为：

```
resolve_workspace(tenant_id, environment) -> workspace_id | DIFY_WORKSPACE_UNRESOLVED
  1. 查 dify_workspaces
  2. 没有 → 用 inner_api 建 workspace（owner_email = 平台 owner）
  3. 装插件 + 写 provider 凭据（admin key + X-WORKSPACE-ID）
  4. 导入模板 DSL（失败只记状态）
  5. 写 dify_workspaces 一行
邀请：admin key + X-WORKSPACE-ID 定向邀请
```

**注意**：`RBAC_ENABLED=False` 时，邀请一个**已激活**的账号会失败（`AccountAlreadyInTenantError`）。
新 workspace 里的人是全新的 Dify 账号（我们按公司用户 id 确定性生成邮箱），所以不受影响。

### 5.1b 旧的共享 workspace 怎么退（已决：先复制、后撤权、不删）

`remove_member_from_tenant` 会把该成员的 app/dataset 转给 owner 且**没有反向操作**
（ADR-0023 已记）。所以**不能**用"移除再邀请"来迁移。顺序固定为：

1. **按租户分派副本**：对旧 workspace 里每个 app，判定它属于哪个租户
   （依据 `dify_app_registry` 的 `(tenant_id, business_entry) → agent_name` + 该 app 的 DSL 内容），
   在目标 workspace 里用 `POST /console/api/apps/imports` 导入一份副本。
   源 app **原样保留**，一行都不动。
2. **逐个租户切换绑定**：`dify_app_registry` 不动（它只认 `(tenant, entry) → agent_name`），
   但 `dify_workspaces` 开始对那个租户生效 —— 之后它的发布与试运行都读新 workspace。
3. **撤旧访问权**：等所有租户都切换完成、且新 workspace 上跑过一轮真实发布与试运行之后，
   才把映射账号从旧 workspace 移除。**这一步最后做，且单独一次变更。**
4. **旧 workspace 不删**：Dify 没有删 tenant 的 API；它留作审计与回滚参照，
   但不再有活跃成员。

**为什么副本而不是搬运**：Dify 的 app 不能跨 workspace 移动。`/apps/<id>/export` +
`/apps/imports` 是唯一的路，而它天然产生**新 app_id** —— 所以 `dify_app_registry` 里的
`app_id` 必须跟着改（运行时只读 `agent_name`，所以运行不受影响；受影响的是拉取与发布路径）。

### 5.2 发布桥

`AIOPS_CONSOLE_BRIDGE_WORKSPACE_ID` 从单一常量改为按发起人解析：

- 桥拿浏览器会话 → 调代理 `/__aiops/publish/check` → 拿到 `company_tenant_id`
- 用 `(tenant_id, environment)` 查 `dify_workspaces`
- **钉住的判据要改成"角色来自发起人所在的那个 workspace"** —— 这是最容易被漏掉的越权口

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

## 8. 仍未决的项

前三个原列的未决项已经定掉（见 §2 的 19/20/21 与 §5.1b），第四个**根本不是未决项**：

| 原「未决项」 | 结论 |
|---|---|
| `AIOPS_GATEWAY_DIFY_CONSOLE_*` 的配置形状 | **定了**：`BASE_URL` 与 `API_KEY` 保持单份（Dify 是平台级的，36 上只有一套）；只有 `WORKSPACE_ID` 改为查 `dify_workspaces` |
| 旧共享 workspace 的迁移顺序 | **定了**：先按租户复制 app 副本到新 workspace → 逐个租户切换 → 最后撤旧访问权；**不删**旧 workspace。见 §5.1b |
| 一人一租户怎么检查 | **定了**：`sys_user.tenant_id` 非空且单值就是判据 —— `sys_user` 一行只属于一个 `sys_organ`（`SysUserMapper.getByUserName` 是 `su.tenant_id = sr.id` 的内连接）。同一个自然人跨租户时，公司侧本来就是**多个 `sys_user` 行（多个账号）**，不是一个账号挂多个租户 |
| 95 的 Dify 何时上 | **不是问题**。Dify 只有一套（36），95 不需要自己的 Dify。这也是本轮被纠正的最后一处误解 |

**确实仍未决的**：

- **Dify 侧的 workspace 命名与 owner 策略**：`AI-Ops Admin` 作为每个新 workspace 的 owner 吗？
  还是每个租户建一个自己的 owner 账号？（前者简单，但它跨 N 个 workspace，`current` 会飘；
  后者多一批账号要管。）
- **`dify_workspaces` 与环境的关系**：一个租户只在**一个**环境里存在（41 的租户换不到 95 去登录），
  那 `environment` 列在真实数据里会不会永远是同一个值？如果永远不会有两个环境出现同名 `tenant_id`，
  这一列就是**没有区分度的**。需要在实施前用真实数据确认一次 —— 如果确实不需要，
  应该去掉这一列而不是留一个"防撞号"的假设。
- **Dify 的凭据轮换**：`SECRET_KEY` 与 provider 凭据现在是"一份/一 workspace"，
  轮换流程要同时覆盖 N 个 workspace 与代理持有的那把 key（ADR-0023 已记代价）。
