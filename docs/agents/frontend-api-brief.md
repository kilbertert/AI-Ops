# AI-Ops 前端联调总览

> 状态：当前有效，2026-09-12。`api.mall.qushiyun.com` 的 `/v1/*` 公网入口已切到 41（`47.97.160.153`）；
> 41 的 KB/RAG 通过受限回环隧道复用移动云 36 单实例，诊断数据源留在 41。
> `api.qumall.qushiyun.com` 是 95 环境入口，继续使用 95 自己的 Gateway、会话库和诊断数据源。
> 读者：前端组、BFF/Java 组、联调测试。
> 权威契约：固定问答见 [固定问答标准接口](../faq-api.md)；健康报告与单问诊断见 [标准后端接口报告](../standard-api-contract.md)。本文含三条线的完整输入/输出定义，冲突时以两份契约文档为准。

## 0. 一图看懂：谁调谁

```text
小程序/浏览器
    │  只带现有登录态（thirdSession），不持有任何 AI-Ops 令牌
    ▼
业务 BFF / Java 后端（41：https://api.mall.qushiyun.com；95：https://api.qumall.qushiyun.com）
    │  1. 验证用户登录态
    │  2. 按访问入口决定 X-Business-Entry: consumer | operator
    │  3. 以服务身份调用 AI-Ops，转发 thirdSession
    ▼
对应环境 Nginx（同域 /v1/*，注入服务身份）
    │  41: api.mall.qushiyun.com → 41:8788；95: api.qumall.qushiyun.com → 95:8788
    ▼
AI-Ops（41 本机 127.0.0.1:8788）
    │  解析会话 → 判定平台身份 → 隔离内容域 → 返回数据
    ▼
前端拿到数据渲染
```

**前端绝对不做**的事：

- 不保存、不打印、不打包 AI-Ops 服务令牌（`aops_*` 也不行）；
- 不提交 `platform`、`user_id`、`tenant_id`、角色、B 端主体 ID——这些全部由 BFF/AI-Ops 从会话推导；
- 不直连 AI-Ops 网关地址；前端 Base URL 必须按业务环境选择 BFF 域名（41 使用
  `https://api.mall.qushiyun.com`，95 使用 `https://api.qumall.qushiyun.com`）。

## 1. 三条业务线怎么选

| 用户动作 | 走哪条线 | 接口 | 同步/异步 |
|---|---|---|---|
| 点击推荐快捷问 | 固定问答 | `POST /v1/faq/answer` | **同步**，直接拿答案 |
| 打开 FAQ 页 | 固定问答 | `GET /v1/faq/recommendations`、`GET /v1/faq/catalog` | 同步 |
| 查看充电体检报告 | 健康报告 | `POST /v1/health-report-jobs` + 轮询 | **异步**（作业模型） |
| 自由输入/订单问题提问 | 单问诊断 | `POST /v1/standard/diagnoses` + 轮询 | **异步**（作业模型） |

分流口诀：**点击固定问题 → FAQ 同步答案；自由文本/订单问题 → 单问诊断；报告 → 健康报告作业。** 不可混用：不能把自由文本塞进 FAQ 接口，不能把固定推荐转成模型问题。

## 2. 公共约定（三条线通用）

### 2.1 BFF 调 AI-Ops 的请求头

**⚠️ 头名大小写有坑（2026-09-07 实测修正）**：会话头**必须用 `third-session`（全小写连字符）**，不能写成 `X-Third-Session`。公司 120 的 nginx 默认丢弃带下划线的自定义请求头（`underscores_in_headers off`），`X-Third-Session` 会被静默丢掉 → 网关收不到会话 → **401 `INVALID_ACCESS_TOKEN`**。`Authorization` 头不受此限制（无下划线）。

```http
# 前端/客户端 → BFF（或直连 BFF 域名）的最小正确载荷（实测 200）：
third-session: <当前用户的有效 thirdSession>     # 全小写连字符！不能写 X-Third-Session
tenant-id: <租户ID>                               # 例如 2019588094906601472
```

```http
# BFF → AI-Ops（BFF 层注入服务身份，不下发给前端）：
Authorization: Bearer <aiops-service-token>     # BFF 的服务身份，不给前端
third-session: <当前用户的有效 thirdSession>     # BFF 验证后原样转发（同样全小写）
X-Business-Entry: consumer                       # consumer | operator，按访问入口设置
Content-Type: application/json                   # POST 时
```

- **`X-Business-Entry` 由 BFF 根据用户从哪个入口进来设置**，不是前端传的选项。只允许 `consumer` / `operator`。
- 身份只有一个可用平台时可省略；联调阶段建议 BFF 始终显式设置，避免双平台用户得到 `409 PLATFORM_AMBIGUOUS`。
- **`Authorization` 与 `third-session` 二者缺一即 `401 INVALID_ACCESS_TOKEN`**——仅带服务令牌、漏传用户会话时，网关按"访问令牌校验失败"整体拒绝（实测确认），不是降级为匿名或仅服务身份调用。
- 前端实际调用的 URL、鉴权方式由 BFF 决定；建议 BFF 保留 `/v1/*` 路径与响应体结构原样透传，前端零转换。
- 实测对照（2026-09-07，公网 `api.qumall.qushiyun.com`，同一有效 thirdSession）：`third-session` → 200；`X-Third-Session` → 401。网关错误码一律下划线格式（`INVALID_ACCESS_TOKEN`），**不会出现带空格的 `INVALID ACCESS TOKEN`**——若收到那个，是中间 BFF/网关自己返的，不是 AI-Ops。

### 2.2 错误响应统一形状

```json
{"error": {"code": "FAQ_NOT_FOUND", "message": "...", "retryable": false}}
```

| HTTP | 通用处理 |
|---:|---|
| 401 | 登录态/服务认证失效 → 引导重新登录 |
| 403 | 入口与身份不符 → 返回业务入口页 |
| 404 | 资源不存在或不在授权范围 → 提示"未找到"，不区分原因 |
| 409 | 平台无法唯一确定 → 联系 BFF 排查，前端不要自行切换 |
| 422 | 请求字段错误 → 修正后重发，不要原样重试 |
| 429 | 限流 → 稍后重试 |
| 503 | 依赖暂时不可用 → `retryable: true`，稍后重试并保留关联 ID |

