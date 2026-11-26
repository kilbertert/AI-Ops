# 41 环境电池健康报告（`health-v3`）公网端到端验收回执

- **日期（UTC）**：2026-11-26
- **目标环境**：41（公网入口 `https://api.mall.qushiyun.com`）
- **部署版本**：`1.41.0+688e751e93f6`（功能 PR [#1748](https://github.com/kilbertert/AI-Ops/pull/1748) `7f22f799`，发版批次 PR [#1752](https://github.com/kilbertert/AI-Ops/pull/1752) `688e751e93f6`，标签 `v1.41.0`）
- **CD 记录**：首次触发 run `45787025335` 因 41 冷启动瞬时耗时约 7s（超过 `deploy-41.sh` 固定 `sleep 5` 自检窗）触发自动回滚，已保留失败事实并按 [ADR-0348](../adr/0348-release-on-main-sha-dispatch.md) 以同 SHA 重跑 run [`45787299054`](https://github.com/kilbertert/AI-Ops/actions/runs/45787299054) 成功完成部署与 Release 发布。
- **部署后健康核验**：`GET http://172.18.0.1:8788/health` → `{"ok":true,"service":"aiops-gateway","version":"1.41.0+688e751e93f6","api_version":"v1","platform":"linux","business_mutations":"disabled"}`
- **关联任务与契约**：Issue #602、[ADR-0353](../adr/0353-battery-health-report-v3-metrics-and-ykc-frames.md)、[电池检查指标规格](../specs/battery-inspection-metrics.md)、[标准 API 契约 §4](../standard-api-contract.md)、[前端联调手册](../agents/frontend-battery-report-handoff.md)。
- **验收主体（加密资产标识，不含明文凭据）**：
  - `aiops-ulink-phone-20261030`（Ulink 租户 `1942105476598861824`）
  - `aiops-youlianchong-phone-20261030`（优链充租户 `1941847786056323072`）

---

## 1. 公网 HTTPS 端到端验收（真实穿过 `https://api.mall.qushiyun.com`）

所有请求按 [前端联调手册](../agents/frontend-battery-report-handoff.md) 约定的 C 端请求头发送（`third-session` + `X-Business-Entry: consumer` + `Accept-Language`，不发 `Authorization`）：

| 编号 | 场景与脱敏样本 | 观察结果 | 结论 |
|---|---|---|---|
| T1 | `GET /v1/openapi.json` | HTTP `200`（`61.2 ms`），27 条公开路径，包含 `/v1/health-report-jobs` 与 `health-v3` 契约说明 | 通过 |
| T2 | 匿名 `POST /v1/health-report-jobs`（无 `third-session`） | HTTP `401`（`23.1 ms`），`error.code = INVALID_ACCESS_TOKEN` | 通过 |
| T3 | 跨租户 `POST`（Ulink 会话请求优链充订单 `sha256:d3875efb73`） | HTTP `404`（`202.9 ms`），`error.code = ORDER_NOT_FOUND` | 通过 |
| T3b | 跨租户 `GET`（优链充会话查询 Ulink 作业 `hrj_2890ffbe...`） | HTTP `404`（`35.5 ms`），`error.code = REPORT_JOB_NOT_FOUND` | 通过 |
| T4 | 本人近 14 天订单 `zh`（`sha256:e0a381c78d`，`OCPP1.6-J`，`ΔSOC = 40%`） | `POST 202`（`183.9 ms`）→ 2 次轮询 `GET 200 completed`（`1068.8 ms`）；外层与 `report.rule_version` 均为 `health-v3`；固定 7 项指标与 5 维雷达齐全；`power` 曲线归一化为 `kW`（`0.0 ~ 72.0 kW`）；`summary == conclusion.text`；缺失 BMS 协议帧的项准确返回 `unavailable` + `reason_code` | 通过 |
| T5 | 同单同语复提（`sha256:e0a381c78d` + `zh`） | `POST 202`（`177.2 ms`），返回同一 `job_id`，`status = completed` 且直接内联完整 `report` | 通过 |
| T6 | 同单跨语（`sha256:e0a381c78d` + `Accept-Language: en`） | `POST 202` 生成独立新 `job_id` → 2 次轮询 `GET 200 completed`（`1080.4 ms`）；`summary`、`conclusion.text`、`indicators[].value` 与全部曲线 `series[].name`（`Actual power` / `Output current` / `Output voltage` / `Battery temperature`）均为 **0 汉字残留** | 通过 |
| T7 | 本人浅充订单 `zh`（`sha256:20a31d0ec9`，`OCPP1.6-J`，`ΔSOC = 10% < 30%`） | `POST 202`（`209.3 ms`）→ `GET 200 completed`（`1083.1 ms`）；`overview.is_shallow_charge = true`、`soh_status = shallow_charge`、`overall_health_score = null`、`radar[capacity].score = null`（`reason_code = SHALLOW_CHARGE`）、`conclusion.advice_codes` 包含 `shallow_charge` | 通过 |
| T8 | 历史订单降级（优链充会话 + 超过 14 天 `YKC1.6` 订单 `sha256:d3875efb73`） | `POST 202` → `GET 200 completed`；无 500，缺失时序/协议帧项规范返回 `unavailable` + 明确 `reason_code`（`TELEMETRY_DATA_MISSING` / `CELL_VOLTAGE_DATA_MISSING` 等），`curves` 返回空 `series` | 通过 |

---

## 2. 已部署版本（`1.41.0+688e751e93f6`）对 41 实网 YKC1.6 订单只读回放核验

因上述两授权 C 端测试账号近 14 天内本人订单为 `OCPP1.6-J` 或超过 14 天历史单，另以 41 上**已部署运行源码 `/opt/aiops-41/src`（`1.41.0+688e751e93f6`）**对 41 现网最近 8 笔 `YKC1.6` 真实充电单（`electricity >= 5 kWh`，零写入、零明文 PII）执行完整 `TDengineSource`（含拆分 `COMM_SUMMARY_CODES` / `COMM_STREAM_CODES` 查询）+ `parse_ykc_comm_frames` + `enrich_report` + `build_curves` 只读回放：

| 脱敏订单摘要 | 遥测/汇总帧/流帧 | `completeness` | `overall_health_score`（SOH） | `is_shallow` / `ΔSOC` | 电池类型 | 5 维雷达可用数 | 最大功率（kW） | `en` 汉字残留 |
|---|---|---|---|---|---|---|---|---|
| `64e542a030` | 198 / 3 / 361 | `1.0`（7/7） | `91.1` | `false` / `77%` | `lfp` | 5/5（`82.0 / 92.8 / 94.8 / 8.5 / 100.0`） | `44.582` | `0` |
| `d512f517c8` | 251 / 4 / 407 | `1.0`（7/7） | `100.0` | `false` / `79%` | `lfp` | 5/5（`88.0 / 100.0 / 100.0 / 0.0 / 100.0`） | `31.166` | `0` |
| `22c0b204da` | 92 / 3 / 173 | `1.0`（7/7） | `null`（`shallow_charge`） | `true` / `25%` | `ncm` | 4/5（`capacity = null`） | `37.732` | `0` |
| `fcadae93f8` | 190 / 3 / 162 | `0.857`（6/7） | `29.1`（见下边界说明） | `false` / `55%` | `null` | 4/5 | `37.953` | `0` |
| `68d99224e7` | 50 / 3 / 40 | `0.857`（6/7） | `31.8`（见下边界说明） | `false` / `43%` | `null` | 4/5 | `84.863` | `0` |
| `65e9f51bf1` | 275 / 2 / 242 | `0.571`（4/7） | `55.8` | `false` / `82%` | `null` | 3/5 | `25.658` | `0` |
| `03d71d5abd` / `ad166c5bda` | 545/328 枪行，0 协议帧 | `0.143`（1/7） | `null` | `false` / `null` | `null` | 0/5（安全降级） | `7.12` / `7.03` | `0` |

**回放观察到的实网数据特征**：
1. 拆分 `COMM_SUMMARY_CODES`（`15/17/19`）与 `COMM_STREAM_CODES`（`23/25`）后，长会话可稳定取回全部 `0x15/17/19` 汇总帧与数百条 `0x23/25` 过程帧，驱动完整 7/7 指标与 5/5 雷达分值。
2. 41 上部分车辆/桩端 `0x17` 报文上报的标称总能量存在偏大或异常值（如样本 `fcadae93f8`、`68d99224e7` 测得 `SOH ≈ 29%~32%`），后续若需收敛该类异常车端报文口径，应结合电池/车端工程师核定规则另立批次。

---

## 3. 未验收与明确边界（不越界宣称）

1. **客户端 UI 未验收**：`qumall-app` 中 `batteryReport.vue` 接线、5 维雷达图 `max=100` 修正、浅充态/缺数据态渲染及实机 App/H5 页面交互尚未由前端完成与验收（Issue #602 保持打开跟踪）。
2. **业务/物理准确率未签收**：本回执证明的是公网 HTTPS 契约、鉴权隔离、异步轮询、现场 `zh`/`en` 双语输出（其余 9 语由自动化单测 `tests/health/test_health_report_language.py` 覆盖，未在 41 现场逐语实测）、YKC 协议帧解析与实网数据可计算性；**不构成**电池工程师对 SOH 数值、五维评分及阈值业务准确率的签收。
3. **过程电压极差与「单体平均电压偏差」曲线暂缓**：依赖车型电芯串联数 `N`，41 现网 `ch_car_spec` 与 `ch_my_car.battery_capacity` 暂无有效数据，本版 `cell_voltage_delta` 仅由 `0x19` 结算帧提供，曲线暂不下发平均电压偏差序列。
4. **C 端公网 YKC1.6 实单直跑受限**：当前两授权 C 端测试账号近 14 天内无本人 `YKC1.6` 实单，公网 HTTPS 链路已由两账号的近 14 天 `OCPP1.6-J`（深充/浅充）及历史 `YKC1.6` 订单实测通过，YKC1.6 全 7 项计算路径由同部署版本 `1.41.0+688e751e93f6` 41 实库只读回放补充核验。
