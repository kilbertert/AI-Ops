# 管家端 + 订单授权：待外部确认的问题清单

> **用途**：把「另起管家端智能体（快捷指令：订单检测、客户案例）+ 订单查询加一层
> 运营商/平台权限判断」这个需求，落到**必须由产品 / 后端 / DBA 回答才能实施**的问题上。
> 每条都带代码或生产库证据，可直接转发。
>
> **证据来源**：① 本仓 `src/aiops_diagnostics/` 源码（行号已核）；② 2026-09-24 对 41
> 生产库（MySQL `192.168.1.45`，只读）的 `information_schema` 元数据与 ID 交集探查。
> **未读业务行，未打印凭据**。
>
> **状态**：2026-09-24 **大幅修订**。P0-1 / P0-2 / P0-3 三条原标记为"阻塞"，经**通读公司
> GitLab 源码**（`git.qushiyun.com:801`，见 §5）后**已由源码回答**，不再是需要问同事的问题。
> 仍真正需要人工确认的已收敛到 §2 末尾。**请勿按本文件旧版本去找同事核对已被源码回答的部分。**
> 后续基线任务见 **#414**。

---

## 5. 源码已答（2026-09-24 补充，取代原 P0-1/P0-2/P0-3）

**方法**：公司 GitLab 可读（`~/.git-credentials` 的 token，519 个可见项目）。**API 优先、
不 clone**；关键仓 `iot/cloud-charging-pile`（真码在 `release` 分支，**默认分支 `master` 只有
`Demo*.java` 脚手架**）、`s2b2c-java/qumall-common`、`s2b2c-java/cloud-upms`。

### 5.1 「平台」= shop id 的哨兵值 `'-1'`（原 P0-1）

不是缺失字段，是**约定**：

```java
// qumall-common/cloud-common-data/.../datascope/shop/ShopIdInterceptor.java
// seePlatform=true 时：
originalSql = "... where " + scopeName + " IN ('" + shopId + "', '-1')";
```

`@ShopDataScope.seePlatform()` 的文档注释即「**是否平台**」（`ShopDataScope.java`）。

### 5.2 「运营商」= `partner_info`，桥是 `agent_id ≡ partner_info.owner`（原 P0-2）

```java
// iot/cloud-charging-pile .../ChOrderInfoController.java:736, :1599
//                              .../HlhtOrderStatisticsController.java:104
orderInfoModel.setAgentId(partnerInfo.getOwner());
// ChOrderInfo.partnerWalletId 原始注释：「运营商车队钱包对应的商城代理商id」
// EasyChargeAuthorityService.java:245
chOrderInfo.setPartnerBId(chSite.getPartnerBId());
```

`ChSite` 字段（源码 javadoc）：`agentId`=「代理商id」、`partnerBId`=「代理商B端账户id」、
`owner`=「店铺管理员id」、`hlhtId`=「互联互通渠道Id」。
`HtChannelInfo.operatorId`=「互联互通**平台**标识」（即 §5.1 之外的另一种"平台"用法）。

### 5.3 后端**已有**订单授权实现（这是最重要的一条）

```java
// data/mapper/ChOrderInfoMapper.java:59   @ShopDataScope(column = "site_id", realTime = true)
//                          :221            @ShopDataScope(column = "o.site_id", montage = true)
// data/mapper/ChSiteMapper.java:44,70,77  @ShopDataScope(column = "id"/"siteId", montage = true, realTime = true)
```

`ShopIdInterceptor.beforeQuery` 用 jsqlparser 改写 SQL 追加 `scopeName IN (…)`；
集合来自 `UpmsAdminFeignClient.getShops(userId)` = **`GET /shopuser/getShops`**（`realTime=true` 时实时取）。
调用者侧有现成函数 `UserInfoUtils.isAgent()` / `getAgentOwner()` / `getSiteIdsByUid(uid)`。

**两道硬门**（决定 AI-Ops 能不能用上）：

1. `ShopIdInterceptor.judge()` 要求请求头 **`client-type ∈ {admin, supply-admin, tenant-app}`**，
   否则**完全不隔离、直接 return**；
2. **`DataScopeInterceptor`（组织级）整个方法体被注释掉**，是死代码——这解释了探库时看到的
   「`organ_ids` 从不下推」。

