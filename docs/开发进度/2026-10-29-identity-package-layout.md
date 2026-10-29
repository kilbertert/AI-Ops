# 目录迁移第五片：身份与范围归包

基线`2662c3fa`，实施分支`refactor/identity-package-layout`，交付分支`refactor/identity-package-delivery`。统一迁移7身份模块、11直接相关测试，
C端与管家共用同一权限实现；http_auth仍为独立Dify桥提供公共头操作，不牵动其打包。
专项399项、业务AST对照、wheel/镜像导入、临时Gateway与静态检查通过；全量3319 passed / 9 skipped，PR/CD和真实跨主体回归待做。当前未完成业务验收。
后续按发布、Dify、Gateway等职责继续归类；旧业务行为和异常处理保持不变。
