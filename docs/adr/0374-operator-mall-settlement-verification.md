---
status: active
---

# 374. 管家端商城结算金额只读取证与 6 大清分规则确定性核验

状态：已采纳（2026-11-28）；扩展管家端（Operator / CPMS）诊断工具集，与 C 端/管家端「车主应付账单核验」（`cpms_order_billing` + `cpms_order_amount_check`）职责分离，为场站运营提供新能源充电订单同步商城后的清分结算核验能力。

## 上下文

管家端此前已具备充电订单详情、计费模板、车主应付账单核验（`cpms_order_billing` + `cpms_order_amount_check`）、过程统计、充电时间线、离线分析与通讯事件等 11 项只读工具，但缺少「场站商城清分结算金额分析」：

1. **业务视角不同**：车主应付账单核验只验证用户实付电费、服务费、启动费与增值税；场站运营更关注充电单完结支付并拆单同步到商城（`qumall_mall.order_info.order_type='11'`、`order_item`、`shop_info`、`shop_level`）后的**订单结算金额、启动费服务费、支付通道费、平台总服务费与订单分佣基数**。
2. **跨库结构与排序规则边界**：充电主库 `cloud_charging_pile` 与商城库 `qumall_mall` 分别使用 `utf8mb4_0900_ai_ci` 与 `utf8mb4_general_ci` 排序规则，跨库 SQL `JOIN` 会触发 `1267 Illegal mix of collations`；且实测 `41` 现网 `qumall_mall` 表结构中，`order_info` 无 `total_balance_reduce` 列，`order_item` 真实列名为 `commission_flag` 与 `goods_cost_price`，`shop_level` 使用 `deleted_at IS NULL` 软删而非 `del_flag`。
3. **复杂清分口径与误报风险**：充电结算存在 3 种分账模式（电费单独结算 `0`、整笔订单跟随 `1`、电费跟随服务费结算 `2`）、通道费溢出扣减、附加费用（停车费/占枪费/安心充/预约费）、按电量定额抽成（`settleFeeOfElect > 0`）及退款等边界；同时商城未保留结算时刻的 `shop_level` 历史费率快照，宽表字段 `serviceAmountServiceFee` 会被汇总值覆盖。

## 决定

1. **双工具职责分离与 13 工具契约**：
   - 在 `src/aiops_diagnostics/operator/operator_tools.json` 新增两项只读工具，并在 `operator_sop.md`、`cpms_proxy.py`、`cpms_evidence.py`、`operator_agent.py`、`operator_evidence.py` 贯通：
     - `cpms_order_settlement`（`/mall-settlement/{order_no}`）：必须先完成 `cpms_order_detail` 取证，以调用者冻结 `QueryScope` 在只读快照内提取脱敏商城结算证据（排除商户手机号、联系人与地址）。
     - `cpms_order_settlement_check`（`/order-settlement-check/{order_no}`）：纯计算核验工具，仅消费本轮已交付的 `cpms_order_detail` 与 `cpms_order_settlement` 证据，不产生外部请求或业务写入。
   - 保持 `agent_content.py`（`OPERATOR_STARTERS`）不变，不改动管家端快捷问题列表。
2. **跨库同快照参数化分步读取与权限先置**：
   - `MySQLSource.operator_settlement_records` 拒绝无 scope、空站点或含 `user_id` 的 C 端 scope；在单一 `REPEATABLE READ` + `START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY` 事务内分步单表参数化查询（零跨库 `JOIN`）。
   - 先锚定 `cloud_charging_pile.ch_order_info`（强制下推 `tenant_id`、可见站点与 `expected_site_id`），未命中直接拦截（`CPMS_SCOPE_MISMATCH`），绝不触碰商城库；命中后再以 `tenant_id + shop_id(=site_id) + order_type='11' + order_no` 精确读取 `qumall_mall.{order_info,order_item,shop_info,shop_level}`。
   - 服务费单项抽成直接取 `sku_code='1'` 子项的 `service_fee`，规避结算宽表聚合覆盖问题。
3. **确定性 6 大规则 `ROUND_HALF_UP` 核验与 3 项已确认边界口径**：
   - 所有金额计算使用 `Decimal` 与 `ROUND_HALF_UP` 四舍五入到分，覆核规则 1（订单结算金额）、规则 2（启动费服务费）、规则 3（实付款组成及充电单/结算单一致性）、规则 4（支付通道费分项与合计）、规则 5（平台总服务费）与规则 6（订单分佣基数）。
   - **边界口径 1（诚实待验证）**：订单含停车费/占枪费/安心充/预约费等附加科目、按电量定额抽成（`settleFeeOfElect > 0`）、非零退款、缺失 `shop_level` 配置或清分模式冲突，以及未支付/未同步商城单时，受影响汇总规则一律标为 `"待验证"` 并说明原因，绝不误报 `"不一致"` 或伪造 `"通过"`。
   - **边界口径 2（规则 2 通道费溢出检出）**：当 `startAmount < rawStartService + startRateOfPayment` 而后台因百分比与小数单位混算漏扣溢出差额时，明确将规则 2 标为 `"不一致"` 并指出未扣减通道费溢出。
   - **边界口径 3（只读与业务边界）**：核验输出固定附带限制声明（当前 `shop_level` 非历史快照、仅核对商城账面清分结果），严禁宣称银行实际打款到账或点位奖已发放，不新增任何业务写动作。

## 后果

- 管家端在保持严格只读（ADR-0001）与多租户站点隔离的前提下，补齐了充电单到商城清分结算的闭环诊断与逐项算术复核能力。
- 遵循 ADR-0188：部署本能力时须先同步 `aiops-node-callback` 白名单、`node-41` `operator-tools.json` 与 Gateway 侧 `operator-exec-manifest.json`，待节点上报新工具摘要后再部署 Gateway 及迁移 Dify `cpms-node` 提供方并发布应用；既有独立客户环境（如 `jiejia`）维持 11 工具冻结快照不受影响，可按 Day-2 清单按需升级。
- 未覆盖的附加费科目与按度抽成场景诚实保留为 `"待验证"`，后续如业务提供独立规则样本可在 `order_settlement.py` 中增量补充。

## 备选方案

1. **直接复用或扩展 `cpms_order_amount_check` 合并输出车主账单与商城结算**：被否。两类问题面向不同业务链路与数据源（充电主库 vs 商城清分库），合并会导致单次工具调用过重、前置依赖混乱且误导普通计费提问。
2. **用跨库 SQL `JOIN` 一次性拉取充电单与商城结算单**：被否。两库排序规则不同会直接报 `1267 Illegal mix of collations`，且无法保证先验证充电单作用域再触碰商城表的最小权限顺序。
3. **遇到附加费用或按度抽成时直接按近似公式强算并判 `"不一致"`**：被否。在无完整历史费率快照与附加费抽成口径下强行判不一致会制造大量误报，违背诊断工具「缺证诚实标待验证」原则。
