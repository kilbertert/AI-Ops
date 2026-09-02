# AI-Ops 前端接口简报（小程序 / 报告页）

> **状态**：草稿 v1（2026-09-01），用于禅道交付前端组。
> **范围**：PRD #23 验收通过后、下一阶段（前端页面）所需接口的封装形态。
> **基线版本**：后端 `main` @ `0f24491`，AI-Ops Gateway `v1`。

---

## 0. 阅读须知

- 现有后端能力面：`aiops_diagnostics` Python 包 + `aiops-gateway` HTTP 服务（FastAPI）。
- 现有鉴权：Gateway 端 `Bearer aops_*`（设备 token，enroll 颁发），不经网关的直连诊断调用 `X-Internal-Token`（HMAC-SHA256，PRD #23 旧 D 方案，但 `/diag/*` Java 侧未合并，目前仅作为设计参考）。
- 现有**受限直连** 6 类查询（MySQL/TDengine/Redis）经 `DiagnosticSources` Protocol 统一输出，前端**不直接调用**，只调 Gateway 包装的 `v1/*`。
- 前端**不持有生产凭据**；所有数据库凭据在 aiops-gateway 进程内，按 PRD #23 的"调用者身份来自已验证凭证、不被前端裸传 user_id 覆盖"原则执行。

---

## 1. 前端页面 → 接口映射

| 前端页面 | 业务诉求 | 后端能力 | 状态 |
|---|---|---|---|
| **小程序 · 智能客服对话页（图1）** | 智能问答、待办报告推送、推荐快捷问、快捷功能（一键拔枪/占位费申诉）、语音转写入口、会话历史 | `POST /v1/runs`（已有）+ 待办报告查询（新增）+ 推荐问列表（新增）+ 语音转写不在后端范围 | **部分已有** |
| **小程序 · 历史会话列表** | 列出当前设备/用户的历史会话 | `GET /v1/runs`（已有） | 已有 |
| **小程序 · 详情页** | 一次 run 的完整过程 | `GET /v1/runs/{id}` + `GET /v1/runs/{id}/events`（已有） | 已有 |
| **报告页 · 充电体检单（图2）** | 7 项检测值 + 5 维雷达 + 4 类一致性文案 + 3 组时序曲线 | **新指标计算层**（本次新增） | **新增** |

---

## 2. 接口总览（按页面分组）

### 2.1 小程序对话页

#### 2.1.1 创建一次智能问答会话

```
POST /v1/runs
Authorization: Bearer aops_<device_token>
Content-Type: application/json

{
  "problem": "我的车充电时突然跳枪了，订单号 2094370061724549120",
  "order_no": "2094370061724549120",
  "tenant_id": "2039889358588481536",       // 可选
  "key_slot": "openai",                     // 可选，后端默认
  "provider": "openai",                     // 可选
  "fixture_name": null                      // 联调可空
}
```

**返回** `202 Accepted`：

```json
{
  "run_id": "run_01J...",
  "status": "queued",
  "incident_id": "inc_..."
}
```

> **调用注意**：
> - 鉴权用设备 token（enroll 后保存在客户端），**不**用用户登录态。
> - `tenant_id` 仅在用户主动指定时传入；后端按 `device.tenant_id` 兜底。
> - 一次 `runs` 视为"一次会话"；对话流式增量由 `events/stream` 拉。

#### 2.1.2 拉取一次会话的实时事件流

```
GET /v1/runs/{run_id}/events/stream
Authorization: Bearer aops_<device_token>
```

**SSE 事件**（参考 `gateway_api.py:_sse`）：

```
event: gateway_run_queued
data: {"sequence": 1, "at": "...", "type": "gateway_run_queued", "run_id": "..."}

event: gateway_worker_started
data: {"sequence": 2, ...}

event: tool_progress
data: {"sequence": 3, "type": "tool_progress", "turn_number": 1, "tools": ["order_snapshot", ...]}

event: gateway_run_completed
data: {"sequence": 99, "type": "gateway_run_completed", "status": "diagnosed"}
```

**前端订阅模式**：
- 订阅后按 `sequence` 顺序追加到本地消息流。
- 收到 `gateway_run_completed` / `gateway_run_interrupted` / `gateway_run_failed` 表示本次问答结束。
- 不需要轮询；SSE 推送已含心跳。

#### 2.1.3 查看一次会话的结果快照

```
GET /v1/runs/{run_id}
Authorization: Bearer aops_<device_token>
```

**返回**：

```json
{
  "run_id": "...",
  "status": "diagnosed",                // queued | running | diagnosed | inconclusive | blocked | interrupted | failed
  "confidence": "high",
  "summary": "充电过程中设备 ... 触发了跳枪",
  "result": { ... },                    // 完整 AgentDiagnosis JSON
  "error_type": null,
  "error_message": null,
  "created_at": "...",
  "completed_at": "..."
}
```

