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
    proxy_pass http://back_server;        # upstream 指向 <公司网关>（cloud-gateway）
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
- 校验：资源服务器用 `RemoteTokenServices` 调 **`/oauth/check_token`**（`<公司网关>/auth/oauth/check_token`）。
  ⚠️ **「无凭据时返回 `Full authentication is required`」曾被读成「端点可达、只差凭据」—— 这是错的**，
  见 §4 第 1 条：带凭据时返回**逐字相同**的那句话。
  **两种响应的形状不同**（2026-09-29 读源码更正，实现依据见
  [``company_token_auth.py``](../../src/aiops_diagnostics/company_token_auth.py) 的模块文档）：
  - **成功**：令牌的 `additionalInformation` 被**合并进顶层**（不是包在 `data` 里），
    且**没有** `code`/`data` 信封。公司那两个 `ResponseBodyAdvice`
    （`I18nResponseAdvice`/`TenantNameResponseAdvice`）的 `supports()` 都要求**控制器方法声明的
    返回类型**可被 `R` 赋值，而 `check_token` 声明返回 `Map` ⇒ 不包装。
  - **成功体比「只有身份字段」更宽**：`DefaultAccessTokenConverter.convertAccessToken` 先放
    框架字段（`username`/`authorities`（仅用户令牌分支）/`scope`/`exp`/`jti`，`resourceIds`
    非空时还有 `aud`），最后一步 `response.putAll(token.getAdditionalInformation())` **合并并
    覆盖同名键**，之后 `CheckTokenAccessTokenConverter` 再补 `active: true`，框架再补
    `client_id`。所以成功体**确实带** `scope`/`exp`/`client_id`；身份判据只能用**增强器写的**
    `id` + `tenant_id`（`username`/`client_id`/`scope`/`exp`/`authorities` 对**客户端凭据令牌
    同样存在**，只有增强器字段能区分）。
  - **失败**：非法/过期令牌由 `CheckTokenEndpoint` 抛异常 → 公司
    `BaseWebResponseExceptionTranslator` → `ResponseEntity.ok().body(R.failed(
    e.getOAuth2ErrorCode(), e.getMessage()))`，即 **HTTP 200 +
    `{"code":<码>,"msg":"token无效","data":null}`**（`CommonConstants.SUCCESS=0`/`FAIL=1`；
    `R.failed(Integer, String)` 走 `restResult(null, code, msg)` ⇒ `data` 是 **null**）。
    **不是 4xx**，所以「HTTP 200 就代表令牌有效」是错的。
    ⚠️ `data:"invalid_token"` 是**另一条**路径的形状（资源服务器入口
    `ResourceAuthExceptionEntryPoint`，且它硬编码 401），**不是** `check_token` 的。
  - 因此判定必须写在**信封**上（「存在且非 0/200 的 `code` ⇒ 拒绝」），
    不能写成「`code` 必须等于 0」（会把每条合法令牌都拒掉），**也不能换成 HTTP 状态**
    （无效令牌也走 200，按状态判会把它读成成功）。
  - ⚠️ 客户端 Basic 凭据不对时是**另一种形状**：`checkTokenAccess("isAuthenticated()")` 返回
    **HTTP 401 + OAuth2 标准错误 JSON**（不是 `R` 信封）。三种形状要分开处理。

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

## 3. 决定（2026-09-29 定稿，含本轮 Q1–Q18 的结论）

| # | 决定 |
|---|---|
| D1 | **管家端走公司 OAuth2 令牌**：AI-Ops 新增 `CompanyTokenCallerResolver`，适配**公司** `/auth/oauth/check_token`（校验落在公司权威那一跳）。客户端的 `app:3rd_session:` 直读路径**本轮保留**，标注为待收敛。 |
| D2 | **复用公司已有的令牌校验能力**（Q7=a）：调 `/oauth/check_token`，不自实现验签、不复制密钥、不靠「能以令牌读到对象」当校验。 |
| D3 | **目标态：AI-Ops 挂在 `cloud-gateway` 之后**（§1.3）。拓扑变更**本轮不做**，作为独立第二段（见 D7、§3.2）。 |
| D4 | **客户端侧直读 `app:3rd_session:` 记为已知偏离**（Q9=A）：与 ADR-0008 同源，收敛方向一致；客户端改 BFF（ADR-0004 落地）**另立后续票**。 |
| D5 | **身份与数据范围的一切来源改走公司体系**（Q8=A、Q12=A）：B 端主体直接取令牌里的 `id`；数据范围用令牌里的 `shop_ids` 算站点集合；**不再**调 `/shopuser/getShops`、**不再**调 `/user/inside/*`（后者实测**无鉴权**，见 §2.3）。 |
| D6 | 旧 PR #439 关闭；其事实并入本文与交接文档，重开一次 PR（#440）。 |
| D7 | **两段实施**（Q15=A）：第一段 = AI-Ops 侧（新解析器 + 来源密钥校验 + 分派 + 单测 + 文档），**合入即生效但默认关闭**；第二段 = 网络/路由/Nginx，独立一票、逐环境做、先 41。 |
| D8 | **入站信任用「来源 + 共享密钥」**（Q17=B）：不靠「绑定容器网可达地址」的网络隔离（网关容器所在的那条网桥上还有公司数十个服务的容器，等于把信任降到多租网络）；来源密钥由**网关那一跳**注入，前端不持有。 |
| D9 | **两条信任模式按来源密钥分派**（Q18=a）：带且验过来源密钥 → 走 OAuth2 解析器；否则走既有链。判据是调用方**无法自报**的东西（#444 已实现：`caller_auth.SourceKeyCallerResolver` + `X-AIOps-Source-Key`）。 |

