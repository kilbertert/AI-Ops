# 执行回执：客户双端 AI 服务开通（第二轮返工）

日期：2026-10-30（第二轮）。执行者：Claude。独立验收：**待 Codex 复核**。
本回执不宣布合格；它写清 R1—R13 每一条的根因、修法、红绿命令、制品与**没做完的部分**。

**一句话**：R1/R2/R4—R11 已在真实链路上修好并有对应测试（57 项专项、3657 项全量通过），
Gateway 已部署 `1.0.0+b0d0bd3f392c` 并在生产库核对到"新授权判据没有停掉现役客服"；
**R3 只有代码与浏览器用例、web 镜像未部署**，**R12 只完成 Gateway 一件**，**R13 未解决**，
**真实双端 A–L 未执行**。

## 1. 规格与子任务

| 内容 | 位置 |
| --- | --- |
| 原规划 / 任务书 | `docs/agents/dual-entry-onboarding-plan.md`、`dual-entry-claude-task.md` |
| 独立验收（退回） | [INDEPENDENT-REVIEW.md](INDEPENDENT-REVIEW.md) |
| 返工任务书 | [CLAUDE-REWORK.md](CLAUDE-REWORK.md) |
| 实施规格 / 验收合同 / 子任务 | [implementation-spec.md](implementation-spec.md)、[acceptance.feature](acceptance.feature)、[tasks.md](tasks.md) |
| 执行映射 / 进度 / 阻塞 | [qa-plan.md](qa-plan.md)、[PROGRESS.md](PROGRESS.md)、[BLOCKED.md](BLOCKED.md) |
| 决策记录 | `docs/adr/0189-dual-entry-onboarding-is-a-managed-catalogue.md` |
| 原生子任务链接 | GitHub PR #1224（第一轮）、#1233（第二轮）；规格以本目录为准（本仓无 issue tracker 的 spec 单，任务书 §3.0 的"规格与子任务"以 `implementation-spec.md` + `tasks.md` + PR 承载） |

## 2. 本轮 PR 与制品

