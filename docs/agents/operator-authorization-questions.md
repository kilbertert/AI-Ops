# 管家端 + 订单授权：待外部确认的问题清单

> **用途**：把「另起管家端智能体（快捷指令：订单检测、客户案例）+ 订单查询加一层
> 运营商/平台权限判断」这个需求，落到**必须由产品 / 后端 / DBA 回答才能实施**的问题上。
> 每条都带代码或生产库证据，可直接转发。
>
> **证据来源**：① 本仓 `src/aiops_diagnostics/` 源码（行号已核）；② 2026-09-24 对 41
> 生产库（只读）的 `information_schema` 元数据与 ID 交集探查。**未读业务行，未打印凭据**；
> **本文件不含任何主机地址、凭据位置或连接信息**（本文档会跨团队转发）。
>
> **状态**：2026-09-24 **大幅修订**。P0-1 / P0-2 / P0-3 三条原标记为"阻塞"，经**通读公司
> GitLab 源码**（`git.qushiyun.com:801`，见 §5）后**已由源码回答**，不再是需要问同事的问题。
> 仍真正需要人工确认的已收敛到 §2 末尾。**请勿按本文件旧版本去找同事核对已被源码回答的部分。**
> 后续基线任务见 **#414**。

---

## 5. 源码已答（2026-09-24 补充，取代原 P0-1/P0-2/P0-3）

**方法**：公司 GitLab 可读（519 个可见项目；**凭据由运维侧管理，位置不记入本文档**）。**API 优先、
不 clone**；关键仓 `iot/cloud-charging-pile`（真码在 `release` 分支，**默认分支 `master` 只有
`Demo*.java` 脚手架**）、`s2b2c-java/qumall-common`、`s2b2c-java/cloud-upms`。

### 5.1 「平台」= shop id 的哨兵值 `'-1'`（原 P0-1）

不是缺失字段，是**约定**：当 `seePlatform=true` 时，隔离条件会把 shop id 与**哨兵值 `'-1'`**
一起放进 `IN (...)`——`'-1'` 即"平台方"。出处：`qumall-common/cloud-common-data/.../datascope/shop/ShopIdInterceptor.java`。
`@ShopDataScope.seePlatform()` 的文档注释即「**是否平台**」（`ShopDataScope.java`）。

### 5.2 「运营商」= `partner_info`；`partner_b_id ≡ partner_info.owner`，`agent_id ≡ partner_info.id`（原 P0-2）

**站点侧两列由同一段代码成对写入，取自 `partner_info` 的不同列**——这是最直接的依据：
`cloud-charging-pile .../ChSiteServiceImpl.java:494-495`，`setAgentId(...)` 取 `partner_info.getId()`，
`setPartnerBId(...)` 取 `partner_info.getOwner()`。

**订单侧从站点继承**：`.../EasyChargeAuthorityService.java:245` 把站点的 `partner_b_id`
复制到订单同名列。

> ⚠️ **注意**：另一处 `ChOrderInfoController.java:736,1599` 写的是**订单模型的一个 DTO 字段**
> （取 `partner_info.owner`），**不是 `ch_site.agent_id`**。此前本文档把它当作站点
> `agent_id` 的依据，**是错的**；站点侧的正确依据是上面的 `ChSiteServiceImpl.java:494-495`，
> 且已由生产库实测交叉验证。

`ChSite` 字段（源码 javadoc）：`agentId`=「代理商id」、`partnerBId`=「代理商B端账户id」、
`owner`=「店铺管理员id」、`hlhtId`=「互联互通渠道Id」。
`HtChannelInfo.operatorId`=「互联互通**平台**标识」（即 §5.1 之外的另一种"平台"用法）。

### 5.3 后端**已有**订单授权实现（这是最重要的一条）

注解 `@ShopDataScope` 标注在 Mapper 方法上，把隔离列声明为站点列——
`ChOrderInfoMapper.java:59`（`site_id`, `realTime=true`）、`:221`（`o.site_id`, `montage=true`）、
`ChSiteMapper.java:44,70,77`（`id` / `siteId`）。

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

