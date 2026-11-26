# AI-Ops 标准后端接口报告

> 2026-11-05适用范围订正：本页健康报告字段仍作参考；第5节单问诊断和第7节旧调用流程为历史合同，
> `/v1/standard/diagnoses` 已退役。现役两端问答使用 `/v1/assistant/questions`，支持有界连续会话，
> 接线以[测试接口文档](testing-api-guide.md)、[系统架构](system-architecture.md)及线上OpenAPI为准。
> 本页旧环境部署表不构成当前可用性证明。

## 1. 这套接口解决什么问题

AI-Ops 提供的是消费端无关的标准后端 HTTP API。调用方可以是业务后端、BFF、Web、移动端接入层或运维平台；接口不绑定小程序页面，也不要求调用方了解 AI-Ops 内部数据库、Gateway、provider 或 Agent 实现。

以下两类说明中，健康报告仍保留，单问诊断仅解释历史：

1. **充电健康报告**：针对一笔已授权充电订单，按确定性规则计算指标、雷达评分、曲线和数据完整度。
2. **单问诊断（已退役接口）**：针对一笔已授权充电订单提交一个独立问题，异步生成诊断状态和诊断结果。

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
third-session: <thirdSession>   # 公网入口头；直达Gateway用X-Third-Session
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
third-session: <thirdSession>   # 公网入口头；直达Gateway用X-Third-Session

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
  "rule_version": "health-v3",
  "status": "queued",
  "language": "zh",
  "retry_after_ms": 1000,
  "report": null,
  "error": null,
  "created_at": "2026-09-02T05:00:00+00:00",
  "updated_at": "2026-09-02T05:00:00+00:00",
  "completed_at": null
}
```

**`language` 的语义**：它描述 **`report` 正文的语言**，即**创建该作业时**的
`Accept-Language` 解析结果 —— 报告在那一刻生成并存储，此后**不再改变**。轮询时带别的
`Accept-Language` 不会改写它（把一份中文报告回显成英文是假陈述）。**只读 + 不缓存**：
请求另一个语言会**新建一个作业**（复用判定含语言），不会把既有作业的正文换语言。

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
  "rule_version": "health-v3",
  "status": "completed",
  "language": "zh",
  "retry_after_ms": null,
  "report": {
    "order_no": "2094370061724549120",
    "order_window": {
      "started_at": "2026-11-26T05:19:03+00:00",
      "stopped_at": "2026-11-26T06:31:22+00:00"
    },
    "summary": "本次充电体检各项已采集指标均处于正常范围。",
    "overall_health_score": 89.2,
    "overall_health_source": "soh",
    "overview": {
      "start_soc": 18.0,
      "end_soc": 99.0,
      "soc_delta": 81.0,
      "energy_charged_kwh": 45.4,
      "duration_seconds": 4339,
      "battery_type": "lfp",
      "overall_health_score": 89.2,
      "overall_health_source": "soh",
      "soh_status": "normal",
      "is_shallow_charge": false
    },
    "conclusion": {
      "level": "normal",
      "code": "ALL_NORMAL",
      "text": "本次充电体检各项已采集指标均处于正常范围。",
      "counts": {
        "normal": 7,
        "attention": 0,
        "abnormal": 0,
        "unavailable": 0
      },
      "triggered_codes": [],
      "advice_codes": []
    },
    "indicators": [
      {
        "code": "stop_reason",
        "status": "normal",
        "value": "车端正常主动停止",
        "reported_value": "车端正常主动停止",
        "reported_language": null,
        "unit": null,
        "reference": null,
        "reason_code": null
      },
      {
        "code": "battery_temp_delta",
        "status": "normal",
        "value": 3.0,
        "reported_value": null,
        "reported_language": null,
        "unit": "°C",
        "reference": "<= 5",
        "reason_code": null
      },
      {
        "code": "temp_rise_rate",
        "status": "normal",
        "value": 0.2,
        "reported_value": null,
        "reported_language": null,
        "unit": "°C/min",
        "reference": "<= 0.3",
        "reason_code": null
      },
      {
        "code": "battery_max_temp",
        "status": "normal",
        "value": 35.0,
        "reported_value": null,
        "reported_language": null,
        "unit": "°C",
        "reference": "<= 45 (safe <= 55)",
        "reason_code": null
      },
      {
        "code": "soc_change_rate",
        "status": "normal",
        "value": 1.2,
        "reported_value": null,
        "reported_language": null,
        "unit": "%/min",
        "reference": "<= 5 (15s jump < 3%)",
        "reason_code": null
      },
      {
        "code": "cell_voltage_delta",
        "status": "normal",
        "value": 50.0,
        "reported_value": null,
        "reported_language": null,
        "unit": "mV",
        "reference": "<= 60 (pass <= 150)",
        "reason_code": null
      },
      {
        "code": "cell_max_voltage",
        "status": "normal",
        "value": 3.47,
        "reported_value": null,
        "reported_language": null,
        "unit": "V",
        "reference": "<= 3.65",
        "reason_code": null,
        "probe_no": 72
      }
    ],
    "radar": [
      { "code": "temperature_balance", "score": 94.0, "status": "normal", "reason_code": null },
      { "code": "max_temperature", "score": 100.0, "status": "normal", "reason_code": null },
      { "code": "capacity", "score": 92.3, "status": "normal", "reason_code": null },
      { "code": "voltage_balance", "score": 92.8, "status": "normal", "reason_code": null },
      { "code": "soc_balance", "score": 100.0, "status": "normal", "reason_code": null }
    ],
    "curves": {
      "power": { "series": [], "sample_range": null, "original_points": 0, "output_points": 0 },
      "voltage": { "series": [], "sample_range": null, "original_points": 0, "output_points": 0 },
      "temperature": { "series": [], "sample_range": null, "original_points": 0, "output_points": 0 }
    },
    "health_metrics": {
      "temperature_delta": 3.0,
      "max_temperature": 35.0,
      "temp_rise_rate_c_per_min": 0.2,
      "soc_delta": 81.0,
      "soc_change_rate_pct_per_min": 1.2,
      "soc_jumps_15s": 0,
      "soc_alarm_count": 0,
      "energy_charged_kwh": 45.4,
      "nominal_capacity_kwh": 59.0,
      "nominal_capacity_source": "ykc_0x17",
      "soh": 89.2,
      "soh_shallow_estimate": null,
      "soh_status": "normal",
      "is_shallow_charge": false,
      "battery_type": "lfp",
      "cell_voltage_delta_end_mv": 50.0,
      "cell_voltage_delta_process_mv": null,
      "voltage_balance_basis": "settlement_0x19",
      "max_cell_voltage_v": 3.47,
      "max_cell_voltage_probe_no": 72
    },
    "completeness": 1.0,
    "rule_version": "health-v3",
    "source_summary": {
      "order_snapshot": "available",
      "telemetry": "available",
      "protocol": "available",
      "vehicle_capacity": "available"
    },
    "data_as_of": "2026-11-26T06:32:00+00:00"
  },
  "error": null
}
```

