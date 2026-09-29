# 管家端身份改由公司 OAuth2 令牌断言

Status: Accepted

## 背景

管家端（`operator` 内容域）的入口要做起来时，AI-Ops 手里其实**只有一条**凭据路径：
共享会话 Redis 直读 `app:3rd_session:<token>`。而管家端 App 登录走的是公司
`/upms/token/login` 的 OAuth2 令牌（存 `CLOUD_ACCESS_TOKEN`），**不写这个会话** ——
前端照现状接必然 `401`（41 实测：OAuth2 令牌 / 裸 uuid / 整键名三种发法全部 `401`）。

追下去发现这不是「少接了一处」，而是**站点位置**：41 上 `location ^~ /v1/` 写在公司网关
前面，AI-Ops 从未经过 `cloud-gateway`，于是公司网关本来就有的那条注入路径
（`AdminProxyHeadFilter`：`client-type: admin` + OAuth2 令牌 → 注入身份头）一次都没被用上。
完整证据见 [docs/agents/company-platform-integration-baseline.md](../agents/company-platform-integration-baseline.md)。

## 决定

**AI-Ops 接受公司签发的 OAuth2 访问令牌作为管家端的身份断言**，经公司权威的这一跳校验：

1. **校验走 `/auth/oauth/check_token`**（公司资源服务器 `RemoteTokenServices` 用的同一入口），
   不自己验签、不复制密钥、不把「能以令牌读到 Redis 对象」当作「令牌有效」。
2. **身份与范围直接取自令牌**：`id` = B 端 `sys_user.id`（管家端授权的起点）、
   `user_id` = C 端、`tenant_id`、`shop_ids`。
   **不再调** `/user/inside/byUserId`（C→B 补全）与 `/shopuser/getShops`（店铺集合）——
   令牌已经带着这两件事的答案。
3. **入站信任 = 来源 + 共享密钥**：来源密钥由网关那一跳注入，**前端不持有**（实现为请求头
   `X-AIOps-Source-Key`，校验在 `caller_auth.SourceKeyCallerResolver`，**装在会话那一级之前**
   —— 会话那一级会直接返回，门在它之后则现网不可达）；
   未配置密钥时该路径**整体不启用**（fail closed，不是放行）。
   两条信任模式按**来源密钥**分派（调用方无法自报）。
4. **客户端的 `app:3rd_session:` 直读路径本轮保留**，标注为待收敛；
   它改走 BFF 是 ADR-0004 的落地，另立一票。

## Consequences

- **少了两条依赖，且其中一条本来就没有鉴权。** `/user/inside/*` 与 `/user/ds` 的
  `@Inside` 校验在公司侧**整段被注释掉**，且被 `PermitAllUrlProperties` 加进 `permitAll`
  —— 即**无凭据可达**。取消这两跳，等于同时取消了「依赖一组实际无鉴权的内部端点」
  这一条隐性成本。授权链的信任边界随之从「网络可达性」回到「被校验过的令牌」。

- **范围语义从实时改为快照。** `/shopuser/getShops` 是实时的（公司 `ShopIdInterceptor`
  在 `realTime=true` 时每查一次拉一次）；令牌里的 `shop_ids` 是**签发时刻**的快照。
  staleness 窗口 = 令牌有效期。**这是本决定明确接受的代价**：运营商的店铺绑定变更是
  低频事件，而少一跳、少一个无鉴权依赖是持续收益。若将来实测到「变更后令牌未过期期间
  的越权查询」成为真实问题，再回到实时查询——那是加一跳，不是改模型。

- **令牌里的字段名是**snake_case**（`user_id` / `shop_ids`），与公司另一处 API 的
  camelCase 不一致** —— 适配器必须按公司那一份契约解析，不能照抄本仓既有的
  `IntrospectionCallerResolver`（RFC 7662 形状：身份在 `sub` + `aud` + `data_scope` 里，
  而公司这份身份在**增强器**注入的 `id`/`tenant_id` 里）。两处形状相同是巧合，不是可复用。
  ⚠️ 公司**也**返回 `active`/`scope`（框架字段），所以「有没有 `active`」不构成差异，
  **形状判据必须窄到增强器那组字段** —— `username`/`client_id`/`exp`/`scope`/`authorities`
  对客户端凭据令牌同样存在。判定还只能写在**信封**上：无效令牌也返回 HTTP 200
  （`{"code":<码>,"msg":"token无效","data":null}`）。实现与依据见
  [`company_token_auth.py`](../src/aiops_diagnostics/company_token_auth.py) 与
  [company-platform-integration-baseline.md](../agents/company-platform-integration-baseline.md) §2.1。

- **本决定不改变 ADR-0003 / ADR-0008 的状态。** 委托句柄仍未实现、客户端仍走共享会话直读；
  本条只把**管家端**那一条支路的身份来源换成公司权威校验。ADR-0008 里
  「不得把直接读会话写成 ADR-0003 的实现」这条规则继续有效。

- **收敛方向不变，且现在更近一步。** 终局是 AI-Ops 挂在 `cloud-gateway` 之后
  （见基线文档 D3/D7–D9）。本决定让「AI-Ops 认公司令牌」这一层先落地 ——
  **不管将来挂不挂网关，这一层都需要**；挂上去之后省掉的是「谁去注入来源密钥」，
  而不是这套解析。

## 被否掉的替代形态

- **（a）让 BFF 再签发一条 `app:3rd_session:*`** —— 复用了现有解析器，零代码改动；
  但它是在给一个**已经存在**的注入路径造第二条，等于把根因（站错位置）固化下来。
  已在 2026-09-29 被本条取代。
- **（b）AI-Ops 自己读 `base_oauth:access:<token>`（照抄 `AdminProxyHeadFilter`）** ——
  零新增凭据，但 41 本机 Redis 实测**没有** `base_oauth:*`（令牌存储不在同一台），
  而且它把「读到对象」当成「校验过」——正是 ADR-0008 已经吃过一次的亏。
- **（c）只收网关注入头，不做令牌校验** —— 少一层网络依赖，但身份变成「谁都能伪造的头」，
  与 ADR-0003「裸身份字段不能作为授权依据」直接冲突。
- **（d）靠绑定容器网可达地址、以网络隔离当作身份边界** —— 网关容器所在的那条
  容器网桥上还有公司数十个服务的容器；
  等于把信任降到多租网络。已按 D8 用「来源 + 共享密钥」替代。
