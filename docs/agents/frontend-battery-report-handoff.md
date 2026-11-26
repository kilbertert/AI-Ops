# 智能电池检查报告页：前端联调交接手册（`health-v3`）

> **读者**：客户端 / H5 前端组（承接 [frontend-banner-handoff.md](frontend-banner-handoff.md) §0 的报告页接线欠账，对应 Issue #602）。
> **状态**：AI-Ops 服务端 **`health-v3`** 已实现（见 ADR-0350 与 [电池检查指标规格](../specs/battery-inspection-metrics.md)），合并发布部署至目标环境后可直接联调；`qumall-app` 侧 `batteryReport.vue` 当前仍为静态示例页，需按本手册改接 `/v1/health-report-jobs`。
> **权威契约**：[standard-api-contract.md §4](../standard-api-contract.md#4-充电健康报告接口) 与 `GET /v1/openapi.json`。

---

## 1. 一次页面加载的完整调用链

报告页沿用既有异步作业端点，**不新增接口、不改鉴权头**（与 `qumall-app/aiPackage/api/aiops.js` 已封装的 `createHealthReportJob` / `getHealthReportJob` 一致）：

```text
1. 确定要查的订单号 order_no
   - 若从订单详情或聊天页带参进入，优先用路由参数 order_no；
   - 若从聊天页横幅无参进入，按 frontend-banner-handoff.md §0 调公司既有接口取最近一笔已完成充电单：
     GET getOrderUserPage?page=1&size=1&status=1&excludeHomeOrder=1 → 取第一条的 orderNo
   - 无已完成订单时：直接展示「暂无已完成充电单，完成一次充电后可查看报告」，不调用 AI-Ops。

2. 发起报告计算作业
   POST /v1/health-report-jobs
   Authorization: Bearer <助手令牌> (公网走既有 nginx 注入 + X-Third-Session)
   X-Business-Entry: consumer
   Accept-Language: <当前 App 语言，如 zh / en / th / vi ...>
   Body: { "order_no": "<orderNo>" }
   ⇒ HTTP 202，返回 { job_id, rule_version: "health-v3", status, language, retry_after_ms }

3. 轮询直到终态（completed / failed / expired）
   GET /v1/health-report-jobs/{job_id}
   - status 为 queued / running 时，按 retry_after_ms（默认 1000ms）轮询，建议总超时 35s；
   - status == "completed"：用响应里的 report 渲染整页；
   - status == "failed"：读取 error.code 与 error.retryable（true 时展示「重试」按钮重新 POST）。
```

**车辆头部信息（车牌 / 车型 / 车图）**：沿用 [frontend-banner-handoff.md §1.2–1.3](frontend-banner-handoff.md) 的公司端点（`myCarList` + `chCarSeries`），AI-Ops 报告响应不返回车牌与车图。

---

## 2. 报告页组件 ↔ `report` 字段逐项映射

### 2.1 顶部概览卡片

| 页面展示位 | 取值路径 | 缺失 / 特殊态渲染规则 |
|---|---|---|
| 初始 SOC / 结束 SOC | `report.overview.start_soc` / `report.overview.end_soc`（单位 `%`） | 任一为 `null` 时显示 `--`，进度条不硬填假百分比 |
| 充电起止时间 | `report.order_window.started_at` / `stopped_at`（UTC ISO-8601） | 按用户本地时区格式化显示 |
| 本次充电度数 | `report.overview.energy_charged_kwh`（单位 `kWh`） | 为 `null` 时显示 `--` |
| **电池健康（原型 `89.0`）** | `report.overall_health_score`（等同 `report.overview.overall_health_score`，直接取有效 SOH） | 若 `overview.is_shallow_charge === true`：显示「浅充暂不评估」（可用小字注明 `health_metrics.soh_shallow_estimate` 仅供参考，切勿当正式健康分）；若为 `null` 且非浅充：显示 `--`（缺标称容量或电量） |
| 充电时长 | `report.overview.duration_seconds`（秒） | 格式化为 `HH:mm` 或 `mm:ss` |

### 2.2 充电体检单卡片

- **结论文案**：直接渲染 `report.conclusion.text`（等同 `report.summary`，已按创建作业时的 `Accept-Language` 本地化，11 语齐全，非中文无汉字）。
- **结论态样式**：按 `report.conclusion.level` 分色，**不解析文案关键词**：
  - `normal`：正常态
  - `attention`：提示/关注态
  - `abnormal`：告警态
  - `insufficient`：灰态（数据不足）
- **7 项体检指标行**（遍历 `report.indicators[]`，按 `code` 映射多语名称，**按 `status` 标红/标黄/置灰，禁止解析 `reference` 字符串**）：

| `code` | 建议中文展示名 | 展示格式 | `status` 渲染 |
|---|---|---|---|
| `stop_reason` | 停止充电原因 | 主文本用 `value`（已本地化）；`reported_value` 仅在需要展示“桩端原话”时以引用样式附注（其语言不保证与当前 App 一致） | `abnormal` 标红、`attention` 标黄、`normal` 正常、`unavailable` 显示「暂无数据」 |
| `battery_temp_delta` | 电池温差 | `{value} / 5°C` | 同上 |
| `temp_rise_rate` | 温升速率 | `{value} / 0.3°C/min` | 同上 |
| `battery_max_temp` | 电池最高温 | `{value} / 45°C`（安全上限 55°C） | 同上 |
| `soc_change_rate` | SOC 变化率 | `{value} / 5%/min` | 同上 |
| `cell_voltage_delta` | 单体蓄电池电压极差 | `{value} / 150mV`（优质 ≤ 60mV） | 同上 |
| `cell_max_voltage` | 单体蓄电池最高压 | 若有 `probe_no` 拼成 `#{probe_no} {value}`，否则 `{value}`；参考值按 `overview.battery_type`：`lfp` → `3.65V`，`ncm` → `4.2V`，未知时不硬写死单一化学阈值 | 同上；`reason_code === "BATTERY_TYPE_UNKNOWN"` 时可展示实测电压并注明“电池类型未知，暂不判阈” |

- **唤起 AI 诊断按钮**：点击跳聊天页并携带当前 `order_no`，不把健康报告当故障根因诊断。

### 2.3 五维雷达图卡片（⚠️ 注意修复现有 `batteryReport.vue` 的 6 维与 `max: 220` 错配）

`report.radar[]` 固定返回 **5 个维度**，分值范围 **`0 ~ 100`**（客户端 `radarOpts.extra.radar.max` 必须改为 `100`，且分类数由原 Demo 的 6 项改为以下 5 项）：

| `code` | 维度名 | 分值 | 缺失态 |
|---|---|---|---|
| `soc_balance` | SOC 均衡性 | `score`（0~100） | `score === null`（`status === "unavailable"`）时在图例/列表标 `--`，雷达绘制可按 0 占位并显式注明“未评估”，不造假分 |
| `temperature_balance` | 温度均衡性 | `score`（0~100） | 同上 |
| `max_temperature` | 最高温度 | `score`（0~100） | 同上 |
| `capacity` | 电池容量 | `score`（0~100） | 浅充（`reason_code === "SHALLOW_CHARGE"`）或无标称容量时为 `null` |
| `voltage_balance` | 电压均衡性 | `score`（0~100） | 同上；`health_metrics.voltage_balance_basis` 说明口径来源（`process` / `settlement_0x19`） |

### 2.4 三张底部时序曲线卡片

`report.curves` 包含 `temperature`、`voltage`、`power` 三个分组，每组返回：

```json
{
  "series": [
    { "name": "实际功率", "field": "power", "unit": "kW", "points": [[1795670400.0, 48.5], ...] }
  ],
  "sample_range": [1795670400.0, 1795674000.0],
  "original_points": 240,
  "output_points": 240
}
```

- `points` 每项为 `[unix_timestamp_seconds, numeric_value]`，后端已做最多 300 点保极值抽样；X 轴按用户本地时区格式化为 `HH:mm`。
- `series[].name` 已随作业语言本地化，可直接用作图例名；功率已统一归一化为 `kW`。
- 当某分组 `series.length === 0` 时展示「本次充电暂无曲线数据」空态，不渲染示例折线。

---

## 3. 错误码与重试策略

| 阶段 / `error.code` | 含义 | 前端建议交互 |
|---|---|---|
| `POST` 返回 `404 ORDER_NOT_FOUND` | 订单不存在或不属于当前登录账号 | 提示「未找到对应充电订单」 |
| `POST` 返回 `503 ORDER_AUTHORIZATION_UNAVAILABLE` | 订单权限核验上游暂不可用（`retryable: true`） | 展示「稍后重试」按钮 |
| 作业 `failed` + `ORDER_NOT_ENDED` | 订单仍在充电中，未结算 | 提示「充电尚未结束，请在充电完成后查看体检报告」 |
| 作业 `failed` + `ORDER_WINDOW_INVALID` / `ORDER_WINDOW_TOO_LARGE` / `DEVICE_MISSING` | 核心订单元数据不完整（不可重试） | 提示「该订单缺少生成报告所需的完整充电记录」 |
| 作业 `failed` + `retryable: true`（`SOURCE_UNAVAILABLE` / `REPORT_TIMEOUT` / `REPORT_INTERRUPTED_BY_RESTART`） | 数据源或服务瞬时中断 | 展示「重新生成」按钮重新 `POST` |

---

## 4. 生产数据源覆盖边界（41 环境 2026-11-26 只读实测）

前端验收与提测时需知晓以下真实数据边界，**出现 `unavailable` 不等于接口故障**：

1. **YKC 协议近 14 天订单**（41 上 `charging-pile_comm` 保留约 14 天，近 7 天 30 笔抽样中 `0x15/17/23` 覆盖 70%、`0x19` 覆盖 77%、`0x25` 覆盖 47%）：可真实驱动全部 7 项指标、SOH 总分、五维雷达与三类曲线；若某单未上报 `0x25` 则 `battery_temp_delta` 降级为 `unavailable`（有 `0x19` 时由结束帧兜底）。
2. **OCPP / 非 YKC 订单或超过 14 天的历史订单**：无 `charging-pile_comm` YKC 帧，仍可正常产出 `stop_reason`、`battery_max_temp`、`temp_rise_rate`、`soc_change_rate` 与功率/电压/温度曲线，其余依赖 BMS 报文的项诚实返回 `status = "unavailable"` + `reason_code`。
3. **浅充订单（`ΔSOC < 30%`）**：按规格不作有效 SOH 判定，`overall_health_score` 与 `radar[capacity].score` 为 `null`，前端必须有清晰的「浅充暂不评估」展示态。
4. **语言切换行为**：作业结果按**创建时**的 `Accept-Language` 冻结存储；切换 App 语言后重新 `POST /v1/health-report-jobs` 会自动新建对应语言的作业（复用键含 `language`），不要复用旧语言的 `job_id`。

---

## 5. 联调自检清单

- [ ] 移除 `batteryReport.vue` 内所有硬编码示例车牌/车型/SOC/指标/6 维雷达/`2018~2023` 曲线
- [ ] `POST /v1/health-report-jobs` 带 `Accept-Language`，轮询至 `completed` 后渲染
- [ ] `overall_health_score === null` 且 `is_shallow_charge === true` 时正确展示浅充说明
- [ ] 7 项指标逐项按 `status` 分色，`unavailable` 展示缺数据态而非 `null`/`NaN`
- [ ] 雷达图切为 5 维且 `max = 100`，`score === null` 不误当 0 分吹高/拉低
- [ ] 非中文语言请求下核对 `conclusion.text`、`stop_reason.value` 与曲线图例均无中文残留
