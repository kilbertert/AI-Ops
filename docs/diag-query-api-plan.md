# 充电业务诊断查询接口方案(需求 + 技术设计)

- 状态:已锁定(D 方案,2026-08-27);TDengine 段暂留直连,待 tsdata 补洞后回收
- 日期:2026-08-25(初稿);2026-08-27(D 方案锁定同步)
- 文档分支:`docs/diag-query-api-plan`
- 范围所有者:AI-Ops(调用方)/ 充电桩 Java 服务(宿主)
- 实现提示:生产 Java 仓库在远端(124.243.178.156),本地 `backend-v2-domestic/` 是 gitignored 只读快照,仅供引用;本文档描述的 Java 侧改动需在团队仓库落地。

---

## 1. 背景与目标

AI-Ops(充电订单只读诊断运行时)目前持有生产库凭据,直连 MySQL `cloud_charging_pile`、TDengine `iot`、Redis 完成充电订单、设备报文、过程时序的排查查询。直查库带来三个问题:凭据散落、无鉴权通道(tsdata)、无审计。

> **方案状态**:本方案最终采用 D 方案(2026-08-27 锁定),TDengine 段暂留直连,Java 侧不动,tsdata 侧不动。

本次目标:把这些查询正式化为**走 Java 平台现有鉴权体系的受控接口**,收回 AI-Ops 的生产库凭据,使数据出口受控、可审计。

### 目标

- **T1** 提供充电订单 / 原始报文 / 过程时序 / 占位费订单 / Redis 同步队列 / 设备信息 6 类固定契约只读查询接口
- **T2** 鉴权**复用 Java 平台现有内部令牌机制**(`X-Internal-Token`,HMAC-SHA256),不新建鉴权体系
- **T3** 关闭 tsdata 无鉴权查询通道(`/tdadmin/*` 补令牌校验)
- **T4** 收回 AI-Ops 的 MySQL / TDengine / Redis 直连凭据(含 SSH 隧道直连配置)
- **T5** 查询行为受控:时间窗必填、行数上限、分页、审计留痕

### 非目标

- 不改动 `iot_device_log`(已有 `/iotDeviceLog/page`,且非诊断主路径)
- 不做写操作、不做通用表透传查询
- 不启用充电桩服务中被注释的 Spring Security OAuth2 资源服务器(除非平台明确要求 OAuth2 化,见 §12)
- 不对第三方/客服系统开放(调用方目前仅 AI-Ops)

---

## 2. 现状盘点

### 2.1 AI-Ops 直查库实现(全部在 `src/aiops_diagnostics/sources.py`)

| 查询函数 | 数据源 | 目标表/键 | 关键行为 |
|---|---|---|---|
| `get_orders(order_no, tenant_id)` | MySQL | `ch_order_info` | `SELECT` 80+ 列,`ORDER BY created_time DESC LIMIT 3`;`SET SESSION MAX_EXECUTION_TIME` + `START TRANSACTION READ ONLY` |
| `get_fee_template_record(order_no, tenant_id)` | MySQL | `ch_fee_template_record` | `fee_template / occupy_fee_template / period_fee_detail`,LIMIT 1 |
| `get_device(device_id/device_code, tenant_id)` | MySQL | `iot_charging_device` | 按 id 或 device_code,LIMIT 1 |
| `get_gun_samples(device, start, end, tx_serial_no)` | TDengine | `charging-gun_property`(超级表) | 时间窗 + 可选 txSerialNo,`LIMIT 2000`(tdengine_max_rows) |
| `get_comm_messages(device, start, end)` | TDengine | `charging-pile_comm`(超级表) | 时间窗,`LIMIT 2000` |
| `inspect_streams(order_no)` | Redis | `third.order.sync.queue` / `third.order.sync.notify.queue` | `XINFO GROUPS` + `XREVRANGE`(有界)+ 按 order_no 匹配计数 |
| `doctor()` | 三库 | 账号只读性断言 | MySQL 断言 `read_only`,防止诊断账号被误授写权 |

连接方式:MySQL 经 pymysql 只读事务 + `MAX_EXECUTION_TIME`;TDengine 经 REST `Basic Auth`(loopback 只读代理 :16041);Redis 经 `redis-py`。生产凭据在 `/home/claude/.config/aiops-diagnostics/production.env`(仓库外)。