### 3.1 第一段（AI-Ops 侧）的交付边界

- `CompanyTokenCallerResolver`：⚠️ 公司 `/oauth/check_token` **成功时**返回的是**框架组装的身份
  映射**（框架字段 `username`/`authorities`/`scope`/`exp`/`jti`（`resourceIds` 非空时 `aud`）
  + 增强器字段 `id`/`user_id`/`username`/`organ_id`/`type`/`tenant_id`/`system_id`/`shop_id`/
  `license`/`tenant_ids`/`shop_ids` 合并、同名覆盖，再补 `active`/`client_id`），**没有**
  `code`/`data` 信封；**失败时**却是**有信封**的
  `{"code":<码>,"msg":"token无效","data":null}`（见 §2.1）—— 与 RFC 7662 不同（自省体是
  `active`+`sub`+`aud`+`scope`+`data_scope`），因此仓里现有的
  `IntrospectionCallerResolver` **不能复用**，要另写一层适配；失败一律 fail closed。
  ⚠️ **`active` 不是判据**：公司确实返回它（Spring `CheckTokenAccessTokenConverter` 无条件
  `put("active", true)`），但客户端凭据令牌同样 `active: true`，而且同样带
  `username`/`client_id`/`exp`/`scope`/`authorities` —— 形状判据必须窄到**增强器写的那组字段**
  （`id` + `tenant_id`），否则会给一条没有身份的通路放行。
- 数据范围按 D5；`platform_entry` 仍决定 consumer/operator 分流（#436 那条**不许动**）。
- 来源密钥：**未配置则该路径整体不启用**（fail closed，不是放行）。✅ #444 已实现。
- 分派接进 `_caller_resolver`。⚠️ **原文写的是「排在既有两个 resolver 之后」，实现时改成了
  「排在会话那一级之前」** —— 后者才是那条边界（§3.3 第 2 条）的落点：会话那一级会直接
  `return`，门若在它之后，41 现网下新路径不可达。既有链内部的选择结果不变。
- 四个新配置键（check_token URL / client 凭据 / 来源密钥）**全部缺省为空 ⇒ 行为与今天逐字一致**，
  这是「先合不启用」的机制保证，清空密钥即回滚。✅ #444 已实现。
- 授权相关的分支做**变异测试**（改坏一处必须转红）。✅ #444 的 8 条变异逐条转红。

### 3.2 第二段（网络与路由）的顺序

1. 拿到 `sys_oauth_client` 那一行（业务侧依赖，**唯一的跨团队前置**）。
2. AI-Ops 绑定变更 —— **一条 exposure 决定**，按 D8 必须同时具备来源密钥才允许。
3. Nacos `dynamic_routes` 新增一条（`DynamicRouteInit` 带监听器，**无需重启**；现有
   35 条里 `das-front` 是「HTTP 上游 + 两个 HeadFilter + RewritePath」的现成样板）。
   **爆炸半径是那 35 条路由**，由 infra owner 执行，先在 41 单独做。
4. Nginx `/v1/` 改向网关，并由它**覆盖式注入**来源密钥（不是新增一个能被客户端伪造的头）。
5. 端到端验收：管家端真实登录 → 动作列表 2 条 → 订单检测（站点外 `404` / 站点内 `202`）；
   客户端**回归**通过（证明未被波及）。

**为什么必须先做第一段**：第二段每一件都有外部依赖与 exposure 决定；第一段的代码在被启用前
对现网零影响，但「AI-Ops 认公司令牌」这一层**不管将来挂不挂网关都需要**。

### 3.3 第一段的交付状态（2026-09-29）

| 票 | 内容 | 状态 |
|---|---|---|
| #442 | 抽出「店铺集合 → 运营商站点范围」的共享解析 `operator_site_scope_from_shops` | 已合入 |
| #443 | `company_token_auth.py`：公司令牌 → 身份 + 站点范围；`_caller_resolver` 里配置门控接线 | 已交付（见下） |
| #444 | 入站来源密钥与按密钥分派（**启用门**） | 已合入（见下） |