> ⚠️ **2026-09-24 更正 —— 方向待定，见 §5.8。**
> 本文档一度写「Q34=A：调一个施加了 `@ShopDataScope` 的 Java 接口复用公司授权」。
> 复核后**撤回该结论**，但**原因与我最初写的不一样**：`@ShopDataScope` 的隔离集合本身来自
> UPMS `/shopuser/getShops`（`ShopIdInterceptor` 用调用者会话身份实时取），**它自己是可用**；
> 真正的卡点是**AI-Ops 用什么身份代表调用者**——这个卡点**对两条路同样存在**。
> （另有一条独立发现：UPMS `/user/ds` **结构性为空**，见 §5.8。）
> **R-2 因此未解，但已收敛为一个精确的问题。**

---

## 2'. 仍真正需要确认的（收敛后）

| # | 问题 | 状态 |
|---|---|---|
| ~~R-1~~ | ~~`partner_b_id` 对哪个注册表？~~ | ✅ **已解（源码 + 生产库实测双证）**：`ch_site.partner_b_id` ≡ `partner_info.owner` ≡ **`qumall_upms.sys_user.id`（`type='5'` 代理商）**。端到端覆盖率 **68.3%（20782/30443）**，不是先前误算的 9.4%——错因是我按 `partner_info.id` 做了 join。 |
| ~~R-2~~ | ~~AI-Ops 用什么身份代表调用者？~~ | ✅ **已解（公司源码 + 生产库实测）**：链条为 **会话 C 端 `userId` → `/user/inside/byUserId/{userId}` 拿 B 端 id → `GET /shopuser/getShops?userId=<B端>` → `shop_id` → 既有 `site_ids_by_shops` 映射 → `site_ids` 下推**。**无需新增接口、无需后端改动。** 注意两次纠正：必须走 C→B 映射（两会话 id 空间不同）；`shop_id` 与 `site_id` **近似但不等同**（954 行中 1 行不同），**不可直接互用**（详见 §5.9） |
| ~~R-3~~ | ~~平台方（`'-1'`）是否应看到所有运营商的订单？~~ | ✅ **已关闭（产品口径 + 数据佐证）**：产品答「不会有出现 `-1` 查看数据的场景，不用理会平台」；实测 `ch_site` 无任何 `shop_id='-1'` 行。**该通路当前是空操作。** 注意范围：这关闭的是"**是否需要跨运营商可见性**"。「**租户内的店铺 ID 仍会匹配到站点**」（即普通站点隔离照常生效）不在本次关闭范围内 |
| **R-4** | 「统一案例库」用哪个知识库、是否允许跨租户读 | 业务决策（见 P1-6） |
| **R-5** | 管家端登录的账号类型 → 映射成哪个 `clientType`（`admin`/`tenant-app`/`supply-admin`） | 需要业务+后端共同确认 |
| **R-6** | 未覆盖的 31.7% 由**三种不同成因**组成（见下），其中两种是**数据完整性缺口**而非"自有站点"。规则必须对三种都给出明确行为 | 需业务确认 + 数据侧决定是否修 |

**R-6 的三分解（2026-09-24 实测，B+C+D+E 与总数严格相等）**：

| 成因 | 订单数 | 占比 | 性质 |
|---|---|---|---|
| B. 站点 `partner_b_id` 为空 | 9,307 | 30.6% | **正常**（自有站点/无代理商）——规则应为"无代理商→按租户或平台放行" |
| C. 有值但 `sys_user` 无此行 | **57** | 0.2% | ⚠️ **悬空引用**（数据完整性缺口） |
| D. 匹配到但 `sys_user.type ≠ '5'` | **297** | 1.0% | ⚠️ **账号类型不符**（数据完整性缺口） |
| E. 匹配且 `type='5'` | 20,782 | 68.3% | 正常路径 |

