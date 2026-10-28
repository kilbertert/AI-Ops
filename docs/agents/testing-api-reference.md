# AI-Ops 客户端 / 管家端接口清单（产品测试用）

> 给测试同事的当前可转发版本：[功能测试接口文档](../testing-api-guide.md)。本页仅保留旧联调记录；不再从下文索取服务令牌或按旧语言限制验收。

> 2026-10-28适用范围：2026-10-19接口参考；新场景/验收范围按当前测试交接。当前入口见[更新说明](../testing-handoff.md)。
> 下文保留原时点事实；其中“当前”“未完成”不得覆盖后继已验收结果。

> **读者**：产品 / 测试。**目的**：不经 Android APK，直接用 HTTP 工具（curl / Postman / Apifox）
> 验证客户端与管家端的功能是否可用。
>
> **状态**：2026-10-19 按 `main` 源码与 41 生产实测整理。
> **前提**：这些接口**本来就是纯 HTTP**，APK 只是它的一个调用方 —— 不走 APK 完全可行，
> 唯一的外部依赖是**一条有效会话**（见 §1）。

---

## 0. 一分钟速览

```text
基址：https://api.mall.qushiyun.com        （41 生产，公网）

必带头：
  consumer：third-session: <正常登录会话>
  operator：Authorization: Bearer <公司个人令牌>，不携third-session
  X-Business-Entry: consumer | operator
  Content-Type: application/json           （POST 才有）

Authorization 按入口不同（见 §0.1）：
  consumer  → 服务器注入，测试方【不要带】
  operator  → 服务器透传正常管家登录取得的个人Bearer
```

> ⚠️ **两个入口的鉴权形状不一样**，这是本节最重要的一条。别把 consumer 的经验套到
> operator 上 —— 见下面两小节。

---

## 0.1 两个入口的鉴权形状（**不同**）

41 的 nginx 按 `X-Business-Entry` 分流到**两个不同的上游**，凭据来源也不同
（依据 `docs/agents/env-41-runbook.md` §1.7 的 `map` 定义）：

| 入口 | 上游 | `Authorization` 谁给 | `X-AIOps-Source-Key` |
|---|---|---|---|
| **consumer**（客户端） | 直连 AI-Ops 网关 | **nginx 注入服务令牌** —— 测试方**不要带** | 不需要 |
| **operator**（管家端） | 先经**公司网关** → AI-Ops | **nginx 透传你带来的那个**（公司用户 JWT）—— **来自正常管家登录** | **必须**，由公司网关那一跳注入 |

⇒ **给测试方的实际含义：**

- **测客户端**：只要一条 `third-session` 就能开工。**不要**带 `Authorization`。
- **测管家端**：使用正常管家密码登录获得个人令牌，并核对目标环境已交付对应节点路线。
  **这不是测试方自己能配的**，必须在开工前和运维确认。

> 📌 **如果运维说「管家端这条链在测试环境还没打通」，那就先只测客户端。**
> 管家新节点场景与C端WF01是不同执行链；其余旧接口以各自契约为准，不能仅凭共用URL判断能力相同。

### 为什么头名必须是 `third-session`（一句话说透）

公网那一跳的 nginx 是这样转发会话的：

```nginx
proxy_set_header X-Third-Session $http_third_session;
```

`$http_<名字>` 按**小写 + 下划线**匹配请求头。所以：

| 你发 | nginx 取到 | 转给网关 | 结果 |
|---|---|---|---|
| `third-session` | 有值 | `X-Third-Session: <值>` | ✅ 200 |
| `X-Third-Session` | **空** | 空 | ❌ 401 |

> ⚠️ **两个名字在不同场景下都"对"**，别被其他文档搞混：**走公网必须 `third-session`**；
> 而**直连网关**（只有我方运维在 41 本机自查时会这么做）时走的是 FastAPI，它把
> `X-Third-Session` 归一成 `x_third_session`，反而认 `X-Third-Session`。
> **测试方一律走公网，所以一律用 `third-session`。**
>
> ✅ **这一条已在 41 的现行配置上核对过**：`nginx -T` 输出里
> `proxy_set_header X-Third-Session $http_third_session;` 实际生效（不是"配置了但没部署"）。