> 字段类校验失败(缺失、格式错误、多余字段、类型不符)**一律 422**,不存在 400 分支——三条业务线的请求体都走 Pydantic 校验,任何一项不满足都映射为 `INVALID_REQUEST`。两份契约文档中"400/422"的写法以本条为准(实测确认)。

### 2.3 状态字段（异步线通用）

健康报告作业与单问诊断都是"创建 → 轮询"模型：

```text
queued → running → completed | inconclusive | failed | expired   （诊断）
queued → running → completed | failed | expired                  （报告作业）
```

- 创建响应都带 `retry_after_ms`，按它节流轮询；不要密集轮询。
- 只有 `completed`/`inconclusive` 时 `result`/`report` 才有值。
- `expired` 表示结果超过保留期，让用户重新发起。
- 重复创建同一订单的报告作业会复用未过期作业（幂等），不会重复计算。

### 2.4 语言标识（`Accept-Language`，国际化）

- 前端按当前用户语言在请求头携带 `Accept-Language`（现有行为保留，如 `en`、`zh-CN`），AI-Ops 以此判断输出语言。
- 解析规则：按 RFC 7231 取 q 值最高的受支持语言；区域/文字子标签折叠（`en-US → en`、`pt-BR → pt`）；受支持集合 `zh/en/de/fr/es/pt`；缺失、为空、`*` 通配或不受支持的语言一律回退 `zh`，同权重取先出现者。
- 响应回显：FAQ 三个只读端点（recommendations/catalog/answer）与统一问答入口（faq 200、qa 202、qa 轮询、qa 列表、diagnosis 202）均带 `language` 字段，前端可据此核对生效语言。
- FAQ 内容已按语言输出（2026-09-12 起）：recommendations 的 `title`、catalog 的条目、answer 与 faq 短路分支的 `question/answer` 会返回 `Accept-Language` 对应语言（en/de/fr/es/pt）；中文（zh）为权威回退，operator 端与缺失翻译条目保持中文。
- 模型回答的语言注入已全链路实测（2026-09-14 起）：qa 自由提问（`Accept-Language: en` 轮询 completed，text 为英文知识库回答）与订单诊断（summary/root_cause/limitations/next_steps 英文书写、id/编号保持原样）均已在 41 公网真实会话下验证；知识库片段与证据原文保持原样；检索未命中/不可用的兜底提示同样本地化。
- `error.code` 与 HTTP 语义不受语言影响（保持英文）。

## 3. 固定问答（FAQ）

> 详见 [固定问答标准接口](../faq-api.md)。一问一答，不建会话、不查订单、不调模型、无 `job_id`/`diagnosis_id`。

### 3.1 GET /v1/faq/recommendations — 推荐问题（页面加载）

返回当前平台的完整候选清单（客户端 28 条 / 管家端 17 条），前端可按 `question_id` 自选子集与顺序，但**不得**把答案预存进推荐配置。

**响应字段**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `platform` | string | 本次请求判定的平台：`consumer` \| `operator` |
| `available_platforms` | string[] | 当前身份可用的内容域集合；仅元数据，不代表可跨平台访问 |
| `faq_version` | string | 目录版本（如 `2026.09.04`） |
| `recommendations[]` | object[] | 推荐项列表 |

`recommendations[]` 元素：

| 字段 | 类型 | 说明 |
|---|---|---|
| `question_id` | string | 稳定问题标识，格式 `<platform>.faq.qNNN` |
| `title` | string | 展示标题 |
| `sort` | int | 后端默认排序；前端可自行调整 |

```json
{
  "platform": "consumer",
  "available_platforms": ["consumer"],
  "faq_version": "2026.09.04",
  "recommendations": [
    {"question_id": "consumer.faq.q001", "title": "快充桩、超充桩与慢充桩有什么区别？我的车应该选哪种？", "sort": 1}
  ]
}
```

### 3.2 POST /v1/faq/answer — 点击推荐 → 同步答案

**请求体**（`Content-Type: application/json`，多余字段直接 422）：

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| `question_id` | string | 是 | 1–128 字符，格式 `^[a-z]+\.[a-z0-9-]+\.q[0-9]{3}$`（如 `consumer.faq.q001`） |

```json
{"question_id": "consumer.faq.q001"}
```

**响应** `200`，字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `platform` | string | 同 3.1 |
| `available_platforms` | string[] | 同 3.1 |
| `faq_version` | string | 同 3.1 |
| `question_id` | string | 回显请求的问题 ID |
| `question` | string | 问题完整文本 |
| `answer` | string | **固定纯文本**答案，含换行；按文本渲染，不作为 HTML |
| `format` | string | 当前固定为 `text` |

```json
{
  "platform": "consumer",
  "available_platforms": ["consumer"],
  "faq_version": "2026.09.04",
  "question_id": "consumer.faq.q001",
  "question": "快充桩、超充桩与慢充桩有什么区别？我的车应该选哪种？",
  "answer": "场站内的充电桩主要分为以下三类……",
  "format": "text"
}
```

### 3.3 GET /v1/faq/catalog — 完整目录（FAQ 页）

**响应字段**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `platform` / `available_platforms` / `faq_version` | — | 同 3.1 |
| `entries[]` | object[] | 当前平台全部条目 |

`entries[]` 元素（比推荐项多了答案正文）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `question_id` | string | 同 3.1 |
| `question` | string | 问题完整文本 |
| `answer` | string | 固定纯文本答案 |
| `format` | string | `text` |

普通推荐区不需要调它。

### 3.4 FAQ 专属错误

| HTTP | `error.code` | 前端处理 |
|---:|---|---|
| 404 | `FAQ_NOT_FOUND` | ID 未知/下线/跨平台 → 刷新推荐列表 |
| 403 | `PLATFORM_FORBIDDEN` | 当前入口与身份不符 → 返回业务入口页 |
| 409 | `PLATFORM_AMBIGUOUS` | 多主体无法唯一确定 → 联系 BFF 排查 |
| 503 | `PLATFORM_UNAVAILABLE` | 身份映射依赖不可用 → 稍后重试 |

## 4. 健康报告（充电体检单）

> 详见 [标准后端接口报告 §4](../standard-api-contract.md)。确定性计算，非模型生成。

