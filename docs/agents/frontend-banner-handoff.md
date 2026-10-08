# 聊天页运营横幅：前端联调交接说明

> **读者**：前端组。
> **状态**：后端已实现（分支 `feat/banner-read-endpoint`，叠于 `feat/banner-row-in-shortcuts`），
> **尚未部署到 41**。本文里的响应样例来自测试环境的真实构造，不是从产线抓的。
> **一句话**：`GET /v1/banner` 给**静态配置**（文案 + 跳转路径 + 运营兜底图）；
> **车图由你们自己合成**；点击按 `jump_path` 本地导航到报告页。

---

## 0. ⚠️ 本期已知欠账 —— 先读这一节

**点过去大概率看不到截图那张报告。** 报告页**还没有**接 AI-Ops 的健康报告接口
（build 21 里 `createHealthReportJob`/`getHealthReportJob` 存在但**零调用**，是死代码；
页面现在走的是 `getReportList`/`getReport` 那条 mall 路由）。

- 这**不是**横幅的缺陷，**是欠账** —— 报告页指标对齐与前端接线共同欠的账，另立票。
- **导航本身是通的**：`/aiPackage/pages/batteryReport/batteryReport` 在客户端
  `pages.json`/`__definePage` 里**已注册**（build 21 实测），`uni.navigateTo` 能打开这个页面。
- 所以你们联调时：**"页面打开了"能验，"页面里是那张报告"现在验不了**。

其余三条边界，同样请当已交付事实接受，不要按直觉改：

| # | 事实 | 后果 |
|---|---|---|
| 1 | **横幅语义是「我的车」，不是「这单的车」** | 横幅画的那台车与报告页那台**可能不是同一台**。**不要假定一致**，也不要用报告页的订单去反推横幅的车 |
| 2 | 车型图**全部是第三方 CDN 外链**（汽车之家 `car2/car3.autoimg.cn`） | 无可用性承诺、有版权风险。图挂了不要当成接口故障 |
| 3 | `/lastChargingOrder` **用不了** | 它硬过滤 `status = 0`（**正在充电**），用它会让横幅「充电中出现、充完就消失」，语义与截图相反 |

**取"最近一笔已完成充电单"** 用现成的 `getOrderUserPage`：
`page=1&size=1&status=1&excludeHomeOrder=1`，取第一条。**不需要改 Java 侧任何仓库。**

---

## 1. 两个请求，一次渲染

横幅由**两个独立来源**合成 —— 我们这边只负责右边那个：

```
GET /v1/banner                                    ← AI-Ops：文案 + 跳转路径 + 兜底图（静态）
GET /charging-pile/chMyCar/myCarList               ← 公司既有端点：我的车（个性化）
GET /charging-pile/chCarSeries/page?name=<车型名>   ← 公司既有端点：车型 → 车图
```

**为什么拆成两半**：AI-Ops 只给**人人同值的运营配置**。一旦让那个端点按人回答，
它就从「运营位」变成「授权查询」，两个变化频率完全不同的东西会焊死在一个响应里。
个性化本来就在你们手上（登录态 + 公司端点），放在前端是**最省一步**的做法，不是推诿。

### 1.1 `GET /v1/banner`（AI-Ops）

```http
GET /v1/banner
Authorization: Bearer <助手只读令牌>
X-Business-Entry: consumer
Accept-Language: zh
```

```json
{
  "type": "banner",
  "language": "zh",
  "count": 1,
  "banners": [
    {
      "code": "battery_report",
      "language": "zh",
      "label": "你有一份待生成智能电池检查报告",
      "description": "想知道你的电池容量衰减多少？",
      "jump_path": "/aiPackage/pages/batteryReport/batteryReport",
      "image_url": null
    }
  ]
}
```

| 字段 | 用途 |
|---|---|
| `code` | 稳定标识（`battery_report`）。别绑 `label`，文案会改 |
| `label` / `description` | 标题与副标题，按 `Accept-Language` 本地化（11 语言，无中文兜底） |
| `jump_path` | **点击目标**，见 §2 |
| `image_url` | **运营兜底图**，见 §1.3 |

**它是静态配置**：响应里**没有订单、没有车辆、没有会话主体**，同一入口下人人同值。
**鉴权与 `/v1/shortcuts` 同形** —— 只读 scope 即可，**不需要管理角色**。

### 1.2 「我的车」

```http
GET /charging-pile/chMyCar/myCarList
tenant-id: <会话租户>
user-id: <uni.getStorageSync("user_info").id>
```

**这条你们已经在调了** —— 与 `utils/iotJavaRequest.js` 里 `getMyCar` 的封装完全一致
（`tenant-id` + `user-id` 两个头，**没有 `Authorization`**）。返回里每台车带
`brand` / `model` / `plateNumber` / `isDef` / `images`。**取默认车**（`isDef`）。

⚠️ `is_def` 在生产库里三种取值都有（`1` / `0` / `NULL`），且 `NULL` 有 117 行。
**不要只判 `=== true`** —— 拿不到 `1` 时用列表第一台，别把横幅画成空的。

### 1.3 车型 → 车图

```http
GET /charging-pile/chCarSeries/page?name=<车型名>&page=1&size=20
```

返回的 `records[]` 里带 `logo`（车系图）。**用 `name` 逐字相等的那一条**。

**命中率（我们实测过，见 §5）**：按「我的车」的 1122 台**个人车**统计 ——
`exact` **97.06%**、`fallback` 0.89%、`miss` 2.05%。

所以**必须有一条回落到运营兜底图的分支**：

