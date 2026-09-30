# 管家端联调链条（前端 → BFF → AI-Ops）：一次请求要经过什么

> **读者**：前端组 / BFF。
> **状态**：2026-09-29 已上线，41 公网入口**实测通过**（下文每条都带实测响应）。
> **一句话**：管家端与客户端**同一个 URL、同一个入口**，差别只在**两个头**：
> `X-Business-Entry: operator` + `Authorization: Bearer <用户 JWT>`。
> **前端必须同时发这两个** —— 少一个就落到另一条链路（下文 §3 有实测对照）。

---

## 0. 先看结论：现在到底能不能调通

> 🛑 **2026-09-30 前置提醒**：本页描述的行为**已实测可用**，但该链路当前**身份可信度未达标**。
>
> 成因**不是「密钥是默认值」这一条，而是两条叠加**（41 公网实测）：
> ① **签名与 `exp` 都不参与判定** —— 把令牌的签名整段换成 `A…`、载荷逐字不变，结果与
> 原令牌**逐字相同**；② 这条链上「谁是运营商员工」的**外部判据只有来源密钥**，而它由
> **我们自己的 nginx** 注入，任何能到公网入口的人带上 `X-Business-Entry: operator` 即可走进来。
>
> ⇒ **对内联调可以，对外不可宣称可用。** 判定方式、口径与解除条件见
> `operator-repair-blueprint.md` §0.1 与 runbook §1.7 检查单。

**能。** 下面是 41 公网入口的实测（2026-09-29，同一时刻、同一进程）：

| 请求 | 结果 |
|---|---|
| `GET /v1/shortcuts` + **两个头** | `200`，`count: 2`（订单检测 / 客户案例） |
| `GET /v1/faq/recommendations` + **两个头** | `200`，`platform: operator`（返回**管家端**的 FAQ 目录） |
| `POST /v1/assistant/questions` + **两个头**（客户案例） | `202`，`type: qa` |
| 同样三个请求，**只发 JWT、不发入口头** | **`401 INVALID_ACCESS_TOKEN`** |
| **什么都不发**（照现在的联调脚本） | **`401 INVALID_ACCESS_TOKEN`** |

**⚠️ 前端现在那张截图里的打法（`GET .../v1/faq/recommendations`、不带任何头）拿到的 401，是预期结果** ——
那条请求既没有用户身份、也没有说明自己是哪个端。加上 §1 那两个头就会通。

---

## 1. 请求要带什么（照抄即可）

```http
POST https://api.mall.qushiyun.com/v1/assistant/questions
Authorization: Bearer <管家端登录拿到的 OAuth2 access_token>   # ← 用户 JWT
X-Business-Entry: operator                                    # ← 管家端就是这一行
Content-Type: application/json
```

**只有这两个头是必须的**，其余与客户端完全一致（URL、方法、返回体形状、轮询）。
⚠️ **取消不是全都一样**：`type=qa` 的作业可以取消；**订单检测的 `type=diagnosis` 没有取消接口**。

### 头从哪来

| 头 | 来源 |
|---|---|
| `Authorization: Bearer <JWT>` | 管家端 App 登录（`/upms/token/login` 那条链）拿到的 `access_token`。**已经在客户端产物里存着**（`CLOUD_ACCESS_TOKEN`），不用新做登录 |
| `X-Business-Entry: operator` | **前端自己显式发**（当前归属定案：前端自报；**目标态**是 D 批之后由公司网关按路由注入并覆盖同名头）。41 的 Nginx 在**缺失时补 `consumer`**；漏发的实际后果**取决于还带没带会话** —— 见 §3.1（两种情形，别只记一半） |

> ⚠️ **这个头决定「内容域」，不是「你是谁」**（2026-09-30 写进契约，可回归形式见
> `tests/test_company_token_auth.py` 的 §8-bis）。它由调用方自报，换来的只有「按哪套目录
> 作答」与「范围取哪一种口径」；**身份始终来自 `Authorization` 里的令牌**。因此
> **「带上 `operator` 头」本身不是一条能用的路径** —— 令牌无效时结果仍是 401。
> 前端不必、也不应该把入口头当成权限声明。

> **`tenant-id` 不用发**（服务端取自 JWT 里的 `tenant_id`，发了也不读）。
>
> ⚠️ 关于「漏发入口头会怎样」，口径**只有 §3.1 那一处**（按是否同时带会话分两种后果）——
> 别按「反正会静默降级」理解。
> **`third-session` 不用发**（那是客户端那条链路的凭据，管家端用 JWT）。

### 其余头：AI-Ops 只读这四个（别照着「删干净」）

公开路径上 AI-Ops **实际会读**的请求头共四个：

| 头 | 谁需要 | 说明 |
|---|---|---|
| `Authorization` | **管家端必需** | 用户 JWT（管家端）或服务令牌（客户端） |
| `X-Business-Entry` | **管家端必需** | 内容域分流；**不是身份**（见上） |
| `Accept-Language` | **可选，但影响结果** | 决定回答语言（`en` / `zh-CN` …）。**删掉它会改变语言** |
| `Range` | 可选 | 媒体分片读取用 |