**C/D 共 354 单**：授权判据遇到它们时**行为未定义**——若只写"有 `partner_b_id` 就按运营商放行"，
C 会放行到不存在的账号、D 会放行到非代理商账号。**判据必须对这两种情况 fail closed（拒绝）**，
且应作为**数据质量问题**反馈给数据侧，不要靠应用层兜。

### 5.5 R-1 追查结论（新增，取代"待问同事"）

**`ch_site.partner_b_id` 指向 `qumall_upms.sys_user.id`，且该行 `type='5'`（代理商账号）。**

三条独立证据：

1. **源码成对写入**（`ChSiteServiceImpl.java:494-495`）——同一次调用里两个字段取自
   `partner_info` 的**不同列**（`getId()` 给 `agent_id`、`getOwner()` 给 `partner_b_id`），
   直接证明是两个 id 空间。
2. **写入落点**（`MallDataMapper.xml:87`）：`partnerBId` 被写进 UPMS 的用户-店铺关系表
   `user_id` 列；UPMS 侧把它定义为 `sys_user.id` 且要求 `type='5'`（`SysUserMapper.xml:428-437`）。
3. **JOIN 路径**（`ChOrderInfoMapper.xml:159-161`）：订单的 `partner_b_id` 与
   `partner_info.owner` 直接等值连接。

**生产库实测（2026-09-24，41 生产环境，只读元数据/基数）**：

| 链路 | 覆盖订单 | 占比 |
|---|---|---|
| `site.partner_b_id → sys_user(type=5)` | **20,782** | **68.3%** |
| `site.partner_b_id → partner_info.owner` | 21,130 | 69.4% |
| `site.agent_id → partner_info.id` | 2,875 | 9.4% |

> 注：68.3% 是 **join 成功率**，**不等于**"站点 `partner_b_id` 非空率"。差额的构成见 §5.6。

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
| C1 | 现有订单授权**只到租户层，没有运营商/平台层**。注意这不是「只按租户」——前端会话还叠加 `user_id`（见 C6），设备入口则只有租户。**缺的是运营商/平台这一维度，不是「完全没有过滤」** | `caller_auth.py:227-230` `can_access`；谓词由 `order_visibility.py:232-240` 渲染为 `tenant_id=?` / `IN (…)` / `1=0`；`user_id` 叠加见 C6 |
| C2 | 显式传 `order_no` 时，无权限返回 **404 `ORDER_NOT_FOUND`**，授权服务故障返回 **503** | `gateway_api.py:819-834` |
| C3 | 问句**文本里内嵌**订单号时，无权限**静默回落**零阶问答（已发布契约，有测试固化） | `gateway_api.py:862-903`；`tests/test_assistant_api.py:595-606`；`docs/validation.md:1420` |
| C4 | 会话内**活跃订单**的追问，无权限时静默回落并清掉绑定 | `gateway_api.py:912-957` |
| C5 | 前端会话（`third-session`）的 `data_scope` **硬编码为 `self`**，从不调 UPMS `/user/ds` | `third_session_auth.py:82` |
| C6 | 但 `self` **确实生效**：下推为 `user_id` 谓词 → 前端会话查订单**已按「本人」过滤** | `query_scope.py:232-240` → `sources.py:217,233-235` → `sources.py:286`，SQL `… AND user_id=?` |
| C7 | 订单可见性随**入口**使用不同 profile：调用者入口用 caller profile，**设备入口没有调用者 self 范围**。这是设计区分（`order_visibility.py:130-139`），**不是口径漂移** | `order_visibility.py:130-139`；`scoped_live_sources` 统一把 `QueryScope` 交给数据源（`sources.py:1306-1326`）。**更正**：先前把 `diagnostic_tools` 的 `get_orders(order_no, None)` 记为"不带 `user_id`、与 C6 不一致"，是**读错参数**——那个 `None` 是 `tenant_id` 实参，不是 `user_column`；`_scope_where()` 走默认值**会**带 `user_id` |
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

