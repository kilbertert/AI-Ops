# WF01 C端只读客服

更新：2026-10-28。原H5已投入使用并提测，当前WF01 V8/revision7；状态与证据见
[当前交付](../current-delivery-state.md)、[真实验收](../../validation/2026-10-28-consumer-multilingual_02-production.md)。
下文描述当前动态文档模式；旧审核包仅在文末作为显式回滚兼容说明。

## 知识维护与媒体

把资料放进Chatflow“检索当前用户适用知识”节点所选的知识库，解析/索引完成后即可供新问题检索。
更新/删除资料影响新问题，不需要KB编号、approved字段、服务器content.json或媒体挂载表。
知识库内容不是流程指令；模型依据本轮来源组织回答，不用自身记忆补费用、承诺或实时订单事实。

视频链接直接写在相关说明正文中；目前解析具体YouTube视频地址及知识服务返回的图片/视频资源。
来源与媒体必须来自本轮引用，模型不能编造链接。外部播放器网络、地区限制与连续播放须另验。
不应把个人订单或凭据写进通用知识资料；已交付数据或下载媒体不能追回。
日常导入见[内容导入](content-import.md)，一次迁移见[动态文档部署](live-knowledge-deploy.md)。

## 节点选库

在Dify节点配置中使用动态多选。目录凭据仅列出当前工作区可用库；它不能代替单轮取数授权。
保存草稿不换正式库，试用读取草稿所选库，发布后新问题才采用新选择；恢复旧版恢复该版所选库。
库内资料变动不推进工作流版本。原publication-targets登记保留环境/应用边界，不再固定最终选库。
见[选库部署与权限](knowledge-picker-deploy.md)、[ADR-0151](../../adr/0151-consumer-knowledge-node-selection.md)
及已替代其审核限制的[ADR-0152](../../adr/0152-knowledge-documents-are-the-live-answer-source.md)。

## 实时查询与语义分流

普通文字先做一次结构化语义分类：状态、账务、知识、人工、安全、写请求、无法核实的实时需求或澄清。
分类器只能返回能力/订单指代/是否依赖前文，实际身份与单号由Gateway确定；多单或不一致先澄清。
当前分类预算1024输出token、HTTP操作超时20秒，无应用自动重试；不把该超时当整个POST的硬截止。
危险/人工的既有明确中英文快速提示不等待模型，其他自然表达依赖语义模型，不能承诺全语言离线识险。
供应商故障返回CONSUMER_INTENT_UNAVAILABLE，不回落关键词或旧答案。见[ADR-0149](../../adr/0149-consumer-semantic-routing.md)。

| 分支 | Dify取数节点 | 交付边界 |
|---|---|---|
| 知识 | consumer_knowledge插件 | 节点所选库的当轮来源；kind/answer/reference_ids结构化回答 |
| 本人状态 | cpms_status HTTP | 同轮CPMS充电状态、来源、UTC观察时间；不等于故障根因 |
| 本人账务 | cpms_billing HTTP | 原金额及退款记录；缺失不当零，不猜币种、不等于扣款或银行到账 |

live_order_status/live_billing_record分别随发布版控制。运营在Dify环境变量中配置
cpms_status_enabled/cpms_billing_enabled（字符串true/false）；保存只影响草稿试用，发布才影响App。
URL、凭据引用与能力分支是发布契约，删除必需节点或改错地址会拒绝发布。
最新/指定/可信前轮订单都需本人授权。
HTTP节点凭data_grant回调精确的`/v1/internal/consumer-data/{knowledge,status,billing}`；
旧回调`/v1/internal/consumer-knowledge`继续兼容。模型不持公司凭据，也不选择回调地址/租户。
回调读前读后、worker写前及历史回读核对授权、绑定、取消和TTL。失败不换引擎或直读。
本退役分支将 C端“订单检测”快捷按钮接入 WF01 本人状态分支，不再启动 Python 根因诊断；
生产启用以本分支部署证据为准。

## 语言与会话

