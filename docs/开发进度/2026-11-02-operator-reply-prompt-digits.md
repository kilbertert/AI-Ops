# 管家输出契约收紧：reply 其它行的数字禁令

日期：2026-11-02。分支 `fix/operator-reply-prompt-digits`，基线 `1538237825f1`。

## 一句话

`#1305` 上线后同一句原话**间歇性整轮失败**；根因是输出契约里「枚举费用字段」与
「不要复述这些值」两句打架，模型在事实段外写数字被 `check_notice` 拒。改措辞，
不放宽校验。

## 已完成

- `OPERATOR_OUTPUT` 里把禁令写死并提高优先级：reply 的其它行**一个字都不要带数字**
  （金额、百分比、条数、时间都算），并明说不要提具体是哪几项费用。
- 新增 `test_output_contract_tells_the_model_the_fact_block_carries_every_fee`：
  同时断言「枚举里有费用项」与「有数字禁令」—— 只满足一半就是这次的缺陷。
- Gateway 镜像重建并 relock（`7d141e5d…`）。
- 决定与取舍并入 ADR-0193 第三节（含被否的「放宽 `check_notice`」方案）。

## 验证

- `tests/operations` + `tests/execution`：624 passed。
- `ruff check` / `ruff format --check`：通过。
- **基线量了 6 次**：同一句「帮我分析这笔单的电费2116050516255272961」，
  4 次 `completed`、2 次 `failed`（`reason=unsupported_fact`），即在**旧契约**下约 67%。

## ⚠️ 未证明的部分

**改后的成功率尚未测。** 提示词冻结在每次运行签发的
`agent_runs.execution_manifest.system_messages` 里（实测确认：最近两次运行冻的是旧文本），
所以这个改动必须**部署 Gateway 并重新发布**才生效。

本记录写的是「改了措辞 + 单测通过 + 改前有量化基线」，**不是**「已修好」。
部署后要用同一句原话至少连跑 6 次、与 4/6 对比，再决定是否关闭这个问题。

## 下一步

合并 → CD 部署网关 → 重新发布（让新契约进清单）→ 连跑 6 次对比基线。