> **`result` 字段**对应 PRD #23 第 25/26 条要求，已脱敏（VIN/手机号/车牌/密码/账号均打码），可直传前端展示。

#### 2.1.4 拉取历史会话

```
GET /v1/runs?limit=20
Authorization: Bearer aops_<device_token>
```

**返回**：

```json
{
  "runs": [
    {
      "run_id": "...",
      "incident_id": "...",
      "problem": "REDACTED",   // 注意：problem 在落库时已脱敏
      "order_no": "2094370061724549120",
      "tenant_id": "2039889358588481536",
      "status": "diagnosed",
      "summary": "...",
      "created_at": "..."
    }
  ]
}
```

#### 2.1.5 拉取证据列表（用于"诊断证据"折叠区）

```
GET /v1/runs/{run_id}/evidence
Authorization: Bearer aops_<device_token>
```

**返回**：

```json
{
  "evidence": [
    {
      "evidence_id": "ev_...",
      "tool": "order_snapshot",
      "source": "mysql:ch_order_info",
      "status": "success",
      "request": { "order_no": "...", "tenant_id": "..." },
      "row_count": 1,
      "error": null
    }
  ]
}
```

> **不返回业务 payload**（避免敏感数据扩散）；业务 payload 通过 `GET /v1/runs/{run_id}` 的 `result` 拿到。

#### 2.1.6 待办报告推送（**新增**）

前端首页需要展示"你有一份待生成智能电池检查报告"卡片。来源：充电订单主键 + 关联车辆。

```
GET /v1/reports/pending
Authorization: Bearer aops_<device_token>
```

**返回**：

```json
{
  "pending": [
    {
      "report_id": "rpt_pending_2094370061724549120",
      "order_no": "2094370061724549120",
      "tenant_id": "2039889358588481536",
      "report_type": "charging_health",      // 预留
      "subject": {
        "order_no": "2094370061724549120",
        "device_code": "12005504505906",
        "start_time": "2026-08-30T10:00:00+08:00",
        "stop_time":  "2026-08-30T11:35:00+08:00",
        "site_id":    "2044314251351162881"
      },
      "ready_at": "2026-08-30T11:36:00+08:00",
      "deep_link": "/v1/reports/charging-health?order_no=2094370061724549120"
    }
  ]
}
```

**实现说明**（后端）：
- 数据源：`MySQL ch_order_info` 状态 `status=1` 且 `stop_time` 在过去 24h 内 + `is_receive_tx_data=1` 的订单 → 视为"数据就绪、未生成报告"。
- 缓存：单次请求 ≤ 200ms；同一设备 60s 内去重。
- 不持有生产凭据；走与 `POST /v1/runs` 同一 `DiagnosticSources`。

#### 2.1.7 推荐快捷问列表（**新增**）

图 1 区块 4 展示「推荐我距离近的空闲场地 / 充电桩故障问题」。来源是平台静态推荐 + 设备用户最近订单上下文。

```
GET /v1/recommendations?category=quick_question&limit=3
Authorization: Bearer aops_<device_token>
```

**返回**：

```json
{
  "recommendations": [
    { "id": "rec_ykc_amount_mismatch", "title": "充电金额与预期不符", "intent": "fee_amount_mismatch", "order_required": true },
    { "id": "rec_unable_to_unplug",     "title": "无法拔枪",            "intent": "gun_unable_to_unplug", "order_required": true },
    { "id": "rec_occupy_fee_appeal",    "title": "占位费申诉",          "intent": "occupy_fee_appeal", "order_required": true }
  ]
}
```

> **前端行为**：点击后调用 `POST /v1/runs` 时把 `problem` 设为该问题的描述（或后端在 `result` 中预填业务字段），不再做客户端拼文案。

#### 2.1.8 快捷功能（**新增**）

图 1 底部"无法拔枪 / 占位费申诉 / 快捷键可"三个按钮。

- **「无法拔枪」/「占位费申诉」**：复用 `POST /v1/runs`，`problem` 模板来自 `recommendations` 列表。
- **「快捷键」**：前端本地定义；不属于后端接口范围。

#### 2.1.9 语音输入（**不在后端范围**）

- 微信小程序 `wx.getRecorderManager()` 直接调腾讯 ASR 或自建 ASR，**与 AI-Ops 后端解耦**。
- 转写后文本作为 `POST /v1/runs` 的 `problem` 传入。

#### 2.1.10 鉴权前置：设备注册（enroll）

```
POST /v1/enroll
Content-Type: application/json

{ "code": "enr_...", "device_name": "用户A-微信小程序", "platform": "wechat_mp" }
```

**返回**：

