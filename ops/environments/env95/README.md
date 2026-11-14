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
归档保留在`/var/backups/aiops-95/REV/ATTEMPT`，不删除恢复副本、公司数据库、Redis、TDengine或其他网站资源。
恢复先停新服务，确认旧路径未被占用后从retired目录按原路径移回；或用已校验legacy.tar还原。
SQLite用备份中的gateway-consistent.db覆盖旧库，删除仅属于旧库的-wal/-shm后启动旧服务；此前须确认两服务均停止。
`systemctl daemon-reload`，启动aiops-gateway，健康应回到原0.1.0；新目录保留用于排查。
备份持有凭据必须root0700/0600，不向公开仓库或CI上传。

每次失败尝试保留独立备份/制品目录，可重新执行同修订；首次安装成功后拒绝再次覆盖。实际恢复目录读取`/var/lib/aiops-95/installed-backup`。退休中断可重跑，只跳过已移动且原路径已不存在的项，有冲突即停止。

## 4. 后续同schema升级

完整合并制品上传并核对摘要后运行`bash <制品>/ops/environments/env95/upgrade.sh <制品>`。
升级复用`install.sh --verify-only`校验文件集；要求服务单元不变、schema不变、没有在飞作业，
停机后二次核对再备份状态并原子切换。首次installed-revision/installed-backup保持不变供旧资产退休。
失败自动启动原制品，需轮询健康确认原构建身份；不自动恢复数据库，以免覆盖已经产生的数据。
升级备份位于`/var/backups/aiops-95/upgrades/ATTEMPT`；成功以health构建身份及随后业务验收为准。

## 95独立节点运行栈

使用`deploy/node-runtime/compose.yaml`加本目录`node-runtime.override.yaml`，只在95首次创建独立
平台、元数据库、节点和受控模型出口；节点名称node-95，环境95，合计容器内存上限1408MiB。
源码/JAR/模型代理从合并制品来；node与platform的JAR摘要按`deploy/m2-live-03/images.lock.json`核对。
内部MySQL通过TCP SELECT 1才算就绪；首次临时socket实例不计通过。95小型元库不使用命名时区表，
关闭其初装与performance_schema并限制buffer/cache/连接数；公司业务数据库保持原配置。
制品目录另包含`services/model-proxy`、`deploy/m2-live-03/schema`和初始化脚本，不能复制其他环境运行数据。

初始化沿用`bootstrap_platform.py`，显式给`M2LIVE_ENVIRONMENT=95`、`M2LIVE_EXPECTED_NODE=node-95`、
`M2LIVE_SERVICE=aiops-env95`和`M2LIVE_BOOTSTRAP_ROOT=<本次0700私有临时目录>`；固定临时输入文件名保持原合同。
模型出口/平台端点、CA和供应商型号运行时读取受控配置。默认管理员先轮换，禁用demo，再发独立应用与节点凭据。
失败保留已持久目标口令以便幂等续跑；绝不使用另一环境的应用token。

TLS回调用包内`PYTHONPATH=<制品>/src /opt/aiops-95/current/venv/bin/python <制品>/tools/content/render_node_callback.py`渲染，目标本机Gateway；
宿主系统Python可能低于工具支持版本，因此只借用已部署Python3.13解释器，模块与工具声明仍从同版节点包读取。
CA使用显式openssl配置文件指定扩展，不叠加宿主默认扩展；必须经过节点镜像keytool解析，节点只挂公开信任库。
平台证书SAN同时覆盖platform及平台内部地址，回调证书覆盖实际网桥。先核Docker网段无重叠并记录分配，
再安装本目录`node-runtime.service`/`node-callback.service`为aiops95-node-runtime/aiops95-node-callback。
回调使用aiops身份，nginx二进制按现场核对；用`nginx -t`和节点网络内匿名请求拒绝共同验证。
按已记录入口使用情况配置精确内网访问，不开放公网端口、不改变公司业务网站。

启用前确认没有在飞；启用后检查节点在线、真实三业务、跨环境拒绝与内存峰值。失败停止仅本次新栈，
保留原Gateway及公司服务。资源上限不是峰值验收，未跑真实请求前不能宣称部署容量合格。

### firewalld宿主的内部网桥

Docker20.10在firewalld宿主可能仅把出站网桥加入docker zone，遗漏internal网桥。判据是节点网络内实际TCP，
不是docker ps或宿主可达。用`docker network inspect`获取本栈桥名/子网，再核`firewall-cmd --get-zone-of-interface`。
保存原规则；仅将本项目两个内部桥加入既有docker zone。该zone默认允许宿主访问，须同时加负优先级rich规则：
先允许本栈trial子网到本栈桥地址TCP8788，再拒绝trial/modellink子网到宿主的其他输入。不要把整机zone改为可信。
Docker自身internal/cross-network隔离链继续保留；确认filter_IN规则生效、容器间连接与宿主非回调端口拒绝，
再把本次精确接口/rich规则写入permanent，不做全局reload或runtime-to-permanent。回退只移除本次新增项。
