# ADR-0350: 电池健康报告 `health-v3` 指标口径、确定性结论与 YKC 协议帧接入

Status: Accepted
Date: 2026-11-26
Issue: #602

## Context

Issue #602 跟踪 #578 运营横幅落地后的报告页后端欠账：既有 `POST/GET /v1/health-report-jobs`
在 `health-v2` 阶段只输出 `stop_reason` 单一指标，其余 6 项体检指标未接入 `indicators[]`，
`health_metrics.py` 存在浅充阈值错配（`ΔSOC < 10%` 而非规格要求的 `< 30%`）、跨全程温差高估
（`max(T_max) - min(T_min)` 而非同帧 `max(T_max - T_min)`）及五维评分缺失末端分段等偏差。

同时，2026-11-26 对 41 生产环境的只读数据源盘点发现两项关键事实：

1. `charging-gun_property` 在 41 上**不存在 `batteryMinTemperature` 列**，导致
   `TDengineSource.get_gun_samples()` 在 41 上 100% 报 `Invalid column name`，健康报告
   作业一直静默降级为无遥测。
2. `charging-pile_comm` 在 41 上持续落库原始 YKC 1.6 协议帧（`0x15` 握手、`0x17` 参数配置含标称总能量
   `E_rated`、`0x19` 充电结束帧含结算单体最高/最低压、`0x23` 充电需求帧含单体最高压低 12 位、
   `0x25` BMS 信息帧含实时最高/最低温与探针号/SOC 告警），近 7 天 30 笔已完成订单中 70%–77%
   有 `0x15/17/19/23`、47% 有 `0x25`；而 MySQL `ch_car_spec` 当前 0 行、`ch_my_car.battery_capacity`
   全空。

项目负责人已确认 #602 的三项业务决策：总健康分暂定直接取有效 SOH（选项 A）；体检单结论采用
确定性分档多语模板；规则版本由工程升至 `health-v3` 并纳入版本治理；本阶段交付 AI-Ops 服务端、
契约与前端联调文档。

## Decision

1. **规则版本升为 `health-v3`**：沿用 `health_metrics.RULE_VERSION` 作为作业行与报告体唯一来源；
   凭借既有 `(scope, order_no, rule_version, language)` 作业复用键自然隔离 `health-v2` 缓存。
2. **修复 `charging-gun_property` 列清单并接入只读 `charging-pile_comm` 协议帧**：
   - 从 `GUN_COLUMN_NAMES` 移除 41 不存在的 `batteryMinTemperature`（与 `cpms_evidence.TIMELINE_FIELDS`
     一致），同步 `tdengine_proxy.py` 白名单；
   - 在 `TDengineSource` 与 `tdengine_proxy.py` 增加有界只读的 `get_pile_comm_frames`
     （受 `DeviceGate`、时间窗与行数上限约束，仅允许读取 `15/17/19/23/25` 五类帧）；
   - 新增纯函数解码器 `src/aiops_diagnostics/health/ykc_frames.py`，按枪号（`child_device_code`
     尾号 ↔ 帧字节 29）与 `txSerialNo` 过滤同桩双枪并发帧，并过滤温度 `-50℃` 与电压 `0x0000` 哨兵。
   - 协议表不存在或不可用时静默降级（`source_summary.protocol = "unavailable"`），不影响非 YKC/OCPP 订单。
