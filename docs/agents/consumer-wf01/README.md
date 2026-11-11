# WF01 C端只读客服

更新：2026-11-05。现役客服为callback_version6，1.2.5已完成原Dify与安卓单轮复验；
原H5连续问答与发布恢复已有同轮证据。Ulink正式Agent v21/revision25、优链充v2/revision2，
具体时点见[生产验收](../../validation/2026-11-05-consumer-agent-production-closeout.md)。
本页先说明现役合同，旧callback1–5仅供恢复解释；不能把旧分类器当成新Agent的前置步骤。

## 新版 Agent 会话（callback_version 6）

新发布合同将流程收敛为开始 → Dify 官方 Function Calling Agent → 回复。
自然语言直接进入 Agent；模型按需组合本人最近订单、状态、账务、知识检索和人工指引，
Gateway 不再先分类成互斥能力，也不覆盖“还是不行”等反馈。最终正文自然表达，
工具参数/证据仍结构化，来源链接和媒体只来自本轮记录。最终正文取最后成功无工具轮次，不能拼接Agent累计text中的工具过程。

会话使用原 conversation_id，保存有界的已完成问题与实际答案（当前最近6回合并受字符上限约束）；每次进入模型前和回读时重新核验
其中业务对象与知识授权。180秒仍限制本轮运行与取数凭据，不再让历史问答随之失忆。
历史结果标明不是当前状态，实时追问重新读。最多8次工具调用，失败与并发都计数；
取消、结束或撤权后不能继续取数。未调用工具的澄清也可以完成，不要求虚构来源。

首次迁移保持原模型、业务提示词与已授权库，删除系统最终JSON/事实段协议；
已有callback 1–5保留其原合同用于显式恢复。照片输入本轮未迁移，默认关闭。
方法见[Agent迁移与验收](agent-conversation-qa.md)。本节合同已部署，真实覆盖范围以本轮回执为准。

安卓1.2.15/code24的实际安装包没有conversation_id传递；公司chargeAbroadNew源码已有修复，
用户确认当前没有可用新包，发包由其他同事负责。#1353保持等待发包，不算连续会话验收通过。

## 知识维护与媒体

把资料放进客服Agent的受控工具配置所选知识库（旧图为“检索当前用户适用知识”节点），解析/索引完成后即可供新问题检索。
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

## 现役取数、语言与会话

官方Agent按需组合知识、本人最近订单、状态、账务和联系指引，回调统一为
`POST /v1/internal/consumer-data/agent`。模型提供resource/query/order_no，发布图固定所选库；
Gateway校验grant、主体、订单、图版本、取消及预算，取数前后和交付时均检查。
状态/账务仍为记录，不代表故障根因或支付渠道到账凭证；缺少币种和差额成因不能猜。

1.7.1在原billing中补充订单摘要、完整费用白名单及有界分时记录，详情与费用问题据此回答；
零、缺项、无效值和截断分开说明，分时电量不作为独立计量证据。现役v6发布图还须精确更新指令块，
保留运营前后文本。1.7.2将事实约束v2放到全文末尾，明确缺币种/缺项与禁止模型自行合计、
未核验安抚结论；1.7.3的v3要求开头概括与缺项一致，1.7.4的v4已部署并发布到两张客服图，补充跨语言逐字段区分零收费与缺项。
方法与字段映射见[订单构成QA](order-composition-qa.md)，双租户发布与原H5四问的费用字段核验见
[交付回执](../../validation/2026-11-11-consumer-composition-live-receipt.md)。最后一轮请求en但正文中文，
语言跟随与全面语言质量未验收；本次不证明计费正确性或支付渠道到账。

App的Accept-Language作为回答语言输入，即使提问是另一语言；语义质量需实际审校。
首次POST返回Gateway conversation_id，后续普通问题原样携带；这不是Dify会话ID。
已完成历史不因180秒取数凭据到期而消失；当前事实需重新读取。发布改变、撤权或失效历史
可以要求新建会话，不能用旧正文授权新取数。原Dify预览按运营公司范围仅查明确选定订单，
正式消费者仍仅本人；预览不能列管理员最近订单。

知识/模型不可用时如实报告或澄清，不回落旧FAQ或另一个引擎。人工指引只证明联系入口，
实际接通、图片输入和新APK连续会话仍按[测试交接](../../testing-handoff.md)分别判定。

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

工作区ready、应用登记和每应用Service key是前置。启用consumer_chatflow_enabled并配置执行地址后，
原发布API冻结图、库选择和能力，库内正文仍实时检索；同配置重复发布不建新版本。
试用导入临时应用并真跑草稿，不修改正式指针；结束删除临时应用，用户数据保留范围按Dify策略另验。
撤回终止后续交付且保留行，恢复使用选定历史版本；删除目标文件不等于撤回已发布路线。
跨主机直连使用HTTPS；已批准的受限SSH隧道可使用本机回环HTTP入口，跨主机段仍须加密，
应用密钥鉴权不变。连接核查见[数据面连接恢复](live-release.md#数据面连接恢复)，其余步骤见[正式交付](live-release.md)。

## 图片输入（现役关闭）

photo_enabled只在完成保留/清理验收后启用。已有实现可上传JPEG/PNG，去元数据、有大小/像素/数量/TTL限制，
photo_id一次性绑定主体与发布内容，不能与显式订单同时使用。识图只提供线索，不授予订单权限。
当前实现上限4MiB/1200万像素，规范化至最大边1600；每主体2图、进程32图，未使用5分钟过期。
这些是单进程限制；图片重启丢失，不能据此宣称跨进程持久化。Gateway内存清理不等于Dify上游文件被删除；原生文件读取、APK/IPA和完整真机验收仍未完成。

## 验证与旧版兼容（callback_version 1–5）

新版复跑入口见[Agent QA计划](agent-conversation-qa.md)，使用 `--agent-conversation` 及固定签名插件。
旧合同复跑入口：`uv run python tools/verification/verify_consumer_chatflow.py --out <私有报告>`；
选库/当前图需配套签名插件、公钥和DIFY_TEST_WEB_IMAGE，见[迁移手册](live-knowledge-deploy.md)。
详细用例见[QA计划](qa-plan.md)与[测试交接](../../testing-handoff.md)。隔离成功不能替代真实供应商/业务/H5证据。

旧1–5使用分类/固定分支或模板；当时的180秒历史限制不适用于callback6。
旧callback_version 1–3读取aiops-consumer-reviewed/1审核包，kind/faq_id只选标准答案，
entries.answers.zh/en及media_ids属于旧协议。`tools/content/build_consumer_knowledge.py`保留编译历史材料，
历史包可用`uv run python tools/content/build_consumer_knowledge.py <私有原包> --output <新私有目录>`重建，输出仍为草稿。
旧动态文档合同callback_version=4/documents不使用这些准入规则，不要求产品维护两个副本。
旧正文按实际中英文标记、旧实时快照按原模板核验，不改写已有QA。回滚可恢复旧版本，不能自动故障回落。