### 4.3 健康报告字段（`health-v3`）

健康报告是确定性结果（完整公式与阈值见[电池检查指标规格](specs/battery-inspection-metrics.md)）：

- `summary`：后端确定性分档模板生成的本地化摘要（与 `conclusion.text` 一致），不是模型建议。
- `overall_health_score` / `overall_health_source`：总健康分（`health-v3` 固定取有效 `SOH`，`overall_health_source = "soh"`）；浅充（`ΔSOC < 30%`）或缺少标称容量时为 `null`。
- `overview`：顶部卡片概览（`start_soc`、`end_soc`、`soc_delta`、`energy_charged_kwh`、`duration_seconds`、`battery_type`、`overall_health_score`、`soh_status`、`is_shallow_charge`）。
- `conclusion`：结构化确定性结论，含 `level`（`normal | attention | abnormal | insufficient`）、`code`（`ALL_NORMAL | ATTENTION_REQUIRED | ABNORMAL_DETECTED | INSUFFICIENT_DATA`）、`text`（11 语本地化结论与分维度建议）、`counts`、`triggered_codes`、`advice_codes`。
- `indicators`：固定 7 项单项健康指标，按 `code` 依次为：

| `code` | 指标 | `unit` | `reference` | `value` 语义 | 常见缺失 `reason_code` |
|---|---|---|---|---|---|
| `stop_reason` | 停止充电原因 | `null` | `null` | **本地化文案**（11 语，非中文不含汉字）；上游原文保留在 `reported_value`（语言不保证，`reported_language` 现为 `null`） | `SOURCE_DATA_MISSING` |
| `battery_temp_delta` | 电池温差 | `°C` | `<= 5` | 数值（同帧 `max(T_max - T_min)`） | `TEMP_MIN_DATA_MISSING` / `TELEMETRY_DATA_MISSING` |
| `temp_rise_rate` | 温升速率 | `°C/min` | `<= 0.3` | 数值（5 分钟滑窗最大温升速率） | `TEMP_SERIES_INSUFFICIENT` |
| `battery_max_temp` | 电池最高温 | `°C` | `<= 45 (safe <= 55)` | 数值（全程最高电池温度） | `TEMP_DATA_MISSING` |
| `soc_change_rate` | SOC 变化率 | `%/min` | `<= 5 (15s jump < 3%)` | 数值（1 分钟滑窗最大速率；若 15s 跳变 ≥ 3% 亦判 `abnormal`） | `SOC_SERIES_INSUFFICIENT` |
| `cell_voltage_delta` | 单体蓄电池电压极差 | `mV` | `<= 60 (pass <= 150)` | 数值（优先 `0x19` 结算极差） | `CELL_VOLTAGE_DATA_MISSING` |
| `cell_max_voltage` | 单体蓄电池最高压 | `V` | `<= 3.65`（LFP）/ `<= 4.20`（NCM） | 数值；可选附 `probe_no`（1~256 探针编号） | `CELL_VOLTAGE_DATA_MISSING` / `BATTERY_TYPE_UNKNOWN` |

