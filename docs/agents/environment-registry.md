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
页面提供“配置与制品预检”和计划重新核对；后台现场预检后可逐步准备、安装和验证已保存的明确租户，
满足D43全部前置后才可正式发布。各步当前部署与现场验收见领域状态。
不能将草稿保存或文件摘要匹配当业务验收。

本地复验：在自有忽略构建目录解出固定Dify源码，`git init`建立独立Git根，安装锁定依赖，按
`deploy/dify-36/web-patches/build-web-image.sh`顺序应用补丁并复制组件/service；不要清理他人的构建树。
缺少独立Git根曾使Tailwind默认扫描node_modules中的图标示例并在页面载入前报错。
复制`aiops-environments.browser.spec.tsx`和`aiops-management.browser.spec.tsx`到上游`web/app/components/`后执行：

```bash
pnpm --dir web exec vp test --project browser --run app/components/aiops-environments.browser.spec.tsx app/components/aiops-management.browser.spec.tsx
pnpm --dir web exec vp test --project unit --run app/components/main-nav/__tests__/index.spec.tsx
```

真实页面自动化读取任务结果时，先按任务编号限定对应`article`卡片，再找资源编号；历史任务可显示同一资源，不能全页断言只有一处UUID。
等待人工验证码的浏览器须保留在可跨消息继续的临时会话，结束后正常退出并清理自己的浏览器和通道；不复用旧验证码。

Web必须完整webpack构建；合并后同修订镜像配套发布，独立URL200只证明路由存在。正常公司会话、
目录授权及保存/刷新须另验；Gateway与管理桥已部署不意味着生产目录已配置。

## 计划预检与读取（P4第一片）

候选知识连接观察后，平台环境管理员可读取可见正式消费者模板并显式选择来源准备客服。
任务冻结已发布Agent版本，保留模型和业务指令，知识选择换为本环境租户的指定库；
只创建停用的advanced-chat应用，不生成应用key、正式grant、目标登记或发布。失联按原ID对账，
编辑漂移阻塞并保留草稿；正常页面的准备完成不表示124业务已启用。Agent插件直接选库，无需dataset。

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

### 受控制品引用更新

新合并修订需更新目标应用/安装器绑定时，使用同修订 `aiops-admin environment-artifacts`。
输入JSON须0600，包含已有授权主体、环境编号、当前引用摘要和两个制品引用。命令只接受当前进程配置指向的目录与references文件，
在同一目录锁内重核环境授权，并核验应用/安装器外层摘要、内部清单、目标Python和完全相同的合并修订。
命令将制品复制到受控资产根的`<完整修订>/`固定目录，应用包文件名含目标Python小版本；当前references位于既有安装代次目录时，
新制品放入资产根的`runtime-install-<修订前缀>/`，以兼容安装器/运行工件的配套资产布局。文件为0600，已有不同内容或同名冲突时拒绝。
引用文件按当前摘要CAS更新，先保存 `<references文件>.<旧摘要>.bak`，再原子替换；配置引用与环境/主机/配置ID保持不变。
重试若旧摘要已变或备份已存在则拒绝，调用者应先核对新旧文件及保留的备份，不能覆盖。新引用使旧计划失效，随后必须从管理页重做预检。
命令也接受互斥的`configuration`输入（不同时传application/installer），字段为`id/path/sha256/expected_sha256`。
仅替换当前草稿credential_ref指向的既有ID；同时比较references的expected_sha256和原配置的expected_sha256。
新模板先经原停用投影校验，再以0600保留到onboarding-assets下按摘要命名的configuration目录；原模板不改写，
旧引用备份与应用更新共用同一原子事务。后续预检、导出和安装均读取新版本，不能只改派生安装包导致下一次覆盖。
命令仅更新控制端制品与引用，不安装、不传输到目标、不改运行配置或激活。输出只含环境编号、修订、摘要和配置ID，不打印路径/内容。

### 停用候选配置投影

受控configuration现在作为模板：保留凭据、本地SQL/Redis与已批准共享连接，地址/身份等生成字段须空或不出现。
CPMS与公开入口直接来自草稿，公司UPMS地址使用既有网关合同`company_gateway_url + /upms`；
运行目录为`/etc/aiops-access-<id>/registry/environment.json`，config_ref为配置根production.env，执行环境为草稿id。
安装器负责进程AIOPS_GATEWAY_ENVIRONMENT和数据根，模板不能覆盖。`AIOPS_GATEWAY_ENVIRONMENT_INSTALLATION_FILE`仅由控制面受管配置维护，租户模板声明该字段时拒绝投影，不能重定向中央节点安装目标。候选环境/租户全停用，业务执行绑定和聊天开关关闭。
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
多个执行端仍以中央租约互斥。省略--id的每次唤醒还在「现场预检」与「安装面」两条认领面间严格交替（中央账本持久轮转游标，ADR-0326）：
两侧都可能长期不还名额（已派发检查登记永不终态、现场reconcile同样跳过计划窗），定序会让一侧永久遮住另一侧；交替保证每侧至多被跳过一轮。
后台运维轮询见下方，文件计划通过不能代替现场业务验收。
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