```json
{
  "device_id": "dev_01J...",
  "workspace_id": "ws_...",
  "tenant_id": "2039889358588481536",
  "token": "aops_..."        // 前端保存到本地存储；后续每次请求都带
}
```

> **enrollment code** 由后端运营/开发线下/接口发放（不在前端组对接范围）。

---

### 2.2 报告页 · 充电体检单

#### 2.2.1 完整体检单（**新增**）

```
GET /v1/reports/charging-health?order_no=2094370061724549120
Authorization: Bearer aops_<device_token>
```

**返回**：

```json
{
  "report_id": "rpt_2094370061724549120",
  "order_no": "2094370061724549120",
  "tenant_id": "2039889358588481536",
  "subject": {
    "device_code": "12005504505906",
    "start_time": "2026-08-30T10:00:00+08:00",
    "stop_time":  "2026-08-30T11:35:00+08:00",
    "site_id":    "2044314251351162881"
  },
  "summary": "模拟充电体检大体正常，热管理表现一般，大电流充电温度会更容易抬升。建议尽量避免长时间大功率快充，充电时保证车辆通风散热条件。",
  "indicators": [
    { "name": "停止充电原因",   "value": "手动停止",  "reference": "--",                  "status": "ok"     },
    { "name": "温差",           "value": 2,          "reference": "5",                    "status": "ok",     "unit": "°C" },
    { "name": "温升速率",       "value": 0.6,        "reference": 0.3,                   "status": "warn",   "unit": "°C/min" },
    { "name": "电池最高温",     "value": 31,         "reference": 55,                    "status": "ok",     "unit": "°C" },
    { "name": "SOC变化率",      "value": 1,          "reference": 5,                     "status": "ok",     "unit": "%/min" },
    { "name": "单体蓄电池电压极差", "value": 170,    "reference": 150,                   "status": "warn",   "unit": "mV" },
    { "name": "单体蓄电池最高压", "value": "电池串号#31", "reference": "4.2",              "status": "ok" }
  ],
  "radar": [
    { "axis": "SOC均衡性",     "score": 82.0 },
    { "axis": "温度均衡性",    "score": 82.0 },
    { "axis": "最高温度",      "score": 82.0 },
    { "axis": "电池容量",      "score": 82.0 },
    { "axis": "电压均衡性",    "score": 82.0 }
  ],
  "consistency": [
    { "key": "voltage",  "title": "电芯一致性",
      "text": "这是您爱车充电过程中，各单体电芯之间的电压偏差情况。单体压差越小、电芯一致性越好，越不易发生虚充或提前跳枪，分数越高。" },
    { "key": "temperature", "title": "温度一致性",
      "text": "..." },
    { "key": "max_temperature", "title": "最高温度",
      "text": "..." },
    { "key": "soc", "title": "SOC一致性",
      "text": "..." }
  ],
  "curves": {
    "power": {
      "unit_x": "min", "unit_y": "kW",
      "series": [
        { "name": "需求功率", "points": [[0, 12.0], [10, 24.5], ..., [90, 30.0]] },
        { "name": "实际功率", "points": [[0, 12.5], [10, 23.0], ..., [90, 30.0]] }
      ]
    },
    "voltage": {
      "unit_x": "min", "unit_y": "V",
      "series": [
        { "name": "需求电压", "points": [...] },
        { "name": "实际电压", "points": [...] }
      ]
    },
    "temperature": {
      "unit_x": "min", "unit_y": "°C",
      "series": [
        { "name": "电池温度", "points": [...] }
      ]
    }
  },
  "battery_health": {
    "soh": 92.3,
    "title": "电池健康",
    "explanation": "这是根据您爱车充入电量、SOC变化及能效模型综合估算的电池健康度（SOH）。电池可用容量衰减越少、越接近出厂标称状态，电池性能越好，分数越高。"
  },
  "evidence_run_id": "run_01J...",
  "generated_at": "2026-08-30T11:36:00+08:00"
}
```

**实现说明**（后端）：
- 输入：`order_no` + device + start/stop 窗口（来自 `ch_order_info`）。
- 数据源：复用 PRD #23 的 `DiagnosticSources`（TDengine `charging-gun_property` 时序 + `ch_order_info` 计费/电量 + 可选 `iot_charging_device`）。
- 指标计算层 = 新增 `report_health.py`（不在 `agent_runner` 主路径中，作为只读派生层）。
- 输出 = 静态 JSON；不调用 Codex，无 AI 推理。
- 鉴权：同 `POST /v1/runs`，按 device.tenant_id 兜底；订单租户与 device 租户不一致 → 403。
- 限额：每 device 每 5 分钟 1 次，缓存 5 分钟。

#### 2.2.2 单独获取某条曲线

```
GET /v1/reports/charging-health/curve?order_no=...&curve=power|voltage|temperature
Authorization: Bearer aops_<device_token>
```

