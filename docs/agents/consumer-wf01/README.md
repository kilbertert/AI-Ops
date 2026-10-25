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
