# 架构规划与现行规范

本目录仅保留已定稿的架构规划、目录规范与核心运行设计；早期方案草稿与退役架构图已按 [ADR-0343](../adr/0343-prune-redundant-drafts-and-obsolete-archives.md) 移出工作树（历史追溯见 Git 提交 `57c1d788`）。

- [手工环境目录与一键开通规划](2026-11-16-manual-environment-onboarding.md)：不改公司仓库的现行方向、Dify 管理 Tab、实施顺序与验收合同；现役行为与阶段证据从 [试点导航](../README.md#环境目录试点与验收) 与 [当前交付状态](../agents/current-delivery-state.md) 读取。
- [仓库目录规范](repository-layout.md#现行目录规则)：仓库源码、测试、部署与工具的一级归属约束（由 `tests/test_repository_layout.py` 门禁守护）。
- [执行节点队列与调度设计](2026-11-05-runner-queue-design.md)：异步作业调度与队列隔离设计。
- [Snail-AI 专项研究](../agents/snail-manual-onboarding-research.md)：固定上游源码事实与复用边界。

当前系统全景与阶段策略见 [系统架构](../system-architecture.md) 与 [系统策略复核](../system-strategy-audit.md)。