> 前端一般不需要单独调；为图表懒加载预留。

---

## 3. 通用约定

### 3.1 响应包装

**成功**（2xx）：

```json
{ "...": "<业务对象>" }     // 直接是业务数据，不额外包一层
```

**失败**（4xx/5xx）：

```json
{
  "detail": "human-readable message"   // FastAPI HTTPException 的 detail
}
```

**网关/服务拒绝**：

| 场景 | 状态码 | detail 前缀 |
|---|---|---|
| 鉴权失败（token 缺失/失效） | 401 | `device token required` / `invalid or revoked device token` |
| 跨租户/越权 | 403 | `requested tenant does not match ...` |
| 参数不合法 | 400 | `requested key slot is not allowed by the gateway` / `gateway fixture execution is disabled` |
| 资源不存在 | 404 | `run not found` |
| 限流 | 429 | `rate limited`（新增） |
| 内部错误 | 500 | `<exception_type>: <message>`（已脱敏） |

### 3.2 时间与时区

- 所有时间字段：ISO 8601 + `+08:00`（北京时间），向下取毫秒。
- 服务端时区：固定 `Asia/Shanghai`。
- 服务端会做 DST 检查（无夏令时）。

### 3.3 脱敏与敏感字段

- 接口响应**不**直接返回：手机号、VIN、车牌、长账号（>12 位）、密码、token、`raw` 报文帧、bcrypt 哈希、`tx_data` 中包含的 PII。
- 订单主键（`order_no` / `transaction_id` / `device_code` / `txSerialNo`）保留——是诊断锚点。
- 实现：经 `redaction.sanitize_data` 统一处理；前端**不要**自行做正则打码，避免漏报。

### 3.4 限流

| 接口 | 限流 | 备注 |
|---|---|---|
| `POST /v1/enroll` | 10 / 小时 / IP | 防爆破 |
| `POST /v1/runs` | 30 / 分钟 / device | 防过载 |
| `GET /v1/reports/charging-health` | 12 / 小时 / device | 计算成本高 |
| `GET /v1/reports/pending` | 60 / 分钟 / device | 轻量 |
| 其余 GET | 600 / 分钟 / device | 通用 |

### 3.5 多租户与身份边界

- `device.tenant_id` 是**基线租户**，前端**不**能跨租户查询。
- `payload.tenant_id` 仅在前端确认"代查"时传入；后端用 `device.tenant_id` 兜底，**不再信任前端** user_id。
- 订单属于非 `device.tenant_id` → 403；不返回任何行。

---

## 4. 与 PRD #23 现有能力的对应关系

| 本次接口 | 复用 PRD #23 哪一条 |
|---|---|
| `POST /v1/runs`（已有） | 故事 1, 3, 5, 6, 7, 25 / 决策"复用现有 DiagnosticSources 适配器" |
| `GET /v1/runs/{id}/events/stream` | 故事 25, 27（事件审计） |
| `GET /v1/runs/{id}/evidence` | 故事 7, 25（证据溯源） |
| `GET /v1/reports/charging-health` | 故事 17, 18（充电遥测 + 报文） + **新增派生层** |
| `GET /v1/reports/pending` | 故事 32（"缺失源显式报 unsupported"）→ 改为"待办源显式列出" |

---

## 5. 端到端样例（小程序 → 报告）

```
1. 小程序启动，enroll → 拿到 aops_*
2. 用户点击"待办报告"卡片
3. 前端 GET /v1/reports/pending → 拿到 [{ order_no, deep_link }]
4. 跳转到报告页，GET /v1/reports/charging-health?order_no=... → 拿到体检单 JSON
5. 渲染报告页：summary / indicators / radar / curves
```

完整用时：2 次 HTTP 调用，端到端 ≤ 1.5s（缓存命中 ≤ 300ms）。

---

## 6. 风险与未决

| # | 项 | 影响 | 处置 |
|---|---|---|---|
| 1 | 体检单指标层是新代码，PRD #23 的"fail closed"语义需继承 | 指标计算失败 = 整个报告 503 | 按 `SourceError` 统一包，前端用 `detail` 展示 |
| 2 | 微信小程序域名白名单 | aiops-gateway 当前 `127.0.0.1:8787` 不可被小程序直连 | 部署侧提供 HTTPS 域名 + 转发到 gateway；不在本简报范围 |
| 3 | 报告缓存策略与 PRD #23 审计要求冲突 | 缓存命中时 evidence_run_id 可能过期 | 缓存保留 `evidence_run_id` 与原 run_id，前端展示时跳转 `/v1/runs/{id}` |
| 4 | 体检单指标层与后端 `report_health.py` 实现位置未定 | 是网关服务内的派生层还是单独 worker？ | 建议放在 `aiops_diagnostics/report_health.py`，由 `gateway_api.py` 直接调用，参考 `run_agent_diagnosis` 模式 |

