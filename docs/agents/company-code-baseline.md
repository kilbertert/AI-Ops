# 公司业务与代码基线

> **这个文件是什么**：#414 的产出。公司业务侧的**可积累、可复用**基线——让下一个业务模块
> 开发**先查基线**，而不是每次从零考古，也不再容易把「没读源码」误当成「数据不存在 / 要问人」。
>
> **怎么读它**：三层按需加深。L1 找仓、L2 找实体、L3 判权限，L4 是按需求驱动的验证用例。
> 每条结论都带**出处**，出处格式 `仓:分支:文件:行`——**没有出处的条目不算结论**，
> 只算待办。三层都**按 `company-code-recon-procedure.md` 执行**（怎么查写在那一页，
> 不在这里复述）。
>
> **怎么往里加**：每层一节，**由一张票独占**（L1 = #476、L2 = #477、L3 = #478、
> L4 = #479），三张票并行开工时各写各的那一节。往别的节里加内容 = 制造冲突。
>
> **边界（不可让步）**：公司源码是外部治理资产，**只读**；本文件只写**结论与出处**，
> **不搬运公司源码正文**；不写任何凭据、token、口令明文，只写位置。**本仓是公开仓库**，
> 写进这里就当作公开内容来写——已有的主机名/路径可以沿用，**新增的基础设施细节先问**。

---

## L1 · 仓库地图

> **本节由 #476 填充。** 粒度只到「域 → 主仓 + 真分支」。
> **全仓逐条索引不在本页**，在 [`company-repo-index.md`](company-repo-index.md)（519 行，
> 由 `tools/company_repo_index.py` 生成）。本页只写「域 → 主仓 → 真分支 → 活跃度」。

**按 `company-code-recon-procedure.md` 执行。** 取数日 **2026-10-01**（UTC）。

### 0. 全仓的活跃度分布（先给量级）

**口径**：距最后活动天数按**日期粒度**算（`取数日 − last_activity_at 的日期`，单位天）。
这与索引里印出来的那列是同一种粒度，也是本表可被机械核对的原因 ——
按到秒算会得到另一组数字（52/37/28/402），**两组都对，但不能混用**：
索引只印到天，文档若引到秒，两者就对不上了。

| 距最后活动（日期粒度） | 仓数 |
|---|---|
| ≤ 30 天（活） | **50** |
| 31–180 天（半活） | 39 |
| 181–365 天（慢） | 28 |
| > 365 天（停） | **402** |

⇒ **519 个可见仓里，近 30 天有活动的只有 50 个（约 10%）。**
这条决定了后续怎么用这张地图：**先按最近活动筛，再归域**。
按「域」遍历 519 个仓会花掉绝大部分时间在 402 个停了一年以上的仓上。

### 1. 六个域的主仓

| 域 | 主仓（`path_with_namespace`） | id | 默认分支 | **真分支** | 判据（`--count-java`） | 最后活动 |
|---|---|---|---|---|---|---|
| **充电桩** | `iot/cloud-charging-pile` | 363 | `master` | **`release`**（`uat` 同代） | master **34** / release **2012** / uat 2021 | 2026-09-30 |
| **商城** | `s2b2c-java/qumall` | 137 | `dev_230201` | **`dev`**（`release`/`uat` 同代） | dev_230201 **3544** / dev **3726** / uat 3722 | 2026-09-30 |
| **UPMS** | `s2b2c-java/cloud-upms` | 557 | `dev_230201` | ⚠️ **待定** | `refactor` **390** / `release` **231** / `dev` 231 | 2026-09-28 |
| **网关** | `s2b2c-java/cloud-gateway` | 488 | `main`（**2023 年的**） | **`release`**（`dev` 同规模） | release **44** / dev 44 / dev_230201 **36** | 2026-09-17 |
| **认证** | `s2b2c-java/cloud-auth` | 489 | `main`（**2023 年的**） | **`dev_251103`** | dev_251103 **17** / dev_230201 9 | 2026-05-11 |
| **通用库** | 见 §3（不止一个） | — | — | — | — | — |

**三个默认分支都不等于真分支**，且形态各不相同——这正是规程 §2 那条判据的三次不同实例：

1. **`master` 是脚手架**（充电桩）：34 → 2012，差两个数量级。
2. **默认分支落后一代**（商城）：`dev_230201` 3544，而 `dev` 3726 —— 差得不多，
   所以**光看「像不像脚手架」看不出来**，必须真的数一遍。
