# 退役工具残留清理

基线`363c82d8`，分支`chore/retired-tool-residue`。发现旧Jev校准工具仍导入已删除模块，
执行`--help`即ModuleNotFoundError；订单状态测试中还留有从未调用的旧engine辅助函数。
新增现役src/tools/tests的AST导入检查，先红并准确列出这两处残留；删除后退役/订单/Dify专项77 passed。
补齐27个已退役模块的安装缺失检查，修正Dify测试说明与历史Jev验收命令的适用范围。
ruff、policy及wheel/sdist构建通过，远端CI待验证。无生产源码/部署配置变动，无新增业务逻辑。
未执行生产操作；本片未完成业务验收，不改变既有真实验收结论。历史记录和生产数据保留。
