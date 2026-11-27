# 360. 健康报告通用 tsdata 时序回退、环境用户入口放行与枪温曲线命名修正

状态：已采纳；补充 ADR-0353 与 ADR-0319，不改变 41 直连 TDengine 路径或单问/问答既有边界。

## 上下文

经 V2.1 自助向导新接入的客户环境（如最小接入模块 `jiejia`）与 41 主控制面存在三项共性架构差异：

1. 客户机接入配置 `/etc/aiops-access-<env>/production.env` 默认不配置直连 TDengine 凭据（`AIOPS_TDENGINE_*` 为空），时序读取统一经本机受管 `cloud-tsdata-biz` 门面（`AIOPS_GATEWAY_OPERATOR_TSDATA_URL`，即 `/tdadmin/data/query`）。既有 `health_job_runner.execute_health_report` 仅调用直连 `TDengineSource`，在新接入环境必然抛 `TDengine 只读账号未配置`，导致报告退化为全空；同时客户库可能缺少可选列（如 `charging-gun_property` 缺 `errorCode`/`errorReason`/`meterNow`），或纯交流桩环境根本不存在 `charging-pile_comm` 超表。
2. 环境用户入口白名单（`ops/nginx/environment-user-ingress.conf` 与 `ops/nginx/environment-user-ingress-routes.txt`）尚未纳入 `/v1/health-report-jobs*`，导致公网域名请求直接返回 Nginx `404`。
3. 无 `batteryMaxTemperature`（常见于交流慢充桩）时，`health_curves.py` 会回退绘制 `charging-gun_property.temperature`，而原文案将 `temperature` 序列译为「电池温度 / Battery temperature」，会把充电枪/设备温度误标为动力电池温度。

## 决定

1. **`health_job_runner` 增加通用 `tsdata` 只读回退**
   - 保持现有直连 `TDengineSource.get_gun_samples` 与 `get_pile_comm_frames` 为第一优先级（41 行为零变化）。
   - 当直连读取抛出 `SourceError` 且运行时 `gateway_settings.operator_tsdata_url` 非空时，分别通过受限 HTTP 客户端请求 `{tsdata_url}/tdadmin/data/query`（`charging-gun_property` 与 `charging-pile_comm`），按数据源声明时区（默认 `Asia/Shanghai`）格式化本地时间窗，单次最大响应体 `8 MiB`、禁止重定向。
   - `charging-gun_property` 明确命中 `Invalid column name` 时执行一次无 `fields` 回退，返回行按 `{"_ts", "batteryMinTemperature", *GUN_COLUMN_NAMES}` 严格投影，兼容不同客户列集差异。
   - `charging-pile_comm` 按 `COMM_FRAME_CODES` 过滤并仅投影 `{"_ts", "code", "direction", "raw"}`；若上游返回 `Table does not exist` 等错误则收敛为 `SourceError` 并由执行器安全降级为 `source_summary.protocol = "unavailable"`，不阻断 `stop_reason` 与功率/电压/电流曲线生成。

2. **统一环境用户入口放行健康报告作业端点**
   - 在 `ops/nginx/environment-user-ingress-routes.txt` 与 `ops/nginx/environment-user-ingress.conf` 中新增：
     - `POST /v1/health-report-jobs`（精确匹配，仅放行 `POST`，其余方法 `405`）；
     - `GET /v1/health-report-jobs/{job_id}`（`^~` 前缀内强制 `$uri ~ "^/v1/health-report-jobs/hrj_[0-9a-f]{32}$"` 形状闸，仅放行 `GET`）。
   - 复用 `environment-user-ingress-proxy.inc` 完成入口分流、凭据转换与 `access_log off`，同步更新 `tests/operations/test_environment_user_ingress.py` 用真实 `nginx` 验证转发与越权/错方法/畸形 ID 拒绝。

3. **修正 `temperature` 曲线图例为充电枪温度**
   - 在 `src/aiops_diagnostics/health/health_report_copy.py` 的全部 11 种语言映射中，将 `HEALTH_CURVE_NAMES[lang]["temperature"]` 由「电池温度 / Battery temperature」改为「充电枪温度 / Charger gun temperature」，避免交流桩或无 BMS 电池温度场景误导用户。

## 替代方案

- **仅在捷佳人工放通 Nginx + 特判捷佳逻辑**：会导致后续每一个经 V2.1 自助向导接入的客户环境重复踩到公网 `404` 和 `TDengine 只读账号未配置` 故障，不可持续，拒绝。
- **要求每个新接入客户环境都单独开通直连 TDengine 账号并开放端口**：与 V2.1 最小接入模块仅依赖既有 `cloud-tsdata-biz` HTTP 门面的架构冲突，额外引入凭据与网络面暴露，拒绝。
- **把 `charging-gun_property.temperature` 仍译作「电池温度」**：在交流慢充桩等无 BMS 电池温度上报的设备上会把枪温误标为电池温度，违背实事求是契约，拒绝。

## 后果

- 所有经 V2.1 自助向导接入的客户环境无需单独定制脚本即可通过公网 HTTPS 提供 `health-v3` 电池健康报告；41 直连路径保持原样不变。
- 若环境未配置 `AIOPS_GATEWAY_OPERATOR_TSDATA_URL`，回退路径不触发，仍保持原有 `telemetry/protocol = unavailable` 安全行为。
- 交流慢充桩本身无直流 BMS CAN 帧（`charging-pile_comm` 缺表、SOH 置空）的物理限制继续通过 `unavailable + reason_code` 如实暴露。