> **2026-09-24 更正**：本节原按「9.4% 可判定 / 90.6% 不可判定」提问，那是**旧结论**。
> 实测：运营商链路覆盖 **68.3%**；未覆盖的 31.7% 中 **30.6% 是站点本就没有代理商**
> （正常类别，见 §5.6），真正的数据完整性缺口只有 **1.2%**（57 悬空 + 297 类型不符）。

因此要问的不再是「90.6% 查不了怎么办」，而是：

- 判据对**无代理商的站点**（30.6%）应如何行为——放行到租户/平台，还是拒绝？
- 判据对**悬空引用/类型不符**（1.2%）应 **fail closed**（建议），并作为数据质量问题反馈数据侧。

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

#### P2-9 演示会话租户与真实租户要分开看

41 上解析到的租户有**两类，不能混为一谈**：

- `1899282205965029376`（H5 演示会话）——**不是 UPMS 租户**，后台租户切换器选不到；
- `1783022023241633792`（小趋充电）——**是 UPMS 真实租户**，后台可选，且有已绑在跑的样本
  （见 `kb-service-test-env.md`）。

**需要回答**：对**非 UPMS 租户**的会话（如 `1899282205965029376`），「运营商门」应放行还是拒绝？
（UPMS 真实租户按正常规则处理即可。）

---

## 3. 我方内部事项（不需要外部回答，但需授权/排期）

| # | 事项 | 说明 |
|---|---|---|
| C-1 | **修前端会话的 `data_scope`**（C5） | 目标仍是"会话能拿到真实范围"，**但路径已变**：`/user/ds` 结构性为空（§5.8 事实 3），**不能再作为目标接口**。改的是**鉴权路径**，需单独评估 + 独立票，不塞进本轮。**依赖 R-2 的答案** |
| C-2 | **operator 正向会话**（C11） | 至今拿不到唯一 B 端主体，验收只能做负向（越权必被拒）+ 消费者端回归；正向标 `blocked` |
| C-3 | ~~收敛 C6/C7 两种订单可见性口径~~ **已撤回**：C7 经核为误读（详见 C7 行），两条路径的差异是**有意的 profile 区分**（调用者 vs 设备），不是技术债 | 无需并入 #406 |
| C-4 | 订单投影加 `operator_id`（C8） | 若 P0-2 确认走订单侧，需要先加投影列 |

---

## 4. 结论：需求当前的可行性

| 需求片段 | 可行性 |
|---|---|
| 管家端入口（`operator`）+ 两个快捷指令 | **可落地**（数据发布为主） |
| 「订单检测」 | **可落地**，但**不能绑智能体**（C9），靠 `requires_order` 弹订单选择器 |
| 「客户案例」用统一案例库 | **待 P1-6**：需指定 KB + 跨租户授权 |
| 「订单查询加运营商权限判断」 | **可落地**。判据与链路已查清（§5.5）：运营商 ≡ `partner_info.owner` ≡ `sys_user.id(type='5')`；覆盖 **68.3%**。**实现路径已解（§5.9）**：会话 `userId` → `GET /shopuser/getShops` → `shop_id`（与站点 id 同域）→ 既有 `site_ids` 下推。**不需要后端改动。** 剩余为数据完整性（§5.9 边界 1）与实现期决策 |
| 现有授权缺口 | **真实缺口，与上述三者独立**：缺的是**运营商/平台维度**。注意前端会话**已**叠加 `user_id`（C6），所以不是「任何人可查」——但按人过滤既过窄（查不到本运营商别人的单），也可能与业务口径不符。即使 P0 全部悬置，也值得单独评估 |

**最重要的一句**：需求说的「该订单的**站点的**运营商 id 和平台 id」——
`ch_site` 的 90 列里**没有这两列**。这两个属性**都不在站点上**：
运营商经 `partner_b_id` 指到商城库的 `partner_info.owner`（**覆盖 68.3%**，见 §5.5），
「平台」在数据里是 shop id 的**哨兵值**而非独立字段（§5.1），**没有可比的平台 id**。
**这个差距不是命名问题，是数据模型与授权路径问题**，需要业务和后端共同确认后才能实施。