3. **默认分支停在 2023 年**（网关 / 认证）：`main` 在网关停在 **2023-11-02**、
   在认证同样停在 **2023-11-02**。这两个仓的 `main` 是**遗留分支**，
   真代码在 `release` / `dev_*` 上。只看默认分支会得出「这个仓三年没动了」，
   而它其实上个月还在提交。

### 2. 一处**不能猜**的地方：UPMS 的真分支标为待定

`cloud-upms` 的分支读数是：`refactor` **390** / `release` **231** / `dev` **231**。
`refactor` 的文件数**明显多于**另外两个，但「文件多」**不等于**「线上那份」——
它更可能是一次进行中的重构（分支名就是这个意思），而 `release`/`dev` 才承载当前结构。

**本票不给结论。** 判「UPMS 真分支是哪个」要看提交内容与部署记录，不在「数文件数」的
能力范围内 —— 按规程 §2 的补充判据，此时正确做法是**标为待定**，而不是从文件数最多的
那个推一个答案出来。

### 3. 通用库不是一个仓

「通用库」在这套命名里是**一组**被各业务域依赖的仓，没有单一的「主仓」：

| 仓 | id | 默认分支 | 真分支 | `.java` | 最后活动 |
|---|---|---|---|---|---|
| `base/ddd4j` | 565 | `master` | `master` | 201 | 2026-07-14 |
| `paas/qushisdk` | 483 | `master` | `master` | 188 | 2026-09-04 |
| `common-java/cloud-message` | 357 | `master` | `release` | 148（两分支同规模） | 2026-07-23 |
| `s2b2c-java/cloud-mall-common` | 558 | `main` | **`dev`**（`dev_251103` 同代） | dev **1376** / dev_251103 1373 / dev_chuanbang 1228 / **main 1209** | 2026-09-28 |

⚠️ `s2b2c-java/cloud-mall-common` 列在这里，是因为**它的名字里有 `common`**；
但它服务的是**商城域**（`PartnerInfo` / `ShopInfo` 的实体都在它里面，见 L2）。
两条分类规则（`mall` 与 `common`）它都命中，于是：

- **索引把跨域命中一律标 `未定`**（不按规则顺序取先命中的那个）——这是「规则怎么分类」；
- **本表按服务对象把它归到商城域**——这是「它服务谁」。

两者不矛盾，是**两个问题**。规则那条是核对时抓出来的：先前的实现按顺序取首个命中，
索引把它标成「商城」，与本页早先的「待定」打架；现在两侧各说各的口径并都写明了。
它也是本组里最活跃的（2026-09-28）。

### 4. 显式未定（不猜）

「未定」有两类，分开写，因为它们的含义不同：

**（a）归域未定**：路径里没有可判定的域前缀，且名字不落在任何规则上。
全索引里 **474 / 519** 行的「域」列是 `未定`，这是**规则**的结果不是遗漏 ——
规则按「命名空间前缀 + 仓名关键词」机械判定，不认识的一律不归类。
**其中近 180 天有活动的 60 个**，逐个判断值不值得归域是**按需**的事，不在本票。

**（b）本票明确不答的**：

| 问题 | 为什么不答 |
|---|---|
| `UPMS` 真分支 | 见 §2：文件数不能判「线上那份」 |
| `cloud-mall-common` 在**索引**里的归域 | 它同时命中两条规则 ⇒ 索引按规则标 `未定`；本表按**服务对象**归商城（见 §3）。**规则不替人做这个判断** |
| 519 个仓的逐条归域 | 粒度超出本票（L1 只到「域 → 主仓」），且 402 个已停一年以上 |

**这两类都不许猜。** 猜出来的归属比 `未定` 更糟：`未定` 会让下一个人去查，
猜错的归属会让他**停止**查。

### 5. 可复现性

```bash
# 域 → 主仓 → 真分支：分支读数（按提交时间倒序取前几个）
deploy/company-gitlab-api.sh branches --project 363 --limit 4

# 真分支判据：数 .java
deploy/company-gitlab-api.sh tree --project 363 --ref master  --count-java
deploy/company-gitlab-api.sh tree --project 363 --ref release --count-java

# 全仓索引（本页 §0/§4 的数字都出自它）
python3 tools/company_repo_index.py --out docs/agents/company-repo-index.md
```

第二人独立执行可得到相同结论：`tools/company_repo_index.py` 与索引都带**取数日**，
比对时看那一列；分支读数会随新提交变动，比对的是**数量级关系**
（脚手架 vs 真码、默认分支 vs 真分支），不是具体数字。

### 6. 本层留给下游的两个钩子

