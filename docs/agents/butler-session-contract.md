# 管家端的会话从哪来：`third-session` 契约与当前真实缺口

> **读者**：前端组 / BFF / 排「管家端鉴权失败」的人。
> **状态**：2026-09-29 实测（41 生产 + 两个前端产物逆向 + UPMS 只读查询）。
> **先读**：[company-platform-integration-baseline.md](company-platform-integration-baseline.md)
> —— 本文每一个「缺口」都是那条根因（AI-Ops 不在公司网关之后）的投影。
> **一句话**：**今天** AI-Ops 只认**共享 Redis 里的 `app:3rd_session:<...>` 会话**；
> 管家端 App 登录走的是 OAuth2 令牌，**不写这个会话**，所以前端直接把管家端 token
> 塞进 `third-session` 必然 `401`。**令牌本身是对的**，缺的是把它翻译成身份的那一跳
> —— 该跳的定稿形状见 §3 与 [ADR-0009](../adr/0009-operator-identity-via-company-oauth2-token.md)。

---

## 1. 前端看到的 401 不是入口头的问题

前端反馈的响应体逐字如下，这**正好**是「服务令牌或会话无效」那一种：

```json
{"error":{"code":"INVALID_ACCESS_TOKEN","message":"access token validation failed","retryable":false}}
```

41 上复现（同一份线上代码、同一入口）：

| 发的 `third-session` | 结果 |
|---|---|
| 管家端登录取到的 OAuth2 `access_token` | **`401 INVALID_ACCESS_TOKEN`** |
| 一条**真实存在**的 C 端 thirdSession（裸 uuid） | **`200`**，`platform=consumer` |
| 一条**不存在或已过期**的 uuid | **`401 INVALID_ACCESS_TOKEN`** |
| 完整的 Redis 键名 `app:3rd_session:<uuid>` | **`401 INVALID_ACCESS_TOKEN`** |

判据在 `RedisThirdSessionResolver.resolve`：

1. `Authorization: Bearer` 必须等于服务端配置的服务令牌（**由 41 的 Nginx 注入**，
   前端不持有）；
2. `third-session` 的值是**裸的会话值**（不带前缀）—— 服务端自己拼
   `AIOPS_GATEWAY_THIRD_SESSION_KEY_PREFIX + <值>` 去 `GET`。
   因此**传整键名会 401**（前缀被拼了两遍），**传 OAuth2 令牌也会 401**
   （那是另一个命名空间的值，不是这个键的一部分）。

> ⚠️ **裸 uuid 本身不是拒绝理由**：它正是服务端要的值，键存在就 `200`。
> 「裸 uuid ⇒ `401`」这个说法只对**不存在或已过期**的那种成立 —— 而 OAuth2 令牌
> 属于前者（值对不上任何键），不是后者。

## 2. 两个 App 各用各的凭据

| | 客户端 `ulink.h5.mall.qushiyun.com` | 管家端 `ulinkmanage.h5.mall.qushiyun.com` |
|---|---|---|
| 产物包 | `aiPackage/*`、`shopPackage/*`… | `adminPackage/*`（**没有 `aiPackage`**） |
| 登录 | 拿 `thirdSession` 存 `third_session` | `/upms/token/login`（`grant_type=password\|sms_login`, `scope=server`, `deviceType=shop`）→ OAuth2 |
| 存储键 | `third_session` | **`CLOUD_ACCESS_TOKEN`** / `CLOUD_REFRESH_TOKEN` / `CLOUD_LOGIN_INFO` |
| 发往公司服务 | `third-session` 头 | `Authorization: Bearer <OAuth2>`，`client-type` 实测发 **`admin`** |
| 聊天页 | **有**（`aiPackage`） | **没有** |

> 这就是「登录只有这个，和用户端又不一样」的确切含义：两个 App 的登录后端与凭据体系
> 本来就不同，不是同一套 token 换个入口头。

客户端聊天页实测发的头（逐字）：`Content-Type / Accept / tenant-id / Accept-Language /
client-type / third-session / X-Business-Entry`，其中 `X-Business-Entry` **硬编码 `"consumer"`**。
管家端产物里 `X-Business-Entry` **零命中** —— 管家端入口目前没有一个管家端页面在调。

## 3. 交付形状：AI-Ops 认公司 OAuth2 令牌（分两段）

曾有两版被取代的结论，先记下来免得再绕回去：

- ✗ 「由 BFF 签发一条 `app:3rd_session:*`」 —— 在给一个**已经存在**的注入路径造第二条，
  等于把根因（AI-Ops 站错位置）固化下来。
- ✗ 「等 AI-Ops 挪到 `cloud-gateway` 之后再说」 —— 拓扑变更带 exposure 决定与
  35 条路由的爆炸半径，把它当成前置会让管家端一直不可用。

**定稿形状**（ADR-0009）：

- **AI-Ops 自己调公司权威的 `/auth/oauth/check_token`** 校验管家端令牌，
  身份与数据范围**直接取自令牌**（`id` = B 端主体、`user_id` = C 端、`tenant_id`、
  `shop_ids`）。校验落在公司自己那一跳，不自己验签。
- **不再调** `/user/inside/byUserId`（C→B）与 `/shopuser/getShops`（店铺集合）——
  令牌已经带着答案；少两跳，其中一条还是**实测无鉴权**的内部端点。
- **入站信任 = 来源 + 共享密钥**（由网关那一跳注入，前端不持有）；
  两条信任模式按来源密钥分派。