### 5.6 未覆盖部分的构成（支撑 R-6）

2026-09-24 实测，四类**严格加总等于总数**（30443）：

| 成因 | 订单数 | 占比 |
|---|---|---|
| 站点 `partner_b_id` 为空 | 9,307 | 30.6% |
| 有值但 `sys_user` 无匹配行 | 57 | 0.2% |
| 匹配到但 `sys_user.type ≠ '5'` | 297 | 1.0% |
| 匹配到且 `type='5'`（正常路径） | 20,782 | 68.3% |

站点粒度（954 个站点）：`partner_b_id` 为空 504 个、有值但无匹配 176 个（其中 10 个是 `type≠'5'`）。

**实现含义**：判据对「无 `partner_b_id`」与「引用悬空/类型不符」**必须区别对待**——
前者走"无代理商→租户/平台"规则，后者**fail closed**。把 31.7% 当成单一原因会导致
放行到不存在的账号或非代理商账号。

---

### 5.8 R-2 复核：卡点收敛为「AI-Ops 用什么身份代表调用者」（2026-09-24）

> **本节曾下过一个过满的结论**（"不需要后端改任何东西，只缺一小段适配"），
> 经核查**撤回**。以下为修正后的版本，并保留撤回原因。

#### 已确证的三条事实

1. **会话只提供身份** —— 41 生产 third-session 的**全部字段**是
   `appId / isEnterprise / isEnterpriseAdmin / openId / phoneAreaCode / sessionKey /
   tenantId / userId / userPhone / wxUserId`。**没有 roles、没有 organs、没有 shop_ids、
   没有任何数据范围**。所以 `RedisThirdSessionResolver` 硬编码 `data_scope=self`
   （`third_session_auth.py:82`）不是取舍，是**数据里就没有**。
2. **取范围的每个接口都要凭证，不是 `userId`** —— `ScopeResolver.resolve` 以
   `credential` 取调用者信息与范围（`scope_context.py:283-312`：`user_info(credential)`、
   `data_scope(credential)`）；`userId` 在该设计里只用于**解析目标主体**
   （`byUserId` 那条）。`/shopuser/getShops`、`/role/list` 同样要凭证。
3. **UPMS `/user/ds` 结构性为空** —— 它读 `sys_organ.biz_data` 并摊平；实测 41：
   349 行 `sys_organ`，**`biz_data` 非 NULL 的 0 行**。即使修好历史上那个空 IN 子句
   缺陷，也只能返回空列表。**这条接口当前不可用**（独立于第 2 条）。

#### 修正后的结论

> **本节的"未解"状态已被 §5.9 取代。** §5.9 查到了那条受信路径
> （`/shopuser/getShops` 直接吃 `userId`）。本节保留，用于存证收敛过程与两条边界。

**R-2 当时未解**，已从"走哪个接口"收敛为一个精确问题：

> **有没有一条受信路径，能用 third-session 的 `userId` 换到该调用者的身份/凭证？**

- **若「有」** → 走 A 方案：接既有 `ScopeResolver`，出 `shop_ids`/`site_ids` 下推
  （运营商范围需**新增** `partner_b_id → 站点集合` 的映射，见下）。
- **若「没有」** → 需要后端提供这样的路径，或改由后端施加 `@ShopDataScope`
  （那时卡点变成"后端如何信任 AI-Ops 传来的身份"）。

**两条路卡在同一处**，所以这个问题的答案同时决定 R-2 与实现方向。

#### 撤回原因（保留存证）

本节初稿把撤回旧方向的理由写成"`@ShopDataScope` 那条路依赖 `/user/ds`"。
**这是错的**：`ShopIdInterceptor` 的隔离集合来自 **`/shopuser/getShops`**（§5.3），
与 `/user/ds` 是**两条独立的调用链**。`/user/ds` 为空只影响"组织/店铺范围解析"，
**不构成对后端复用方向的否定**。

