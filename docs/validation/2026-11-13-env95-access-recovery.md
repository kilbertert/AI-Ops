# 95 管理访问只读复验

基线 `f9a75be1`，分支 `docs/env95-access-recovery`；2026-11-13 UTC。
本片只更新文档，判据为受管 SSH 可执行只读命令及本机健康，未增加长期自动化检查。

| 检查 | 本次结果 | 边界 |
|---|---|---|
| 开发机直连目标 TCP | 6 秒超时 | 不证明目标宕机 |
| 原受管 SSH 三次复验 | 3/3 成功，6.17 / 6.38 / 5.71 秒 | 公钥认证、目标主机公钥校验；未用口令 |
| 进程与本机健康 | active；0.1.0；business_mutations=disabled | 旧服务无提交来源标记 |
| 配置位置 | 服务端配置、Gateway DB 存在，运行目录无 ops/environments | 不等于依赖配置齐全 |
| 旧 Gateway 数据库 | mode=ro、query_only=ON；六表各 0 行，user_version=0，无 dify_workspaces 表 | 没有读公司订单或业务行，不代表公司业务库为空 |
| 配置项盘点 | UPMS/Redis/MySQL/TDengine 项存在；无显式 CPMS/Dify/环境身份项 | 只输出名称及非空数量，未验上游可用性 |
| 历史失败 | 错误指向中继连接超时 | 中继后来在线，不足以证明历史是休眠或重启 |

原始证据保留于私有运维记录：`env95-current-ssh`、`env95-ssh-recheck`、
`windows-relay-status`、`env95-configuration-inventory`、`env95-effective-inventory`；主机地址与凭据不复制入仓。
可复执行的 SSH、进程与健康方法已写入[95 手册](../agents/env-95-facts.md#2-怎么连区分目标与中继故障)。

`uv run pytest tests/test_repository_layout.py tests/consumer/test_consumer_live_documents.py -q`
36 项通过；`ruff check .`、`ruff format --check .`、`git diff --check` 通过。CI 待执行。
没有修改生产行、配置、SSH 路径或服务；无须生产回滚。
没有执行真实用户问答、订单诊断或设备离线诊断，**未完成业务验收**。