**其余头一律忽略** —— `client-type`、`tenant-id`、`app-id`、`Referer`、`sec-ch-*`、
`Connection`、`third-session` 等，发不发、发什么都不影响结果：

- ⚠️ **`third-session` 是例外中的例外**：它在**客户端**那条链路上是凭据（删掉会让客户端
  认证失败），只是**管家端**这条链路不用它。所以「可以删」只针对管家端；
- `client-type: H5` 形状正常，但 **AI-Ops 完全不读**（它由**公司后端**读）。两套取值并存
  是既定事实（见 [client-type-vs-business-entry.md](client-type-vs-business-entry.md)），
  **不要**指望用它在 AI-Ops 侧切入口；
- `tenant-id` 同上：服务端取自 JWT 里的 `tenant_id`，发了也不读。

### 一个需要前端确认的小问题（B4）

前端发出的 `-H 'app-id;'` 是一个**畸形头**（没有冒号，不是合法的 `name: value`）。
AI-Ops 侧**零改动**（它不在上面那四个里，会被忽略）。**请前端确认是否笔误**：
是笔误就去掉；确实需要就补成合法形状 `app-id: <值>` —— 无论如何**不写进接口契约**。

---

## 2. 这次请求在链路上经过了什么

```
管家端 App / 页面
  │  Authorization: Bearer <用户JWT>   X-Business-Entry: operator
  ▼
41 的 Nginx（api.mall.qushiyun.com 的 /v1/）
  │  按 X-Business-Entry 分流：
  │    · operator  → **改经公司网关**（cloud-gateway）：透传用户 JWT，
  │                 由**网关那一跳**注入 X-AIOps-Source-Key（2026-09-30 D-4 起）
  │    · consumer/缺失 → 直连 AI-Ops，换成 AI-Ops 服务令牌（行为与今天逐字一致）
  ▼
AI-Ops 网关（127.0.0.1:8788）
  │  ① 凭据：带来源密钥 ⇒ 认用户 JWT（按**公司当前口径**判定：不验签、不判 exp；
  │     只要求结构是 JWT、alg 声明为 HS256、身份字段齐备）→ 解析出 B 端主体 / C 端用户 / 租户 / 店铺
  │  ② 内容域：X-Business-Entry = operator ⇒ 走「管家端」FAQ 目录与动作列表
  │  ③ 数据范围：运营商站点集合（由该公司账号名下店铺 → ch_site 站点）
  ▼
返回 —— 形状与客户端完全一致
```

**对它做联调时只需要记住两条**：`operator` 决定「内容域 + 订单范围」，用户 JWT 决定「你是谁」。

---

## 3. 三个易错点（每个都有实测对照）

### 3.1 漏发 `X-Business-Entry` —— 后果**取决于还带没带会话**（实测）

Nginx 缺省把入口补成 `consumer`，并**把 `Authorization` 换成 AI-Ops 服务令牌**。于是：

| 只带 JWT、漏入口头 | 结果 |
|---|---|
| **只带用户 JWT**（管家端的正常形态） | **`401`** —— 因为换成服务令牌后那条链**需要 `third-session`**，而管家端没有它 |
| 同时带**有效的 `third-session`**（极少见） | `200`，但落到**客户端内容域与本人订单范围**（**静默走错域**） |

⇒ 管家端**漏发入口头通常表现为 `401`**（好定位）；真正危险的是**第二种**：一个同时带着
客户端会话的双平台账号，漏头会静默拿到客户端内容域。**两条都指向同一个结论：这个头必须显式发。**

**排查信号**：`GET /v1/shortcuts` 的响应**没有** `platform` 字段（别拿它自检）。用这两条：
- **最可靠的是 `/v1/faq/recommendations` 的 `platform` 字段**（下面那条）；
- **次选**：动作列表的 `count` 与内容 —— **但这只是当前发布状态**（动作是运营发布的，
  数量会变）：现在管家端 `2` 条、客户端 `3` 条；`case_exploration` 在客户端带
  `target_agent_version: "agt_…#vN"`、管家端为 `null`。**别把 `count` 写成硬断言**；
- **需要 `platform` 字段时改用 `GET /v1/faq/recommendations`** —— 它的响应里有
  `platform: operator` 与 `available_platforms`。

### 3.2 只发入口头、不发 JWT ⇒ `401`

不是「权限不足」，而是**根本没有身份**。响应：
```json
{"error":{"code":"INVALID_ACCESS_TOKEN","message":"access token validation failed","retryable":false}}
```

### 3.3 传了非法入口值（如 `operator-admin`）⇒ `401`

非法值会被**原样透传**给上游，由上游拒掉 —— 而**不会**被悄悄改写成 `consumer`。

所以「传错值」与「不传头」的后果**不同**，而且两种都别按一句话记：

| 情形 | 后果 |
|---|---|
| **传错值**（`operator-admin`） | **`401`** —— 明确失败，不会被当成任何一端 |
| **不传头**，且只带用户 JWT | **`401`**（服务令牌链缺 `third-session`），见 §3.1 |
| **不传头**，且**同时带有效会话** | `200`，但落到**客户端内容域**（静默走错域）—— 这才是危险的那一种 |

