# Gateway 数据库检查与升级

适用1.7.0候选，schema版本1；生产交付仍按标准PR/CD。作者、独立Hack和一致副本检查已完成，
1.7.0尚未上线；副本检查不代表生产升级完成。

## 检查与迁移

使用目标制品的 `aiops-admin`，显式指定数据库文件；不要让环境缺省选到另一个空库。

```bash
aiops-admin schema check --db /path/to/gateway.db
aiops-admin schema migrate --db /path/to/gateway.db
aiops-admin schema check --db /path/to/gateway.db
```

`check` 用SQLite只读URI和只读事务，不创建文件、不迁移。版本0只说明形状可升级，
不代表已达到版本1。当前版本完整检查列/type/notnull/default/pk、外键、唯一约束、
索引名称/列序/排序/collation/partial，及PRAGMA未表示的CHECK等声明。
未知版本、错误库、未知默认/约束/索引冲突拒绝，不能手工改user_version绕过。
额外历史表保留并报告；唯一允许的默认差异是旧 `chat_dify_runs.scope_json DEFAULT ''`。
迁移失败不改逻辑schema、行和版本；首次空文件与WAL模式可能保留。

## 一致备份与副本演练

在线库不能裸 `cp gateway.db` 忽略WAL。以下仅为方法，路径由授权操作选择：

```python
from pathlib import Path
from aiops_diagnostics.gateway.gateway_schema import rehearse

report = rehearse(Path("/path/to/gateway.db"), Path("/path/to/new-backup.db"))
print(report)
```

`rehearse` 独占创建备份，拒绝覆盖；用SQLite backup取得含WAL已提交行的一致视图。
备份保持旧版，另建临时副本做migrate/check/integrity/FK，完成后删除临时副本。
只输出结构，不输出业务行、凭据或FK失败行。备份仍含业务数据，按原数据库权限和保留策略管理。

## 标准CD的失败边界

`deploy/deploy-41.sh`沿用dev-host远端执行：在版本对应的候选源码上预检控制台、
保存到本次代码备份内 `schema/gateway.db`，以服务身份做副本迁移和完整设置校验。
本次备份目录 `B` 由root创建并保留其owner，显式设置group为服务组、mode为0710，
使服务组仅能按已知路径穿过它；`B/schema`为服务owner、0700。不能依赖历史备份的755模式，
umask 027/077会让新目录缺少服务身份所需的遍历权限，子目录chown不能补救祖先拒绝。
只调整本次B及其schema，不改变备份祖先、源码备份或其他备份的权限；
既有备份祖先必须已允许服务身份遍历，不满足时应拒绝并另行处理宿主权限，不能递归放宽。
设置校验的database与同库路由路径指向临时副本，避免其内部store构造写真实库。
任何预检失败都发生在rsync替换源码和重启之前，旧服务继续运行。
实际迁移也在替换源码前执行；候选启动再次严格校验结构。
后续应用启动失败只恢复代码，保留数据库；禁止自动用备份覆盖可能已有的新业务写入。
代码恢复后若旧程序再次写入旧token字典，新程序启动维护会重新扫描清理，独立get始终只返回ID。

部署回执必须记录代码revision、schema版本、备份身份、检查结果和时间。
已执行的副本、旧程序兼容和独立Hack边界见本轮验收记录；上线验收由交付会话负责。

## 本地复验

`uv run pytest tests/gateway/test_gateway_schema.py tests/gateway/test_database_initialization.py tests/test_cd_deploy_scripts.py`
使用临时库与dev-host替身，不访问服务主机。真实临时Gateway启动命令见[QA计划](../acceptance/gateway-schema/qa-plan.md)。
镜像用 `M2LIVE_GATEWAY_IMAGE=<任务标签> M2LIVE_DIST=<证据目录> deploy/m2-live-03/build-image.sh`，
容器只在本机无网络运行导入及临时数据库检查，保留 `gateway-image/identity.json`。
