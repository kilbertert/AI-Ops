# #1463 历史库 scope_json 默认值归一：盘点验收

日期：2026-11-11 UTC。任务 #1463，Refs #1448/#1462。
判据来自 issue 验收条款：实施前给出真实结构/数据盘点、目标语义、受控验收与授权；
用户已选定盘点即验收、不做结构改动（方案A）。本文不连接生产写入，全部为只读证据。

## 结论

`chat_dify_runs.scope_json DEFAULT ''` 这个被 `_validate` 单点容忍的旧默认，
**在本轮已盘点的全部可达库上都不存在**；现役生产库与迁移前备份均为 `TEXT NOT NULL` 无默认，
已与 fresh 逐字一致。延期项的实施对象（携带该默认的旧库）在可达范围内不存在，
故无表可重建、无坏行可处置；root 独占、服务身份不可读的个别旧备份单独注明，
不把结论扩大到未读文件。issue 按盘点证据关闭。

## 盘点方法与范围

真实服务身份（`aiops41`）+ 现役 venv 只读 URI 执行，经项目声明的 `dev-host` 受控入口；
不写库、不建文件、不改权限。现役库使用普通 `mode=ro` 连接——WAL 感知的一致快照，
覆盖已检查点与未检查点的已提交行；`immutable=1` 只用于无写方存在的备份文件。
原始命令与输出私有留存（不含业务行值/凭据），本表为脱敏摘要。

| 库 | schema | scope_json 默认 | 备注 |
|---|---|---|---|
| 41 现役 `gateway.db`（14MB，服务打开中） | v1 | **无** | `chat_dify_runs` 265 行，0 NULL/空串；`agent_runs` 同样无默认 |
| 41 迁移前备份（1.7.0 部署内 schema/） | v0 | **无** | 264 行；证明线上从未携带该默认 |
| 41 迁移后复核备份 | v1 | **无** | 265 行 |
| 41 其余 3 个副本、5 个近期 bak、onboarding 存档 | v0 | 无此表或无默认 | onboarding 存档 97 行亦无默认 |
| 36 两个 gateway.db | v0 | 无此表 | 诊断库，非受管表集 |

现役库 `schema check`：版本 1、28 张受管表、warnings 仅 5 张已知额外历史表
（devices/enrollment_codes/events/ops_tool_audit/runs），**零结构漂移**；
`integrity_check` ok、`foreign_key_check` 无行；无任何表 FK 指向 `chat_dify_runs`，
声明索引 `idx_chat_dify_runs_qa` 在。

## 目标语义与残留决定

- 目标语义即 `gateway_schema.TABLES` 声明：`scope_json TEXT NOT NULL` 无默认；现役库已达。
- `DEFAULT ''` 唯一可产生的代码路径是 `ensure()` 对**缺列旧库**的 ALTER 补列
  （SQLite 非空表禁止加无默认 NOT NULL 列），随后被 `_validate` 记 warning 容忍。
  本舰队没有走过该路径的库：41 的表是原 CREATE 含列、非 ALTER 追加。
- 唯一写方 `dify_run_store` 的 INSERT 显式传 `scope_json`，无写路径依赖列默认；
  容忍分支对运行时零行为影响，保留给真正列缺失的老库（开发机/历史恢复）。
- **不做的事**：不为「干净」重建表、不删改任何行、不动 5 张额外历史表、
  不改 `_validate`/`ensure()` 容忍与补列规则。若将来某库升级后携带该默认并须归一，
  重建动作另开任务并自带受控复制/回滚验收。

## 本轮自动化检查与验收标识

- `tests/test_repository_layout.py` 19 通过、`tests/test_readme_architecture.py` 6 通过，
  无失败与修复；本 PR 确定性 CI 结果以最终运行为准。
- **未完成业务验收**：本回执只覆盖结构盘点与文档检查，
  不证明任何业务问答、迁移执行或生产行为变化。

## 范围边界

本回执只证明盘点时点上述库的结构事实；root 独占、服务身份不可读的
`gateway.db.bak-prompt-20260914` 未盘点（旧备份、非实施对象，不递归放宽权限读取）。
不评价业务数据内容、不改变任何运行行为。
私有证据目录记录原始命令与输出；未把任何凭据、业务行值或设备标识写入本文件。