### 5.4 因此：我们的"缺口"重新定义

**不是"没有授权"，是"AI-Ops 绕开了它"**：

```
业务路径：  前端 → cloud-gateway → cloud-charging-pile   （@ShopDataScope 生效）
我们的路径：前端 → AI-Ops 网关 → 直连 MySQL / /diag/*    （只验 X-Internal-Token，无数据范围）
```

AI-Ops 是外挂的第三条数据路径。需求真正的意思是**把 AI-Ops 的订单访问接回公司既有授权语义**。
**已定方向（Q34=A）**：调一个施加了 `@ShopDataScope` 且接受 `client-type` 头的 Java 接口，
**复用公司授权，不重写一份**。

---

## 2'. 仍真正需要确认的（收敛后）

| # | 问题 | 状态 |
|---|---|---|
| ~~R-1~~ | ~~`partner_b_id` 对哪个注册表？~~ | ✅ **已解（源码 + 生产库实测双证）**：`ch_site.partner_b_id` ≡ `partner_info.owner` ≡ **`qumall_upms.sys_user.id`（`type='5'` 代理商）**。端到端覆盖率 **68.3%（20782/30443）**，不是先前误算的 9.4%——错因是我按 `partner_info.id` 做了 join。 |
| **R-2** | AI-Ops 走哪个后端接口？现在有没有一个**接受 `client-type` 且暴露订单查询**的接口可复用？ | 需要与后端确认是**复用现有接口**还是**新开一个**，以及 AI-Ops 的调用身份怎么映射成 `clientType` |
| **R-3** | `seePlatform=true` 的语义：平台方（`'-1'`）**是否应该**看到所有运营商的订单？ | **业务规则**，不是代码事实。**需要产品确认** |
| **R-4** | 「统一案例库」用哪个知识库、是否允许跨租户读 | 业务决策（见 P1-6） |
| **R-5** | 管家端登录的账号类型 → 映射成哪个 `clientType`（`admin`/`tenant-app`/`supply-admin`） | 需要业务+后端共同确认 |
| **R-6** | 未覆盖的 ~31.7% 订单（站点无 `partner_b_id`）是**自有站点（正常）**还是缺数据？ | 需业务确认；若是自有站点，则"无代理商→按租户/平台放行"是正确规则而非漏洞 |

### 5.5 R-1 追查结论（新增，取代"待问同事"）

**`ch_site.partner_b_id` 指向 `qumall_upms.sys_user.id`，且该行 `type='5'`（代理商账号）。**

三条独立证据：

1. **源码成对写入**（`ChSiteServiceImpl.java:494-495`）——同一次调用里两个字段取自
   `partner_info` 的**不同列**，直接证明是两个 id 空间：
   ```java
   chSite.setAgentId(partnerInfo.getId());       // agent_id     ← partner_info.id
   chSite.setPartnerBId(partnerInfo.getOwner()); // partner_b_id ← partner_info.owner
   ```
2. **写入落点**（`MallDataMapper.xml:87`）：`insert into qumall_upms.sys_user_shop(..., user_id, ...) value ('0',#{partnerBId},...)`；
   UPMS 定义 `sys_user_shop.user_id = sys_user.id where type='5'`（`SysUserMapper.xml:428-437`）。
3. **JOIN 路径**（`ChOrderInfoMapper.xml:159-161`）：`coi.partner_b_id = pi.owner`。

**生产库实测（2026-09-24，41 → `192.168.1.45`，只读元数据/基数）**：

| 链路 | 覆盖订单 | 占比 |
|---|---|---|
| `site.partner_b_id → sys_user(type=5)` | **20,782** | **68.3%** |
| `site.partner_b_id → partner_info.owner` | 21,130 | 69.4% |
| `site.agent_id → partner_info.id` | 2,875 | 9.4% |

`sys_user.type` 实测：`-1:35, 1:452, 2:540, 3:154, 5:314, 6:4, 7:1, 8:2, 9:132`——
**代理商（type=5）314 个账号**；`partner_info.owner ∩ sys_user` = 312（type=5 有 307）→ **运营商身份可绑**。