> **完整覆盖性说明**:T4(收凭据)要求 **AI-Ops 的全部直查函数**都有接口,因此除需求点名的三个场景外,还必须覆盖 `iot_charging_device`(get_device)与 `ch_fee_template_record`(get_fee_template_record)两个辅助查询。否则凭据无法收干净。

### 2.2 Java 侧已有接口(backend-v2-domestic 快照)

| 端点 | 所属 | 用途 | 鉴权 |
|---|---|---|---|
| `GET /chOrderInfo/page` | 充电桩服务 | 订单分页(管理后台) | 网关 JWT(admin 用户态) |
| `GET /chOccupyOrderInfo/page` | 充电桩服务 | 占位费订单分页(管理后台) | 网关 JWT |
| `GET /iotDeviceLog/page` | 充电桩服务 | 设备日志分页 | 网关 JWT |
| `POST /tdadmin/data/query\|stat\|snapshot\|data` | tsdata(Vert.x) | TDengine 通用表查询/写入 | **无任何鉴权** |

tsdata 的 `/tdadmin/data/query` 是表名透传 + 动态 SQL(`QueryRequest.buildQuerySql`),无 token/签名校验——这是现状里最需要堵的洞。

### 2.3 缺口

1. 设备报文(TDengine)没有受控接口,tsdata 无鉴权
2. 占位费订单无诊断查询(AI-Ops 侧零实现,净新增)
3. 没有面向 M2M 内部调用方的固定契约查询接口(现有 `/page` 面向人工后台,条件与返回均不匹配诊断场景)
4. 无审计与查询保护

---

## 3. 范围

**进**:充电订单(含计费模板快照)、设备信息、Redis 同步队列、占位费订单 4 类只读查询接口;AI-Ops 切流量 + 分阶段收凭据。

**不进(本轮)**:TDengine 段两个查询方法(`/diag/comm-message` 原始报文、`/diag/gun-property` 过程时序)暂不进,等 tsdata 补洞后追加;`iot_device_log`;写操作;通用表透传查询;OAuth2 化;对外部开放。

---

## 4. 总体设计

### 4.1 架构与调用链

```
AI-Ops (sources.py 增加 HttpSources,镜像现有 Protocol)
  │  X-Internal-Token + X-Request-Timestamp (HMAC-SHA256, 300s 窗口)
  ▼
充电桩 Java 服务 (cloud-charging-pile-web, 新增 DiagQueryController)
  ├─ 校验内部令牌(InternalTokenManager.validateToken)→ 401/403 拒绝
  ├─ MySQL   ch_order_info / ch_fee_template_record / ch_occupy_order_info / iot_charging_device
  │          ← MyBatis-Plus 直查(沿现有 MAX_EXECUTION_TIME 只读事务约束)
  ├─ Redis   两个 Stream 有界窗口 ← redis 客户端直查
  └─ TDengine charging-pile_comm / charging-gun_property
       │     经 tsdata(补 X-Internal-Token 校验)
       ▼
  R<T> 统一响应, 审计日志(@SysLog 适配无登录态, §8)
```

> **本轮边界**:上图 TDengine 分支(`charging-pile_comm` / `charging-gun_property` 经 tsdata)是 Phase 3b 目标;本轮 4 个 `/diag/*` 接口只覆盖 MySQL / Redis 查询,TDengine 段继续由 AI-Ops 直连(§3、§10)。

### 4.2 宿主决策

- **新接口宿主:充电桩 Java 服务(cloud-charging-pile-web)**。理由:已持有三张业务表 + `ch_occupy_order_info` 的 MyBatis-Plus 映射、已有调 tsdata 的先例(`IndexV2Controller`)、与内部令牌机制同仓库、形成统一审计点。
- **tsdata 保持 TDengine 后端**,补令牌校验(§5.4),不再被外部直接调用。
- **AI-Ops 直连服务**(绕过网关)。网关 `AuthGlobalFilter` 只处理 admin/tenant-app 的 JWT 请求;AI-Ops 无 JWT,走服务自身校验最贴合现有 `InternalTokenManager` 语义。

### 4.3 设计原则

1. **固定契约**:每个场景一个端点,查询条件白名单化,返回字段固定。不做表透传(避免复制 tsdata 的洞)。
2. **`order_no` 锚定**:诊断入口永远是订单号,所有订单域接口支持按 `order_no` 反查,叠加设备/时间窗/状态过滤。
3. **查询约束内置**:时间窗必填、行数上限、分页(§7)。
4. **全量审计**:每次查询留痕(§8)。