---

**冒烟验证**（拿到会话后先跑这条）：

```bash
curl -i 'https://api.mall.qushiyun.com/v1/faq/catalog' \
  -H 'third-session: <会话值>' \
  -H 'X-Business-Entry: consumer'
# 期望：200，且 body 里有 "entries" 数组
# 若 401 INVALID_ACCESS_TOKEN ⇒ 见 §5 排查表
```

---

## 1. 会话怎么来（唯一的卡点）

**这是外部依赖，测试方自己拿不到，必须由 AI-Ops 运维提供。**

一条会话长这样：一个**裸的 uuid 字符串**（形如 `a1b2c3d4-...`），存放在公司共享 Redis 的
`app:3rd_session:<值>` 键里。你只需要**去掉前缀的那个值**。

向运维索取时，说清这三件事：

| 要说明 | 例子 | 为什么 |
|---|---|---|
| 哪个**租户** | `1942105476598861824` | 不同租户看到不同的数据/智能体 |
| 哪个**入口** | `consumer`（客户端）/ `operator`（管家端） | 同一个账号在两个入口的可见范围**不同** |
| 需要**多久** | 例如「测试到下周三」 | 会话**有时效**，过期就 401 |

⚠️ **会话等同密码**：不要在文档、聊天记录、截图里传播它。测试池里的会话过期后找运维换新的即可。

---

## 2. 客户端（consumer）能测什么

`X-Business-Entry: consumer`

### 2.1 固定问答（同步，最快）

| 端点 | 方法 | 用途 |
|---|---|---|
| `/v1/faq/catalog` | GET | 取全部固定问答目录（28 条） |
| `/v1/faq/recommendations` | GET | 取「推荐问题」列表 |
| `/v1/faq/answer` | POST | 按 `question_id` 直接取答案（**同步返回，无轮询**） |

```bash
# 取目录
curl -s 'https://api.mall.qushiyun.com/v1/faq/catalog' \
  -H 'third-session: <会话值>' -H 'X-Business-Entry: consumer'

# 取某一条的答案（question_id 从目录里拿）
curl -s -X POST 'https://api.mall.qushiyun.com/v1/faq/answer' \
  -H 'third-session: <会话值>' -H 'X-Business-Entry: consumer' \
  -H 'Content-Type: application/json' \
  -d '{"question_id": "consumer.faq.q010"}'
# 期望：200，含 question / answer / format 三个字段
```

### 2.2 统一助手入口（自由提问，异步）

**所有「用户自己打字提问」都走这一个端点**，它内部分流成三种 `type`：

```bash
curl -s -X POST 'https://api.mall.qushiyun.com/v1/assistant/questions' \
  -H 'third-session: <会话值>' -H 'X-Business-Entry: consumer' \
  -H 'Content-Type: application/json' \
  -d '{"question": "充电结束后拔不出充电枪怎么办"}'
```

| 返回 | 状态码 | 怎么处理 |
|---|---|---|
| `type=faq` | **200** | 直接读 `answer`，**流程结束**（同步） |
| `type=qa` | **202** | 拿 `qa_id` 轮询 → `GET /v1/assistant/questions/{qa_id}` |
| `type=diagnosis` | **202** | 拿 `diagnosis_id` 轮询 → `GET /v1/standard/diagnoses/{diagnosis_id}` |
| `type=clarification` | 200 | 缺信息（通常是订单号），按 `missing_fields` 补发 |

**请求体字段**（多一个字段就 422）：

| 字段 | 必填 | 说明 |
|---|---|---|
| `question` | ✅ | 1–4000 字 |
| `order_no` | ❌ | 带订单号的提问会走诊断 |
| `conversation_id` | ❌ | 绑定多轮会话（`conv_` 开头） |
| `shortcut_code` | ❌ | 点击快捷动作时带（如 `case_exploration`） |

**轮询**：按响应里的 `retry_after_ms`（通常是 1000ms）节流。

**停止条件只有一条：`retry_after_ms` 变成 `null`。** 那一刻作业已到终态，不会再更新。
终态不止 `completed` / `failed`：