**`partner_b_id` 的消费方**（供实现参考）：`ChOrderInfoMapper.xml:151-166` 的传化订单推送
显式 JOIN：`coi.partner_b_id = pi.owner`（同一 UNION 里出现三次）。


---

## 0. 一句话背景

需求是两件事：

1. **入口**：管家端（`X-Business-Entry: operator`）要能列出两个快捷指令——「订单检测」「客户案例」；
2. **授权**：订单查询要加一层判断，**「该订单的站点的运营商 id 和平台 id 都能有权限查询」**。

第 2 件是核心。经排查：**这两个字段在当前数据模型里都没有现成对应**，且调用者侧
也没有运营商/平台身份。下面按「已确证事实 → 待答问题」展开。

---

## 1. 已确证的代码事实（不需要问，但决定方案形状）

| # | 事实 | 证据 |
|---|---|---|
| C1 | 现有订单授权**只有租户一层** | `caller_auth.py:227-230` `can_access` → `get_orders(order_no)`；谓词由 `order_visibility.py:232-240` 渲染为 `tenant_id=?` / `tenant_id IN (…)` / `1=0` |
| C2 | 显式传 `order_no` 时，无权限返回 **404 `ORDER_NOT_FOUND`**，授权服务故障返回 **503** | `gateway_api.py:819-834` |
| C3 | 问句**文本里内嵌**订单号时，无权限**静默回落**零阶问答（已发布契约，有测试固化） | `gateway_api.py:862-903`；`tests/test_assistant_api.py:595-606`；`docs/validation.md:1420` |
| C4 | 会话内**活跃订单**的追问，无权限时静默回落并清掉绑定 | `gateway_api.py:912-957` |
| C5 | 前端会话（`third-session`）的 `data_scope` **硬编码为 `self`**，从不调 UPMS `/user/ds` | `third_session_auth.py:82` |
| C6 | 但 `self` **确实生效**：下推为 `user_id` 谓词 → 前端会话查订单**已按「本人」过滤** | `query_scope.py:232-240` → `sources.py:217,233-235` → `sources.py:286`，SQL `… AND user_id=?` |
| C7 | 订单路由路径（`diagnostic_tools.py`）调用同一构造函数时**不带** `user_id`，与 C6 口径不一致 | `diagnostic_tools.py` vs `sources.py:286` |
| C8 | 我方订单投影 **不含** `operator_id`（要用必须先加投影列） | `sources.py:105-123` `ORDER_COLUMNS` |
| C9 | 「订单检测」**不能绑智能体**：`target_agent_version` 只允许宣传类 intent | `shortcut_lifecycle.py:1022-1037` |
| C10 | 「客户案例」的智能体绑定**强校验租户匹配**，跨租户解析失败且不回退 | `promo_agents.py:80-108` |
| C11 | 41 上 `operator` 入口至今 **503 `PLATFORM_UNAVAILABLE`**（无唯一 B 端主体） | `docs/validation.md:1444` |

---

## 2. 待答问题

### P0（~~不答就无法出实现方案~~ → **已由源码回答，见 §5；保留原文以存证**）

#### P0-1 「平台」在这个业务里到底指什么？

**已查到的**：充电库 `cloud_charging_pile` 里**没有 `platform_id`**（全库零命中）。
`ch_site` 的 **90 列**中也没有 `operator_id`/`platform_id`，只有 `agent_id`（代理商id）、
`partner_b_id`（代理商B端账户id）、`hlht_id`（互联互通渠道id）、`use_platform_template`（布尔开关）。

UPMS（`qumall_upms`）里**有**平台概念但是**退化的**：

```
sys_organ.parent_id  注释「为0时是租户，-1时是平台系统管理」  → 318 租户 / 1 平台
sys_organ.platform_code                                      → 只有 1 个取值
sys_organ.saas_type  注释「0.平台 1.商城…」                    → 349 行全是 1（商城）
```

**需要回答**：
1. 需求里「**平台 id**」指的是哪一套？
   - (a) UPMS 的 `sys_organ` 平台层（但现在只有一个平台，等于没有区分度）；
   - (b) 某个**业务侧**的平台概念（如 SaaS 运营方 / 渠道方）；
   - (c) 其实和「运营商」同义，只是叫法不同。