---

## 7. 待办（给前端组）

1. 确认 2.1.6 / 2.1.7 / 2.2.1 三类接口的命名与字段是否符合预期。
2. 确认 2.1.2 SSE 订阅是否需要客户端断开重连 / 心跳。
3. 报告页三组曲线建议用 ECharts / uCharts / 微信原生 canvas —— 自由选择；后端只输出 `points: [[t, v], ...]`。
4. 微信小程序 `wx.request` 不支持 SSE 长期连接，建议在前端封装：`xhr.onreadystatechange` 拼接 `text/event-stream` 分块；或后端新增 `GET /v1/runs/{id}/events?after=` 轮询接口兜底。

---

## 附录 A：现有 `/v1/*` 接口索引

| 端点 | 已有/新增 | 鉴权 | 备注 |
|---|---|---|---|
| `GET /health` | 已有 | 无 | 健康检查 |
| `POST /v1/enroll` | 已有 | 无 | 设备注册 |
| `POST /v1/runs` | 已有 | Bearer | 创建诊断会话 |
| `GET /v1/runs` | 已有 | Bearer | 历史会话 |
| `GET /v1/runs/{id}` | 已有 | Bearer | 会话结果 |
| `GET /v1/runs/{id}/events` | 已有 | Bearer | 事件拉取（轮询） |
| `GET /v1/runs/{id}/events/stream` | 已有 | Bearer | 事件推送（SSE） |
| `GET /v1/runs/{id}/evidence` | 已有 | Bearer | 证据元数据 |
| `GET /v1/reports/pending` | **新增** | Bearer | 待办报告列表 |
| `GET /v1/recommendations` | **新增** | Bearer | 推荐快捷问 |
| `GET /v1/reports/charging-health` | **新增** | Bearer | 充电体检单 |
| `GET /v1/reports/charging-health/curve` | **新增** | Bearer | 单条曲线懒加载 |

---

## 附录 B：增量 v2（基于《电池充电检测协议解析与健康评估规范》与车型库）

> **状态**：草稿 v2（2026-09-01），基于补充材料 `/home/claude/Projects/AI-Ops/docs/智能客服相关文档及数据/` 增量更新。
> **触发条件**：v1 简报被确认后，运营/产品要求"前端的报告页必须**严格按规范**计算，而不是按 mock 数据"；同时要求接入**车型库 + 品牌车标库**用于报告头部展示。

### B.1 规范条文 → 现状映射

| 规范条文 | 涉及字段 | 现状 | 缺口 / 处置 |
|---|---|---|---|
| §1.1 `0x13 实时监测帧`（SOC/电压/电流/度数/时间） | 序号 7/8/11/13/15 | `get_gun_samples` 已返回 `outputVoltage / outputCurrent / soc / power / chargingTime`；度数 = `chargingElectricityQuantity` | ✅ 复用 |
| §1.2 `0x23 充电需求帧`（BMS总电压/单体最高压/组号） | 序号 7/9 | `get_gun_samples` **不返回 BMS 总电压与单体最高压** | **扩展 SELECT**：增加 `bmsVoltage`、`cellMaxVoltage`、`cellMaxVoltageGroup` |
| §1.3 `0x25 BMS信息帧`（最高单体电压编号/最高最低温/探针号） | 序号 4/5/6/7/8 | `get_gun_samples` 已返回 `temperature / batteryMaxTemperature / batteryMinTemperature`，但**没有探针号** | **扩展 SELECT**：增加 `probeMaxNo / probeMinNo` |
| §1.4 `0x15 握手帧`（电池类型 03H/06H / VIN） | 序号 5/16 | VIN 在 `ch_order_info.tx_data` 中（已有）；电池类型可由 0x15 解析 | **新增** `get_parsed_comm_messages` 解析 `code → frame_type → typed_value` |
| §1.5 `0x17 参数配置帧`（标称总能量/单体最高允许电压） | 序号 4/6 | 不在当前查询面 | **优先 VIN → 车型库** 兜底；车型库查不到时返回 `null`，报告标 "无法计算 SOH" |
| §1.6 `0x19 充电结束帧`（单体最低/最高电压/温） | 序号 5/6/7/8 | 不可从 TDengine gun 表回查（结束值）；用 `charging-gun_property` 时间窗末帧代替 | **约定**：`get_gun_samples` 末帧视为"充电结束快照" |
| §1.7 `0x3B 交易记录帧`（起止时间/停止原因） | 序号 4/5/30 | 起止时间用 `ch_order_info.created_time/stop_time`；停止原因 = `ch_order_info.stopped_reason_content`（已被业务侧映射为可读文本） | ✅ 复用 |
| §2 SOH 公式 | E_actual = ΔE/ΔSOC × η，η=0.94；SOH = E_actual/E_nominal × 100% | 没有 | **新增派生层** `report_health.compute_soh()` |
| §2.3 浅充判定 | ΔSOC < 10% → 标记"浅充估算值" | 没有 | 同上派生层内置 |
| §3 7 项检测值 | 停止原因/温差/温升速率/电池最高温/SOC变化率/单体电压极差/单体最高压 | 缺 `BMS总电压` 与 `单体最高压` | 同 B.1 第 2/3 行 |
| §4.1 SOC 均衡性评分 | 100 − 15×(15s 内跳变≥3%次数) − 30×(0x25 序号 10 告警次数) | 缺告警次数解析 | **新增** 0x25 序号 10（异常告警）解析 |
| §4.2 温度均衡性评分 | ΔT_max ≤ 2℃→100；2~5℃→100−6×(ΔT_max−2)；>5℃→82−10×(ΔT_max−5)，下限 0 | 没有 | **新增派生层** |
| §4.3 最高温度评分 | T_max ≤ 35→100；35~45→100−1.8×(T_max−35)；45~55→82−5×(T_max−45)；>55→截断 0 | 没有 | **新增派生层** |
| §4.4 电池容量评分 | SOH≥95→100；80~95→80+(SOH−80)/15×20；70~80→60+(SOH−70)/10×20；<70→max(0, SOH/70×60) | 没有 | **新增派生层** |
| §4.5 电压均衡性评分 | ΔV_cell ≤ 30 mV→100；30~80→100−0.36×(ΔV_cell−30)；>80→82−0.45×(ΔV_cell−80)，下限 0 | 没有 | **新增派生层** |
| §5 曲线抽取 | 温度/电压/功率三类 | gun_samples 缺 BMS 总电压与单体最高压 | **扩展 SELECT** + 在派生层做对齐 |

