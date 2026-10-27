# WF01 C 端业务完善

目标是原 App 内的只读客服：操作指导、真实状态、连续追问、图文/视频和人工入口。
实现按内容基线、Chatflow会话、受控只读能力、媒体与原App验收推进；图片输入单列保留策略验收。
目标租户已切至WF01，旧状态节点路线作为显式恢复路径保留，失败不自动回落。

2026-10-25：目标租户原H5已完成正式发布和真实验收，WF01 V3启用，旧节点版本保留恢复。
完整构建、部署和恢复见[正式交付手册](live-release.md)，边界见[验收记录](../../validation/2026-10-25-consumer-wf01-live-release.md)。

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
远端`AIOPS_GATEWAY_DIFY_CHAT_BASE_URL`必须使用HTTPS，与执行客户端相同；不能直接沿用控制台HTTP地址。
沿用`CHAT_EXECUTION_BINDINGS_FILE`，engine=dify，另需app_id、workflow_id、workflow_key_file、
agent_id、agent_version_no及environment/tenant_id/entry=consumer；本人状态及账务记录可按下节显式启用；支付渠道到账仍待接入。
首次POST返回conversation_id，客户端后续原样带回；它是Gateway会话，不是Dify会话ID。
失败/取消或发布版变化后的会话返回409要求新会话；原QA与会话正文过期/换绑后拒绝交付。
旧状态查询场景与开关独立，未配置WF01时保持既有路线。本片仅供隔离接线验证，不能当完整客服上线。

## 知识、人工与安全

`consumer_service`登记可增加`content_file`和`knowledge_base_ids`，模板为
`deploy/dify-chat/consumer-service.dsl.yml`。content_file必须是该租户/环境的私有发布内容：
`schema=aiops-consumer-reviewed/1`、`state=published`、entries按FAQ_ID索引，条目含
`state=approved`和`answers.zh/en`；可选support按语言提供label/url。原编译草稿不能直接用。
真实地址只由受控配置给出，电话/HTTPS渠道不来自模型；没有配置就指向App/站点已有入口。

Dify的HTTP节点自己带data_grant回调`POST /v1/internal/consumer-knowledge`；无图兼容空体，
图片分支以有界纯文本body携带识图JSON，不允许自报租户/订单。
API域名部署`ops/nginx/dify-consumer-callback.conf`精确POST转发，保留原Authorization，
清空业务身份头；不能经过会替换App服务令牌的通用`/v1/`代理。先备份、`nginx -t`再reload，
失败恢复原片段。可用已撤销验收凭据验证得到`GRANT_REVOKED`而非`GRANT_UNKNOWN`，不得重放有效凭据。
模型只接收检索结果，不接收凭据；它只返回kind/faq_id，Gateway投影标准答案和来源。
知识库需单FAQ文件，标题带KB-C-ID；本轮命中且发布内容里存在才允许选择。
该路径没有启用元数据过滤，不要把不同地区/版本的未审内容混入同一个发布集。

安全关键词先给出保守避险提示，不以订单号/模型响应为前提；人工请求给真实已配置入口。
支付渠道到账等未接能力明确不可核实，不能用FAQ替代实际状态，也不会提交写操作。

## 连续未解决反馈

同会话第一次明确反馈“还是不行”或“still not working”时，检索带上已验证的原问题，
回复询问当前步骤和错误提示。第二次及之后显示受控人工渠道，不再请求Dify或知识库。
新主题/新会话重置；计数落在现有运行上下文中，不依赖App内存。前轮过期时不继承旧上下文，但明确的新问题可在同一会话继续；正文不一致仍拒绝。
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

## 已审核素材交付

content_file可选`media`按素材ID登记：state=approved、kind=image/video、mime_type、
knowledge_base_id/document_id/backend_id/title，以及language=zh/en/any（缺省any）。
每条FAQ的`media_ids`最多4项；只能引用同登记知识库的素材，媒体语言必须匹配本轮语言。
素材ID与真实文件的对应、来源授权、画面准确性由内容审核确认，不从模型生成URL。

