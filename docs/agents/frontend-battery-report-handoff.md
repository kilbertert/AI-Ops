# 智能电池检查报告：前端接口与联调文档（`health-v3`，可直接转发前端）

> **用途**：前端组将报告页（`/aiPackage/pages/batteryReport/batteryReport`）从静态示例数据改接 AI-Ops 后端接口的完整联调文档。
> **关联任务**：Issue #602（承接 [frontend-banner-handoff.md](frontend-banner-handoff.md) §0 报告页欠账）、ADR-0353。
> **上线前提**：请先确认目标环境 Gateway 已升级至含 `health-v3` 的版本（`POST /v1/health-report-jobs` 返回 `"rule_version": "health-v3"`）。若当前环境仍返回 `health-v2`，说明后端尚未发布。

---

## 1. 接口总览（就这 2 个接口）

- **公网 Base URL（41 正式/测试环境）**：`https://api.mall.qushiyun.com`
- **交互模式**：异步作业（**创建作业 → 轮询结果**），不复用聊天/问答接口，无独立取消接口。

| 序号 | 方法与路径 | 用途 | 成功状态码 |
|---|---|---|---|
| 1 | `POST /v1/health-report-jobs` | 为指定已完成充电订单创建（或复用）健康报告计算作业 | `202 Accepted` |
| 2 | `GET /v1/health-report-jobs/{job_id}` | 轮询作业状态，终态 `completed` 时返回完整报告 `report` | `200 OK` |

### 1.1 请求头约定（⚠️ C 端不要传错）

| 请求头 | C 端（用户 App / H5） | 管家端（如需复用） | 说明 |
|---|---|---|---|
| `third-session` | **必填**（当前 C 端登录态） | 不发送 | 公网发送 `third-session`，**不是** `X-Third-Session` |
| `Authorization` | **不发送**（由公网 Nginx 自动注入） | 必填 `Bearer <公司 JWT>` | C 端发用户态依赖 `third-session` |
| `X-Business-Entry` | `consumer` | `operator` | 入口标识 |
| `Accept-Language` | 推荐显式传（如 `zh` / `en` / `th` / `vi`） | 同左 | 决定报告正文语言（11 语支持：`zh`、`zh-Hant`、`en`、`de`、`fr`、`es`、`pt`、`vi`、`mn`、`th`、`km`） |
| `Content-Type` | `application/json`（仅 POST） | 同左 | JSON 请求体 |

> **语言冻结规则**：报告正文在 `POST` 创建作业时按 `Accept-Language` 生成并落库；切换 App 语言后重新 `POST` 同一订单号即可，后端会自动新建对应语言的 `job_id`（复用键包含语言，不会串语）。

---

## 2. 接口详细定义与真实样例

### 2.1 创建健康报告作业：`POST /v1/health-report-jobs`

**请求体**：

```json
{
  "order_no": "2094370061724549120"
}
```

- `order_no`（string，必填）：**已结束**的本人充电订单号（仅允许字母、数字、下划线、点、冒号、连字符）。
- 若从横幅无参进入报告页，先调公司既有接口取最近一笔已完成订单：`getOrderUserPage?page=1&size=1&status=1&excludeHomeOrder=1` → 取首条 `orderNo`。无已完成订单时直接展示空态，不调本接口。

**成功响应（HTTP `202`）**：

```json
{
  "job_id": "hrj_01jexample9x8y7z",
  "order_no": "2094370061724549120",
  "rule_version": "health-v3",
  "status": "queued",
  "language": "zh",
  "retry_after_ms": 1000,
  "report": null,
  "error": null,
  "created_at": "2026-11-26T06:32:00+00:00",
  "updated_at": "2026-11-26T06:32:00+00:00",
  "completed_at": null
}
```

> 同一账号 + 同一订单 + 同一语言若已有未过期的运行中或已完成作业，后端会直接复用并返回已有 `job_id`（若已完成，响应里 `status` 直接为 `"completed"` 且已带 `report`，可直接渲染无需再轮询）。

