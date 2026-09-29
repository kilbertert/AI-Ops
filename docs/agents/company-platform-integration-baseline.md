# 立项根基：AI-Ops 是公司体系里的一个模块

> **读者**：任何人改 AI-Ops 的入口、鉴权、身份或数据范围之前。
> **状态**：2026-09-29 定稿（41 生产 + `cloud-gateway` / `cloud-auth` / `cloud-upms` 源码 + 两个
> App 的产物逆向 + UPMS 只读实测）。
> **一句话**：AI-Ops **不自己发明入口、鉴权与身份**；它复用公司既有网关注入、
> OAuth2 令牌体系与数据范围机制。凡是本仓自造了公司体系已经提供的东西，都是待收敛的偏离。
> 判据：**在 41 的生产链路上，AI-Ops 到底有没有站在公司网关之后。**

---

## 0. 为什么这篇是「根基」而不是「一篇排查记录」

「复用公司体系」在很长时间里是一句口头共识：本仓文档里**一次都没有出现过「公司体系」
这四个字**（全库 grep 零命中），于是它无法被执行、无法被评审、也无法被回归。

后果是三次同形的问题，每次都被当成孤立缺陷来修：

| 当时被当成的问题 | 实际上是 |
|---|---|
| 标准 API 面用不上公司的 OAuth2 令牌，于是自己直读共享会话 Redis（ADR-0008 的偏离） | **没有走公司网关**：网关本来就会把 OAuth2 令牌翻译成身份头 |
| 三套 `client-type` 取值互不相交 | **两侧各发各的**：公司网关认的那一套，AI-Ops 不读；AI-Ops 读的那一套，前端不发 |
| 管家端拿着 OAuth2 令牌 `401` | **令牌体系本来是对的**：AI-Ops 缺的是「谁能把它翻译成身份」的那一层 |

三条是同一个根因的三种投影。**根因：AI-Ops 站错了位置** —— 它被挂成了
「Nginx 直连的一个独立 API」，而不是「公司网关身后的一个下游服务」。

---

## 1. 结论先行：41 上 AI-Ops 不在公司网关之后

### 1.1 证据

41 上 `api.mall.qushiyun.com` 的 Nginx 有两条互不相交的转发：

```nginx
location ^~ /v1/ {                       # ← AI-Ops 走这条
    proxy_pass http://127.0.0.1:8788;                       # 41 本机 AI-Ops 网关
    include /etc/aiops-41/nginx-aiops-service-token.conf;   # 注入 AI-Ops 服务令牌
    set $aiops_business_entry $http_x_business_entry;
    if ($aiops_business_entry = "") { set $aiops_business_entry consumer; }
    proxy_set_header X-Business-Entry $aiops_business_entry;
}

location ~* ^/(erp|qm|das|dis|...|upms|mall|mallapi|...)  {  # ← 公司服务走这条
    proxy_pass http://back_server;        # upstream 指向 192.168.1.44:30899（cloud-gateway）
}
```

**`/v1/` 被写在网关的前面。** 于是 AI-Ops 收到的 `X-Business-Entry` 是 Nginx 从**客户端
原样转发**的头部（缺省补 `consumer`），而不是 `cloud-gateway` 按公司规则注入的。

### 1.2 公司网关本来会做什么

`cloud-gateway` 的两个 `AbstractGatewayFilterFactory` 是设计好的两条注入路径：

- **`ApiProxyHeadFilter`**（读 `client-type` + `third-session`，认 `ma` / `h5` / `app`）：
  用 `third-session` 去 `${THIRD_SESSION_BEGIN}:${third-session}`（即 `app:3rd_session:<值>`）
  查 Redis，注入 `user-id`、`uid`、`tenant-id`、`site`、`client-type`。
- **`AdminProxyHeadFilter`**（读 `Authorization` + `client-type`，认 **`admin`**）：
  用 `Authorization` 的令牌值去 `base_oauth:access:<tokenValue>` 查 Redis，
  从令牌的 `additionalInformation` 注入 **`user-id`、`admin-id`、`tenant-id`、`site`**，
  并顺带设 `saasType: STANDARD`。