| 面 | 全部终态 |
|---|---|
| `type=qa`（助手问答） | `completed` / `failed` / `expired` / `cancelled` / `inconclusive` |
| `type=diagnosis`（诊断） | 同上（另有诊断特有的 `failed` 错误码） |
| 健康报告作业 | `completed` / `failed` / `expired` |
| 运维分析作业 | 同上 |

⚠️ **只判 `failed` 会漏掉一半**：`expired`（超过时效被清扫）与 `cancelled`（用户/系统中断）
都是终态，继续轮询只会空转到超时。**判 `retry_after_ms == null` 最稳** —— 它与终态严格对应。

### 2.3 需要订单号的

| 端点 | 方法 | 用途 |
|---|---|---|
| `/v1/orders/{order_no}/access` | GET | 只验「这单我能不能看」，**不返回明细** |
| `/v1/health-report-jobs` | POST | 充电体检报告（异步） |
| `/v1/health-report-jobs/{job_id}` | GET | 轮询报告 |
| `/v1/standard/diagnoses` | POST | 单问诊断（异步） |
| `/v1/standard/diagnoses/{id}` | GET | 轮询诊断 |

```bash
# 体检报告：POST 创建 → 轮询
curl -s -X POST 'https://api.mall.qushiyun.com/v1/health-report-jobs' \
  -H 'third-session: <会话值>' -H 'X-Business-Entry: consumer' \
  -H 'Content-Type: application/json' \
  -d '{"order_no": "<本账号自己的订单号>"}'
# 期望：202 + job_id；再 GET /v1/health-report-jobs/{job_id} 轮询
```

### 2.4 会话与多轮

| 端点 | 方法 |
|---|---|
| `/v1/conversations` | POST 创建 / GET 列表 |
| `/v1/conversations/{id}` | GET 详情 / DELETE 删除 |
| `/v1/conversations/{id}/active-order` | POST 绑定活跃订单（之后追问可省略订单号） |

### 2.5 其他

| 端点 | 方法 | 说明 |
|---|---|---|
| `/v1/assistant/starters` | GET | 开场白（进入会话页时展示） |
| `/v1/shortcuts` | GET | 快捷动作按钮列表 |
| `/v1/banner` | GET | 聊天页横幅 —— ⚠️ **当前无已发布横幅，返回空列表**（是数据状态，不是接口约束），见 §4 |
| `/v1/media/{signed_id}` | GET | 取图片/视频（**签名约 10 分钟过期**） |

---

## 3. 管家端（operator）能测什么

⚠️ **先说清楚凭据**（见 §0.1）：管家端**不是**只带 `third-session` 就能测。
公网那一跳对 `operator` 是**透传你的 `Authorization`**（公司用户 JWT），
所以**所有 operator 请求都必须带 `Authorization: Bearer <公司用户 JWT>`**。
这是运维要提供的东西，不是测试方能自己配的。

`X-Business-Entry: operator` + `Authorization: Bearer <公司用户 JWT>`

### 3.1 与客户端**完全相同**的部分
固定问答、统一助手入口、会话管理、媒体 —— **路径、请求体、响应形状一模一样**，只是入口头与凭据不同。

### 3.2 管家端**独有**

| 端点 | 方法 | 用途 | 额外要求 |
|---|---|---|---|
| `/v1/standard/analyses` | POST | 运维巡检卡（异步） | 必须 `operator` 入口，否则 **403** |
| `/v1/standard/analyses/{id}` | GET | 轮询结果 | 同上 |

```bash
curl -s -X POST 'https://api.mall.qushiyun.com/v1/standard/analyses' \
  -H 'Authorization: Bearer <公司用户 JWT>' \
  -H 'X-Business-Entry: operator' \
  -H 'Content-Type: application/json' \
  -d '{"card": "failure_rate",
       "window_start": "2026-10-19T00:00:00+08:00",
       "window_end":   "2026-10-19T12:00:00+08:00"}'
# card 可选值：failure_rate | offline | fault_code | payment
# 期望：202 + analysis_id
```

### 3.3 「客户案例」在管家端**已恢复**（2026-10-19，可测）