---

## 5. 鉴权设计

### 5.1 复用机制(现状代码,`cloud-common-auth`)

- `InternalTokenManager`(`component/InternalTokenManager.java`):
  - 头部:`X-Internal-Token` + `X-Request-Timestamp`(Unix 秒)
  - 生成:`token = hex( HMAC-SHA256(secret, "{timestamp}:{expireSeconds}") )`
  - 校验:`|now - timestamp| <= expireSeconds`(默认 300s),签名比对
- `GatewayAccessInterceptor` / `GatewayAccessChecker`:拦截非网关直连,放行条件 = 内部令牌校验通过 / `X-From-GW: qushiyun` / localhost;失败返回 403 `{"code":"-1","msg":"非法请求！"}`

### 5.2 AI-Ops 调用流程

1. AI-Ops 持有共享密钥(§5.3)
2. 生成 `X-Internal-Token` + `X-Request-Timestamp` 两个请求头
3. 直连充电桩服务 `/diag/*`(网络路径见 §10 Phase 1)
4. `DiagQueryController` 入口校验令牌(控制器自校验,不依赖拦截器路径配置)
5. 校验通过 → 执行查询 → 返回 `R<T>`

> **控制器自校验**为权威门槛(而非依赖 `GatewayAccessInterceptor` 的 include/exclude 路径配置),因为拦截器默认放行 localhost,且 `/diag/*` 是否被 `GatewayCheckProperties` 覆盖存在不确定性。自校验后,即使路径配置变化,鉴权语义不变。

### 5.3 密钥管理

- secret 从配置读取(`InternalTokenProperties`,前缀 `internal-token.*`),**淘汰默认硬编码值**(现配置默认 `qushiyun-internal-secret-2024`,属安全气味,需在实现时替换并从配置下发)。
- AI-Ops 侧 secret 存 `/home/claude/.config/aiops-diagnostics/` 下配置,**不落仓库**。
- 轮换预案:先发新 key 并存、双 key 校验过渡、再移除旧 key;轮换窗口 5 分钟(令牌 300s 有效期)内新旧均有效。

### 5.4 tsdata 补鉴权

- `/tdadmin/data/query|stat|snapshot|data` 全部要求 `X-Internal-Token`(tsdata 为 Vert.x 服务,自实现同一 HMAC-SHA256 校验,配置独立或共享同一密钥)。
- 推荐与平台共享 `internal-token` 密钥,统一轮换。校验失败返回 401。

---

## 6. 接口定义

**统一约定**:
- 响应包装复用 `com.qushiyun.cloud.common.core.util.R<T>`:`{code, msg, data}`,成功 `code ∈ [0, 200]`
- 鉴权失败:`401 {"code":401,"msg":"令牌无效或过期"}`;校验通过但越权/异常:`4xx/5xx + R`
- 时间格式:ISO 8601(如 `2026-08-25T10:00:00.000+08:00`),向下取毫秒
- 分页:`limit`(本次行数上限,服务端强 cap)+ `offset`(仅订单域需要分页;TDengine/Redis 用时间窗 + limit)

### 6.1 `GET /diag/order` —— 充电订单 + 计费模板快照

对应 `order_snapshot` + `fee_snapshot` 两个诊断工具。

| 参数 | 必填 | 说明 |
|---|---|---|
| `order_no` | 是 | 订单号(主键锚点) |
| `tenant_id` | 否 | 租户过滤;缺省 = 跨租户(受信内部调用,见 §9) |
| `include_fee_template` | 否 | 默认 `true`,附带 `ch_fee_template_record` 快照 |

`data`:
```json
{
  "orders": [ { ...ch_order_info 全字段, 见附录 B.1... } ],
  "fee_template": { "order_no": "...", "tenant_id": "...",
                     "fee_template": {...}, "occupy_fee_template": {...},
                     "period_fee_detail": {...} } | null
}
```

约束:返回按 `created_time DESC` 最多 3 条(镜像现状 `LIMIT 3`)。

### 6.2 `GET /diag/comm-message` —— 原始协议报文

对应 `comm_messages`。

