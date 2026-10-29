# 目录迁移第六片：发布管理归包

基线`f17fd3cc`，分支`refactor/publication-package-layout`。7个发布/内容模块和7个测试归位，
同步调用者、工具、monkeypatch与规范导航，异常处理和版本规则不变。
专项夹具路径遗漏已修复，400项及业务AST、wheel/镜像/临时Gateway/静态检查通过；全量与部署证据待填写；未完成业务验收。
后续整理节点执行、Dify集成与Gateway装配；既有循环依赖不会借路径移动隐藏。
