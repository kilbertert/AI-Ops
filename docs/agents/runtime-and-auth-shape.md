# AI-Ops 运行形态与鉴权形态（一页看全）

> **读者**：接手这条链的人。**状态**：2026-10-16 按线上实测 + 代码追出。
> 这份文档回答两个问题，而这两个问题的答案散在十几个 ADR 与 docstring 里，此前没有一处能一眼看全：
> **(1) 一次管家端诊断运行时到底经不经过 Dify？** **(2) 公司的鉴权是以什么形态进来的？**
>
> 与 ADR 的关系：本文是**现状描述**，不是决定记录。每个"为什么"指向对应的 ADR。

---

## 一、运行形态：Dify 是编辑面，AI-Ops 引擎是执行面

### 一次管家端诊断的调用链

```
管家端 APK
  └─ POST /v1/standard/diagnoses        (X-Business-Entry: operator)
       └─ gateway_api.create_standard_diagnosis
            ├─ _order_authorization              ← 订单可见性（与试运行同一判据）
            ├─ AgentWorkspace.create(...)
            ├─ store.create_standard_diagnosis   ← 先落一行 running
            └─ 丢给 worker 线程
                 └─ gateway_runtime._execute_standard_diagnosis
                      ├─ _diagnosis_methodology   ← 取方法论
                      │    └─ select_operations_agent(store, tenant, name)
                      │         └─ 读 agent_versions 的**快照**（本地 sqlite）
                      ├─ run_agent_diagnosis(workspace, request, settings, fixture=None,
                      │                      methodology=..., allowed_tools=...)
                      │    ├─ DiagnosticToolExecutor        ← 七个工具
                      │    │    └─ 打**本地只读源**（MySQL / TDengine / Redis）
                      │    ├─ AgentCoordinator 循环         ← 回合协议、证据 journal
                      │    └─ SDKCodexSession → **LLM provider base_url**  ← 推理在这
                      └─ 写 result.json
管家端 APK
  └─ GET /v1/standard/diagnoses/{id}    → 读 result.json 渲染报告
```

### 一句话

**运行时一次 Dify 网络调用都没有。** 推理发生在 `codex_runtime.py` → LLM provider；
工具在本地只读源上执行；方法论从 `agent_versions` 的**本地快照**读。
Dify 在这条路上只以一个本地 sqlite 绑定行出现（`(租户, operator) → agent_name`）。

### Dify 参与的全部位置（都在"人点了一下"的时刻）

| 位置 | 方向 | 时机 | 做什么 |
|---|---|---|---|
| `POST /v1/operator/publish` | 出站调 Dify | **人点发布** | 拉 DSL → 冻结成 `agent_versions` 一版 |
| `POST /v1/dify/publish` | 出站（共享密钥版） | 人点发布 | 同上 |
| `POST /v1/dify/trial` | 出站拉草稿 | **人点试运行** | 读"最近一次保存"的配置当草稿 |
| `POST /v1/dify/retrieval` | **入站**（Dify 调我们） | 编辑/调试 | 知识检索 |
| `POST /v1/dify/tools/{tool}` | **入站** | Dify 里预览工具 | **只回一句固定说明，不读任何数据** |

**分界**：Dify 只在「人改完东西、点了一下」时参与；冻结成版本之后，**运行时再也不问 Dify**。
这是 PRD #693 明确要的形状 ——「护栏恒在我们侧」「**不把 Dify workflow 变成第二个编排引擎**」。

### 有意**不生效**的 Dify 部分（不是缺陷）

- **编排图**：冻结时只取 DSL 里的 `prompt` 与工具绑定（`map_dsl_to_config`），
  workflow/agent 节点**被丢掉**。
- **模型配置**：Dify 里换的模型不影响运行时；跑的是 `env-41.toml` 里配的。
- **会话变量**：不参与。

### 试运行 == 正式跑（同一个函数）

`dify_trial.run_trial` 直接调 `run_agent_diagnosis` —— 与正式 worker、CLI `aiops diagnose`
**同一个函数**。差别只有两个入参：`methodology`（草稿 prompt）、`allowed_tools`（草稿白名单）。
所以"试运行通过 ⇒ 上线行为一致"是**构造出来的**，不是承诺出来的。

### ⚠️ 注册表的权威在**数据库**，不在 `env-41.toml`

`ops/environments/env-41.toml` 里的 `[[dify_apps]]` 是**声明/种子**；
运行时读的是 `gateway.db` 的 **`dify_app_registry` 表**（`_registered_agent` → `lookup`）。