> **暂不进本轮**:TDengine 段暂留直连,本接口等 tsdata 补洞后由 Java 侧追加(§3、§10 Phase 3b)。

| 参数 | 必填 | 说明 |
|---|---|---|
| `device` | 是 | 设备标识(超级表 tag) |
| `start_time` | 是 | 窗口起点(ISO 8601) |
| `end_time` | 是 | 窗口终点(ISO 8601) |
| `direction` | 否 | `1`=平台下行 / `2`=桩上行 |
| `include_raw` | 否 | 默认 `false`;`true` 附带原始帧 `raw` 字段 |
| `limit` | 否 | 行数上限,服务端强 cap(默认 2000) |

`data`:`[ { _ts, direction, code, decoded(, raw) } ]`,按 `_ts ASC`。

约束:时间窗必填,窗口跨度上限默认 24h(可配置,§7);`device` 需通过 `SAFE_VALUE` 同款白名单校验。

### 6.3 `GET /diag/gun-property` —— 充电过程时序

对应 `gun_timeseries`。

> **暂不进本轮**:TDengine 段暂留直连,本接口等 tsdata 补洞后由 Java 侧追加(§3、§10 Phase 3b)。

| 参数 | 必填 | 说明 |
|---|---|---|
| `device` | 是 | 设备标识 |
| `start_time` / `end_time` | 是 | 窗口 |
| `tx_serial_no` | 否 | 会话号过滤(超级表列) |
| `fields` | 否 | 逗号分隔字段白名单,缺省返回默认集 |
| `limit` | 否 | 行数上限,服务端强 cap(默认 2000) |

`data`:`[ { _ts, txSerialNo, status, isReturn, isInsert, outputVoltage, outputCurrent, power, chargingTime, chargingElectricityQuantity, soc, temperature, batteryMaxTemperature, batteryMinTemperature, errorCode, errorReason, meterNow } ]`(镜像现状 SELECT,附录 B.3)。

### 6.4 `GET /diag/occupy-order` —— 占位费订单(净新增)

| 参数 | 必填 | 说明 |
|---|---|---|
| `order_no` | 二选一 | 充电订单号反查 |
| `order_id` | 二选一 | 充电订单主键反查 |
| `tenant_id` | 否 | 租户过滤 |
| `status` | 否 | 占位费订单状态过滤 |

`data`:`[ { id, orderId, order_no, device_id, device_code, child_device_id, child_device_code, site_id, userId, free_time, timeout, occupy_amount, pay_amount, status, out_trade_no, is_pay, is_sync_mall_order, pay_time, tenant_id, startTime, endTime, operator_id, refund_status, refund_amount, refund_time, refundRemark } ]`(附录 B.4)。

约束:最多 20 条,按 `startTime DESC`;`order_no` 为空时需提供 `order_id`,二者必居其一。

> 关联键语义(已确认):`orderId` = 充电订单主键(`ChOrderInfo.id`,雪花 ID),`orderNo` = 充电订单业务编号(`ChOrderInfo.orderNo`),二者指向**同一**充电订单。因此 `order_no` 参数查 `WHERE order_no=?`,`order_id` 参数查 `WHERE orderId=?`。证据:`OccupyOrderTxDataHandler.java:109-110`(`setOrderId(chOrderInfo.getId())` / `setOrderNo(chOrderInfo.getOrderNo())`)。

### 6.5 `GET /diag/redis-stream` —— 同步队列检查

对应 `redis_sync`。

| 参数 | 必填 | 说明 |
|---|---|---|
| `stream` | 否 | 白名单内 Stream;缺省 = 两个都查 |
| `order_no` | 否 | 消息内容匹配计数 |
| `max_messages` | 否 | `XREVRANGE` 条数,服务端强 cap(默认 1000) |

`data`:`[ { stream, type, length, groups: [{ name, consumers, pending, lag }], inspected_messages, matches } ]`

约束:`stream` 仅允许 `third.order.sync.queue` / `third.order.sync.notify.queue`。

### 6.6 `GET /diag/device` —— 设备信息

对应 `device_snapshot`。

| 参数 | 必填 | 说明 |
|---|---|---|
| `device_id` | 二选一 | 设备主键 |
| `device_code` | 二选一 | 设备编码 |
| `tenant_id` | 否 | 租户过滤 |

