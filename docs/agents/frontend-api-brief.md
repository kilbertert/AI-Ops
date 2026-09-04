# AI-Ops 固定问答前端联调简报

> 状态：当前有效，2026-09-04。
> 范围：客户端/管家端固定推荐问与固定答案，不包含页面样式、健康报告或自由文本诊断。
> 权威接口契约：[固定问答标准接口](../faq-api.md)。

## 1. 联调边界

AI-Ops 提供标准后端接口，不允许小程序或浏览器保存服务令牌并直接调用。真实调用链是：

```text
小程序/前端
  -> 现有业务后端或 BFF，携带现有登录态 thirdSession
  -> BFF 验证登录态，并按访问入口确定 consumer 或 operator
  -> BFF 以服务身份调用 AI-Ops，转发 thirdSession 和可信入口
  -> AI-Ops 再次解析会话、判定 C/B 身份映射并隔离 FAQ 内容
```

前端只处理 `question_id`、标题、排序和答案文本。以下内容不能由前端提交或推断：

- AI-Ops 服务 Bearer；
- `platform`、`user_id`、`tenant_id`、角色或权限；
- B 端主体 ID；
- 固定答案正文。

当前没有修改 Java/BFF 仓库。前端正式接入前，调用方需要使用已有 BFF 通用代理或网关配置暴露前端可访问的同名路由；若现有 BFF 没有这项转发能力，需要由 BFF 维护方另行接线，不能把服务令牌下发给前端代替。

## 2. 相比旧方案的变化

以下旧草案接口或行为不再用于固定问答联调：

| 旧设想 | 当前契约 |
|---|---|
| 前端持有 `aops_*` 设备令牌直连 Gateway | BFF 持有 AI-Ops 服务令牌，前端只使用现有登录态 |
| `GET /v1/recommendations?category=quick_question` | `GET /v1/faq/recommendations` |
| 点击推荐后调用 `POST /v1/runs` | 点击后调用同步 `POST /v1/faq/answer` |
| 推荐问题触发模型或订单诊断 | 固定问答不调用模型、不查订单、不创建诊断资源 |
| 前端传 `platform` | AI-Ops 根据会话、入口与 C/B 映射判定平台 |
| 一次 `runs` 代表一段会话 | 固定问答是一问一答；自由文本诊断仍是独立资源 |

`GET /v1/reports/pending`、`GET /v1/reports/charging-health` 和页面级待办设计也不属于本次固定问答契约。健康报告与单问诊断使用 [标准后端接口报告](../standard-api-contract.md)。

## 3. 测试地址

AI-Ops 后端到后端测试 Base URL：

```text
https://aiops-api-test.ranlei.work
```

健康检查：

```http
GET https://aiops-api-test.ranlei.work/health
```

这不是允许前端携带服务令牌直连的公共用户接口。前端实际使用的 Base URL 由业务 BFF 决定；建议 BFF 保留 `/v1/faq/*` 路径和响应体，减少二次转换。

## 4. BFF 调用约定

BFF 调用三条 FAQ 接口时统一设置：

```http
Authorization: Bearer <aiops-service-token>
X-Third-Session: <当前用户的有效 thirdSession>
X-Business-Entry: consumer
```

`X-Business-Entry` 只允许 `consumer` 或 `operator`。它由 BFF 根据用户访问的业务入口设置，不是前端提交的平台选择器。

如果当前身份只有一个可用平台，可以省略 `X-Business-Entry`；联调阶段仍建议 BFF 明确设置，避免用户同时存在两个内容域时得到 `PLATFORM_AMBIGUOUS`。

## 5. 前端调用流程

### 5.1 页面加载推荐问题

业务 BFF 转发：

```http
GET /v1/faq/recommendations
```

AI-Ops 当前返回所在平台的完整推荐候选清单：客户端 28 条，管家端 17 条。前端可随自身版本按 `question_id` 选择其中一部分展示，并自行调整展示顺序；不得把答案打包进推荐配置。

