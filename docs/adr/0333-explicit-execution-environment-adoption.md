# 333. 旧执行环境向唯一运行目录环境的一次性受控迁移（`environment-adopt-execution`）

状态：已采纳；为 41 旧执行环境（`production-41` → `41`）并入唯一运行目录提供受控一次性迁移与逐项 CAS 恢复工件，不引入运行时环境别名或双读回落。

## 背景与实证

41 生产只读盘点与源码核查证实：

- `gateway_config.py` 启动闸要求启用运行目录后 `execution_environment == environment`（即 `production-41` 必须迁至 `41`），且 `AIOPS_GATEWAY_ENVIRONMENT_REGISTRY_FILE` 与 `AIOPS_GATEWAY_BUSINESS_ENVIRONMENTS_FILE` 互斥。
- 既有管理入口均无法直接完成 `production-41` → `41` 指针重键：
  1. `chat_route_bindings` 有 5 条 `production-41` 活跃路由，其中 2 条 `consumer_service` 路由的 `agent_versions` 快照冻结了 `consumer_service.content.environment = "production-41"`，`_bindings_from_store` 逐字比对 `row.environment`，直接复制路由而不生成新版本会导致启动拒绝；
  2. 历史 `chat_dify_runs.binding_key` 仍指向 `production-41`，路由表只能 `INSERT` 复制新环境行，不能覆盖或删除旧行；
  3. `operator_apps` 含 11 条 `production-41` 与 2 条无绑定引用的空串 `''` 孤儿行；
  4. `consumer_knowledge_grants` 含 13 条 `production-41` 行（其中 5 条已撤销），若依赖启动 backfill 会复活已撤销授权；
  5. `conversations.operator_binding` 含 97 条现役管家应用正式会话（`_operator_conversation` 逐字校验 `binding.key:app_id:formal`），不显式迁移会令现役管家追问全部返回 404，而预览会话与历史退役应用会话则不应批量改写。

## 决定

1. 新增一次性受控迁移模块 `src/aiops_diagnostics/gateway/execution_environment_adoption.py` 与运维入口 `aiops-admin environment-adopt-execution`。
2. **默认只读预演**：不传 `--backup` 时以 SQLite `?mode=ro` 打开，校验零在飞任务（`JOB_TABLES`、未过期的 `agent_runs`——沿用 `agent_runs._expire_in` 的 `expires_at > now` 单时间戳判据，不因历史已过期未领取行永久阻塞、`chat_dify_runs`、`operator_setup_jobs` 与 `conversations.generating_since`；独立草稿库的 `environment_tasks` 由切换窗口停机前置步骤另行核验）、无目标环境冲突及源快照完整契约（直接复用 `chat_execution.validate_consumer_snapshot`，并拒绝多条消费者活跃路由共享同一 `agent_id`），输出不含密钥的冻结迁移清单（含孤儿 `operator_apps`、跳过会话计数及可选配置文件引用计数）。预演亦支持 `--verify-bindings`（含可选 `--bindings-file` 静态路由冲突核验）只读预验配套配置与凭据文件。
3. **提交前校验与独占备份的单事务迁移**：传 `--backup` 时在同一 `BEGIN IMMEDIATE`（`gateway_db.LEASE`）事务内先完成全部预检与可选 `--verify-bindings` 提交前真实绑定校验（`chat_execution._validate_binding` 与静态路由文件无重名校验），再完成以下状态迁移与回读断言，全部通过后才于 `COMMIT` 前以 `O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW`（`0600`）写冻结清单并 `fsync` 文件与父目录（若备份落盘中途或 `COMMIT` 任一步骤异常，均立即清理本次独占创建的备份并整体回滚，不留孤儿文件或半迁移态）：
   - 对 `consumer_service` 路由沿既有 Agent 发布合同（`published → draft → publish` 单调修订）克隆**路由当前实际指向版本**（即使曾回退至早于 `agents.published_version` 的历史版本）生成新的不可变版本，仅变更 `consumer_service.content.environment` 为目标环境，历史版本快照原样保留，并在清单中分别记录 `from_version_no` 与 `from_published_version`；由于消费者路由推进至新 `agent_version_no`，切换前固定在旧版本的消费者历史会话将按既有合同返回 `409 CHATFLOW_NEW_CONVERSATION_REQUIRED`，上线验收须包含新开消费者会话核验；
   - `INSERT` 复制活跃路由至目标环境，并在同一事务中将旧环境路由单调撤回（`state='retracted', revision=revision+1`，保留整行供 `by_key` 溯源，绝不删行）：`_bindings_from_store` 对全部 `active` 节点路由强制校验 `AgentRunConfig.load(node_config_file).environment == binding.environment`，切换共用 `agent-runs.json` 至目标环境后，若旧环境节点路由仍保持 `active` 将直接令启动与路由加载抛出 `ChatBindingError`；
   - 为防切换后误将消费者路由回指到仍带旧环境标记的迁移前历史版本导致 `_bindings_from_store` 整表加载失败，在 `self_service.change_version` 与 `gateway_api._advance_route_binding` 写指针前校验目标快照的 `consumer_service.content.environment == target_environment`，不匹配一律拒绝；
   - 仅迁移 `environment == source` 的 `operator_apps`（空串孤儿行不动）；
   - `INSERT` 复制 `consumer_knowledge_grants` 并原样保留 `granted_at`（授权世代）与 `revoked_at`；
   - 仅将匹配现役管家路由 `(scenario, source, tenant_id, app_id, formal)` 的 `conversations.operator_binding` 迁至目标键（预览与退役应用会话不动；所有前缀筛选均使用 `substr` 逐字匹配，避免 SQLite `LIKE` 通配符误伤含 `_` 的环境名）。
