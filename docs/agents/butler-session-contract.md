# 管家端的会话从哪来：`third-session` 契约与当前真实缺口

> **读者**：前端组 / BFF / 排「管家端鉴权失败」的人。
> **状态**：2026-09-29 实测（41 生产 + 41 上前端产物逆向 + UPMS 只读查询）。
> **一句话**：AI-Ops 只认**共享 Redis 里的 `app:3rd_session:<...>` 会话**；
> 管家端 App 登录走的是 `/upms/token/login` 的 **OAuth2 令牌**，**不写这个会话**，
> 所以前端直接把管家端 token 塞进 `third-session` 必然 `401`。
> 交付形状已定：**由 BFF 签发 thirdSession**（见 §5）。

---

## 1. 前端看到的 401 不是入口头的问题

前端反馈的响应体逐字如下，这**正好**是「服务令牌或会话无效」那一种，不是入口问题：

```json
{"error":{"code":"INVALID_ACCESS_TOKEN","message":"access token validation failed","retryable":false}}
```

41 上复现（同一份代码、同一入口）：

| 发的 `third-session` | 结果 |
|---|---|
| 管家端登录取到的 OAuth2 `access_token` | **`401 INVALID_ACCESS_TOKEN`** |
| 裸的会话 uuid（不发 `app:3rd_session:` 前缀） | **`401 INVALID_ACCESS_TOKEN`** |
| 完整的 Redis 键名 `app:3rd_session:<uuid>` | **`401 INVALID_ACCESS_TOKEN`** |
| 一条**真实存在**的 C 端 thirdSession | **`200`**，`platform=consumer` |

判据在 `RedisThirdSessionResolver.resolve`：

1. `Authorization: Bearer` 必须等于服务端配置的服务令牌（**由 41 的 Nginx 注入**，
   前端不需要也不应该持有它）；
2. `third-session` 的值是一个 **Redis 键的组成部分**，不是「一个 token」——
   服务端自己拼 `AIOPS_GATEWAY_THIRD_SESSION_KEY_PREFIX + <值>` 去 `GET`。
   所以传 token、传裸 uuid、传整键名，三种都不会命中。

## 2. 管家端登录产生的是 OAuth2 令牌，不是 thirdSession

管家端 H5 = `ulinkmanage.h5.mall.qushiyun.com`（产物是 `adminPackage/*`）。
它的登录调用链是：

```
POST /upms/token/login?username=&password=&randomStr=&code=&grant_type=password&scope=server&deviceType=shop
POST /upms/token/loginByPhone?phone=&code=&grant_type=sms_login&scope=server&deviceType=shop
```

拿到的 `{access_token, refresh_token, expires_in}` 存在
`uni.setStorageSync` 的 `CLOUD_ACCESS_TOKEN` / `CLOUD_REFRESH_TOKEN` / `CLOUD_LOGIN_INFO`，
后续请求带 `Authorization: Bearer <access_token>`。

**它完全不写 `third_session`，也不发 `third-session`。** 全量搜 `adminPackage`：
`getStorageSync("third_session")` 在 `adminPackage/*` 里**零命中**。

而 AI-Ops 的会话解析走的是另一条完全不同的链：

```
third-session → app:3rd_session:<值> (共享 Redis) → {userId, tenantId}
```

这条链是**客户端**用的：客户端产物（`ulink.h5.mall.qushiyun.com` = `aiPackage/*`）里，
登录后 `uni.setStorageSync("third_session", <thirdSession>)`，请求时
`"third-session": uni.getStorageSync("third_session")`。

> **这就是「登录只有这个，和用户端又不一样」的确切含义**：两个 App 的登录
> 后端与凭据体系本来就不同，不是同一套 token 换个入口头。

## 3. 聊天页现在只存在于客户端 App 里

41 上两个产物的对比（同一份单 App 代码的两个构建目标）：

| 域名 | 产物包 | 有 `aiPackage`（聊天页）吗 | 有 `adminPackage` 吗 |
|---|---|---|---|
| `ulink.h5.mall.qushiyun.com`（客户端） | `aiPackage/*`、`shopPackage/*`… | **有** | 没有 |
| `ulinkmanage.h5.mall.qushiyun.com`（管家端） | `adminPackage/*`… | **没有** | **有** |

所以「管家端入口」目前**在管家端 App 里没有一个页面在调它**。
`X-Business-Entry` 在管家端产物里同样**零命中**。

客户端聊天页发往 AI-Ops 的头（实测源码，逐字）：

```js
u = {
  "Content-Type": "application/json", Accept: "application/json",
  "tenant-id": o.default.tenantId,
  "Accept-Language": r,
  "client-type": c,                       // H5 / H5-WX —— 对 AI-Ops 无效
  "third-session": h,                     // uni.getStorageSync("third_session")
  "X-Business-Entry": "consumer"          // ← 客户端里是硬编码，不是按端推导
}
```

## 4. 与入口有关的两个既有坑（这次一并核实）