- **L2/L3 要用哪个分支搜代码**：本页 §1 的「真分支」列就是答案（UPMS 除外，标了待定）。
  出处一律写成 `仓:分支:文件:行`，分支取自本列。
- **`iot/tsdata`（604）** 是充电桩域里近 30 天活跃的一个仓（2026-09-24），
  与 AI-Ops 的 TDengine 数据面相关 —— **L2/L3 若碰到数据面，从这里进**。

> **2026-10-01 由 #477 回填一处**：§3 表里 `cloud-mall-common` 的「真分支」原写「待定」，
> 现补为 **`dev`**。判据（当场跑）：dev **1376** / dev_251103 1373 / dev_chuanbang 1228 /
> **main 1209** —— 与「默认分支落后一代」同形，只是差距小。
> 注意它**不属于** §2 那种「无法判」的情形：§2 是 `refactor` 390 与 `release` 231 的
> 近一倍差、且**方向不明**（更像进行中的重构）；这里是四个分支同一数量级、`dev` 最新也最大，
> 方向清楚。
> **这是跨节修改**（L1 本属已合并的 #476）。判据是：**已知写错的一格比节边界更重要**，
> 且该票已合并、没有并行写者会冲突。回填由 #479 统一负责，此处按同一纪律先修。

---

## L2 · 关键实体图

> **本节由 #477 填充。** 覆盖 租户 / 平台 / 运营商 / 店铺 / 站点 / 订单 / 设备 / 用户
> 的主键、外键、真实注释。**每条边必须有 `仓:分支:文件:行` 出处**——这是本层唯一的可信度来源。

**按 `company-code-recon-procedure.md` 执行。**

⚠️ 两条已知纪律，先写在这儿免得被忘掉：
**① 不是实体的东西不要混进实体表**（「平台」是 shop id 的**哨兵值**，不是一张表）；
**② 近似 ≠ 等同**，已知反例是 `shop_id` 与 `site_id`（954 行里 1 行不同），
差异必须连同量级一起写出来，不能四舍五入。

**本节口径**：出处写 `仓:分支:文件:行`。**行号取自下面列出的那个分支**，
换分支行号会漂。

---

### L2-0 一个先把结论钉死的发现：这套库里「运营商」是两个不同的东西

需求里的「运营商」在本层必须区分，否则后面每条边都会对错：

| 词 | 指向 | 出处 | 特征 |
|---|---|---|---|
| **商城侧的「运营商」** | `partner_info`（合伙人/代理商） | `cloud-mall-common:dev:entity/marketPool/PartnerInfo.java:28`（`@TableName(value = "partner_info")`）；同仓 `:19-23` 类注释「合伙人」 | 有 `owner`（**b 端账号 id**，`:89`）、`groupHeader`（c 端账号 id，`:94`）、`shopId`（运营商店铺，`:58`）、`parentId`（有限级，`:147`）、`type`（`:149`） |
| **互联互通侧的「运营商」** | `ht_stations_info.operator_id` / `ht_channel_info.operator_id` / `ch_order_info.operator_id` —— **对端平台在互操作协议里的标识** | `cloud-charging-pile:release:data/po/HtStationsInfo.java:47-50`（注释「运营商id」）；`cloud-charging-pile:release:application/controller/HtStationsInfoController.java:47` 的 `operatorIds` 来自 `HlStationOpenReq`，是**请求里带的对端标识**，不是本地账号 | 与 UPMS 账号无关；`ch_order_info.operator_id` 只在互操作链路上被赋值/筛选（`cloud-charging-pile:release:application/hlht/easycharge/EasyChargeAuthorityService.java:206` 等） |

**判据**：`partner_info` 与 UPMS 账号有稳定的桥（见 L2-1）；`ht_*` 系列的 `operator_id`
**没有**这条桥，它只在互操作协议内部有意义。**把两者当成一个「运营商」是本层最容易犯的错**。

> 旧文档 `operator-authorization-questions.md` §5.2 末句把 `HtChannelInfo.operatorId`
> 注释为「互联互通**平台**标识」——那说的是**同一个东西的另一侧**（对端平台自己叫它「平台」）。
> 两处注释措辞不同、指向一致，**不是矛盾**，但合起来读很容易误解，故在此并排写清。

---

### L2-1 关键实体与主键