4. **逐项 CAS 恢复（`--restore --backup`）**：触库前先执行严格备份清单 schema 与交叉引用校验（`routes ↔ agents` 一一匹配、`operator_conversations.(from_binding, to_binding) ↔ routes` 精确匹配、必填键与类型完整，不接受缺段/篡改清单），再核验目标路由、已撤回的源路由（`revision+1`）、Agent 修订/发布版本与原版本快照存在性、`operator_apps`、目标 `grants`（含撤销态）与已迁移会话（含 `last_turn_no`）全部无迁移后漂移、零在飞任务且（如开启 `--verify-bindings`）恢复后绑定在提交前校验通过时才执行恢复；恢复采用单调修订撤回目标路由并单调重新激活源路由（不删行）、单调推进 Agent 修订回指原 `from_published_version`（保留新版本快照）、停用目标新授 `grants`（不复活撤销），不整库恢复。恢复后若需再次迁移至同一目标环境，工具按设计拒绝（`ADOPTION_TARGET_ROUTE_CONFLICT` / `ADOPTION_TARGET_GRANT_CONFLICT`），以防静默复活已撤销授权或破坏单调历史。

## 被否方案

- **在 `chat_execution` / `gateway_config` 增加 `production-41 ↔ 41` 运行时别名或双读回落**：违背唯一运行目录契约，使旧执行环境长期滞留并模糊路由归属。
- **直接 `UPDATE` 改写 `chat_route_bindings` 与历史 `agent_versions.snapshot_json`**：破坏历史 `chat_dify_runs.binding_key` 溯源与版本快照不可变性。
- **迁移后将旧 `production-41` 路由继续保持 `active`**：`_bindings_from_store` 加载全部 `active` 行并校验 `node_config_file` 环境与行环境一致，共用 `agent-runs.json` 切到 `41` 后会导致全部流量报错 `EXEC_CONFIG_UNAVAILABLE`。
- **迁移时忽略 `conversations.operator_binding` 或整表替换所有 `production-41` 会话**：忽略会导致 97 条现役管家正式会话追问全部 404；整表替换会误改历史退役应用和预览会话归属。
- **回退时整库覆盖 `gateway.db`**：会丢失窗口内产生的新日志/审计或复活新撤销的授权。

## 验证范围

- 本地隔离单测覆盖默认只读预演、单事务迁移、真实 `_bindings_from_store` 加载、真实 `_operator_conversation` 老会话追问与错主体拒绝、在飞/冲突拒绝以及迁移后三类漂移下的 CAS 恢复拒绝（见 `D49`）。
- 本工件不执行生产切换、不改公司配置或服务进程；41 实际切换窗口仍需另行批准并与 KB 重键、配置更新和登录代理目录启用成套执行。