`case_exploration` 现在**在管家端正常可用**，绑定的宣传智能体版本与客户端不同
（管家端 `#v3`、客户端 `#v2`——这是设计，两个入口各自冻结自己的版本）。

**已实测的三层证据：**

| 层 | 证据 |
|---|---|
| 数据侧 | 41 上 `operator` 入口的有效动作 = **客户案例(10) / 订单检测(20) / 运维巡检(30)**，`case_exploration` 的 `target_agent_version` = `agt_ed443cae…#v3` |
| 路由侧 | 线上代码实跑 `_promo_route(platform="operator")` → 解析出 `#v3`；`select_promo_agent` 也解析成功 |
| 端到端 | 同机制的宣传作业经真实会话执行：**`completed` + `retrieval_status=found`**，产出 **12 个内容块（含 2 个 image + 6 个 reference）**，耗时 51s |

**怎么测**（预期与客户端一致，但凭据是 JWT 不是会话）：

```bash
curl -s -X POST 'https://api.mall.qushiyun.com/v1/assistant/questions' \
  -H 'Authorization: Bearer <公司用户 JWT>' \
  -H 'X-Business-Entry: operator' \
  -H 'Content-Type: application/json' \
  -d '{"question": "我想看看客户案例"}'
# 期望：202 type=qa → 轮询 → completed，retrieval_status=found
```

> ⚠️ **前置条件**：管家端需要公司用户 JWT（由公司网关注入来源密钥），
> 且该身份要有运营商站点范围。见 §0.1 —— 这是运维要准备的，不是测试方自己能解决的。
> 若拿 consumer 那条链去测（只有 `third-session`），会得到 **401/503**，那是**凭据不对**，
> 不是动作没恢复。

---

## 4. 已知的「正常但反直觉」的行为（测试前必读）

| 现象 | 是 bug 吗 | 说明 |
|---|---|---|
| `/v1/banner` 返回 `count: 0` | ❌ 不是 | **当前生产上没有已发布的横幅行**，路由是好的。空态是 200，不是 404。运营发布横幅后即非空 |
| 管家端「客户案例」返回 `unavailable` | ⚠️ 看会话形状 | 该动作**已恢复**（§3.3）。若仍 `unavailable`，先确认会话是 operator 形状 —— `self` 形状拿不到站点范围，宣传路由会 fail-closed |
| 泰语 / 高棉语提示不支持语言 | ⚠️ 按路线判断 | 当前WF01与明确订单诊断不应拒绝；仅无明确订单的旧关键词路线保留该边界 |
| 订单不存在 vs 无权查看，都返回 **404** | ❌ 不是 | 刻意不区分，避免泄露「这单存在」 |
| 字段写错返回 **422** 不是 400 | ❌ 不是 | **请求体**校验一律 422（`extra="forbid"`）。⚠️ **路径参数**例外：`/v1/orders/{no}/access` 与 charging-status 的订单号形状非法返回 **400 `INVALID_ORDER_NO`** |
| 多传一个字段就 422 | ❌ 不是 | `extra="forbid"`，严格拒绝 |
| `type=qa` 的 `result` 里读不到 `text` | ❌ 不是 | 用 `blocks[]`；读 `result.text` 的旧写法已废弃 |
| 同一会话连续提问得到 `409 CONVERSATION_BUSY` | ❌ 不是 | 一次只能有一个生成中的轮次，等它结束 |
| 轮询拿到 `failed` | ⚠️ 看错误码 | `DIAGNOSIS_BLOCKED` = 模型配额受限，可重试；其他见错误码 |
| 轮询拿到 `expired` / `cancelled` | ❌ 不是 bug | 都是**终态**（超时清扫 / 被中断）。重新 POST 一个新作业即可 |
| `/v1/orders/{no}/charging-status` 返回 404 | ❌ 不是 | 该能力**默认关闭**（未配业务地址），不是路由不存在 |

---

## 5. 测试踩坑排查表

