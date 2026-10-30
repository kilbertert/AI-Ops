# 执行回执：客户双端 AI 服务开通与自主管理

日期：2026-10-30。执行者：Claude（开发、自验、PR/CI、配套部署）。独立验收：**待 Codex**。
本回执不宣布最终合格；它写清做了什么、证据在哪、什么没做。

**结论一句话**：后端双端开通链路与页面入口已合并且 Gateway 已部署回读，34 项契约用例与
10 项浏览器用例通过；**真实页面开通与真实消费者/员工验收未完成**，A–L 中 9 项只有契约级
证据、4 项未验证（见 §5），所缺条件写死。

## 1. 规格与子任务

| 内容 | 位置 |
| --- | --- |
| 源规划（只读追溯） | [docs/agents/dual-entry-onboarding-plan.md](../../agents/dual-entry-onboarding-plan.md) |
| 任务书 | [docs/agents/dual-entry-claude-task.md](../../agents/dual-entry-claude-task.md) |
| 实施规格 | [implementation-spec.md](implementation-spec.md) |
| 验收合同（A–L） | [acceptance.feature](acceptance.feature) |
| 子任务与依赖 | [tasks.md](tasks.md) |
| 验收执行映射 | [qa-plan.md](qa-plan.md) |
| 决策记录 | [ADR-0189](../../adr/0189-dual-entry-onboarding-is-a-managed-catalogue.md) |
| 进度 / 阻塞 | [PROGRESS.md](PROGRESS.md) / [BLOCKED.md](BLOCKED.md) |

## 2. PR、合并提交与部署