| 实体 | 表 | 主键 | 出处（仓:分支:文件:行） |
|---|---|---|---|
| **租户** | 由 `@Tenant` 注解与**列 `tenant_id`** 表达，未找到独立的「租户 PO」 | —— | `cloud-charging-pile:release:data/po/IotChargingDevice.java:27`（`@Tenant`）、`:43`（`tenantId`）；`cloud-charging-pile:release:data/po/ChSite.java:52`、`cloud-charging-pile:release:data/po/ChOrderInfo.java:327`、`cloud-mall-common:dev:entity/ShopInfo.java:52`、`cloud-mall-common:dev:entity/marketPool/PartnerInfo.java:36` |
| **平台** | **没有表**。是 shop id 的**哨兵值 `'-1'`** | —— | 见 L2-3 |
| **运营商** | `partner_info` | `id`（`@TableId`） | `cloud-mall-common:dev:entity/marketPool/PartnerInfo.java:28` |
| **店铺** | `shop_info` | `id` | `cloud-mall-common:dev:entity/ShopInfo.java:33`（`@TableName`） |
| **站点** | `ch_site` | `id` | `cloud-charging-pile:release:data/po/ChSite.java:37` |
| **订单** | `ch_order_info` | `id` / `order_no` | `cloud-charging-pile:release:data/po/ChOrderInfo.java:28`、`:37`、`:42` |
| **设备** | `iot_charging_device` | `id` | `cloud-charging-pile:release:data/po/IotChargingDevice.java:28`、`:28-39`（类注释「充电桩设备表」） |
| **用户** | `sys_user`（UPMS） | `id` | 见 L2-2（本仓只有它的外键方，实体在 `cloud-upms`） |

⚠️ **租户这一行是有意写「没有独立 PO」的**：本仓检索到的都是 `tenant_id` **列**与
`@Tenant` 注解，没有找到一张以租户为主键的表定义。把「找不到租户表」写成
「租户不是一个实体」是**过度推断**——它在 UPMS 侧大概率有表，但我没查到出处，
所以这一格留白，不猜。

---

### L2-2 关键边（每条都有出处）

| 边 | 语义 | 出处 | 备注 |
|---|---|---|---|
| `ch_site.shop_id` → `shop_info.id` | 站点所属店铺 | `cloud-charging-pile:release:data/po/ChSite.java:54-57`（注释「店铺ID;关联商城店铺ID」） | 列名就是 `shop_id`，与 `ch_site.id` **同域但不等同**（见 L2-4） |
| `ch_site.agent_id` ← `partner_info.id` | 代理商 | `cloud-charging-pile:release:domain/service/impl/ChSiteServiceImpl.java:491`（`setAgentId(partnerInfo.getId())`） | **成对写入的一半** |
| `ch_site.partner_b_id` ← `partner_info.owner` | 代理商 B 端账户 | `cloud-charging-pile:release:domain/service/impl/ChSiteServiceImpl.java:492`（`setPartnerBId(partnerInfo.getOwner())`） | **成对写入的另一半**；两列取自 `partner_info` 的**不同列**，所以是两个 id 空间 |
| 成对写入的上下文 | 由 `shopInfoModel.getPartnerId()` 取 `PartnerInfo` | `cloud-charging-pile:release:domain/service/impl/ChSiteServiceImpl.java:486-490` | 前提是店铺的 `partnerId` 非空 |
| `shop_info.partner_id` → `partner_info` | 店铺关联的合伙人 | `cloud-mall-common:dev:entity/ShopInfo.java:719`（`partnerId` 注释「合伙人id」） | 是上面那条写入的**上游**：店铺先有合伙人，站点才被填代理商 |
| `ch_order_info.site_id` → `ch_site.id` | 订单所属站点 | `cloud-charging-pile:release:data/po/ChOrderInfo.java:77`；`cloud-charging-pile:release:data/mapper/ChOrderInfoMapper.xml:41`（`left join ch_site ch_site on ch_site.id = ch_order_info.site_id`） | |
| `ch_order_info.partner_b_id` = `partner_info.owner` | 订单侧的代理商 B 端账户 | `cloud-charging-pile:release:data/po/ChOrderInfo.java:698`（注释「代理商B端账户id」）；`cloud-charging-pile:release:data/mapper/ChOrderInfoMapper.xml:110-111`（`convert(pi.owner …) = convert(coi.partner_b_id …)`） | 跨库 join，**两侧都要 convert + 显式排序规则**（见下） |
| `iot_charging_device.site_id` → `ch_site.id` | 设备投放的场地 | `cloud-charging-pile:release:data/po/IotChargingDevice.java:45-47`（注释「投放的场地id」） | |
| `iot_charging_device.agent_id` | 设备侧也带代理商 id | `cloud-charging-pile:release:data/po/IotChargingDevice.java:72-74`；建表列出处 `cloud-charging-pile:release:resources/db/migration/V1.0.4__device_agentId_add.sql:1-2`（`add agent_id … comment '代理商id'`） | ⚠️ **与 `ch_site.agent_id` 是否同源，本票未证**：设备侧的赋值链没查，**不写成等同** |
| `partner_info.owner` → UPMS 的 B 端账号 | 运营商身份可绑 | **本节不给出处**（见 L2-5 第 1 行） | 这条映射**不在源码里**；既有记录把它列为生产库实测，本票**没有复核**，所以既不写成出处也不指向它 |

