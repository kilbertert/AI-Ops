# 最小Gateway制品与候选预演

这是一键开通前的P0/P3基础，不是已完成的安装或租户开通。判据沿用
[环境目录Feature](../../../docs/acceptance/environment-directory.feature) D9/D10及
[预检手册](../../../docs/agents/environment-onboarding-preflight.md)。

共享执行侧到目标环境的正式回调另见[既有HTTPS扩展入口](ingress.md)；本预演本身不开放该入口。
目标模块向共享平台派发的反方向见[共享执行入口](execution-ingress.md)，与工具回调分别验证。
持久独立实例的冷安装、重复执行与同schema恢复见[受管安装](install.md)，不从预演状态直接切业务流量。

## OCI 容器镜像分发（ADR-0363，唯一主路线）

所有新客户机冷装与 Day-2「版本与发版」升级回滚均统一走 **公司阿里云企业版 ACR 不可变容器镜像（`@sha256`）**：

1. **统一发布脚本构建推送**（日常由 `cd.yml` 自动调用；人工补发可跑 `release-gateway-image.yml` 或本地脚本）：
   ```bash
   AIOPS_GATEWAY_IMAGE_REPO=qushi-registry.cn-hangzhou.cr.aliyuncs.com/release/aiops-gateway \
   AIOPS_ACR_USERNAME=<user> \
   bash ops/environments/gateway/publish-release-image.sh --tag v1.43.23
   ```
   （脚本通过 `AIOPS_ACR_PASSWORD` 环境变量登录隔离 `DOCKER_CONFIG`，调用 `build-image.sh` 执行非 root `--read-only` 冒烟核验并上传 `gateway-image.json` 至 GitHub Release。）

2. **在 41 控制面写入受控 Registry 配置**（`0600`）：
   ```bash
   bash ops/environments/gateway/configure-registry.sh \
     --repository qushi-registry.cn-hangzhou.cr.aliyuncs.com/release/aiops-gateway \
     --no-pull-auth --central 41
   ```
   客户机动作统一收口在纯标准库脚本 `ops/environments/gateway/host_release_agent.py`，经 SSH stdin 投递执行；容器直接由 `systemd` `EnvironmentFile=/opt/aiops-access-<env>/container.env` 托管。

---

## 历史离线预演制品参考（已退出日常发版主链）

在开发机从已合并修订执行：

```bash
bash ops/environments/gateway/build.sh origin/main 3.12 /private/gateway.tar.gz
```

目标平台目前为Linux x86_64，解释器选择3.11/3.12/3.13；不下载或升级目标解释器。
`--python`只接受`/usr/bin`或`/usr/local/bin`下受管的`python3[.11|.12|.13]`（符号链接解析目标与父目录须root拥有且组/他人不可写，核验后只执行解析后最终目标）；
平台默认`python3`过旧的主机（如Alibaba Linux 8的3.6）应显式给出/usr/local下匹配的受管解释器。
构建用相同小版本解析依赖标记，下载锁定版本及hash匹配的wheel，无源码包、配置或凭据；
wheel里的health版本带合并提交。输出包括manifest.json、SHA256SUMS和wheelhouse，外层SHA由命令给出。
上传通过目标已有dev-host声明及`--artifact-sha256`，不要把95历史包当通用模板。

依据：[pip download](https://pip.pypa.io/en/stable/cli/pip_download/)、[venv](https://docs.python.org/3/library/venv.html)。
`--python-version`不保证全部环境标记按目标求值，所以构建进程本身也使用目标Python小版本。

## 预演

从同一合并修订用build-installer.sh取得完整安装器包，核SHA和SHA256SUMS；其中probe.py
和artifact_checks.py须配套，不能只上传探针到无预装AI-Ops的主机。主机已登记角色/所有者、SSH公钥身份及项目路径；
先只读核Python、已有非登录服务身份、CPU/内存/磁盘、旧实例流量。对公司业务库不做DDL或压力测试。
主机维护身份执行，应用仅使用已有项目服务账号；`--root`是新建且root所有、不可被其他用户写入的项目父目录（代码父目录0755，状态子目录0700）：

```bash
python3 probe.py /private/gateway.tar.gz --sha256 <外层摘要> \
  --python /usr/bin/python3.12 --environment <环境编号> \
  --user <已有AI-Ops服务账号> --root /opt/aiops-candidate --verify-only
# 校验通过且本次预演获准后，去掉--verify-only。
```

先对归档文件集、每个摘要及Python版本核验，再用真实服务账号执行父目录可穿越检查；不满足即拒绝，不自动改现有权限。运行时复制到新root并再次校验，离线安装、pip check，
启动两个临时systemd服务，回环动态端口、不继承进程配置、状态根独立、无公司凭据。
单实例MemoryMax384MiB、CPUQuota50%、RuntimeMaxSec180、TasksMax32，`PrivateNetwork=yes`独立网络命名空间仅有自己的回环；启动后核对namespace与宿主不同，探针用nsenter进入该网络验证HTTP。
健康请求耗时包含nsenter/解释器启动开销，不代表业务请求延迟。这些是保护上限，不是容量合格阈值；systemd必须支持相应属性，拒绝时不放宽后重试。
命令行环境、数据根与新进程不会读取旧业务配置，配置文件0600、目录0700。

每实例记录空闲RSS/HWM、40个健康请求/4并发时的RSS与cgroup用量，以及双实例同时存在的RSS总和。
health必须匹配制品修订、业务只读；匿名问答必须拒绝。正常完成、启动失败或TERM都会停止本次临时单元；
只有确认进程已停才删除本次临时根，清理未确认则保留根并报失败。不停止旧服务、不写正式配置、不改公网路由。
按[systemd-run](https://www.freedesktop.org/software/systemd/man/latest/systemd-run.html)的临时单位和
[资源控制](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html)映射上述上限。

只部署已合并的固定制品；外层摘要、探针/校验模块SHA、环境、时间及结果在私有运维账本记录。
通过仅说明基础冷启动/匿名拒绝和健康请求成本。真实订单、离线、知识、模型及共享节点成本、
HTTPS两个方向工具往返、升级/回退和切流量仍须各自验收，不能据此激活目录。
