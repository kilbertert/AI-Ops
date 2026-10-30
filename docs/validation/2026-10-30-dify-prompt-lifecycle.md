# 提示词生命周期验证记录

日期：2026-10-30
分支：`feat/dify-prompt-lifecycle`（未合并）
里程碑：[开发进度](../开发进度/2026-10-30-dify-prompt-lifecycle.md) ·
契约：[acceptance.feature](../acceptance/dify-prompt-lifecycle/acceptance.feature) ·
步骤：[qa-plan](../acceptance/dify-prompt-lifecycle/qa-plan.md)

**结论先说**：本地确定性检查全绿，六条变异逐条转红；**真实 Dify／Java 节点／真实模型的业务
验收未完成**，留给 Codex 在合并制品后接续。本文所有数字来自本次实际输出。

## 一、基线（改动前）

```bash
uv run pytest -q tests/consumer/test_consumer_model_reply.py \
  tests/operations/test_operator_native_preview.py tests/execution/test_node_publication.py
# 64 passed, 0 skipped, 0 failed  （main b5d46610）
```

主线比任务书里的 `4af09893` 多一个提交 `b5d46610`（#1222 仓库布局门禁测试）。它只加
`tests/test_repository_layout.py` 与文档，**不改运行时**，基线仍是 64 项全过 —— 没有为凑数
删测试。改动后同一组用例复跑：**64 passed**。

## 二、新增自动化检查

| 文件 | 数量 | 覆盖 |
|---|---|---|
| `tests/consumer/test_consumer_prompt_facts.py` | 23 | 四条事实判据、澄清/无结果正文、系统出口不受影响 |
| `tests/consumer/test_consumer_prompt_migration.py` | 5 | 初始化／幂等／拒绝覆盖／显式替换／拒绝时零改动 |
| `tests/consumer/test_prompt_lifecycle_grid.py` | 20 | 16 格样本 + 空结果/空范围/缺单/多语言 |
| `tests/execution/test_prompt_version_lifecycle.py` | 3 | 草稿预览不动正式版、发布推进版本、恢复指回旧版 |

```bash
uv run pytest -q tests/consumer/test_consumer_prompt_facts.py \
  tests/consumer/test_consumer_prompt_migration.py tests/consumer/test_prompt_lifecycle_grid.py \
  tests/execution/test_prompt_version_lifecycle.py
# 51 passed
```

## 三、验收脚本（无人值守可复跑）

```bash
uv run python tools/verification/verify_prompt_lifecycle.py
# 共 23 项，失败 0 项；证据写入 /tmp/aiops-prompt-lifecycle-xxxx/grid.json
```

23 项 = 16 格（8 场景 × 2 风格）+ 4 条事实负例 + 3 条版本判据。脚本默认写临时目录，拒绝在
真实数据根写证据，并在末尾打印**未验证清单**（不把本地桩算作通过）。

## 四、变异验证（红 → 绿）

逐条改坏、跑对应用例、恢复后再跑：

| 变异 | 结果 |
|---|---|
| 去掉金额标签绑定 | `test_amount_labels_cannot_be_swapped` **1 failed** |
| 去掉货币断言守卫 | `test_a_currency_amount_must_be_in_the_snapshot` **1 failed** |
| 去掉到账/退款承诺守卫 | `test_payment_promises_are_refused` **5 failed** |
| 去掉状态方向守卫 | `test_status_cannot_be_inverted` **1 failed** |
| 允许无证据的找站说明 | `test_a_site_note_written_before_the_query_is_refused` **1 failed** |
| 迁移改回无条件覆盖 SYSTEM | `test_consumer_prompt_migration.py` **2 failed** |
| 全部恢复 | 对应文件 **7 passed** / **23 passed** |

## 五、旧用例的调整（逐条依据）

| 用例 | 改动 | 依据 |
|---|---|---|
| `test_replies_that_break_the_evidence_are_refused["合计 2.5"]` | 期望理由改为 `CHATFLOW_FACT_LABEL_MISMATCH` | 新判据更具体（说出"挂错标签"而不只是"没出处"），拒绝强度不变 |
| `test_prompt_written_reply_is_delivered_and_survives_readback` 的正文 | "已经充完电结束了" → "还在充电中" | 桩返回 `status=0`（充电中），旧正文与快照矛盾；这正是新判据要拦的话 |
| `test_readback_refuses_a_reply_whose_numbers_do_not_match` 的正文 | 同上 | 同上（该用例测的是回读篡改，前提正文必须合格） |
| `test_reply_without_live_data_is_refused` | kind 从 `not_found` 改为 `knowledge` | 澄清/无结果现在允许提示词写正文；"知识答复不许无依据写正文"这条**未放宽** |
| `test_search_ignores_model_prose_and_delivers_only_cpms_sites` | 改名为 `test_a_site_note_written_before_the_query_is_refused`，期望从"静默丢弃、照常交付"改为"整轮失败" | 静默丢弃会让写提示词的人以为那段话在生效；失败会说原因。**没有任何文字因此被交付**，比改动前更严 |
| `test_consumer_multilingual` / `test_consumer_answer_language` / `test_live_publication_ignores_...` | `live_documents(..., replace_system=True)` | 迁移默认不再覆盖运营编辑，这三条测的是模板写入后的产物，须显式替换 |
| `tests/common/test_answer_caller_shapes.py` | finalisation point 计数 2 → 3 | 澄清/无结果成为同一 surface 的第二个出口，`retrieved_titles=[]` 显式传入（不省略） |

**没有删除或 skip 任何测试，也没有放宽任何断言。** 既有条件跳过仍只有两条，且与本次无关：

```
SKIPPED tests/operations/test_operator_catalog_vi_mn.py:48  operator 目录已覆盖全部语言（#565）
SKIPPED tests/operations/test_operator_questions_triage.py:200  未配置 AIOPS_GL_CA
```

## 六、仓库要求的检查

```bash
uv run ruff check .            # All checks passed!
uv run ruff format --check .   # 1184 files already formatted
uv run pytest -q               # 见下方全量结果
```

全量结果（本次实际输出）：**3647 passed, 9 skipped, 0 failed**（`tests/`，`-n 4`，EXIT=0）。
`deploy/m2-live-03/images.lock.json` 的 `gateway_image.source_sha256` 因 `src/` 变更同步更新
（`deploy/m2-live-03/context_digest.py` 计算，190 个文件），否则复核者算出的摘要必然对不上。

Java 节点本轮**没有改动**（`services/execution-node/` 未触碰），因此不重建 jar；桥与 nginx
snippet 同样未改动，`ops/dify-console-bridge/build-bundle.sh` 本轮无需重跑。

Gateway 对外行为：`/v1/assistant/questions` 的响应形状未变（只是同一条出口的正文来源可以是
提示词）。`/v1/dify/publish|retract|restore` 未改。**未执行** `verify-aiops-gateway`——
本轮没有改动它的路由、鉴权或注册，改了的是正文内容判据，本地 pytest 已覆盖到交付与回读。

## 七、未完成（不得记作通过）

- **业务验收未完成**：41 上的真实 Dify 应用、真实 Java 节点、真实模型、原 H5 都没有跑。
  16 格样本的"两种风格"在本地是合成文本，只证明契约与判据，不证明真实模型写得像人。
- 找站说明依赖真实模型"先调工具再写说明"；本地无法证明它会稳定这么做。
- 页面级 发布／恢复／在途会话 的点击验收未做，步骤见 qa-plan §五。
