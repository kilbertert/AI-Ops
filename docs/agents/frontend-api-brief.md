# AI-Ops 前端联调总览

> 状态：当前有效，2026-09-04。
> 读者：前端组、BFF/Java 组、联调测试。
> 权威契约：固定问答见 [固定问答标准接口](../faq-api.md)；健康报告与单问诊断见 [标准后端接口报告](../standard-api-contract.md)。本文是三者的前端视角总览，冲突时以两份契约文档为准。

## 0. 一图看懂：谁调谁

```text
小程序/浏览器
    │  只带现有登录态（thirdSession），不持有任何 AI-Ops 令牌
    ▼
业务 BFF / Java 后端
    │  1. 验证用户登录态
    │  2. 按访问入口决定 X-Business-Entry: consumer | operator
    │  3. 以服务身份调用 AI-Ops，转发 thirdSession
    ▼
AI-Ops（https://aiops-api-test.ranlei.work）
    │  解析会话 → 判定平台身份 → 隔离内容域 → 返回数据
    ▼
前端拿到数据渲染
```

**前端绝对不做**的事：

- 不保存、不打印、不打包 AI-Ops 服务令牌（`aops_*` 也不行）；
- 不提交 `platform`、`user_id`、`tenant_id`、角色、B 端主体 ID——这些全部由 BFF/AI-Ops 从会话推导；
- 不直接连 AI-Ops 测试域名；前端实际 Base URL 由 BFF 决定。

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

```http
Authorization: Bearer <aiops-service-token>     # BFF 的服务身份，不给前端
X-Third-Session: <当前用户的有效 thirdSession>   # BFF 验证后原样转发
X-Business-Entry: consumer                       # consumer | operator，按访问入口设置
Content-Type: application/json                   # POST 时
```

- `X-Business-Entry` 由 BFF 根据用户从哪个入口进来设置，**不是前端传的选项**。只允许 `consumer` / `operator`。
- 身份只有一个可用平台时可省略；联调阶段建议 BFF 始终显式设置，避免双平台用户得到 `409 PLATFORM_AMBIGUOUS`。
- 前端实际调用的 URL、鉴权方式由 BFF 决定；建议 BFF 保留 `/v1/*` 路径与响应体结构原样透传，前端零转换。

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

### 2.3 状态字段（异步线通用）

健康报告作业与单问诊断都是"创建 → 轮询"模型：

```text
queued → running → completed | inconclusive | failed | expired   （诊断）
queued → running → completed | failed | expired                  （报告作业）
```

- 创建响应都带 `retry_after_ms`，按它节流轮询；不要密集轮询。
- 只有 `completed`/`inconclusive` 时 `result`/`report` 才有值。
- `expired` 表示结果超过保留期，让用户重新发起。

## 3. 固定问答（FAQ）

> 详见 [固定问答标准接口](../faq-api.md)。一问一答，不建会话、不查订单、不调模型、无 `job_id`/`diagnosis_id`。

### 3.1 推荐问题（页面加载）

```http
GET /v1/faq/recommendations
```

返回当前平台的完整候选清单（客户端 28 条 / 管家端 17 条），前端可按 `question_id` 自选子集与顺序，但**不得**把答案预存进推荐配置。

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

### 3.2 点击推荐 → 同步答案

```http
POST /v1/faq/answer
Content-Type: application/json

{"question_id": "consumer.faq.q001"}
```

`200` 直接返回答案，无轮询：

```json
{
  "platform": "consumer",
  "available_platforms": ["consumer"],
  "faq_version": "2026.09.04",
  "question_id": "consumer.faq.q001",
  "question": "快充桩、超充桩与慢充桩有什么区别？我的车应该选哪种？",
  "answer": "固定纯文本答案",
  "format": "text"
}
```

`answer` 按纯文本渲染、保留换行，不作为 HTML。

### 3.3 完整目录（FAQ 页）

```http
GET /v1/faq/catalog
```

响应字段：`platform`、`available_platforms`、`faq_version`、`entries`。普通推荐区不需要调它。

### 3.4 FAQ 专属错误

