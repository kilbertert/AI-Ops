# Claude-Mem 双端上下文试点

日期：2026-11-05。分支：`chore/claude-mem-pilot`；当前基线：`4951db77`。

目标：保留 Claude Code/Codex 入口，用 Claude-Mem 接手进行中的 AI-Ops 工作。
用户确认摘要沿用 `claude-deepseek`，范围先限 AI-Ops。

已完成：固定 15.9.0 官方安装、DeepSeek 真实摘要、hook 双向读取与项目隔离、Claude 原生
采集/MCP 检索、原生 Codex 取回 Claude 观察、loopback 用户服务、现有 checkout 的本地启用配置。
端口通过治理 PR #82 合并并经完整安装器发布。

用户已完成 Codex 7 个 hooks 信任；Codex 自动摘要被新 Claude 启动上下文接收并正确复述。
原生双端共享合成验收完成；长任务保真和无工具 Claude print 自动摘要不在通过范围内。
文档检查见[验证记录](../validation/2026-11-05-claude-mem-pilot.md)。

操作知识已整理进[手册](../agents/claude-mem-runbook.md)，含新 worktree 启用、升级、凭据刷新与回滚。
本地安装证据与秘密留在私有状态目录；AI-Ops 文档未提交或推送。下一步观察日常长任务接力质量。