2. 如果确实是 (a)：**一个运营商会不会同时属于多个平台？** 这决定判据是「或」还是包含关系。

> **为什么阻塞**：没有「平台」这个可比的 id，「平台 id 有权限查询」这半句无法写成任何
> SQL 或判定逻辑。

#### P0-2 「运营商」= `qumall_mall.partner_info`（代理商）吗？

**线索**：产品答「管家端登录的是**代理商、平台**」。而 `qumall_mall.partner_info`
的**表注释就是「运营商」**（419 行），字段形态吻合：

| 字段 | 注释 |
|---|---|
| `owner` | **b端账号id** |
| `group_header` | **c端账号id** |
| `shop_id` | 关联店铺 |
| `parent_id` | **上级代理id**（有层级） |
| `type` | 0-区域 / 1-线上 |
| `agent_level_id` | 代理等级 |

**但覆盖率只有 9.4%**，这是最要命的一条：

```
订单 → 站点 → 运营商(partner_info)
    2875 / 30443 单  = 9.4%
```

原因是**一个 id 空间的死结**：

| `ch_site` 上的列 | 覆盖订单 | 与 `partner_info.id` 交集 |
|---|---|---|
| `agent_id`（代理商id） | 2,875 | **88 ✅ 连通** |
| `partner_b_id`（代理商B端账户id） | **21,131（69%）** | **2 ❌ 不同 id 空间** |
| `ch_order_info.partner_b_id` | 14,357 | **0 ❌** |

**需要回答**：
1. 「运营商」是不是就是 `partner_info` 这个实体？**请给一个是/否。**
2. **`partner_b_id` 该对到哪个注册表？** 它是「代理商**B端账户**id」，和
   `partner_info.id` 不是一套。它在哪个表？这个问题答了，覆盖率从 9.4% 变成 69% 量级。
3. `ch_site.agent_id` 与 `partner_info.id` 同源（ID 形态一致，19 位雪花，交集 88）——
   **它是「代理商」还是别的（区域代理？渠道商？）**？两个字段口径不同是否是历史遗留？

> **为什么阻塞**：不确认实体，判据写不出；不解决 `partner_b_id`，90.6% 的订单判不出
> 归属——**在生产上等于随机拒绝用户**。

#### P0-3 调用者侧：运营商身份从哪来？

**已查到的**：前端会话**没有**运营商身份（`data_scope` 硬编码 `self`，C5）。
可用的绑定键是 `partner_info.owner` / `group_header`，但它们是**账号级**字段，
**不在 UPMS `/user/ds` 的返回里**（该接口只给 `organIds/shopIds/siteIds`）。

UPMS 里另有一批名字很像的字段，**全部对不上**：

```
sys_user.partner_id（注释「所属运营商员工」，仅 3 行有值） ∩ partner_info.id = 0
sys_user.belong_distributor_id / distributor_flag（0 行）              = 0
sys_organ.distributor_id                                              = 0
```

**需要回答**：
1. **代理商账号登录管家端时，系统是怎么知道"这个账号属于哪个运营商"的？**
   是 `partner_info.owner`/`group_header`，还是别的表/字段，还是登录时算出来的？
   有没有现成接口能问「当前登录用户的运营商 id / 其可见站点集合」？
2. 如果**后端已有这样一个权限接口**，请给出路径与字段——**我们复用它，而不是自建一套**。

> **为什么阻塞**：调用者侧集合拿不到，判据没有左操作数。

---

### P1（答了才能确定方案边界）

#### P1-4 覆盖率不足时怎么处置？

9.4% 能判定，90.6% 判不出。三种处置，**需要业务方拍板**：

- (a) 判不出就**拒绝** → 90.6% 的订单管家端查不了；
- (b) **先补数据**再上线（推荐）；
- (c) 判不出时**放宽到租户级**（不推荐：等于门形同虚设，且与需求意图相反）。

**需要回答**：管家端日常实际要查的订单，落在哪一类？如果绝大多数落在 90.6% 里，那么
**这个问题必须在实现之前解决，而不是作为上线后的已知限制**。

#### P1-5 运营商之间的**层级**关系怎么算？

`partner_info.parent_id`（上级代理id）说明代理商**有层级**（区域代理 → 下级）。