| HTTP | `error.code` | 前端处理 |
|---:|---|---|
| 404 | `FAQ_NOT_FOUND` | ID 未知/下线/跨平台 → 刷新推荐列表 |
| 403 | `PLATFORM_FORBIDDEN` | 当前入口与身份不符 → 返回业务入口页 |
| 409 | `PLATFORM_AMBIGUOUS` | 多主体无法唯一确定 → 联系 BFF 排查 |
| 503 | `PLATFORM_UNAVAILABLE` | 身份映射依赖不可用 → 稍后重试 |

## 4. 健康报告（充电体检单）

> 详见 [标准后端接口报告 §4](../standard-api-contract.md)。确定性计算，非模型生成。

**流程**：`POST /v1/health-report-jobs`（带 `order_no`）→ 拿 `job_id` → 按 `retry_after_ms` 轮询 `GET /v1/health-report-jobs/{job_id}` → `status=completed` 读 `report`。

`report` 关键字段：

| 字段 | 说明 |
|---|---|
| `summary` | 固定模板摘要文本，可直接展示 |
| `indicators[]` | 单项指标，`status` 只会是 `normal / attention / abnormal / unavailable`，按状态渲染，不解析中文阈值 |
| `radar[]` | 五维评分；缺数据时该维 `score=null`、`status=unavailable` |
| `curves` | 功率/电压/温度曲线，每 series ≤300 点，格式 `[[t, v], ...]` |
| `health_metrics` | 中间计算值（`soh`、`soc_delta` 等） |
| `completeness` | 数据完整度 `0..1` |
| `source_summary` | 各数据源可用状态 |
| `rule_version` | 计算公式版本 |

**专属错误**：`404 REPORT_JOB_NOT_FOUND`（作业不存在/不属于当前调用者）、`503 REPORT_JOB_UNAVAILABLE`。

**典型耗时**：秒级；轮询间隔按 `retry_after_ms`（约 1s）。

## 5. 单问诊断（自由文本/订单问题）

> 详见 [标准后端接口报告 §5](../standard-api-contract.md)。一问一诊断，非多轮会话。

**流程**：`POST /v1/standard/diagnoses`（`order_no` + `question`，可选 `indicator_code`）→ 拿 `diagnosis_id` → 轮询 `GET /v1/standard/diagnoses/{diagnosis_id}` → `completed`/`inconclusive` 读 `result`。

- `question` 最多 4000 字符；不能提交 `score`、曲线、报告、`user_id`、`tenant_id` 作为可信输入。
- 历史列表：`GET /v1/standard/diagnoses?limit=50`。
- 诊断结果不暴露证据 ID、内部 run、provider、SQL、原始报文。

**专属错误**：`404 DIAGNOSIS_NOT_FOUND`、`503 DIAGNOSIS_UNAVAILABLE`。

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

## 8. 常见坑（来自真实验收）

- **`aops_*` 设备令牌会被拒绝**：标准 API 侧收到 `aops_` 前缀 token 一律 401。那是旧 Gateway 设备流（`/v1/runs`），三条标准业务线都不用它。
- **Apifox 手测 422**：请求体多一个字段都不行（`extra="forbid"`），且 `Content-Type: application/json` 必须放在 Headers，不能塞 Query 参数。
- **404 不区分"不存在"与"无权限"**：这是故意的资源存在性隐藏，前端统一提示"未找到"即可。
- **旧接口迁移**：`GET /v1/recommendations?category=...` → `GET /v1/faq/recommendations`；点击推荐 → `POST /v1/runs` 改为 `POST /v1/faq/answer`；`GET /v1/reports/pending`、`GET /v1/reports/charging-health` 不在当前契约，改走 `/v1/health-report-jobs`。

## 9. 当前真实验收状态（2026-09-04）

测试 Gateway 已部署，公网健康检查通过。使用当前真实有效 C 端会话完成：

- consumer 推荐接口 HTTP 200，返回 28 条候选；
- consumer 目录接口 HTTP 200，返回 28 条目录；
- consumer 固定答案 HTTP 200，返回 `text` 格式答案；
- consumer 请求 operator 问题 ID 返回 404 `FAQ_NOT_FOUND`；
- 同一用户请求 operator 入口返回 503 `PLATFORM_UNAVAILABLE`。

当前可解析会话中尚未找到具备唯一 B 端主体映射的真实样本，因此管家端成功响应仍待一个有效管家用户会话。该缺口不影响客户端联调，也不能通过伪造 `platform` 或临时扩大权限绕过。