`data`:`{ id, tenant_id, site_id, device_code, protocol, online_status, status, work_status, error_reason, fee_template_id } | null`(镜像现状 SELECT)。

---

## 7. 查询约束与性能保护

| 层 | 约束 |
|---|---|
| MySQL | 沿现状:只读事务 `START TRANSACTION READ ONLY` + `MAX_EXECUTION_TIME`;订单返回 `LIMIT 3`,占位费 `LIMIT 20` |
| TDengine | 时间窗**必填**;窗口跨度默认上限 24h(配置项,可按诊断场景放宽);行数强 cap(默认 2000);`device`/`tx_serial_no` 走白名单字符校验,防注入 |
| Redis | Stream 白名单固定;`XREVRANGE` 有界;`max_messages` 强 cap(默认 1000) |
| 全接口 | 服务端查询超时(沿用 `query_timeout_seconds` 语义);单 IP/调用方速率上限可选启用 |

保护上限统一收在**服务端配置**,请求可请求更小值但不可突破服务端上限。

---

## 8. 审计设计

- 每次 `/diag/*` 调用记录:调用方标识(`internal:AIOps`)、时间、端点、参数摘要(`order_no`/`device`/时间窗/`limit`)、返回行数、耗时、结果(成功/失败)。
- 实现:控制器 AOP 切面记录(现有 `@SysLog` 面向登录用户操作日志,无登录态时其"操作人"字段取不到,需适配为取鉴权调用方标识);**不改动**现有人工后台的审计。
- 审计日志含订单号/设备标识等查询参数;响应体不落日志。若平台对 `user_id`/`card_id` 有合规红线,仅对日志字段打码(数据返回按 §12 不脱敏)。

---

## 9. 多租户与身份边界

- `tenant_id` 为可选过滤参数:提供则限定租户;缺省 = 跨租户查询。
- 信任边界:跨租户能力**仅**对持有有效内部令牌的调用方开放;`/diag/*` 不得暴露公网(仅生产内网/隧道);AI-Ops 是唯一受信调用方。
- 现状 AI-Ops 即跨租户直查(诊断任意订单),接口保留此能力;若后续收紧,可在令牌上绑定租户范围(预留,不本次实现)。

---

## 10. 收口 / 迁移计划

本节按 D 方案改写为分阶段收凭据:**2/3 凭据 → 1/3 凭据 → 0/3 凭据**。凭据删除动作由后续收口任务执行,本 PR 只同步方案文档。

### Phase 1 —— 接口实现与自测(充电桩服务 + tsdata)

- 充电桩服务:`DiagQueryController` + 令牌校验 + 审计切面;tsdata 补令牌校验
- 网络路径:AI-Ops → 生产内网充电桩服务 API(经现有 SSH 隧道或 Tailscale 内网入口,端口不暴露公网)
- 验收:§11.1 / §11.2 通过

### Phase 2 —— AI-Ops 切流量

- `sources.py` 新增 `HttpSources`(实现同一 `DiagnosticSources` Protocol,镜像 6 个查询函数签名,内部改为调 `/diag/*`)
- 用三份 fixture(`examples/fixtures/`:ykc_amount_mismatch / ocpp_consistent / missing_tx_data)对**直连源 vs HTTP 源**回放比对,输出一致
- 验收:§11.3 通过

### Phase 3a(本 PR 后删 MySQL+Redis 凭据)

- 删除 `production.env` 中 MySQL / Redis 凭据及对应直连配置;TDengine 凭据与 SSH 隧道直连配置暂时保留。
- `sources.py` 移除 MySQL / Redis 直连代码路径(或经开关彻底禁用),TDengine 直连路径继续保留。
- `doctor()` 分类断言:HTTP 段与 TDengine 直连段可用,MySQL / Redis 已退役或未配置。
- 验收:Phase 3a 完成后持有 **1/3 凭据**(仅 TDengine);`aiops-gateway` 功能回归不受影响。

### Phase 3b(待 tsdata 补洞后删 TDengine 凭据)

- 前置条件:tsdata 真正补完令牌校验,且充电桩 Java 服务实装 `/diag/comm-message` `/diag/gun-property`。
- 删除 `production.env` 中 TDengine 凭据及 SSH 隧道直连配置;`sources.py` 移除 TDengine 直连代码路径。
- `doctor()` 断言:三库不再可直连、无直连凭据残留。
- 验收:Phase 3b 完成后持有 **0/3 凭据**;`aiops-gateway` 功能回归不受影响。

