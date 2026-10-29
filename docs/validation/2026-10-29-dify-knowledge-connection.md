# 知识连接工作区隔离修复

基线f906f337，分支fix/dify-knowledge-connection。
真实平台已成功进入优链充RAGFlow；原开通任务knowledge_ready=true，模型验证完成，app/workspace不变。
页面新建测试库c8eec310d38a11f1a026f1f706dc4043，上传265字节测试文档，解析DONE、progress=1、1切片，
RAGFlow检索命中YLC-KB-20261029-7QX。Dify列表未出现，内部sync回skipped，日志为ConsoleApiError。
回读Dify资源：唯一配置模板属于Ulink，优链充连接列表0条。同步错误是把全局UUID用于另一工作区。
本片让开户和同步创建/回读本工作区连接，保持旧工作区模板。
专项42项通过；最终全量与真实Dify同步待后续记录。未完成全链路业务验收，不将RAGFlow检索等同Dify通过。

最终本地3403 passed/9 skipped，临时Gateway8项、连接4项通过；新模块75%语句/分支覆盖，
工作区身份反向变异被测试检出。旧Ulink归档连接有历史密文解密失败，单读配置中的有效连接成功；
因此先按明确UUID取旧连接，新工作区404/400后查询创建自己的连接，不让无关归档条目拖垮旧正常路径。
