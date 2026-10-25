# WF01 C 端业务完善

目标是原 App 内的只读客服：操作指导、真实状态、连续追问、图文/视频和人工入口。
实现顺序为内容基线 → Chatflow/会话 → 受控只读能力 → 媒体/人工 → 图片输入 → 原App整体验收。
现有状态节点路线保持原行为，WF01通过等价业务验收之后才切换。

## 内容包生成

```bash
uv run python tools/build_consumer_knowledge.py <私有原包目录> --output <新的私有输出目录>
```

输入：运营工作簿、`knowledge_markdown/`、`MEDIA_ASSET_MANIFEST.csv`、`00_System_Prompt.md`。
复用仓库已有OOXML读取器，不安装新的Excel运行依赖。工作表按名称读取；答案与Markdown逐字比对，
媒体CSV与工作簿比对；重复/未知ID、缺必填字段、错正文拒绝，写入前完成所有校验。

输出为草稿：每问答中英文各一文件；`content.json` 保留来源摘要、原始行、内部依赖/风险/配置、
媒体规划与双向冲突。缺媒体地址不造URL；有地址也不自动当作已核验素材。
两个关联方向不一致时保留双方事实而非擅自取并集。既有输出拒绝覆盖。
正文只投影标准答案，不将内部工具名或升级规则直接交给用户。

`workflow_scope`只是后续能力核对清单，不是可执行白名单：所有只读候选仍需实际接口与授权核验。
启动/停止/下单/隐私请求/安全事件建单/工单创建列入首轮禁用。
原System Prompt保留为来源，不能原样启用其中的建单指令。

## 原App完成判据

以[业务验收计划](qa-plan.md)执行：实际Chatflow编排，原App连续会话、适用知识、
API异常/越权、安全优先、真实可播放媒体、实际人工入口逐项留证。导入成功不能代替这些结果。
私有材料不提交，仓库只收工具、脱敏说明和合成测试。业务问答仍须按租户规则审核。

## Chatflow Service API复跑

`uv run python tools/verify_consumer_chatflow.py --out <私有报告.json>`
启动一次性Dify1.17.1和官方provider，合成模型驱动真实Chatflow三轮；
无宿主服务端口、无生产凭据。依赖已有Docker镜像及官方插件可下载。结束清理自己的容器与卷。
`dify_chatflow.py`同时核对workflow_finished和message_end，固定版本与会话；调用方仍需实现范围授权。

## 原提问接口的Chatflow接线（默认关闭）

`AIOPS_GATEWAY_CONSUMER_CHATFLOW_ENABLED=1`启用服务端登记的`consumer_service`场景。
沿用`CHAT_EXECUTION_BINDINGS_FILE`，engine=dify，另需app_id、workflow_id、workflow_key_file、
agent_id、agent_version_no及environment/tenant_id/entry=consumer；本人状态可按下节显式启用；支付/退款等仍待接入。
首次POST返回conversation_id，客户端后续原样带回；它是Gateway会话，不是Dify会话ID。
失败/取消或发布版变化后的会话返回409要求新会话；原QA与会话正文过期/换绑后拒绝交付。
旧状态查询场景与开关独立，未配置WF01时保持既有路线。本片仅供隔离接线验证，不能当完整客服上线。

## 知识、人工与安全

`consumer_service`登记可增加`content_file`和`knowledge_base_ids`，模板为
`deploy/dify-chat/consumer-service.dsl.yml`。content_file必须是该租户/环境的私有发布内容：
`schema=aiops-consumer-reviewed/1`、`state=published`、entries按FAQ_ID索引，条目含
`state=approved`和`answers.zh/en`；可选support按语言提供label/url。原编译草稿不能直接用。
真实地址只由受控配置给出，电话/HTTPS渠道不来自模型；没有配置就指向App/站点已有入口。

Dify的HTTP节点自己带data_grant回调`POST /v1/internal/consumer-knowledge`，没有请求体。
模型只接收检索结果，不接收凭据；它只返回kind/faq_id，Gateway投影标准答案和来源。
知识库需单FAQ文件，标题带KB-C-ID；本轮命中且发布内容里存在才允许选择。
该路径没有启用元数据过滤，不要把不同地区/版本的未审内容混入同一个发布集。

安全关键词先给出保守避险提示，不以订单号/模型响应为前提；人工请求给真实已配置入口。
实时支付/退款等未接能力明确不可核实，不能用FAQ替代实际状态，也不会提交写操作。

## 连续未解决反馈

同会话第一次明确反馈“还是不行”或“still not working”时，检索带上已验证的原问题，
回复询问当前步骤和错误提示。第二次及之后显示受控人工渠道，不再请求Dify或知识库。
新主题/新会话重置；计数落在现有运行上下文中，不依赖App内存。前轮过期或正文不一致时拒绝续用。
明确表达之外仍由Chatflow处理，尚未证明所有隐含未解决表达；提示渠道不代表已转接。

## WF01本人实时订单状态

在consumer_service登记加入`live_order_status: true`，并提供已审核content_file；缺省false。
复用既有租户充电端点配置，当前调用方必须能重新通过`aiops:orders:read`。
明确状态问法指定本人订单或最新单时，由网关选单并冻结进本轮grant；HTTP步骤回调时只读该单CPMS。
该轮不检索知识、也不派发节点，模型只选realtime类型，原GET返回同轮状态、来源与UTC查询时间。
缺单号先澄清，其他实时需求仍明确不可用；不从充电状态推断支付/退款结果。

实时轮次及其下一轮不复用Dify上游会话，避免失效业务数据进入后续模型输入；Gateway会话记录保留。
回调读前读后、worker写前、QA/会话回读都再次判订单归属与TTL/当前绑定。失败不回落其他引擎。

## 订单账务记录（不等同银行到账）

`live_billing_record: true`独立启用只读订单详情，缺省false且要求已审核内容。明确账务/退款记录
问法使用同一本人选单与短期授权路径，run.data_kind固定billing，模型不得改取数能力。
返回订单金额、支付金额字段、退款状态及金额，不补币种、不把缺失当零、不进行结算计算。
显示记录来源与查询时间；金额字段不证明扣款、退款记录不证明银行到账。操作咨询仍走知识。
支付渠道交易状态、银行到账和退款审批进度尚未接入；不调用付款或退款申请接口。