### 一次准备候选资源（D36）

在有效冻结计划下，选择明确客服来源版本，再对一个已保存租户点击“准备候选”。
同源任务输入为`{candidate_plan, tenant_id, source_app_id, source_version}`；服务器冻结原正式来源、
依赖摘要及该计划，持久去重父任务，跨设备重复提交回读同编号。

现有后台轮询先推进候选父任务，再执行原现场/工作区/知识/连接/管家/客服子任务；每次只启动下一个满足条件的步骤。
父任务不占子任务的环境租约，内部waiting状态不被旧执行端误当作现场检查。子任务编号与父进度同事务保存，
进程重启或网络失联沿原子任务对账，不重新开户。页面定时回读，只展示服务器记录，不靠前端串行动作维持任务。

取消只取消父任务新拥有且未结束的资源子任务；共用现场检查、原已存在任务和已完成资源保留。
权限、模板凭据/地址、计划或子任务漂移阻止新的步骤，已发上游请求按原合同安全返回并保存确定结果。
`observed/scope=candidate-resources-only`只表示候选资源已备齐；安装、业务验收和正式激活仍未执行。
原单步骤入口不变，旧Web不提交此能力；部署顺序为控制Gateway→规范执行端→管理桥/Web，
旧执行端可执行已生成的原子任务但不能推进父任务，未配共享依赖时新提交明确拒绝。

### 空机初装（包含初装，ADR-0308）

仅当目标主机确实为空时使用：同一“准备候选”区域另有点击“空机初装并准备”，浏览器显式确认后
同源任务输入为`{candidate_plan, tenant_id, source_app_id, source_version, initial_installation:true}`；
缺省或真值以外取值按无效载荷拒绝，原“准备候选”语义不变。父任务编号摘要计入该声明，
含/不含初装的重复提交不是同一任务。

首个子任务为安装面`site-install`：沿用同一SQLite行、`waiting/executing/checking`租约、
`attempted/dispatched/authorize`授权闸与安装面互斥，stage只导出`application`与`candidate`
（无runtime、无expected基线），`python`取计划部署目标声明。首个控制任务可能就是本环境
首行，任务表初始化先于任何任务表查询；父任务`result.initial_installation`独立记录基础
初装结果，原`installation`字段仍只表示运行安装。
执行端`task.py cold-run`先在固定`root/state/config/unit`、`-preparing-*`残迹与
systemd单位上证明「未安装」；声明`backups`根允许受管staging预建，仅可信私有空目录仍属真空，
其中已有内容按旧业务资产对账。端口可临时绑定才视为真空；已安装目标只跳过不重放，
未知资产/旧单位/占用端口/漂移/半失败落`reconcile`或`blocked`具名结论，不当空机。
`installed.json`与`pending.json`双账并存是同一安装器切换断点的真实形状，先恢复原语对账，
不收养也不补写；旧config-only账本没有`registry_sha256`，按`already_installed`收养并以
`absent`哨兵对账（与site_observation同约定），中央只对该形状接受absent，本任务新装仍须
完整摘要。
意图`cold-intent.json`先于`apply`首写持久在worker创建的受管staging根，不落在`inst.root`；
冷投影仅停用目录与公司连接，不装runtime；事务仍归原`Installer.apply`与`pending.json`权威。
`observed`仅当`installation∈{installed,already_installed}`；`not_installed`只能`blocked`。
已派发（dispatched）任务取消或父过期后，租约到期重领与登记同规：只用冻结输入
`observe_only`回收真实结果，父终态不复活也不补建资源；未派发请求在取消或过期时仍拒绝。
`site-install`与`candidate-register`的`checking`行按60秒节奏重领（含已取消行——
取消的排空对`executing`行本就即时，已观察仍未知的checking行不设例外）：worker是
全局单循环，未知登记每轮原地重领会占满名额，任何排队的现场预检因而无人处理
（ADR-0326）。节奏只降重领频率——行仍在`checking`，冲突安装/登记/激活照旧被BUSY
拒绝（让位≠放行）；非cold、非登记的检查任务沿原即领合同不变。

