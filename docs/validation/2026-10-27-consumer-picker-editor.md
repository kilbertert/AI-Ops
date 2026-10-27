# 知识节点editor权限回归

日期：2026-10-27；基线9a25424f，分支fix/consumer-picker-editor。

## 复现

现役dynamic-options多次403；截图账号在该工作区为editor。原版Dify1.17.1容器运行
`tools/verify_dify_knowledge_role.py`的editor场景，原权限装饰器抛出与用户相同的英文Forbidden，目录调用0次。
测试仅替代已登录身份、启动/CSRF前提和目录适配器，角色判断及完整端点装饰链使用原Dify代码；不声称真人登录。

## 修复验证

- 13组角色/请求矩阵：owner/admin/editor可读本节点；normal/dataset_operator、错误plugin/provider/action/parameter/type、
  私有或跨工作区凭据拒绝；企业RBAC维持原管理权限。只通过Dify既有凭据可见性列表选取凭据，不回传凭据。
- 真Dify/Gateway加浏览器95/95：测试账号临时降为editor后点选B、保存200并后端回读；管理API阶段恢复隔离账号owner，
  试用B、发布B、恢复A全部通过。这里的owner恢复仅限一次性测试栈，生产账号角色未修改。
- 首轮editor浏览器已通过，但后续管理API因隔离栈没有owner而401；修正测试角色恢复步骤，不改生产授权判据。
- 全量首轮4121 passed/9 skipped/1 failed；失败只因tools/m2_close参与镜像内容锁，按标准构建更新。
  角色/部署集合8 passed；最终含内容锁集合25 passed。角色、限定工具身份、凭据选择三组变异均由断言检出。原权限报错的针对性回归保留为必需检查。
- 原镜像文件SHA核验、完整补丁apply和ruff/format/policy/repo-map通过。生产尚未应用，不宣称用户当前错误已消失。

**未完成业务验收**：本片是编排编辑权限修复，不证明故障诊断准确率；上线后用户个人会话点选仍需复验。
