# 测试接口文档与 OpenAPI 验证

日期：2026-10-28（北京时间）；分支 `docs/qa-api-guide`，基线 `1370e11b`。
新实现：`public_openapi.py`及Gateway文档GET；业务路由、鉴权、数据库行为未改。
契约：[场景](../acceptance/public-openapi/acceptance.feature) / [QA计划](../acceptance/public-openapi/qa-plan.md)。

## 自动化与执行证据

| 检查 | 结果与范围 |
|---|---|
| 新增测试 | 4条：方法/路径公开边界、匿名文档不影响鉴权、两种个人身份/媒体/分析、参数与任务ID |
| 相关回归 | `test_public_openapi`、FAQ、standard contract、assistant、conversation、cancel、README，共101 passed |
| 规范 | `openapi-spec-validator`通过OpenAPI 3.1校验，JSON Schema验证9份合成HTTP响应 |
| 中文指南 | 23个方法/路径与代码一致，11个JSON合法，13条curl语法/参数拦截检查，4个响应字段与采集结果一致 |
| 临时Gateway | 真进程、临时根、动态回环端口；规范200、查阅页200、匿名FAQ/问答历史401、默认全量规范404 |
| 既有Gateway技能 | health/注册/设备令牌通过；设备令牌访问管理接口仍401；未留进程或数据根 |
| 静态/构建 | ruff check与format（371文件）、repo-map、workflow policy、wheel/sdist通过；上下文锁一致 |

核心样例沿用仓库fixture，共采集19个HTTP交互。业务/目录/模型为合成替身；不是真Dify/Java
或生产业务验收。独立规范校验器仅临时开发使用，不加运行时依赖。
契约测试包括同一URL额外注册其他方法的反例；遗漏的管理方法不得进入规范。

## 失败与修正

首次采样错误地patch了不存在的管家方法，改为既有测试使用的dispatch入口后补齐管家样例。
新增约束测试首次把FastAPI省略null默认值误判为参数漂移；改为比较实际约束。
README模块索引首次漏记public_openapi，补齐后101项回归全绿。
检索状态补记既有limited，健康报告移除未证实的details字段说明。

## 实际环境与边界

只读核对现有API域名`/v1/`转发，两个新GET可沿用；本地完成API-DOC-01/02/03。
API-DOC-04（合并/CD后公网验证）待执行，不提前写通过。方法见[部署核验](../agents/public-openapi.md)。
未完成业务验收：本轮未登录真实用户、未读取真实订单、未调用真实供应商；不表示诊断正确率已验收。
未改资金、业务权限或持久化核心规则，因此未做变异/复杂度专项；本轮重点是文档投影和鉴权边界。