### B.2 新增 / 调整清单（按优先级）

#### B.2.1 数据资产（**离线导入，不连生产**）

| 资产 | 路径 / 表 | 行数 | 用途 |
|---|---|---|---|
| 车型库 | `data/assets/car_models.sqlite` 表 `car_model` | 6787（`autohome_cleaned_data_2026-08-26-2.csv`） | 按车型名/VIN 片段匹配 → 标称总能量、电池类型、CLTC/WLTC 续航 |
| 品牌车标 | 同库表 `car_brand` | 415（`新能源汽车全量品牌及车标数据.csv`） | 报告头部展示品牌车标 |
| 协议代码表 | 同库表 `bms_code` | ~50（手动维护） | 0x3B 序号 30 停止原因 / 0x15 序号 5 电池类型 / 0x25 序号 10 告警 文本化 |
| 评分公式 | `data/assets/scoring_rules.json` | 1 文件 | 雷达分分段表，与规范 §4 一一对应，**实现不要硬编码** |

> **资产加载**：`python -m aiops_diagnostics.assets_loader`（一次性），生成 SQLite 资产文件；运行期只读。
> **资产文件不入仓库**（与 production.env 一致），但 `assets_loader` 命令 + schema DDL 入仓。
> **车型库字段不全是空的**（autohome CSV 中 `电池容量(kWh)` 空行较多），匹配失败 → 返回 `null`，报告对应分项标 "无车型数据"。

#### B.2.2 后端查询扩展（**只读、不动 PRD #23 鉴权链**）

| 现有函数 | 扩展 | 优先级 |
|---|---|---|
| `get_gun_samples` | SELECT 列追加：`bmsVoltage`（BMS 总电压 0x23 序号 7）、`cellMaxVoltage`（0x23 序号 9 低 12 位）、`cellMaxVoltageGroup`（0x23 序号 9 高 4 位）、`probeMaxNo`（0x25 序号 4）、`probeMinNo`（0x25 序号 6） | P0 |
| `get_comm_messages` | 不直接动 SELECT；上层派生层用 `decoded` 字段反序列化 0x3B/0x15/0x25 报文 → 解析为 `bms_code` 表里的语义值 | P0 |
| `get_orders` | 不动；`tx_data` 中已经有 `vin` 字段（如果订单上有） | — |
| `HttpSources` | 同步扩展 `/diag/gun-property` 契约（B.3） | P1 |

> **可发现性**：扩展后由 `scope_blocked()` + `allowed_devices` 双重约束保持；不允许前端绕过 `device.tenant_id` 透传 SQL。

#### B.2.3 新增派生层（**新文件 `aiops_diagnostics/report_health.py`**）

入口（仅 1 个公共函数）：

```python
def build_health_report(
    sources: DiagnosticSources,
    order_no: str,
    *,
    safety: SafetySettings,
    asset_db: Path,  # 车型库 SQLite 资产
) -> HealthReport: ...
```

