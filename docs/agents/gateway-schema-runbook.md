# Gateway 数据库检查与升级

适用已交付的1.7.0、schema版本1；PR #1466已合并并通过标准CD上线。
现场检查与原入口的成功/失败边界见[真实交付回执](../validation/2026-11-11-gateway-schema-live-receipt.md)，
此前作者/独立Hack/副本结果仍保留各自时点，不与现场证据混算。

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
已执行的副本、旧程序兼容和独立Hack边界见[作者验收](../validation/2026-11-11-gateway-schema-authority.md)；
线上验收另列，不能仅凭CD日志宣布原入口问答通过。

## 部署后只读核验

按项目声明的既有`dev-host`受控入口操作，以真实服务身份和现役Python/包根执行；路径取本次部署记录，
不从会话历史猜测。先核health的版本/合并SHA、代码制品身份及schema-preflight/schema-migrate/console-preflight结果。
只读验收调用`check`/`integrity`，不重复调用`migrate`、`ensure`或`startup`。

```bash
set -eu
# GATEWAY_DB由操作者填入本次核对的绝对路径；先确认是正式库而非新建空库。
: "${GATEWAY_DB:?填写已核对的数据库绝对路径}"
case "$GATEWAY_DB" in /*) ;; *) echo "需要绝对路径" >&2; exit 1 ;; esac
test -f "$GATEWAY_DB"
aiops-admin schema check --db "$GATEWAY_DB"
GATEWAY_DB="$GATEWAY_DB" python - <<'PY_CHECK'
import os
from pathlib import Path
from aiops_diagnostics.gateway import gateway_schema

path = Path(os.environ["GATEWAY_DB"])
assert path.is_absolute() and path.is_file()
gateway_schema.integrity(path)
print("integrity/FK: passed")
PY_CHECK
```

`integrity`以只读事务执行integrity_check与foreign_key_check，失败不打印含业务键的行。
退出失败就是核验失败，不能只截取先前成功的版本输出；保留脱敏错误分类，原始数据继续私有。

- 对照备份与线上库时分别调用只读`check`/`integrity`，先确认哪个是迁移前备份。备份应保持原schema版本，
  1.7.0本轮为备份0、线上1；不要为了“版本一致”升级备份。后续升级以各自变更前版本为准。
- 用`namei -l`和`stat`核对备份祖先遍历、本次父目录0710/schema目录0700/备份文件0600及owner/group，
  并以服务身份实读备份。只记录权限判定，不修改历史祖先权限，也不将root可读当服务身份可读。
- 稳定表比较先列明表集合、比较时点和列排序方式，在各自只读快照内比较原列逻辑行数/摘要。
  表的定义、列序规范与排序规则保持一致，不比较SQLite文件字节；摘要和原行保留在私有证据。
  本轮集合为agents、agent_versions、shortcuts、shortcut_versions、chat_route_bindings、dify_app_registry、
  operator_apps、dify_workspaces、consumer_endpoints、consumer_knowledge_grants，共10张。
  不把这些稳定表不变扩大为运行日志/审计表在不同时间没有合法写入。
- 现场构造trace只观测已有当前版本库，不制造生产锁。记录每类耗时与SQL形状，区分只读BEGIN与写租约；
  本轮构造无BEGIN/DDL/写语句。受控持锁竞争仍在临时库或获准副本验证。

## 原入口结果与范围

通过正常登录的原Dify入口提问，再在原logs查看同一运行与部署版本；只记录匿名关联是否一致、终态、
耗时和错误分类。页面/日志HTTP200不代表问答成功；工具成功而复核拒绝时，必须保留最终failed。
按端到端页面耗时、服务端run、tool与review分别记时，不能将重叠/不同范围耗时相加当总时延。
若第二次缩小问题范围后成功，原失败样本继续保留，两种scope不能合成“100%准确”或冒充原问法复验。
不为验收通过关闭质量门、切模型或改提示词；业务根因与长期稳定性单独判断。
取证通道沿用[多租户手册](operator-cpms/multitenant.md)的受控动态回环方法，结束清理并确认无监听。

## 本地复验

`uv run pytest tests/gateway/test_gateway_schema.py tests/gateway/test_database_initialization.py tests/test_cd_deploy_scripts.py`
使用临时库与dev-host替身，不访问服务主机。真实临时Gateway启动命令见[QA计划](../acceptance/gateway-schema/qa-plan.md)。
镜像用 `M2LIVE_GATEWAY_IMAGE=<任务标签> M2LIVE_DIST=<证据目录> deploy/m2-live-03/build-image.sh`，
容器只在本机无网络运行导入及临时数据库检查，保留 `gateway-image/identity.json`。