#### 仍成立的两条实现约束（与 R-2 答案无关）

1. ~~运营商范围需要新增站点映射~~ —— **已被 §5.9 撤回**：`shop_id` 与 `ch_site.id` 同域
   （954 行中 953 行相同），既有 `shop_ids` 下推**直接可用**，无需 `partner_b_id → 站点` 的新映射。
2. **既有验证用的是平台凭证** —— M34 那次端到端跑的是 `testadmin`，属平台凭证路径；
   **不能推断会话凭证路径已通**。

#### 顺带发现（应报平台侧）

`BaseSecurityInsideAspect`（`@Inside` 的鉴权）**整个判断体被注释掉**：

```java
//if (inside.value() && !StrUtil.equals(SecurityConstants.FROM_IN, header)) {
//     throw new AccessDeniedException("访问被拒绝，没有权限");
//}
return point.proceed();      // 无条件放行
```

`@Inside` 形同虚设。与 `DataScopeInterceptor`（组织级隔离，同样整段注释掉）
是**同一种形态**：**看着有隔离，实际是空的**。两者都应报平台侧。

---

### 5.9 R-2 答案：有受信路径，且不需要凭证（2026-09-24）

> 承接 §5.8 的收敛问题：**有没有一条受信路径，能用会话的 `userId` 换到调用者身份/范围？**
> 结论：**有。**

#### 证据链（公司源码 + 生产库实测）

1. **公司网关对 third-session 做的事与我们完全相同** ——
   `cloud-gateway/.../filter/ApiProxyHeadFilter.java`：从 Redis 取
   `app:3rd_session:<token>`，解析出 `ThirdSession`，然后**只注入头部**：
   `user-id` / `uid` / `tenant-id` / `site`。**没有 credential、没有 roles、没有范围。**
   即**公司自身从不做"userId 换凭证"**——身份以**头**的形式向下游传递，由下游各自解析。

2. **后端也采用同一模式** —— `ShopIdInterceptor.judge()` 要求
   `SecurityUtils.getUser()` 非空，而那个用户由 `from: Y` 头经 `@FeignAutoFillHeader` 注入。
   也就是说后端期望的**正是**"受信调用方 + 身份头"，而非某个用户凭证。

3. **`/shopuser/getShops` 直接接受 `userId`** ——
   `cloud-upms-admin/.../ShopUserController.java`：
   该端点（`GET /shopuser/getShops`）**无 `@Inside`、无类级鉴权**，SQL 为
   `select distinct shop_id from sys_user_shop where user_id = #{id}`。

4. **它正是后端权威授权所用的同一个接口** —— `ShopIdInterceptor` 在 `realTime=true` 时
   调的就是 `UpmsAdminFeignClient.getShops(userId)` → `GET /shopuser/getShops`。
   **我们调它不是在另发明一套范围，而是在用同一个数据源。**

5. **`shop_id` 可直接当站点集合用** —— 生产库实测：`ch_site.id` 与 `ch_site.shop_id`
   **954 行中 953 行相同**（同域）。`sys_user_shop.shop_id` 命中 `ch_site.id` 299 个，
   命中 `ch_site.shop_id` 同样 299 个 —— 两列可互换。

#### 因此落地方案（A 方案的具体形状）

> ⚠️ **本节初稿漏了一跳，已更正。** 初稿写「会话 `userId` → `getShops`」，
> 但两者**不是同一个 id 空间**：会话给的是 **C 端** `userId`，而 `getShops` 要 **B 端**
> `sys_user.id`。修正后的链条多一跳 C→B 映射，而该端点**既有**
> （`/user/inside/byUserId/{userId}`，`scope_context.py:414`）。