`site-install`确认在场后才出现场预检：`tasks.submit(after=<初装编号>)`延迟建行，
同计划陈旧阻塞预检行不复用；此后走原STEPS与`{candidate_task, installation:true}`升级链，
冷装账本即`installed.json`，升级按原expected基线加runtime指针接续。
部署顺序与候选准备相同：控制Gateway、规范执行端、管理桥/Web；旧执行端不认识`site-install`
动作时任务停`checking`，须先升级执行端。

### 候选工作区准备

同一冻结计划的现场任务未过期且observed后，在已保存草稿租户对应按钮点击“准备候选工作区”。
同源`environment-task`请求`{site_task, tenant_id}`，中央按计划/动作/租户持久去重；刷新或换电脑读取同一任务。
配置须包含既有Dify inner/console地址、密钥及任务执行密钥。部署顺序为中央Gateway、规范checkout执行端、管理桥/Web，
配套完成后才提交workspace-prepare；旧site-preflight及客户开通接口保持。混用旧执行端不能处理新准备动作。

执行端只请求中央固定prepare动作；中央亲自创建并回读工作区，不接受执行端或浏览器提交资源编号。
新候选名字使用`aiops-<环境字符数>-<环境>-<完整租户>`，避免连字符产生复合身份歧义；已有映射按保存名字复用。
发创建请求前保存名字，重核最新取消标记及现场回执有效期，并核全环境映射中该UUID归属唯一。
旧库只有UUID而名字为空时按UUID回读实际名字再补存，保留原就绪状态，不推导名字或重建。
已发出而结果未知时只查名字：单命中补UUID，零命中继续reconcile，多命中blocked并需人工核对。
无法读取列表时保留待核对；不删除资源、不盲目再创建。取消前未发出创建则不创建；在飞或未知取消保存确定结果后结束。

成功结果scope=candidate-workspace-only，工作区passed，应用/知识/业务pending，executable=false；
不复制应用、不授权公司租户、不发布或切正式路由。恢复旧制品前停止提交准备动作并让未知任务完成核对，保留账本与映射。
本片没有业务服务部署或目录激活；真实事件从交付索引选择。

### 候选知识资源准备

在同计划未过期且observed的候选工作区上点击“准备候选知识资源”，同源任务请求为`{workspace_task, tenant_id}`。
中央沿原任务账本处理knowledge-prepare，明确用目标环境＋租户的知识命名空间；控制服务的旧默认空间不参与。
共享知识连接、加密密钥与embedding模型配置须齐备，提交时冻结其摘要；执行中改变时具名阻塞。
模型验证与凭据保管复用既有开户接口，每个副作用前重核计划、现场回执、授权及取消；不改公司业务服务。

创建空库前保存随机名字、既有库清单与发出标记；失联后只查实际列表，唯一命中复用、零命中待核对、多命中阻塞。
新计划继承同环境/租户的原准备记录，并回读原知识库；不删除资源或通过再创建抹平未知结果。
同端点凭据轮换后，新计划使用当前凭据重新验证原资源；已确认知识库按编号回读，允许改名。
端点或知识命名空间改变仍阻塞，不通过清空创建进度迁移资源。
知识账户仍以密文保存在原Gateway库，密码不进入任务/回执。旧无命名空间账户错配时拒绝，不自动改密或收养。
准备成功仅workspace/knowledge passed，application/business pending、executable=false，不绑定应用、发布或激活。
部署顺序中央Gateway、规范执行端、管理桥/Web；旧1.23组件只支持原工作区任务，配套后再提交知识准备。
回退保留新知识空间/空库与任务进度，不覆盖旧资料；原客服开通和知识检索继续按原默认合同验证。

### 候选知识连接准备

在同计划未过期且observed的知识资源任务上点击“准备候选知识连接”，请求仅为`{knowledge_task, tenant_id}`。
中央处理knowledge-connect，复用现有Dify外部知识API入口，凭据限定环境、租户、工作区及单库。
提交冻结Console、知识服务、共享入口与签名根的配置摘要；凭据只交付上游，不进入页面或任务结果。
真实列表和检索均重核任务、管理授权、目录输入、工作区唯一归属与实际知识命名空间。
任务证据到期不撤销连接；取消、撤权或输入漂移会拒绝，重新准备须有新鲜上游证据。
候选媒体用同一复合身份，取回前后重核权限，禁止把已编码命名空间再次编码。