**需要回答**：上级代理能不能看下级代理的订单？如果能，要展开几层（`partner_info`
里有没有像 `sys_organ_relation` 那样的层级闭包表，还是递归 `parent_id`）？

#### P1-6 「统一案例库」用哪一个、跨租户读是否被允许

产品要「客户案例」用**统一案例库**。现状：智能体绑定**强校验租户**
（C10），跨租户会解析失败且不回退；已有素材在两个**不同**租户下：

- 租户 `1942105476598861824` 的宣传 KB `8701c742b28111f18637d95f7710e3a3`
- UPMS 真实租户 `1783022023241633792`（小趋充电）的 `canary-media-0911`

**需要回答**：
1. 「统一案例库」**具体是哪一个知识库**（ID）？
2. **允许所有租户的会话读它吗？** 这是比订单授权更大的边界，需要明确授权。

---

### P2（实现细节，答了能少踩坑）

#### P2-7 跨库查询的排序规则冲突

充电库与商城库的字符集排序规则不同，直接 join 会报错：

```
Illegal mix of collations (utf8mb4_0900_ai_ci,IMPLICIT) and (utf8mb4_general_ci,IMPLICIT)
```

**需要回答**：**后端生产代码里是怎么跨这两个库查的？** 是否有现成的 join 或视图
（我们照抄，避免自己发明）？

#### P2-8 `ht_stations_info` 那套（互联互通）要不要纳入

`ht_stations_info` / `sup_stations_info` / `ht_station_log` 有 `operator_id`（注释「运营商id」）
和 `equipment_owner_id`（注释「**设备所属运营平台组织机构代码**」），但只覆盖
**650 / 30443 单**（`ht_stations_info` 仅 41 行）。

**需要回答**：这是**另一套体系**（互联互通渠道对端）吗？管家端**要不要**看到这类订单？
如果要，判定条件是什么？

#### P2-9 现在这个演示租户在真实业务里对应什么

41 上现有 H5 演示会话解析到的租户（`1899282205965029376` / `1783022023241633792`）
**都不是 UPMS 租户**，后台租户切换器选不到。

**需要回答**：它们对应真实业务的**哪一类主体**？这决定"运营商门"在这类会话上应该
放行还是拒绝。

---

## 3. 我方内部事项（不需要外部回答，但需授权/排期）

| # | 事项 | 说明 |
|---|---|---|
| C-1 | **修前端会话的 `data_scope`**（C5） | 让它真的调 UPMS `/user/ds`。**这是唯一能拿到真实组织层级的路**，但改的是**鉴权路径**，需单独评估 + 独立票，不塞进本轮 |
| C-2 | **operator 正向会话**（C11） | 至今拿不到唯一 B 端主体，验收只能做负向（越权必被拒）+ 消费者端回归；正向标 `blocked` |
| C-3 | 收敛 C6/C7 两种订单可见性口径 | 同一仓两种写法，属既有技术债，建议并入 #406 |
| C-4 | 订单投影加 `operator_id`（C8） | 若 P0-2 确认走订单侧，需要先加投影列 |

---

## 4. 结论：需求当前的可行性

| 需求片段 | 可行性 |
|---|---|
| 管家端入口（`operator`）+ 两个快捷指令 | **可落地**（数据发布为主） |
| 「订单检测」 | **可落地**，但**不能绑智能体**（C9），靠 `requires_order` 弹订单选择器 |
| 「客户案例」用统一案例库 | **待 P1-6**：需指定 KB + 跨租户授权 |
| 「订单查询加运营商权限判断」 | **被 P0-1/P0-2/P0-3 阻塞**。判据字段不存在、覆盖率 9.4%、调用者身份拿不到 |
| 现有缺口本身（租户内任何人凭订单号可查） | **真实缺口，且与上述三者独立**——即使 P0 全部悬置，也值得单独评估是否先补一道最小门 |

**最重要的一句**：需求说的「该订单的**站点的**运营商 id 和平台 id」——
`ch_site` 的 90 列里**没有这两列**。这两个属性**都不在站点上**，
一个在商城库的 `partner_info`（9.4% 可连），一个在数据里**无区分度**。
**这个差距不是命名问题，是数据模型问题**，需要业务和数据侧共同确认后才能实施。
