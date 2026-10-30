# 双端开通后端：自动化验证记录

日期：2026-10-30。分支 `feat/dual-entry-onboarding`，基线 `origin/main` @ `b5d46610`（rebase 前 `4af09893`）。
本文件只记录**本次实际跑过**的检查与真实发现的缺陷。真实环境（41）验收不在本片，见
[docs/acceptance/dual-entry/PROGRESS.md](../acceptance/dual-entry/PROGRESS.md)。

## 新增自动化检查

```bash
uv run pytest -n 0 tests/dify/test_dual_entry.py tests/consumer/test_consumer_draft_trial.py
# 29 passed
```

`tests/dify/test_dual_entry.py`（26 项）覆盖：

| 用例 | 断言的是什么 |
| --- | --- |
| `test_both_entries_are_created_and_published_separately` | 一次提交里两端各建一个应用、各自一个正式指针，来源客户的指针不动 |
| `test_consumer_only_scope_does_not_open_the_operator_entry` | 只开客户端不产生管家路由 |
| `test_missing_source_for_a_selected_entry_is_refused` | 选了某端却没给来源 ⇒ 拒绝 |
| `test_a_source_of_the_other_entry_cannot_be_relabelled` | 管家应用不能填进客户端来源槽位 |
| `test_adding_consumer_keeps_the_existing_operator_route` | 补开客户端不改现役管家路由 |
| `test_same_request_key_creates_nothing_twice` | 同请求键重跑不新建应用、不新建凭据 |
| `test_changed_sources_under_the_same_key_are_refused` | 同键换来源 ⇒ `SETUP_REQUEST_CHANGED` |
| `test_consumer_import_uses_the_same_deterministic_app_id` | 客户端应用 id 由 `(workspace, job, ":consumer")` 派生 |
| `test_lost_key_response_is_reconciled_not_duplicated` | 凭据响应丢失 ⇒ 对账复用那一枚，只申请一次 |
| `test_ambiguous_key_reconciliation_refuses_to_publish` | 新增两枚 ⇒ `SETUP_KEY_AMBIGUOUS`，不切指针，另一端仍成功 |
| `test_consumer_failure_leaves_operator_ready_and_retry_only_resumes_consumer` | 一端失败 ⇒ `partial`；重试只补建客户端应用 |
| `test_operator_failure_leaves_consumer_ready` | 公共准备失败 ⇒ 明确失败，不谎报部分完成 |
| `test_existing_operator_route_makes_the_new_copy_a_draft` | 已有正式管家时新副本 `draft_only`，原指针不变 |
| `test_consumer_publish_freezes_the_draft_and_retract_does_not_fall_back` | 发布→撤回→恢复，撤回后不回落 |
| `test_publishing_one_entry_does_not_move_the_other` | 发布一端不动另一端 |
| `test_source_change_after_freezing_is_refused` | 来源在本轮期间变了 ⇒ `SETUP_SOURCE_CHANGED` |
| `test_consumer_copy_rebinds_knowledge_and_never_carries_source_ids` | 副本的知识选择被换成目标客户新建的对客库 |
| `test_rebind_refuses_source_ids_for_another_tenant` | 源客户库 ID 出现在目标选择里 ⇒ 拒绝 |
| `test_rebind_requires_at_least_one_library` | 空选择 ⇒ 拒绝（发布契约要求 1..20） |
| `test_catalogue_lists_sources_per_entry` | 目录按端列来源；顶层 `sources` 仍是管家端 |
| `test_channel_state_comes_from_declaration_not_from_publication` | 已发布但无渠道声明 ⇒ `pending` |
| `test_conflicting_channel_declarations_are_refused` | 同一 (租户, 端) 两个矛盾声明 ⇒ 拒绝 |
| `test_managed_registration_is_the_first_authority_for_consumer_targets` | 受管行优先于现役文件 |
| `test_file_registration_conflicting_with_managed_row_is_refused` | 两条权威指向不同应用 ⇒ 显式拒绝 |
| `test_restoring_a_version_of_another_app_is_refused` | 版本快照属于别的应用 ⇒ `SETUP_VERSION_MISMATCH` |
| `test_restoring_with_a_stale_revision_is_refused` | 过期 revision ⇒ `SETUP_ROUTE_CHANGED` |

`tests/consumer/test_consumer_draft_trial.py`（3 项）：未发布副本可以构造试用（合成
`state="draft"` 路由、正式指针表仍为空）、副本被移出目录后试用失效、非客户端/未知应用拒绝。

## 回归中发现的真实缺陷

**`node_publication.all_targets` 被客户端行打断。** 受管目录加了客户端行之后，
`all_targets` 仍然遍历全部行并用 `profile_id` 查 profile；客户端的 `profile_id` 是空串，
于是 `KeyError: ''` —— 而这会让**整张**目标列表加载失败，也就是"开通一个客户端应用，顺手
把管家端所有节点路由停掉"。这是 `test_both_entries_are_created_and_published_separately`
第一次运行就红出来的，不是事后补的判据。修复：只读 `entry='operator'` 且 `profile_id` 非空的行。

**`self_service_config.app_binding` 把客户端行当成 operator 绑定。** 同一张表里两种端，
只按 `app_id` 查会让"这一版走节点引擎"变成一句凭空的事实。修复：只认 `entry='operator'` 的行。

## 全量与静态检查

```bash
uv run pytest -n 4
# 3620 passed, 9 skipped（既有跳过，本片未新增 skip）

uv run ruff check src tests          # All checks passed
uv run ruff format --check src tests # 364 files already formatted
```

## 制品与锁

`deploy/m2-live-03/images.lock.json` 由仓库自己的流程重建并写回：

```bash
M2LIVE_DIST=/tmp/m2live-dist \
M2LIVE_BASE_IMAGE=python@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f \
deploy/m2-live-03/build-image.sh --update-lock
# source_sha256=6605528f516ec7cf…  image_id=sha256:b66c32ed46df0887…
# built_at=2026-10-30T11:34:16Z   base_digest 与锁内原值一致
```

## 未覆盖（如实说明）

- 真实 Dify 1.17.1 上的复制/导入/凭据申请：本片用替身证明契约与对账逻辑，
  **不证明**真实租户空间里的行为。见 PROGRESS 的真实验收项。
- 真实消费者与员工会话、原 H5/App 页面、站点范围：需要真实账号与部署，未执行。
- 模型驱动的答复质量、长期稳定性：不在本片范围。
