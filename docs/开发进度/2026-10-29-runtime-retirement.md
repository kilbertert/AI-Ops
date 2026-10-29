# 旧执行器与诊断 CLI 完整退役

目标：按用户确认先清理仓库中的退役模块及上下游，保留独立运维入口，目录迁移后置。
canonical checkout：`<canonical-checkout>`，`main`；任务 worktree：
`<task-worktree>`，分支 `refactor/thin-harness-layout`，基线 `55a284d6`。

## 已完成

- 删除 27 个旧 Python 模块，包括协调器、工具循环、工作区/恢复、Codex、诊断 CLI、设备客户端和旧回退。
- 删除旧标准诊断/设备/共享工具端点、裸 prompt 试用、便携包与专属依赖；运维改为 `aiops-admin --config …`。
- C端快捷检测进入自己的工作流，管家与 Dify 试用按业务绑定走 Dify/Java；无绑定不回退 Python。
- 删除旧费用/占位费/通信报文/Redis 流/doctor 取证，保留健康、分析、身份所需数据访问和权限。
- 更新调用方、测试、OpenAPI、部署脚本、依赖锁、镜像内容锁、README、规格和验证技能。
- 历史数据库/证据保留；新库不创建设备专属表；目录结构未迁移。

验证：全量 **3313 passed / 9 skipped**；包、镜像、真实临时 Gateway、知识同步命令及静态检查通过。
完整证据与限制见[验收记录](../validation/2026-10-29-runtime-retirement.md)，决策见 [ADR-0158](../adr/0158-retire-python-runtime.md)。

## 操作知识自检与下一步

本轮实际运维动作仅为本地构建、依赖安装、临时服务验证；方法已入
[本地构建手册](../agents/local-gateway-build.md)和 [Gateway 验证技能](../../.claude/skills/verify-aiops-gateway/SKILL.md)。
知识同步探针已迁到独立 admin 入口并使用临时回环端口。未执行 SSH、SCP、生产重启或业务数据变更。
源码退役尚未提交/推送/集成；生产配置与远端业务验收仍需独立交付。
本轮完成后再进入目录迁移，不把候选代码或本地测试写成生产已退役。
