# 知识授权管理验证

基线f9865f17，fix/dual-entry-knowledge-access。临时Gateway的管理HTTP验证：默认内部、
显式允许、显式撤销，数据库回读一致；其他客户库403，注入tenant_id 422，其他租户管理者403。
接口暴露登记新增两项。真实页面与客户资料尚未验收，**未完成业务验收**。
证据位于本机私有cache aiops-dify-self-service-research/direct-takeover/knowledge-management-*。

知识授权、现役撤销、暴露登记及管理回归49项通过；2个定向变异（跳过归属、不撤授权）被杀死。
真实临时Gateway检查和镜像构建通过。页面在workflow-ui分支验证，待合并与真实验收。
