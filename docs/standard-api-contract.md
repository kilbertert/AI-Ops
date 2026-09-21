# AI-Ops 标准后端接口报告

## 1. 这套接口解决什么问题

AI-Ops 提供的是消费端无关的标准后端 HTTP API。调用方可以是业务后端、BFF、Web、移动端接入层或运维平台；接口不绑定小程序页面，也不要求调用方了解 AI-Ops 内部数据库、Gateway、provider 或 Agent 实现。

目前提供两类独立能力：

1. **充电健康报告**：针对一笔已授权充电订单，按确定性规则计算指标、雷达评分、曲线和数据完整度。
2. **单问诊断**：针对一笔已授权充电订单提交一个独立问题，异步生成诊断状态和诊断结果。

健康报告不是故障根因结论；诊断报告也不会把客户端提交的分数或曲线当成证据。两者可以被同一个上层产品同时展示，但接口和生命周期彼此独立。

## 1.1 访问地址

标准 API 的测试环境基础地址（Base URL）是：

```text
95 环境：`https://api.qumall.qushiyun.com`
41 环境：`https://api.mall.qushiyun.com`

两套入口使用各自环境的 Gateway、会话库和诊断数据源；接口路径和响应合同一致，禁止跨环境混用会话或数据源。
```

健康检查地址为：

```text
41：GET http://127.0.0.1:8788/health      # 在 41 本机执行（ssh 进去后打）
```

**注意：41 的公网地址上 `/health` 不可用。** nginx 只代理 `^~ /v1/`，
`/health` 落到默认处理返回 403（实测 `https://api.mall.qushiyun.com/health`
→ 301 → `/health/` → 403）。这是**有意不对外暴露**，不是配置错误——
探活请在本机 loopback 上做。公网可用的是业务端点，例如
`https://api.mall.qushiyun.com/v1/health-report-jobs`。

95 环境的 `/health` 记录未在本轮核实，故不再列出；需要时按同一方式实测后补。

业务后端直接调用 AI-Ops 时统一携带：

```http
Authorization: Bearer <aiops_service_token>
Content-Type: application/json
third-session: <thirdSession>   # 全小写；X-Third-Session 会被 nginx 丢弃
```

示例：

```bash
curl --fail-with-body \
  -H "Authorization: Bearer $AIOPS_SERVICE_TOKEN" \
  -H "third-session: $THIRD_SESSION" \
  -H "Content-Type: application/json" \
  -d '{"order_no":"2094370061724549120"}' \
  https://api.mall.qushiyun.com/v1/health-report-jobs
```

上例使用 41 入口；95 环境替换为 `api.qumall.qushiyun.com`，订单和会话必须属于 95。

## 2. 调用接口

调用方（Java 业务后端/BFF）必须使用 AI-Ops 服务 Bearer，并同时转发已由 Java 鉴权的
C 端 `thirdSession`：

```http
Authorization: Bearer <aiops_service_token>
third-session: <thirdSession>   # 全小写；X-Third-Session 会被 nginx 丢弃

`thirdSession` 不是 JWT 或服务凭据。AI-Ops 通过只读 Redis 查询
`app:3rd_session:<thirdSession>`，解析会话后建立 `ScopeContext`；小程序不直接调用 AI-Ops。
```

`access_token` 是凭据，`user_id` 只是认证成功后解析出的身份标识。调用方不得用以下内容代替 token：

```http
X-User-Id: 123
X-Tenant-Id: 456
```

```json
{ "user_id": "123", "tenant_id": "456" }
```

AI-Ops 收到 token 后通过现有平台用户/权限链建立不可变 `ScopeContext`，再执行订单范围校验。租户相同不代表租户内所有用户的订单都可访问；用户数据范围、站点范围和代查权限仍然有效。

## 3. 访问判断怎么理解

一次请求需要同时满足：

```text
调用者 token 有效
调用者具备所需接口能力
订单属于调用者的数据范围
订单满足该接口的业务准入条件
```

例如：

```text
调用者 user_id = A
调用者 tenant_id = T
调用者数据范围 = self
订单 user_id = B
订单 tenant_id = T
```

虽然租户都是 `T`，但 `self` 范围只允许读取 A 的订单，因此读取 B 的订单会返回 `ORDER_NOT_FOUND`。这是故意隐藏资源存在性的安全行为。

