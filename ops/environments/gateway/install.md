# 最小接入模块受管安装

适用P3；[QA D12–D14](../../../docs/acceptance/environment-directory-qa-plan.md)与
[ADR0272](../../../docs/adr/0272-managed-gateway-install.md)定义边界。只安装Gateway，不安装共享平台。
`installed`只代表基础进程与版本就绪；网络、权限、索引及三功能未通过时不得激活租户。

## 前置与制品

目标须在私有主机清单登记并通过`dev-host`断言；已有项目非登录账号，Linux x86_64及系统Python3.11–3.13。
已分配独立回环端口；安装器检查占用但不负责登记端口。至少1GiB可用内存及1GiB磁盘是保守预检下限，
不是业务容量结论。旧Gateway和公司服务继续运行，新增自有单位须在当前任务授权内。
首版要求代码、配置、状态与备份父目录同文件系统，写入前核设备号；独立挂载布局明确拒绝，
不在恢复途中发现EXDEV后留下半移动状态。安装器包与应用包必须同一合并修订。

```bash
bash ops/environments/gateway/build.sh <已合并SHA> 3.12 /private/gateway.tar.gz
bash ops/environments/gateway/build-installer.sh <同一SHA> /private/installer.tar.gz
```

两个固定包及受控配置分别通过`dev-host cp --artifact-sha256`传输；外层与包内摘要核对后解包到root控制的私有暂存区。
`runtime_check.py`由安装器复制为无秘密代码供服务身份读取；不要给服务账号开放含配置秘密的暂存目录。
配置是root所有0600普通文件，不允许符号链接。应用配置值不输出；摘要、版本与环境可入私有部署账本。
配置不得覆盖安装器确定的环境/数据根/配置根/数据库/密钥路径；首次先用无业务连接的空配置验证安装生命周期。
有效业务配置应来自管理目录的受控投影；此安装器不自行发现凭据、创建账号或修改公司索引。

## 执行与重跑

```bash
python3 <已核验安装器>/install.py install --environment <环境> --user <服务账号> --port <登记端口> \
  --python /usr/bin/python3.12 --archive /private/gateway.tar.gz --sha256 <应用包摘要> \
  --config /private/input.env --config-sha256 <配置摘要>
```

固定单位`aiops-access-<环境>.service`，代码`/opt/aiops-access-<环境>`，配置`/etc/aiops-access-<环境>`，
状态`/var/lib/aiops-access-<环境>`，冷失败资料`/var/backups/aiops-access-<环境>`。
首次拒绝任何同名既有路径/单位；不猜测或收养历史实例。同计划重跑验证现役版本后返回unchanged，不重启。
升级只允许相同配置摘要与Python小版本、同schema、零在飞任务；不自动迁移或修改配置。

候选在私有网络命名空间、新状态根、MemoryMax384MiB/CPUQuota50%限制下启动并验证。
正式自有服务只监听回环，状态根唯一可写，启动后核health版本及主进程实际监听socket。
不安装Nginx、不切旧入口、不激活目录；服务有长期资源上限，不把短时健康检查当负载验收。

## 失败与恢复

安装按环境加锁，pending账本区分准备与切换；中断后install拒绝继续，先核现场再执行：
首次将已写pending的临时目录原子改名为正式根；更早中断只留下私有暂存目录，不占用安装根。
临时目录精确名称记入pending，恢复只清本次路径，不按`trial-*`清理未知证据。

```bash
python3 <同版已核验安装器>/install.py recover --environment <环境> --user <服务账号> --port <登记端口>
```

准备失败时原实例继续；切换失败时仅停止本安装器的自有单位，恢复旧制品并读回旧版本。
状态不被旧快照覆盖，备份保留；schema或配置漂移、停止未确认、旧版本不健康时保留pending并明确失败。
冷失败将自有状态/配置/代码根移到新备份目录，不删业务证据；修复原因后可重新冷安装。
已成功安装不提供隐式卸载；退役须先观察实际流量，按独立任务授权处理。

## 验证与局限

```bash
uv run pytest tests/tooling/test_gateway_install.py tests/tooling/test_gateway_candidate.py
```

本地以真实文件与SQLite、主机命令替身证明恢复边界；systemd、权限、网络namespace与真实版本读回须目标现场验证。
预检和安装器只处理自有资产，不承担公司API修复、业务网络开通、跨schema升级、凭据轮换或高可用切换。

基础现场演练只在本轮新建、空业务配置、没有接入流量的自有实例上执行。开始前保存旧Gateway的
`MainPID`、启动时间、health及已有容器身份快照；结束逐项比较，不将其他并行变更归因于安装器。
运行相同install命令前后读`systemctl show <自有单位> -p MainPID`，同时要求unchanged与PID不变。

故障演练须记录本次精确单位与进程：仅终止本次临时候选验证原实例不变；仅在自有实例切换期间
终止本次安装器验证pending/recover。不得按名称通配杀进程，不向公司或已有业务实例注入故障。
恢复必须读回旧制品指针、health及自有状态标记，确认无候选/pending；备份和原始失败证据保留。
同修订包重压缩可产生不同摘要来驱动安装事务，须核内部tar字节相同并注明不证明跨版本兼容。
出现未解释失败时保留错误类别、时间和服务状态；重试成功不能抹掉原失败，也不能据此关闭稳定性缺口。
