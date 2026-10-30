# 双端依赖修复验证

基线f9865f17，fix/dual-entry-dependencies。Dify规范来源为固定上游
langgenius/dify@8387590ace4a094de812b7847fc6a4c3a27cd52b：
`api/controllers/console/workspace/tool_providers.py`及`services/tools/builtin_tools_manage_service.py`。
用其顶层列表、provider路径、api-key、掩码与默认激活形状构造契约替身。

新工作区安装模型并激活、同provider不同模型拒绝、丢失凭据添加响应后不重复、复制去除源凭据，
状态/账务实际读取函数只登记受管连接即可调用，换环境拒绝。依赖/开通73通过；
受管连接模块覆盖率97%，准备模块78%，含分支总82%。第一轮全量3663/9跳过，
最终微调后专项67通过；临时Gateway HTTP检查通过。构建通过，远端CI待验证。

41只读health仍1.0.0+b0d0bd3f392c，business_mutations=disabled。配置实读缺知识插件和
渠道文件，业务连接仍只含原租户。未改变生产；**未完成业务验收**。

证据在本机私有cache的aiops-dify-self-service-research/direct-takeover/dependencies-*。
操作方法已同步self-service.md；凭据内容未写入记录。4个定向变异（错误provider、不激活模型、错环境、源凭据残留）全部被测试杀死；最终CI待验证。

补充现场证据：客户端现役使用 deepseek-v4-flash，管家profile使用 deepseek-v4-1-flash-261017。
新增profile.consumer_models显式批准客户端模型列表，逐项验证私有凭据/预算及重复身份；
模型/自助管理47项通过。不是把管家模型名覆盖掉，也不开放provider下任意模型。