**实测**：41 的 `production.env` 里 `ops-methodology-700` 出现 **0 次**、`[[dify_apps]]` **0 次**，
而 `dify_app_registry` 表里有那一行（`app_id=e35f8fda-…`）。
**⇒ 推断"operator 绑了谁"必须以库为准，不要把 toml 当事实。**

### fail-closed 方向

operator 入口 + 注册表已有行 ⇒ **必须**选出已发布的 operations agent，否则作业终止
（`DIAGNOSIS_AGENT_UNREGISTERED`），**不回落**到烤死提示词。
⇒ **管家端诊断的提示词今天只可能来自 Dify 那条冻结链，不再是代码里的。**

---

## 二、鉴权形态：四种凭据，一张分派表

### 两种入口，两条完全不同的信任模式（#448 / ADR-0009）

nginx 在 `api.mall.qushiyun.com` 上按 `X-Business-Entry` **分流到两个上游**：

| 入口 | 上游 | 凭据 | 身份由谁断言 |
|---|---|---|---|
| `consumer`（客户端 H5） | 直连 AI-Ops `172.18.0.1:8788` | **AI-Ops 自己的服务令牌**（nginx 注入，客户端不持有） | AI-Ops（会话 Redis） |
| `operator`（管家端 APK） | 先经**公司网关** `127.0.0.1:30899` → cloud-gateway | **公司用户 JWT**（明文透传） | **公司网关**（它注入来源密钥） |

分流实现在 `0.aiops-entry-map.conf` 的四个 `map`：`$aiops_upstream` / `$aiops_auth` /
`$aiops_srckey` / `$aiops_entry`。**非法非空入口原样透传，由上游 403，不静默落进 consumer。**

### 网关侧的分派顺序（顺序本身就是设计）

```
_caller_resolver:
  1. 配了公司来源密钥？
       是 → SourceKeyCallerResolver
              ├─ 请求带对 X-AIOps-Source-Key → CompanyTokenCallerResolver（新链）
              └─ 否则                        → fallback（既有链）
  2. fallback = 会话 → UPMS 兜底 → introspection   （顺序与新增本票之前逐字一致）
```

**为什么来源密钥那道门必须在会话之前**（代码注释里写得很清楚）：
会话那一级是 `if third_session_service_token: … return`，而 41 现网**那个键有值**
⇒ 门若排它之后，带公司令牌的请求会在第一级就被 401，新路径**永远不可达**。

**为什么按来源密钥分派而不是按"令牌长什么样"**：按形态分派要靠试错，顺序错了就是
**静默降级**（先问会话、被拒，再问令牌路径），而两条路径的身份来源不同，降级的落点无法预期。
来源密钥是**调用方自报不了**的判据 —— 前端不持有，由可信的那一跳覆盖式注入。

### ⚠️ 公司令牌今天走的是**"公司一致模式"：不验签**

`AIOPS_GATEWAY_COMPANY_JWT_KEY` 在 41 上是 `qushi@2026`（10 字符），但**它今天不被用来验签**：

```
trust_company_payload = True（默认）⇒ 不验签、不判 exp
  保留的判据只有：三段式结构 + alg 必须显式 HS256（不接受 none、不按令牌自报选算法）
trust_company_payload = False      ⇒ 严格模式：额外验签 + 判 exp
```

**为什么这样**：公司侧实测**既不验签也不判 `exp`**，按载荷里的 `id` 查用户；
若我们验签而公司不验，会出现「公司能用、AI-Ops 401」的割裂。

**代价（公司自己的安全弱项，不是我们引入的）**：公司 JWT 的**载荷可伪造**
（实测：签名整段换掉公司仍 200，只改 `id` 才 500）。

**我们的防线因此后移**到两处：
1. **令牌必须来自 nginx 那一跳**（`X-AIOps-Source-Key`，调用方自报不了）；
2. **身份字段必须齐备且租户一致**。

**要收紧就一行**：把 `trust_company_payload` 设回 `False`（ADR-0009 记着这条路径）。

### 数据范围的来源：QueryScope 下推

```
身份（公司用户 JWT）→ 租户 + (代理商) 店铺
   → UPMS /shopuser/getShops（服务侧内部凭据 AIOPS_UPMS_INSIDE_TOKEN）
   → 充电库 ch_site.shop_id → ch_site.id 映射（既有跳板隧道）
   → ScopeContext{ tenant_id, site_ids }  ← 一次构造，授权与执行共用
   → 只读 SQL 的 WHERE tenant_id=… AND site_id IN (…)
```