原GET返回既有image/video块，URL走同一媒体端点与短期签名。取图前后核对QA仍成功、未过期且
内容/发布绑定未变，视频沿用Range。没有素材仍交可靠文字。链接是短期持有凭据，可被转发，
已下载文件无法追回；只允许通用操作素材，不放个人订单或身份数据。旧手册/示意图须注明适用范围。

## 发布、试用、撤回和恢复

正式WF01使用既有管理路由表及Agent不可变版本；新增
`AIOPS_GATEWAY_CONSUMER_PUBLICATION_TARGETS_FILE`指向0600目标文件：

```json
{"schema":"aiops-consumer-targets/1","targets":[{
  "app_id":"11111111-2222-3333-4444-555555555555",
  "tenant_id":"TENANT-A","environment":"env-a",
  "content_file":"/private/reviewed-content.json","knowledge_base_ids":["reviewed-kb"],
  "live_order_status":true,"live_billing_record":false
}]}
```

应用须在既有dify_app_registry登记、工作区ready，已有发布Agent基线，且每应用Service key已准备。
WF01应用必须是advanced-chat；不能与节点目标或同应用旧个人状态路线并存，也不能同时保留文件执行绑定。
同租户另一应用的旧节点场景可以保留；发布WF01不会推进或删除它的版本。
目标不自动启用App。发布使用原`/v1/dify/publish`及现有运营按钮：冻结Dify图、完整审核内容及能力。
只改内容或能力也发新版本；相同图和内容不重复发布。运行只读版本里的内容，修改草稿文件不会切版。

原试用接口增加可选question（默认check order status），order_no仍是必须且按试用人范围授权。
WF01试用导入临时Chatflow、发布并执行，回调仅使用这次冻结内容，结束删除临时应用。
它不会增加正式版本或切换App；试用期间撤回使本地运行取消并中断上游，不交成功正文。
原撤回/版本列表/恢复入口按该应用真实场景定位；撤回后新请求与旧正文拒绝，恢复按旧版内容与图执行。
删除发布目标不自动撤回已发布路线；可继续使用原撤回入口，重新发布/试用则必须有目标。

## 图片输入（默认关闭）

文件执行登记或发布目标的`photo_enabled: true`允许照片，须使用含视觉步骤的最新DSL，
并将`__VISION_MODEL__`配置为已验证的视觉模型。普通问题仍只跑原知识步骤；试用暂不接照片。
`POST /v1/assistant/photos`沿用提问身份头，body为JPEG/PNG原始字节，Content-Type与格式一致，
成功201返回photo_id；原`POST /v1/assistant/questions`可携带photo_id。不可与order_no同时提交。
图片里的订单号不决定数据权限，实时查询需另行明确选单。

图片上限4MiB/1200万像素，规范化为最大边1600的JPEG并去元数据。主体最多2图/进程32图，
上传/排队/处理中均计数；未使用图5分钟过期，后台每30秒清理。ID一次性使用、绑定当前发布内容；
重启丢失，客户端重新选择。限额与内存存储仅适用于当前单进程部署，不是假定跨进程共享。
识图结果仅可提供readable/text线索；看不清时要求重拍或输入文字，不检索也不推测设备状态。
识图线索以JSON文本放在text/plain请求体送入回调，不放URL；用户照片不进入后续Dify对话。

Gateway不保存原图磁盘副本，Dify按平台策略保存上传文件与运行输入；Gateway过期清理不等于
上游副本删除。现役Dify保留策略及真实App图片验收尚未完成，不能据此开放生产图片能力。

最新真实依赖与App候选结果见[整合验收](../../validation/2026-10-25-consumer-integrated-acceptance.md)。

## 普通文本语义路由

