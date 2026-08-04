# 生产访问边界

TDengine Community Edition 3.4 允许非超级用户写入已有数据库，并且拒绝
`GRANT READ`。因此生产诊断不得通过 SSH 直接转发到 6041 端口。

`aiops-tdengine-readonly-proxy.service` 运行在数据库主机 loopback 接口上，只接受本项目发出的精确
`SHOW STABLES` 和有界 `SELECT` 语句。上游 TDengine 凭据只保留在该主机。工程师使用的 SSH key 只能转发到代理、MySQL 和 Redis 诊断端点。

代理必须使用 root 所有的环境文件部署。客户端凭据和上游 TDengine 凭据都不得放入本仓库。回滚生产访问路径时，必须一并移除 service、service account、上游用户和 SSH `permitopen` 配置。