**跨库 join 必须带排序规则转换**：`cloud-charging-pile:release:data/mapper/ChOrderInfoMapper.xml:109-111` 的 join 条件两侧
都套了 `convert(… using utf8mb4) collate utf8mb4_general_ci`。两库排序规则不同，
直接等值 join 会报 `Illegal mix of collations`；**这不是我们踩的坑，是公司自己代码里的写法**，
照抄即可。（此处不贴原文，只记这个决策——见本节开头的「不搬运正文」。）

---

### L2-3 「平台」= 哨兵值 `-1`（**不是实体，不进实体表**）

`@ShopDataScope` 的 `seePlatform` 开关打开时，隔离条件会把调用者可见的 shop 集合
**并上一个哨兵 `'-1'`**，再整体放进 `IN (…)`；若调用者没有 shop 集合，则直接写成
`IN ('<当前 shop>', '-1')`。两种情况都会 `add("-1")`。

出处：`qumall-common:dev_251103:cloud-common-data/…/datascope/shop/ShopIdInterceptor.java:158-166`；
另有 `:178`、`:193` 两处相同的 `add("-1")`。`seePlatform` 的定义在
`qumall-common:dev_251103:cloud-common-data/src/main/java/com/qushiyun/cloud/common/data/datascope/ShopDataScope.java:26-31`（注释「**是否平台**」，默认 `false`）。

**为什么它是哨兵而不是实体**：`'-1'` 出现在 `scopeName IN (…)` 列表里，作为**一个取值**参与
匹配；没有任何表以 `-1` 为键。生产库实测 `ch_site` 里 `shop_id='-1'` 的行数为 **0**
（结论另记在 `operator-authorization-questions.md` §5.1，**本票未重测**）——
即该通路今天**是空操作**。

**边界（必须写出来）**：
- 本票**只从源码证明**「`seePlatform=true` 时会并上 `'-1'`」；
- 「因此平台方能看到所有订单」**不在本票结论内**——那取决于产品口径，
  既有记录已按「无此场景」关闭（同上 §5.1）。

---

### L2-4 近似对：`shop_id` 与 `site_id`

**不要互用。** 生产库实测 **954 行里 1 行不同**（出处：
`operator-authorization-questions.md` §5.9，**本票未重测**），量级是千分之一，
但方向是「把某一行的站点给了另一个店铺」，在授权场景下**不是舍入误差**。

任何「用 `shop_id` 当站点集合」的写法都必须走既有映射
（`site_ids_by_shops`，即 `ch_site.shop_id → ch_site.id`），不能直接代入。

---

### L2-5 本票没答的（不猜）

| 问题 | 为什么不答 |
|---|---|
| `partner_info.owner → sys_user.id` 的**源码**出处 | 这条映射在源码里没找到。既有记录（`operator-authorization-questions.md` §5.5）列了三条证据，其中一条说某个 Mapper 把 `partnerBId` 写进 UPMS 的 `user_id` 列 —— **本票没有复核那三条**，所以此处**不复述、也不写成出处**（写成出处就等于替它背书） |
| `iot_charging_device.agent_id` 与 `ch_site.agent_id` 是否同源 | 设备侧的赋值链没查 |
| 租户表的实体定义 | 见 L2-1 的说明 |
| `sys_user` / `sys_user_shop` 的表结构 | 实体在 `cloud-upms`，本票只用到外键方 |

**上游**：本节的分支与仓取自 L1 §1（`cloud-charging-pile` → `release`、
`qumall-common` → `dev_251103`、`cloud-mall-common` → `dev`）。

---

## L3 · 鉴权与数据范围

> **本节由 #478 填充。** 回答的是**「谁能看到哪些行」，且答案可判定**——
> 不是「大概有权限校验」。给定身份与请求上下文，要能推出可见行集合。

**按 `company-code-recon-procedure.md` 执行。**

### 与既有文档的分工（一句，写入本节开头）

