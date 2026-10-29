# 优链充租户真实操作与预览请求修复

分支`fix/dify-self-service-acceptance`，基线`db6bda19`。
正常公司账号登录公网Dify：优链充租户，platform=false；自己工作区可见、readiness=true。
原生Duplicate创建“优链充－自助开通验收”（`65255300-e932-4589-bf06-4bb8ba7598cd`），
自动受管登记并显示尚未发布，原正式V2/revision1保持不变。这是真实租户内复制验证，非平台跨租户开通。

从同账号正常公司登录后的授权订单列表取得明确订单；原生预览实际失败为
`unexpected field in request body`。抓到字段为response_mode=streaming、conversation_id=""、files=[]、inputs={}，
对应固定Dify useChat源码，桥尚未调用Gateway trial。凭据与订单明细只在私有证据中保留。

修复：接收已知空聊天外壳后剥离，四类非空/非streaming请求拒绝；Java预算未声明时显示模型默认。
本地60项HTTP/开通相关回归、4项Chromium组件通过；最终生产预览、平台跨租户开通仍待验证。
**未完成业务验收**，没有把配置成功或站点权限检查当作真实报告通过。

补充验证：临时Gateway8项通过、Webpack完整构建通过；2个空外壳校验变异被测试检出。
最终全量3350 passed / 9 skipped；ruff、policy、内容锁一致。远端CI与修复上线待验证。