- 内部流程：
  1. `get_orders` → 取 `order_no`、租户、`device_code`、`tx_data.vin`、`created_time/stop_time`、计费电量
  2. `get_gun_samples` → 取**全段**时序（device+窗口，不限 tx_serial_no）
  3. `get_comm_messages` → 取 0x25 BMS 帧（direction=2 / code=0x25 过滤）
  4. 派生：
     - ΔT_max = max(batteryMaxTemperature) − min(batteryMinTemperature)
     - SOC 跳变次数 = 15s 滑窗内 soc 差 ≥ 3% 计数
     - BMS 告警次数 = comm_messages 中 0x25 序号 10 出现的次数
     - ΔV_cell = 结束帧 cellMaxVoltage − cellMinVoltage（若 0x19 不可用，用 gun_samples 末帧推算）
     - E_charged = gun_samples.chargingElectricityQuantity 末值 − 首值（kWh）
     - ΔSOC = soc.末 − soc.首
     - E_actual = E_charged / ΔSOC × 0.94
     - E_nominal = 车型库 [vin 末 8 位 OR 车型名] 查表 → 电池容量
     - SOH = E_actual / E_nominal × 100；若 ΔSOC < 10% → `shallow_charge: true`
     - 5 维雷达分 = `scoring_rules.json` + 上述输入
- 错误语义：
  - VIN/车型查不到 → `battery_capacity = null` → SOH 标 `null`；雷达 S_cap = `null`；前端按 "—"
  - 0x19 不可用 → ΔV_cell 退化用 gun_samples 末帧
  - SOC 跳变无法计算（< 5 个采样点）→ S_SOC = `null`
- 输出 dataclass 直接序列化为 §2.2.1 的 JSON（与简报 v1 字段完全兼容，新增字段在末尾 `consistency` 之后补 `battery_health_origin`）。

#### B.2.4 新增 HTTP 端点（**对应 §2.2.1**，扩展 v1）

无新增端点；**只扩展现有 `GET /v1/reports/charging-health` 的返回字段**：

| 字段（新增） | 类型 | 说明 |
|---|---|---|
| `subject.vin` | string \| null | `ch_order_info.tx_data.vin`（**保留** — 是诊断锚点） |
| `subject.vehicle` | object \| null | `{ brand, brand_logo_url, model_name, model_image_url, battery_type, nominal_capacity_kwh }`，全部来自车型库；查不到 = null |
| `indicators[*].reference` | 改用规范 §3 的"参考标准值"列（`≤5 ℃` / `≤0.3 ℃/min` / `≤45 ℃` / `≤5 %/min` / `≤150 mV` / `≤4.2 V`），前端用 `value > 阈值` 自渲染 warn |
| `indicators[停止充电原因].value` | 改用 0x3B 序号 30 映射文本（`bms_code` 表） |
| `radar[*].score` | double \| null | 严格按 §4 公式；缺数据 = null |
| `radar[*].breakdown` | object | 调试用（不展示给最终用户）：`{ rule_id, inputs, computed }` |
| `battery_health.soh` | double \| null | 严格按 §2.1+§2.2 |
| `battery_health.shallow_charge` | bool | §2.3 浅充标记 |
| `battery_health.energy_charged_kwh` | double \| null | E_charged（中间值） |
| `battery_health.soc_delta` | double \| null | ΔSOC（中间值） |
| `battery_health.nominal_capacity_kwh` | double \| null | E_nominal（来自车型库） |
| `battery_health.efficiency_factor` | double | 固定 `0.94` |
| `consistency` 4 项 | 改用规范 §4 各小节"诊断依据说明"作为正文（与图2 一致） |
| `curves.temperature.series` | 改用 §5.1 定义：增加 `电池实时温差` 系列 |
| `curves.voltage.series` | 改用 §5.2 定义：增加 `BMS充电总电压` + `单体动力蓄电池最高压` + `单体平均电压偏差` |
| `curves.power.series` | 改用 §5.3 定义：保留 `需求功率` / `实际功率` / `输出电流` |
| `scoring_rule_version` | string | 评分公式版本号（`data/assets/scoring_rules.json` 的 `version` 字段），便于回溯 |

### B.3 接口契约调整

> **后端 HTTP 契约（不破坏 v1）**：`GET /v1/reports/charging-health` 保持原 v1 路径与鉴权，仅扩展返回字段。请求参数 `order_no` 不变。

> **Java 侧 `/diag/gun-property`（D 方案）**：若 Phase 3b 启用，**必须**同步增加 B.2.2 的 5 个新字段；不增加 → 我方派生层拿不到 BMS 电压，雷达分 S_ΔV 强制 null。

> **enroll 阶段**：`POST /v1/enroll` 不变；车型库加载是后端运维操作，与前端无关。

### B.4 验收（gherkin 草稿，落到禅道）

