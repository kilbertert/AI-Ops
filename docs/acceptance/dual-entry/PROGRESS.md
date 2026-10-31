# 进度

> **先读 [任务回执](TASK-RECEIPT.md)**：一个任务一份回执，含交付清单、真实验收结果、
> 未完成项与原因、当前生产状态与回滚点。本文件以下内容是过程记录。

## 六片修复逐片交付（2026-10-31，Claude 接力 Codex 会话）

Codex 会话 `01a180d5` 在第三批推送时被中断（并发工作区锁定）。本会话按原计划的
"六个小 PR，先状态安全，再依赖、受管预览、知识权限、兼容性，最后 UI" 逐片 rebase 到
当时 `origin/main` 后交付：

| 批 | 分支 | PR | 合并提交 | 内容 |
| --- | --- | --- | --- | --- |
| 1 状态安全 | `fix/dual-entry-state-safety` | #1235 | `1d4eb94d` | 已由 Codex 交付 |
| 2 依赖实接 | `fix/dual-entry-dependencies` | #1236 | `e0341551` | 已由 Codex 交付 |
| 3 受管预览 | `fix/dual-entry-managed-trial` | #1237 | `25d5b7dc` | 已由 Codex 交付 |
| 4 知识对客授权 | `fix/dual-entry-knowledge-access` | #1238 | `f168bf53` | 本会话 rebase + 合并；9 项用例 |
| 5 执行器兼容 | `fix/dual-entry-runtime-compatibility` | #1239 | `5ab4e4cf` | 本会话 rebase + 合并；65 项用例；摘要重建 |
| 6 真实工作流页 | `fix/dual-entry-workflow-ui` | #1240 | `79acaf4c` | 本会话 rebase + 合并；浏览器 15 项 |

**仍未完成**：`aiops-41-platform-dify` 正常登录后的真实页面开通、真实消费者/员工原 H5
会话、36 上的 web 镜像与桥/API 补丁同版部署。这些不是代码缺口，见 [BLOCKED.md](BLOCKED.md)。


## 收尾（2026-10-31 晚）

真实页面开通与发布已完成（见 [验收证据](../../validation/2026-10-31-real-page-onboarding.md) 与
[收尾记录](../../validation/2026-10-31-dual-entry-wrap-up.md)）。逐项状态：

| 项 | 状态 |
| --- | --- |
| 知识对客授权面板 | **完成**：根因是 36 的桥包落后（缺 `knowledge`/`knowledge-access`），重装后回读 9 个库 |
| 36 web 镜像 | **完成**：`dual-entry-merged-86d63d4a` 已部署并页面验证 |
| J2 未接入渠道不谎报 | **完成**：两端 `channel=pending`，客户端显示"该端尚未接入使用渠道" |
| 真实消费者/员工会话（H/I/J1） | **未完成**：原 H5 登录卡在公司 mall API 的 SQL 错误 |
| 孤儿知识库清理 | **未做**：8 个历次失败留下的 `对客知识库-<hash>` |


### 收尾追加（2026-10-31 深夜）

- **孤儿知识库**：实际 4 个（不是 8），已逐个核对"0 文档 0 分段 无引用"后删除，
  授权行置 revoked；该租户知识库 9 → 5。详见
  [清理与登录根因](../../validation/2026-10-31-kb-cleanup-and-h5-login-blocker.md)。
- **原 H5 登录**：不是资产问题。不存在的手机号得到同一错误；真实根因是公司商城后端
  插入 `UserInfo` 时 `sysem_id`/`tenant_id` 为 null（`Field 'sysem_id' doesn't have a
  default value` → `ThirdSession is null` NPE）。H/I/J1 仍未执行。
- **待修**：`_run_consumer` 每轮重试都先建库，失败后留下空库；本轮只清产物未改代码。

## Codex 直接接手（2026-10-31）

用户已改为由 Codex 直接实现与验收，不再向 Claude 派发。当前分支 `fix/dual-entry-state-safety`，
基线 `f9865f17`。状态安全五项与 R13 已有固定反例及修复，尚未部署；
后续继续处理受管连接、工作区模型/知识插件、真实编排页、配套部署与 A–L。
以下第二轮记录保留其当时结论；R13 本轮根因为取证时间数字污染金额白名单。


