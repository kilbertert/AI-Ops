# 发布管理Python包迁移

基线`f17fd3cc`，分支`refactor/publication-package-layout`。7模块归入publication包，
7个直接相关测试归入tests/publication；模块为Agent内容/生命周期/清单/发布前检查、快捷生命周期/迁移和客服元数据选择。
Dify控制台协议、节点执行与通用HTTP辅助函数不在本片移动，不保留旧模块壳。

初次专项399通过/1失败：移动测试后夹具相对路径仍指向旧深度，已修复。复验400 passed。七模块业务AST一致，wheel/镜像新包导入与旧路径缺失、临时Gateway通过；
镜像162文件内容锁一致，ruff/policy通过，spex 0错误/50既有覆盖或漂移警告。全量与CD待记录。
本片未完成业务验收，需部署后原H5回归；不改生产发布指针或权限数据造样本。
