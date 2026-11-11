# 消费者订单构成 QA

判据来自本轮已确认需求、父会话提供的上游字段清单和仓库 ADR0193/管家既有单位映射；
合成源独立填写，不从候选输出反推。新增检查拦截已发生的分项丢失、金额舍入与null变零，
旧测试只有总额/支付/退款且Agent快照为替身，无法检验真实reader组合链路。

| 入口与输入 | 必须观察到 / 必须拒绝 |
|---|---|
| 临时HTTP订单源→原提问→`/v1/internal/consumer-data/agent` | 单次GET，电费5.20/服务费3.30，摘要及两行分时逐项相同；自然正文不被覆盖 |
| 0、null、缺字段、NaN/Inf/bool/负值/错类型 | 零保留；缺失不补零；非法金额拒绝或标缺项；JSON数字不经float损失精度 |
| 缺币种、USD、相互冲突币种 | 只报来源币种；冲突明示，禁止换算、合计与推断差额 |
| 分时缺失、空数组、错误行、25行、过长文本 | 明确区分；最多24行、有遗漏计数，不默默清空 |
| 上游额外电话/姓名/嵌套信息 | 不进入工具结果；feeRate仅保留来源数值，单位/构成未核定 |
| 他人单、跨租户、取数前后撤权/取消、第9次调用 | 原拒绝与预算有效；不泄露构成 |
| callback1–5及旧plain/facts/reply | 原投影不变，原金额/退款语义回归 |

作者运行focused、全量一次、构建、容器导入和Gateway临时根检查，证据在
`dist/verification/consumer-order-composition/`。解析/金额做定向coverage与最小受控变异。
父会话负责独立Hack、私有来源核对、原Dify/H5真实模型问答和部署，本轮未完成业务验收。

真实问答至少覆盖“订单详情”“电费和服务费各多少”“分时明细”“总额为何与支付不同”，
逐项对照独立源；重点确认无人民币猜测、无差额归因、无计费正确性结论。

## 来源字段映射

全部来自同次`GET /chOrderInfo/getOrderInfoDetail/{order_no}`。身份仍由已验证调用者提供。

| 来源位置/键 | 消费者工具字段与口径 |
|---|---|
| `chOrderInfoModel.totalAmount/payAmount/refundStatus/refundAmount` | 原`order_amount/recorded_payment_amount/refund_status/recorded_refund_amount`，账务记录不是渠道回执 |
| `electricity/time/createdTime/stopTime` | `order_summary`同名键：kWh、分钟、创建时间、充电停止时间，不补时区或计费开始 |
| `electricityFee/serviceFee/launchFee` | `fee_components`同名：电费/服务费/启动费 |
| `dsElectricFee/dsServiceFee/parkFee/occupyFee` | 电损电费/电损服务费/停车费/占位费，不混用 |
| `marketAmount/vat/vatFee` | 优惠减免/税率百分比/税费；税率仅0–100 |
| `currency/currencyCode` | `currency`及`currency_status`：缺失、非法、冲突均不猜 |
| 外层`periodFeeDetail` | `periods.records`，保留`startTime/endTime/electricFee/serviceFee/totalFee/electricity/feeRate/type` |

`missing_fields`标null或缺字段，`invalid_fields`标被拒绝值；不会透传原值或上游其他键。
分时数组区分missing/empty/invalid/provided/partial，保留顺序与来源行数；最多24行，文本64字符，
金额最长32字符、最多8位小数、上限999999999999.99，拒绝非有限值、bool、负值与错误类型。
日期支持`YYYY-MM-DD HH:mm:ss`或`T`分隔，可带小数/offset；分时时钟支持`HH:mm[:ss]`及
`24:00[:00]`。日期/时钟实际解析验证，非法内容标invalid，保留其余可用项，不补时区。
分时electricity可能由费用反推；feeRate是组合费率，不保证每kWh，不用于正确性结论。

## 本地复跑与后续真实部署清单

```bash
uv run pytest tests/consumer tests/dify/test_consumer_dependencies.py tests/common/test_bounded_http.py
uv run python .claude/skills/verify-aiops-gateway/scripts/observe.py --out dist/verification/consumer-order-composition/gateway
uv build --out-dir dist/verification/consumer-order-composition/packages
M2LIVE_GATEWAY_IMAGE=aiops-gateway:consumer-order-composition M2LIVE_DIST="$PWD/dist/verification/consumer-order-composition" deploy/m2-live-03/build-image.sh
docker run --rm --network none --read-only --entrypoint python aiops-gateway:consumer-order-composition -c 'from aiops_diagnostics.gateway.gateway_api import create_gateway_app; print("Gateway import: passed")'
```

父会话在获准发布流程中更新Gateway **1.7.5镜像**及执行离线迁移的同版本工具包；
不需要更新Java节点、消费者签名插件、公司App/H5或数据库schema。
现役两张消费者Agent图均要更新已知指令块；仅换Gateway代码不会自动改已发布图。
先保存原草稿、正式图和hash，再运行以下离线命令（只写新文件，不导入/发布）：

```bash
uv run python tools/content/upgrade_consumer_flow.py <原v6草稿.yml> \
  --gateway <原可信根地址> --order-composition --out <新草稿.yml>
```