- **分两段**：第一段只做 AI-Ops 侧，**合入即生效但默认关闭**（三个配置键全空 =
  与今天逐字一致）；第二段做网络/路由/Nginx，先 41。

**第二段落地前需要业务侧提供**（基线文档 §4，**两条都已在 2026-09-29 更新形状**）：

- `/oauth/check_token`：问题**不是**「用哪个 `client_id`/`secret`」—— 库里 7 个客户端的 secret
  **都等于自己的 id**（`admin`/`admin` …，公开值），而带上它请求与不带请求得到**逐字相同**的
  `Full authentication is required`；`/oauth/token` 则返回「验证码不能为空」。⇒ 要问公司侧的
  是**「怎么让一个服务调用这条路径」**（建一个真正的客户端？另有入口？）。
- 管家端实际请求的域名：**已有答案** —— `api.mall.qushiyun.com`，其前缀全部已经过公司网关，
  只有 `/v1/` 被单独摘出来直连 AI-Ops。

> **状态更正（2026-09-29，#444 已交付）**：上一条「定稿形状」的前四点已实现
> （`company_token_auth.py`），**启用门也已装上**（`caller_auth.SourceKeyCallerResolver`：
> `X-AIOps-Source-Key` 带且验过才走新链路，未配置密钥则整层不构造）。但**仍未可用**：
> 41 现网配置下这条路径不可达，因为第二段的网络与路由没做，而且**来源密钥还没有注入主体**
> —— 设计是「可信的那一跳」（未来是公司网关），第二段才接上。**管家端今天的 `401` 一个字
> 都没变**，前端现在照样不能直接接。
> ⚠️ **还有一条更前置的**：公司侧那条校验入口对「服务间调用」是否成立**尚未确认，且证据指向
> 不成立**（见上）。因此第二段**做完也不会有可用结果** —— 路由通了、令牌也换不出身份。
> 四条边界见基线文档 §3.4，公司侧依赖见 §4 第 1 条。

**对前端的两条硬性要求**：

1. 管家端请求**必须带 `X-Business-Entry: operator`**（见 §4）；
2. 客户端聊天页那条**硬编码的 `"X-Business-Entry": "consumer"`** 在双平台账号上会把
   管家端用户按客户端处理 —— 别照抄进管家端。

## 4. 与入口有关的两个既有坑（本次一并核实）

- **`client-type` 在 AI-Ops 侧不生效**：服务端不读它。41 的 Nginx 只转发
  `X-Business-Entry`（`$http_x_business_entry`），**缺省补 `consumer`**。
  详见 [client-type-vs-business-entry.md](client-type-vs-business-entry.md)。
  注意公司后端**是读 `client-type` 的**（`ShopIdInterceptor.judge()` 要求它属于
  `admin`/`supply-admin`/`tenant-app` 才做数据隔离）——两边对同一个头有不同依赖，
  这是 §1 根因的直接后果。
- **`409 PLATFORM_AMBIGUOUS` 在公网可达**：运营商的验收账号同时有客户端 id
  与管家端角色（`client_type` = `admin` / `tenant-app`），于是两个平台都可用；
  此时**不带 `X-Business-Entry`** 会被平台决策判为歧义并拒绝
  （`business entry is required to select a platform`）。管家端必须显式带这个头。
- **`tenant-id` 请求头没有被 AI-Ops 使用**：租户来自会话载荷的 `tenantId`。
  带它无害，但排查时别把它当身份来源。

## 5. 站点绑定缺口：当前 41 上管家端订单范围是空的

一个运营商账号（租户 `<该租户>`）实测：

```
GET /user/inside/byUserId/<C 端用户 id>
  → data = { id: <B 端主体 id>, userId: <C 端用户 id>,
             type: "5", username: "<运营商账号>", shopId: null, shopIds: [] }   # 唯一 B 端主体 ✓

GET /user/inside/byId/<B 端主体 id>        # 注意与上面同值，不是笔误
  → data = { id: <B 端主体 id>, shopId: null, shopIds: [] }

GET /shopuser/getShops?userId=<B 端主体 id>
  → {"code":0,"data":[]}                                                        # 没有绑定任何店铺 ✗
```

AI-Ops 的运营商站点范围 = 「该 B 端主体的店铺集合 → `ch_site` 站点」。
店铺集合为空 ⇒ 站点集合为空 ⇒ **该账号的订单查询一律 `404`**。这是**设计内的
fail closed**（空集合不得被改写成「不限制」），不是故障：**数据没配**。

**归属**：运营商账号的店铺绑定由业务侧补登记（已确认）。在补上之前，即使身份打通，
管家端「订单检测」对这类账号仍然是「订单不存在或无权查看」。

对照组：另一租户的一个运营商账号在 UPMS 里同样唯一
且带管家端角色（`type=5`），但 Redis 里**没有它的会话**
（今天没登录过），因此连「拿它验收」都做不到。

## 6. 对 2026-09-28 那条验收结论的更正

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

### 复现命令（41）

```bash
B=https://api.mall.qushiyun.com/v1/faq/recommendations
curl -sS -H "third-session: <任意非 Redis 会话值>" $B          # → 401 INVALID_ACCESS_TOKEN
curl -sS -H "third-session: <真实 app:3rd_session:* 的值>" $B   # → 200 platform=consumer
```

（`third-session` 的值由 Nginx 原样转发；服务令牌由
`/etc/aiops-41/nginx-aiops-service-token.conf` 在 `location ^~ /v1/` 内注入。）
