# Dify 与公司租户体系的对接现状

> **读者**：要判断"能不能让第二个租户自行编辑智能体"的人；以及第一次读这套鉴权链的人。
> **状态**：2026-10-20 据代码与 36 线上数据核实。**记录的是现状，不是目标态。**
> 决定的论证在 ADR-0023 / 0024 / 0025 / 0028 / 0056；本页讲**现在实际是怎么跑的**。

---

## 0. 三句话

1. **公司租户不进入 Dify。** Dify 侧只有一个 workspace，所有映射账号都是它的 `editor`。
2. **租户只在执行时生效**：登录后点「试运行」，网关按发起人的 `tenant_id` + `user_type`
   算订单可见范围。编辑面完全没有租户概念。
3. **这是 V1 的暂定**（PRD #772 §C），触发条件是"出现第二个需要自行编辑智能体的租户"。
   **该条件于 2026-10-20 由用户确认已满足** —— 见 ADR-0056 与本文 §5。

---

## 1. 三层各有各的范围

| 层 | 有没有租户隔离 | 依据 |
|---|---|---|
| 公司 UPMS | **有**。`sys_organ`，租户是实体 | 公司体系本身 |
| AI-Ops 登录代理 | **记录，但不使用**。映射表每行存 `tenant_id` | `dify_login_service.py` |
| Dify | **没有**。全网关一个 workspace | 36 上 `tenants` 表 1 行 |
| AI-Ops 网关 | **有**。按 `tenant_id` + `user_type` 算订单范围 | `dify_trial.build_trial_scope` |

### 线上数据（2026-10-20 实测，36）

```
Dify tenants 表                          1 行
  AI-Ops Admin's Workspace               2db44904-f981-42ca-b7e2-6f35cb2afffd

tenant_account_joins
  dify-admin@aiops.local                  → owner   current=t
  1454348737375174658@aiops-login.invalid → editor  current=t

代理映射表 /var/lib/aiops-login-proxy/mappings.db
  company_user_id=1454348737375174658 | tenant_id=-1 | user_type=-1 | status=active
```

`tenant_id = -1` 是公司体系的**平台哨兵值**（不是实体 id）。`user_type = -1` 同理。

---

## 2. 开户为什么不接 `tenant_id`

```python
provision_editor(
    settings,
    owner_session=self.owner_session(),   # ← 持 SECRET_KEY 自签的 owner 会话
    company_user_id=identity.user_id,
    username=identity.username,
)                                          # ← 没有 tenant 参数
```

`INVITE_PATH = "/console/api/workspaces/current/members/invite-email"` ——
**`workspaces/current` 由 owner 会话的 `current` 标志决定**。36 上那个 owner 的
`current=true` 只指向唯一那个 workspace，所以每一次开户都落进同一个地方。

这不是"忘了传参数"，而是**在单工作区的前提下无处可传**。要让开户按租户分流，需要：

1. 每个公司租户对应一个 Dify workspace（或一个 workspace 下的多个 app 归属机制）
2. owner 会话能指向目标 workspace（Dify 用 `TenantAccountJoin.current` 表达，
   或 admin API 的 `X-WORKSPACE-ID` 头）
3. `provision_editor` 接 `tenant_id`，并在邀请前把 owner 的 current 切过去

`login-proxy.env` 里也没有任何 workspace 配置项 —— 它只配了 Dify 的 base_url 与
`SECRET_KEY`。

---

## 3. 租户唯一真正生效的地方：试运行

```
运营在 Dify 里点「试运行」
        │
        ▼
发布桥 → POST /__aiops/publish/check（带浏览器那枚 Dify 会话）
        │
        ▼
代理：验会话 → 查映射 → 回
      { company_user_id, company_username, company_tenant_id,
        company_user_type, dify_account_id, allowed }
        │
        ▼
桥转成请求头（服务端到服务端，浏览器提交的同名字段一律忽略）
      X-AIOps-Initiator        = 公司用户 id
      X-AIOps-Initiator-Tenant = 公司租户 id
      X-AIOps-Initiator-Type   = user_type
        │
        ▼
网关 build_trial_scope()：
      user_type ∈ {-1, 1}（平台 / 租户主账号） → 看整个租户
      其余（代理商，如 5）                      → 店铺 → 站点解析
      解析失败                                   → 空站点集合（拒绝全部订单）
```

**两条硬规则**：

- `company_tenant_id` 为空 ⇒ 桥**必须**当作"身份不全"拒绝，不能照发。
  网关在租户为空时会静默退回固定调试租户 —— 那是一次范围放宽。
- 解析失败**不**回落成 `self`（"只能看自己的单"），而是空集合。把"查不到范围"
  伪装成"范围很小"比直接拒绝更危险。

---

## 4. 现在这样会漏什么

| 缺口 | 具体表现 |
|---|---|
| **跨租户互相可见且可编辑** | 同一 workspace 内 `editor` 能看到并修改**全部** app。两个租户的运营会互相改到对方的智能体。 |
| **发布无租户维度** | 发布桥的 `AIOPS_CONSOLE_BRIDGE_WORKSPACE_ID` 是**单一常量**，钉死那一个 workspace。 |
| **映射表的 `tenant_id` 只存不用** | 除了算订单范围，它不参与任何准入判断。 |
| **无法按租户授予编辑范围** | 权限码 `aiops:agent:edit` 是全局的，不区分"能改哪些 app"。 |