### 4.1 POST /v1/health-report-jobs — 创建计算作业

**请求体**（多余字段直接 422）：

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| `order_no` | string | 是 | 1–128 字符，只允许字母/数字/`_`/`.`/`:`/`-` |

```json
{"order_no": "2094370061724549120"}
```

**响应** `202`，字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `job_id` | string | 作业标识，`hrj_` 前缀；轮询用它 |
| `order_no` | string | 回显订单号 |
| `rule_version` | string | 计算规则版本（如 `health-v2`） |
| `status` | string | `queued` \| `running` \| `completed` \| `failed` \| `expired` |
| `retry_after_ms` | int \| null | 轮询间隔；非终态固定 1000，终态为 null |
| `report` | object \| null | 仅 `completed` 时有值，见 4.3 |
| `error` | object \| null | 仅 `failed` 时有值：`{code, message, retryable}` |
| `created_at` / `updated_at` / `completed_at` | string | ISO 8601 时间戳 |

```json
{
  "job_id": "hrj_9f2c...",
  "order_no": "2094370061724549120",
  "rule_version": "health-v2",
  "status": "queued",
  "retry_after_ms": 1000,
  "report": null,
  "error": null,
  "created_at": "2026-09-04T10:00:00+00:00",
  "updated_at": "2026-09-04T10:00:00+00:00",
  "completed_at": null
}
```

### 4.2 GET /v1/health-report-jobs/{job_id} — 轮询作业

路径参数 `job_id`；响应结构与 4.1 完全相同。`status=completed` 时读 `report`。

**专属错误**：`404 REPORT_JOB_NOT_FOUND`（作业不存在/不属于当前调用者）、`503 REPORT_JOB_UNAVAILABLE`、`503 ORDER_AUTHORIZATION_UNAVAILABLE`。

### 4.3 report 对象字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `order_no` | string | 订单号 |
| `order_window` | object | `{started_at, stopped_at}`，订单起止时间（ISO 8601） |
| `summary` | string | 固定模板摘要，可直接展示 |
| `indicators[]` | object[] | 单项指标，见下表 |
| `completeness` | number | 数据完整度 `0..1` |
| `rule_version` | string | 计算公式版本 |
| `data_as_of` | string | 本次计算实际使用数据的时间 |
| `source_summary` | object | 各数据源可用状态：`order_snapshot` / `telemetry` / `protocol` / `vehicle_capacity` → `available` \| `not_requested` \| `unavailable` |

`indicators[]` 元素：

| 字段 | 类型 | 说明 |
|---|---|---|
| `code` | string | 稳定指标代码（如 `stop_reason`） |
| `status` | string | `normal` \| `attention` \| `abnormal` \| `unavailable`，按此渲染，**不解析中文阈值** |
| `value` | string \| number | 指标值 |
| `unit` | string \| null | 单位 |
| `reference` | string \| null | 参考条件 |
| `reason_code` | string \| null | 不可用/异常的原因代码 |

**典型耗时**：秒级；轮询间隔按 `retry_after_ms`（1s）。

## 5. 单问诊断（自由文本/订单问题）

> 详见 [标准后端接口报告 §5](../standard-api-contract.md)。一问一诊断，非多轮会话。

### 5.1 POST /v1/standard/diagnoses — 创建诊断

**请求体**（多余字段直接 422）：

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| `order_no` | string | 是 | 1–128 字符，同 4.1 格式 |
| `question` | string | 是 | 1–4000 字符 |
| `indicator_code` | string | 否 | ≤64 字符，`^[a-z][a-z0-9_]{0,63}$`；仅提供提问上下文 |

```json
{
  "order_no": "2094370061724549120",
  "question": "为什么这次充电提前停止？",
  "indicator_code": "temperature_balance"
}
```

不能提交 `score`、曲线、报告、`user_id`、`tenant_id` 作为可信输入。

**响应** `202`，字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `diagnosis_id` | string | 诊断标识，`dx_` 前缀；轮询用它 |
| `order_no` | string | 回显 |
| `question` | string | 回显 |
| `indicator_code` | string \| null | 回显 |
| `status` | string | `queued` \| `running` \| `completed` \| `inconclusive` \| `failed` \| `expired` |
| `retry_after_ms` | int \| null | 非终态 1000 |
| `result` | object \| null | 仅 `completed`/`inconclusive` 有值，见 5.3 |
| `error` | object \| null | 仅 `failed`/`expired` 有值 |
| `created_at` / `updated_at` / `completed_at` | string | ISO 8601 |

```json
{
  "diagnosis_id": "dx_5b8a...",
  "order_no": "2094370061724549120",
  "question": "为什么这次充电提前停止？",
  "indicator_code": null,
  "status": "queued",
  "retry_after_ms": 1000,
  "result": null,
  "error": null,
  "created_at": "2026-09-04T10:01:00+00:00",
  "updated_at": "2026-09-04T10:01:00+00:00",
  "completed_at": null
}
```

### 5.2 GET /v1/standard/diagnoses/{diagnosis_id} — 轮询诊断

响应结构同 5.1。历史列表：`GET /v1/standard/diagnoses?limit=50`（默认 limit=50，返回 `diagnoses[]` 摘要数组，字段为 `diagnosis_id/order_no/question/indicator_code/status/created_at/updated_at`）。

**专属错误**：`404 DIAGNOSIS_NOT_FOUND`、`503 DIAGNOSIS_UNAVAILABLE`。

### 5.3 result 对象字段（AgentDiagnosis）

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | string | `"1.0"` |
| `incident_id` | string | 事件标识 |
| `order_no` | string | 订单号 |
| `tenant_id` | string \| null | 租户（已脱敏语境） |
| `status` | string | 诊断结论状态 |
| `summary` | string | 摘要（≤2000 字符），可直接展示 |
| `root_cause` | string | 根因分析（≤4000 字符） |
| `confidence` | string | 置信度等级 |
| `evidence_ids[]` | string[] | 证据引用 ID（仅引用，不含原始数据） |
| `hypotheses[]` | object[] | 假设列表（≤10 条） |
| `limitations[]` | string[] | 本次诊断的限制说明（≤30 条） |
| `failed_sources[]` | string[] | 不可用数据源清单（≤20 个） |
| `next_steps[]` | string[] | 建议后续步骤（≤20 条） |

