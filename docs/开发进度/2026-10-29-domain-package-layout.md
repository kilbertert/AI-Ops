# 目录迁移第九片：领域模块收口

基线`4ab5bf3a`，分支`refactor/domain-package-layout`。12个管家/健康/数据模块和25个测试归类，旧根模块直接移除。
专项342 passed / 2既有跳过，旧路径修正复测99 passed / 1既有跳过；tests/operations避免标准库同名冲突。
业务AST一致，wheel/sdist、镜像、临时Gateway与静态检查通过；全量3320 passed / 9既有条件跳过，CI/CD与真实回归待完成。
未完成本片业务验收；下一片共享基础与Gateway归位，公开命令入口保持稳定。
