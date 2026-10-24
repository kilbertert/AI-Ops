# 2026-10-24 · M3-01：SOP 编辑→试用→发布→撤回 控制 App 的 Dify 执行

**这一片把"运营点发布，App 才换版本；点撤回，不再交付"接成了真的。**
默认关闭；41 与 36 未操作；公司 App/后端仓、节点协议未改。

## 一、新增的自动化检查

| 文件 | 内容 | 结果 |
|---|---|---|
| `tests/test_chat_route_bindings.py`（新增，33 条） | 正式指针推进 / 撤回改状态不删行 / 撤回幂等 / 不自动恢复旧版 / 两租户两环境互不影响 / `revision` CAS 冲突 / 管理行与文件行冲突即启动失败 / 未知引擎拒绝 / 三组反向验证 / **撤回拒绝而不是回落直读** / **发布要求凭据文件已就位** | 33 passed |
| `tests/test_dify_publish_api.py`（+12 条，共 29） | 撤回与发布同一道门 / 未配置 404 / 未绑定 404 / 跨 app 撤回 409 / 请求体不能选版本 / 延迟发布不覆盖撤回 / chat 应用不要求 workflow 指针 | 29 passed |

**变异验证**（每条都确认删掉修复会转红、还原转绿）：

| 删掉的修复 | 转红的用例 |
|---|---|
| 撤回改成 `DELETE` | 7 条（含 `test_reverse_retracting_must_not_delete_the_row`） |
| 「正好五个字段」/ 空 `workflow_id` 校验 | `test_reverse_publishing_without_a_workflow_identity_is_refused` |
| **撤回后回落直读** | `test_a_retracted_route_is_refused_instead_of_falling_back_to_direct_read` |
| 回读 TTL、绑定当前性、同轮快照（APP-RUNTIME-02 遗留判据） | 各自对应用例 |

| `tests/test_dify_console_publish.py`（+5 条） | 撤回/恢复/版本列表走桥：转发路径与请求体、网关未确认状态**不报成功**、恢复必须带选中版本号、请求体不能夹带租户 | 93 passed |
| `tests/test_dify_workflow_trial.py`（新增，5 条） | 试用不动正式指针、不产生 `agent_versions`、有真实执行身份、临时应用成功与失败都删、路由变更即拒绝、失败也撤权 | 5 passed |

全量：**3846 passed / 9 skipped**。

## 二、真实环境验证（43/43 PASS）

`tools/m2_close/m3node/verify_dify_chat_entry.py` 扩展了运营闭环五步，从**运营能点到的
HTTP 入口**驱动（`POST /v1/dify/publish` / `POST /v1/dify/retract`），不手改绑定文件：

| 步骤 | 实测 |
|---|---|
| 前置：注册表 / workspace / agent | 三者就绪 |
| 保存 V2 草稿 | 真的有 dify run，实际执行 **V1**（`83cc5625…`） |
| 发布 | 200，`action: synced`，`version_no: 2` |
| 发布后 | 真的有 dify run，实际执行 **V2**（`8a64ca7a…`） |
| 撤回 | 200，`state: retracted`，`revision: 3` |
| 撤回后新请求 | **没有执行**（无 completed、无直读正文） |
| 撤回后历史正文 | **410 `CHAT_ROUTE_RETRACTED`** |
| retracted 行 | 仍在（`rows: 1`，不删行） |
| 显式恢复 | 200，`completed` |

每条判据都额外要求"这一轮真的走了 Dify"（执行记录里有 `workflow_run_id`）——
直读同样返回 completed，只看 completed 分不清"执行了"与"掉回直读"。

## 三、失败与修复（这一轮真实发生的）

| 现象 | 真因 | 修法 |
|---|---|---|
| **撤回后新请求仍 completed** | 撤回把行改成 `retracted`，`select_binding` 查不到活动绑定就返回 `None`，而 `None` 的既有含义是"没配过，走原直读" | 两种情形分开：没配过 ⇒ `None`（兼容）；撤回过 ⇒ 410 `CHAT_ROUTE_RETRACTED` |
| 发布成功但随后每次请求 `DIFY_UNAVAILABLE` | 发布推进指针时没确认 `<key_dir>/<app_id>.key` 存在 | 发布时校验凭据可读，缺失返回 409 `DIFY_KEY_MISSING`，当场失败 |
| `DSL has no model_config object` | `sync_binding_from_dify` 只支持 chat/completion 应用；M3-01 发布的是 **workflow** 应用（内容在 `workflow.graph`） | 新增 `sync_workflow_from_dify`，冻结 workflow 快照；按**结果**分流，不额外打一次导出 |
| `EXEC_CONFIG_UNAVAILABLE`（改完文件后） | `settings` 在启动时构造；配上文件路径后删文件只会让整表读失败。而"写一张空表"也是配置错误 | 管理模式**重启**网关且不配文件路径 —— 这才是真实迁移形状 |
| `DIFY_UNAVAILABLE` 反复出现 | 验收脚本里 `for key, expected in (...)` 把 app service key 覆盖成 `"b"`，写进凭据文件的就是一个字符 | 循环变量改名；并把"拿到 service key"的判据从 `bool(key)` 收紧为长度 ≥16 |

同时修掉一批**验收脚手架的假绿**：`with sqlite3.connect()` 不提交、手写 INSERT 与真实表结构
列数不符、`roles={"admin"}` 而非 `ROLE_*`、`AgentManager(GatewayStore)` 而非 `AgentStore`、
空绑定表当成"没有绑定"、app key 当 console 凭据（要的是 Dify 全局 `ADMIN_API_KEY`）。

## 四、未完成业务验收

**本次没有真实业务故障案例。** 真实公司 UPMS 权限、真实模型供应商、真实业务库、
登录 App 真机验收都**没有**做；生产开关未启用；41 与 36 未部署。
本轮全部结论来自隔离 Dify 1.17.1 栈 + 合成模型/业务/身份，**不能**写成真实故障结论。