> **回滚预案**:Phase 3a 保留删除前 MySQL / Redis 凭据备份(仓库外);Phase 3b 保留 TDengine 凭据备份,异常时逐段回滚。

---

## 11. 验收标准

### 11.1 功能(Feature)

```gherkin
Feature: 诊断查询接口
  Rule: 固定契约只读查询
    Scenario: 按订单号查充电订单
      Given 存在已完成的充电订单 O,订单号 ON,租户 T
      When 调用 GET /diag/order?order_no=ON&tenant_id=T(带有效内部令牌)
      Then 返回 HTTP 200 且 R.code 成功
      And data.orders 恰含订单 O 的全字段
      And data.fee_template 包含计费模板快照
    Scenario: 报文与时序按设备+时间窗查询
      Given 设备 D 在窗口 [S,E] 内有报文与时序样本
      When 调用 GET /diag/comm-message?device=D&start_time=S&end_time=E
      Then 返回按 _ts 升序的报文列表,且不含超窗数据
    Scenario: 占位费订单按订单号反查
      Given 充电订单 O 关联占位费订单 P
      When 调用 GET /diag/occupy-order?order_no=ON
      Then data 包含订单 P
    Scenario: Redis 队列检查
      When 调用 GET /diag/redis-stream
      Then data 含两个白名单 Stream 的长度与消费组滞后
```

### 11.2 安全

```gherkin
  Rule: 鉴权
    Scenario: 无令牌拒绝
      When 不带 X-Internal-Token 调用任一 /diag/* 
      Then 返回 401
    Scenario: 伪造/过期令牌拒绝
      When 携带错误签名或超 300s 窗口的令牌调用
      Then 返回 401
    Scenario: tsdata 无令牌拒绝
      When 直接调用 /tdadmin/data/query 不带 X-Internal-Token
      Then 返回 401
    Scenario: 非法设备标识拒绝
      When device 参数含 SQL 元字符(如 -- 或引号)
      Then 返回 4xx 且不执行查询
```

### 11.3 回归

- 三份 fixture 在直连源与 HTTP 源下输出逐字段一致(orders / fee_template / comm / gun / streams / device)
- `aiops-gateway` 全流程(自然语言 → 诊断工具 → 新接口)冒烟通过

### 11.4 收口

- **Phase 3a**:`production.env` 无 MySQL / Redis 凭据;`doctor()` 断言 HTTP 段与 TDengine 直连段可用,MySQL / Redis 已退役或未配置。
- **Phase 3b**:`production.env` 无三库凭据;`doctor()` 直连断言失败即"已收口"。
- 审计日志中出现新增接口调用记录(调用方、参数、结果)

---

## 12. 风险与未决问题

| # | 项 | 影响 | 处置 |
|---|---|---|---|
| 1 | tsdata 密钥与 Spring 配置的一致性 | 若独立密钥,轮换需双处管理 | 推荐共享 `internal-token` 密钥 |
| 2 | `@SysLog` 无登录态适配 | 审计"操作人"缺省 | 审计切面取鉴权调用方标识 `internal:AIOps` |
| 3 | 充电服务 `GatewayCheckProperties` 与 `/diag/*` 路径关系 | 拦截器行为不确定性 | 控制器自校验令牌为权威门槛,不依赖拦截器 |
| 4 | 占位费订单关联键确认 | 已确认:`orderId`=充电订单主键(`ChOrderInfo.id`),`orderNo`=充电订单业务编号(`ChOrderInfo.orderNo`),指向同一充电订单 | 已解决(`OccupyOrderTxDataHandler.java:109-110`) |
| 5 | 生产 Java 仓库在远端 | 本地无法直接实现/验证 Java 改动 | 本文档为交付物;实现走团队仓库 |
| 6 | 默认 secret `qushiyun-internal-secret-2024` | 若已被泄露,内部令牌形同虚设 | 上线即轮换,从配置下发新 secret |
| 7 | Spring Security OAuth2 资源服务器被注释 | 若平台后续强推 OAuth2,方案需调整 | 本次不启用;留作演进选项,不阻塞 |
| 8 | TDengine 段仍直连 | 当前生产环境 AI-Ops 仍持有 TDengine 凭据,直到 Phase 3b 完成;tsdata 真正补洞之前 TDengine 段仍直连 | Phase 3a 只删 MySQL+Redis 凭据;Phase 3b 完成后才删 TDengine 凭据 |
| 9 | D 方案不解决 tsdata 自身访问控制 | D 方案不解决 tsdata 自身被任意人访问的问题,tsdata 的无鉴权访问通道仍未关闭 | tsdata 保持内网隔离与现有运维限制;补洞后作为 Phase 3b 前置条件 |

