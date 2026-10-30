# 进度

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
