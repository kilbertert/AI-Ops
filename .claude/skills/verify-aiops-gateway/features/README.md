# Gateway 本地验证范围

统一入口为 `scripts/observe.py`：一次性配置/数据根、动态回环端口、真实进程与 HTTP。

| 范围 | 判据 |
|---|---|
| 服务启动 | `/health` 为 200，`business_mutations=disabled` |
| [鉴权](auth-boundary.md) | 现役问答和管理接口匿名请求为 401 |
| 用户文档 | `/v1/openapi.json` 为 200，不列出已退役的标准诊断 |
| 退役 | 注册、设备运行、旧标准诊断、旧 Python 工具端点均不存在 |

凭据、模型、Dify、Java 节点和真实业务数据未在该脚本中模拟为已验收。