| PR | 内容 | 合并提交 | CI |
| --- | --- | --- | --- |
| [#1233](https://github.com/kilbertert/AI-Ops/pull/1233) | 第二轮返工：桥/导入/草稿/重试/凭据/知识边界 + 真实 Chatflow 页与首页入口 | `b0d0bd3f` | verify / policy / spec-tree 通过（首轮 CI 因 `test_h5_assistant_routes.py` 端口占用偶发失败，rerun 通过） |

**部署制品（41 `aiops-gateway-41.service`）**：`1.0.0+b0d0bd3f392c`，`/health` ok，
`business_mutations=disabled`，服务 active。CD run 成功。恢复点：上一版 `1.0.0+bec88a1ae690`
对应的部署备份（`/var/backups/aiops-41/backup-*`）。

**未部署**：Dify web 补丁镜像、控制台同源桥包、Dify API 补丁、节点 jar（见 BLOCKED §1/§4）。

## 3. R1—R13 处理矩阵

| 项 | 根因 | 修法 | 红→绿证据 | 状态 |
| --- | --- | --- | --- | --- |
| **R1** 桥拒双端字段 | `self_service_bridge.py` 的字段白名单缺 `scope`/`sources` | 加字段**并**逐字段校验形状（范围/端/版本号），畸形仍在桥层 400 | `tests/dify/test_dual_entry_bridge.py` 10 passed（改前反例 400 `unexpected field`） | **代码完成**；36 上的桥包未重装（BLOCKED §4） |
| **R2** 导入 id 不合规 | `request_id=job+":consumer"` 不满足补丁 `^[a-f0-9-]{36}$` | `consumer_request_id()` = `uuid5(NS,"consumer:"+job)`；`consumer_app_id()` 按补丁同一条派生 | `test_consumer_import_id_satisfies_the_published_patch_and_replays_the_same_app`（用补丁正则做 Pydantic 校验 + 回读同一应用） | **完成** |
| **R3** 组件没挂到真实 Chatflow 页 | 只挂 `Configuration` 视图；`/app/[appId]/workflow` 渲染 `WorkflowApp` | 新增 `WorkflowManagedStrip` + `06-workflow-and-home-entry.patch`（编排页挂条、首页加入口）；不受管应用返回 null | `aiops-workflow-managed.browser.spec.tsx` 2 passed；既有 10 passed | **代码+组件级完成，镜像未部署** |
| **R4** 草稿被 active 拒 | `_workflow_trial_draft` 无条件要求 `row.active` | 只对**正式**路线要求 active；草稿要求"仍属于该客户"（`require_current` 草稿分支） | `test_unpublished_consumer_draft_can_be_previewed_through_the_real_builder`（真实 builder + 正式指针表不变） | **完成** |
| **R5** 第二份草稿拿不到目标 | `consumer_target` 按租户 `fetchone()`；文件冲突按租户整体 | 目标按 `(租户, 环境, **应用**)`；同一应用两条权威不一致才冲突 | `test_second_consumer_draft_targets_its_own_app_and_can_be_published`、`test_different_apps_of_one_tenant_do_not_conflict` | **完成** |
| **R6** 发布忽略草稿摘要 | `publish` 对 consumer 分支丢弃 `expected_draft` | `status.draft_sha256` + 发布核对（`SETUP_DRAFT_CHANGED`） | `test_stale_draft_digest_is_refused_and_the_current_one_is_accepted` | **完成** |
| **R7** 重试复活已撤回 | 把"已撤回"当成"首次发布" | 指针已是本应用 ⇒ 只对账不推进 | `test_retry_after_lost_receipt_does_not_reactivate_a_retracted_version` | **完成** |
| **R8** 凭据跨请求重复 | 创建前快照只在内存 | `service_key_ops` 表持久快照 + 差集对账 | `test_key_reconciliation_survives_a_lost_list_and_a_new_attempt`（第二次尝试 `key_posts` 仍为 1） | **完成** |
| **R9** 首次发布失败留死草稿 | 非 published agent 一律拒绝 | `bootstrap` 接管**配置逐项相同**的未发布草稿；不同的一律不动 | `test_failed_first_consumer_publish_resumes_instead_of_creating_another_agent`（agent 集合不变） | **完成** |
| **R10** 本租户内部库可交付 | 判据只有"属于本租户" | 显式 `(租户, 环境, 库)` 授权行；发布与检索都查；撤销即时生效；不再按名字复用库 | `tests/consumer/test_consumer_knowledge_grants.py` 6 passed（含内部库被拒、缺字段 fail closed、撤销后已发布版本被拒） | **完成（契约级）**；41 上已对现役 3 个库补授权 |
| **R11** 依赖/业务连接没准备 | 只装管家模型与 `cpms-node`；客户端端点没登记 | 业务连接从**已批准环境连接**登记（无则拒绝该端）；插件+目录凭据写入并回读；源图每个模型必须有凭据；模型/插件/provider 改为**按端**准备 | `tests/dify/test_consumer_dependencies.py` 8 passed | **完成（契约级）** |
| **R12** 配套部署 | 只部署过 Gateway | Gateway 已部署并回读；web/桥/API/节点未做 | 见 §2 与 BLOCKED §1/§3/§4 | **部分完成** |
| **R13** 间歇回读失败 | 未定位（原 CI 日志已被 rerun 覆盖，取不到） | 本轮未改；已排除与本片 #1229 的凭据窗口同因 | 本机 3/3、`-n 4` 2/2、#1233 CI 通过 ⇒ **未复现** | **未解决，风险在册**（BLOCKED §5） |

## 4. A–L 状态（第二轮）

| 编号 | 状态 | 说明 |
| --- | --- | --- |
| A 双端一次开通 | 契约 PASS / 真实 **BLOCKED** | 契约用例覆盖；页面不可点（web 镜像未部署） |
| B1–B3 单端/补开 | 契约 PASS / 真实 **BLOCKED** | 同上 |
| C1–C2 重复与导入丢回执 | PASS | R2/R7 用例 + 既有幂等用例 |
| C3 凭据丢回执 | PASS | R8 跨请求/重启对账 |
| D 一端失败不破坏另一端 | PASS | 含"管家依赖失败不影响已就绪客户端"（模型/provider 按端准备） |
| E1 草稿预览 | PASS | R4 真实 builder |
| E2–E5 保存/发布/撤回/恢复 | 契约 PASS / 真实 **BLOCKED** | R5/R6/R7 用例 |
| F 来源更新不覆盖 | PASS | 来源摘要核对 |
| G1 命中本客户对客库 | 契约 PASS / 真实 **BLOCKED** | R10 授权行 |
| G2 内部库不可检索 | PASS（本租户内部库负例） | `test_internal_library_of_the_same_tenant_is_not_publishable`（不是拿跨租户充数） |
| G3 跨租户库拒绝 | PASS | 既有 + R10 |
| H 消费者本人数据 | **BLOCKED** | 真实会话未执行 |
| I 管家站点范围 | **BLOCKED** | 真实员工会话未执行 |
| J1 已接入渠道完整结果 | **BLOCKED** | 现役入口只回读到匿名 401 |
| J2 未接入渠道不谎报 | PASS（契约+组件） / 真实取值 **PARTIAL** | 渠道声明未写（BLOCKED §3） |
| K1 平台按授权目录 | 契约 PASS / 真实 **BLOCKED** | |
| K2 改一端不影响另一端 | PASS | 按端分派 + 用例 |
| K3 改一租户不影响其他 | 契约 PASS / 真实 **BLOCKED** | |
| L1 重启后可用 | PARTIAL | 三次 CD 重启后 `/health` ok、受管目录与授权行可读 |
| L2 版本不匹配阻止启用 | PASS（本轮范围） | 恢复他应用版本被拒 + 过期 revision 被拒；**不拿"恢复别的 app 版本"当"执行组件不匹配"的证据** —— 后者仍缺真实组件版本校验的用例，见遗留 |

## 5. 复跑命令

```bash
# 第二轮专项（57 项）
uv run pytest -n 0 tests/dify/test_dual_entry.py tests/dify/test_dual_entry_bridge.py \
  tests/dify/test_dual_entry_lifecycle.py tests/dify/test_consumer_dependencies.py \
  tests/consumer/test_consumer_knowledge_grants.py tests/consumer/test_consumer_draft_trial.py

# 全量
uv run pytest -n 4            # 3657 passed, 9 skipped
uv run ruff check src tests && uv run ruff format --check src tests

# 浏览器（固定 1.17.1 + 全部 web-patches）
cd .scratch/dify-1.17.1/web
npx vitest --project browser --run app/components/aiops-management.browser.spec.tsx
npx vitest --project browser --run app/components/aiops-workflow-managed.browser.spec.tsx

# 线上只读
dev-host exec 41 --allow-service-exec -- curl -sS http://172.18.0.1:8788/health
```

## 6. 证据位置

- 仓库内：`docs/validation/2026-10-30-dual-entry-{onboarding-backend,onboarding-frontend,adoption-fix,adoption-environment}.md`、
  `docs/开发进度/2026-10-30-dual-entry-{onboarding-backend,onboarding-frontend,r2-lifecycle,r2-frontend}.md`、
  本目录四个文件。
- 私有：`var/verify-evidence/dual-entry-management.json`（真实进程关闭态）、CI/CD run 日志、
  41 的只读 SQL 输出（受管目录 5 行、`consumer_knowledge_grants` 3 行）。
- 凭据：全程只读资产字段是否存在，未打印、未入库。

## 7. 遗留（不因本回执关闭）

1. Dify web 镜像未构建部署 ⇒ R3 无真实页面证据、A/B/E/K 的真实动作做不了。
2. 桥包 / API 补丁 / 节点 jar 未重装 ⇒ R1 的修复在 36 上还没生效。
3. 渠道声明未写 ⇒ J2 真实取值仍是"待接入"。
4. R13 未解决（原日志不可取、未复现），风险面：回读路径的篡改检测。
5. L2 只有"恢复他应用版本/过期 revision"两种拒绝；"执行组件版本不匹配阻止启用"仍缺专门用例。
6. 真实双端 A–L 未执行（两个租户对照、真实消费者与员工、原页面正文、QA/run/版本关联）。

**待 Codex 复核。**
