# Dify 预览连接故障与部署预检

目标：修复原Dify“天河有站点吗？”在运行创建前失败，并阻止非法控制台配置再次通过部署。
分支 `fix/dify-console-preflight`，基线 `481f81e6401cf12b3eb0430558f5635bb22e8903`。

已完成：原生页面两次复现HTTP400/OPERATOR_PREVIEW_INVALID；单变量对照确定远端HTTP
控制台地址触发已有安全规则。新增启动校验和部署前候选客户端检查、拒绝分类及回归测试；
夹具改为安全地址，沿用现有隧道的配置恢复方法已写入[手册](../agents/operator-cpms/dify-console-preflight.md)。
版本预留1.0.23；本片不改工具权限、模型、正式发布指针或业务数据。

验证结果见[验收记录](../validation/2026-11-04-dify-console-preflight.md)。
下一步：必需CI、审查闭环、合并部署与原入口复测；完成前不标记线上恢复。