POST前保存固定资源名和发出标记。响应丢失只对账：唯一命中回读、零命中等待、多命中阻塞；不重复创建。
连接名遵循Dify创建接口的40字符上限；1.25.0的旧41字符确定性名称在创建前被422拒绝。
1.25.1升级后以新合同摘要提交新任务，保留原失败；仅该已知无创建的旧名称改为40字符接续，未知合法名称仍只对账。
新计划复用同环境/租户的原资源；同端点Console管理凭据轮换可重新核对，知识签名根或端点改变仍拒绝旧连接，
本片不更新上游密钥或迁移资源。scope=candidate-knowledge-connection-only，仅连接通过；应用/业务仍未验证。
部署顺序中央Gateway（含规范任务执行入口）、管理桥、完整Web；旧1.24桥和页面不接受新动作，配套后再提交。
回退到1.24保留连接与任务记录，候选凭据停止服务；原生产/调试知识入口继续使用原凭据。
本片不创建Dify外接dataset、不绑定应用、不发布或激活环境；空库验证不证明内容质量。

### 候选管家应用准备

同计划未过期的知识连接卡片请求`{connection_task, tenant_id}`，中央operator-prepare冻结唯一平台模型模板、
凭据摘要、仓内SOP/工具声明及目标草稿Gateway地址；浏览器不提供配置路径、密钥或工具URL。
模型模板复用平台批准部分，不复制控制环境节点目标。不同模型模板且无唯一选择时具名拒绝。
先保存操作UUID、期望应用ID和发出标记；失联或新计划只回读原ID，零命中等待，不盲目再次导入。
调用实际创建前重核fresh计划/知识连接/授权及取消；恢复只读仍保留已创建记录，配置/草稿漂移拒绝副作用。
Dify能力需返回aiops_candidate_import=1；候选导入第一次写入即API/site=false，不生成应用key。
回读SOP、模型和工具选择吻合且入口停用才application passed，business pending；不登记或发布正式路由。
部署先自有Dify API固定补丁全包→Gateway→规范执行端→桥/Web，原普通导入默认不变。
回退保留停用候选应用和任务，不删资料；旧组件不处理新动作。消费者、知识dataset、目标profile登记和合法run为后继。

### 候选运行接线工件

已观察的双端应用与专属节点配置通过`aiops-admin environment-runtime`导出私有运行工件。
这是配置准备，不安装、生成应用密钥、写正式route/grant或启用租户。消费者Agent直接选库，无需dataset。
输入复用原目录管理员identity、有效plan_id、tenant_id、operator_task及consumer_task；不从名字猜应用。
节点私有JSON明确environment、config_file、manifest_file和proxy_base_url，前两文件为当前环境
AgentRunConfig及受控模型模板，proxy固定专属`https://gateway-egress:9443`；不能拿41节点配置给另一环境。

```bash
aiops-admin --config /private/control-production.env environment-runtime \
  --file /private/runtime-request.json --node /private/environment-node.json \
  --output /private/artifacts/runtime.tar
```

输出目录须当前身份所有且0700；已有输出拒绝。工件包含节点绑定、双端批准模型、管家SOP/工具清单、
消费者目标与应用登记数据及摘要，秘密仅在0600成员中，公开回执只含摘要和边界。
双端必须同环境租户/工作区/单库；任务取消/撤权/配置/应用漂移拒绝。Console全程仅GET，导出前后重核输入。
双端任务必须来自同一有效计划且观察未过期；新计划可以复用资源，但须先重新完成观察，不借旧过期回执。
回执明确installation、candidate-runtime、consumer-callback、knowledge-content、business、activation待验证；
后继在受管安装事务接入工件再作正常目标登录/合法run验证，不手工逐字段改运行文件或直接激活。

### 停用客服候选的受限试运行（D35）

仅用于已批准的一次候选验证，不开放普通客服准入。控制Gateway持有Console/模型/知识配置，
目标124只持有自己的公司会话与数据源；两端均须部署支持D35的合并制品。原管理桥/Web保持兼容，
本片无新网页按钮。通过服务身份执行`aiops-admin --config <该服务配置> environment-trial-dispatch`，
请求JSON走stdin；命令只到固定本机`/v1/internal/environment-trial`，独立任务HMAC不返回操作者。
CLI以回环来源连接已配置本机监听地址，兼容仅监听Docker桥地址的控制Gateway；远端来源仍拒绝。

1. 控制端`consumer-describe`接收`task_id/order_no`，核新鲜observed客服准备任务、停用应用与精确workflow，
   返回`candidate`描述。只忽略Dify导出补回的变量自身`selector`，变量值与行为字段仍严格核对。
   旧计划需重新观察，不能手改期限或取最新成功任务代替。
2. 目标端`consumer-open`接收该描述与本次正常C端登录的`third_session`；原Redis解析器核本人及可选明确订单，
   返回随机`ect_`能力。公司会话只在目标进程内存保留；不得用管家令牌或扫描现有会话替代。