诊断结果不暴露内部 run、provider、SQL、原始报文、凭据。

**典型耗时**：几十秒到分钟级；轮询间隔按 `retry_after_ms`。

## 6. 端到端时序（小程序典型流）

```text
1. 打开智能客服页
   GET /v1/faq/recommendations          → 渲染推荐快捷问（同步，秒回）

2. 用户点击某推荐问
   POST /v1/faq/answer                  → 同步拿到 answer，直接渲染成消息气泡

3. 用户自由输入"这次充电为什么提前停了"，选了订单
   POST /v1/standard/diagnoses          → 202，拿 diagnosis_id
   GET  /v1/standard/diagnoses/{id}     → 按 retry_after_ms 轮询
   status=completed → 渲染诊断结果

4. 用户点开该订单的"充电体检单"
   POST /v1/health-report-jobs           → 202，拿 job_id
   GET  /v1/health-report-jobs/{id}     → 按 retry_after_ms 轮询
   status=completed → 渲染报告/雷达/曲线
```

以上每一步的 Authorization 头都由 BFF 注入，前端只传业务参数。

## 7. 联调检查表

- [ ] 前端未保存、打印或打包 AI-Ops 服务令牌。
- [ ] BFF 验证现有 thirdSession，并转发同一值。
- [ ] BFF 根据路由入口设置 `X-Business-Entry`，前端请求体没有 `platform`。
- [ ] 推荐配置只保存 `question_id/title/sort`，不保存答案。
- [ ] 点击推荐只调 FAQ 答案接口，成功响应不轮询。
- [ ] 答案按纯文本保留换行，不作为 HTML 渲染。
- [ ] `FAQ_NOT_FOUND` 会刷新清单，不显示另一平台内容。
- [ ] 自由输入与订单问题走 `/v1/standard/diagnoses`，不走 FAQ。
- [ ] 异步作业按 `retry_after_ms` 节流轮询，不密集轮询。
- [ ] 报告 `indicators.status` 按枚举渲染，不解析中文阈值、不自行重算。
- [ ] QA 结果按 `blocks[]` 类型分派渲染，**不解析 Markdown、不从文本提取 URL**。
- [ ] 媒体 `media.url` 直接当 `src`；`media: null` + `unavailable: true` 时保留文本与引用并显示"资源不可用"。

## 8. 常见坑（来自真实验收）

- **`aops_*` 设备令牌会被拒绝**：标准 API 侧收到 `aops_` 前缀 token 一律 401。那是旧 Gateway 设备流（`/v1/runs`），三条标准业务线都不用它。
- **Apifox 手测 422**：请求体多一个字段都不行（`extra="forbid"`），且 `Content-Type: application/json` 必须放在 Headers，不能塞 Query 参数。
- **404 不区分"不存在"与"无权限"**：这是故意的资源存在性隐藏，前端统一提示"未找到"即可。
- **旧接口迁移**：`GET /v1/recommendations?category=...` → `GET /v1/faq/recommendations`；点击推荐 → `POST /v1/runs` 改为 `POST /v1/faq/answer`；`GET /v1/reports/pending`、`GET /v1/reports/charging-health` 不在当前契约，改走 `/v1/health-report-jobs`。
- **媒体 URL 会过期**：`/v1/media/...` 签名有效期约 10 分钟，且智能体停用/重新发布后立即失效——**不要缓存媒体 URL**，每轮渲染用当轮 `blocks[]` 里的新 URL；过期拿到 403/404 时显示占位即可，不要重放旧 URL。

## 9. 当前真实验收状态（2026-09-04）

测试 Gateway 已部署，公网健康检查通过。使用当前真实有效 C 端会话完成：

- consumer 推荐接口 HTTP 200，返回 28 条候选；
- consumer 目录接口 HTTP 200，返回 28 条目录；
- consumer 固定答案 HTTP 200，返回 `text` 格式答案；
- consumer 请求 operator 问题 ID 返回 404 `FAQ_NOT_FOUND`；
- 同一用户请求 operator 入口返回 503 `PLATFORM_UNAVAILABLE`。

当前可解析会话中尚未找到具备唯一 B 端主体映射的真实样本，因此管家端成功响应仍待一个有效管家用户会话。该缺口不影响客户端联调，也不能通过伪造 `platform` 或临时扩大权限绕过。

## 9.5 QA + blocks[] 媒体验收状态（2026-09-12 更新，可联调）

- **联调环境已就绪**：41 的 `api.mall.qushiyun.com/v1/*` 已切到 41 Gateway；95 的
  `api.qumall.qushiyun.com/v1/*` 保持 95 Gateway 和独立数据面。41 通过受限隧道复用移动云
  36 的 RAG/KB 单实例。41 已迁入联调租户的已发布客服智能体及绑定的图片（PNG）/视频
  （MP4）知识库；但当前 RAGFlow embedding 与模型调用均返回百炼 `Arrearage`，当前
  自由 QA 和媒体 `blocks[]` 需恢复 provider 后才能复验。
- 联调用 C 端会话（thirdSession）与测试问题由服务侧提供（会话有时效，失效时向运维索取新的）。
- `qa` 作业返回 `blocks[]` + `retrieval_status`；`/v1/media/{id}` 支持 Range（视频分段播放）、短时签名（约 10 分钟）、停用即失效。多轮会话 `/v1/conversations`（#180）可用，含 active-order 绑定与 409 忙碌语义。
- **视频播放历史证据**：36/95 入口的服务端租户绑定修复（PR #196）与两层反代 Range
  透传曾验收 `206/416`；该证据不等于当前 41 的媒体链路已通过。41 需在 embedding
  provider 恢复后重新取得 video block，再复验 `<video src={media.url}>`。
- 未配置 KB 栈的租户提问会回落纯文本（`blocks[]` 只有 text 块，无 media）；每个租户需要服务侧先完成智能体发布与知识库绑定。

## 9.6 C 端接口消费者复验（2026-09-12）

环境：2026-09-12T14:50:05+08:00，公网 `https://api.qumall.qushiyun.com`（95/36 历史
canary 入口），已合并的 PR #196 运行版本；请求只带前端应有的 `third-session` 与
`tenant-id`，不带服务令牌。当前 41 结果见本页 §9.5 及 `docs/validation.md`。

