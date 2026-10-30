# 真实工作流页验证

固定Dify 1.17.1上游8387590ace4a094de812b7847fc6a4c3a27cd52b，AI-Ops基线f9865f17。
专用构建树在任务工作区的deploy/dify-36/web-patches/work，没有改共享上游工作区。
全部补丁fuzz=0应用；原生FeaturesTrigger组件29项通过；浏览器15项覆盖双端开通、保存后摘要、
并发编辑拒绝、预览先保存/停止、客户知识授权。组件替身不代表真实公司与Dify服务通过。

webpack全量编译多次OOM，4GB堆明确超限；限制工作线程并关闭持久缓存后完整Next构建退出0，
含standalone/server.js。相同配置已固化到构建脚本。知识授权面板增补后最终重建退出0，完整standalone已生成。
vp lint通过；tsc全量仅报上游dev-proxy.config.ts缺@langgenius/dev-proxy，未宣称类型全通过。
vp原生测试入口避免重复vitest runner；真实页面登录及使用仍待同版部署。

**未完成业务验收**。本机私有证据：aiops-dify-self-service-research/direct-takeover/workflow-*。

文档/目录25项通过；最终浏览器15项，真实工作流发布组件29项，vp lint无告警。
