# 捷佳真实客户环境（`jiejia`）电池健康报告（`health-v3`）公网端到端复测回执

- **日期（UTC）**：2026-11-27
- **目标环境与入口**：`jiejia`（受管逻辑主机 `jiejia-gw`，租户 `1990990768688599040`），客户公网入口 `https://api.juxichongdian.cn`
- **已合入变更与部署版本**：
  - 通用架构改造 PR [#1797](https://github.com/kilbertert/AI-Ops/pull/1797)（`6fa97919`，[ADR-0360](../adr/0360-health-report-tsdata-fallback-and-user-ingress.md)）
  - 发版批次 PR [#1798](https://github.com/kilbertert/AI-Ops/pull/1798)（`3ad573eca3eb2d5b6af00bc0436cae9e0db84e11`，版本号 `1.43.19`）
  - 构建并经 `dev-host cp --artifact-sha256` 校验投递的两份合并后不可变产物：
    - 应用离线条目包：`artifact_sha256 = 495e0ce1a38ec3e227d1bd102c9ecdeaaa843a7d2ab8ec0c9b199cf332d759cf`（`1.43.19+3ad573eca3eb`）
    - 用户 Nginx 入口包：`sha256 = 4c33c6618ea963a55badcb3de17e937705f5c838eb2910a2d3e2e6ceec8a4bb3`
  - **升级与零漂移说明**：采用与 V2.1 `wizard_worker` Step 3 相同的离线 wheel 原位升级链（停止服务、备份 `venv` / `installed.json` / 既有 Nginx 扩展、离线 `--no-index` 安装并只更新 `revision`/`version`/`artifact_sha256`，保留既有 `production.env`、`registry/environment.json`、`runtime/`、`gateway.db` 与 `41` 运行面零改动），启动后 `http://127.0.0.1:8788/health` 核验返回 `{"ok":true,"service":"aiops-gateway","version":"1.43.19+3ad573eca3eb","api_version":"v1","platform":"linux","business_mutations":"disabled"}`，`nginx -t && nginx -s reload` 通过，已清除 `/tmp` 临时包与暂存目录。
- **平台通用性说明（非捷佳特判）**：
  1. `health_job_runner` 仅在直连 `TDengineSource` 不可用（`SourceError`）时按运行时 `AIOPS_GATEWAY_OPERATOR_TSDATA_URL`（`/tdadmin/data/query`）只读回退，支持缺列单次回退与 `charging-pile_comm` 缺表安全降级；`41` 直连 TDengine 路径零变更。
  2. `ops/nginx/environment-user-ingress.conf` 与 `environment-user-ingress-routes.txt` 统一放行 `POST /v1/health-report-jobs` 与 `GET /v1/health-report-jobs/hrj_[0-9a-f]{32}`，适用于全部 V2.1 自助向导接入的客户环境。
  3. `HEALTH_CURVE_NAMES[lang]["temperature"]` 全 11 语修正为「充电枪温度 / Charger gun temperature」，防止交流慢充等无 BMS 电池温度场景将枪温误标为电池温度。

---

## 一、公网 HTTPS 端到端复测结果（`https://api.juxichongdian.cn`）

全部 C 端请求按规范发送 `third-session` + `X-Business-Entry: consumer` + `Accept-Language`（不发 `Authorization`），不打印任何 `third-session`、手机号、明文订单号、车牌或 VIN：

| 编号 | 测试场景与脱敏样本 | 实测结果 | 结论 |
|---|---|---|---|
| T1 | 匿名 `POST /v1/health-report-jobs`（无 `third-session`） | `HTTP 401`（`36.6 ms`），`error.code = INVALID_ACCESS_TOKEN` | 通过 |
| T2 | 未声明方法 `GET /v1/health-report-jobs` | `HTTP 405`（`16.8 ms`） | 通过 |
| T3 | 畸形作业 ID `GET /v1/health-report-jobs/bad_id` | `HTTP 404`（`15.5 ms`） | 通过 |
| T4 | 跨用户 `POST /v1/health-report-jobs`（用户 A 请求非本人订单 `sha256:a70b4d1128`） | `HTTP 404`（`78.3 ms`），`error.code = ORDER_NOT_FOUND` | 通过 |
| T5 | 本人近 14 天完成订单 `zh`（`sha256:cf6d5cc27a`，`37.315 kWh`） | `POST 202`（`85.6 ms`）→ 2 次轮询 `GET 200 completed`（`1020.5 ms`）；外层与 `report.rule_version` 均为 `health-v3`；`source_summary` 为 `order_snapshot=available, telemetry=available, protocol=unavailable, vehicle_capacity=unavailable`；`stop_reason` 判定为 `abnormal`（`异常中止：余额不足`），其余 6 项 BMS 指标及 5 维雷达规范返回 `unavailable + reason_code`；输出电压曲线 `224.8~239.4 V`（64 点） | 通过 |
| T6 | 跨用户 `GET /v1/health-report-jobs/{job_id}`（同租户用户 B 查询用户 A 作业） | `HTTP 404`（`9.5 ms`），`error.code = REPORT_JOB_NOT_FOUND` | 通过 |
| T7 | 同单同语复提（`sha256:cf6d5cc27a` + `zh`） | `HTTP 202`（`76.3 ms`），复用同一 `job_id`，`status = completed` 且直接内联完整 `report` | 通过 |
| T8 | 同单英文请求（`Accept-Language: en`） | 生成独立 `job_id`，`GET 200 completed`（`1020.3 ms`），`summary`、`conclusion.text`、`stop_reason.value`（`Aborted: insufficient balance`）及曲线图例 **0 汉字残留** | 通过 |
| T5b/T8b | 本人近 7 天有功充与枪温真实大电量订单补充实测（`sha256:0f0a497362`，`78.697 kWh`，`1034` 原始遥测点） | `zh`（`1022.8 ms`）：实测功率曲线最大 `7.044 kW`，温度曲线图例为 **`充电枪温度`**（`39.0 ~ 63.0°C`，未误标「电池温度」）；`en`：温度曲线图例为 **`Charger gun temperature`**，全报告 **0 汉字残留** | 通过 |
| T9 | 客户既有业务路由回归 `GET /upms/tenant/list` | `HTTP 200`（`9.0 ms`），Nginx 重载未影响客户原业务 | 通过 |

---

## 二、捷佳现网数据客观边界与未验收项（不越界宣称）

1. **硬件与协议客观边界**：只读盘点证实捷佳租户 `1990990768688599040` 下 **551 台**已登记充电桩全部为交流慢充桩（`charging_type=1`，`YKC1.6`，`~220V / ~7 kW`）；近 14 天 1,603 笔完结订单无直流 BMS SOC/单体电压、`charging-gun_property.batteryMaxTemperature` 均为 `-50` 哨兵值，时序库不存在 `charging-pile_comm` 超表（`tdadmin` 报 `Table does not exist`），业务库无 `ch_car_spec`。因此在捷佳交流桩场景下，`health-v3` **正确产出 `stop_reason` 与功率（kW）/输出电流（A）/输出电压（V）/充电枪温度曲线**，而依赖直流 BMS 的 SOH 总分、6 项 BMS 指标及 5 维雷达按契约降级为 `unavailable` + 明确 `reason_code`。
2. **多语言现网与单测边界**：现场公网直接验证了 `zh` 与 `en`（0 汉字残留）；其余 9 语覆盖与零汉字泄漏由单测 [test_health_report_language.py](../../tests/health/test_health_report_language.py) 保证。
3. **未验收项**：客户端前端页面（如 `batteryReport.vue`）接线与交互渲染、`business_accuracy="unconfirmed"`（人工/工程师业务准确率签收），以及需依赖车型电芯串联数 `N` 与直流 CAN 报文才能评估的全 7 项直流 BMS 场景（捷佳暂无直流桩）不在本回执宣称范围内。
