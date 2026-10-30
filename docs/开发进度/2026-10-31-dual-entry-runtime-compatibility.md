# 双端执行组件兼容性门

fix/dual-entry-runtime-compatibility，基线f9865f17。实际Java节点以自身凭据报告协议与加载工具摘要，
Gateway按目标节点/环境记录并限制120秒有效期。管家开通/发布/恢复前检查，不兼容不推进指针。
现役路由不自动撤回。新增精确内部回调，部署需要Gateway/回调/Java成套更新。

88项回归通过，2个定向变异被杀死；Java构建通过并实际启动发送HTTP报告，协议/摘要与源码一致。
首次关闭Snail导致依赖Bean缺失；改为合成配置启用starter后通过，未访问生产或模型。
全量、CI和真实节点部署待验证，未完成业务验收。

最终Java jar已按源码重建，实际HTTP报告通过；Gateway镜像构建/源码锁及临时Gateway通过。
节点锁与HOST-CHANGE已更新为本轮实际jar，线上部署待合并。
