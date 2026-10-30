# 编排页提示词写正文：本地验证记录

日期：2026-10-30（UTC）。分支`feat/consumer-model-reply`，基线`425b5f79`。
本记录是**提交前**的本地验证；远端 CI、合并与部署不在本记录内。
目标与缺口见[开发进度记录](../开发进度/2026-10-30-consumer-model-reply.md)，
取舍见 [ADR-0187](../adr/0187-consumer-chatflow-prompt-writes-the-reply.md)。

## 环境与范围

开发机、本任务 worktree、本地 Docker（仅重建内容锁）。未访问 41、未改任何 Dify 提示词、
未发布任何线上配置。

## 自动化检查

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 静态检查 | `uv run ruff check .` | 通过 |
| 全量测试 | `uv run pytest -n 4` | **3562 passed / 9 skipped**，178s |
| 本片专项 | `uv run pytest tests/consumer/test_consumer_model_reply.py` | 15 passed |
| DSL 可解析 | `yaml.safe_load(consumer-service.dsl.yml)` | 通过，`answer` 节点提示词为新版 |

**一次读数说明**：同一命令首次跑出现 3 failed（`test_h5_assistant_routes` 等），复跑全绿 ——
与该测试在 CI 上的既有偶发同形（见 `docs/validation/2026-10-30-catalogue-source-labels.md`），
不是本片引入。

## 变异验证（先红后绿）

| 变异 | 被拦住的用例 |
| --- | --- |
| 去掉"正文里每个数字都要有出处" | 编造金额、编造时长、小计数三条 |
| 回读时直接放行（不做证据校验） | `test_readback_refuses_a_reply_whose_numbers_do_not_match` |

第二条第一次**没有变红**：篡改用例只改了 `assistant_questions.result_json`，被"正文 == 快照"
那条既有检查先拦下，没走到本片的容差分支。把快照一起改掉之后，该变异才真正变红 ——
这条记录留着，因为"测试因为错误的理由通过"比没有测试更危险。

## 未完成项（不得读作通过）

- **未完成业务验收**：没有在 Dify 编排页改过线上提示词，也没有真实账号的答复样本。
- 语气是否真的更拟人，只能人眼看；本地断言只管数字、标识、订单号与形状。
- 回读判据比确定性渲染弱一档（保留了全部数字的改写会通过），已在 ADR-0187 写清。
- 远端 CI、合并、部署均未开始。
