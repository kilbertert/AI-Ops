# 充电税费来源与商城支付记录只读取证

适用于已授权订单，先读[环境手册](../env-41-runbook.md)和[公司源码规程](../company-code-recon-procedure.md)。
不发支付/退款/重算请求，不重放Kafka消息，不修改业务行或权限。主机与路径按项目dev-host声明解析。

1. 从已知QA精确读取`chat_dify_runs.scope_json`，用原`ScopedOrderAuthorizer`与`resolve_query_scope`
   确认订单仍可见。不是取“最新一条”替代用户反馈对象；首尾均检查主体范围。
2. 通过`scoped_mysql_source`使用只读一致快照。充电order_no谓词与`_scope_where`的租户/用户/站点条件共同下推，
   只选关联ID、费用/税率/税额、payAmount、状态与必要时间，不SELECT个人资料。
3. 首次查询先只读`information_schema`确认实际表列。商城父单关联为充电out_trade_no→父单order_no；
   父单tenant_id/user_id须与授权充电记录相同，再按parent_id读取子单、order_id读取明细。
   支付记录按已确认父/子单号和同租户同用户读取，必须检查唯一性与状态。多条、不完整或身份不符就停止结论。
4. 税项按已核业务合同sku_code=9取其payment_price及tax_rate，禁止把差额算作税。
   订单金额、支付金额、已付字段各自比较，不能互换。重复/退款/分支付等单独留待适用规则核验。
5. 通过声明入口`dev-host exec 41 --allow-service-exec`只读列容器和挂载位置，再读选定服务的历史压缩日志。
   只扫描订单发生当日、精确订单/父单/支付ID；输出写开发主机私有文件，不能直接把整条业务日志发给模型/PR。
   Mapper的Preparing/Parameters按线程和紧邻语句配对；必须确认参数数量和尾部主键，避免将并发别单SQL误配。
6. 用运行制品而非默认分支猜生产代码：`docker exec <container> jar tf <jar>`定位目标类，
   `javap -c -p 'jar:file:<jar>!/BOOT-INF/classes/<class>.class'`读选定类字节码，另取JAR SHA256。
   嵌套lib不能用上述外层类URL假称不存在；只按已核路径提取必要类在开发机分析，不部署临时源码。
7. 记录来源值、链路关联、时间顺序及代码对象流，分开“生产实测”“静态解释”“仍未验证”。
   raw日志、订单标识、账户/业务拓扑和凭据留私有证据；repo仅保留脱敏结论与可执行方法。

相同数字存在不能证明因果：只有真实商城税项、支付回执及对应回写链支持，才能判断税归属。
父单、子单和支付总表若有冲突，不选一个“看起来正确”的金额覆盖业务；先还原每个字段写入顺序。
已知实例与修复方向见[税费来源调查](../../validation/2026-11-11-mall-tax-origin.md)。