WF01普通文本先用现有provider做一次无工具的结构化分类，区分本人状态、账务记录、知识指导、
人工、安全、写请求、无法核实的实时信息与澄清。订单指代仅为来源标签，实际单号取自原文、
页面选择、可信前轮或本人最新单SQL；多单/不一致先澄清，授权在取数和交付阶段仍分别检查。
模型失败返回`CONSUMER_INTENT_UNAVAILABLE`，提示尚未查单。原POST/GET和blocks契约不变，
但POST增加一次模型延迟；1024输出token预算，HTTP操作超时20秒，无应用重试。
危险/人工快速提示仍不依赖模型；该改动不覆盖图片识别、未解决反馈规则或未启用WF01的旧路由。

前轮过期后的明确新问题可继续，旧GET仍410；“那笔呢”等依赖失效前文的请求需要重述。
失败/取消/换版仍拒绝续用。协议与取舍见[ADR-0149](../../adr/0149-consumer-semantic-routing.md)，
回归步骤见[验收计划](qa-plan.md)。线上效果需合并部署后从原App另验。

## 知识与CPMS独立节点

新版模板将取数拆为三个真实HTTP节点：知识检索、CPMS本人订单状态、CPMS本人订单账务记录。
Gateway注入data_kind=knowledge/status/billing，图中“本轮取数能力”直接分支；这里没有第二次意图分类。
状态/账务节点调用时才读CPMS，知识路径不读业务；模型只见本轮裁剪结果，原App无需更新。

运营在Dify右上角环境变量面板修改`cpms_status_enabled`或`cpms_billing_enabled`（字符串true/false）。
值为false时对应能力关闭，true仍受服务器现有授权上限约束。节点提示词和布局可改；
URL/凭据引用/分支契约不可绕过，删除必需节点会在“发布到AI-Ops”前显示中文拒绝原因。
保存草稿只影响试用，发布才切App。试用使用已选订单的既有运营权限；“最新单”不替代选定订单。
关能力的试用会明确失败并说明未开通，不发生业务读取；显式恢复历史版本可重新开启。

新版回调为`POST /v1/internal/consumer-data/{knowledge|status|billing}`，Authorization直接绑定data_grant。
状态/账务不接收请求体单号/租户/用户。每轮只兑现自己的能力，重复调用只读同轮缓存，
取消/过期/撤权/换发布版仍拒绝。`callback_version=2`在发布时冻结，旧版本默认1并保留旧指纹。

迁移先保留源草稿与已发布DSL，用`tools/upgrade_consumer_flow.py <源草稿.yml> --gateway <可信网关根地址>
--out <新草稿.yml>`离线生成候选；保留源模型、提示词、应用特性和图片关闭设置。原图有自定义节点时
应人工核对差异；工具只支持已登记WF01结构，不发布也不覆盖原文件。核对后按[发布手册](live-release.md)迁移。
契约依据见[ADR-0150](../../adr/0150-consumer-cpms-workflow-nodes.md)。

2026-10-27：独立CPMS节点已在原WF01应用发布V4（revision3）。三个真实取数节点、能力开关、
真实试用及原H5三条路径已核验；见[正式交付记录](../../validation/2026-10-27-consumer-cpms-production.md)。

## 在知识节点换库

选库节点部署后，打开“检索当前用户适用知识”，在右侧“检索知识库”多选框按名称选本租户RAGFlow库。
保存后先试用，确认答案与引用，再点“发布到AI-Ops”；正式App随后使用新库。只保存草稿不影响用户，
恢复历史版本也恢复当时的选库。库列表为空/库被删除/不属于当前租户时明确拒绝，不退回服务器旧库。

新版本的knowledge_base_ids来自节点常量，服务器publication-targets.json中的同名字段只供旧版本兼容，
日常换库无需再编辑该文件。模型不决定库、租户或订单；本轮授权与图片线索两项保留迁移工具生成的引用。
知识库参数在节点的“设置”区，是多选常量；两个授权/线索输入不需要运营改写。

本轮继续只交付已审核FAQ。新库文档应带现有审核条目的KB-C-NNN标识；没有审核对应项时转澄清/人工指引，
不会直接采用召回文档自由编写答案。安装和迁移见[部署手顺](knowledge-picker-deploy.md)，取舍见[ADR-0151](../../adr/0151-consumer-knowledge-node-selection.md)。