3. 控制端`consumer-start`接收`candidate/token/question`；核任务与目标绑定后原子领取一次，
   复用ConsumerTrial导入唯一临时应用、关闭其site、发布临时workflow并生成本轮key；源候选仍停用。
   Dify使用原插件回调控制端；控制端经目标已有HTTPS agent回调核会话与本人订单，知识只查候选单库。
4. 控制端`consumer-read`或`consumer-cancel`仅接收`trial_id`；回读核当前任务/权限/目标身份及同轮证据。
   取消先撤本轮grant并关闭目标读取；完成/失败同样关闭，临时应用及其key回收失败则trial为failed。

能力最多5分钟、目标最多8次明确订单读取，不能列出运营账号最近订单；普通入口、旧回调均拒绝`ect_`。
重启丢失内存能力，过期、取消、配置/安装/任务/知识归属漂移均拒绝，不写正式route/target/永久customer grant。
本轮媒体只在成功完成且原授权窗口内可读，下载前后重核目标身份与候选；取消或期限届满立即拒绝。
目标源配置与数据保持不变；恢复代码前先结束本片任务，保留失败与临时应用ID供定点对账，不恢复整库。
控制端可先部署，旧目标会明确拒绝新能力；目标可先部署，无本机HMAC开通不创建任何能力。
技术试运行不是P5原App/H5或业务准确性验收；实际部署/试跑结果另从交付索引读取。
候选核权的Console来源超时返回503；没有任何工具证据的候选结果失败，模型保护答复不能代替真实工具回调。

### 停用候选安装任务（D37）

已安装自有实例完成一次候选准备后，选择“安装停用候选”，确认后提交
`{candidate_task, installation:true}`。平台角色、目录范围、有效计划与目标当前配置/目录/运行摘要缺一不可；
旧现场回执没有运行摘要时需重新观察，不能猜现役接线。任务只成套安装配置与运行工件，不登记或激活。

控制配置新增可选`AIOPS_GATEWAY_ENVIRONMENT_INSTALLATION_FILE`，0600文件：

```json
{"schema":"aiops-environment-installation/1","targets":[{"id":"target-a","node_file":"/private/environment-node.json"}]}
```

id匹配已有部署目标引用，node_file沿D30专属节点引用格式；秘密仍在已有文件，不复制环境/主机角色目录。
缺引用时安装明确拒绝，原候选准备及单步骤install/recover保持。配置与服务重启仍需独立授权。
安装器/应用须同合并修订；新worker调用安装专用接口，旧worker不会领取waiting/executing/checking安装任务。
先部署控制，再快进规范执行checkout，最后桥/Web；旧安装器仍可用于既有预检，不能据此执行新安装任务。

原timer以900秒租约领取安装，导出同计划私有配置/运行包，通过dev-host固定制品访问目标。
控制端暂存包位于`AIOPS_GATEWAY_DATA_HOME/installation-tasks/<任务编号>/`，目录0700、文件0600；
安装引用只读，配置目录不承担任务写入。`ProtectSystem=full`保持，不能为了导出包放开`/etc`。
传输后、远端动作前重新核取消/计划，远端固定task.py持原环境锁，先记意图再调用原安装器事务。
相同远端编号不再次apply；租约/响应失联只observe原意图与installed，有pending标待恢复，不自动recover未知任务。
导出与`attempted`只证制品冻结，不等于派发（ADR-0289/0290沿继承的授权链；ADR-0321）：`dispatched`翻转先于远端执行，
未派发任务过期或取消即safe终态（blocked/cancelled，释放互斥），已派发丢回执仍按原冻结观察回收。
取消只阻止新安装，在飞等待安全结果，不杀切换；结果留原摘要与版本，业务/注册/激活分别未执行。
此片只证明已安装实例接续；冷安装完整链、真正三功能/身份/网络/客户端与正式激活仍需后继。

### 受管主机身份验收（D38）

环境页在有效冻结计划下点“主机身份验收”，提交`{verify_task:'managed-host-identity', plan_id}`。
同一计划重复或换会话提交返回同一任务编号；任务沿原子任务的租约/取消/漂移/失联合同，
由开发宿主既有`aiops-site-preflight` timer 领取，`host-verify`动作经 dev-host 指纹断言
实际访问目标，只读采集`hostname`、`/etc/machine-id`的SHA256与目标服务身份存在性。
原始machine-id不出目标，回执只含声明名、解析目标名、远端hostname、标识摘要与身份布尔。

回执`scope=managed-host-verification-only`、`check=managed-host-identity`、`executable=false`、
created/expires沿用900秒；`observed`要求三项现场事实齐全，缺失写`blocked`，
声明解析歧义（零/多命中或名字不合形）同样写`blocked`且`resolved_host`为空；
目标不可达或断言失败仍记`ENVIRONMENT_TASK_TARGET_UNCONFIRMED`待核对，不判阻塞。
执行端落回执前先过同一validate_verify自检，拒绝形状不外发。
验证只证明任务落在已登记主机，不代替正式网络、容量与业务验收，计划级pending不变；
替身无法证明真实dev-host指纹断言，合并后按既有worker合同现场复验并登记交付状态。