**第二条正是管家端需要的**：管家端浏览器的存储键是 **`CLOUD_ACCESS_TOKEN`**（OAuth2），
它发的 `client-type` 实测正是 **`admin`**。也就是说公司网关会把「管家端的一个 OAuth2
令牌」翻译成 `user-id` + `tenant-id` —— AI-Ops 现在做的那一大段（读共享 Redis →
按前缀拼键 → 解 Java 序列化载荷 → 补 B 端主体），**大部分是在重抄这一步**。

### 1.3 于是有了一句可执行的判据

> **AI-Ops 应当挂在 `cloud-gateway` 之后，作为它的一个下游服务。**
> `X-Business-Entry` 应当由**受信的那一跳**决定，而不是由调用方自报。
> 只要 `/v1/` 还写在网关之前，本仓的一切「入口判定」都是在替网关做它没做的工作。

⭐ 本轮**不改**这条部署拓扑（那是一次入口变更，要有自己的验收与回滚路径）。
但它是目标态，理由与现状都记在这里，避免它像上次那样悄悄变成默认。

---

## 2. 公司体系的鉴权契约（本次实测，非推测）

### 2.1 令牌：OAuth2，不透明令牌，`cloud-auth` 签发

- `cloud-auth` 是 **Spring Security OAuth2 传统栈**（`@EnableAuthorizationServer` +
  `RedisTokenStore`），不是 JWT + Resource Server。令牌**不透明**，存在 Redis 里，
  前缀 `base_oauth:access:`。
- 客户端（`sys_oauth_client` 表行）+ 用户两类主体；**client-credentials 令牌刻意不做
  token enhancement**（不带身份），用户令牌才把身份写进 `additionalInformation`：
  `id` / `user_id` / `type` / `tenant_id` / `system_id` / `shop_id` / `tenant_ids` / `shop_ids`。
- 校验：资源服务器用 `RemoteTokenServices` 调 **`/oauth/check_token`**（实测可达：
  `http://192.168.1.44:30899/auth/oauth/check_token`，无凭据时返回
  `Full authentication is required to access this resource`）。

### 2.2 数据范围：`@ShopDataScope` + `ShopIdInterceptor`，**还是那一条链**

`ShopIdInterceptor` 的隔离集合来自 **`UpmsAdminFeignClient.getShops(user.getId())`**
（`GET /shopuser/getShops?userId=`），生效条件写死在 `judge()` 里：

```java
String clientType = WebUtils.getRequest().getHeader("client-type");
if (!StrUtil.equalsAny(clientType, "admin", "supply-admin", "tenant-app")) { return false; }
```

平台行 `'-1'` 只在 `seePlatform()==true` 时并入 `IN (...)`。

> **与 #426 的关系**：AI-Ops 的运营商站点范围**用的就是同一条链**
> （`/shopuser/getShops` → `ch_site.shop_id → ch_site.id`），这点没有错、要继续保留。
> 但它生效要求 `client-type ∈ {admin,supply-admin,tenant-app}` —— 而 AI-Ops 的 `/v1/`
> 链路**根本不读 `client-type`**。公司后端是按这个头决定要不要做隔离的。

### 2.3 ⚠️ `@Inside` 端点实际上没有鉴权

`qumall-common/cloud-common-security` 的 `BaseSecurityInsideAspect`：

```java
String header = request.getHeader(SecurityConstants.FROM);
//if (inside.value() && !StrUtil.equals(inside.value()... ) ) {   // ← 整段被注释掉
//    throw new AccessDeniedException("访问被拒绝，没有权限");
//}
return point.proceed();
```

而且 `PermitAllUrlProperties.afterPropertiesSet()` 会把**所有 `@Inside` 端点加进
`releaseUrls` → `permitAll()`**。

**结论**：`/user/inside/byId/*`、`/user/inside/byUserId/*`、`/user/ds`、`/token/page`
**不需要任何凭据**，只要网络可达。`from: Y` 是约定，不是校验。

**对 AI-Ops 的两条实际含义**：