---

### 2.2 轮询查询结果：`GET /v1/health-report-jobs/{job_id}`

**作业状态流转**：

- `queued` / `running`：继续按 `retry_after_ms`（默认 `1000` ms）轮询，建议总超时 `35s`；
- `completed`：停止轮询，用 `report` 渲染整页；
- `failed`：停止轮询，按 `error.code` 与 `error.retryable` 展示错误提示或「重试」按钮；
- `expired`：停止轮询，提示结果已过期并允许用户重新 `POST`。

**完成态完整响应样例（HTTP `200`，`status = "completed"`）**：

```json
{
  "job_id": "hrj_01jexample9x8y7z",
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
    "overall_health_score": 89.3,
    "overall_health_source": "soh",
    "overview": {
      "start_soc": 18.0,
      "end_soc": 99.0,
      "soc_delta": 81.0,
      "energy_charged_kwh": 45.4,
      "duration_seconds": 4339,
      "battery_type": "lfp",
      "overall_health_score": 89.3,
      "overall_health_source": "soh",
      "soh_status": "normal",
      "is_shallow_charge": false
    },
    "conclusion": {
      "level": "normal",
      "code": "ALL_NORMAL",
      "text": "本次充电体检各项已采集指标均处于正常范围。",
      "counts": { "normal": 7, "attention": 0, "abnormal": 0, "unavailable": 0 },
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
      { "code": "capacity", "score": 92.4, "status": "normal", "reason_code": null },
      { "code": "voltage_balance", "score": 92.8, "status": "normal", "reason_code": null },
      { "code": "soc_balance", "score": 100.0, "status": "normal", "reason_code": null }
    ],
    "curves": {
      "power": {
        "series": [
          { "name": "实际功率", "field": "power", "unit": "kW", "points": [[1795670400.0, 48.5], [1795674000.0, 12.1]] },
          { "name": "输出电流", "field": "outputCurrent", "unit": "A", "points": [[1795670400.0, 138.0], [1795674000.0, 32.0]] }
        ],
        "sample_range": [1795670400.0, 1795674000.0],
        "original_points": 240,
        "output_points": 240
      },
      "voltage": {
        "series": [
          { "name": "输出电压", "field": "outputVoltage", "unit": "V", "points": [[1795670400.0, 351.2], [1795674000.0, 378.4]] },
          { "name": "BMS 充电电压", "field": "bmsMeasuredVoltage", "unit": "V", "points": [[1795670400.0, 350.0], [1795674000.0, 378.0]] },
          { "name": "单体最高压", "field": "maxCellVoltage", "unit": "V", "points": [[1795670400.0, 3.35], [1795674000.0, 3.47]] }
        ],
        "sample_range": [1795670400.0, 1795674000.0],
        "original_points": 240,
        "output_points": 240
      },
      "temperature": {
        "series": [
          { "name": "最高温度", "field": "batteryMaxTemperature", "unit": "degC", "points": [[1795670400.0, 26.0], [1795674000.0, 35.0]] },
          { "name": "最低温度", "field": "batteryMinTemperature", "unit": "degC", "points": [[1795670400.0, 24.0], [1795674000.0, 32.0]] },
          { "name": "实时温差", "field": "batteryTempDelta", "unit": "degC", "points": [[1795670400.0, 2.0], [1795674000.0, 3.0]] }
        ],
        "sample_range": [1795670400.0, 1795674000.0],
        "original_points": 240,
        "output_points": 240
      }
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
      "soh": 89.3,
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
    "data_as_of": "2026-11-26T06:32:01+00:00"
  },
  "error": null,
  "created_at": "2026-11-26T06:32:00+00:00",
  "updated_at": "2026-11-26T06:32:01+00:00",
  "completed_at": "2026-11-26T06:32:01+00:00"
}
```

