# 95独立Gateway重建

本目录是旧95服务到当前合并制品的首次重建资产，非通用全舰队安装器。
Gateway、登录代理与数据根的环境隔离合同见环境登记手册。服务器只接收构建制品，禁止在服务宿主开发。

## 1. 开发机准备

从合并修订运行`build.sh REV OUT.tar.gz`；它注入构建身份并打包Python3.13/Linux离线依赖。
用`dev-host cp 95 ... --artifact-sha256 ...`上传，目标目录按`.dev-host.toml`登记。
制品不含公司配置/凭据；安装前在`/etc/aiops-95`准备0600 aiops所有的gateway.env、server.env及registry目录。
配置引用必须指向本环境server.env，登记Gateway为本机8788，CPMS为本机业务网关的charging-pile路径。
UPMS使用现场确认的本机地址；旧诊断配置仅复用已核验的数据源，禁用未知/失效连接，不复制41身份配置。

环境变量至少指定AIOPS_GATEWAY_ENVIRONMENT=95、AIOPS_GATEWAY_EXECUTION_ENVIRONMENT=95、
AIOPS_GATEWAY_BIND=127.0.0.1、AIOPS_GATEWAY_PORT=8788、
AIOPS_GATEWAY_SERVER_CONFIG_FILE=/etc/aiops-95/server.env、
AIOPS_GATEWAY_DATA_HOME=/var/lib/aiops-95/gateway、AIOPS_GATEWAY_ENVIRONMENT_REGISTRY_FILE=/etc/aiops-95/registry/catalogue.json。
登记只启用已正常登录并核对身份的测试租户；不由客户端tenant-id覆盖身份。

## 2. 安装与检查

先盘点旧服务及两个/v1反代的实际使用，记录窗口、备份范围与回退；旧服务保留到候选健康通过。
`install.sh <展开制品>`检查摘要，创建独立venv和新数据根，备份明确旧路径，再以aiops身份起临时候选。
候选健康版本须等于制品修订；随后停旧服务、启动新服务占用原8788，不修改其他网站反代。
失败自动启动旧服务。成功只代表Gateway安装；真实身份、订单、离线与模型链必须另验。
安装器使用旧8788作为继承端口，不新增公网端口或转移公网域名。

## 3. 退休与恢复

新实例达到本轮验收条件后才执行`retire.sh REV`，停止/禁用旧服务并把旧代码、配置、数据移出原运行路径。
归档保留在`/var/backups/aiops-95/REV`，不删除恢复副本、公司数据库、Redis、TDengine或其他网站资源。
恢复先停新服务，确认旧路径未被占用后从retired目录按原路径移回；或用已校验legacy.tar还原。
SQLite用备份中的gateway-consistent.db覆盖旧库，删除仅属于旧库的-wal/-shm后启动旧服务；此前须确认两服务均停止。
`systemctl daemon-reload`，启动aiops-gateway，健康应回到原0.1.0；新目录保留用于排查。
备份持有凭据必须root0700/0600，不向公开仓库或CI上传。
