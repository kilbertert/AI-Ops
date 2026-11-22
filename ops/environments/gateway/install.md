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
应用成员校验与管理页预检共用`gateway.environment_artifacts`；安装器包携同修订的artifact_checks.py，
冷安装先加载该固定副本，不依赖目标预装AI-Ops或从归档执行任意探针。旧安装器制品仍按其原合同恢复。
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
升级要求相同Python小版本、同schema、零在飞任务；默认仍要求相同配置摘要。
更换配置须显式传`--expected-config-sha256 <当前已安装配置摘要>`，与当前文件及账本同时核对。
新配置先保存为本次制品的0600快照，候选只读新快照，现役文件在候选通过前保持不变。
候选用systemd只读绑定挂载维持正式配置路径，支持登记表config_ref校验；仅候选命名空间看到新快照。
切换前保存旧配置；失败与recover同时恢复旧配置及制品，任何第三方配置漂移都拒绝覆盖。
停服务前/替换前各核一次旧摘要；所有配置写入须遵守安装器同环境锁，禁止并行手改现役文件。
新配置事务的pending只用同版安装器恢复，不能交给不支持配置事务的旧脚本。
配置引用的外部文件须为固定版本，不可原地改写；本片冻结env正文，不递归备份任意引用。

## 停用候选的成套配置

ADR0280后继支持环境计划导出的三文件候选，使用`--candidate <包> --candidate-sha256 <外层摘要>`
替代`--config/--config-sha256`，应用包/安装器仍须同一合并修订。原单配置方式保持；两种输入不能混用。
首次给已安装实例引入目录还需`--expected-config-sha256 <当前env>`及`--expected-registry-sha256 absent`，
已存在未登记目录不收养。之后目录变化用已安装目录摘要；配置与目录都须与账本和当前文件一致。

目录在`/etc/aiops-access-<环境>/registry/environment.json`，registry父目录0700且服务身份持有，
production.env仍在配置根。固定包仅三份普通文件，摘要/环境/停用状态先核；新venv核实际配置与目录合同后才起候选。
候选完整目录与env只读挂载到正式路径，旧文件保持；真实候选namespace不能与宿主相同。
本地目录修订由安装器续接，投影原revision不是现役版本；同内容重跑复用原字节，变化增新审计。

停止自有服务后切两文件/制品，pending允许文件暂时旧/新混合，不能宣称跨文件原子。
任一阶段中断先recover；逐文件摘要仅接受原/本次新/已记恢复值，第三方漂移拒绝覆盖。
恢复旧目录业务内容使用新修订并记录恢复摘要，重跑恢复不会倒写版本；旧状态库与证据不从备份覆盖。
同环境已有启用租户时，新增停用租户可沿原候选安装：保留现行租户准入、配置和共享节点，
仅累积新租户运行目标。正式激活或共享配置变更仍需独立窗口，不能借新增租户改变原入口。
新pending只能同版安装器恢复，旧pending先用旧版recover；权限/只读挂载/实际启动恢复仍须现场验证。

## 已安装实例现场预检

使用已合并的固定安装器包，仍先核SHA256SUMS、目标dev-host身份与自有服务归属：

```bash
python3 <固定安装器>/install.py preflight --environment <环境> --user <自有服务账号> --port <已登记端口>
python3 <固定安装器>/install.py preflight --environment <同环境> --user <同账号> --port <同端口> --receipt <site_编号>
```

命令与install/recover共用环境锁，拒绝archive/config/candidate等部署参数；只核已安装实例，不冷建、不停止/启动或恢复。
分项为固定本地身份、应用制品/配置/目录、pending、服务实际版本/监听、schema/在飞及启动资源余量；
pending和漂移给blocked。read_record发现身份/路径/权限错误仍拒绝，不为了给结果收养未知实例。
回执位于自有安装根site-preflight，root所有0700、文件0600；15分钟同状态复用，读取重核。
PID/启动时间、文件摘要、分项、pending变化或到期时expired，记录中的资源数字为创建时观察值。
历史数量超过4096须操作者有界归档，预检不自动删记录。

scope=installed-instance-readiness-only/executable=false，主机受管SSH身份、正式网络、业务三项pending。
dev-host成功只证明另行的主机访问断言，不能由此填业务passed；中央计划未接这些回执，不自动变为可执行。
通过不代表负载容量或任意业务连接有效；后继冻结任务需要重新核当前状态与专门的网络/业务证据。

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
预检和安装器只处理自有资产，不承担公司API修复、业务网络开通、跨schema升级、上游凭据签发/撤销或高可用切换。