- **`client-type` 在 AI-Ops 侧不生效**：服务端不读它。41 的 Nginx 只转发
  `X-Business-Entry`（`$http_x_business_entry`），**缺省补 `consumer`**。
  详见 [client-type-vs-business-entry.md](client-type-vs-business-entry.md)。
- **`409 PLATFORM_AMBIGUOUS` 在公网可达**：管家端账号 `15800395017` 同时有
  客户端 id 与管家端角色（`client_type` = `admin` / `tenant-app`），
  于是「客户端」和「管家端」两个平台都可用；此时**不带 `X-Business-Entry`** 就会被
  平台决策判为歧义并拒绝（`business entry is required to select a platform`）。
  **管家端必须显式带这个头**，漏传不是静默降级。
- **`tenant-id` 请求头没有被使用**：租户来自会话载荷的 `tenantId`，
  不是这个头。前端带它无害，但排查时别把它当成身份来源。

## 5. 交付形状：由 BFF 签发 thirdSession

**已定（2026-09-29）**：管家端登录后，由**业务后端**像 C 端一样向共享 Redis 写一条
`app:3rd_session:<不透明值>`（内容含 `userId`（C 端 id）与 `tenantId`），
前端把它当 `third-session` 发。AI-Ops **零改动**即可复用整条既有链路：

```
third-session ─► app:3rd_session:<值> ─► userId/tenantId
             └► /user/inside/byUserId/<userId> ─► 唯一 B 端主体
             └► /shopuser/getShops?userId=<B 端 id> ─► 店铺 ─► ch_site ─► 站点集合
```

**为什么是这个形状而不是「AI-Ops 也认 OAuth2 令牌」**：那条路要给 AI-Ops 新增一条
安全敏感的鉴权分支（验 token、补主体、再判定），评审与验收面积远大于让 BFF 签发一条
本来就存在的会话；而且**BFF 断言身份**正是 ADR-0003 想要的方向
（现在的实现是 AI-Ops 直读 Redis，见 [0008](../adr/0008-adr-0003-deviation-session-direct-read.md)）。
签发侧的键名/前缀/载荷字段必须与 C 端完全一致，否则解析会失败关闭。

**对前端的两条硬性要求**（与 §4 呼应）：

1. 管家端请求**必须带 `X-Business-Entry: operator`**；
2. 客户端聊天页那条**硬编码的 `"X-Business-Entry": "consumer"`** 在双平台账号上
   会把管家端用户按客户端处理，接口形状是对的、语义是错的 —— 别照抄进管家端。

## 6. 站点绑定缺口：当前 41 上管家端订单范围是空的

`15800395017`（运营商，租户 `2019588094906601472`）实测：

```
GET /user/inside/byUserId/2043951654176063490
  → data = { id: 2043992894120771586, userId: 2043951654176063490,
             type: "5", username: "15800395017",
             shopId: null, shopIds: [], ... }          # 唯一一个 B 端主体 ✓

GET /shopuser/getShops?userId=2043992894120771586
  → {"code":0,"data":[]}                               # 没有绑定任何店铺 ✗
```

AI-Ops 的运营商站点范围 = 「该 B 端主体的店铺集合 → `ch_site` 站点」。
店铺集合为空 ⇒ 站点集合为空 ⇒ **该账号的订单查询一律 `404`**。这是**设计内的
fail closed**（空集合不得被改写成「不限制」），不是故障：**数据没配**。

**归属**：运营商账号的店铺绑定由业务侧补登记（已确认）。在补上之前，
即使 thirdSession 打通，管家端「订单检测」对这类账号仍然是「订单不存在或无权查看」。

顺带一条同类事实：租户 `1942105476598861824` 的运营商账号 `13333102001` 在 UPMS
里**存在且唯一**（B 端 id `1980205180737404929`，`type=5`，`client_type` = admin /
tenant-app），今天连「Redis 里有没有它的会话」都查不出来——因为它没登录过。

## 7. 对 2026-09-28 那条验收结论的更正

`docs/validation.md` 与 [frontend-operator-handoff.md](frontend-operator-handoff.md)
写的「管家端入口与订单运营商级授权已在 41 端到端验收通过」，需要收窄：

- **仍然成立**：运营商站点范围的判定与 fail closed、`operator` 动作列表（2 条）、
  显式 `operator` 的订单检测路径、入口判据、会话按入口隔离。
- **需要更正**：验收用的是**代造的会话行**（在租户内挑了一条裸 uuid 的会话记录并
  直接发它），**不是一次管家端真实登录**。管家端 App 的登录链路
  （`/upms/token/login` → OAuth2 → 无 thirdSession → **`401`**）当时**没有被走到**，
  实际是**不通**的。这不是代码缺陷，是当初把「恰好有 B 端账号的会话」当成了
  「管家端登录」，而这两者在凭据层根本不是一回事。

更正依据是本次的复现：裸 uuid / 整键名 / OAuth2 令牌三种发法**全部 `401`**，
只有真实存在于 Redis 的 `app:3rd_session:*` 值才通（并返回 `platform=consumer`）。