已知残余（独立Hack复核记录，非阻塞）：恰为32位小写hex的声明名/解析名/远端hostname
会触发防泄漏守卫使自检不通过，任务停在待核对（罕见但对该环境是确定性的）；
claim本身字段不合形（如端口/用户名不合规）同样走UNCONFIRMED待核对——
确定性输入在reconcile中循环是本任务合同与execute()共有的既定形状。

### 已安装候选运行登记（D39）

安装任务确认成功后，重新点击“配置与制品预检”取得输入相同的新鲜计划，再在安装卡片选择
“登记已安装候选”。同源输入仅`{installation_task, registration_plan}`；不能指定应用、地址或秘密。
目标应用与固定安装器须为同修订且支持1.35.0合同；旧安装记录可以查看，但不执行不支持的登记观察。
同安装结果/同计划重复提交返回同编号。新计划只续期授权窗口，不重新安装或重启；
输入摘要变化须重新准备与安装，不能拿新配置收养旧结果。

原安装执行端领取`candidate-register`，以同环境租约和原安装锁串行调用固定`task.py register`；
只调用原`register-runtime`三行同事务登记，不签发业务密钥、不激活环境或正式路由。
远端先存意图，失联或重复进入仅`observe-registration`；该路径使用只读SQLite连接，
缺失返回待核对，部分登记或归属冲突拒绝，不能靠再执行登记修复。
取消/过期/撤权阻止新写；已经发出请求沿原冻结目标只读核对，草稿变化不能释放未知操作的互斥。
结果仍由管理页当前权限控制；不倒写安装账本或恢复数据库快照。

配套顺序：控制Gateway→规范执行端→管理桥/Web；目标合并应用与同版安装器先升级再登记。
新执行端沿旧installation_claim合同兼容旧控制端；旧执行端误领新动作时stage被拒绝，
升级后按租约重新领取。代码回退保留候选登记；现役准入与业务入口不依赖此新增任务。
现场开通与三业务结论仍须分别验收，`registered`仅表示本地运行目录成套登记。

### 已安装租户业务验证（D40）

在成功登记卡片填写`case_ref`，明确选择智能客服、异常订单、离线分析，再用同输入的新鲜计划提交。
输入仅`{registration_task, validation_plan, capabilities, case_ref}`，不接受URL、token或passed。
任务持久复用同编号，后台沿原installation_claim取得环境租约，调用目标原本机HMAC试运行；
客服仍用原consumer-open短时能力到中央执行，目标正常消费者会话不离开目标。

案例由运维在目标`/etc/aiops-access-<环境>/validation-cases/<case_ref>.json`准备，目录0700、文件0600、
归目标非登录服务身份；有效期最多一小时，仅保存本轮正常认证产物，不从公司缓存扫描会话或代用户开户。
字段为environment、tenant_id、expires_at（Unix秒）、operator_token、consumer_session和cases。
每个已选案例须完整；缺某项只阻塞该能力，不删除已选范围或把别项冒充通过：

- consumer：question、明确本人order_no、预期知识分段reference_id。问题应同时请求知识和该订单状态。
- order：question、order_no。问题应明确异常原因与充电过程；判据是异常状态、同单详情、监测样本及过程统计实际入证。
- offline：question、device_id、带偏移的start/end，最长31天。问题明确对象与窗口；判据是同设备/窗口实际离线事件。

回执逐能力给出工具、对象、trial/run及范围指纹，另绑定已安装build/config/runtime；不包含原问答、令牌或连接地址。
passed仅表示这些实际技术证据齐备；`business_accuracy=unconfirmed`、`activated=false`保持，
不等于工程师确认故障原因、正式网络/身份隔离/容量通过或可以自动激活。
只读恢复核原意图及run，不再次调用模型；目标/中央重启使原短时授权失效时留下阻塞，需新计划显式重验。
取消不杀正在执行的模型，阻止后继步骤；原消费者运行结束仍按原清理/撤权规则处理临时资源。

部署顺序：控制Gateway→规范worker→目标同版Gateway→管理桥/Web。目标至少1.36.0；
原worker用户unit须同版配套TimeoutStartSec=880（短于900秒租约）；单次试跑最多200秒，
一轮已消耗560秒时不再启动下一能力，保留未验证而不冒险跨租约新派发。
未配置案例、未登记目标、过期身份或缺实际工具证据明确阻塞，原准备/安装/登记及业务入口保持。
固定worker将目标已登记端口交给本机CLI；CLI只接受原固定配置档案并强制回环来源，不能从页面改执行主机。