3. **完整实现 7 项 `indicators[]`、五维评分、`ΔSOC < 30%` 浅充判定与 Option A 总健康分**：
   - `indicators[]` 固定输出 `stop_reason`、`battery_temp_delta`、`temp_rise_rate`、
     `battery_max_temp`、`soc_change_rate`、`cell_voltage_delta`、`cell_max_voltage`；
     缺数据的项返回 `status = "unavailable"` 与明确 `reason_code`。
   - `temp_rise_rate` 采用 5 分钟滑窗、`soc_change_rate` 采用 1 分钟滑窗加 15 秒跳变检测，
     避免 1℃ / 1% 整数采样量化产生假异常。
   - `ΔSOC < 30%` 标记 `soh_status = "shallow_charge"`、`is_shallow_charge = true`、`soh = null`、
     `overall_health_score = null`（不作有效 SOH 判定），参考值另存 `soh_shallow_estimate`。
   - `radar[voltage_balance]` 优先使用过程压差（需电芯串数 `N`），缺 `N` 时回退 `0x19` 结算压差并在
     `health_metrics.voltage_balance_basis` 显式声明依据。
   - 曲线功率由 TDengine 的 W 归一化为 kW（对齐规格 `V × I / 1000`），并补充实时温差与单体/BMS 电压序列。
4. **新增结构化确定性多语结论 `report.conclusion` 并同步 `report.summary`**：
   - 依据 7 项指标状态确定性归为 `normal` / `attention` / `abnormal` / `insufficient` 四档，
     按触发维度拼接固定建议（11 语齐全，非中文报告零汉字残留）。
   - 不新开 HTTP 端点、不破坏既有作业信封字段，所有新增字段（`overview`、`overall_health_score`、
     `overall_health_source`、`conclusion`）均为向后兼容的增量字段。

## Alternatives considered

### 1. 五维雷达分加权聚合作为总健康分

- **最强论点**：即使订单是浅充或缺少标称总能量 `E_rated`，仍可通过温度/电压/SOC 等可用维度算出一个
  非空总分，界面较少出现「暂不可评估」。
- **否决原因**：业务规格中不存在五维权重公式；在缺维度时动态重加权会让同一辆车因不同桩上报字段不同而
  分数大幅跳变，且与原型弹窗「综合估算的电池健康度（SOH）」自相矛盾。负责人已拍板暂定选项 A（直接取有效 SOH）。

### 2. 仅依赖 `charging-gun_property`，不解析 `charging-pile_comm`

- **最强论点**：无需新增 TDengine 协议帧查询和 YKC 二进制解析逻辑，代码量更小、跨协议一致。
- **否决原因**：41 实测证明 `charging-gun_property` 缺 `batteryMinTemperature`、`0x15` 电池类型、
  `0x17` 标称容量与 `0x19` 结算极差，且 `maxCellVoltage` 字段存在平台解析异常值（如 `38.61`）。
  放弃 `charging-pile_comm` 将导致 41 现网上温差、SOH、单体压差、单体最高压 4 项核心指标永久 `unavailable`。

### 3. 调用大模型生成体检单结论文案

- **最强论点**：措辞更自然灵活，能模仿原型中的个性化总结口吻。
- **否决原因**：违背 `/v1/health-report-jobs` 的确定性契约（[standard-api-contract.md](../standard-api-contract.md) §4.3）、
  引入延迟与模型波动，并增加 11 语一致性与幻觉风险；确定性模板在性能、可测性与多语可控性上均明显更优。

## Consequences

- **收益**：
  - 41 上 YKC 充电订单首次具备真实数据驱动的 7 项体检指标、5 维评分、SOH 总分与校准后 kW 曲线；
  - OCPP/无协议帧订单也能恢复 `charging-gun_property` 遥测读取并诚实标注缺失项，不再静默全空；
  - 前端可直接依据统一 `health-v3` 契约接线，无需在客户端重算阈值或解析中文文本。
- **代价与约束**：
  - 41 若后续启用独立部署的 `tdengine_proxy`，需同步升级代理以放行新的 `GUN_COLUMNS_SQL` 与 `COMM_QUERY`；
  - `charging-pile_comm` 实测保留期约 14 天，超出保留期的历史订单将降级为仅遥测/订单口径；
  - 过程压差公式 `2 × (V_cell_max - V_total / N)` 在 `ch_car_spec` 补齐电芯串数 `N` 前仍由 `0x19` 结算压差兜底；
  - 本次仅交付 AI-Ops 服务端、契约与前端交接文档，`qumall-app` 报告页改造与端到端页面验收仍需客户端仓库配合完成。
