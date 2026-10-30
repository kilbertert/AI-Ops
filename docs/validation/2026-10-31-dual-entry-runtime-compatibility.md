# 执行器兼容性验证

基线f9865f17，fix/dual-entry-runtime-compatibility。复用现有执行清单schema、request-target/1
工具契约与SHA-256字节身份；Spring @Scheduled每30秒报告，Gateway当前节点凭据定环境/节点。
未引入新的业务执行权限或公网入口。

L2/L3验证：过旧schema、工具摘要不同、心跳过期时发布/恢复均拒绝且正式指针不变；
正确报告重试成功，伪造节点令牌403。管理/双端/目录88项通过，2个定向变异被杀死。
Maven编译打包通过（该模块原无JUnit测试，不能称Java单测通过）。实际完整Java启动4秒内
向合成HTTP接收器报告，schema/tool_contract/tools_sha256一致；容器和回环服务器已清理。
首次禁用Snail启动失败依赖Bean缺失，已修探针并保留失败日志。

构建、测试及HTTP记录在私有cache aiops-dify-self-service-research/direct-takeover/runtime-compat-*。
源码验证脚本已入tools/verification/verify_node_capabilities.py；尚未部署，**未完成业务验收**。

最终Java jar已按源码重建，实际HTTP报告通过；Gateway镜像构建/源码锁及临时Gateway通过。
节点锁与HOST-CHANGE已更新为本轮实际jar，线上部署待合并。