- **顶层账号**（`sys_user.type ∈ {-1,1}`）⇒ 整租户；**其余（代理商）** ⇒ 店铺→站点集合。
- **fail-closed**：解析失败（UPMS 不可达、凭据未配、店铺无站点）一律**空站点集合**，
  即看不到任何订单 —— **不回落租户级、不套用 `self`**。
- `scope_fingerprint` 是范围哈希，作业行按它隔离（这也正是"提交与读取必须同一范围"的原因）。

### 四层凭据清单

| 凭据 | 谁持有 | 保护什么 | 形态 |
|---|---|---|---|
| **公司用户 JWT** | 管家端 APK（浏览器/App 里） | `operator` 入口的身份 | 公司签的 HS256 JWT（**不验签，见上**） |
| **`X-AIOps-Source-Key`** | 只有**公司网关**（覆盖式注入） | 声明"这一跳被信任过" | nginx/map 注入，前端不持有 |
| **AI-Ops 服务令牌** | nginx（`consumer` 分支） | 客户端入口的会话鉴权 | nginx 注入的 Bearer |
| **`third_session`** | 客户端 H5 / 管家端 | 会话 → 身份（Redis 查） | Redis 会话值 |

（另有设备令牌/注册码用于 `aiops-gateway` 的 enrollment，是另一条面，不在这里。）

### Dify 的鉴权：**独立的一层，不与公司身份混用**

Dify 相关端点**不走上面那条链**，各用**独立共享密钥**，且互不相同（启动期强制）：

| 端点 | 钥匙 | 为什么独立 |
|---|---|---|
| `/v1/dify/retrieval` | `AIOPS_GATEWAY_DIFY_KNOWLEDGE_API_KEY` | Dify 的外接知识库 |
| `/v1/dify/tools/{tool}` | `AIOPS_GATEWAY_DIFY_TOOLS_API_KEY` | 让七个工具在 Dify 里可选 |
| `/v1/dify/publish` | `AIOPS_GATEWAY_DIFY_PUBLISH_API_KEY` | 运营自助发布 |
| `/v1/dify/trial` | `AIOPS_GATEWAY_DIFY_TRIAL_API_KEY` | 触发一次**真模型 + 真数据**执行 |

nginx 对 `/v1/dify/` 有**单独的 location**：**保留 Dify 自带的 Authorization**、
**不注入服务令牌/来源密钥/入口/会话** —— 注释原话：**"Dify 不是我们的用户"**。

### 公司身份是怎么"融入"的（这是问题的核心答案）

**Dify 本身没有接入公司鉴权体系 —— 接进来的是「谁在用 Dify」。**

```
公司账号 + 密码 + 公司验证码
   → 登录代理（36，回环）亲自完成公司登录交换
   → 读 /upms/user/info 的 permissions
   → 有 aiops:agent:edit ⇒ 为该用户开一个 Dify editor 账号
   → 签一枚 Dify 控制台会话（持 SECRET_KEY 自签，ADR-0023）交给浏览器
```

于是：
- **进 Dify 的资格** = 公司权限码 `aiops:agent:edit`（不是 Dify 角色）；
- **发布到 AI-Ops 的资格** = 公司权限码 `aiops:agent:publish`（桥问代理裁定，ADR-0024）；
- **Dify 自己的登录接口全部关掉**，公司登录是唯一入口（#783 阶段 3）。

**换句话说**：Dify 是**被包在公司鉴权外面**的 —— 它是编辑工具，
"谁能打开它、谁能从它发布"由公司裁定；而 Dify 内部 **不承载任何公司身份**。

---

## 三、一张总图

```
                    ┌──────────── 公司体系 ────────────┐
管家端 APK ──JWT──► │ 公司网关 ──注入 Source-Key──┐    │
                    │      │                     │    │
                    │      └─► UPMS（角色/权限/店铺）  │
                    └──────────────────────────────┼────┘
                                                   ▼
                                        AI-Ops 网关（41）
                              SourceKey 门 → CompanyToken 解析 → ScopeContext
                                                   │
                              tenant_id + site_ids ▼ 只读 SQL
                    ┌──────────── 数据面 ────────────┐
                    │ MySQL / TDengine / Redis（只读）│
                    └────────────────────────────────┘

  浏览器 ──公司账号──► 登录代理（36）──读 AI-Ops 权限码──► 自签 Dify 会话
                                    └─► Dify 控制台（编辑面）
                                             │ 人点发布
                                             ▼
                                    AI-Ops 网关冻结 agent 版本
                                             │
                                             ▼
                                    （回到上面那条运行链）
```

**记住这一句**：**运行时经公司鉴权进 AI-Ops；编辑时经公司鉴权进 Dify。
两条链在"发布"那一跳汇合，Dify 内不承载公司身份。**
