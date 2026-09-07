# 固定问答标准接口

## 用途

固定问答是已确认业务文案的确定性内容服务。它不创建诊断运行、不查询订单、不调用模型，也不接受前端传入的平台或身份字段。

业务后端或 BFF 调用 AI-Ops 时使用服务 Bearer，并转发已验证的 `thirdSession`。对于可能同时存在客户端和管家端关联的 C 端用户，BFF 还必须传递可信的 `X-Business-Entry`：`consumer` 或 `operator`。这个请求头只应由服务端设置，不能让小程序直接控制。

前端联调责任、旧接口迁移和页面分流见 [前端联调总览](agents/frontend-api-brief.md)。

前端/BFF 统一 Base URL：`https://api.qumall.qushiyun.com`（AI 接口为同域 `/v1/*`，2026-09-06 服务迁移后由公司 120 直接承载，见迁移记录 issue #147）

公共请求头：

```http
Authorization: Bearer <aiops-service-token>
third-session: <verified-third-session>   # 全小写连字符；不能写 X-Third-Session（nginx 会丢带下划线头）
X-Business-Entry: consumer
```

## 接口

完整测试 URL：

- `GET https://api.qumall.qushiyun.com/v1/faq/recommendations`
- `GET https://api.qumall.qushiyun.com/v1/faq/catalog`
- `POST https://api.qumall.qushiyun.com/v1/faq/answer`

上述 URL 供受信任业务后端/BFF 联调。前端不得直接持有 `aiops-service-token`；前端实际地址由 BFF 暴露。

### `GET /v1/faq/recommendations`

返回当前平台的完整推荐候选。推荐项只有展示字段，不包含答案；前端可随版本选择其中一部分展示。

```json
{
  "platform": "consumer",
  "available_platforms": ["consumer", "operator"],
  "faq_version": "2026.09.04",
  "recommendations": [
    {"question_id": "consumer.faq.q001", "title": "快充桩、超充桩与慢充桩有什么区别？我的车应该选哪种？", "sort": 1}
  ]
}
```

### `GET /v1/faq/catalog`

返回当前平台完整正式目录，包含 `question_id`、问题、纯文本答案和格式。

### `POST /v1/faq/answer`

请求体只允许一个稳定问题标识：

```json
{"question_id": "consumer.faq.q001"}
```

成功响应为同步 `200`：

```json
{
  "platform": "consumer",
  "available_platforms": ["consumer", "operator"],
  "faq_version": "2026.09.04",
  "question_id": "consumer.faq.q001",
  "question": "快充桩、超充桩与慢充桩有什么区别？我的车应该选哪种？",
  "answer": "...",
  "format": "text"
}
```

前端点击推荐问题时只提交推荐项中的 `question_id`。自由输入或订单问题继续调用普通单问诊断接口，不要把自由文本传给本接口。

## 平台判定

- `consumer`：有效 C 端 `thirdSession` 对应的客户端身份。
- `operator`：同租户内存在具备已知管家端 `sys_role.client_type` 的 B 端主体，并且本次入口唯一确定该主体。
- C/B 关联不自动扩大订单权限；多 B 主体无法唯一确定时拒绝管家端请求。
- `available_platforms` 只是当前身份可用的内容域集合，不表示请求可以跨平台读取内容。

前端不读取数据库、不传 `platform`、`user_id`、`tenant_id`、角色或权限。入口缺失且身份同时可用两个平台时返回 `409 PLATFORM_AMBIGUOUS`。

## 错误

所有错误使用标准形状：`{"error":{"code":"...","message":"...","retryable":false}}`。

| HTTP | code | 含义 |
|---:|---|---|
| 401 | `ACCESS_TOKEN_REQUIRED` / `INVALID_ACCESS_TOKEN` | 缺少或无效服务认证 |
| 403 | `PLATFORM_FORBIDDEN` | 入口与身份不匹配，或请求尝试切换平台 |
| 409 | `PLATFORM_AMBIGUOUS` | 多个 B 主体无法唯一确定 |
| 422 | `INVALID_REQUEST` | 请求字段缺失、格式错误或包含额外字段 |
| 404 | `FAQ_NOT_FOUND` | 未知、下线或跨平台问题 ID |
| 503 | `PLATFORM_UNAVAILABLE` | UPMS/只读身份依赖不可用或角色无法解析 |

## 缓存策略

身份判定和答案默认不做服务端缓存（等价于 TTL 0），避免角色或租户权限变化时使用陈旧结果；如果后续有明确性能需求，只允许按身份摘要、`faq_version` 和 `question_id` 做私有缓存，TTL 不超过 60 秒。

## 内容版本

目录制品由 `tools/generate_faq_catalog.py` 从本地业务 DOCX 生成。DOCX 被 `.gitignore` 忽略，不能提交；生成后的 JSON 是服务运行时制品。内容变更通过代码评审、自动化校验和版本发布完成，废弃的 `question_id` 不复用。

## 真实验收状态

2026-09-04 使用测试 Gateway 和当前有效真实 C 端会话完成 consumer 推荐、目录和固定答案调用，均返回 HTTP 200；跨平台问题 ID 返回 404 `FAQ_NOT_FOUND`，无 B 端映射时 operator 入口返回 503 `PLATFORM_UNAVAILABLE`。验收过程未记录会话、用户或租户原文。

当前可解析会话集合中未找到唯一 B 端主体映射，operator 成功链路待有效管家用户会话，不将 consumer 通过结果扩写为管家端验收通过。
