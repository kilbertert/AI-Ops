# 优链充管家失效路由恢复

- 里程碑目标：恢复租户 1941847786056323072 已失效的 operator_order_diagnosis
  路由（旧 Dify 应用 a57602c1… 已 404），并对"模型SOP工具循环"架构链路做
  实施复盘。
- 已完成：同租户已登记应用 b8690d6d… 草稿预览 2 轮 succeeded → 原 Dify 会话
  正常发布（version_no 1，route revision 2→3，publish_audit_ledger #95）→
  原 H5 同题诊断与同会话追问两轮 succeeded 且页面可见；跨租户提问越权事实
  零泄漏；其余 4 条路由逐字段不变，主租户与消费者入口未动。
- 当前分支/提交：fix/secondary-operator-route，基线 239f8bec；本 PR 仅文档与
  验收用例，无代码改动。
- 验证结果：docs/validation/2026-11-05-secondary-operator-route-recovery.md；
  验收计划与用例 docs/acceptance/secondary-operator-route-qa-plan.md、
  secondary-operator-route.feature。
- 已知缺口：工具拒绝（查无此单/越权/参数非法）以 model_failed 终结整轮、
  用户见 OPERATOR_DEPENDENCY_UNAVAILABLE 而非业务答复；节点 cpms_unavailable
  掩盖网关具体拒绝码。均为架构复盘确认的真实偏移，代码未改。
- 下一步：工具拒绝按业务结局分流并回喂模型、节点保留原始拒绝码，单独立项 PR。
