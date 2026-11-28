# AFK 规划与仓库交付

## 规划阶段

`idea → /grill-with-docs → /to-spec → /to-tickets → implement → review → merge`

- `/grill-with-docs` 收敛术语与决策。没有待澄清问题后报告 `GRILLING_COMPLETE`、请用户确认并结束本轮；确认不授权下一阶段。
- `/to-spec` 将已确认需求记录在 GitHub 规格 issue；`/to-tickets` 按批准顺序拆分原生子 issue 与依赖。
- 只有实际实现得到明确授权后才开始改动。按 [交付约定](agents/actions-delivery.md) 在任务分支提交，经过 PR、确定性检查与适用的评审。合并不等于部署或验收。

## 当前仓库的交付门禁

[#1833](https://github.com/kilbertert/AI-Ops/pull/1833) 后，原仓内 `.sandcastle/`、`.afk-bootstrap.json` 和 Sandcastle 依赖已移除。本仓没有旧 `pnpm ralph` / `pnpm afk` 脚本或 `agent-implement.yml` 工作流。不要通过恢复旧目录或脚本修复门禁，也不要把历史执行工具的 provider 配置当作本仓的现行前置条件。

- [`afk-policy.yml`](../.github/workflows/afk-policy.yml) 调用 `kilbertert/fleet-kit/actions/policy-gate@v1`；[`.fleet-kit.json`](../.fleet-kit.json) 启用 ADR 检查并配置本仓的冲突面校验。
- [`ci.yml`](../.github/workflows/ci.yml) 的 `verify` 与 `Workflow policy` 是必需检查。不要用顶层 `paths` 过滤跳过必需检查；草稿转为可评审会重新触发。
- [`spec-tree.yml`](../.github/workflows/spec-tree.yml) 提供规格树提示，不替代必需检查。[Sync Open PRs](../.github/workflows/sync-open-prs.yml) 是状态汇总与受控单 PR 同步，不会合并 PR。
- 门禁失败时从失败步骤定位：共享实现查 fleet-kit 及对应运行日志，本仓自定义检查查 [`conflict-surface.mjs`](../tools/verification/conflict-surface.mjs)。不为通过门禁而放宽检查、改保护规则或绕开 PR。

术语见 [GLOSSARY](../GLOSSARY.md)，设计取舍见 [ADR 索引](adr-index.md)，当前部署和验收证据见 [交付索引](agents/current-delivery-state.md)。历史沙箱与评审记录用于了解既往做法，不是现行工作流接线。