- FAQ：推荐、目录均为 `200`/28 条；固定答案为 `200`/`text`；统一助手的快捷问分支为
  `200 type=faq`，可同步渲染。
- QA：自由问返回 `202`，轮询至 `completed/found`，`blocks[]` 含 text、video、reference；
  QA 历史可读且包含新作业。签名视频 Range 为 `206`，超范围为 `416`。
- 会话：创建 `201`、列表可见、删除 `200`、删除后读取统一 `404 CONVERSATION_NOT_FOUND`。
- 错误头：将 `third-session` 错写为 `X-Third-Session` 得到 `401 INVALID_ACCESS_TOKEN`，
  前端/BFF 必须继续使用全小写连字符头。
- 订单线边界：当前联调会话对测试订单创建健康报告得到 `404 ORDER_NOT_FOUND`，诊断历史为空；
  这是授权隔离的正确行为，不代表健康报告或诊断成功路径已通过。补验需要业务方提供该会话
  有权访问的一笔订单，不应通过伪造订单号或扩大数据范围绕过。

## 10. 前端真实链路诊断（2026-09-05，Pyrovolt Move 1.0.3 APK 实测）

> 背景：前端反馈"都没按要求传参、最基础的推荐问题用不了"。经对 APK（`pyrovolt.move.qushiyun`，PGYer v1.0.3 build 5）逆向与服务器实测，**根因不在前端传参，在部署链路**。诊断明细见 GitHub issue《前端真实链路三断点》。

### 10.1 前端实际架构（已确认）

- APK 为 uni-app 原生打包，AI 客服页面（`aiPackage/pages/chat/chat`、`aiPackage/pages/batteryReport/batteryReport`）**打包进 APK 本体**，不在服务器 H5 上；
- AI 客户端模块 `aiPackage/api/aiops.js`，`APIURL = https://xyh5.xyseeker.com`（"95 环境"移动云），`tenantId = 2019588094906601472`（平高充电桩）；
- 请求头：`tenant-id` / `third-session`（登录响应 `thirdSession` 字段存 storage 后回传）/ `client-type: APP` 等，**不含** `Authorization`，**不含** `X-Business-Entry`；
- 接口封装与本文档契约完全一致：`/v1/faq/recommendations|catalog|answer`、`/v1/health-report-jobs`、`/v1/standard/diagnoses`，字段名 `question_id`/`order_no`/`indicator_code` 均正确。

### 10.2 三个断点

| # | 断点 | 实测证据 | 后果 |
|---|---|---|---|
| 1 | **`xyh5.xyseeker.com` 没有部署 `/v1/*` 服务**（致命） | `GET /v1/faq/recommendations` 返回 200 但 body 是宝塔默认页 HTML；`POST /v1/standard/diagnoses` 被 nginx 405。同域名的 Java BFF（`/work/router/rest`、`/charging-pile/*`、`/mallapi/*`）均在线，唯独 `/v1/*` 无反代 | 前端永远拿不到 JSON，表现为"传参错误/接口用不了" |
| 2 | **APK 直连 BFF 域名，缺服务身份头** | APK 不发 `Authorization`；即使 BFF 反代 `/v1/*`，纯 nginx 转发也会因缺 `Authorization`（→401 `ACCESS_TOKEN_REQUIRED`）与 `X-Third-Session` 头名不匹配（APK 发的是 `third-session`）而全部 401 | 必须在 Java/BFF 层注入服务令牌并做头名映射，不能纯 nginx 转发 |
| 3 | **前端 UI 未接线（半成品）** | chat 页 `onLoad` 调 `getFaqRecommendations().catch(()=>{})` 后**丢弃响应**，推荐列表用硬编码 10 条 questionPool；点击问题/语音后只跑打字机动画，不调 `getFaqAnswer`/`createDiagnosis`；电池报告页未接 `health-report-jobs` | 接口封装层已就绪且正确，UI 接线是前端侧剩余工作 |

### 10.3 BFF 侧修复方案（历史：36 入口切换阶段）

断点 1+2 在 36 入口切换阶段由 120 公网入口向 36 受限入口反代；当前正式入口已按 §10.6
切换到 41，下面配置仅保留为历史证据：

```nginx
# api.qumall.qushiyun.com（120 nginx, /www/server/panel/vhost/rewrite/）：
location /v1/ {
    proxy_pass https://36.156.159.175:8789;                      # 36 受限 AI-Ops 入口
    proxy_ssl_server_name on;
    proxy_ssl_name api.qumall.qushiyun.com;
    proxy_http_version 1.1;
    proxy_set_header Authorization "Bearer <AI-Ops 服务令牌>";  # 服务端注入，不下发前端
    proxy_set_header X-Third-Session $http_third_session;       # 透传前端 third-session 头（nginx 内部可大写，网关侧 ASCII 头不区分大小写）
    proxy_set_header Range $http_range;                          # 保证 video 元素的 seek/Range 合同
    proxy_set_header X-Business-Entry "consumer";               # C 端 APP 入口固定 consumer
    proxy_set_header Host $host;
}
# 注意：$http_third_session 取的是入站请求的 third-session 小写头——前端仍必须发小写 third-session，
# 若发 X-Third-Session（带下划线）nginx 默认丢弃（underscores_in_headers off），此处会拿到空值。
```

实施说明：
- 反代与头注入在 **`api.qumall.qushiyun.com`**（公司 BFF 域名）上，不在 xyh5（xyh5 是前端同事的独立调试环境，其数据与公司库零交叉，2026-09-05 实测）。
- **前端侧唯一必改项**：APK 的 `APIURL` 从 `https://xyh5.xyseeker.com` 改为 `https://api.qumall.qushiyun.com`。该变量是全局 basePath，业务接口（mallapi/charging-pile）会一并切换——已实测该域名上业务接口与 AI 接口均可用且为同一数据世界。
- 若在 Java 网关（Spring Cloud Gateway / Nacos 体系）实现，语义相同：路由 `/v1/**` 到 AI-Ops，加请求头改写过滤器。`third-session` 值无需变换——它就是登录响应中的 `thirdSession`，AI-Ops 侧用同一值直查 Redis 会话。

### 10.4 后端接口本体已验证正常（平高租户实测）