- `radar`：五维评分（`temperature_balance`、`max_temperature`、`capacity`、`voltage_balance`、`soc_balance`，均 `0..100`）；缺少必要输入或浅充时该维度 `score = null`、`status = "unavailable"` 且给出 `reason_code`。
- `curves`：功率（`power` kW、`outputCurrent` A）、电压（`outputVoltage`、`bmsMeasuredVoltage`、`maxCellVoltage` V）、温度（`temperature`、`batteryMaxTemperature`、`batteryMinTemperature`、`batteryTempDelta` degC）三组曲线；每条 series 最多 300 个点，保留首点、末点和极值；`name` 按作业语言本地化。
- `health_metrics`：经单位确认的中间计算值（含 `soh`、`soh_shallow_estimate`、`soh_status`、`is_shallow_charge`、`nominal_capacity_kwh`、`nominal_capacity_source`、`voltage_balance_basis`、`max_cell_voltage_probe_no` 等）。
- `completeness`：7 项体检指标中已采集项的占比，范围 `0..1`。
- `source_summary`：`order_snapshot`、`telemetry`、`protocol`、`vehicle_capacity` 四类数据源的可用状态（`available | unavailable | not_requested`）。
- `data_as_of`：本次计算实际使用数据的时间。
- `rule_version`：确定性公式版本（现行 `health-v3`，与外层作业行 `rule_version` 严格相等）。

指标状态只有：

```text
normal | attention | abnormal | unavailable
```

前端或其他调用方只根据 `status`、`conclusion.level` 和数值字段渲染，不解析中文阈值，也不自行重算；前端联调步骤与页面字段映射见[报告页前端联调交接手册](agents/frontend-battery-report-handoff.md)。

