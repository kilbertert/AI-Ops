# 领域目录归位

基线`4ab5bf3a`，分支`refactor/domain-package-layout`。12模块分为operator、health、data，25测试随职责归类。
测试operator目录与标准库冲突导致首次收集失败，改为tests/operations后专项342 passed / 2既有条件跳过。
全量首次4项失败：3处文件清单遗留旧路径，1处README在并行记录收尾时暂时还原；修正后路径/架构专项99 passed / 1既有跳过。
12模块去除导入和文档后的业务AST一致；wheel/sdist构建、镜像新模块导入和旧模块缺失检查通过，内容锁一致。
临时Gateway真进程健康/鉴权/旧路由404检查通过，ruff与policy通过，spex 0错误/49既有警告。
全量3320 passed / 9既有条件跳过；远端CI、CD与原双端H5待验证，未完成本片业务验收。
本片仅调整位置及调用引用，没有新增业务分支；未追加复杂度或变异测试。管家既有间歇完成失败根因尚未关闭。