**浅充订单（`ΔSOC < 30%`）关键字段形态**：

```json
{
  "overall_health_score": null,
  "overall_health_source": "soh",
  "overview": {
    "start_soc": 62.0,
    "end_soc": 88.0,
    "soc_delta": 26.0,
    "overall_health_score": null,
    "soh_status": "shallow_charge",
    "is_shallow_charge": true
  },
  "health_metrics": {
    "soh": null,
    "soh_shallow_estimate": 91.4,
    "soh_status": "shallow_charge",
    "is_shallow_charge": true
  }
}
```

**失败态响应样例（HTTP `200`，`status = "failed"`）**：

```json
{
  "job_id": "hrj_01jexample9x8y7z",
  "order_no": "2094370061724549120",
  "rule_version": "health-v3",
  "status": "failed",
  "language": "zh",
  "retry_after_ms": null,
  "report": null,
  "error": {
    "code": "ORDER_NOT_ENDED",
    "message": "order has not ended",
    "retryable": false
  }
}
```

---

## 3. 前端怎么联调（可直接复制执行）

### 3.1 命令行 `curl` 冒烟（拿到自己的 `third-session` 和本人订单号即可测）

```bash
BASE_URL="https://api.mall.qushiyun.com"
C_SESSION="<当前登录 C 端 third-session>"
ORDER_NO="<本人已结束充电订单号>"

# 1) 创建作业（预期 HTTP 202，记住返回的 job_id）
JOB_ID=$(curl -sS -X POST "$BASE_URL/v1/health-report-jobs" \
  -H "third-session: $C_SESSION" \
  -H "X-Business-Entry: consumer" \
  -H "Accept-Language: zh" \
  -H "Content-Type: application/json" \
  --data-raw "{\"order_no\":\"$ORDER_NO\"}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('job_id','')); print(d, file=sys.stderr)")

# 2) 轮询结果（间隔 1s，直到 status 为 completed 或 failed）
curl -sS "$BASE_URL/v1/health-report-jobs/$JOB_ID" \
  -H "third-session: $C_SESSION" \
  -H "X-Business-Entry: consumer" | python3 -m json.tool
```

### 3.2 `uni-app` 前端调用范例（复用既有 `aiPackage/api/aiops.js`）

`qumall-app/aiPackage/api/aiops.js` 里已经封装了 `createHealthReportJob(orderNo)` 和 `getHealthReportJob(jobId)`（注意确认当前分支的 `aiops.js` 发的是 C 端 `third-session` + `X-Business-Entry: consumer`，不要误发成 `operator`）：

```js
import aiopsApi from '@/aiPackage/api/aiops.js'

async function loadBatteryReport(orderNo) {
  const created = await aiopsApi.createHealthReportJob(orderNo)
  let current = created
  const deadline = Date.now() + 35000

  while (current.status === 'queued' || current.status === 'running') {
    if (Date.now() > deadline) {
      throw { code: 'POLL_TIMEOUT', retryable: true }
    }
    await new Promise((r) => setTimeout(r, current.retry_after_ms || 1000))
    current = await aiopsApi.getHealthReportJob(created.job_id)
  }

  if (current.status !== 'completed' || !current.report) {
    throw current.error || { code: 'REPORT_FAILED', retryable: true }
  }
  return current.report
}
```

---

## 4. `batteryReport.vue` 页面组件 ↔ `report` 字段映射

### 4.1 顶部概览卡片

- **车辆头部（车牌 / 车型 / 车图）**：沿用 [frontend-banner-handoff.md §1.2–1.3](frontend-banner-handoff.md) 的公司端点（`myCarList` + `chCarSeries`），AI-Ops 报告接口不返回车牌与车图。
- **报告核心数字**：