⇒ 「不传头会静默走错域」**只在第三种情形成立**；正常管家端（只带 JWT）两种都是 `401`。

---

## 4. 两个动作点下去会发生什么（实测）

`GET /v1/shortcuts` + 两个头：

```json
{
  "type": "shortcut_list", "language": "zh", "count": 2,
  "shortcuts": [
    {"code": "case_exploration", "intent": "case_exploration", "requires_order": false,
     "label": "客户案例", "question_template": "有哪些充电运营的客户案例",
     "target_agent_version": null, "jump_path": null},
    {"code": "smart_diagnosis", "intent": "order_issue", "requires_order": true,
     "label": "订单检测", "question_template": "帮我检测这个订单的充电异常",
     "target_agent_version": null, "jump_path": null}
  ]
}
```

**渲染规则**（与客户端相同）：`jump_path` 有值 ⇒ 本地跳转；为 `null` ⇒ 提示动作，点击后调
`POST /v1/assistant/questions`；`requires_order: true` ⇒ 点击时先弹订单选择器。

⚠️ **请求体的字段名是 `shortcut_code`，不是 `code`** —— 该请求体是 `extra="forbid"` 的，
**传错字段名会直接 `422`**（不是被忽略）。两个动作的完整请求体：

```json
// 订单检测（不带订单号 → 200 clarification，前端据此弹选择器）
{"question": "帮我检测这个订单的充电异常", "shortcut_code": "smart_diagnosis"}

// 订单检测（用户选完单 → 202 diagnosis）
{"question": "帮我检测这个订单的充电异常", "shortcut_code": "smart_diagnosis", "order_no": "<订单号>"}

// 客户案例（→ 202 qa）
{"question": "有哪些充电运营的客户案例", "shortcut_code": "case_exploration"}
```

### 4.1 订单检测（`smart_diagnosis`）

- **主流程**：前端看到 `requires_order: true` ⇒ 点击即弹订单选择器 ⇒ 带上 `order_no` 发请求。
- **兜底**：万一没带 `order_no`，后端返回 `200 clarification` + `missing_fields: ["order_no"]`。
  **这不是两套 UI**：同一个选择器组件，前端主动弹是优化，后端澄清是保底。
  实测（不带单号）：`{"type":"clarification","missing_fields":["order_no"],"platform":"operator"}`
- 带上订单号 ⇒ `202` + `retry_after_ms`，**轮询契约与客户端完全相同**。
- 订单不在可见范围内 ⇒ **`404`**，提示「订单不存在或无权查看」，**不要重试**。

> ⚠️ **当前终态提醒**：验收用的运营商账号**没有任何店铺绑定**，所以它的可见站点集合是空的
> ⇒ **带单号也仍然是 `404`**。这是**数据没配**（产品/运营补绑定），不是接口问题 ——
> 联调时用**已有店铺绑定的运营商账号**才能看到 `202`。

### 4.2 客户案例（`case_exploration`）

`202` + `type: qa` → 轮询至 `completed`。**当前终态**：`retrieval_status: "unavailable"`，
文案「客户案例服务暂时不可用」。

**这不是故障**：该动作的知识库素材与智能体绑定**尚未配置**（需要新建一个宣传类智能体、
绑定知识库，并把它的版本绑到该动作的**租户行**上）。前端按「暂不可用」渲染即可，
**不要**写成「没有找到案例」——后端刻意区分了 `unavailable` 与 `not_found`。

---

## 5. 前端接入清单

- [ ] 进管家端的页面/请求，**同时**带 `X-Business-Entry: operator` 与 `Authorization: Bearer <用户JWT>`。
- [ ] 用响应里的 `platform` 字段自检：应当是 `operator`（若是 `consumer` ⇒ 入口头没生效）。
- [ ] 动作列表按 `jump_path` / `requires_order` 决定行为，**不要硬编码 `code` 列表**。
- [ ] 订单检测：`requires_order: true` 时点击即弹选择器；同时处理后端返回的 `clarification`（同一个组件）。
- [ ] **订单检测**的 `404 ORDER_NOT_FOUND` 提示「订单不存在或无权查看」，**不重试**。
      （FAQ / 问答作业 / 会话的 `404` 是另一回事，别套这句文案。）
- [ ] `401` 时先查**两个头是否都发了**，再怀疑登录态。
- [ ] 不要保存、打印或打包 AI-Ops 服务令牌（它只在 BFF 与 Nginx 之间）。

---

## 6. 还没完成的两件事（会影响你看到的终态，但不阻塞联调）

1. **运营商账号的店铺绑定**（运营/产品）：没有绑定 ⇒ 站点范围为 Ø ⇒ 订单检测恒 `404`。
   补上之后**前端不用改任何代码**。
2. **客户案例的素材 + 智能体绑定**（产品）：配好后同样的请求返回 `retrieval_status: "found"`，
   前端也不用改代码。

另：**本机制目前用公司源码里的默认签名密钥**（换钥匙是公司侧的事，已在跟踪），
它不影响前端联调，但决定了这套机制在换钥匙前的安全边界。