本层**只写「公司侧机制本身怎么判权限」**；**「AI-Ops 站在公司体系的哪个位置」不在本层**，
一律指向 `company-platform-integration-baseline.md` §2。两份各写一遍会漂移，而它们回答的是
两个不同的问题——分工写在这里，是为了不重复搬运。

⚠️ **死代码必须点名**：`@Inside` 的鉴权体与组织级 `DataScopeInterceptor` 都是**整段被注释掉**的。
不点名，后来者会照着它做安全推断。点名要附出处；没有出处的「疑似死代码」标为**待定**，不当结论。

**本节口径**：出处写 `仓:分支:文件:行`，行号取 L1 §1 的真分支
（`qumall-common` → `dev_251103`、`cloud-gateway` → `release`、`cloud-upms` → `release`）。

---

### L3-0 一句话总纲：判权限的是**同一个拦截器**，不是每个接口自己

「谁能看到哪些行」在这套体系里只有一个决定点：**缓存/ORM 层的 `ShopIdInterceptor`**。
它在 SQL 执行前改写 `WHERE`，追加一个 `scopeName IN (…)`。**没有第二套**——
所以下面所有「谁能看到」的答案，都可以从这一个函数的判据推出来。

**代码量本身不是判据，位置才是**：这个函数在网关**不**生效（网关用的是 `AuthGlobalFilter`，
开关默认关；见 L3-3），在后端**生效但不总是**（下面五道门，任何一道不过就完全不隔离）。

---

### L3-1 `@ShopDataScope` 的语义

注解定义在 `qumall-common:dev_251103:cloud-common-data/src/main/java/com/qushiyun/cloud/common/data/datascope/ShopDataScope.java:9-46`：
标在 **mapper 的 select 方法**上（`:10` 注释「店铺数据隔离，mapper select方法上」），
五个开关：

| 开关 | 默认 | 含义 | 出处（同文件） |
|---|---|---|---|
| `isolation` | `true` | 是否隔离 | `:22` |
| `column` | `"shop_id"` | **隔离列名** | `:27` |
| `seePlatform` | `false` | 「是否平台」——为真时把哨兵 `'-1'` 并进集合 | `:33` |
| `realTime` | `false` | 用 `getShops` **实时**取店铺集合（而不是用会话里那份） | `:38` |
| `montage` | `false` | 隔离条件**直接作用于当前 where**，而不是包一层子查询 | `:45` |

`montage` 的语义只在注解注释里写清（`:41-44`：「false:使用子查询 / true:直接作用于当前的
where条件」），落地在 `ShopIdInterceptor.java:189-221`。

实际取值举例（充电桩侧，`cloud-charging-pile:release`）：
- `data/mapper/ChOrderInfoMapper.java:59` —— `@ShopDataScope(column = "site_id", realTime = true)`，
  **隔离列是 `site_id`**，所以订单按站点隔离；
- `data/mapper/ChSiteMapper.java:44` —— `@ShopDataScope(column = "id", montage = true, realTime = true)`，
  站点按**自身 id** 隔离，且用 montage 直接改 where。
  同文件 `:43` 有一行**被注释掉的旧注解** `//@ShopDataScope(column = "id")` —— 是历史，不是配置。

⚠️ **「隔离列」是可变的，不要在别处硬编码 `shop_id`。** 充电桩侧两处用的就是 `site_id` 与 `id`。

---

### L3-2 「谁能看到哪些行」——一个可判定的过程

给定「请求头 + 会话身份 + 目标 mapper」，可见行集合由下面五道门依次决定。
**任何一道不过，`ShopIdInterceptor` 直接 `return`，SQL 原样执行 ⇒ 该查询不发生隔离。**

| # | 门 | 不过会怎样 | 出处 |
|---|---|---|---|
| 1 | `ShopIdInterceptorContextHolder.isClose()` 为真 | 打日志「关闭店铺拦截器」并放行 | `ShopIdInterceptor.java:78-81` |
| 2 | `@ShopDataScope(isolation=false)`，或该 mapper 不在配置的 mapper 组里 | `judge` 返回 false | `:285-301`（`ShopScopeHelper.isolation` / mapper 组查找） |
| 3 | 取不到当前用户（`SecurityUtils.getUser()` 抛异常或为 null） | `judge` 返回 false | `:259-266` |
| 4 | **用户类型是 `-1`（平台）或 `1`（租户）**，且请求头 `shop-id` 为空或是 `-1` | `judge` 返回 false | `:269-271` |
| 5 | **请求头 `client-type` 不在 `{admin, supply-admin, tenant-app}`** | `judge` 返回 false | `:280-282` |