App的Accept-Language决定回答语言，即使问题/来源是其他语言。现役H5支持简体、英文、葡萄牙语、
西班牙语、泰语、越南语、蒙古语、高棉语、繁体；后端另支持德/法语，但本轮未在该H5点选。
Dify输入枚举与模型提示一致，状态/账务/安全/人工等固定文案按语言渲染。模型正文过语言守卫，
精确引用的原文件名可保留；只有文件名不构成答案。字符检查不保证全部外语语义正确。

首次POST返回Gateway conversation_id，后续普通问题原样携带；不是Dify会话ID。
过期快照不再用于上下文，但明确的新问题可以重新查询；依赖失效前文需重述，旧GET仍拒绝。
失败/取消/换发布版需新会话；历史保留当轮正文与实际语言，不因GET的语言头重写。
实时轮次及下一轮不复用Dify上游记忆；知识续问也须过当前绑定和有效期检查。

第一次明确未解决反馈追问步骤，重复反馈提供人工指引；目前相关快捷规则覆盖显式中英文表达。
人工按钮进入原联系页，配置为空不声称已接通。未知实时能力、退款或停止充电执行请求不交给模型代办。

## 发布、试用、撤回和恢复

正式路由使用agents.published_version、不可变agent_versions及chat_route_bindings。
应用为advanced-chat；同一路由不允许文件绑定与管理表并存，旧独立节点场景可以保留恢复。
部署配置默认关闭，现役启用状态以交付索引为准。应用环境/租户来自服务端登记，不能由模型传入。

`AIOPS_GATEWAY_CONSUMER_PUBLICATION_TARGETS_FILE`保留目标登记；动态模式不需要content_file。
兼容字段knowledge_base_ids必须合法，当前选库从节点配置校验并冻结。受控配置示例：

```json
{"schema":"aiops-consumer-targets/1","targets":[{
  "app_id":"11111111-2222-3333-4444-555555555555",
  "tenant_id":"TENANT-A","environment":"env-a",
  "knowledge_base_ids":["owned-kb"],"live_order_status":true,"live_billing_record":false
}]}
```

工作区ready、应用登记和每应用Service key是前置。启用consumer_chatflow_enabled及HTTPS执行地址后，
原发布API冻结图、库选择和能力，库内正文仍实时检索；同配置重复发布不建新版本。
试用导入临时应用并真跑草稿，不修改正式指针；结束删除临时应用，用户数据保留范围按Dify策略另验。
撤回终止后续交付且保留行，恢复使用选定历史版本；删除目标文件不等于撤回已发布路线。
步骤见[正式交付](live-release.md)。

## 图片输入（现役关闭）

photo_enabled只在完成保留/清理验收后启用。已有实现可上传JPEG/PNG，去元数据、有大小/像素/数量/TTL限制，
photo_id一次性绑定主体与发布内容，不能与显式订单同时使用。识图只提供线索，不授予订单权限。
当前实现上限4MiB/1200万像素，规范化至最大边1600；每主体2图、进程32图，未使用5分钟过期。
这些是单进程限制；图片重启丢失，不能据此宣称跨进程持久化。Gateway内存清理不等于Dify上游文件被删除；原生文件读取、APK/IPA和完整真机验收仍未完成。

## 验证与旧版兼容

复跑入口：`uv run python tools/verify_consumer_chatflow.py --out <私有报告>`；
选库/当前图需配套签名插件、公钥和DIFY_TEST_WEB_IMAGE，见[迁移手册](live-knowledge-deploy.md)。
详细用例见[QA计划](qa-plan.md)与[测试交接](../../testing-handoff.md)。隔离成功不能替代真实供应商/业务/H5证据。

旧callback_version 1–3读取aiops-consumer-reviewed/1审核包，kind/faq_id只选标准答案，
entries.answers.zh/en及media_ids属于旧协议。`tools/build_consumer_knowledge.py`保留编译历史材料，
历史包可用`uv run python tools/build_consumer_knowledge.py <私有原包> --output <新私有目录>`重建，输出仍为草稿。
新版callback_version=4/documents不使用这些准入规则，不要求产品维护两个副本。
旧正文按实际中英文标记、旧实时快照按原模板核验，不改写已有QA。回滚可恢复旧版本，不能自动故障回落。
