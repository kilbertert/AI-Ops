# 347. 同环境多租户发布后按租户独立停用恢复

状态：已采纳（2026-11-26）。

## 背景

[2026-11-16 手工环境接入与一键开通规划](../architecture/2026-11-16-manual-environment-onboarding.md)（§8、P1/P2/P5）要求同环境多租户可分别发布与停用，停用其中任一租户时不影响同环境其他已启用租户继续服务。

原 `ops/environments/gateway/publication.py` 与控制面 `onboarding/publication.py` 隐含「单租户或严格后进先出（LIFO）」假设：若租户 A 发布激活后再发布激活租户 B，随后对较早发布的租户 A 触发「恢复本次开通」：
1. 目标安装器 `check_files()` 只认本任务的 `before/after` 字节与自身 `installed.json` 快照，因租户 B 追加了 `knowledge-delegation.json`、推进了 `environment.json` 与 `installed.json` 而误报漂移；
2. 目标安装器 `recover()` 直接用 `previous-registry.json` 的 `original["enabled"]` 覆盖整环境开关，且按 `change["before"]` 删除 `knowledge-delegation.json` 并回退 `production.env`，会将仍在服务的租户 B 一并停用并破坏其知识委派；
3. 若随后再恢复最后剩余的租户 B，一方面直接写回旧 `before` 会复活租户 A 已撤销的能力，另一方面若因 B 自身 `before == after == P1` 而保留悬空的 `AIOPS_GATEWAY_KNOWLEDGE_DELEGATION_FILE`，会导致目标 Gateway 重启或后续 `candidate-install` 在启动加载委派文件时崩溃死锁；
4. 控制面 `source_configuration(restore=True)` 在同环境仍有其他已启用或已 `staged` 委派任务时会错误回退 `references.json`，而在非 LIFO 恢复最后租户后又无法把 `references.json` 同步退回直连基线。

## 决定

1. **环境级去委派基线（消除非 LIFO 最后租户恢复后的悬空委派引用）**：
   - 目标侧首次将 `production.env` 从直连基线 `P0` 切换为委派配置 `P1` 时，落盘 root 私有 `0600` 文件 `publication-delegation-baseline.json`（位于 `publication-tasks/` 之外、与 `installed.json` 同级，避免原子写入中断遗留临时文件阻塞任务目录扫描）；控制面首次或重领前向 `source_configuration` 将 `references.json` 源配置从 `T0` 切换为 `T1` 前，同步确保 `0600` `publication-bundles/delegation-baseline-<env>.json` 落盘。
   - 无论以 LIFO、非 LIFO 或三租户任意顺序停用至**最后一个离开委派的租户**，目标侧与控制面均基于该环境级基线成套回退 `production.env (P1 → P0)`、清理 `knowledge-delegation.json` 并按当前 `references.json` 文档摘要 + 本环境配置项 `expected_sha256` 双重 CAS 同步回退 `references.json (T1 → T0)`（不受其他无关环境更新共享 `references.json` 阻塞），杜绝悬空 `AIOPS_GATEWAY_KNOWLEDGE_DELEGATION_FILE` 引发的重启崩溃与 `candidate-install` 死锁。
2. **严格 fail-closed 摘要绑定与受信任同级核验**：
   - `publication-tasks/` 及所有子目录、`operation.json`、`delegation-baseline.json` 强制校验非符号链接、root 属主、`0700/0600`、`st_nlink == 1` 与目录名同任务编号，拒绝任何未知条目。
   - 非 `pending.json` 稳态下，`check_files()` 与 `setup()` 强制核验磁盘 `production.env`、`environment.json`、`knowledge-delegation.json` 分别与当前受信任 `installed.json` 的 `config_sha256`、`registry_sha256`、`knowledge_sha256` 精确一致，拒绝空 `capabilities: []` 与任一活跃对端能力缺失，阻断第三方历史快照回滚或删改能力被洗白入账本；同时允许受管 `install.py` 在两次发布间推进 `installed.json`（保留 `knowledge_sha256`）而不阻塞既有租户恢复。
   - 同级能力汇总按相位优先级（`activated > activating/restoring > staged > configuring`）确定性取优，不再受任务编号字典序影响；若同租户已存在更新的 `activated` 发布，旧残留任务禁止执行 `recover()` 误杀活跃租户。
3. **按租户剥离恢复、同环境互斥与双端开关收敛**：
   - 目标侧 `recover()` 存在其他已启用租户时保持 `environment.enabled=true` 与 `production.env` 不变（零重启），仅关闭本租户并单调递增 `revision`、撤回本租户路由与密钥、从 `knowledge-delegation.json` 精确剥离本租户 `consumer.app_id`；`activate()` 基于实时 `installed.json` 与当前已验 `knowledge-delegation.json` 写入最新 `knowledge_sha256`；`observe()` 在恢复终态核对本租户已停用且密钥/能力已移除，不锁死后续最后租户回退 `production.env` 后的幂等重入。
   - 控制面 `restore_task()` 增加与 `submit()` 一致的同环境 `ENVIRONMENT_TASK_BUSY` 互斥保护；`source_configuration(restore=True)` 同时检查中央目录其他已启用租户及同环境在飞/已 `staged` 的发布任务；`activate_directory(restore=True)` 在最后租户停用后同步将中央目录 `row["enabled"]` 收敛为 `false`。

## 备选方案

- **仅在文档要求运维严格按 LIFO 倒序恢复租户**：现实运维中需按业务事件停用任意租户，且无法解决三租户交错启停后的悬空基线问题。
- **最后租户停用后保留悬空 `AIOPS_GATEWAY_KNOWLEDGE_DELEGATION_FILE` 并放宽 Gateway 启动加载**：会破坏委派模式「配置后不回退直连且文件必须有效」的 fail-closed 安全语义，仍采用环境级基线双端对称回退。

## 后果与恢复

- 仅涉及目标受管安装器 `ops/environments/gateway/publication.py` 与控制面 `src/aiops_diagnostics/onboarding/publication.py`，不改变数据库 schema、公开 API 或单租户恢复契约；
- 需随同修订受管安装器与控制面 Gateway 一并部署生效；多租户非 LIFO 独立停用适用于基于本修订及之后安装器创建的发布任务（受管 worker 保持按任务冻结安装器摘要校验，不弱化安装器固定边界；历史单租户旧任务仍向后兼容，可按既有安装器回退）。