基线：`origin/main` @ `b0d0bd3f`（第二轮返工完成时）。第一轮基线 `4403681e`。
中断后先读本文件，不重复已完成项。阻塞与所缺条件见 [BLOCKED.md](BLOCKED.md)。
第二轮的依据：[INDEPENDENT-REVIEW.md](INDEPENDENT-REVIEW.md) 与 [CLAUDE-REWORK.md](CLAUDE-REWORK.md)。

## 第二轮（2026-10-30）逐项

| 项 | 状态 | 证据 |
| --- | --- | --- |
| R1 桥接受双端字段 | **完成** | `self_service_bridge.py` + `tests/dify/test_dual_entry_bridge.py`（10 项：合法穿过 + 5 种畸形仍被拒） |
| R2 导入 id 合规 | **完成** | `consumer_request_id`/`consumer_app_id` + `tests/dify/test_dual_entry_lifecycle.py`（用补丁正则校验 + 回读同一应用） |
| R3 真实 Chatflow 页 | **代码完成，镜像未部署** | `aiops-workflow-managed.tsx` + `06-workflow-and-home-entry.patch`；浏览器 2 项通过；36/41 上的 web 镜像仍是旧补丁 |
| R4 草稿试用 | **完成** | `gateway_api._workflow_trial_draft` 区分草稿/正式；`test_unpublished_consumer_draft_can_be_previewed_through_the_real_builder` |
| R5 多草稿目标 | **完成** | 目标按 `(租户, 环境, 应用)`；`test_second_consumer_draft_targets_its_own_app_and_can_be_published` |
| R6 草稿摘要 | **完成** | `status.draft_sha256` + 发布核对；`test_stale_draft_digest_is_refused_and_the_current_one_is_accepted` |
| R7 重试不复活撤回 | **完成** | `test_retry_after_lost_receipt_does_not_reactivate_a_retracted_version` |
| R8 凭据对账跨重启 | **完成** | `service_key_ops` 持久快照；`test_key_reconciliation_survives_a_lost_list_and_a_new_attempt` |
| R9 首次发布失败可续跑 | **完成** | `test_failed_first_consumer_publish_resumes_instead_of_creating_another_agent` |
| R10 对客/内部边界 | **完成（契约级）** | `consumer_knowledge_grants.py` + 6 项用例；41 上已对 3 个现役库补授权（见下） |
| R11 客户端依赖/业务连接 | **完成（契约级）** | `consumer_endpoint`/`consumer_plugin`/`consumer_models` + 8 项用例 |
| R12 配套部署 | **部分完成** | Gateway 已部署 `1.0.0+b0d0bd3f392c`；web/桥/API/节点未部署；渠道未声明 |
| R13 间歇回读失败 | **未解决** | 原 CI 日志已不可取；风险与影响面见 BLOCKED §5 |
| 真实 A–L 验收 | **未执行** | 缺 web 镜像部署与真实登录，见 BLOCKED §1/§2 |

## 自动化结果（本机）

```bash
uv run pytest -n 0 tests/dify/test_dual_entry.py tests/dify/test_dual_entry_bridge.py \
  tests/dify/test_dual_entry_lifecycle.py tests/dify/test_consumer_dependencies.py \
  tests/consumer/test_consumer_knowledge_grants.py tests/consumer/test_consumer_draft_trial.py
# 57 passed
uv run pytest -n 4      # 3657 passed, 9 skipped
uv run ruff check src tests && uv run ruff format --check src tests
```

浏览器（固定 1.17.1 + 全部 web-patches）：

```
npx vitest --project browser --run app/components/aiops-management.browser.spec.tsx      # 10 passed
npx vitest --project browser --run app/components/aiops-workflow-managed.browser.spec.tsx #  2 passed
```

## 部署记录（真实环境）

| 时间（北京） | 动作 | 结果 |
| --- | --- | --- |
| 第一轮 20:08 | CD `8a995897` | 自检失败，主机自动回滚 |
| 第一轮 20:45—21:4x | CD `ffa9ed64`、`f4601b25`、`bec88a1a` | 成功，最终 `1.0.0+bec88a1ae690` |
| 第二轮 ~23:4x | CD `b0d0bd3f` | 成功，`/health` = `1.0.0+b0d0bd3f392c`，服务 active |

**真实只读核对（第二轮部署后）**：`consumer_knowledge_grants` 表已建，并对租户
`1942105476598861824` 的 3 个**已发布给消费者**的库补了 `published-backfill` 授权 ——
即新的"允许对客"判据没有把现役客服停掉。匿名管理面仍 403，`/v1/openapi.json` 200，
`/v1/runs` 404，现役问答匿名 401。