```json
{
  "platform": "consumer",
  "available_platforms": ["consumer"],
  "faq_version": "2026.09.04",
  "recommendations": [
    {
      "question_id": "consumer.faq.q001",
      "title": "快充桩、超充桩与慢充桩有什么区别？我的车应该选哪种？",
      "sort": 1
    }
  ]
}
```

前端发布时也可以使用仓库制品 `src/aiops_diagnostics/faq_recommendations.json` 选择固定推荐项，但页面进入后仍应以接口返回的平台和当前版本为准。

### 5.2 点击推荐问题

前端只把所点击的 `question_id` 交给 BFF：

```http
POST /v1/faq/answer
Content-Type: application/json

{"question_id":"consumer.faq.q001"}
```

同步成功返回 HTTP 200：

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

前端直接把 `answer` 作为普通文本消息渲染，并保留换行。该响应没有 `job_id`、`diagnosis_id` 或轮询过程。

### 5.3 完整 FAQ 页

需要完整 FAQ 列表时调用：

```http
GET /v1/faq/catalog
```

响应字段为 `platform`、`available_platforms`、`faq_version` 和 `entries`。普通推荐区不需要调用该接口。

### 5.4 自由输入分流

```text
点击固定推荐 -> question_id -> POST /v1/faq/answer -> 同步固定答案
自由文本/订单问题 -> POST /v1/standard/diagnoses -> 异步单问诊断
```

第一版不提供多轮会话。前端不能把自由文本塞入 FAQ 答案接口，也不能把固定推荐转换成模型问题。

## 6. 错误处理

错误统一为：

```json
{"error":{"code":"FAQ_NOT_FOUND","message":"FAQ question was not found","retryable":false}}
```

| HTTP | `error.code` | 前端处理 |
|---:|---|---|
| 401 | `ACCESS_TOKEN_REQUIRED` / `INVALID_ACCESS_TOKEN` | BFF 登录态或服务认证失效；重新登录或由后端排查 |
| 403 | `PLATFORM_FORBIDDEN` | 当前入口与身份不符；返回业务入口页 |
| 409 | `PLATFORM_AMBIGUOUS` | BFF 未能唯一确定平台/主体；不要让前端自行切换 |
| 404 | `FAQ_NOT_FOUND` | ID 未知、已下线或属于另一平台；刷新推荐列表 |
| 422 | `INVALID_REQUEST` | 前端/BFF 请求字段错误；不要重试同一请求 |
| 503 | `PLATFORM_UNAVAILABLE` | 身份映射或只读依赖不可用；可以稍后重试并保留关联 ID |

## 7. 联调检查表

- [ ] 前端未保存、打印或打包 AI-Ops 服务令牌。
- [ ] BFF 验证现有 thirdSession，并转发同一值。
- [ ] BFF 根据路由入口设置 `X-Business-Entry`，前端请求体没有 `platform`。
- [ ] 推荐配置只保存 `question_id/title/sort`，不保存答案。
- [ ] 点击推荐只调用答案接口，成功响应不轮询。
- [ ] 答案按纯文本保留换行，不作为 HTML 渲染。
- [ ] `FAQ_NOT_FOUND` 会刷新清单，不显示另一平台内容。
- [ ] 自由输入与订单问题走标准单问诊断，不走 FAQ。

## 8. 当前真实验收状态

测试 Gateway 已部署，公网健康检查通过。使用当前真实有效 C 端会话完成：

- consumer 推荐接口 HTTP 200，返回 28 条候选；
- consumer 目录接口 HTTP 200，返回 28 条目录；
- consumer 固定答案 HTTP 200，返回 `text` 格式答案；
- consumer 请求 operator 问题 ID 返回 404 `FAQ_NOT_FOUND`；
- 同一用户请求 operator 入口返回 503 `PLATFORM_UNAVAILABLE`。

当前可解析会话中尚未找到具备唯一 B 端主体映射的真实样本，因此管家端成功响应仍待一个有效管家用户会话。该缺口不影响客户端联调，也不能通过伪造 `platform` 或临时扩大权限绕过。