1. `AIOPS_UPMS_INSIDE_TOKEN` **不是**一个需要「拿到正确令牌」的门槛 —— 它只是形式上被带上；
   在 41 上换成稳定入口后立即返回 `code:0`。
2. **这条链的信任边界是网络可达性**，不是令牌。AI-Ops 的管家端授权**建立在它之上**，
   这一点必须写进 ADR-0008 那条偏离的代价里：偏离不只是「读了共享 Redis」，
   还包括「依赖了一组实际无鉴权的内部端点」。

---

## 3. 本轮的决定（六条）

| # | 决定 |
|---|---|
| D1 | **管家端走公司 OAuth2 令牌**：AI-Ops 扩展成能收下管家端的 OAuth2 令牌并解析成身份（客户端的 `app:3rd_session:` 直读路径保留，作为既有链路的兼容）。 |
| D2 | **复用公司已有的令牌校验能力**，不自己实现验签或复制密钥。 |
| D3 | **目标态：AI-Ops 挂在 `cloud-gateway` 之后**（§1.3）。本轮不动部署拓扑，只记录理由与现状。 |
| D4 | **客户端侧直读 `app:3rd_session:` 记为已知偏离**：它其实是在重复公司网关的工作；与 ADR-0008 同源，收敛方向一致。 |
| D5 | **身份与数据范围的一切来源改走公司体系**：后端 `admin-id`/`user-id`、`/shopuser/getShops`、`@ShopDataScope` 的隔离语义**逐条对齐**，不再另立一套。 |
| D6 | 旧 PR #439 关闭；其事实并入本文与交接文档，重开一次 PR。 |

---

## 4. 未决（不猜，逐条列出）

1. **`/oauth/check_token` 的客户端凭据**：`sys_oauth_client` 里给 AI-Ops 用哪一个
   `client_id` / `client_secret`；谁去建这一行。需要业务侧提供。
2. **管家端请求在 41 上是否已经过 `cloud-gateway`**：浏览器直连 `api.mall.qushiyun.com/v1/*`
   时不经过；但若走的是别的域名（例如从网关进来的域），结论会不同。**需要前端确认它实际请求的域名**。
3. **`client-type` 的对齐**：公司后端用 `admin` / `supply-admin` / `tenant-app` 决定是否隔离；
   AI-Ops 现在不读它。管家端产物实测会发 `admin` / `tenant-app`（另有 `H5` / `H5-WX` / `APP` / `"1"` 等
   其它取值），需要确认哪一个才是管家端**真实请求**带的值。
4. **`X-Business-Entry` 的最终归属**：若 AI-Ops 真的移到网关之后，这个头应由谁来发、
   公司与 AI-Ops 之间是否需要新增一条路由（`/aiops/v1/*` 还是继续占用 `/v1/`）。
5. **客户端 App 里 AI-Ops 请求的鉴权方式**：`aiPackage` 只发 `third-session`、不带
   `Authorization`；Nginx 补的是 **AI-Ops 自己的服务令牌**，不是公司令牌。
   这条「客户端不经过公司网关」的现状是否需要一并收敛，取决于 D3 的落地方式。

---

## 5. 这条根基对既有文档的效力

- `docs/adr/0003-bff-delegated-user-identity.md`（委托句柄）与
  `docs/adr/0008-adr-0003-deviation-session-direct-read.md`（直读偏离）**仍然有效**，
  且现在有了更完整的「为什么」：偏离的实质是**站点位置**（不在网关之后），
  不只是「读了哪个存储」。
- `docs/adr/0004-bff-owns-frontend-api-boundary.md`（前端只调 BFF）**未实现**：
  41 上 `/v1/` 公开可达、Nginx 注入服务令牌，前端实际上是直连的。
- `docs/agents/client-type-vs-business-entry.md`：三套取值那部分是事实，仍然有效；
  本文补上「为什么会有三套」——因为**两侧各发各的**，中间没有那一跳做翻译。
- 本文与 ADR 冲突时**以 ADR 为准**；本文记录的是现状事实与目标态，不是决策。