| 症状 | 原因 | 怎么办 |
|---|---|---|
| `401 INVALID_ACCESS_TOKEN` | 会话过期 / 头名写错 | 换新会话；头名**必须**是 `third-session`（全小写连字符） |
| `401`，且你写了 `X-Third-Session` | nginx 的 `$http_third_session` 按**小写+下划线**取头，`X-Third-Session` 取到空值后被丢掉 | 改成 `third-session`（**公网实测：前者 200，后者 401**） |
| `401`，且用了 `aops_` 令牌 | 服务令牌不是用户凭据 | 去掉，公网入口自动注入 |
| `403 PLATFORM_FORBIDDEN` | 入口值非法（如 `operator-admin`） | 只允许 `consumer` / `operator` |
| `403 OPERATOR_ENTRY_REQUIRED` | 调管家端接口时没带 `operator` | 加 `X-Business-Entry: operator` |
| `409 PLATFORM_AMBIGUOUS` | 账号同时属于两个平台且没带入口头 | 显式带 `X-Business-Entry` |
| `404 ORDER_NOT_FOUND` | 订单不属于当前账号 | 换本账号自己的订单 |
| **走错了内容域但不报错** | **漏传 `X-Business-Entry`** | 见下方专条 —— 公网与直连行为**不同** |
| 收到的错误码带空格（`INVALID ACCESS TOKEN`） | 是中间层返的，不是 AI-Ops | 本项目错误码一律下划线格式 |

---

## 5.1 漏传 `X-Business-Entry` 会怎样（**公网与直连不同，别混**）

这条是最容易误判的，因为它**两条路行为不一样**：

**经公网入口（`api.mall.qushiyun.com`）** —— 41 的 nginx 有一个 `map`，**默认值是 `consumer`**。
所以漏传时请求会被**静默改写成 `consumer`**。表现是「不报错，但拿到的数据不对」。

**直连网关**（只有我方在 41 本机自查时会这么做）—— 没有 nginx 那一跳，行为由代码决定：

| 这个账号可用的平台 | 不传入口头的结果 |
|---|---|
| 只有一个 | **自动选中它**（日志 `reason=唯一可用平台`） |
| 两个都有 | **409 `PLATFORM_AMBIGUOUS`**，提示「business entry is required」 |

⇒ **给测试方的结论只有一条：永远显式带 `X-Business-Entry`。** 带上它，两条路行为一致；
不带它，公网会悄悄给你 consumer 的数据，而你可能以为自己测的是管家端。

---

## 6. 环境与版本

| 项 | 值 |
|---|---|
| 41 生产入口 | `https://api.mall.qushiyun.com` |
| 95 环境入口 | `https://api.qumall.qushiyun.com`（**另一套**，会话/数据源不通用） |
| 健康检查 | `GET /health`（无需任何头）→ `{"ok":true,"version":"0.1.0+<commit>"}` |
| 版本怎么看 | `/health` 的 `version` 字段带 commit 短 sha，可自证跑的是哪一版 |

⚠️ **两个环境的会话不通用**，不要拿 95 的会话打 41。

---

## 7. 这份文档与既有文档的关系

仓库里已有九份**单主题**交接文档（面向前端组与 BFF），但**没有一份是给测试方的一站式清单**。
本文件就是那个缺口。要更深的字段级契约时，按需查：

| 想要什么 | 看哪份 |
|---|---|
| 固定问答的完整字段与错误码 | `docs/faq-api.md` |
| 体检报告 / 诊断的完整契约 | `docs/standard-api-contract.md` |
| 前端视角的联调总览（最厚） | `docs/agents/frontend-api-brief.md` |
| 统一助手的三种 type 怎么触发 | `docs/agents/assistant-type-trigger-guide.md` |
| 管家端专属说明 | `docs/agents/frontend-operator-handoff.md` |
| 取消 / 中断 | `docs/agents/assistant-cancel-handoff.md` |
| 会话（thirdSession）的权威语义 | `docs/agents/butler-session-contract.md` |
| 横幅与跳转动作 | `docs/agents/frontend-banner-handoff.md` |

> **冲突时以哪份为准**：`faq-api.md` 与 `standard-api-contract.md` 是**契约**；
> 其余是交接与解释。本文件是**索引与用法**，字段级冲突以那两份契约文档为准。