基础现场演练只在本轮新建、空业务配置、没有接入流量的自有实例上执行。开始前保存旧Gateway的
`MainPID`、启动时间、health及已有容器身份快照；结束逐项比较，不将其他并行变更归因于安装器。
运行相同install命令前后读`systemctl show <自有单位> -p MainPID`，同时要求unchanged与PID不变。

故障演练须记录本次精确单位与进程：仅终止本次临时候选验证原实例不变；仅在自有实例切换期间
终止本次安装器验证pending/recover。不得按名称通配杀进程，不向公司或已有业务实例注入故障。
恢复必须读回旧制品指针、health及自有状态标记，确认无候选/pending；备份和原始失败证据保留。
同修订包重压缩可产生不同摘要来驱动安装事务，须核内部tar字节相同并注明不证明跨版本兼容。
出现未解释失败时保留错误类别、时间和服务状态；重试成功不能抹掉原失败，也不能据此关闭稳定性缺口。

就绪检查只要求主进程持有监听socket，不要求其所有连接在采样期间保持存在。
Linux的`/proc/<pid>/fd`枚举后，HTTP连接可能关闭；忽略该单个链接的ENOENT后仍核监听归属。
权限错误、进程消失、版本或监听不匹配继续拒绝。依据[proc_pid_fd](https://man7.org/linux/man-pages/man5/proc_pid_fd.5.html)
与[readlink错误语义](https://man7.org/linux/man-pages/man2/readlink.2.html)，不能用增加等待时间代替该区分。

## 停用运行工件安装

ADR0289接续D30导出格式；在上述`--candidate`调用中加`--runtime /private/runtime.tar`
和`--runtime-sha256 <外层摘要>`。两包必须同plan/digest，固定应用/安装器仍同一合并修订。
已有实例首次引入运行包还须`--expected-runtime-sha256 absent`；以后更换用当前账本摘要。
运行包必须与基础候选成套输入；已有运行工件的实例不能只传基础包将接线隐式删除。

运行成员放在本次release/runtime私有快照，`/etc/aiops-access-<环境>/runtime`为受管指针。
只增加operator-profiles与consumer-targets固定引用；节点凭据、模型和成员内容不得进入公开输出。
候选完整配置根只读挂载到正式位置，其运行指针指向新快照；候选通过前现役配置/目录/运行指针保持。
安装与recover核所有成员及指针，只接受账本旧/新版本，第三方漂移拒绝覆盖。
失败恢复原运行指针、配置与制品，目录revision递增，当前数据库与证据不从备份覆盖。
重复摘要返回unchanged；现场preflight也核运行工件漂移，但通过仍是本机就绪，不是业务验收。

本片只安装停用运行接线，不登记应用、发布路由或创建密钥，不授予正常业务准入。
后继须接消费者回调/知识内容、正常身份和专属节点能力，完成候选三业务后再激活；
不以健康检查、成员摘要或进程在线代替业务结论。旧pending使用原同版安装器恢复。

## 已安装运行资源登记

应用包与固定安装器升级到同一支持修订后，执行：

```bash
python3 <同修订固定安装器>/install.py register-runtime --environment <环境> --user <服务账号> --port <登记端口>
```

不接受部署包、配置、任意资源编号或预检回执。入口持原环境锁，先核现役工件/配置/目录摘要、
应用版本与零在飞，再以服务身份调用原aiops-admin environment-runtime-register；不需要目标持共享Console管理密钥。
安装器传递root私有账本的只读文件描述符；服务身份核inode与现役指针/摘要，直接CLI缺描述符拒绝。
本机三行登记同事务写入，事务内重核在飞任务；工作区created、双端原应用目录；相同输入unchanged，部分/错主体/已有正式路由拒绝。
数据和运行文件不被修改，服务不重启；本次登记租户必须停用，其他现役租户可保持启用。恢复数据库备份不能作为登记回退。
登记后节点可回读受管目标，正式运行仍需正常身份/能力/回调/知识和业务验证；不生成key、不发布或激活。

页面续接见[运行登记D39](../../../docs/agents/environment-registry.md#已安装候选运行登记d39)。
固定task.py增加register/observe-registration，仍消费原安装工件并持环境锁；只读观察向原
运维命令传`--observe-only`，不补缺失行、不修复权限或迁移schema。首次调用先记远端意图，
之后仅观察。该能力需应用与固定安装器同为1.35.0或更高支持版本；旧直接登记入口不变。

## 停用管家业务试运行

完成运行登记与节点能力核对后，受管操作者向目标公司环境执行正常登录，再交接本次登录产物。
管理入口为目标本机 `POST /v1/internal/environment-trial`，复用该目标的
`AIOPS_GATEWAY_ENVIRONMENT_TASK_KEY` HMAC 请求头；未配置时关闭，非回环来源拒绝。
密钥通过目标受控配置维护，不复制共享 Console 管理密钥；请求体、登录令牌和原始业务结果保持私有。

`action=start` 提交 `app_id`、已安装 `runtime_sha256`、正常登录 `company_token` 和自然语言 `question`；
返回 `trial_id`。`action=read` 提交同一 `app_id` 和 `trial_id`，等待任务终态并回读原证据交付结果。
运行沿原 Java 模型循环和 HTTPS 工具；不在请求中填写租户、站点、工具或服务器地址。
每次授权核目标 `/user/info`、本次租户停用状态、运行工件、应用登记和节点能力；变化、过期和上游不可用均拒绝。
完成后仍按原范围、证据、节点和有效期复核；临时授权最多20项、5分钟，到期释放，服务重启不恢复授权。

这只证明受管候选运行，不证明来源未知旧 JWT 的环境归属，也不等同普通 App/H5 会话验收。
新入口不写正式路由或业务密钥，不激活目录；失败原样保留，后续身份、客服、知识内容和三业务矩阵仍须验收。

## 持久候选安装包装入口

管理安装任务使用同合并包内task.py；单步install/recover合同不变。task.py从stdin读取固定任务与归档摘要，
不接受shell、任意安装目录或业务操作。调用前沿dev-host身份断言和SHA256SUMS核固定包。
先记录本任务意图，再复用Installer.apply；现役baseline必须匹配，配置/目录/运行仍原成套事务。
重复run或observe只读本任务意图与实际installed，绝不再次apply；未知或pending不冒称成功，恢复仍由原recover单独操作。
在飞取消不终止安装进程，中央租约到期后先观察；安全确定结果保留，不能重启另一个未知安装。

本片新增现场回执runtime_sha256供安装baseline使用；旧回执仍可读，但新安装不能用旧回执猜运行工件不存在。
不安装公司软件、不改变网络入口或登记/发布应用，scope为candidate-installation-only，业务/激活仍未执行。

## 登记后的业务验证

[管理手册D40](../../../docs/agents/environment-registry.md#已安装租户业务验证d40)定义案例与页面输入。
验证不会调用install/recover或重启服务；只消费当前安装指针和运行登记。
案例文件独立于安装包，不把正常登录产物打入发行制品；过期即拒绝，令牌不写任务意图/回执。
失联后同任务不得重放未知模型请求；原运行与失败记录保留。实际三业务、正式网络/容量/身份以及
工程师语义确认各有边界，验证回执不能绕过正式激活授权。


## 同环境新增租户保留现役运行（D42）

原候选task与install.py入口均在目标CAS核旧账本后组合运行快照。原`runtime/1`单租户包保持支持；
新增租户组合仍用相同八文件，`consumer-targets.json`保留旧目标并加入新目标，`registrations.json`只描述本次租户，
全量登记仍在Gateway原数据库。`runtime/2`清单记录输入候选与前驱摘要；回执的runtime_sha256是实际安装包，
source_runtime_sha256是本次输入包，previous_runtime_sha256是本次安装CAS基线，不可互相冒充。
同输入新任务复用已组合包，重复原任务只观察，不因新operation重复修改资源。

共享agent-runs、模型凭据、工具manifest及profile字节必须与已安装版本相同；不同则拒绝，交独立配置维护窗口。
旧单租户且停用的环境仍可按原方式更新自身runtime，不能把新增租户条件反向施加到原用法。
已启用环境的配置保持原字节（包括现行consumer开关），其余投影变化必须单独批准；源模板不被目标修改。
目录保留已有tenant及enabled，新租户保持false，不能借候选删除或关闭旧租户。

候选venv校验读取已核当前目录，核对旧准入逐项保持、新准入未打开；正式服务仍只在原安装事务切换。
失败后原recover恢复配置/runtime，目录采用更高修订并同步installed摘要，运行历史不覆盖。
安装需要原零在飞检查和自有Gateway重启窗口；该过程保持旧租户配置和服务语义，但不承诺升级期间零中断。
旧安装器不识别runtime/2；产生新pending后只能用同版安装器恢复，恢复旧代码应先还原支持的运行工件。