使用从 Redis 收割的 2 个平高租户（2019588094906601472）活跃 C 端会话实测：

- `GET /v1/faq/recommendations`（consumer）→ 200，28 条；
- `POST /v1/faq/answer`（`consumer.faq.q001`）→ 200，text 答案。

即：**断点修复（BFF 反代 + 头注入）完成后，FAQ 线立即可用**；健康报告/单问诊断另受 #113 测试数据缺口（需"已登录+有订单"账号）约束。

### 10.6 链路现状（2026-09-12）

断点 1+2 已修复；当前正式链路按环境隔离。41 的链路为：

```text
APK / 前端 → https://api.mall.qushiyun.com/v1/*（41 入口 + 头注入）
           → 41 本机 AI-Ops 网关（127.0.0.1:8788, systemd aiops-gateway-41.service）
           → 41:29380 SSH 回环隧道 → 36 本机 kb-service/RAGFlow（127.0.0.1:9380）
           → 41 本机 MySQL/Redis/TDengine 诊断数据源
```

95 使用 `https://api.qumall.qushiyun.com/v1/*` 和 95 自己的 Gateway、会话库及诊断数据源。

- C 端验收状态：41 FAQ 已通过；自由 QA 当前受百炼 `Arrearage` 阻塞，媒体 blocks 同样
  未生成。95/36 的视频 Range 通过记录仅作历史参考；41 当前会话的健康报告和
  单问诊断成功路径仍需一笔已授权订单，不能用未授权订单或历史他人诊断替代。
- 前端剩余工作见 §10.5；接口消费者只需切换 APIURL，并保留 `third-session` 小写头。

### 10.7 前端最小验证命令（改完直接跑，无需后端配合）

```bash
curl -i "https://api.mall.qushiyun.com/v1/faq/recommendations" \
  -H "tenant-id: 2019588094906601472" \
  -H "third-session: <你的有效 thirdSession>"   # 全小写连字符，不是 X-Third-Session
# 41 使用 api.mall.qushiyun.com；95 使用 api.qumall.qushiyun.com。期望：HTTP/1.1 200 + recommendations 数组（28 条）
```

要点：
- **只有 `tenant-id` + `third-session` 两个头就够**，不需要 `Authorization`（公网 nginx 已注入服务令牌），不需要 `X-Business-Entry`（单手台消费者身份自动判定）。
- 若用 `X-Third-Session`（大写）→ **401 `INVALID_ACCESS_TOKEN`**（头被 nginx 丢弃，实测对照）。
- 若收到**带空格**的错误码（如 `INVALID ACCESS TOKEN`）→ 那是中间 BFF/网关自己返的，**不是 AI-Ops**；先检查有没有 IP/端口转发链在中间拦截。


### 10.5 前端剩余工作（断点 1、2 修复后）

1. chat 页消费 `getFaqRecommendations()` 响应，用 `recommendations[].title` 替换硬编码 questionPool（保留 `question_id` 供点击时传给 `getFaqAnswer`）；
2. 点击推荐问题/发送自由文本时真正调用 `getFaqAnswer(question_id)` / `createDiagnosis({order_no, question})`，替换当前打字机假回复；
3. 电池报告页接 `createHealthReportJob(order_no)` + 按 `retry_after_ms` 轮询 `getHealthReportJob(job_id)`；
4. 错误分支按 §2.2 处理（422 不原样重试、401 引导重新登录、503 稍后重试）。

## 10.8 智能问答调用示例（前端可直接照抄）

统一入口 `POST /v1/assistant/questions`（`order_no` 可选）。三种场景：

### 场景 A：快捷问 / FAQ 命中 —— 同步返回答案

```http
POST https://api.qumall.qushiyun.com/v1/assistant/questions
third-session: <当前登录态>   tenant-id: <租户>   X-Business-Entry: consumer
Content-Type: application/json

{"question": "无法拔枪怎么办"}
```
→ `200` `{type:"faq", question_id, answer, ...}` **直接渲染答案**。

### 场景 B：自由提问（任意问题）—— 异步 job，轮询，返回 blocks[]

```http
POST https://api.qumall.qushiyun.com/v1/assistant/questions
{"question": "充电桩怎么拔枪？有没有演示视频"}

→ 202 {type:"qa", qa_id, status:"queued"}

GET https://api.qumall.qushiyun.com/v1/assistant/questions/{qa_id}
   （按 retry_after_ms 轮询，1s 一次）
```

`status=completed` 时 `result` 是 **blocks-v1 内容块合同**（不要解析 Markdown、不要从文本里提取 URL）：

```json
{
  "blocks": [
    {"kind": "text", "text": "请先停止充电，再按下枪柄卡扣拔出。"},
    {"kind": "image", "resource_id": "media_xxx", "title": "拔枪示意.png",
     "media": {"resource_id": "media_xxx",
                "url": "/v1/media/media_xxx.<24位签名>",
                "kind": "image", "mime_type": "image/png",
                "title": "拔枪示意.png", "reference_id": "chunk-1"}},
    {"kind": "video", "resource_id": "media_yyy", "title": "拔枪演示.mp4",
     "media": {"url": "/v1/media/media_yyy.<24位签名>",
                "kind": "video", "mime_type": "video/mp4", ...}},
    {"kind": "reference", "reference_id": "chunk-1", "title": "充电操作手册.docx"}
  ],
  "retrieval_status": "found"
}
```

**四种块的渲染规则**：

| kind | 字段 | 前端行为 |
|---|---|---|
| `text` | `text` | 按顺序渲染段落 |
| `image` | `media.url` | `<img src={media.url}>`，按 `media.mime_type`（png/jpeg/webp） |
| `video` | `media.url` | `<video src={media.url}>` 直接播放（服务端已支持 Range 分段） |
| `reference` | `title` | 显示来源文件名（折叠区/角标） |

**媒体 URL 规则（重要）**：