## 5. 单问诊断接口（历史，现役返回404）

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
queued | running | completed | inconclusive | failed | expired | cancelled
```

`cancelled`（PRD #346 新增，语义是"用户主动停止"）与`assistant_questions` 共用同一套终态状态集，因此出现在诊断枚举里；诊断没有独立的取消入口，**不会**在诊断线产出该值——出现即视为契约破裂。`cancelled` 时 `result` 与 `error` 均为 `null`，`retry_after_ms` 为 `null`（终态不轮询）。

`completed` 或 `inconclusive` 时 `result` 才可能有值。诊断结果中的证据 ID、内部 run、provider、workspace、SQL、原始报文和凭据不会进入标准 API 响应。

终态 `failed` 时 `error` 非空，其 `code` 区分失败根因：

| code | 含义 |
|---|---|
| `DIAGNOSIS_ORDER_OUT_OF_SCOPE` | 订单不在当前授权租户内（越权）。创建时的越权订单已由 `404 ORDER_NOT_FOUND` 拒绝；此码是**纵深防御**信号而非必经终态——worker 携带冻结的 `QueryScope`，租户以参数绑定的 SQL 谓词下推，行级规则只会看到 SQL 已放行的行，越权订单因此在创建阶段即被拒绝。仅当"下推之后仍有行未通过行级规则"时出现（某一证据源无法下推，或创建与 worker 之间授权范围发生变化），用途是把授权问题与供应商问题分开，重试不会改变结论 |
| `DIAGNOSIS_BLOCKED` | 模型未按结构化输出 schema 产出结论（供应商能力或配额受限） |
| `DIAGNOSIS_FAILED` | 其余运行失败 |

### 5.2.1 错误信封的本地化约定（`code` 与 `message` 的分工）

消费者自由提问的分类依赖故障仍返回503。明确额度错误返回
`CONSUMER_MODEL_QUOTA_EXHAUSTED`且`retryable=false`；普通鉴权拒绝仍为
`CONSUMER_INTENT_UNAVAILABLE`且不可自动重试，限流/网络/上游故障保留可重试标记。
此处请求级`message`是下文通则的明确例外：客户端直接展示本地化服务提示，不把模型服务故障描述成用户表达不清。
`retryable`描述自动重试是否有意义，额度/凭据恢复后允许用户重新发起。
受管消费者草稿结果另返回固定本地化`error_message`；不回传存储或供应商的异常正文。
日志只含本地原因枚举及HTTP状态。供应商403兼容规则与历史边界见
[自由提问排查手册](agents/consumer-wf01/free-question-failures.md)。

错误信封在所有面共用同一形状 `{"code", "message", "retryable"}`，但**两者的用途不同**，
这一点此前只存在于实现里、从未写进契约（#538）。

| 字段 | 语言 | 谁消费 |
|---|---|---|
| `code` | **英文，不随语言变化** | **客户端按它分支**并渲染自己的文案。它是契约的一部分，取值集合稳定 |
| `message` | **不作为用户文案**（见下） | 开发者与运维排查用；**客户端不应把它当作面向用户的展示文案**，除非下文明确列出例外 |

**为什么 `message` 不翻译**：它是**开发者可读**的诊断信息，不是产品文案。把它当文案翻译，
等于承诺为 53 处内部错误串各维护 11 种语言 —— 而它们描述的是路由、鉴权与请求形状，
用户读了也没有用。用户能读的那句话由**客户端按 `code`** 渲染（例如诊断失败按
`DIAGNOSIS_ORDER_OUT_OF_SCOPE` / `DIAGNOSIS_BLOCKED` / `DIAGNOSIS_FAILED` 分三句，
见前端接口说明）。

**例外：两个作业面的 `message` 是本地化的。** 与请求级不同，下列**异步作业**失败时
的 `message` 随请求语言变化：

| 作业面 | `message` 现状 | 客户端被要求 |
|---|---|---|
| 统一助手 QA（`/v1/assistant/questions`） | **本地化** | **直接渲染它**（场景 B）⇒ 本地化是**必要的** |
| 单问诊断（`/v1/standard/diagnoses`） | **本地化** | 按 `code` 分支，**不渲染 `message`** ⇒ 本地化**客户端不消费** |
| 健康报告（`/v1/health-report-jobs`） | **不本地化**（直出存储的 `error_message`） | 按 `code` 分支 |

**这张表是穷举的**：上面三个是当前全部异步作业面。**不要把「作业态」当成一条通则** ——
健康报告就在同一类面里、却不本地化；把它一并概括进去会让契约与实现不符。

⇒ 请求级信封通常不本地化；本节明确列出的消费者分类依赖错误除外。作业态逐面而异，QA与受管消费者草稿按各自字段展示固定本地化提示。

这条区分此前只在实现里，契约没写；本版把它写下来，因为"统一把信封都翻掉"或
"统一都别翻"两种改动都会各自打破某个面的既有约定。**诊断那一条**若要改成纯英文形态，
改的是**服务端**（不再本地化该字段）——它与客户端如何渲染无关，因为客户端本来就不渲染它。

`message` 无论哪条路径都**不含**租户标识、SQL、原始报文、内部 run/turn id、
provider 凭据或账户额度；上游异常串一律**不**直接透出（见 `_internal_error_message`
与各面的有界文案表）。

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
| 403 | `INSUFFICIENT_SCOPE` | 凭据断言的 scope 集合不含本接口所需 scope。**只有断言 scope 的凭据链（`ASSERTED` 档）会产生此错**；会话/公司令牌/UPMS 链不做 scope 成员判定，那里的能力判据是角色门（见 GLOSSARY「能力来源档」） |
| 400/422 | `INVALID_REQUEST` | 参数格式或字段不合法 |
| 404 | `ORDER_NOT_FOUND` | 订单不存在或不在当前授权范围；不区分两者 |
| 404 | `REPORT_JOB_NOT_FOUND` | 作业不存在或不属于当前调用者 |
| 404 | `DIAGNOSIS_NOT_FOUND` | 诊断不存在或不属于当前调用者 |
| 503 | `ACCESS_TOKEN_VALIDATION_UNAVAILABLE` | 调用方认证暂时不可用（含网关自身配置缺失，fail closed）。**按 503 处理：可重试，不要引导重新登录** |
| 503 | `ORDER_AUTHORIZATION_UNAVAILABLE` | 权限目录或数据源暂时不可用 |
| 503 | `REPORT_JOB_UNAVAILABLE` | 报告作业暂时不可用 |
| 503 | `DIAGNOSIS_UNAVAILABLE` | 诊断执行暂时不可用 |

内部异常细节只进入脱敏日志，不返回异常类型、SQL、绝对路径、密码或原始响应。

## 7. 调用方最小流程（历史单问诊断）

```text
1. 从 cloud-auth 获取 Bearer access token
2. 调用 POST /v1/health-report-jobs
3. 使用 job_id 轮询 GET /v1/health-report-jobs/{job_id}
4. status=completed 时读取 report
5. 用户主动提问时调用 POST /v1/standard/diagnoses
6. 使用 diagnosis_id 轮询诊断状态
```