缺本机任务HMAC的验证显示`ENVIRONMENT_VALIDATION_KEY_REQUIRED`，不发HTTP或模型；网络/未知失败仍需对账。


## 正式受限知识读能力（D41）

目标客服只需中央知识的单应用/单库读权限，不需要共享KB或Dify Console密钥。
中央沿原KnowledgeGrants明确授权对客资料，并登记同一environment/tenant/workspace/app/library后，
受管运维可导出能力；命令不会grant、注册、发布或启用：

```bash
aiops-admin --config <中央私有配置> knowledge-delegation \
  --environment env-a --tenant TENANT-A --app <客服应用> --library <已授权库> \
  --output <新建的私有绝对路径>
```

输出文件父目录0700、文件0600，终端只显示结果与逻辑对象。已有输出拒绝覆盖，秘密不进入任务JSON或浏览器。
目标经标准配置部署设置`AIOPS_GATEWAY_KNOWLEDGE_DELEGATION_FILE`；文件只含固定中央HTTPS origin和
该环境各应用的精确能力，不能与`AIOPS_GATEWAY_KB_SERVICE_BASE_URL`同时配置。每个应用只授权一个库，
缺能力即拒绝，不能退回直接知识服务。动态文档客服callback>=4可消费，现役Agent callback6复用原工具链。
原直连部署不新增配置要求；旧ckx候选与ckc目录能力不变。

目录与检索每次重核中央原知识授权、工作区和应用归属。撤权后再授权推进同一授权行的世代，旧能力不能复活；
需明确重新导出和标准配置部署。目标不另存一份grant真值。媒体先由中央限定本轮搜到的对象，目标再绑定原QA签名，
中央撤权、目标租户/路由停用或QA不可交付时都拒绝；读取期间撤权同样不交付。
委派媒体逐跳透传Range（含非零偏移与后缀），按实际HTTP状态/Content-Range只切片一次；大于32MiB对象的小片仍可读。
单次响应体上限32MiB，无Range整段超限明确不可用；未知长度的响应也检查溢出，不返回截断成功。

本片提供知识读链，不完成应用发布与租户激活。正式接入仍须确认目标不能绕过中央能力访问共享KB原入口；
原共享服务网络暴露不由本功能自动收敛。安装账本目前记录停用目录摘要，后继激活须通过原安装器事务成套更新，
不能直接编辑registry或installed.json。真实grant/配置/准入需单独现场批准。


## 新租户安装保留同环境旧租户（D42）

环境页仍用原候选准备、安装、登记与验证，不增加另一套发布入口。安装器累积旧consumer targets，
共享节点/模型等运行输入不同则拒绝，不能拿新租户模板覆盖旧租户；目录证明环境与全部既有租户
停用时本次安装即维护窗口（ADR-0324），共享字节允许随候选更新。已有启用租户保持，新增租户仍停用。
回执分别记录上传输入与实际组合runtime摘要；中央验证输入/前驱后接受实际安装摘要供后续登记和业务验证。
旧单租户回执与包继续支持。候选验证只要求本次租户停用，不强制把整个环境其他租户关闭。

