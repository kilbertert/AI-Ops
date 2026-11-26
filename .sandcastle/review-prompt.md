# TASK

审查分支 {{BRANCH}} 对 issue #{{ISSUE_NUMBER}} 的实现：{{ISSUE_TITLE}}。
本轮为只读审查，不修改候选实现或长期测试，不提交、推送或修改 GitHub 状态。

# CONTEXT

先读取 issue 的行为要求，再读取 `GLOSSARY.md`、`.sandcastle/CODING_STANDARDS.md` 和相关 ADR。

<issue>

{{ISSUE_BODY}}

</issue>

<diff-to-main>

!`git diff origin/main...HEAD`

</diff-to-main>

# BOUNDARIES

- 候选：分支 {{BRANCH}} 的当前 HEAD（上方 diff）。
- 允许的操作：只读检查、运行既有测试与临时验证；禁止修改候选实现、长期测试或 GitHub 状态；
  禁止生产写入、故障注入与超出沙箱额度的外部调用。
  本容器无 GitHub 凭据，gh/git push 必然失败——这不是要绕过的故障，是身份边界。
- 凭据边界：禁止读取任何凭据或认证配置路径（`~/.config`、`~/.claude`、
  `cliproxyapi` 配置、`settings*.json`、`.env*`、SSH/agent 私钥与令牌文件）。
- 证据：全部写入本轮 stdout 回报（调用方按日志消费），不写仓库文件。
- 时间预算：由调用方 AFK_RUN_TIMEOUT 限定；到期未覆盖项如实列入未验范围。

# EXECUTION

1. 对照 issue 需求、`.sandcastle/CODING_STANDARDS.md` 与 Economy 阶梯审查改动。
2. 针对可疑逻辑、边界条件或测试替身与真实上游的差异，用既有检查或只读命令核实并给出具体反例。
3. 记录已确认缺陷、预期来源和实际输出；未执行的具体假设单列“待复现”。
4. 回报候选版本、实际检查内容、发现与未验范围。

发现未解决的实质缺陷、核心判据缺失或必要范围未验时输出 `<promise>BLOCKED</promise>`；
否则输出 `<promise>COMPLETE</promise>`。
裁决标记必须独占一行并位于输出的**最后一行**：正文引用、代码样例里的标记不充当裁决，
最后一行不是独立裁决标记的报告按无裁决处理。