---

## 附录 A:代码事实索引

| 事实 | 位置 |
|---|---|
| AI-Ops 直查实现(6 函数 + doctor) | `AI-Ops/src/aiops_diagnostics/sources.py` |
| 诊断工具与契约枚举 | `AI-Ops/src/aiops_diagnostics/diagnostic_tools.py`、`agent_contracts.py` |
| 直连凭据(仓库外) | `/home/claude/.config/aiops-diagnostics/production.env` |
| 内部令牌算法 | `backend-v2-domestic/cloud-common/cloud-common-auth/.../component/InternalTokenManager.java` |
| 网关访问拦截 | `.../auth/interceptor/GatewayAccessInterceptor.java`、`component/GatewayAccessChecker.java` |
| tsdata 无鉴权路由 | `backend-v2-domestic/tsdata/.../verticle/WebVerticle.java` |
| 充电桩服务启动类(OAuth2 被注释) | `backend-v2-domestic/cloud-charging-pile-web/.../ChargingPileApplication.java` |
| 现有后台查询控制器 | `ChOrderInfoController.java`、`ChOccupyOrderInfoController.java`、`IotDeviceLogController.java` |
| 统一响应包装 | `cloud-common-core/.../util/R.java` |
| 查询 SOP(字段/表 DDL 注释) | `AI-Ops/充电桩问题排查SOP.md`、`AI-Ops/SOP.md` |
| 诊断 fixture | `AI-Ops/examples/fixtures/`(ykc_amount_mismatch、ocpp_consistent、missing_tx_data) |

## 附录 B:关键字段参考

### B.1 `ch_order_info`(充电订单)

现状 SELECT 列见 `sources.py:ORDER_COLUMNS`(80+ 列):id, order_no, tenant_id, status, type, billing_type, launch_type, is_test, device_id, device_code, child_device_id, child_device_code, site_id, electricity, electricity_fee, service_fee, ds_*(电损), tip/peak/flat/valley 四段分时电量/电费/服务费, fee_template_id, launch_fee, park_fee, **occupy_fee**, appointment_fee, insurance_amount, market_amount, platform_amount, pay_amount, total_amount, reduce_balance, is_pay, is_receive_tx_data, sync_mall_order, out_trade_no, stopped_reason_code, stopped_reason_content, error_time, error_info, last_report_amount, transaction_id, meter_start, meter_end, white_flag, balance_insufficient_stop, start_soc, end_soc, device_protocol, created_time, stop_time, draw_gun_time, **tx_data(JSON)**。

状态枚举(`CommonConstant`):0=充电中、1=结束、2=不可控异常、3=异常已处理、5=设备已上报但异常结束。

### B.2 `ch_fee_template_record`(计费模板快照)

`order_no, tenant_id, fee_template, occupy_fee_template, period_fee_detail`。

### B.3 TDengine 超级表

- `charging-pile_comm`(tag `device`):`_ts, direction(1=平台下行/2=桩上行), code, decoded, raw`
- `charging-gun_property`(tag `device`):`_ts, txSerialNo, status, sourceStatus, isReturn, isInsert, outputVoltage, outputCurrent, power, chargingTime, remainingTime, chargingElectricityQuantity, soc, temperature, batteryMax/MinTemperature, errorCode, errorReason, meterNow`

### B.4 `ch_occupy_order_info`(占位费订单)

`id, orderId, order_no, device_id, device_code, child_device_id, child_device_code, site_id, userId, free_time, timeout, occupy_amount, pay_amount, status, out_trade_no, is_pay, is_sync_mall_order, pay_time, tenant_id, startTime, endTime, operator_id, refund_status, refund_amount, refund_time, refundRemark`。

### B.5 `iot_charging_device`(设备)

`id, tenant_id, site_id, device_code, protocol, online_status, status, work_status, error_reason, fee_template_id`。
