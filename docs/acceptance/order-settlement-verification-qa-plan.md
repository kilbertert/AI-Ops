# 管家端商城结算金额核验 QA 计划

关联决策：[ADR-0374](../adr/0374-operator-mall-settlement-verification.md) · 场景定义：[order-settlement-verification.feature](order-settlement-verification.feature)

## 重点风险与防呆项

1. **跨库排序规则与表结构映射**：`cloud_charging_pile`（`utf8mb4_0900_ai_ci`）与 `qumall_mall`（`utf8mb4_general_ci`）不得 SQL `JOIN`；`order_info` 无 `total_balance_reduce` 列，`order_item` 真实列名为 `commission_flag` / `goods_cost_price`，`shop_level` 软删条件为 `deleted_at IS NULL`。
2. **金额舍入口径**：严格使用 `Decimal` + `ROUND_HALF_UP` 四舍五入到分，禁止 Python 默认银行家舍入（`ROUND_HALF_EVEN`）或浮点误差。
3. **诚实边界与误报防护**：附加费用（停车费/占枪费/安心充/预约费）、按电量定额抽成（`settleFeeOfElect > 0`）、非零退款、缺失 `shop_level`、模式冲突及未同步/未支付商城单均标为「待验证」；规则 2 通道费溢出漏扣明确检出「不一致」。
4. **作用域与安全红线**：必须先凭 `QueryScope` 锚定充电订单归属并拒绝带 `user_id` 的 C 端范围，脱敏输出不含商户手机号、联系人与地址；严格只读，禁止宣称银行实际打款到账。
5. **ADR-0188 上线顺序**：`aiops-node-callback` 白名单、`node-41` `operator-tools.json` 与 `operator-exec-manifest.json` 必须先于 Gateway 与 Dify 发布就位。

## 验证矩阵

| 编号 | 层级与环境 | 验证内容 | 通过判据 |
|---|---|---|---|
| SETTLE-01 | 单测 `tests/operations/test_order_settlement.py` | `MySQLSource.operator_settlement_records` 快照事务、SQL 列名/软删条件、作用域拦截、30 条截断 | 仅发参数化单表查询；`order_item` 查 `commission_flag`/`goods_cost_price`；`shop_level` 用 `deleted_at IS NULL`；无效/C 端 scope 零 SQL 返回 `None` |
| SETTLE-02 | 单测 `tests/operations/test_order_settlement.py` | 3 种清分模式 × 多种费率、规则 2 溢出扣减与单位混算检出、规则 3/4 偏差检出 | 常规 8 组合全 6 规则「通过」；溢出未扣检出「不一致」与原因；偏差逐项定位 |
| SETTLE-03 | 单测 `tests/operations/test_order_settlement.py` | 停车费/按度抽成/缺 `shop_level`/模式冲突/退款/未同步/未支付商城单 | 受影响项标「待验证」并附具体原因，独立分项保持精确校验 |
| SETTLE-04 | 代理集成测 + `verify-aiops-gateway` | 前置证据顺序守卫（未读 `detail` 拒 `settlement`，未读 `settlement` 拒 `settlement_check`）、引用渲染、本地网关路由与鉴权 | 前置缺失回 `403`；完整链路 `completed` 且引用含原始与核验标签；网关契约全绿 |
| SETTLE-05 | 生产环境 `41` 公网 HTTPS 实跑（Ulink + 优链充） | 双租户已支付结算单正向核验、0 元/未结算边界单、跨租户订单与 `qa_id` 越权、匿名 `401`、既有诊断无回归、`jiejia` 无扰动 | 双租户 `202 -> 200 completed` 且 `agent_runs=succeeded`；工具链命中两新工具且数值与只读 SQL 逐分一致；跨租户 `404`/`CPMS_SCOPE_MISMATCH`；答复无「已打款到账」表述 |