| PR | 内容 | 合并提交 | CI |
| --- | --- | --- | --- |
| [#1221](https://github.com/kilbertert/AI-Ops/pull/1221) | 规划与任务书入主干 | `f5b6cd56` | verify / policy / spec-tree 通过 |
| [#1224](https://github.com/kilbertert/AI-Ops/pull/1224) | 双端目录、客户端准备、逐端发布、草稿预览、ADR、契约 | `8a995897` | 通过；**CD 自检失败并自动回滚**（见 §6） |
| [#1225](https://github.com/kilbertert/AI-Ops/pull/1225) | 页面双端入口与逐端状态 | `8161b4f4` | 通过（不改 `src/`，不触发 CD） |
| [#1226](https://github.com/kilbertert/AI-Ops/pull/1226) | 启动纳管对已在册应用 no-op | `ffa9ed64` | 通过；**CD 成功** |
| [#1227](https://github.com/kilbertert/AI-Ops/pull/1227) | 启动纳管补齐端/环境/目标 | 已合并 | 通过；CD **排队中**（runner 被别的会话占用） |
| 本回执 PR | 进度、阻塞、回执 | 见 PR 链接 | 仅文档 |

**部署制品（41，`aiops-gateway-41.service`）**

| 项 | 值 |
| --- | --- |
| 部署前版本 | `1.0.0+e30318737089` |
| 当前线上版本 | `1.0.0+ffa9ed645e12`（CD run `41057759344` success，20:45 北京时间） |
| 源码落点 | `/opt/aiops-41/src/aiops_diagnostics/dify/{dual_entry.py,service_keys.py,self_service_store.py,self_service.py}`，mtime `2026-10-30 20:45:26` |
| 恢复点 | `/var/backups/aiops-41/backup-20261030-200848`（#1224 失败时自动回滚用的那一份） |
| 未部署 | Dify web 补丁镜像（#1225 的页面改动）—— 见 BLOCKED §1 |

## 3. A–L 逐项结果

状态口径：**PASS**（有本次可复现证据）／**PARTIAL**（契约级通过，真实环境未验证）／
**BLOCKED**（缺条件，条件写在 BLOCKED.md）。环境：本机（契约）+ 41（部署与只读回读）。

| 编号 | 契约级 | 真实环境 | 证据 |
| --- | --- | --- | --- |
| A 双端一次开通 | PASS | BLOCKED | `test_both_entries_are_created_and_published_separately`（两端各一应用、各一正式指针、来源客户指针不动）；页面真实开通缺 Dify web 镜像 |
| B1 只开客户端 | PASS | BLOCKED | `test_consumer_only_scope_does_not_open_the_operator_entry` |
| B2/B3 补开另一端 | PASS | BLOCKED | `test_adding_consumer_keeps_the_existing_operator_route`、`test_existing_operator_route_makes_the_new_copy_a_draft` |
| C1 重复提交 | PASS | BLOCKED | `test_same_request_key_creates_nothing_twice`、`test_changed_sources_under_the_same_key_are_refused` |
| C2 导入响应丢失 | PASS | BLOCKED | `test_consumer_import_uses_the_same_deterministic_app_id`、`test_import_retries_use_same_tenant_scoped_identity` |
| C3 凭据响应丢失 | PASS | BLOCKED | `test_lost_key_response_is_reconciled_not_duplicated`、`test_ambiguous_key_reconciliation_refuses_to_publish` |
| D 一端失败不破坏另一端 | PASS | BLOCKED | `test_consumer_failure_leaves_operator_ready_and_retry_only_resumes_consumer`、`test_operator_failure_leaves_consumer_ready` |
| E1 未发布副本可预览 | PASS | BLOCKED | `tests/consumer/test_consumer_draft_trial.py`（3 项：合成 draft 路由、正式指针表仍为空、移出目录后失效） |
| E2 保存不发布 | PASS | BLOCKED | `test_existing_operator_route_makes_the_new_copy_a_draft` |
| E3 发布冻结同一份草稿 | PASS | BLOCKED | `test_consumer_publish_freezes_the_draft_and_retract_does_not_fall_back` |
| E4 撤回不回落 | PASS | BLOCKED | 同上（撤回后 `state=retracted`、不回落直读） |
| E5 恢复历史版本 | PASS | BLOCKED | 同上（恢复后指针回到该版本号） |
| F 来源更新不覆盖客户修改 | PASS | BLOCKED | `test_source_change_after_freezing_is_refused` |
| G1 命中本客户对客库 | PARTIAL | BLOCKED | `test_consumer_copy_rebinds_knowledge_and_never_carries_source_ids`（副本选库=目标客户新建的对客库） |
| G2 内部库不可检索 | PARTIAL | BLOCKED | 既有 `tests/consumer/test_consumer_knowledge_picker.py`（选库决定真实搜索、越权拒绝）；真实会话未跑 |
| G3 跨客户/源客户库拒绝 | PASS | BLOCKED | `test_rebind_refuses_source_ids_for_another_tenant`、`test_rebind_requires_at_least_one_library` |
| H 消费者只读本人数据 | PARTIAL | **BLOCKED** | 既有 `tests/consumer/*` 授权用例；**真实消费者会话未执行** |
| I 管家站点范围 | PARTIAL | **BLOCKED** | 既有 `tests/operator/*`；**真实员工会话未执行** |
| J1 已接入渠道完整结果 | — | **BLOCKED** | 部署后现役入口匿名 401（边界未放宽），但真实入口实跑未执行 |
| J2 未接入渠道不谎报 | PASS | PARTIAL | `test_channel_state_comes_from_declaration_not_from_publication`；页面显示"已发布 · AI 已准备，渠道待接入"（浏览器用例）；41 未声明渠道 |
| K1 平台按授权目录 | PASS | BLOCKED | `test_catalogue_lists_sources_per_entry`、`test_cross_tenant_create_requires_platform` |
| K2 改一端不影响另一端 | PASS | BLOCKED | `test_publishing_one_entry_does_not_move_the_other` |
| K3 改一个租户不影响其他 | PASS | BLOCKED | 受管行按 `(tenant, entry, scenario)` 分键 + 同上用例；真实双租户对照未做 |
| L1 重启后可用 | PARTIAL | PARTIAL | 契约：`test_lost_key_response_is_reconciled_not_duplicated`（凭据回读）；真实：CD 重启后 `/health` ok、受管目录 5 行可读、启动纳管在生产库上执行 —— 但"已开通客户的双端资源"尚不存在 |
| L2 版本不匹配阻止启用 | PASS | BLOCKED | `test_restoring_a_version_of_another_app_is_refused`、`test_restoring_with_a_stale_revision_is_refused` |

## 4. 复跑命令

```bash
# 契约（34 项）
uv run pytest -n 0 tests/dify/test_dual_entry.py \
  tests/consumer/test_consumer_draft_trial.py \
  tests/dify/test_self_service_adoption.py

# 既有基线契约（任务书 §2 的 53 项）
uv run pytest -n 0 tests/execution/test_chat_route_bindings.py \
  tests/consumer/test_consumer_publication.py tests/dify/test_dify_app_registry.py

# 全量
uv run pytest -n 4          # 3625 passed, 9 skipped（既有跳过，本次未新增 skip）

# 静态
uv run ruff check src tests && uv run ruff format --check src tests

# 真实进程的关闭态（一次性数据根，真实 Gateway）
uv run python tools/verification/check_dual_entry_management.py --out var/verify-evidence

# 浏览器（固定 1.17.1 源码 + 全部 web-patches，组件与 service 已复制）
cd .scratch/dify-1.17.1/web
npx vitest --project browser --run app/components/aiops-management.browser.spec.tsx

# 线上只读回读
dev-host exec 41 --allow-service-exec -- curl -sS http://172.18.0.1:8788/health
```

## 5. 证据位置

- **仓库内（脱敏）**：
  - `docs/validation/2026-10-30-dual-entry-onboarding-backend.md`（契约、真实进程关闭态、镜像锁）
  - `docs/validation/2026-10-30-dual-entry-onboarding-frontend.md`（浏览器 10 项、tsc）
  - `docs/validation/2026-10-30-dual-entry-adoption-fix.md`（CD 失败与修复）
  - `docs/validation/2026-10-30-dual-entry-adoption-environment.md`（生产数据核对发现的缺口）
  - `docs/开发进度/2026-10-30-dual-entry-onboarding-{backend,frontend}.md`
- **私有（不入库）**：`var/verify-evidence/dual-entry-management.json`（真实进程的响应与
  任务行计数）、CI/CD run 日志、41 的 `journalctl` 片段。凭据一律运行时从库外 age 清单
  读取，未打印、未入库、未写进任何证据文件。

## 6. 失败、修复与恢复

| # | 现象 | 根因 | 处理 |
| --- | --- | --- | --- |
| 1 | #1224 部署到 41 后 `selfcheck=failed` | 启动纳管把 `created_by` 算进了"同一条登记"的比较，每次启动抛 `SETUP_APP_CONFLICT` | 主机**自动回滚**到 `1.0.0+e30318737089`（服务 active、`/health` 旧版本）；#1226 修比较元组并加 4 项回归；CD 重部署成功 |
| 2 | 受管目录里 5 行 `environment` 为空 | 启动纳管只写了应用与 agent 名 | 核对生产库时发现；#1227 补齐端/场景/环境/目标并加回归；CD 排队中（不影响生产） |
| 3 | `test_readme_architecture` / 镜像锁 变红 | 新增模块未进 README；`src/` 变更后 m2-live-03 镜像锁必须重建 | README 补两行；按仓库流程 `build-image.sh --update-lock` 重建并写回摘要 |
| 4 | 浏览器用例旧后端兼容那条失败 | 期望"客户端勾选未选中"，而旧后端形状下根本不渲染客户端选项 | 改断言为"不渲染"，理由写进用例注释 |

失败样本**没有被重试覆盖**：CD run `41053070025` 的失败结论、主机回执、回滚备份路径都保留。

## 7. 入口

- **原有入口（未改）**：Dify 控制台应用列表 / 编排页（`TenantSetup`、`ManagedPublish` 的挂载点）、
  消费者原 H5、员工原管家 H5。本次没有新增域名、没有新增公网暴露。
- **新入口（已合入仓库、镜像未部署）**：应用列表顶部"复制并开通客户 AI 服务"与逐端进度；
  编排页"发布到客户端客服 / 发布到管家"。
- **API**：`/v1/dify/management/{catalogue,operations,operations/{id},retry,publish,versions,version,status}`，
  形状见 [implementation-spec.md](implementation-spec.md) §4。

## 8. 遗留与清理

**遗留**

1. Dify web 补丁镜像未构建部署 → 页面级真实验收的前提（BLOCKED §1）。
2. 真实消费者/员工会话未执行（BLOCKED §2）。
3. 41 未声明渠道（BLOCKED §3）。
4. #1227 的 CD 排队（BLOCKED §4）。
5. 管家端仍不支持带知识库发布（既有约束，本次未改）。
6. `deploy/m2-live-03/images.lock.json` 与另一条分支 `fix/readable-answer-output` 会冲突
   （两边都改同一文件）—— 后合并的一方需要重建锁。

**清理**

- 本次所有工作区在 `~/Projects/.worktrees/AI-Ops-dual-entry*`，没有改动其他会话的工作区；
  临时数据根（验证脚本）每次运行结束自动删除。
- 验证用的 Dify 1.17.1 源码树 `.scratch/dify-1.17.1/web` 上留了已应用的 web-patches 与
  复制进来的组件/service —— 这是该目录既有的用途（固定源码 + 补丁），没有清理。
- 没有删除任何既有测试、没有新增 `skip`、没有降低任何断言。

## 9. 声明

本回执**不宣布功能已验收合格**。真实页面开通、真实消费者与员工链路均未执行；
请按 BLOCKED.md 的条件补齐后由 Codex 独立复核。**待 Codex 独立验收。**
