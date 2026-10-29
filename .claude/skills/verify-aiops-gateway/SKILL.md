---
name: verify-aiops-gateway
description: 在一次性数据根运行真实 Gateway，验证启动、鉴权、公开规范及退役路由。
---

# 验证 AI-Ops Gateway

从任务 checkout 运行：

```bash
uv run python .claude/skills/verify-aiops-gateway/scripts/observe.py --out dist/verification/gateway
```

脚本创建临时配置和数据根，清除继承的 `AIOPS_*`，使用动态回环端口，
启动真实 `aiops-gateway serve`，执行 HTTP 检查并关闭自己启动的进程。
不使用真实 `AIOPS_DATA_HOME`，不需要诊断 CLI、设备注册码或设备令牌。

通过条件：`/health` 报告只读；问答/管理接口匿名访问为 401；公开 OpenAPI 为 200；
旧 `/v1/runs`、`/v1/enroll`、标准诊断与旧工具端点为 404。记录请求、响应与失败。
完成时进程和临时根清理，证据保存在指定输出目录。

范围见 [features](features/README.md)。本地检查不能证明远端 Dify/Java 执行、
知识库活性、故障诊断准确率或生产部署；涉及这些行为时须另外验收并如实报告缺口。