如果调用者是平台管理员或拥有已配置的代查权限，平台权限上下文可以覆盖更大的用户、站点或租户范围；这由 `cloud-auth/UPMS` 返回的权限决定，不由请求参数决定。

## 4. 充电健康报告接口

### 4.1 创建计算作业

```http
POST /v1/health-report-jobs
Authorization: Bearer <access_token>
Content-Type: application/json
```

请求：

```json
{ "order_no": "2094370061724549120" }
```

输入字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `order_no` | string | 是 | 已授权充电订单号；只允许字母、数字、下划线、点、冒号和连字符 |

成功响应：HTTP `202`。

```json
{
  "job_id": "hrj_opaque_id",
  "order_no": "2094370061724549120",
  "rule_version": "health-v2",
  "status": "queued",
  "retry_after_ms": 1000,
  "report": null,
  "error": null,
  "created_at": "2026-09-02T05:00:00+00:00",
  "updated_at": "2026-09-02T05:00:00+00:00",
  "completed_at": null
}
```

### 4.2 查询计算作业

```http
GET /v1/health-report-jobs/{job_id}
Authorization: Bearer <access_token>
```

作业状态：

| 状态 | 含义 |
|---|---|
| `queued` | 已创建，等待执行 |
| `running` | 正在读取数据和计算 |
| `completed` | 报告已完成，`report` 有值 |
| `failed` | 核心订单、授权或计算失败 |
| `expired` | 作业或结果超过保留时间 |

完成响应示例：

```json
{
  "job_id": "hrj_opaque_id",
  "order_no": "2094370061724549120",
  "rule_version": "health-v2",
  "status": "completed",
  "retry_after_ms": null,
  "report": {
    "summary": "本次充电健康报告有 1 项指标需关注",
    "indicators": [
      {
        "code": "stop_reason",
        "status": "normal",
        "value": "用户主动停止",
        "unit": null,
        "reference": null,
        "reason_code": null
      }
    ],
    "radar": [],
    "curves": {},
    "health_metrics": {},
    "completeness": 1.0,
    "source_summary": {
      "order_snapshot": "available",
      "telemetry": "available",
      "protocol": "not_requested",
      "vehicle_capacity": "unavailable"
    },
    "data_as_of": "2026-09-02T05:00:02+00:00"
  },
  "error": null
}
```

### 4.3 健康报告字段

健康报告是确定性结果：

- `summary`：后端固定模板生成的摘要，不是模型建议。
- `indicators`：单项健康指标。
- `radar`：五维评分；缺少必要输入时该维度 `score=null`、`status=unavailable`。
- `curves`：功率、电压、温度等曲线；每条 series 最多 300 个点，保留首点、末点和极值。
- `health_metrics`：经单位确认的中间计算值，例如 `soc_delta`、`energy_charged_kwh`、`soh`。
- `completeness`：数据完整度，范围 `0..1`。
- `source_summary`：订单、遥测、协议、车辆容量等来源的可用状态。
- `data_as_of`：本次计算实际使用数据的时间。
- `rule_version`：确定性公式版本。

指标状态只有：

```text
normal | attention | abnormal | unavailable
```

前端或其他调用方只根据状态渲染，不解析中文阈值，也不自行重算。

## 5. 单问诊断接口

### 5.1 创建诊断

```http
POST /v1/standard/diagnoses
Authorization: Bearer <access_token>
Content-Type: application/json
```

请求：

```json
{
  "order_no": "2094370061724549120",
  "question": "为什么这次充电提前停止？",
  "indicator_code": "temperature_balance"
}
```

输入字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `order_no` | string | 是 | 已授权订单号 |
| `question` | string | 是 | 独立问题，最多 4000 字符 |
| `indicator_code` | string | 否 | 稳定指标代码，只提供提问上下文 |

客户端不能提交 `score`、`curve`、完整报告、`user_id` 或 `tenant_id` 作为可信输入。

成功响应 HTTP `202`：

