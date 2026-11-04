# Dify工具提供方更新验证

真实迁移返回422，缺credentials、icon、original_provider；未修改正式应用版本。
对照Dify1.17.1官方载荷，回归先因original_provider缺失失败，修复后39项通过，Ruff通过。
迁移保持auth_type=none、原图标/标签/说明；必须收到success才报告迁移完成。
无新增Gateway公开接口；本片验证为管理客户端载荷，不重复整站HTTP验收。
未完成业务验收：真实迁移与计费问答在合并部署后继续。