过了五道门，隔离条件这样拼（`scopeName` 即上面的「隔离列」）：

- **`seePlatform = true`**：把哨兵 `'-1'` **并进**集合，再整体放进 `IN (…)`；
  没有集合时写成 `IN ('<当前 shop>', '-1')`。出处 `:158-166`（`selectCount` 支）与 `:187-196`（主子查询支）。
- **`seePlatform = false`**（默认）：有 `user.shopIds` ⇒ `scopeName IN (店铺集合)`；
  否则若 `shopId` 非空 ⇒ `scopeName = '<shopId>'`；
  **两者都没有 ⇒ `return`，SQL 原样执行**（`:241-243`）。

分支结构：`selectCount` 走 `:147-176`，其余走 `:177-247`；`montage` 在 `:189-221`。
⚠️ 最后那个 `return` 只在 `seePlatform=false` 这一支上 —— 关掉一个分支的隔离，
在**代码层**是「放行」，在**语义层**是「不追加条件」。

**集合为空时到底放行还是收紧，取决于哪一个开关**，这是本节最容易被读错的一处：

| `seePlatform` | 会话里没有 `shopIds` 也没有 `shopId` | 结果 |
|---|---|---|
| `false`（默认） | —— | **放行**（`:241-243` 的 `return`） |
| `true` | —— | **收紧**：`IN ('<当前 shop>', '-1')`（`:164`、`:196`） |

**实时取集合的条件**（`:139-145`）：`realTime=true`，**或**用户类型以 `5` 开头
**且**会话里 `shopIds` 为空 **且** 头 `shop-id` 是 `""`/`-1` —— 满足才去调
`upmsAdminFeignClient.getShops(user.getId())`。

**店铺集合来自哪里**：`GET /shopuser/getShops`，SQL 是
`select distinct shop_id from sys_user_shop where user_id = #{id}`
（`cloud-upms:release:cloud-upms-admin/src/main/resources/mapper/SysUserMapper.xml:526-530`；
端点 `.../controller/ShopUserController.java:386-389`，`@RequestMapping("/shopuser")` 在 `:65`）。
⚠️ 该端点**没有 `@Inside`、没有类级鉴权注解**（`:62-66` 只有 `@RestController` 等）——
它靠的是「内网 + 身份头」的约定，不是端点自带保护。

---

### L3-3 `client-type` 的取值全集，与 **AI-Ops 落在哪一支**

**网关产出的那一套**（`cloud-gateway:release:src/main/java/com/qushiyun/cloud/gateway/filter/ApiProxyHeadFilter.java:30`）：
`MA`、`H5`、`APP`、`PC-MA`、`BP`、`BP-MA`、`H5-PC`、`WX-H5`。

**后端隔离门认的那一套**（`qumall-common:dev_251103:cloud-common-data/src/main/java/com/qushiyun/cloud/common/data/datascope/shop/ShopIdInterceptor.java:280`）：
`admin`、`supply-admin`、`tenant-app`。

**两个集合不相交**，而且两处的大小写处理还不一样：网关用
`equalsAnyIgnoreCase`（`:55`）比对，拦截器用 `equalsAny`（大小写敏感）。
⇒ `H5` 过得了网关，过不了隔离门；`admin` 反之。

**AI-Ops 落在哪一支** —— ⚠️ **这里要说准：是「分两次调用」+「将来取决于 D 批」，不是一句
「AI-Ops 不隔离」**。本层的评审抓出过一个把结论下满的写法（说「管家端在网关之后、
隔离可能已生效」），所以按调用形态分开写：

| 调用形态 | 带不带 `client-type` | 门 5 | 结果 |
|---|---|---|---|
| AI-Ops 出站调公司接口（`/diag/*`、UPMS `/user/inside/*` 等） | **不带**：出站头只有 `X-Internal-Token` 与 `Accept`（`src/aiops_diagnostics/sources.py:722` + `:672-677`），**不转发任何入站头** | **不过** | 这些调用**不隔离** |
| 管家端浏览器 → 公司后端（**不经过 AI-Ops**） | 带（前端产物实发 `admin` / `tenant-app`） | **过** | 隔离**生效**，与 AI-Ops 无关 |

⇒ **正确结论是**：AI-Ops 是第三条数据路径（直连生产库 / 走 `/diag/*`），因此**绕开**了
这套隔离；**不是**「这套隔离对 AI-Ops 不生效所以无所谓」——后者会把「缺一层授权」
读成「不需要这层授权」。两条事实各有出处：

