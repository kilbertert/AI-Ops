# 管家端联调链条（前端 → BFF → AI-Ops）：一次请求要经过什么

> **读者**：前端组 / BFF。
> **状态**：2026-09-29 已上线，41 公网入口**实测通过**（下文每条都带实测响应）。
> **一句话**：管家端与客户端**同一个 URL、同一个入口**，差别只在**两个头**：
> `X-Business-Entry: operator` + `Authorization: Bearer <用户 JWT>`。
> **前端必须同时发这两个** —— 少一个就落到另一条链路（下文 §3 有实测对照）。

---

## 0. 先看结论：现在到底能不能调通

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
| `X-Business-Entry: operator` | **前端自己显式发**。41 的 Nginx 在**缺失时补 `consumer`**；漏发的实际后果**取决于还带没带会话** —— 见 §3.1（两种情形，别只记一半） |

> **`tenant-id` 不用发**（服务端取自 JWT 里的 `tenant_id`，发了也不读）。
>
> ⚠️ 关于「漏发入口头会怎样」，口径**只有 §3.1 那一处**（按是否同时带会话分两种后果）——
> 别按「反正会静默降级」理解。
> **`third-session` 不用发**（那是客户端那条链路的凭据，管家端用 JWT）。

---

## 2. 这次请求在链路上经过了什么

```
管家端 App / 页面
  │  Authorization: Bearer <用户JWT>   X-Business-Entry: operator
  ▼
41 的 Nginx（api.mall.qushiyun.com 的 /v1/）
  │  按 X-Business-Entry 分流：
  │    · operator  → 透传用户 JWT；并注入 X-AIOps-Source-Key（来源密钥）
  │    · consumer/缺失 → 换成 AI-Ops 服务令牌（客户端链路，行为与今天逐字一致）
  ▼
AI-Ops 网关（127.0.0.1:8788）
  │  ① 凭据：带来源密钥 ⇒ 认用户 JWT（按公司 HS256 验签）→ 解析出 B 端主体 / C 端用户 / 租户 / 店铺
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