**#443 的两条边界**（写在这里是为了不让后来者把它读成「管家端已经能用」；两条都在 #444 里被处理，
保留原文以便追溯当时的判断）：

1. **入口未装门**。来源密钥与分派在 #444；在那之前，任何走到新解析器的请求只是「带了公司
   令牌」，没有「来自网关那一跳」的判据。因此 #443 的启用前提是**网络层尚未暴露这条路径**
   （第二段）。
2. **41 现网配置下不可达**：`_caller_resolver` 的第一级是
   `if settings.third_session_service_token:` 且**直接 return**，而 41 上那个键有值 ⇒ 新分支
   走不到。这是「合入不启用」的最强形式。**#444 必须把来源密钥这道门放在会话那一级之前**，
   否则带令牌、不带 `third-session` 的请求会在第一级就被 `401` 掉。

**写给 #444 的三条**（前两条是本票实现出来的，第三条是复核时补的；#444 已逐条关闭，见 §3.4）：

1. **门要在会话那一级之前**（见上第 2 条），否则新路径在 41 现网不可达。
2. **「令牌无效」也返回 HTTP 200**，因此 #444 里任何「404/401 就拒绝、其余放行」的写法都会把
   一条无效令牌静默读成有效 —— 判据只能在**公司信封**上。本票的实现已经是这样，但 #444 装门时
   若写成「先按状态判、失败再落回既有链」，就会退化成放行。
3. **客户端 Basic 凭据不对是一种不同的形状**：`checkTokenAccess("isAuthenticated()")` 返回
   **HTTP 401 + OAuth2 标准错误 JSON**（不是 `R` 信封）。本票把它映射成「AI-Ops 侧凭据问题」
   （可重试的 `ACCESS_TOKEN_VALIDATION_UNAVAILABLE`），#444 不要把它并回「用户令牌无效」。

**#443 的 URL 校验与已知地址冲突**：新配置键沿用仓库既有规则（远程必须 HTTPS，loopback 除外，
见 `caller_auth.IntrospectionSettings.validate` / `gateway_config.canonical_gateway_url`），
因此 §4.1 里那个 `http://192.168.1.44:30899/...` **会被拒绝**。这是有意的：非 loopback 明文
发送的是**用户令牌与客户端密钥**，而它同时是内网地址、也会出现在 URL 日志里。启用时（第二段）
应配公司网关侧的域名 URL —— 若届时实测确认只能走该内网地址，那是一次**显式的安全决定**
（放宽哪一条、为什么），不是实现细节，需要在此处记录后再改。

---

## 4. 未决（不猜，逐条列出）

1. **`/oauth/check_token` 是不是「服务可调用」的形态** —— （原条目问的是「用哪一行凭据」，
   2026-09-29 实测后**问题本身改了形状**）：`sys_oauth_client` 里 **7 个客户端的 `client_secret`
   都等于它自己的 `client_id`**（`admin`/`admin` …）。这不是「有凭据可取」而是**凭据即公开值**；
   而带上它请求 `/auth/oauth/check_token`，与**不带**凭据得到**逐字相同**的
   `{"code":1,"msg":"Full authentication is required to access this resource"}` —— 请求没有走到
   `client_details` 认证那一步就被拒。另一条入口 `/auth/oauth/token` 返回
   `{"code":1,"msg":"验证码不能为空"}`（与管家端登录同一条被验证码拦住的链）。
   ⇒ **答案不是「用哪个 client」，而是「公司侧要怎么让一个服务调用这条路径」**：要么建一个真正的
   客户端（`id == secret` 不算），要么公司另有服务间校验入口。**需要公司侧/接口人答复。**
   **这是 AI-Ops 侧第一段之外唯一的跨团队阻塞**；在那之前 #443/#444 的代码保持「实现已合入、
   依赖未就绪」。（源码层面的一个候选解释，**未证实**：`cloud-auth` 的 `WebSecurityConfigurer`
   注册 `PasswordEncoderFactories.createDelegatingPasswordEncoder()`，要求 `{bcrypt}` 之类前缀，
   与库里的字面量 secret 匹配不上 —— 与观测一致，但这是解释候选，不是结论。）
2. ~~**管家端请求在 41 上是否已经过 `cloud-gateway`**~~ —— **已解（2026-09-29 实测，由 #448 记录）**：
   管家端 App 打的是 `api.mall.qushiyun.com`，其路径前缀（`upms`/`das`/`mall`/`charging-pile`/
   `mallapi`）**全部命中 Nginx 那条 `→ upstream back_server`（＝公司网关）**。也就是说管家端流量
   **今天已经在网关之后**；被单独摘出来直连 AI-Ops 的只有 `/v1/` 一个前缀。因此它不是「新开一条
   路由」，而是「让 `/v1/` 也走那条已经在走的路」。