- `media.url` 是**同域相对路径**（`/v1/media/...`），直接当 `src` 用，**不需要 Authorization 头**——短时签名就在 URL 里，有效期约 10 分钟。
- 媒体块失效时 `media: null` 且带 `unavailable: true`：**保留文本与引用**，显示"资源不可用"，不要整条消息报错。
- `retrieval_status`：`found`（命中知识库）/ `not_found`（无命中，通用回答）/ `unavailable`（检索依赖故障，文本仍可交付）/ `limited`（达检索上限）。
- **`status=failed` 处理（重要）**：failed 是**终态**——收到即**停止轮询该 `qa_id`**（failed 的作业永远不会变成 completed）。轮询响应此时带 `error: {code, message, retryable}`（与订单诊断线同形状，多为模型限流）：

```json
{
  "type": "qa",
  "qa_id": "qa_7478808279b2493f8984753c65b9b766",
  "status": "failed",
  "retry_after_ms": null,
  "result": null,
  "error": {"code": "QA_FAILED", "message": "模型服务暂时不可用，请稍后重试", "retryable": true}
}
```

  前端在气泡里渲染 `error.message`（服务端脱敏后的真实原因）。**"稍后重试"指重新发起一次新提问**——用同一个 question 再 POST `/v1/assistant/questions`，产生新 `qa_id`，而不是继续轮询旧作业；失败原因多为暂时性（限流/供应商欠费），过一会儿重问通常即成功。`retryable: true` 时展示"稍后重试"按钮，点击执行重新 POST。**不要**把失败兜底显示成"未找到"——那是 404 的语义，不是失败作业的语义。

### 场景 C：带订单的自由提问 —— 走订单诊断

```http
POST https://api.qumall.qushiyun.com/v1/assistant/questions
{"question": "这个订单为什么提前停了", "order_no": "2096164064667852801"}

→ 202 {type:"diagnosis", diagnosis_id, status:"queued"}
  轮询 GET /v1/standard/diagnoses/{diagnosis_id} 到 completed
```

### 场景 D：产品快捷动作（shortcut）与澄清（clarification）

> **跳转类动作的联调交接**见 [frontend-jump-path-handoff.md](./frontend-jump-path-handoff.md)：含 41 真实响应样例、一个分支的判别写法、以及 8 个易踩点。

#### D.1 GET /v1/shortcuts — 读取当前入口的有效快捷动作

页面加载时调用一次（产品首页按钮的渲染源）。**任何通过助手鉴权的调用者都可读**，
不需要管理角色。

```http
GET /v1/shortcuts
third-session: <当前用户的有效 thirdSession>   # 全小写连字符（§2.1 的坑）
tenant-id: <租户ID>
X-Business-Entry: consumer                       # 建议始终显式（§2.1）
Accept-Language: zh                              # 可选，影响 label/description 语言
```

该读取接口复用助手只读权限；创建、发布、停用等管理操作继续需要
`aiops:shortcuts:manage`，并额外校验对应管理角色。

响应 `200`（2026-09-15 41 公网真实返回）：

```json
{
  "type": "shortcut_list",
  "language": "zh",
  "count": 4,
  "shortcuts": [
    {
      "code": "case_exploration",
      "intent": "case_exploration",
      "requires_order": false,
      "sort_order": 10,
      "label": "客户案例",
      "description": "查看充电运营标杆案例",
      "question_template": "我想看看客户案例",
      "target_agent_version": "agt_ed443cae10ac4ba78c81b9d1b43fd91d#v1",
      "jump_path": null
    },
    {
      "code": "smart_diagnosis",
      "intent": "order_issue",
      "requires_order": true,
      "sort_order": 20,
      "label": "智能检测",
      "description": "选择订单后自动诊断充电异常",
      "question_template": "帮我检测这个订单的充电异常",
      "target_agent_version": null,
      "jump_path": null
    },
    {
      "code": "report_fault",
      "intent": "report_fault",
      "requires_order": false,
      "sort_order": 30,
      "label": "故障上报",
      "description": "描述故障现象，由平台跟进处理",
      "question_template": "我要上报一个故障",
      "target_agent_version": null,
      "jump_path": "/charge/pages/faultReport/faultReportList"
    }
  ]
}
```

**字段语义（前端怎么用）**：

| 字段 | 用途 |
|---|---|
| `code` | **稳定标识**，前端把交互行为绑到它，不要绑 `label`（文案会改）；点击时原样回传（见 D.2） |
| `intent` | 服务端内部意图（`case_exploration`/`solution_discovery`/`order_issue`/`report_fault` 等），仅供前端区分展示形态，**不需要回传** |
| `requires_order` | `true` → 点击后先弹**订单选择器**（用户订单列表由 BFF 既有接口提供），选完再把 `order_no`+问题提交统一入口；`false` → 直接提交 |
| `sort_order` | 按升序渲染按钮 |
| `label` / `description` / `question_template` | 按 `Accept-Language` 本地化（缺失翻译回退中文）；`question_template` 可作为默认问题文案预填输入框 |
| `target_agent_version` | 宣传类快捷动作绑定的已发布 agent 版本，**前端不需要理解，原样忽略**（服务端路由用；不要展示给用户）。与 `jump_path` **互斥**：跳转动作不会到达 agent，因此不允许同时绑定 |
| `jump_path` | **跳转动作的唯一判别依据**，见 D.2a。**该字段恒存在**：非 `null` → 点击后导航到该路径；`null` → 走的提示动作流程（D.2）。不要区分"字段缺失"与"字段为空" |

**两种动作形态**：每条动作要么是**提示动作**（`jump_path` 为 `null`，点击后把问题提交统一入口），
要么是**跳转动作**（`jump_path` 非 `null`，点击后本地导航）。判别只看这一个字段——
服务端不下发额外的类型字段，也不要按 `intent` 或 `code` 去猜。

- 列表是当前入口的**有效合并结果**：平台已发布默认动作对所有租户可见；租户已发布覆盖优先，
  租户级停用抑制平台默认；草稿不可见。前端不需要感知管理端的作用域细节。
- `language` 回显实际生效语言（§2.4 同一规则），可据此核对。
- 只列当前认证租户的有效结果和当前业务入口；平台默认可以跨租户可见，但订单、Agent、知识库、
  媒体和执行数据仍按当前租户隔离。

错误：401/403/409/503 与 §2.2 通用处理一致（409=平台无法唯一确定，同 FAQ 线）。

#### D.2a 跳转动作 —— 本地导航，不经过统一入口

`jump_path` **非 `null`** 的动作是**跳转动作**。点击后**不要**调用统一助手入口，直接本地导航：

