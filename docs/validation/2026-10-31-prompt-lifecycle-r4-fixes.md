# 提示词生命周期：第四轮复核的四项实际反例修复

日期：2026-10-31；分支 `test/prompt-lifecycle-browser`（承接 PR #1232）；未合并、未部署。

## 四项反例 → 用例 → 结果

| 反例 | 判据 | 用例 | 结果 |
|---|---|---|---|
| A 快照 finished，旁白写“这笔订单尚未完结，仍在供电。\n{{fact:status_line}}” | 否定词 × 状态词（同子句）+ 状态断言形式 + 现役文案状态标签 | `test_a_negated_state_prose_cannot_contradict_the_evidence` | 修复前**被接受**，修复后拒绝 |
| B 正常英文追问 “{{fact:status_line}}\nDo you have any other questions about charging?” | 裸状态词不再算断言 | `test_b_a_normal_english_follow_up_survives` | 修复前**被误拒**，修复后通过 |
| C 管家英文 reply 展开 order_facts 后仍中文标签 | 标签/状态值/来源标题按运行语言；完成时渲染并冻结 | `test_english_reply_gets_english_fact_labels_and_same_values`、`test_a_frozen_report_is_not_rewritten_by_a_later_upgrade` | 修复后英文正文无 CJK，值与来源不变 |
| D 简洁账单 “账单内容：{{fact:billing_line}}” | 片段必须独立成行，不得加前后缀；协议块与消费契约一致 | `test_d_inline_prefix_on_a_fact_segment_is_refused` | 拒绝；协议块含“每个片段单独占一行，不得添加前后缀” |

共同根因：状态判据原来只有“裸状态词/现役文案标签”一维，既漏掉同义改写（A），又把正常追问当断言（B）。
现改为**两个有限维度**：① 否定词 × 状态词（同子句）；② 断言形式（正在/仍在/还在/已经 + 状态动词、
`is/still + state word`）。状态词来自公司状态表与 `consumer_language.COPY`，加一组**维护中的**同义词
（供电/完结/进行/停止/异常）。这仍是维护中的词汇，不是对自然语言的穷举 —— 残留风险如实记在 ADR-0190。

管家事实段：`render_report(..., language=)`，标签/状态值/来源标题按运行语言；**完成时渲染并写入
`snapshot["rendered_report"]`**，回读优先用它，旧 run 没有冻结文本时按中文重算（与签发时一致），
所以升级不会改写历史答复。

## 实跑

```bash
uv run pytest -q tests/consumer/test_prompt_fact_segments.py tests/operations/test_operator_reply_facts.py -n 0   # 29 passed
uv run python tools/verification/verify_prompt_lifecycle.py --out /tmp/pl-browser-r4                                # 35 项 0 失败，exit 0
uv run pytest -q tests/ -n 4                                                                                        # 3764 passed / 9 skipped / 0 failed
uv run ruff check . / ruff format --check .                                                                          # 通过
```

## 仍未完成（不宣称通过）

- 固定两批 8 场景 × 2 风格共 32 次真实模型验收：未执行（上一轮 16 格样本仍记录为 15/16，账单行内前缀失败保留）。
- 用将要部署的受管 UI 做“编辑→预览→发布→恢复”：未执行（此前的 119 项联合页面证据用的是旧注入按钮）。
- 合并、部署与 41 原应用/原 H5 验收：未执行。
