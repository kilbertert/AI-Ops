# 业务环境与租户接入登记

本文是操作方法；获批行为以[环境目录Feature](../acceptance/environment-directory.feature)为准，
未来步骤见[开通规划](../architecture/2026-11-16-manual-environment-onboarding.md)，
运行/启用/验收读[领域状态](current-delivery-state.md#环境目录与开通试点)。保存草稿不是启用。

启用运行目录后，客服开户及取数直接读取该环境已准入租户的`cpms_base_url`，不再写旧
`consumer_endpoints`地址副本；历史行保留作恢复，不参与该模式取址。运行目录与
`AIOPS_GATEWAY_BUSINESS_ENVIRONMENTS_FILE`互斥，管理目录准备模式不受影响。
迁移先核旧地址与目录/主体环境，清空旧独立清单引用，再应用目录配置；旧部署未配运行目录时行为保持。

## Dify独立管理页

从主导航「环境与租户」进入`/aiops/environments`，可直接打开、刷新与返回。
工作室/首页保留原客户AI开通区，环境目录不再混放其中。页面显示当前生效目录与配置草稿两种状态；
保存只调用草稿接口，不能立即安装、激活或停用现役业务。旧准入API继续存在，但本页不调用它。

全局目录管理员可新增环境；已有范围授权可编辑获授权环境。填写稳定环境编号、名称、公司网关、
AI-Ops入口、CPMS地址、部署目标引用、凭据引用、IANA时区和租户编号/名称/期望准入。
引用来自受控清单，不能填私钥、密码或配置文件路径。租户登记不代替公司权限验证；未填引用或时区时需补齐才可保存。
已有环境/租户编号保持固定，跨环境迁移不是改编号；新环境和租户默认不勾选准入。
版本冲突保留输入并要求刷新；切换/刷新未保存内容先确认；刷新失败清空已显示目录。后端始终重新核权。
页面提供“配置与制品预检”和计划重新核对；后台现场预检通过后可为已保存的明确租户准备候选工作区，安装与激活仍未开放。
不能将草稿保存或文件摘要匹配当业务验收。

本地复验：在自有忽略构建目录解出固定Dify源码，`git init`建立独立Git根，安装锁定依赖，按
`deploy/dify-36/web-patches/build-web-image.sh`顺序应用补丁并复制组件/service；不要清理他人的构建树。
缺少独立Git根曾使Tailwind默认扫描node_modules中的图标示例并在页面载入前报错。
复制`aiops-environments.browser.spec.tsx`和`aiops-management.browser.spec.tsx`到上游`web/app/components/`后执行：

```bash
pnpm --dir web exec vp test --project browser --run app/components/aiops-environments.browser.spec.tsx app/components/aiops-management.browser.spec.tsx
pnpm --dir web exec vp test --project unit --run app/components/main-nav/__tests__/index.spec.tsx
```

Web必须完整webpack构建；合并后同修订镜像配套发布，独立URL200只证明路由存在。正常公司会话、
目录授权及保存/刷新须另验；Gateway与管理桥已部署不意味着生产目录已配置。

## 计划预检与读取（P4第一片）

从已保存、无未提交修改的草稿点击“配置与制品预检”。同源动作 `environment-plan` 使用
`{environment, revision, registry_revision}`，回读使用 `{id}`，均POST到 `/v1/dify/management/environment-plans`。
当前授权、草稿和目录版本须匹配；相同输入15分钟内复用同一计划。刷新后可重新预检取得同编号；
草稿/目录/受控文件变化或15分钟到期，旧计划显示expired。后续执行须重核，不能拿旧编号代替版本校验。

`AIOPS_GATEWAY_ENVIRONMENT_ONBOARDING_FILE` 指向本服务身份拥有的0600受控引用JSON，不含另一份业务地址：

```json
{
  "schema": "aiops-environment-onboarding/1",
  "targets": [{
    "id": "host-a", "host": "managed-host", "user": "aiops", "port": 18951, "python": "3.12",
    "application": {"path": "/private/app.tar.gz", "sha256": "<64位摘要>", "revision": "<40位合并修订>"},
    "installer": {"path": "/private/installer.tar.gz", "sha256": "<64位摘要>", "revision": "<同一合并修订>"},
    "configurations": [{"id": "config-a", "path": "/private/input.env", "sha256": "<64位摘要>"}]
  }]
}
```

目标id匹配草稿deployment_target，配置id匹配credential_ref。上述占位值须替换，文件不允许符号链接，
共享服务凭据不得放宽权限来满足读取。制品用原P3构建命令从合并修订生成；本片只核摘要与声明修订一致，
应用归档复用P3成员/内部摘要清单及目标Python规则；安装器只接受固定普通文件及完整内部摘要清单，
两包REVISION/manifest修订须与声明一致。不解压执行归档代码；允许已交付旧安装器缺少后继artifact_checks.py。
新增application-contents、installer-contents和embedded-revision分项只证明归档完整性及声明一致，
**不证明Git合并来源、wheel内部代码身份、主机指纹或配置里的业务地址**，这些继续作为执行前置。
回执 `scope=local-file-integrity-only/executable=false`，所有计划仍blocked；缺引用或错摘要给分项阻塞。
配置投影由草稿与受控模板生成；公司地址为空或模板格式/部署身份覆盖不合规时blocked，未配置引用时pending。
主机、容量、双向网络和业务项仍pending，不发SSH、HTTP探测或安装命令，不启用目录。
秘密正文、主机/路径不入计划，公开字段仅编号/版本/摘要与分项；原始配置继续在受控位置。

恢复CLI `aiops-admin environment-plan --registry <私有目录> --file <私有请求> --references <引用清单>`
使用同一实现；文件字段为identity三元组加上述POST字段。CLI本地操作者仍须显式目录授权，
不能借CLI生成被浏览器接受的身份；不安装、不激活。计划持久化到既有草稿SQLite，不更改正式业务库schema。

### 停用候选配置投影

受控configuration现在作为模板：保留凭据、本地SQL/Redis与已批准共享连接，地址/身份等生成字段须空或不出现。
CPMS与公开入口直接来自草稿，公司UPMS地址使用既有网关合同`company_gateway_url + /upms`；
运行目录为`/etc/aiops-access-<id>/registry/environment.json`，config_ref为配置根production.env，执行环境为草稿id。
安装器负责进程AIOPS_GATEWAY_ENVIRONMENT和数据根，模板不能覆盖。候选环境/租户全停用，业务执行绑定和聊天开关关闭。
本地SQL时区不从IANA时区反推；manifest记录草稿timezone，实际数据源口径另验。
模板按既有dotenv格式读取；拒绝重复键、无值键、解析错误、`${...}`插值和声明数值类型错误，保留合法引号/换行/空值。
不把类型检查当完整Gateway启动验证；模板中的外部文件/共享地址权限由后继现场验证，秘密不回显。

预检新增projection_sha256/projected_config_sha256/projected_registry_sha256，页面只显示摘要。
所有本地项通过后，可用同一个`--file`编号回读请求追加`--output /private/candidate.tar.gz`，
导出production.env、environment.json和manifest.json；输出父目录须当前操作者独有0700，文件0600且必须不存在。
重跑可选新的文件名，包摘要一致；既有输出/符号链接、到期/漂移/越权拒绝，不覆盖文件。
候选manifest为aiops-environment-candidate/1、scope=bootstrap-configuration-only、executable=false。
ADR0280安装器可按[成套配置手册](../../ops/environments/gateway/install.md#停用候选的成套配置)显式接收该包；
目录放入服务身份持有的0700私有子目录。旧导出路径不自动改写，需重做预检；旧计划因投影摘要变化过期。
导出仍不自动传主机或安装；安装权限、目标身份、实际配置验证和恢复回执独立核对，不启用租户。

## 草稿准备入口（P1基础片）

`AIOPS_GATEWAY_ENVIRONMENT_MANAGEMENT_REGISTRY_FILE`仅为管理准备配置，不启用该目录的运行准入。
`AIOPS_GATEWAY_ENVIRONMENT_REGISTRY_FILE`仍是业务准入开关；若两项同时设置，必须指向同一个JSON。
目录授权、控制环境身份来自该JSON，草稿在同目录`<目录文件名>.drafts.db`；备份时两者一并保存。
生效JSON和草稿账本的模式分别仍为0600，所在目录0700；数据根不从浏览器传入。

空目录用原CLI引导：私有输入为`{"environments":[],"managers":[{"environment":"control","tenant_id":"platform","user_id":"operator","environments":["*"]}]}`。
`control/platform/operator`必须替换成已经能正常登录控制环境的主体，不使用尚未开通的目标环境。
用下方`apply --expected-revision 0`发布初始目录，再配置管理引用；`["*"]`显式授权管理全目录及新增环境。
已有有限环境管理员不能新增或查看其他环境；公司type=-1不是自动管理授权。不能从页面授予`*`。

正常控制台会话经过原同源桥/Origin/CSRF与后端活动令牌交换、公司身份交叉核对后，使用：

- `GET /v1/dify/management/environment-drafts`：获授权生效摘要和草稿，不返回config_ref/管理员/凭据正文。
- `POST`同路径：`{"revision":0,"registry_revision":1,"draft":{...}}`，首次草稿revision为0。
- 同源桥动作分别为`environment-drafts`和`environment-draft-save`；服务鉴权仍必需。

草稿字段：`id/name/enabled/gateway_url/cpms_base_url/company_gateway_url/deployment_target/credential_ref/timezone/tenants`；
tenant字段仍为`id/name/enabled`。目标及凭据为1～64位字母数字下划线/短横线逻辑引用，时区使用IANA时区名。
地址遵循现有端点规则，拒绝userinfo/query/fragment；草稿只保存，不访问填写的地址或凭据文件。
`enabled`在草稿中只是期望状态；响应始终`state=draft/acceptance=unverified`，不启用新环境。
草稿记录原目录版本和digest，之后目录变化时返回`registry_changed=true`，执行前需重新预检。
旧`environment-update`仍是已有环境名称/准入启停，不能把它误称为草稿发布。
引用预检、安装和激活尚未交付；独立管理页使用下述接口，不能向用户宣称一键开通。
受管恢复命令`aiops-admin environment-draft --registry <目录> --file <私有输入>`调用同一保存函数；
输入为`identity:[控制环境,租户,用户]`和API的三个字段。CLI本地身份由操作者选择，但仍需目录中已有显式授权；
浏览器身份只能从正常会话取得。该命令不发布、不允许修改管理员，不打印连接或凭据引用。

目标基础连接已配置时，仍先保存`enabled=false`环境草稿和未启用租户，不能由health成功激活。
`credential_ref`先在既有加密清单登记为配置位置引用，正文不复制口令；时区结合业务容器TZ和数据库会话核对。
使用现有显式全局管理员执行恢复CLI前，读当前registry/draft版本；保存后按同一授权身份回读，
同时比较生效目录摘要和生效环境数。CLI持久化证据不替代原页面保存、配置投影或真实开通验收。

> 下文是现有隔离实例的登记实现。95试验已撤回，新目标见[手工目录与一键开通规划](../architecture/2026-11-16-manual-environment-onboarding.md)。
> 现有JSON/CLI不是集中管理页面；每环境独立Gateway也不表示必须部署在客户服务器。
> 新规划允许平台管理员通过受控表单编辑连接；下文“不让客户端提交URL”描述现有业务入口，不能作为禁止管理表单的理由。

公司认证负责用户/权限，AI-Ops维护环境与租户开通关系；采用每环境独立Gateway、登录代理、数据根。
首轮由平台运维维护私有JSON，不逐个登记用户、不让客户端提交服务URL。决策见[ADR0256](../adr/0256-platform-environment-registry.md)。

## 登记与发布

输入是环境数组，各条含id、enabled、gateway_url、cpms_base_url、config_ref、tenants；
tenants为id/enabled数组。id使用规范环境编号；config_ref是该环境运行配置文件绝对路径，
配置正文及凭据仍存受控位置。端点远端HTTPS、本机回环可HTTP，不带凭据/query/fragment。
环境可共用物理机，不可共用Gateway入口；同租户编号可在不同环境独立登记。

```bash
# input.json与目录为0600/0700；不要将现场数据放入Git。
aiops-admin environment-registry apply --registry /var/lib/aiops-registry/registry.json \
  --file /var/lib/aiops-registry/input.json --expected-revision 0 --by operator
aiops-admin environment-registry check --registry /var/lib/aiops-registry/registry.json
```

每次修改携带当前预期版本；过期写者被拒。修改通过同一apply发布，保留递增版本及操作人/时间。
check仅打印版本和计数，不打印地址、配置引用或秘密。备份旧私有文件后再发布，回退也按新版本发布旧内容。
多宿主分发由受控部署传递同一版本，运行时副本不可各自编辑；未回读目标版本的宿主不算生效。
入口渲染使用`render_business_ingress.py --registry <登记文件>`，独立凭据文件只保存环境id与密钥；地址只来自登记。

## 生效与范围

Gateway配置AIOPS_GATEWAY_ENVIRONMENT_REGISTRY_FILE，登记config_ref必须与server_config_file一致，
execution_environment与environment一致；登录代理配置AIOPS_LOGIN_PROXY_ENVIRONMENT_REGISTRY_FILE。
95试验期间启用登记，现已撤回；未设置的新选项不改变旧41。首次启用需重启配套合并制品，后续租户启停无须重启。
共享知识服务按环境和公司租户共同识别账号，贯穿开户、检索/媒体、发布检查及运维命令；
本地账本与公司API仍使用原公司租户ID。旧实例启用登记前需显式迁移知识账号，不能只切开关。
Gateway用户准入、控制台新登录/令牌获取/身份查询、自助开通目标与受控工具重新授权均校验登记。
客户端业务端点由登记决定，不再使用旧CPMS默认清单推导未登记租户。

停用只表示下一次准入拒绝，不撤销已完成回答，也不能撤回已经发送的上游请求。
Dify已签会话仍受原寿命约束；登记不声称全局清除共享Dify的所有浏览权限。
环境地址/配置变化不是租户迁移快捷方式，须准备完整配置并重新部署绑定；历史会话仍属于原环境。

## 95历史重建操作约束

以下仅为历史试验与恢复资料；95已撤回，任何恢复须独立决定，不是当前部署计划。

通过[受管访问](env-95-facts.md)盘点aiops-gateway、两个既有/v1反代、数据根和实际访问日志。
日志仅在主机内汇总路径/状态/时间段，不输出用户令牌/查询串。先做私有备份并验证恢复资料，
再将合并制品安装到独立目录和新数据根，核服务身份及健康，最后切换原入口、退休旧运行文件。
公司MySQL/Redis/TDengine及非AI-Ops反代不属于清理对象。旧资产备份从运行目录隔离，不再参与新实例。
没有真实认证/业务样例时只报告基础设施与合同验证，不能报告三个业务功能验收通过。

## 管理目录schema2与原Dify页面

先部署兼容schema2的Gateway/管理桥，再迁移目录；旧运行程序不认识新格式，不能直接用旧制品恢复schema2。
`migrate`在同一目录锁内显式备份schema1并递增版本，名称初值沿用编号，公司网关地址为空，管理员授权为空，不自动扩大权限。

```bash
aiops-admin environment-registry migrate --registry /var/lib/aiops-registry/registry.json \
  --expected-revision 1 --by operator
```

新`apply --file`接受`{"environments": [...], "managers": [...]}`；旧数组输入只用于schema1，拒绝覆盖已升级目录。
环境在旧字段之外必填`name`和`company_gateway_url`，后者为空表示尚未登记，非空须HTTPS，可含公司路径前缀。
它不替代现有`gateway_url`（隔离AI入口）或`cpms_base_url`（现役工具入口），本片不据该新字段发请求。
租户增加`name`。管理员记录为`environment/tenant_id/user_id/environments`，前三项是正常登录主体，最后是可管理环境ID数组。
schema1不能直接apply新格式；必须migrate，迁移中断后可复用同内容且私有的备份重试，冲突备份明确拒绝。
管理员列表只由受管CLI配置，不从公司type=-1自动推导，也不能从页面编辑。实际身份与地址保存在私有文件，不入仓。

Dify原AI管理页增加“环境与租户管理”：读取获授权环境，修改环境或已有租户名称、准入开关；
每次写入携带读取版本，冲突先刷新。目录列表不返回地址/配置路径/管理员/秘密。
API为`GET/POST /v1/dify/management/environments`，经现有同源桥的`environments/environment-update`动作，
保留Origin、CSRF、Dify角色、服务身份和正常公司会话验证；不提供任意URL、SQL或主机执行。

本片只管理登记与准入，所有新接入仍标“待验收”；不创建新环境运行服务、自动发现租户或撤销所有既有Dify会话。
目录管理员的独立授权可在业务准入停用后继续操作；公司会话失效仍拒绝。登录阶段的业务停用限制未迁移，
因此若当前登录环境也被停用且会话随后过期，需要受管CLI恢复；不要把唯一控制环境作为试验停用目标。
新租户由公司目录核权后受管登记，真实发现及跨环境开通留后续片；不得把手工填写ID当业务授权。
回退目录内容仍走递增版本发布；保留任务备份，不能用旧schema1数组静默丢掉新授权。

目录修改使用全局revision，两个环境同时保存也可能冲突，需刷新；未将目录并发扩展为分环境版本。
环境运维不要求公司智能体编辑/发布权限，但正常公司身份和显式范围授权缺一不可；应用内容权限不因此放宽。

## Web发布后核验与镜像摘要

按`ops/dify-console-bridge/README.md`完整配套管理桥；Gateway CD不会更新桥或Web。
Web只从合并修订执行`build-web-image.sh`，为`DIFY_WEB_WORK`指定自有构建目录，归档SHA校验后在服务宿主加载。
保存现役镜像、私有.env和其他容器ID；只改`DIFY_WEB_IMAGE`，用既有compose的`--no-deps --pull never --force-recreate web`切换。
失败恢复原.env和原完整镜像，核登录页健康；其他容器ID不能改变。

Docker镜像身份可能因存储后端不同而展示不同层级：开发端OCI index摘要不等于旧Docker的image config摘要。
外层归档SHA先匹配；读取归档`manifest.json`中对应标签的`Config`，核该blob摘要，再与目标`docker image inspect`的Id比较。
同时保留index/manifest/config与合并SHA，不能仅因标签相同就放行，也不能把层级差异当成内容篡改。
规范依据：[OCI descriptor](https://github.com/opencontainers/image-spec/blob/v1.1.1/descriptor.md)和[manifest](https://github.com/opencontainers/image-spec/blob/v1.1.1/manifest.md)。

上线后核镜像内`/app/targets/next/web/.next/server/app-paths-manifest.json`含环境页，
再从正式TLS入口检查`/signin`及`/aiops/environments`、`/apps`、`/datasets`，保留证书校验。
未登录307/登录页200只证明入口与登录重定向；`/__aiops/manage`匿名403只证明拒绝门。
正常公司会话＋显式目录授权后的表单操作另行验收；无真实会话时不伪造身份或绕过验证码。


## 首次管理目录引导的现场顺序

只读从受控平台账号对应的登录代理映射确认控制环境/用户/租户，私有保存复合身份；账号既有角色不能自动授予全目录权限。
用当前合并版`environment-registry apply`生成空schema2与单个明确`environments:["*"]`管理员，目录0700、文件0600，属主为Gateway服务身份。
备份服务配置后仅设置`AIOPS_GATEWAY_ENVIRONMENT_MANAGEMENT_REGISTRY_FILE`，保留运行准入变量为空。
确认无在飞任务，再重跑同一已合并SHA的标准CD；失败恢复该项配置，不覆盖业务DB或已有目录。
读回health版本与私有目录计数，使用正常公司登录验证页面；无真实会话时只报告配置准备完成。
此阶段不填生效环境、不启用租户、不从其他环境借凭据；用户ID、租户ID、引用与备份细节保存在私有记录。

## 中央现场预检任务

配套Gateway、同源管理桥与Web后，环境页可请求已冻结计划的现场预检、按环境查看最近20条任务及安全取消。
服务端以环境/目标/site-preflight动作/计划摘要去重，同一有效计划重复提交复用编号；原计划仍executable=false。
Gateway配置独立`AIOPS_GATEWAY_ENVIRONMENT_TASK_KEY`，为空拒绝新任务，原草稿/文件预检及业务不变。
该密钥只交给受管运维执行端，不复用Dify发布、公司或其他内部服务密钥，不进入浏览器/目录/回执。

执行端从干净、已合并的规范checkout运行；本机须有原dev-host受管声明和获准目标：

```bash
aiops-admin environment-task-worker --id task_<编号> --gateway https://control.example   --key-file /private/environment-task.key
```

命令只领取这个任务，冻结host经原项目.dev-host.toml唯一解析为目标name（零个或多个匹配拒绝），校验安装器摘要，从origin/main已合并修订构建同一固定安装器，
经dev-host传输与身份断言执行preflight，不install/recover。省略--id时自动选择一个待处理任务，queued过期/撤权明确结束，未知reconcile至少延后60秒重试；
多个执行端仍以中央租约互斥。后台运维轮询见下方，文件计划通过不能代替现场业务验收。
中央`/v1/internal/environment-tasks`只接受独立HMAC；管理桥不能访问或提交结果。
不让业务Gateway持有SSH私钥；若正式访问路径尚未允许内部worker接口，停止并按原入口交付，不能临时开放任意端点。

queued表示等待执行端，running表示当前租约取证；失联/超时或租约到期为reconcile（待核对）。
同编号再运行只重新核只读现场；文件计划已经到期时只允许原不确定任务核对，配置漂移/撤权仍拒绝。
新任务不得复用过期计划，旧租约不能报告；已取消的排队任务不执行，在飞取消等动作边界返回且保留证据。
observed仅本地观察通过，blocked保留失败项；受管身份/网络/业务pending不可冒充开通完成。
回执到期显示expired；任务留在独立草稿库，旧版忽略该表，回退不删除记录或恢复业务库快照。
本机替身检查证明状态/授权/并发与固定操作，不证明真实主机、网络、负载或业务。

### 自动执行端配套

中央为已有41控制Gateway档案，执行端仍在规范开发checkout，未配置新档案不能猜路径或主机。
`aiops-admin environment-task-worker --central-target 41`经现有dev-host管理通道调用中央合并CLI，
以Gateway服务身份读取自身私有配置、构造本机内部HMAC；请求JSON从stdin传入，不在shell拼接，
密钥不返回执行宿主。此路径不新增公网接口/端口，不改变公司的网关或登录代码。
原`--gateway/--key-file/--id`显式HTTP操作保持兼容，不能与central-target混用。

先以合并制品准备受控引用文件与独立执行密钥，配置仅写本项目production.env，
保留旧内容与0600权限；确认零在飞并重跑标准CD后，从服务身份回读文件/归档/投影分项。
引用中的模板、主机和凭据必须已有授权，不能用空模板或其他环境秘密制造通过。
回退只恢复本次配置和停止本任务timer，保留新任务/回执，业务数据库不被旧副本覆盖。

在干净、已合并的规范checkout执行固定用户单位安装入口：

```bash
bash ops/environments/gateway/install-worker.sh install
systemctl --user status aiops-site-preflight.timer
bash ops/environments/gateway/install-worker.sh stop
```

timer每轮结束20秒后只处理一个任务；同一service不会叠加运行，不启动通用任务引擎。
停止只禁用后续轮询，不终止在飞远端预检。开发checkout有未提交改动或未合并修订时worker拒绝执行，
既有任务仍持久保存；由操作者恢复规范checkout后下一轮续接，不自动切分支或覆盖工作。
中央refs/key与timer分别准备，最后在正常公司页面提交有效plan，刷新读相同任务/现场版本，
再原生退出核旧会话拒绝；真实目标的PID/配置/目录/业务库和公司容器须前后比较。
读旧回执或本机替身不能证明生产自动执行；结果仍仅本地就绪，网络/业务pending，不激活租户。

### 候选工作区准备

同一冻结计划的现场任务未过期且observed后，在已保存草稿租户对应按钮点击“准备候选工作区”。
同源`environment-task`请求`{site_task, tenant_id}`，中央按计划/动作/租户持久去重；刷新或换电脑读取同一任务。
配置须包含既有Dify inner/console地址、密钥及任务执行密钥。部署顺序为中央Gateway、规范checkout执行端、管理桥/Web，
配套完成后才提交workspace-prepare；旧site-preflight及客户开通接口保持。混用旧执行端不能处理新准备动作。

执行端只请求中央固定prepare动作；中央亲自创建并回读工作区，不接受执行端或浏览器提交资源编号。
新候选名字使用完整`aiops-<环境>-<租户>`，已有映射按保存名字复用；发创建请求前保存名字。
已发出而结果未知时只查名字：单命中补UUID，零命中继续reconcile，多命中blocked并需人工核对。
无法读取列表时保留待核对；不删除资源、不盲目再创建。取消前未发出创建则不创建；在飞或未知取消保存确定结果后结束。

成功结果scope=candidate-workspace-only，工作区passed，应用/知识/业务pending，executable=false；
不复制应用、不授权公司租户、不发布或切正式路由。恢复旧制品前停止提交准备动作并让未知任务完成核对，保留账本与映射。
本片没有业务服务部署或目录激活；真实事件从交付索引选择。