- AI-Ops 出站不带 `client-type`：`src/aiops_diagnostics/sources.py:712-723`、
  `src/aiops_diagnostics/http_auth.py`（此前记作蓝图 F5「AI-Ops 侧零引用」，**本票复核为
  「出站构造里没有这个头」——比「零引用」更准**）；
- AI-Ops 现在**不在**公司网关之后：`company-platform-integration-baseline.md` §1
  （nginx 把 `/v1/` 写在网关前面）。

⚠️ **第二条是会被 D 批改掉的**（蓝图 §2 的目标态就是把 AI-Ops 挂到网关之后）。
到那时**本节这一段要重测**：挂过去之后，`/v1/` 是否会被网关注入 `client-type`、
注入成哪一个值，直接决定门 5 过不过。**本票不预测那个结果**——它取决于 D 批怎么落。

---

### L3-4 `/shopuser/getShops` 与 `/user/ds` 各返回什么、供谁用

| 端点 | 返回 | 供谁用 | 状态 |
|---|---|---|---|
| `GET /shopuser/getShops?userId=<B端 id>` | `sys_user_shop` 里该用户的 **distinct `shop_id`** 列表 | `ShopIdInterceptor` 在 `realTime=true` 时调它取隔离集合（`ShopIdInterceptor.java:141`） | **可用**，是授权链的权威数据源 |
| `GET /user/ds` | 读 `sys_organ.biz_data` 摊平 | 组织/店铺范围的**旧**解析路径 | **结构性为空**：41 实测 349 行 `sys_organ` 里 `biz_data` 非 NULL 的 **0 行** |

`/user/ds` 那条的实测结论出自 `operator-authorization-questions.md` §5.8 第 3 条
（**本票未重测**）；列在这里是因为 L3 要回答「哪个接口是权威」——
**权威是 `/shopuser/getShops`**，与 `/user/ds` 是两条独立调用链。

---

### L3-5 死代码点名（**三处**，逐条附出处）

| # | 什么 | 形态 | 出处 |
|---|---|---|---|
| 1 | **`@Inside` 的鉴权体** | 整个 `if` 块被注释，函数体只剩 `return point.proceed()` ⇒ **无条件放行** | `qumall-common:dev_251103:cloud-common-security/src/main/java/com/qushiyun/cloud/common/security/component/BaseSecurityInsideAspect.java:27-36`（注释在 `:31-34`） |
| 2 | **组织级 `DataScopeInterceptor`** | `beforeQuery` 的**整个方法体**包在 `/* … */` 里，方法只剩空壳 | `qumall-common:dev_251103:cloud-common-data/src/main/java/com/qushiyun/cloud/common/data/datascope/DataScopeInterceptor.java:25-91`（注释块 `:30-90`；`:91` 之后只有结束大括号） |
| 3 | **网关 `AdminProxyHeadFilter`** | `apply` 第一句就是 `return chain.filter(exchange)`，**其余全部注释** ⇒ 空操作 | `cloud-gateway:release:src/main/java/com/qushiyun/cloud/gateway/filter/AdminProxyHeadFilter.java:34-37`（注释自 `:38` 起） |

**为什么必须点名**：这三处的注释块里，写的是**完整的、看起来在生效的鉴权逻辑**。
读到它们的人会做出安全推断（「这个端点有 `@Inside`，所以只有内网能调」），
而实际行为是「谁都能调」。**判断一个机制在不在，要看它有没有在注释里，不能只看它存在。**

第 3 处还有一层：`AuthGlobalFilter` 的开关 `cloud.auth.enable` **默认关闭**
（`cloud-gateway-dev.yml`，结论另记在 `company-platform-integration-baseline.md` §1.2，
**本票未重测**），所以网关侧的「必须登录」同样不成立。

> ⚠️ 「疑似死代码」与「死代码」要分开：上表三条都**逐行看过注释边界**才写。
> 只凭「没见它生效」推断的，标**待定**，不进这张表。

---

## L4 · 按需求深化（验证用例）

> **第一节由 #479 填充：订单授权。** L4 的作用不是再查一遍，而是**证明基线可用**——
> 从 L2 拿实体与边、从 L3 拿判据，推出结论，并把引用链逐条写出来。
> **基线若在某一步不够用，那一步就是基线的缺口**，据此回填 L1/L2/L3，而不是绕过它自己另查一遍。

后续 L4 按实际需求逐票驱动（计费 / 设备 / 知识库 …），不预先铺开。

（尚未填充。）