PRD #772 §C 的应对是**限制发放范围**（只给公司内部运营/平台人员），
而不是做隔离。那是一个"先不做"的取舍 —— 而**这个取舍的记录（ADR）此前是缺的**，
本次补上（ADR-0056）。

### 上表第一行已实测（2026-10-20）

用代理的 `provision_editor` 真的开了一个"租户 B"账号（`zz-tenantb-probe-*`），
再看它能做什么：

```
开户后立刻可见 app 数：3        ← 与既有账号完全相同
  aiops-41-ops-methodology-agent | agent-chat
  charge_charging_A_kbqa         | advanced-chat
  aiops-41-kefu-agent            | agent-chat

读 app 详情（两个账号都成功）
  tenant-A(既有) -> 200  aiops-41-ops-methodology-agent
  tenant-B(新建) -> 200  aiops-41-ops-methodology-agent

拿完整可编辑 DSL（= 能改它）
  tenant-B GET /console/api/apps/<id>/export -> 200，4975 字节
```

**结论**：不是"看得到"，是**新开的账号一上来就能拿到别人智能体的完整可编辑配置**。
`editor` 在单一 workspace 内是平权的。

探针已清理：先从 workspace 移除成员，再删 `tenant_account_joins` 与 `accounts` 行；
最终回到原来的两个账号、两条 join。三个生产 app 的 `updated_at` 仍是 10-14/10-15，
**未被本次探针改动**（全程只读 + 一次 export，没有任何写 app 的调用）。

---

## 5. 触发条件已满足

PRD #772 §C 原文：

> 按租户划分工作区推迟到**出现第二个需要自行编辑智能体的租户**。

2026-10-20 用户确认：**该条件已满足**，并指定用 41 与另一个客户环境（用户称 "95"）
作为多租户的开发与测试环境。

### 关于"95"

**「95」= `prod-bugtrack-120`**（2026-10-20 由用户在 host declaration 中确认）：

| 字段 | 值 |
|---|---|
| ssh_alias | `prod-bugtrack-120` |
| role | service-host（公司环境，未纳入本编队编队管理） |
| owner | qushiyun |
| 判别 | 41 = `api.mall.qushiyun.com`；**95 = `api.qumall.qushiyun.com`**；36 = 移动云 KB 栈 |
| 其它 | qumall dev 网关兼 healthcare 主实例；SSH 端口 2134 |

`api.qumall.qushiyun.com` 解析到 `120.55.45.59`，本机 nginx 把该域名反代到
`127.0.0.1:8788` 的 aiops-gateway。

> 本次从开发机 `ssh prod-bugtrack-120` **连接超时**（`120.55.45.59:2134`），
> 因此本页关于 95 的信息**全部来自用户提供的声明，未在主机上核实**。

这意味着 PRD §C 的暂定前提消失，**多工作区从"推迟"变成"待做"**。设计未在本页展开 ——
那是一次真实的范围与拓扑变更，需要单独的决定记录。

---

## 6. 要改成按租户隔离，需要动哪几处

按依赖顺序（**这是清单，不是计划**；具体设计留在单独的票里）：

1. **Dify 侧**：为每个需要自行编辑的公司租户建一个 workspace。
   备选是"一个 workspace + 按租户分 app"，但那要求 app 归属可表达 —— Dify 1.17.1 的
   `App.tenant_id` 是 workspace 级，没有租户字段，所以多半要走多 workspace。
2. **映射表**：`tenant_id` 从"只存"变成"决定 workspace"，并加一列
   `dify_tenant_id`（两套 id 不能混用 —— 这与 ADR-0008 记过的"同名不同物"是同一类风险）。
3. **开户**：`provision_editor` 接 `tenant_id`；邀请前把 owner 会话的 current
   workspace 切到目标。**这一步要确认 Dify 的会话/租约语义**，不能假设。
4. **发布桥**：`AIOPS_CONSOLE_BRIDGE_WORKSPACE_ID` 从单一常量改为按发起人租户解析。
   钉住 workspace 的那条安全判据要保留，只是钉的对象变成"发起人的 workspace"。
5. **试运行**：不变 —— 它已经按公司租户算范围，与 Dify workspace 无关。
6. **发布权**：需要决定 `aiops:agent:publish` 是否还要加 workspace 维度。

**一处必须同时改的**：桥现在用 `AIOPS_CONSOLE_BRIDGE_WORKSPACE_ID` 做"角色来自哪个
workspace"的验证。多工作区之后，这条判据必须变成"角色来自**发起人所在**的 workspace"，
否则运营 A 可以在 workspace A 是 owner、在 workspace B 里只是 editor，而桥读错了那一个。

---

## 7. 相关记录

- **ADR-0023** 自签会话的论证与代价（代理持 `SECRET_KEY`）
- **ADR-0024** 发布权改由公司权限码裁定
- **ADR-0025** 试运行范围跟随发起人
- **ADR-0028** 公司身份是控制台会话与试运行范围的唯一来源
- **ADR-0056** 「单一固定工作区」这个 V1 暂定本身（本次补记）
- `dify-login-proxy-36-runbook.md` 部署与验收手顺
- PRD #772 §C 工作区、§Implementation C 按租户划分的条件
