# 客服商城账务证据：启用与验证

仅AI-Ops读取商城证据，不改公司代码、业务行或权限。先读[环境手册](../env-41-runbook.md)。

## 配置与权限

`AIOPS_MYSQL_MALL_DATABASE`是与现有充电MySQL同实例的商城schema，默认`qumall_mall`；
显式空值表示不取商城证据。连接复用服务端Settings、只读事务和受限SSH入口，不另建凭据来源。
运行配置构建器已允许该键。模板账号不是线上实际账号，不能据模板推断线上授权。

上线前经`dev-host exec 41 --allow-service-exec`，用运行服务的环境文件与服务身份加载
`GatewayServerSettings.from_env()`，再按其`server_config_file`加载`Settings.from_config()`。
只输出判定，不输出配置、口令或连接字符串。复用`scoped_mysql_source`的
`source._cursor(consistent_snapshot=True)`，对配置指定的商城schema执行四次
`SELECT 1 FROM <schema>.<table> WHERE 1=0`；schema须经既有安全标识符验证，table固定为
`order_info_parent`、`order_info`、`order_item`、`pay_record_info`。
有权限只证明可读；仍须选定已授权订单验证完整链路。读取失败不自动GRANT、不换高权账号。

## 取数与判据

- callback6的billing保留原HTTP字段，附`mall_payment`；callback1–5不变。
- 充电订单先按原QueryScope的租户、站点、消费者过滤。管理员预览也必须先通过选中订单授权；
  商城父子单和支付按该充电订单消费者及租户限定，工单`user_id=-1`时用`consumer_id`。
- 一个REPEATABLE READ只读一致快照查询充电→父单→子单→明细→支付，行数有上限且报告截断。
  MySQL官方事务语义见[START TRANSACTION](https://dev.mysql.com/doc/refman/8.0/en/commit.html)。
  HTTP详情在另一时刻读取，必须另做原字段相等核对；不能宣称HTTP与SQL全链原子。
- `payment_includes_tax=confirmed`只覆盖已核实的普通充电类型11、唯一子单/税项/成功订单支付、
  无优惠抵扣退款、未截断且当前HTTP费用/支付/税率/税额均一致的场景。其余只列来源与缺口。
- 支付只采商城父单号直接关联的记录；不凭充电单号、子单号或未核实的sub_out_trade_no扩展。
  缺少关联支付、退款字段缺失/无效或截断时，refund_present为unknown；任一已关联退款大于0才报true。
- 原值均不重算。商城父单`paid_amount`只报告一致性结论，不作为支付权威，也不把它覆盖到原字段。
  每条支付保留state；商城内部单号不交给模型。金额相同不证明业务收费正确或银行到账。

## 发布与回归

1. 代码按正常PR/CI/合并/CD交付；本地测试不代表线上版本。
2. 客服自然正文恢复按[ADR0250](../../adr/0250-natural-replies-and-nonblocking-currency.md)执行，
   商城证据读取不回退，提示词要求原值；恢复方法见[操作手册](../response-policy-restore.md)。
   读取当前已发布客服图，通过`tools/content/upgrade_consumer_flow.py --order-composition`
   生成候选草稿，保留运营文字与模型；不可使用`--live-documents`替代本次迁移。
3. 按[正常发布手册](live-release.md)预览，再发布并记录版本/revision。旧图不被代码部署自动替换。
4. 原Dify/H5新会话核对原问题、税费归属、主动要求加减、无币种、缺项、跨用户拒绝。
   核对工具快照及正文：原数值、成功支付state、含税判据、父单冲突、未查过程不得说无异常。
5. 不完整/退款/重复记录只报告不能确认关系。数据库失败仍能读充电详情且标unavailable；
   此路径只在临时替身制造，不在生产撤权或制造故障。

原始日志、输入和会话只留私有证据。候选本地投影、模型回放、正式入口验收分别记录，不能互代。

## 本轮QA步骤与判据

| 用例 | 环境、前提与操作 | 预期及清理 |
|---|---|---|
| TAX-01 | 开发机，合成同权限单子单/税项/成功支付；调用新版billing | 原值不变且confirmed；临时库销毁 |
| TAX-02 | 开发机，分别改变HTTP原值、税项数、退款空值、关联支付 | 不误确认，unknown与0不同，不泄漏未关联支付；临时库销毁 |
| TAX-03 | 开发机，以不同租户、本人/他人、站点及工单主体驱动取数 | SQL过滤越权数据，缺权不查询商城；无生产清理 |
| TAX-04 | 开发机临时Gateway，旧回调与新版异常分支 | 旧合同不变，商城故障保留原账务；自动关闭服务与临时根 |
| TAX-05 | 当前授权订单只读快照→开发机候选，随后同应用原模型回放 | 金额原值、含税限定及缺证表述；不改正式应用，无业务清理 |
| TAX-06 | 合并部署并正常发布后，原Dify/H5新会话提原问题及主动运算请求 | 记录版本/revision与工具结果，逐项核对正文；撤销测试授权，保留正式审计 |

TAX-06需交付授权并实际执行，不能用TAX-05替代。Gherkin场景见[订单构成](order-composition.feature)。

## 历史确定性事实交付验收（ADR0250已更新表达方式）

模型只负责选取工具与检索依据，最终不采用自由模型正文。已授权原值由程序呈现，知识原文
不用于证明本单结论；没有当前订单证据时不复用历史数值。相同订单的后次读取失败不复用
本轮更早成功结果。只有status工具的`cpms.chargingInfo`可提供充电状态，支付state不代替它。
大数值不经过全局长号脱敏，避免合法金额被删除；身份、日期、数值先由各取数边界校验，
资料文字沿用既有脱敏与URL规则。资料不能安全呈现只隐藏摘录，不丢弃订单事实。
Dify试用summary和回读都由全部text blocks拼接，受管预览不再截断金额；正式H5读取同一冻结答案。

TAX-07：在临时原HTTP入口使用工具真取合成来源、模型返回已知错误金额/状态，最终、回读与历史
均不出现模型断言；改语言、原值null/0、重复查询失败、知识混合及不安全URL，再逐项复验。
TAX-08：用此前三份真实失败模型输出与同一授权快照调用最终交付函数，结果必须逐字相同且原值保留。
前者是本地整链接线，后者是固定真实证据回放；均不能替代TAX-06部署后原入口验收。
