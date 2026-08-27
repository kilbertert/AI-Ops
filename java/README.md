# Java 侧诊断接口落地工件

本目录承载由 AI-Ops 团队仓库保管的充电桩 Java 服务源码工件。生产 Java 仓库在远端
（`124.243.178.156`），本地不复制完整工程；这些文件用于落地 `docs/diag-query-api-plan.md`
定义的受控只读查询接口，待平台侧合并到 `cloud-charging-pile-web` 对应包路径。

## 文件

- `cloud-charging-pile-web/src/main/java/com/qushiyun/cloud/charging/pile/web/controller/DiagQueryController.java`
  —— `/diag/*` 受控只读查询：T1 `GET /diag/order` 按 `order_no` 返回 `ch_order_info`
  全字段与 `ch_fee_template_record` 快照、`LIMIT 3`；T4 `GET /diag/occupy-order` 按
  `order_no` 或 `order_id` 反查 `ch_occupy_order_info` 全字段、`LIMIT 20`；T5
  `GET /diag/redis-stream` 仅放行 `third.order.sync.queue` / `third.order.sync.notify.queue`，
  返回有界 `XREVRANGE` 窗口、Stream 长度、消费组滞后与按 `order_no` 的匹配计数，`max_messages`
  强制上限 1000；T6 `GET /diag/device` 按 `device_id` 或 `device_code` 二选一查询
  `iot_charging_device` 的十字段快照，`LIMIT 1`。四个端点均控制器自校验 `X-Internal-Token`、
  参数白名单校验并返回 `R<T>`。
- `cloud-charging-pile-web/src/main/java/com/qushiyun/cloud/charging/pile/web/aspect/DiagQueryAuditAspect.java`
  —— `/diag/*` 审计切面：记录调用方 `internal:AIOps`、脱敏后的查询参数摘要、返回行数、耗时与成功/失败，不落响应体。

## 落地与验证边界

- 包路径与平台类名（`InternalTokenManager`、`R<T>`、框架注入方式）在合并到真实 Java 仓库时需按实际模块包名微调。
- 本仓库无 JDK/Spring 工具链，未编译或部署该 Java 服务；这里通过
  `tests/test_diag_order_contract.py`、`tests/test_diag_occupy_order_contract.py`、
  `tests/test_diag_redis_stream_contract.py` 与 `tests/test_diag_device_contract.py`
  守护工件中的外部契约关键点，不等价于真实环境验收。
- 真实环境验收按 `docs/diag-query-api-plan.md` §11 的 Gherkin 场景执行，当前状态为“待验证”。