```json
{
  "diagnosis_id": "dx_opaque_id",
  "order_no": "2094370061724549120",
  "question": "为什么这次充电提前停止？",
  "indicator_code": "temperature_balance",
  "status": "queued",
  "retry_after_ms": 1000,
  "result": null,
  "error": null,
  "created_at": "2026-09-02T05:01:00+00:00",
  "updated_at": "2026-09-02T05:01:00+00:00",
  "completed_at": null
}
```

### 5.2 查询诊断

```http
GET /v1/standard/diagnoses/{diagnosis_id}
Authorization: Bearer <access_token>
```

状态：

```text
queued | running | completed | inconclusive | failed | expired
```

`completed` 或 `inconclusive` 时 `result` 才可能有值。诊断结果中的证据 ID、内部 run、provider、workspace、SQL、原始报文和凭据不会进入标准 API 响应。

终态 `failed` 时 `error` 非空，其 `code` 区分失败根因：

| code | 含义 |
|---|---|
| `DIAGNOSIS_ORDER_OUT_OF_SCOPE` | 订单不在当前授权租户内（越权）。创建时的越权订单已由 `404 ORDER_NOT_FOUND` 拒绝；此码是**纵深防御**信号而非必经终态——worker 携带冻结的 `QueryScope`，租户以参数绑定的 SQL 谓词下推，行级规则只会看到 SQL 已放行的行，越权订单因此在创建阶段即被拒绝。仅当"下推之后仍有行未通过行级规则"时出现（某一证据源无法下推，或创建与 worker 之间授权范围发生变化），用途是把授权问题与供应商问题分开，重试不会改变结论 |
| `DIAGNOSIS_BLOCKED` | 模型未按结构化输出 schema 产出结论（供应商能力或配额受限） |
| `DIAGNOSIS_FAILED` | 其余运行失败 |

`error.message` 只说明根因，不含租户标识、SQL、原始报文或内部 run 信息。

### 5.3 历史诊断

```http
GET /v1/standard/diagnoses?limit=50
Authorization: Bearer <access_token>
```

只返回当前授权主体范围内的摘要列表：

```json
{
  "diagnoses": [
    {
      "diagnosis_id": "dx_opaque_id",
      "order_no": "2094370061724549120",
      "question": "为什么这次充电提前停止？",
      "indicator_code": "temperature_balance",
      "status": "completed",
      "created_at": "2026-09-02T05:01:00+00:00",
      "updated_at": "2026-09-02T05:01:04+00:00"
    }
  ]
}
```

第一版是“一问一诊断”，不是多轮会话；调用方自行决定是否把多个结果组合到一个页面。

## 6. 错误响应

标准 API 的失败响应统一为：

```json
{
  "error": {
    "code": "ORDER_NOT_FOUND",
    "message": "order not found",
    "retryable": false
  }
}
```

常见错误：

| HTTP | code | 含义 |
|---:|---|---|
| 401 | `ACCESS_TOKEN_REQUIRED` | 缺少 Bearer token |
| 401 | `INVALID_ACCESS_TOKEN` | token 无效、过期或不是 cloud-auth token |
| 403 | `INSUFFICIENT_SCOPE` | 调用方没有接口所需能力 |
| 400/422 | `INVALID_REQUEST` | 参数格式或字段不合法 |
| 404 | `ORDER_NOT_FOUND` | 订单不存在或不在当前授权范围；不区分两者 |
| 404 | `REPORT_JOB_NOT_FOUND` | 作业不存在或不属于当前调用者 |
| 404 | `DIAGNOSIS_NOT_FOUND` | 诊断不存在或不属于当前调用者 |
| 503 | `ORDER_AUTHORIZATION_UNAVAILABLE` | 权限目录或数据源暂时不可用 |
| 503 | `REPORT_JOB_UNAVAILABLE` | 报告作业暂时不可用 |
| 503 | `DIAGNOSIS_UNAVAILABLE` | 诊断执行暂时不可用 |

内部异常细节只进入脱敏日志，不返回异常类型、SQL、绝对路径、密码或原始响应。

## 7. 调用方最小流程

```text
1. 从 cloud-auth 获取 Bearer access token
2. 调用 POST /v1/health-report-jobs
3. 使用 job_id 轮询 GET /v1/health-report-jobs/{job_id}
4. status=completed 时读取 report
5. 用户主动提问时调用 POST /v1/standard/diagnoses
6. 使用 diagnosis_id 轮询诊断状态
```