| 页面展示位 | 取值路径 | 缺失 / 特殊态渲染规则 |
|---|---|---|
| 初始 SOC / 结束 SOC | `report.overview.start_soc` / `report.overview.end_soc`（单位 `%`） | 任一为 `null` 时显示 `--`，进度条不硬填假百分比 |
| 充电起止时间 | `report.order_window.started_at` / `stopped_at`（UTC ISO-8601） | 按用户本地时区格式化显示 |
| 本次充电度数 | `report.overview.energy_charged_kwh`（单位 `kWh`） | 为 `null` 时显示 `--` |
| **电池健康（原型 `89.0`）** | `report.overall_health_score`（直接取有效 SOH） | 若 `overview.is_shallow_charge === true`：显示「浅充暂不评估」（可用小字附注 `health_metrics.soh_shallow_estimate` 仅供参考，切勿当正式健康分）；若为 `null` 且非浅充：显示 `--` |
| 充电时长 | `report.overview.duration_seconds`（秒） | 格式化为 `HH:mm` 或 `mm:ss` |

### 4.2 充电体检单卡片

- **结论文案**：直接渲染 `report.conclusion.text`（等同 `report.summary`，已按 `Accept-Language` 本地化，11 语齐全，非中文无汉字）。
- **结论态样式**：按 `report.conclusion.level` 分色（`normal` / `attention` / `abnormal` / `insufficient`），**禁止解析文案关键词**。
- **7 项体检指标行**（遍历 `report.indicators[]`，按 `code` 映射多语标题，按 `status` 标红/标黄/置灰）：

| `code` | 中文展示名 | 展示格式 | `status` 渲染 |
|---|---|---|---|
| `stop_reason` | 停止充电原因 | 主文本用 `value`（已本地化）；`reported_value` 仅在需要展示“桩端原话”时以引用样式附注 | `abnormal` 标红、`attention` 标黄、`normal` 正常、`unavailable` 显示「暂无数据」 |
| `battery_temp_delta` | 电池温差 | `{value} / 5°C` | 同上 |
| `temp_rise_rate` | 温升速率 | `{value} / 0.3°C/min` | 同上 |
| `battery_max_temp` | 电池最高温 | `{value} / 45°C`（安全上限 55°C） | 同上 |
| `soc_change_rate` | SOC 变化率 | `{value} / 5%/min` | 同上 |
| `cell_voltage_delta` | 单体蓄电池电压极差 | `{value} / 150mV`（优质 ≤ 60mV） | 同上 |
| `cell_max_voltage` | 单体蓄电池最高压 | 若有 `probe_no` 拼成 `#{probe_no} {value}`，否则 `{value}`；参考值按 `overview.battery_type`：`lfp` → `3.65V`，`ncm` → `4.2V` | 同上；`reason_code === "BATTERY_TYPE_UNKNOWN"` 时可展示实测电压并注明“电池类型未知，暂不判阈” |

### 4.3 五维雷达图卡片（⚠️ 修复原 Demo 的 6 维与 `max: 220` 错配）

`report.radar[]` 固定返回 **5 个维度**，分值范围 **`0 ~ 100`**（客户端 `radarOpts.extra.radar.max` 必须改为 `100`，分类改为以下 5 项）：

| `code` | 维度名 | 分值 | 缺失态 |
|---|---|---|---|
| `soc_balance` | SOC 均衡性 | `score`（0~100） | `score === null`（`status === "unavailable"`）时在图例/列表标 `--`，不造假分 |
| `temperature_balance` | 温度均衡性 | `score`（0~100） | 同上 |
| `max_temperature` | 最高温度 | `score`（0~100） | 同上 |
| `capacity` | 电池容量 | `score`（0~100） | 浅充（`reason_code === "SHALLOW_CHARGE"`）或无标称容量时为 `null` |
| `voltage_balance` | 电压均衡性 | `score`（0~100） | 同上；`health_metrics.voltage_balance_basis` 说明口径来源（`process` / `settlement_0x19`） |