```
third-session 会话 → userId（C 端）
  → GET /user/inside/byUserId/{userId}     ← C→B 映射（既有端点）
  → B 端 sys_user.id
  → GET /shopuser/getShops?userId=<B 端 id> ← 既有端点
  → shop_id 集合
  → site_ids_by_shops(...)                  ← 既有映射，**不要**把 shop_id 直接当 site_id
  → QueryScope.site_ids 下推
```

**关于 `shop_id` 与站点 id**：两者**近似但不等同**（954 行中 953 行相同，**有 1 行不同**）。
因此**必须走既有 `site_ids_by_shops`**（`ch_site.shop_id → ch_site.id`，`sources.py:435-453`），
**不能把 `shop_id` 直接当 `site_id`** —— 那会绕过那一行。

**关于范围类型**：`RedisThirdSessionResolver` 现在产出 `data_scope=self`
（`third_session_auth.py:82`），而 `resolve_query_scope` 的 **self 分支会提前返回并忽略店铺集合**
（`query_scope.py:232-240`）。所以适配**必须改范围类型**（从 `self` 改为组织/店铺型），
不能只"填上 shop_ids"。

**但改范围类型会触发代查分支，这是第二个陷阱**：`RedisThirdSessionResolver` 同时设了
`delegated=True`（`third_session_auth.py:80`）与 `subject.c_user_id`，而
`query_scope.py:250-260` 对该组合会：

1. **要求 Dis 配置** —— `dis.point_ids_for_user(...)`，未配置则抛
   `SCOPE_ERROR_DIS_CONFIG_MISSING`；即"只改范围类型"会**直接报错**（若 41 未配 Dis）；
2. **取交集收窄** —— `sites = target_sites if sites is None else sites & target_sites`：
   把店铺推出的站点与 Dis 点位推出的站点**取交集**，**缩小**运营商可见站点。

**语义上 `delegated` 用错了**：该标志的原意是"代查**他人的**目标主体"
（`scope_context` 的设计如此），而会话场景里**用户查的是自己的单**。
所以适配**必须同时处理 `delegated`**（会话路径应置 `delegated=False`，或明确设计交集语义），
否则会踩上面两条之一。**这条必须在实现前定，不能留给实现时发现。**

**不需要后端新增或修改任何接口。**（§5.8 曾把"新增 `partner_b_id → 站点` 映射"列为约束，
据此**撤回**——走既有 `site_ids_by_shops` 即可。）

#### 仍成立的两条边界

1. **覆盖面不全** —— 实测 310 个代理商账号中，**只有 131 个有 `sys_user_shop` 绑定**。
   其余 179 个走这条路会得到**空集合 → fail closed**，即使其名下站点 `partner_b_id` 有值。
   这与 R-6 是**同一类问题**（数据完整性），应按同一规则处置：**fail closed 并记录，不兜**。
2. **`@Inside` 与组织级 `DataScopeInterceptor` 的鉴权体均被注释掉**（§5.3、§5.8）——
   意味着**端点本身不拒绝调用方**。我们依赖的是"内网服务 + 身份头"这一约定，
   而不是端点自带的保护。这一点应在实现时明确记录。

#### 安全核查（2026-09-24，针对审查提出的 id 碰撞）

审查提出：把会话 `userId` 传给 `getShops` **可能因标识碰撞读到他人范围**。实测：

| 检查 | 结果 |
|---|---|
| `sys_user.id` ∩ `sys_user.user_id`（B 端主键 ∩ C 端 id） | **0** — 两个 id 空间**当前无重叠** |
| `sys_user_shop.user_id` 命中 `sys_user.id`（B 端） | 468 |
| `sys_user_shop.user_id` 命中 `sys_user.user_id`（C 端） | 0 |
| 两者都命中（**真正的碰撞**） | **0** |

**结论**：传错 id 空间**今天不会**读到他人范围（会得到空集），但**这仍是必须修的设计错误**——
它是类型错误，且**碰撞何时出现取决于两个 id 空间将来是否分叉**，不能依赖"当前恰好不重叠"。
修正后的链条已含 C→B 映射（见上）。
