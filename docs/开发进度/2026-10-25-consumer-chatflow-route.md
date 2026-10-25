# WF01 原App接口接入Chatflow会话

基线1116e504，分支feat/consumer-chatflow-route。
已接原提问/轮询/取消，服务端创建并绑定用户会话，保留上游conversation/message/run关联与绑定摘要。
无登记或开关关闭保持原行为。取消、失败、过期、换版后不交付旧正文，QA与会话详情共用守卫。

真Gateway/真Dify三轮整链通过；模型/身份为合成，原App页面尚未接会话字段。
下一片接知识/安全/人工业务分支及媒体，再接客户端。未完成业务验收，生产未启用WF01。