### 4.4 三张底部时序曲线卡片

- `report.curves` 含 `temperature`、`voltage`、`power` 三个分组。
- `series[].points` 每项为 `[unix_timestamp_seconds, numeric_value]`，最多 300 点（已保首尾与极值），X 轴按用户本地时区格式化为 `HH:mm`。
- `series[].name` 已随作业语言本地化，可直接用作图例；`power` 单位已统一为 `kW`。
- 某分组 `series.length === 0` 时展示「本次充电暂无曲线数据」空态，不渲染示例折线。
- 规格中可选的「单体平均电压偏差」曲线依赖车型电芯串联数 `N`，41 现网车辆规格表暂无 `N` 数据，本版暂不下发该序列（后端已预留字段，前端按 `series` 列表动态渲染即可）。

---

## 5. 错误码与重试策略

| 阶段 / `error.code` | 含义 | 前端建议交互 |
|---|---|---|
| `POST` `401` | 未登录或 `third-session` 过期 | 引导重新登录 |
| `POST` `404 ORDER_NOT_FOUND` | 订单不存在或不属于当前登录账号 | 提示「未找到对应充电订单」 |
| `POST` `503 ORDER_AUTHORIZATION_UNAVAILABLE` | 订单权限核验上游暂不可用（`retryable: true`） | 展示「稍后重试」按钮 |
| 作业 `failed` + `ORDER_NOT_ENDED` | 订单仍在充电中，未结算 | 提示「充电尚未结束，请在充电完成后查看体检报告」 |
| 作业 `failed` + `ORDER_WINDOW_INVALID` / `ORDER_WINDOW_TOO_LARGE` / `DEVICE_MISSING` | 核心订单元数据不完整（不可重试） | 提示「该订单缺少生成报告所需的完整充电记录」 |
| 作业 `failed` + `retryable: true`（`SOURCE_UNAVAILABLE` / `REPORT_TIMEOUT` / `REPORT_INTERRUPTED_BY_RESTART`） | 数据源或服务瞬时中断 | 展示「重新生成」按钮重新 `POST` |

---

## 6. 现网数据边界（出现 `unavailable` 不等于接口故障）

1. **YKC 协议近 14 天订单**：41 上 `charging-pile_comm` 保留约 14 天，可真实驱动全部 7 项指标、SOH 总分、五维雷达与三类曲线；若某单未上报 `0x25`，`battery_temp_delta` 降级由 `0x19` 兜底或标 `unavailable`。
2. **OCPP / 非 YKC 订单或超过 14 天的历史订单**：无 YKC BMS 帧，正常产出 `stop_reason`、`battery_max_temp`、`temp_rise_rate`、`soc_change_rate` 与基础曲线，其余缺数据项返回 `status = "unavailable"` + `reason_code`。
3. **浅充订单（`ΔSOC < 30%`）**：`overall_health_score` 与 `radar[capacity].score` 为 `null`，前端需展示「浅充暂不评估」。

---

## 7. 前端联调自检清单

- [ ] 核对请求头：C 端发 `third-session` + `X-Business-Entry: consumer` + `Accept-Language`，不发 `Authorization`
- [ ] 移除 `batteryReport.vue` 内所有硬编码示例车牌/车型/SOC/指标/6 维雷达/`2018~2023` 曲线
- [ ] `POST /v1/health-report-jobs` → 轮询 `GET /v1/health-report-jobs/{job_id}` 至 `completed`
- [ ] `overall_health_score === null` 且 `is_shallow_charge === true` 时正确展示浅充说明
- [ ] 7 项指标按 `status` 分色，`unavailable` 展示缺数据态而非 `null`/`NaN`
- [ ] 雷达图切为 5 维且 `max = 100`，`score === null` 不误当 0 分
- [ ] 非中文请求下核对 `conclusion.text`、`stop_reason.value` 与曲线图例均无中文残留
