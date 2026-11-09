# 客服编辑保存后发布失败

目标：解决原Dify受管发布500，并避免正常编辑清空授权。
分支fix/consumer-publish-roundtrip，基线fba98028，版本1.2.8。
已定位：query序列化被误拒、工具表单挂载覆盖grant、ConsumerFlowError漏映射。
已修复并跑107Python、表单/管理面浏览器与隔离Gateway；真实表单旧红新绿。
独立Hack、完整镜像、PR/CI/部署、原页面修复与发布待完成。
保留旧正式版和用户当前业务指令/模型/选库；未完成业务验收。