```js
const image = carSeriesLogo ?? banner.image_url ?? null
if (!image) {
  // 兜底图也没有 —— 不要画半张卡片，整条横幅不显示
  return
}
```

> **两个数字别搞混**：车图取不到（`miss`，2.05%）与**取到了图但加载失败**（第三方 CDN）
> 是两件事。前者走上面的回落，后者是 `<image>` 的 `error` 事件，同样回落到 `image_url`。

### 1.4 「没有横幅」是正常状态

未发布、停用、或该入口本就没有横幅 ⇒ **HTTP 200**：

```json
{ "type": "banner", "language": "zh", "count": 0, "banners": [] }
```

**不是 404、不是 5xx。** 当前**只有客户端入口（consumer）有横幅**，管家端入口返回空列表。

**配置端点失败时不要显示半个卡片** —— 整个位子不渲染，而不是渲染一张没字的卡。

---

## 2. 点击：走 `jump_path` 本地导航，**与快捷动作完全同一套规则**

**横幅是跳转动作。** 它**不经过**统一助手入口，也**没有**响应 `type`。
判别与写法与 `frontend-jump-path-handoff.md` **逐条相同**，这里只重复最要紧的三条：

```js
if (banner.jump_path) {
  uni.navigateTo({ url: banner.jump_path })   // 原样使用，不拼接、不改域名、不加参数
}
```

1. **路径原样使用**，跨语言是同一个串（`jump_path` **不做国际化**）。
2. **不要为了"保险"再调一次统一入口** —— 那会拿到一个兜底澄清，多一次请求且没有答案。
3. **`navigateTo` 不能打开 tabBar 页面**（要用 `uni.switchTab`，且 `switchTab` 路径后不能带参数）。
   `/aiPackage/pages/batteryReport/batteryReport` 是不是 tabBar 页**由你们确认**。

> **完整的坑清单不在这里复抄** —— 见
> [`frontend-jump-path-handoff.md`](frontend-jump-path-handoff.md) §5 的八条表，
> 以及 §6「服务端不校验页面是否存在」。那两条对横幅**同样适用**，一字不改。

**横幅的 `banners[]` 里不会出现 `question_template` 或 `target_agent_version`** ——
服务端不下发它们。所以不存在"拿 `question_template` 给横幅预填输入框"这个坑：
那个字段根本不在这里。

---

## 3. 横幅与按钮**互不串面**（你们不需要过滤）

`GET /v1/banner` **只**返回横幅行，`GET /v1/shortcuts` **只**返回按钮行。
选面在服务端按行自己的判别字段完成 —— **前端不要做任何过滤**，也不要用
`image_url` 是否为空去猜（那是内部判别字段，不是给客户端用的）。

两条线的行数互不影响：客端入口的按钮仍是**三条**（`case_exploration` /
`smart_diagnosis` / `report_fault`），横幅是**另外**一条。

---

## 4. 快速自检清单

- [ ] 调 `/v1/banner` 只调**一次**，不等个性化接口
- [ ] `count === 0` 时**整条横幅不渲染**（不是渲染空卡片）
- [ ] `isDef` 判 `1`，拿不到时退列表第一台（**别只判 `true`**）
- [ ] 车系图取不到 → 回落 `image_url`；`image_url` 也为 null → **不渲染**
- [ ] 车图**加载失败**（`<image>` `error`）也要回落，不只是"取不到"
- [ ] `jump_path` **原样**传给 `uni.navigateTo`，不拼接不加参
- [ ] 点击后**不调**统一助手入口
- [ ] 确认 `batteryReport` 是否为 tabBar 页（是则改用 `switchTab`）
- [ ] **不要**假定横幅那台车 == 报告页那台车
- [ ] 报告页里没有报告 —— **已知欠账**，见 §0

---

## 5. 交接必须带上的实测数字（都是可复跑的，见 `docs/validation.md`）

| 事实 | 值 | 怎么来的 |
|---|---|---|
| 「我的车」端点可达且可逐条命名 | 3/3 受试身份 http=200，`brand`+`model` 都在 | 真身份直连实测（#590） |
| 身份确实由 `user-id` 头承载 | 伪造 `user-id` ⇒ **0 台车** | 同上的对照 |
| 「最近一笔已完成单」可取 | 3/3 返回 `total>0` 且首条带 `orderNo` | 同上 |
| 车型目录对客户端开放 | 客户端头下 200 且带 `logo` | 同上 |
| **车型命中率（按用户 / 按车数）** | **97.06%**（1089 / 1122） | 逐车型走端点 + 生产库对账 |

**⚠️ 「约 1/6」那个数字是分母错误，别再用它做判断。** 它是把 `brand IS NULL` 的企业车
（1978 行，多为企业用车）也当成了分母；而**空白车型**（`model` 只有空格，114 行）
是数据质量问题，与"目录查不到这台车"是两件事，也不该进分母。
含进去会得到错误的 88.11%。逐条口径见 `docs/validation.md`「#590」。

---

## 6. 相关文档

- **完整接口契约**：`docs/agents/frontend-api-brief.md` **§D.2b 聊天页运营横幅**。
- **跳转动作的坑清单**：`docs/agents/frontend-jump-path-handoff.md`（横幅共用同一套规则）。
- **后端设计与取舍**：`docs/adr/0006-shortcut-actions-extend-to-in-app-navigation.md`。
- **直连实测证据**：#590，`docs/validation.md` + `tools/verify_banner_car_lookup.py`。
- **报告页欠账**：PRD #578 / #592（电池检查指标规格，含三个**产品还未定义**的缺口）。