1.7.5迁移保留BASE及未撤销的运营文本，移除唯一完整已知v1–v5规则，并将v5追加到全文末尾；
同时精确撤销全部已知算术差额许可。支持BASE、v1–v4及当前v5，重复运行结果一致；已发布v2/v3/v4按完整字节冻结。
未知改写/重复块/异常标记拒绝，不得把运营后缀删掉来消除冲突。
核对diff仅instruction一处，再按[现役发布手册](agent-conversation-qa.md#复现命令与迁移方法)
试用、冻结正式版、原Dify/H5问答。恢复走原版本恢复API，不能直接改数据库行。
原有`--agent-conversation`只用于旧图升级，不用于重建现役v6图。

## 事实约束的真实语义复验

本轮合成图测试只证明迁移保留、指令位置、未知拒绝和幂等，不用关键词断言冒充自然语言语义验收。
新增检查拦截真实图后缀覆盖事实约束的迁移风险；来源为父会话已核图形状，旧测试只要求原位替换。
父会话真实preview逐句对照工具结果，不能只核completed：

- 币种missing/invalid/conflicting：开头说明金额币种未提供，所有金额无元/人民币/美元/币种符号，不能靠末尾免责声明补救。
- 费用明细：只列各分项、来源总额/支付；不自行合计或税基运算，不作“无异常/放心”等未核验承诺。
- null占位费：未提供，不称未收费；零值可简洁列示。单项问题仅答该项。
- v4逐字段存在性：`parkFee=0.00`与`occupyFee=null`必须分别说明停车费零值、占位费未提供；所有语言的金额或缺项断言均对照同名来源键，不能合并为两项缺失。
- v3开头完整性：只概括已取得的具体分项，同时点出缺项；有missing/invalid/null、币种缺失或截断时，不能先称齐全再补限制，不能用“放心核对”保证完整性。
- 分时与订单记录不同时：逐项保留来源值，不拼总计或编原因；分时电量不反算费用。
- 用户要求加总或问是否收费正确：缺确定性核验时说明记录范围与限制，不自行运算下结论。

只读语义复验不修改订单、公司App/H5、模型、工具、库或权限。发布前保留版本，发现语义失败走正常恢复API。
冻结当前规则后先在两份真实授权草稿仅更新instruction，正常预览、逐句核对后再推进代码发版；
正式发布及原H5仍需父会话另行验收，不能把草稿结果视为已经上线。

### 英文受管预览方法

现有manage桥未转发`Accept-Language`，从该桥试用默认中文，不能用英文提问文本代替语言选择。
英文核验直达已运行Gateway的受管`POST /v1/dify/management/trial`与`POST /v1/dify/management/trial/result`：复用正常Dify console session，
通过`X-AIOps-Console-Session`传会话、`Authorization: Bearer <独立trial key>`传试用密钥，
并设`Accept-Language: en`。请求仍按现有试用合同给授权app/order及问题，不换模型或绕过主体核验。
读取结果时传同一`app_id`与返回的`trial_id`，继续使用相同受管凭据和语言头，核对实际正文而非只看completed。
所有凭据由受控进程运行时从受控位置读取，不写命令行、证据、日志或仓库；只保留脱敏判据和结果。
Bridge语言转发功能不在本任务范围内；此方法由父会话实测，作者不读取会话、密钥或真实订单。

## 严格原值候选与事件取证

v5候选验收来自产品“字段读取多少就展示多少”的要求，原金额数值本身保持原样。
新增最小迁移检查拦截两处已知计算许可未移除；旧检查只保护运营文字不变，无法证明撤销许可。
既有reader检查增加独立总额/支付断言：源总额与各分项和不一致时也必须原样保留，不做重算。

| 用例 | 输入与独立判据 | 预期 |
|---|---|---|
| SV-01 来源追踪 | 同一Dify会话→message.workflow_run_id→Gateway run；同主体权限读取订单表和两个原页面接口 | 明确哪个字段首次出现该值，不能把“数值相加恰好等于它”当AI计算证据 |
| SV-02 迁移 | BASE/v1–v4、原前后文本两处已知计算许可 | 仅撤销完整已知许可及替换当前规则；其他图字段与输入不变，二次幂等 |
| SV-03 原问题 | 真实原问题及其已授权工具结果，同模型/参数、仅替换候选instruction | 费用/支付逐项列原值，无差额数字、无舍入；完成状态不推无异常 |
| SV-04 主动要求运算 | 要求金额相减、分项合计、电量两位小数 | 说明只提供原值；不能把同值税费当作来源差额，不生成近似电量 |
| SV-05 原入口 | 候选按正常预览/发布流程接入后同题重放 | 正常鉴权和工具闭环、正式版本可追踪；未执行前不能报通过 |

现场读取按环境手册与公司只读源码规程。Dify PostgreSQL使用只读事务及精确会话谓词，
只选必要message/workflow/node列；Gateway SQLite使用mode=ro及精确workflow_run_id关联。
导出会包含grant/输入，原件必须私有，报告仅保留经筛选字段，不能打印整条agent_log。
原App详情必须抓同一订单的实际请求和页面标签；公司缓存源码是解释线索，不冒充现役构建身份。

离线真实模型回放只将已授权结果送回同应用原模型；用Dify PluginModelAssembly现有创建器，
在受控进程运行时取模型连接，不输出凭据，不改应用/草稿/正式指针，不调用业务写接口。
回放不执行工具、不写正式QA，因此与原入口端到端验收分别记录；保留每次失败，不做无界重试。