```js
if (shortcut.jump_path) {
  uni.navigateTo({ url: shortcut.jump_path })   // 路径由服务端下发，原样使用
} else {
  submitToAssistant(shortcut)                   // 见 D.2
}
```

规则：

- **路径原样使用**，不要拼接、改写或加参数。跨语言是同一个路径（`jump_path` 不做国际化）。
- **忽略该动作的 `question_template`**。产品规则是两者互斥（面板文案：「填写路径链接后用户点击将导航到对应界面，预设提示词不起作用」），但服务端仍会返回该字段的历史值；跳转动作**不得**拿它去填输入框或提交提问。
- **跳转动作不会产生任何响应 `type`**。别去 D.4 那张表里找它——它根本不走统一入口。
- **不要为了"保险"仍然提交一次入口**。服务端对误投的跳转动作会同步返回 `type=clarification`、`message="请点击页面上的快捷按钮进入对应页面。"`、`missing_fields` 为空，**且不创建任何作业**。那只是一个兜底，不是正常流程。
- **tabBar 页面**：`uni.navigateTo` 无法跳转 tabBar 页面，需要 `uni.switchTab`。具体路径属于 tabBar 与否由前端确认。
- **服务端不校验页面是否存在**。路径是客户端路由的键，可达性由前端验收；后端只保证格式（以 `/` 开头、长度上限）与如实下发。

#### D.2 快捷动作的执行 —— 没有独立执行协议，走统一助手入口

适用于 `jump_path` 为 `null` 的**提示动作**（含下面的 `requires_order` 规则）。点击后把 `question`（用 `question_template` 或用户输入）和 **`shortcut_code`** 提交到场景 B 的统一入口：

```http
POST /v1/assistant/questions
{
  "question": "我想看看客户案例",
  "shortcut_code": "case_exploration"
}
```

- `shortcut_code` 可选字段（1-64 字符，`[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}`）；传了且命中已发布行 → 服务端按该行的意图路由（宣传类 → 独立宣传 agent；订单类 → 诊断）。
- **`requires_order=true` 的动作（如 smart_diagnosis）**：先弹订单选择器，`question` 里带订单或显式传 `order_no`（场景 C 合同），服务端做归属校验。
  - 两种传法等价，服务端都会提取并做归属校验：**①** `order_no` 字段单独传；**②** 订单号出现在 `question` 文本里（如「帮我检测（2099…）这个订单的充电异常」）。
  - 两处都没有订单号时，才同步返回 `type=clarification` + `missing_fields:["order_no"]`，**不创建作业**。
  - 订单号不是本人的：回落通用问答（非 404、不泄露），本人在订单选择器里选到的订单正常进诊断。
- **`requires_order=false` 的宣传类动作（案例/方案）**：直接提交 → `202 type=qa` → 按 `retry_after_ms` 轮询（场景 B 完全相同的合同）；completed 后 `result.blocks[]` 是四段式卡片（标题/行业痛点/破局方案/商业成果），按块渲染即可（不解析文本）。
- 未传 `shortcut_code` 时，用户自由输入里明确出现"客户案例/行业解决方案"等词也会命中宣传路由；普通问题走场景 B。
- 过期/未发布的 `shortcut_code` 被服务端忽略，按普通问题处理（不报错）。

#### D.3 type=clarification —— 同步澄清（不建作业）

高风险问题（扣费/订单争议类）缺少订单上下文时，统一入口**同步**返回：

```json
{
  "type": "clarification",
  "language": "zh",
  "question": "是不是扣错钱了",
  "missing_fields": ["order_no"],
  "message": "请提供需要核查的订单号后，我才能继续处理。"
}
```

- HTTP 200，**没有 qa_id/diagnosis_id，不轮询**——渲染 `message` 并按 `missing_fields` 引导补信息（`order_no` → 弹订单选择器）后重新提交。
- `report_fault` 第一版只收集故障描述，**不创建工单**。

#### D.4 响应 type 速查（统一入口 POST /v1/assistant/questions）

| type | HTTP | 含义 | 前端动作 |
|---|---:|---|---|
| `faq` | 200 | FAQ 命中 | 直接渲染 answer（场景 A） |
| `clarification` | 200 | 缺关键信息 | 渲染 message + 按 missing_fields 补信息（D.3） |
| `qa` | 202 | 问答/宣传卡片作业 | 按 retry_after_ms 轮询（场景 B） |
| `diagnosis` | 202 | 订单诊断 | 轮询诊断线（场景 C） |

**注意**：这张表只覆盖**走统一入口**的响应。**跳转动作不在其中**——它由客户端本地导航
（D.2a），不调用统一入口，因此没有响应 `type`。

### 历史分离

- **通用问答历史**：`GET /v1/assistant/questions?limit=50` → `{type:"qa_list", questions:[{qa_id,question,status,...}]}`
- **订单诊断历史**：`GET /v1/standard/diagnoses?limit=50`（独立）

两者按调用者隔离，前端"我的问答"与"我的诊断"分开展示。

### 多轮会话（conversation_id，#172 合同）

场景 B 可选带 `conversation_id` 连续追问；订单确认后 follow-up 可省订单号：

```http
POST /v1/conversations {"agent_version_key": "agt_xxxx#v1"}
                                                → 201 {conversation_id, business_entry, active_order_no: null, is_generating, ...}
POST /v1/assistant/questions {"question": "...", "conversation_id": "conv_xxx"}
POST /v1/conversations/{id}/active-order {"order_no": "2096..."}   → 绑定归属校验通过的订单
POST /v1/assistant/questions {"question": "这个订单为什么提前停了", "conversation_id": "conv_xxx"}
                                                → follow-up 无需再传 order_no
GET    /v1/conversations                        → 会话列表（刷新/换设备可续）
GET    /v1/conversations/{id}                    → 单会话（含历史轮次）
DELETE /v1/conversations/{id}                    → 删除
```

- 上下文窗口：最近 **8 轮或 8k token**（取小者），保留 **30 天**。
- 同会话并发提问 → **409 `CONVERSATION_BUSY`**，提示"上一条还在生成"。
- 每轮重新鉴权：权限/订单归属撤销后绑定自动失效，follow-up 回落通用问答。
- 非本人/过期会话 → 统一 404，无存在性泄露。
