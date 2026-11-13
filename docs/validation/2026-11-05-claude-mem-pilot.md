# Claude-Mem 试点验证

日期：2026-11-05。AI-Ops 分支 `chore/claude-mem-pilot`，初始基线 `09772a76`，
验证期间随 origin/main 快进至 `4951db77`。这是开发工具接入，不是业务功能验收。

范围由用户确认：保留双端入口，摘要使用 `claude-deepseek` 通道，首批仅 AI-Ops。
验收合同、操作方法见[手册](../agents/claude-mem-runbook.md)。

## 结果

| 检查 | 结果 | 边界 |
|---|---|---|
| 两种 hook 来源写入与相互读取 | 通过 | 合成 ORBIT-731 / NEBULA-842；真实 DeepSeek 摘要，不是双原生 CLI 自动接力 |
| 不同项目 context 隔离 | 通过 | 两个独立 Git 仓库；同仓库子目录不隔离 |
| 实际摘要模型 | 通过 | 已保存观察标记 `deepseek-v4.1-flash`，配置别名 `deepseek-latest` |
| 原生 Claude 采集 | 通过 | Read 合成 fixture 后生成观察；MCP `get_observations` 可取回 |
| 原生 Claude → Codex MCP 检索 | 通过 | 新 Codex 会话调用插件 get_observations，正确返回 CEDAR-985 / 43，未翻读日志 |
| 原生 Codex 自动采集 | 通过 | 用户确认后 `hooks/list` 返回 7 个 `trusted`，失败工具事件与 Stop 摘要自动保存 |
| Codex 自动摘要 → 新 Claude 启动注入 | 通过 | MAPLE-986：正确回答 11、测试未运行、空输入拒绝；接收方未读文件或调用工具 |
| 无工具 Claude print 退出摘要 | 未通过本轮观察 | 记录了 prompt，未观察到自动摘要；显式 hook 的 summarize 能保存 |
| loopback 服务与启动配置 | 通过 | 用户级 systemd，12112，健康/就绪通过，已重启验证 |
| 既有全局 hooks | 通过 | 与安装前备份比较未变；仅新增插件/marketplace 配置 |

私有证据在 `~/.local/state/claude-mem-adoption/`，包括 trial-results、MCP 回执、原生 CLI
输出、hooks-list、安装日志与配置备份；不提交原始会话或凭据。

## 失败与修复

1. 第一条重复观察被模型跳过，不能把调用成功当作保存成功；加入新的合成改动并核对实际存储。
2. Claude hook 的适配器未映射 `last_assistant_message`，合成 Stop 要提供合成 transcript 才保存。
3. 安装器拒绝 `--model deepseek-latest`；重跑不带此参数，恢复已验证运行配置。
4. 安装器清空网关凭据；从指定通道的受控 settings 重新写入私有 `.env`，重启核对 Gateway。
5. 治理安装器两次因本轮 worktree 生命周期失败并回滚；清理已合并 worktree、更新另一个
   任务基线后，重新运行完整 `bash install.sh` 成功，没有单独投射端口文件。
6. Codex 只读沙箱执行 shell 遇到 `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`；
   未绕过保护，改用只读 MCP 完成合成任务。该失败本身被自动采集，宿主沙箱问题未修复。

## 交付与检查

端口单行经用户批准公开，治理 [PR #82](https://github.com/kilbertert/server-development-consensus/pull/82)
已合并：`7c25631ea29295212234c948ee1be4eb3551af94`；必需 CI 通过。
Codex 自动审查额度耗尽；Devin 已请求但未返回结果，均未伪记为通过；无未解决线程。
origin/main 已核对端口，规范 checkout 已快进；远端任务分支、本地分支与 worktree 均已清理。

正式 worker 再验：原生 Claude 读取 CEDAR-985 合成 fixture，保存观察的模型为
`deepseek-v4.1-flash`，项目键为 `kilbertert/AI-Ops`；MCP 返回同一标识及校验码 43。
正式记忆中这一条明确标记为安装合成验收，不是业务结论。

用户信任后复验：Codex 正常执行 MCP get_observations，Stop 自动生成 MAPLE-986 摘要。
新 Claude 会话的原生 SessionStart hook 包含该任务；无工具回答正确给出重试上限 11、
测试未运行和下一步验证空输入拒绝。证据为私有 `hooks-trusted-list.json`、
`codex-trusted-mcp-smoke.jsonl`、`claude-resume-codex-context.jsonl` 与 `native-handoff-result.json`。
这些证明实际双端采集/共享路径可用，不证明完整长会话无损，也不证明业务故障诊断正确率。

AI-Ops 文档检查通过：目录与 README 关联测试、Ruff 静态检查、Ruff 格式检查
（1,627 个文件符合格式），以及本地链接/空白检查。本轮没有新增长期测试或业务代码，
未完成业务验收。
