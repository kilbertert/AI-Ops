# #1463 历史默认值归一：盘点关闭

目标：关闭 #1448 延期的「旧库 scope_json 默认值与 fresh 完全归一」。

完成内容：经全舰队可达库只读盘点，现役生产库与全部可达副本的
`chat_dify_runs.scope_json`/`agent_runs.scope_json` 均无 `DEFAULT ''`，
已与 fresh 逐字一致；实施对象不存在，按用户选定方案A以盘点证据关闭，不做结构改动。

当前提交：本 PR 候选（`docs/schema-legacy-default-inventory` 分支），只改文档。

验证结果：`tests/test_repository_layout.py` 19 通过、
`tests/test_readme_architecture.py` 6 通过，无失败与修复；
详细盘点表、目标语义与残留决定见同名 validation。**未完成业务验收**。

已知缺口：root 独占的 `gateway.db.bak-prompt-20260914` 服务身份不可读、未盘点；
不评价业务数据内容。

下一步：将来若某库经补列路径携带该默认并须归一，另开受控重建任务。
