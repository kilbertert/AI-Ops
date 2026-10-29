# Gateway 部署与接口入口

Gateway 负责公司身份、授权范围、业务发布绑定、任务与结果交付；C端工作流和 Java 节点负责业务执行。
用户接口见[测试指南](testing-api-guide.md)，现役路线见[架构](system-architecture.md)。

## 启动与管理

- 服务启动：`aiops-gateway serve`，读取私有 `production.env` 与 `AIOPS_GATEWAY_ENVIRONMENT`。
- 管理操作：`aiops-admin --help`，开户/知识同步/发布管理保留，命令入口独立于运行服务。
- 设备注册、设备运行、标准 Python 诊断、裸 prompt 试用和诊断 CLI 已退役，见[清单](runtime-retirement.md)。
- 本地 HTTP 验证：[Gateway 技能](../.claude/skills/verify-aiops-gateway/SKILL.md)。

## 部署

代码从合并版本构建为制品，按[现有环境手册](agents/env-41-runbook.md)部署；本地镜像与锁文件
按[构建手册](agents/local-gateway-build.md)产生。模型依赖改变须同步锁定依赖环境，不能只复制源码。
现役 provider 必须可直接处理有界单次请求；旧 Responses 适配器是否可停需另行核使用流量。

保留已有数据库与历史证据；新数据库不创建设备专属表。分析和 Dify/节点试用沿用既有作业存储。
升级前确认 C端与管家的发布绑定；配置缺失明确拒绝，不会回退到旧 Python 诊断。
源码退役、生产切换和旧服务停止是不同的交付步骤，本分支未执行后两项。
