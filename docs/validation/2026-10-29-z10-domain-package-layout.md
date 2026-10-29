# 领域目录归位

基线`4ab5bf3a`，分支`refactor/domain-package-layout`。12模块分为operator、health、data，25测试随职责归类。
测试operator目录与标准库冲突导致首次收集失败，改为tests/operations后专项342 passed / 2既有条件跳过。
全量首次4项失败：3处文件清单遗留旧路径，1处README在并行记录收尾时暂时还原；修正后路径/架构专项99 passed / 1既有跳过。
12模块去除导入和文档后的业务AST一致；wheel/sdist构建、镜像新模块导入和旧模块缺失检查通过，内容锁一致。
临时Gateway真进程健康/鉴权/旧路由404检查通过，ruff与policy通过，spex 0错误/49既有警告。
全量3320 passed / 9既有条件跳过；远端CI、CD与原双端H5待验证，未完成本片业务验收。
本片仅调整位置及调用引用，没有新增业务分支；未追加复杂度或变异测试。管家输出格式缺陷已由独立PR #1149修复并在#1152回执验收；长期稳定性仍未验收。

恢复交付：整合`363c82d8`自助开通及输出修复，保留最新业务实现；两个新增自助调用及测试导入同步新路径。
当前12模块业务AST与新主线一致，真实Java/Gateway 12场景通过，自助测试24项通过；重建镜像及临时Gateway通过。

## 接到最新主线后复验（2026-10-29，基线 `8acc9045`）

rebase 到 `8acc9045`（含 #1153 退役残留清理、#1155–#1157 自助开通与受管试用修复）。两处冲突按「保留主线业务、采用本片路径」解决：
README 模块表用新路径并补 `operator_conversation.py`；`images.lock.json` 保留主线构建身份后按新源码摘要重建。

- 全量：**3406 passed，0 failed，9 skipped**（junit 计数）。
- 目标修复：`tests/test_operator_conversation.py` 两处旧导入（`test_operator_cpms` → `operations.test_operator_cpms`；
  `aiops_diagnostics.operator_cpms` → `aiops_diagnostics.operator.operator_cpms`）；README 补 `operator_conversation`。
- 镜像 `aiops-m2-live-03-gateway:domain-rebased2`：12 个新模块可导入，11 个旧包根模块全部 ModuleNotFound，内容锁 `0f0adbf3`。
- `uv build`、临时 Gateway（健康/鉴权/退役路由 404）、ruff/format、policy、ADR 均通过。
- 仍未完成：远端 CI、CD 部署、原双端 H5 业务回归。
