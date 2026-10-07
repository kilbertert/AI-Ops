# 固定问答标准接口

## 用途

固定问答是已确认业务文案的确定性内容服务。它不创建诊断运行、不查询订单、不调用模型，也不接受前端传入的平台或身份字段。

业务后端或 BFF 调用 AI-Ops 时使用服务 Bearer，并转发已验证的 `thirdSession`。对于可能同时存在客户端和管家端关联的 C 端用户，BFF 还必须传递可信的 `X-Business-Entry`：`consumer` 或 `operator`。这个请求头只应由服务端设置，不能让小程序直接控制。

前端联调责任、旧接口迁移和页面分流见 [前端联调总览](agents/frontend-api-brief.md)。

环境入口：`https://api.qumall.qushiyun.com` 为 95 环境，使用 95 自己的 Gateway、会话库和
诊断数据源；`https://api.mall.qushiyun.com` 为 41 环境，使用 41 自己的 Gateway、会话库和
诊断数据源。两套环境的 `/v1/*` 合同相同，但请求不得跨环境转发或混用会话。

前端/BFF 选择与业务环境一致的 Base URL；AI 接口为同域 `/v1/*`。

公共请求头：

```http
Authorization: Bearer <aiops-service-token>
third-session: <verified-third-session>   # 全小写连字符；不能写 X-Third-Session（nginx 会丢带下划线头）
X-Business-Entry: consumer
```

## 接口

完整测试 URL：

- 95：`https://api.qumall.qushiyun.com/v1/faq/{recommendations|catalog|answer}`
- 41：`https://api.mall.qushiyun.com/v1/faq/{recommendations|catalog|answer}`

三类路径分别对应 `GET`、`GET`、`POST`；请求必须使用同一环境的会话。

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

**当前 `faq_version`：`2026.10.09`**（以 `src/aiops_diagnostics/faq_catalog.json` 为准，此处只是快照）。

制品来源分两层，**不要混为一谈**：

| 层 | 来源 | 备注 |
|---|---|---|
| 题面 | 产品宽表（consumer 6 列 / operator 11 列）**或** `tools/faq_i18n/questions_<lang>.json` | consumer 的 `vi/mn/th/km` 宽表没有对应列，题面为起草 |
| 答案 | `tools/faq_i18n/{answers,operator_answers}_<lang>.json` | **全部为起草**，以简体权威答案为源 |

内容变更通过代码评审、自动化校验和版本发布完成，废弃的 `question_id` 不复用。

### 受支持语言（11 门）

`zh` `zh-Hant` `en` `de` `fr` `es` `pt` `vi` `mn` `th` `km`

- **两个平台的固定问答目录均覆盖 11/11**（#565 补齐）。缺内容时回退 `zh`，
  响应里的 `language` 字段**如实上报实际服务的语言**。
- `zh-Hant` 由简体权威**派生**（`tools/derive_zh_hant.py`，opencc `s2twp`），不是另求一套翻译。
- **泰语/高棉语：能读不能问。** 目录可渲染，但自由文本提问返回**该语言自己的**拒绝文案
  —— 这两门语言的能力声明里 `prompting=False`（无词边界，确定性匹配器对它们不可靠）。
- **译文质量未抽查**：代码只守结构（覆盖、无汉字残留、单位/标记未丢、长度带），
  **不守译文正确性**。未抽查的语言不得当作已验收。

## 真实验收状态

**2026-10-07（本仓库当前可复现的做法）**：`.claude/skills/verify-aiops-client-e2e/` 在 41 上以**真实会话**驱动
**生产**网关。实测矩阵 **7 语言 × 2 路由 = 14 个组合**，全部 **HTTP 200 且 `language` == 请求语言、
条目非空（各 28 条）**：

```
✅ zh / en / zh-Hant / vi / mn / th / km  ×  /v1/faq/recommendations  n=28
✅ zh / en / zh-Hant / vi / mn / th / km  ×  /v1/faq/catalog          n=28
```

判据是**三合一**（200 **且** served == 请求语言 **且** 非空）：只报 200 会放过「回显请求语言却服务兜底文案」，
空列表会把「没有任何可看的内容」判成通过。命令与边界见该技能的 SKILL.md 与
`docs/agents/env-41-runbook.md` §5.6。

**探测的写风险与本次对账**：构造 app 会调 `recover_interrupted_jobs()`。本次探测**前后各对账一次**，
三张作业表的在飞数与状态分布**均无变化**（`standard_diagnoses {expired:45, failed:2}` /
`assistant_questions {completed:1, expired:267}` / `health_report_jobs {}`）。

**2026-09-04（历史）**：使用测试 Gateway 和当时有效的真实 C 端会话完成 consumer 推荐、目录和固定答案调用，
均返回 HTTP 200；跨平台问题 ID 返回 404 `FAQ_NOT_FOUND`，无 B 端映射时 operator 入口返回 503
`PLATFORM_UNAVAILABLE`。验收过程未记录会话、用户或租户原文。

**未验**：operator 入口的成功链路需要有效**管家用户会话**；本仓库当前**没有**带管家范围的会话可用，
故 operator 侧的端到端仍为**未验**（不是通过）。

当前可解析会话集合中未找到唯一 B 端主体映射，operator 成功链路待有效管家用户会话，不将 consumer 通过结果扩写为管家端验收通过。