```gherkin
Feature: 充电体检单 V2（规范对齐）

  Rule: 5 维雷达分严格按规范 §4
    Scenario: 全指标齐全
      Given 订单 O 有完整 0x13/0x23/0x25 时序、车型库命中
      When 调用 GET /v1/reports/charging-health?order_no=O
      Then radar[*].score 全部为 double 且 ∈ [0, 100]
      And radar[*].breakdown.inputs 命中规范 §4 公式输入字段
      And battery_health.soh 满足 SOH = E_actual/E_nominal × 100，E_actual = E_charged/ΔSOC × 0.94

  Rule: 浅充判定
    Scenario: ΔSOC < 10%
      Given 订单 O ΔSOC = 0.05
      When 调用报告接口
      Then battery_health.shallow_charge = true
      And summary 中包含 "浅充估算值"

  Rule: 车型库兜底
    Scenario: VIN 查不到车型
      Given 订单 O 的 vin 在车型库中无匹配
      When 调用报告接口
      Then subject.vehicle = null
      And battery_health.nominal_capacity_kwh = null
      And radar[电池容量].score = null

  Rule: 扩展字段无破坏
    Scenario: v1 客户端兼容
      Given 前端按 v1 简报字段渲染
      When 调用 V2 接口
      Then summary / indicators / radar / curves / consistency 全部存在
      And 新增字段（vin/vehicle/scoring_rule_version）不影响 v1 渲染
```

### B.5 风险与未决（v2 新增）

| # | 项 | 影响 | 处置 |
|---|---|---|---|
| 1 | 车型库匹配规则 | VIN 17 位 vs 车型名匹配成功率与"误匹配"风险 | 匹配顺序：`vin` 末 8 位完全匹配 → 车型名完全匹配 → 车型名前缀匹配；前缀匹配须人审。**不**做模糊匹配 |
| 2 | 0x25 序号 10 告警定义缺失 | 规范未列告警码表 | 后端用 0x25 序号 10 非零即"告警一次"；告警码表由业务侧补，**不是**本次范围 |
| 3 | ΔV_cell 在 0x19 不可用时退化用 gun_samples 末帧 | 末帧可能在 BMS 报 0x19 之后 | 后端实现：若 gun_samples 末帧 `_ts` 距 `stop_time` > 30s，强制 ΔV_cell = null |
| 4 | 评分公式版本管理 | 业务调整后旧报告如何溯源 | `scoring_rule_version` 字段写进报告 JSON（只增，不改）；后端不删除旧版公式 |
| 5 | 报告缓存与规范变更 | 缓存命中可能用旧公式 | 缓存 key 加 `scoring_rule_version`；版本变更 → 自动失效 |
| 6 | `tx_data.vin` 在 `ch_order_info` 是否稳定 | 取决于上游充电桩服务是否每次都写 | 后端读不到时 `subject.vin = null`；SOH 走"无车型数据"路径 |
| 7 | 扩展 `get_gun_samples` 是否影响 PRD #23 既有测试 | TDengine SELECT 列增加 → 列数/行大小变化 | 既有 `test_sources_*` 断言按列名而非位置；扩展后回退测试一次 |

### B.6 改动文件清单（v2 范围）

| 文件 | 状态 |
|---|---|
| `src/aiops_diagnostics/sources.py` | 改：`get_gun_samples` SELECT 追加 5 列；保持 `allowed_devices` 约束 |
| `src/aiops_diagnostics/report_health.py` | 新增：派生层主文件（compute_soh / 5 维评分 / 曲线抽取） |
| `src/aiops_diagnostics/assets_loader.py` | 新增：CSV → SQLite 资产导入 CLI |
| `src/aiops_diagnostics/gateway_api.py` | 改：`GET /v1/reports/charging-health` 调用 `report_health.build_health_report`；扩展返回字段 |
| `data/assets/scoring_rules.json` | 新增：评分公式版本化定义 |
| `data/assets/car_models.sqlite` | 新增：运行期生成，不入仓 |
| `docs/assets-loader.md` | 新增：导入步骤 |
| `tests/test_report_health.py` | 新增：派生层单元测试 |
| `tests/test_sources_gun_columns.py` | 新增：扩展列查询测试 |
| `tests/test_charging_health_endpoint.py` | 新增：HTTP 端点 + v1 兼容测试 |
| `tests/fixtures/health_report_*.json` | 新增：规范 §4 各分段的固定样例 |
| `docs/agents/frontend-api-brief.md` | 改：本文档（本附录 B） |

> **B.6 改动**全部在 Python 端完成，**不动 Java 仓库**（与 PRD #23 决策一致）。Java 侧 `/diag/gun-property` 若启用，**必须**同步加 B.2.2 的 5 列；不启用 → 现有 aiops_ro 最小只读直连 + TDengine 受控代理足够覆盖派生层需求。
