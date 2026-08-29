# AFK 可信交付 QA 计划

## AFK-B10 可信工作流静态门

- 环境：AI-Ops AFK 任务分支。
- 前置：模板 1.1.1 已部署。
- 数据：六条 AFK 变更 workflow。
- 动作：运行 `node .sandcastle/policy-check.mjs workflows`、actionlint、ShellCheck，并与 afk-bootstrap 的受管文件逐字节比较。
- 预期：同仓库 owner gate、可信 controller、候选只读 token、干净 delivery checkout 和 AGENT_PAT fail-closed 全部通过，受管文件无漂移。
- 清理：无。

## AFK-B11 Bundle 状态机回归

- 环境：afk-bootstrap 临时 Git 仓库测试。
- 前置：模板测试 checkout 可用。
- 数据：落后的本地 main、前进的 origin main、合并结果和远端竞态。
- 动作：运行 afk-bootstrap 的 `test/trusted-pr-delivery.sh`。
- 预期：基线被重置、bundle 原样保留提交、远端竞态被拒绝。
- 清理：测试 trap 删除临时仓库。

## AFK-B12 Live canary

- 环境：AI-Ops self-hosted runner。
- 前置：加固 workflow 已合并，runner 与只读 token、AGENT_PAT 在线。
- 数据：仓库所有者创建的一次性 PR。
- 动作：添加 `agent:review` 并检查 workflow、review、标签和交付分支。
- 预期：使用当前 main，通过 controller/candidate/delivery 隔离完成审核且没有 blocked 标签。
- 清理：关闭一次性 PR，删除临时分支和标签。

AFK-B10：已通过，时间 `2026-08-30T03:31:15+08:00`，提交
`7380fe8c12c738c4f365db8b13c758d204ebed90`，Linux x86_64，Python
3.13.13、Node v24.15.0、actionlint 1.7.12、ShellCheck 0.11.0。证据：`uv run
ruff check .`、`uv run ruff format --check .`、`uv run pytest -q`、compileall、
`uv pip check`、policy checker、actionlint、ShellCheck、`git diff --check` 全部通过；
受管文件与模板逐字节一致。

AFK-B11：已通过，复用模板提交 `84e9537c661f676f68951eb3e7480472b91ff728` 的
`bash test/trusted-pr-delivery.sh`，覆盖 stale main、bundle 提交保留和远端竞态拒绝。

AFK-B12：待模板合并后在 AI-Ops 在线 self-hosted runner 执行 owner-authored
`agent:review` canary，保留 workflow URL、review、标签和清理证据后再标记通过。
workflow YAML 不适用复杂度或 mutation 工具；安全状态机由模板动态测试覆盖，本仓负责静态门和部署一致性。