正常安装/恢复步骤与兼容边界见[安装手册](../../ops/environments/gateway/install.md#同环境新增租户保留现役运行d42)。
该能力不发布业务、不授予新知识或准入；后续正式激活仍须原安装账本成套切换及真实必要证据。

## 正式发布、最后准入与恢复（D43）

三项业务技术验证通过后，原任务卡提供“发布并开通”。平台管理员确认**只读诊断范围**及本应用
所选知识库对客授权；这不是工程师故障根因/业务准确率确认。提交只包含验证任务、当前计划及两个确认，
不能提交服务器、密钥或`passed`。目标租户须保持停用，已有租户不被这个操作关闭。

后台仍是原worker环境租约，分阶段保存并接续：
1. 重新核实际安装、正常公司身份与本人订单、HTTPS地址；以现有案例并发运行三项业务，核实际运行窗口重叠。
2. 冻结共享应用内容，沿原唯一app key账本发布；原`KnowledgeGrants`签发精确应用读能力。
3. 原安装器保存前后配置、安装账本及两端原route，安装受限知识和app key；首次开启客服需要零在飞及自有Gateway重启窗口。
4. 在准入仍关闭时用正式app配置运行三项只读验证。目标正常身份、同轮工具、资料引用、节点与案例均重新核对。
5. 原配置引用双CAS同步，目标逐端route CAS后最后启用本租户，中央目录随后同步。页面显示部分生效，不能将目标已启用/中央待对账称全成功。

容量配置复用受控`environment-installation`目标项的可选`capacity.shared`列表，每行只有已登记`host`、
项目自有`service`和`kind=systemd|docker`。记录目标与共享服务空闲/并发cgroup内存及宿主余量，
实际服务有上限时要求使用率低于80%，宿主可用内存至少1GiB。升级重叠从同修订原安装器
`current/capacity.json`读取两个真实进程/cgroup观测；没有该事实则拒绝发布，不接人工填通过的回执。
三项run没有实际重叠也拒绝，不能以发起三个线程冒充并发容量完成。资源采样不是模型业务准确率证明。
发令按窗口起点而非同刻抛三腿：consumer 腿在 run 落库前有十余秒串行准备（候选租约、
会话解析、DSL 快照与 claim），被测窗口从 run 创建起算，故先起 consumer，其 run 确认在飞后
再放行 order/offline（ADR-0325）。重叠判据、采样频率与全部余量阈值不因此放宽。

验证回执的`observed_at`是「最后一次全部核对通过」的确认时刻，不是首次完成时刻（ADR-0318）：
activate前距上次复验逾300s时，worker先经`publication-trial-complete`在目标按持久锚点重核
身份/案例/原run/配置/知识取回新proof，远端activate再经`refresh_verification`准入前复验——
两次都只刷新确认时刻，不重放模型、不续命过期证据。发行物或案例期限已尽属终态：
任务落`blocked`并标`ENVIRONMENT_PUBLICATION_VERIFICATION_EXPIRED`，原页面取消→恢复照旧可用；
恢复链不读verification。进程内300s租约只约束试跑数据能力窗，不是复验窗口。

“恢复本次开通”复用同一任务：先关闭本次准入，再用原route CAS推进新修订，恢复本操作拥有的
配置及源引用，保留运行历史。任何第三方配置/route变化、已撤权能力或未知模型启动都不会被自动覆盖/重发。
源引用或中央目录恢复失败仍保留待对账，不能删除资源掩盖部分状态。正常发布首次绑定与已有租户维护发布分开；
已启用租户的日常内容更新继续使用原应用版本发布入口。

各步骤意图标记 `{"state":"started"}` 是可续跑授权而非已消费结果（ADR-0316）：worker 死在授权与
结果落账之间时，下一认领周期重返 execute 并续跑同一步骤——中央步按确认读守卫收敛到同一 bundle、
远端按幂等脚本重跑、容量按重测产出新证据；只有已落账结果才把意图消费为只读观察。续跑里
中央步的容量 900s 新鲜度闸不再按墙钟复核（授权已在容量新鲜时铸成），发行物仍携带原测量时刻。
已知续跑边界：消费者试用已开未收的容量死亡、远端 configuring/activating 相位死亡仍收敛核查；
后者走取消→远端 restore 的设计出口。

中央步发行前的工作区占用闸只判外来占用（ADR-0317）：同一 `(environment, tenant_id)` 下
其他 app 的既有 consumer 登记是本发布自身的血统残留——提升孤儿（ADR-0312）或既有正式版——
续发按取代放行，旧登记行与凭据原样保留；他租户或他环境占用同一 workspace 仍按
「显式凭据迁移」拒绝。

取消发生在证明副作用之前时不再跑回滚、直接按restored终态（ADR-0310/0311）：中央侧中央步只记意图未落bundle、
且intent/release两个工件标记皆无；目标侧目标步只记意图、且任务环境锁下远端`publication-tasks/<任务>`不在场。
两种情形共享应用或bundle工件作为证据级留档保留，不做清理；标记在场、锁被占或探测失败仍维持待核对，
已越过副作用点的取消仍走原受管恢复链。

取消若发生在`central_bundle`已提升消费者候选之后，候选应用已被消费且无法降级；该租户不要试图
复用旧候选验证——用新计划重跑"准备候选"，对账会判定提升态、放弃继承资源并重铸候选（ADR-0312）：
受管插件凭据随重建回指候选形状，安装组合把该租户的未开通运行行原位替换成新候选身份（ADR-0313），
目标登记再按血统原位替换被消费的app行。提升态孤儿应用含其api-key
留作证据级留档，不属本次恢复，绝不由操作者在控制台手工删除；回收须走独立受管清理面。

配套顺序：控制Gateway与管理桥、目标同修订应用/安装器及worker就绪，再交付Web。
目标只拿精确app key与知识读能力，不拿Console或全局知识管理密钥。正式网络/容量/合法样例、
资料对客授权及目标配置重启窗口由现场确认；本地合成验证不替代P5两环境各两租户三业务验收。
