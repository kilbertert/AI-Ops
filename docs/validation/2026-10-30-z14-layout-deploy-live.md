# 目录归位三片的上线验证

本文件记录通用/会话、Gateway、工具与集成三片目录归位**合并之后在 41 上的实际状态**。
三片的实现与测试证据在各自记录里（[通用与会话](2026-10-30-common-conversation-layout.md)、
[Gateway](2026-10-30-gateway-package-layout.md)、
[工具与集成](2026-10-30-tooling-layout.md)）；这里只回答“线上跑的是什么”。

## 修订与制品

| 片 | 合并提交 | 标签 | 部署 |
|---|---|---|---|
| 通用/会话 | `e33c1d6c` | `v1.0.11` | CD `40990314106` 成功 |
| Gateway/管家会话 | `041a1eb0` | `v1.0.12` | 该片单独的 CD `40992252464` 被后一次合并的 CD 取消；内容包含在下一行工件里 |
| 工具/集成 | `83ba786a` | `v1.0.13` | CD `40993606651` 成功 |

`041a1eb0` 那次 `cancelled` 不是漏部署：每次 CD 部署的是它自己的 `GITHUB_SHA`，
而 `83ba786a` 是它的后继（含全部 Gateway 改动），所以取消是正常收敛。
判断依据不是 run 的结论，而是 `/health` 的 `version`；这次踩到的判读方法与队列诊断
已补进[运行手册 §2.1](../agents/env-41-runbook.md)。

检查时点生产版本：`1.0.0+96a06f6f775a`（另一会话 #1195 在本次之后合并并部署，
位于上述三片之上），`aiops-gateway-41.service` **active**，`NRestarts=0`，
`ActiveEnterTimestamp` 2026-10-30 11:20:38 CST。

## 只读验证（在 41 上执行）

```bash
dev-host ls 41 /opt/aiops-41/src/aiops_diagnostics
dev-host exec 41 --allow-service-exec -- 'runuser -u aiops41 -- /opt/aiops-41/.venv/bin/python -I -c "..."'
dev-host exec 41 --allow-service-exec -- 'runuser -u aiops41 -- /opt/aiops-41/.venv/bin/aiops-admin --help'
dev-host exec 41 --allow-service-exec -- 'curl -sS --resolve api.mall.qushiyun.com:443:127.0.0.1 ...'
```

- 包根只余 `__init__.py`、`admin_cli.py`、`gateway_server.py`、`tdengine_proxy.py` 与
  各职责子包；`gateway_runtime.py`、`gateway_api.py`、`operator_cpms.py`、`faq.py`
  这类根层旧模块**不存在**。
- 以服务身份隔离模式导入 `aiops_diagnostics.gateway.gateway_runtime`、`common.config`、
  `conversation.faq`、`operator.operator_conversation` 全部成功，解析到
  `/opt/aiops-41/src/aiops_diagnostics/gateway/gateway_runtime.py` —— 线上跑的是新包，
  不是旧根模块的残留。
- 独立运维命令 `aiops-admin --help` 正常，说明运维入口仍在。
- 公网入口 `GET /v1/shortcuts`（经 41 回环 + 真主机名与证书）返回
  `401 {"error":{"code":"INVALID_ACCESS_TOKEN",...}}`：路由表与鉴权契约按预期工作。

## 未证明的范围

- 三片只移动模块、资源与引用，**没有重跑真实 C 端/管家业务验收**；因此本文件不构成
  业务准确率结论。真实 Dify/模型驱动链路依赖远端服务，本地与本次只读检查都不覆盖它们。
- 生产备份、回滚点沿用各片 CD 前的既有备份；本次未做依赖环境变更，未改动
  `/etc/aiops-41/*`、数据库或凭据。