3. **`client-type` 的对齐**：公司后端用 `admin` / `supply-admin` / `tenant-app` 决定是否隔离；
   AI-Ops 现在不读它。管家端产物实测会发 `admin` / `tenant-app`（另有 `H5` / `H5-WX` / `APP` / `"1"` 等
   其它取值），需要确认哪一个才是管家端**真实请求**带的值。
4. **`X-Business-Entry` 的最终归属**：若 AI-Ops 真的移到网关之后，这个头应由谁来发、
   公司与 AI-Ops 之间是否需要新增一条路由（`/aiops/v1/*` 还是继续占用 `/v1/`）。
5. **客户端 App 里 AI-Ops 请求的鉴权方式**：`aiPackage` 只发 `third-session`、不带
   `Authorization`；Nginx 补的是 **AI-Ops 自己的服务令牌**，不是公司令牌。
   这条「客户端不经过公司网关」的现状是否需要一并收敛，取决于 D3 的落地方式。

---

## 4.1 未决三条的「不是未决」

- **令牌里的 `shop_ids` 能拿到吗** —— 能，但**不是**从网关注入头拿：`AdminProxyHeadFilter`
  只注入 `user-id`/`admin-id`/`tenant-id`/`site`，**不转发** `shop_ids`。
  所以 D5 的「用令牌里的 `shop_ids`」隐含**AI-Ops 自己调 `check_token`**（D2），
  二者是同一件事的两面。（实测：41 本机 Redis **没有** `base_oauth:*`，
  令牌存储不在 AI-Ops 够得到的那台上，因此不能靠「自己读 Redis」省掉这一跳。）
- **`/oauth/check_token` 是否可达** —— **网络可达，但「服务可调用」未成立**（这正是上面 §4 第 1 条
  改形状的原因）：无凭据、带 `admin:admin`、带错凭据三种请求得到**同一句话**。原先据「无凭据也返回
  这句话」推断「只差凭据」是把「可达」当成了「可用」。
- **网关路由好不好加** —— 好加，且**无需重启**（Nacos `dataId=dynamic_routes` + `DynamicRouteInit` 监听器）；
  难的是前置（凭据）与爆炸半径（35 条），不在配置本身。

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

### 3.4 第一段的交付状态（2026-09-29，#444 合入后）

| 票 | 内容 | 状态 |
|---|---|---|
| #442 | 抽出「店铺集合 → 运营商站点范围」的共享解析 `operator_site_scope_from_shops` | 已合入 |
| #443 | `company_token_auth.py`：公司令牌 → 身份 + 站点范围；`_caller_resolver` 里配置门控接线 | 已交付 |
| #444 | 入站来源密钥与按密钥分派（**启用门**） | 已交付 |

**#444 装上了什么**（第一段的 AI-Ops 侧到此完整）：

- `caller_auth.SourceKeyCallerResolver`：按 `X-AIOps-Source-Key` 在两条信任模式间分派。
  命中 ⇒ 公司令牌解析器（且不把会话值递过去：两种凭据不是一回事）；否则原样委派给既有链
  （会话值、业务入口都原样透传，既有链的解析结果零变化）。
- **门在会话那一级之前**：`_caller_resolver` 里 `company_source_key` 有值时先构造这一层。
  这关掉了 §3.3 第 2 条那条边界 —— 41 现网配置下，带正确密钥的请求现在走得到新链路。
- **未配置密钥 ⇒ 整层不构造**（fail closed）。只配密钥不配校验入口、或密钥短于 16 字符，
  都是**启动失败**（前者是装了门没有目标、每条带密钥的请求静默落回既有链；后者等于没有门）。
- 端到端用例**由真实配置链构造解析器**（不注入替身）：注入式用例对「header 有没有被传到门」
  是盲的 —— 首轮变异 M5 就是这样漏过去的，补了第二个入口后才转红。

**#444 之后的边界（这些仍然成立，不得读成「管家端可用」）**：

1. **第二段仍未做**：Nacos 路由、Nginx 改向、绑定变更、`sys_oauth_client` 凭据 —— 41 上这条
   路径**仍不可达**，管家端前端今天的 `401` 一个字未变。
2. **来源密钥由谁注入还没有落地主体**：设计是「可信的那一跳」（未来是公司网关），第二段才把它
   接上。在那之前，任何直接可达这条入口的调用方**只要能拿到密钥**就能走新链路 —— 所以密钥的
   分发与注入本身就是第二段的安全动作，不是配置细节。
3. **分派失败不单独记日志**（#444 已知缺口）：密钥抄错/没带，在生产日志里与「既有链拿到一条
   不认识的令牌」是同一条 401。
