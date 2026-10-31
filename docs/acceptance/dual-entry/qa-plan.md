# 验收执行映射（A–L）

每条编号对应任务书 §4 的必须通过项、[acceptance.feature](acceptance.feature) 里的场景、
以及**可执行的**检查。命令都在仓库根目录执行。

状态口径只有三种：**PASS**（有本次可复现证据）、**FAIL**（跑了、没过）、
**BLOCKED**（缺条件，写明缺什么）。没有"应该能过"。

## 自动化契约（本轮全部在本机跑过）

```bash
uv run pytest -n 0 tests/dify/test_dual_entry.py tests/consumer/test_consumer_draft_trial.py
uv run pytest -n 0 tests/dify/test_self_service.py tests/consumer/test_consumer_publication.py \
  tests/execution/test_chat_route_bindings.py tests/dify/test_dify_app_registry.py
uv run pytest -n 4 tests/dify tests/consumer tests/execution tests/publication tests/knowledge
uv run pytest -n 4            # 全量
uv run ruff check src tests && uv run ruff format --check src tests
uv run python tools/verification/check_dual_entry_management.py --out var/verify-evidence
```

`check_dual_entry_management.py` 在一次性数据根起**真实 Gateway**，断言管理面在
"匿名 / 错密钥 / 平台未启用"三种情况下都被拒绝，且被拒的请求不产生任何开通任务行。
它不证明真实 Dify 上的复制与凭据申请，也不证明真实用户可用。

| 编号 | 契约用例（自动化） | 真实环境用例（41） | 状态 |
| --- | --- | --- | --- |
| A 双端一次开通 | `test_both_entries_are_created_and_published_separately` | 页面开通两个租户，逐端回读 | 见 PROGRESS |
| B1 只开客户端 | `test_consumer_only_scope_does_not_open_the_operator_entry` | 同页只勾客户端 | 见 PROGRESS |
| B2/B3 补开另一端 | `test_adding_consumer_keeps_the_existing_operator_route`、`test_existing_operator_route_makes_the_new_copy_a_draft` | 现役客户补开 | 见 PROGRESS |
| C1 同一请求重复提交 | `test_same_request_key_creates_nothing_twice`、`test_changed_sources_under_the_same_key_are_refused` | 页面双击/刷新 | 见 PROGRESS |
| C2 导入响应丢失 | `test_import_retries_use_same_tenant_scoped_identity`、`test_consumer_import_uses_the_same_deterministic_app_id` | — | 见 PROGRESS |
| C3 凭据响应丢失 | `test_lost_key_response_is_reconciled_not_duplicated`、`test_ambiguous_key_reconciliation_refuses_to_publish` | — | 见 PROGRESS |
| D 一端失败不破坏另一端 | `test_consumer_failure_leaves_operator_ready_and_retry_only_resumes_consumer`、`test_operator_failure_leaves_consumer_ready` | — | 见 PROGRESS |
| E1 未发布副本可预览 | `tests/consumer/test_consumer_draft_trial.py`（3 项） | 编排页预览 | 见 PROGRESS |
| E2 保存不发布 | `test_existing_operator_route_makes_the_new_copy_a_draft` | 编排页保存后原入口不变 | 见 PROGRESS |
| E3 发布冻结同一份草稿 | `test_consumer_publish_freezes_the_draft_and_retract_does_not_fall_back` | 页面发布后回读版本 | 见 PROGRESS |
| E4 撤回不回落 | 同上 | 页面撤回后原入口停 | 见 PROGRESS |
| E5 恢复历史版本 | 同上 | 页面恢复 | 见 PROGRESS |
| F 来源更新不覆盖客户修改 | `test_source_change_after_freezing_is_refused` | — | 见 PROGRESS |
| G1 命中本客户对客库 | `test_consumer_copy_rebinds_knowledge_and_never_carries_source_ids` | 真实消费者问知识 | 见 PROGRESS |
| G2 内部库不可检索 | 既有 `tests/consumer/test_consumer_knowledge_picker.py` | 真实消费者问内部库问题 | 见 PROGRESS |
| G3 跨客户/源客户库拒绝 | `test_rebind_refuses_source_ids_for_another_tenant`、`test_rebind_requires_at_least_one_library` | — | 见 PROGRESS |
| H 消费者只读本人数据 | 既有 `tests/consumer/*` 授权用例 | **真实消费者会话**（原 H5） | 见 PROGRESS |
| I 管家站点范围 | 既有 `tests/operator/*` | **真实员工账号**（原管家 H5） | 见 PROGRESS |
| J1 已接入渠道完整结果 | 既有 `tests/test_h5_assistant_routes.py` | 原入口实跑 | 见 PROGRESS |
| J2 未接入渠道不谎报 | `test_channel_state_comes_from_declaration_not_from_publication` | 页面显示"渠道待接入" | 见 PROGRESS |
| K1 平台按授权目录 | `test_catalogue_lists_sources_per_entry`、`test_cross_tenant_create_requires_platform` | 篡改请求被拒 | 见 PROGRESS |
| K2 改一端不影响另一端 | `test_publishing_one_entry_does_not_move_the_other` | 页面改一端 | 见 PROGRESS |
| K3 改一个租户不影响其他 | 受管行按 `(tenant, entry)` 分键 + `test_publishing_one_entry_does_not_move_the_other` | 两个租户对照 | 见 PROGRESS |
| L1 重启后可用 | `test_lost_key_response_is_reconciled_not_duplicated`（凭据回读） | 41 重启后回读 | 见 PROGRESS |
| L2 版本不匹配阻止启用 | `test_restoring_a_version_of_another_app_is_refused`、`test_restoring_with_a_stale_revision_is_refused` | — | 见 PROGRESS |

## 真实环境（41）执行顺序

状态安全补验（本地隔离）：重启backfill后保留撤销、旧密钥快照迁移只留ID、失败日志不含
合成敏感标记、摘要检查与冻结之间改稿被拒、知识创建丢回执对账。采用真实存储/发布入口，
只模拟外部服务和响应丢失；必须有正反例并比较旧正式指针不变。

真实用例按 `docs/agents/env-41-runbook.md` 执行：正常登录、原页面、真实账号、
把每条请求关联到确切的 QA/run/版本。凭据从库外加密清单现场读取，不打印、不入库。
消费者验收**不能**用平台身份、自签令牌或直写数据库替代。

## 不在本映射里

新域名/小程序/安装包、公网暴露、账号提权、额外主机、业务写动作：不在本次授权范围，
也没有对应的验收项。

## 依赖实接修复（2026-10-31）

K4 使用固定 Dify 1.17.1 的 provider 路径、顶层列表、`api-key` 请求及脱敏凭据响应驱动；
空工作区自动安装并激活批准模型，已有凭据不复制来源客户密钥；不批准的缺失模型拒绝。
K5 只登记受管表后分别驱动真实消费者 billing/status 读取入口，核对连接、租户与用户；
另一租户/环境必须拒绝。临时数据库与合成凭据，结束随 tmp_path 清理。真实依赖与原 H5另验。
