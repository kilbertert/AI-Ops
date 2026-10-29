# 目录迁移第七片：执行委派归包

基线`75cf1d68`，分支`refactor/execution-package-layout`。6执行模块和9测试归位，
保持路由、授权、版本清单和完成通知逻辑，原错误处理不变；测试和部署引用同步。
专项647项、AST一致、真Java整链18项、wheel/镜像/Gateway通过；全量3319 passed / 9 skipped，CI/生产回归待完成，当前未完成业务验收。
后续整理Dify集成和Gateway；现有装配循环另行拆解，不借目录移动扩展例外。
