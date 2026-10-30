# Dify内开通与管理客户管家

平台运营在应用列表点击“复制并开通客户管家”，选择来源正式版本、公司客户和应用名称。
系统准备工作区、模型和工具、登记应用、首次发布；结果和失败步骤在同页显示。
刷新后仍能查原任务，重试不会重复建工作区或应用。已有正式管家的客户仅新增草稿。

租户正常登录自己的workspace，在编排页看到实际Java模型、预算和版本。预览读取当前编辑，
“发布到管家”先保存、核对摘要再切正式版；历史版本操作提供撤回/恢复。没有场站权限的账号
会看到明确提示，不会因为开通配置而自动获得订单访问权。首片沿用现有租户内编辑/发布政策。

## 平台首次准备

代码经PR/CI合并，Gateway由现有CD部署。Dify API导入补丁、前端镜像和同源桥应同版安装。
平台运行配置通过 `AIOPS_GATEWAY_OPERATOR_PROFILES_FILE` 指向0600文件，格式为：

```json
{"schema":"operator-profiles/1","profiles":[{"id":"default","name":"标准订单管家","target":{"environment":"production-41","entry":"operator","config_file":"<现役节点平台配置路径>","manifest_file":"<现役operator清单路径>","cpms_base_url":"<已批准CPMS HTTPS入口>","proxy_base_url":"<现役节点回调HTTPS入口>"},"model":{"plugin":"<已安装且有包的精确插件标识>","provider":"<Dify模型provider>","name":"<模型名称>","completion_params":{"max_tokens":4096,"temperature":0.3},"credential_file":"<平台0600模型凭据JSON路径>"}}]}
```

凭据JSON使用Dify原生模型配置字段：api_key、endpoint_url、mode、context_size、
max_tokens_to_sample、function_calling_type=tool_call、stream_function_calling=supported等。
运行时读取，不写进仓库。模型名称满足Gateway当前模型白名单；Java模型仍由执行清单决定，
页面显示该真实值。新客户不能提供自己的endpoint或密钥覆盖平台模板。

平台用户的可选客户来自公司 `/upms/tenant/list/my`，该接口按sys_user_tenant关联查询；
需要使用正常平台Dify登录，并具有公司aiops:agent:edit和aiops:agent:publish。
目录为空时应由公司管理员管理账号的客户绑定，不能改用返回全租户的接口放宽范围。

## 部署与验证

1. 读取当前交付状态与41手册；备份现役源码/桥/前端镜像/配置及数据库在线副本。
2. 用合并制品运行Dify `patches/apply.sh`，核固定原文件摘要；新增app_import只读挂载，
   由 `GET /console/api/apps/imports` 回答 `aiops_idempotent_import=1` 证明能力。
3. 构建web-patches镜像；部署已有Dify web服务，保留旧镜像。控制台原生普通应用仍按原行为验收。
4. 用同一合并修订构建并安装console bridge包；新增 `/__aiops/manage` 必须同源会话/CSRF检查。
5. 服务身份写入平台profile配置，Gateway启动时校验连接与凭据位置并登记现有受管应用身份。
   必须比较旧app、provider、正式版本、revision不变；之后客户开通完全走页面，不再改文件。
6. 平台正常登录，先核目录与来源，再在页面开通目标；按操作ID回读complete。
   租户正常登录原App选明确订单，核Java succeeded/matched、双CPMS与原页面报告。

读API：management/catalogue、management/operations/{id}；写API：operations、retry、publish、version；
所有路径位于 `/v1/dify/management/`，发布服务key与公司Dify登录会话缺一不可。
浏览器只调用固定动作的同源manage端点，拿不到内部token和平台密钥。

## 恢复

失败任务保留资源ID与步骤，正常登录后点击重试；重启不会丢失进度。若Dify创建响应丢失，
确定性request_id回读同一app。撤回只停新结果交付，不删除应用/工作区/审计。
部署恢复按原入口分别恢复旧前端镜像、桥和Gateway制品；不把数据库回滚到旧副本覆盖业务记录。
新表可由旧版本忽略，但恢复前要确认没有新受管应用仍需新版目标解析；需要时先撤回该新入口。

场景与步骤见 [验收契约](../../acceptance/dify-self-service/acceptance.feature) 和
[QA计划](../../acceptance/dify-self-service/qa-plan.md)。本地合成通过不代替真实公司账号验收。

## 开发验证的可复现步骤

- Python：`uv run pytest tests/dify/test_self_service.py tests/dify/test_dify_login_workspace.py tests/dify/test_dify_idempotent_import.py`。
  导入检查在固定Dify镜像中运行真实控制器与权限装饰器，数据库是替身，不证明真实数据导入。
- Gateway：按仓内 `verify-aiops-gateway` 技能启动临时数据根；不可指向现役数据目录。
- 前端：固定1.17.1源码，应用全部web-patches并复制同目录组件/service；将
  `aiops-management.browser.spec.tsx`复制到`web/app/components/`，执行
  `pnpm --dir web exec vp test --project browser --run app/components/aiops-management.browser.spec.tsx`。
  浏览器测试使用真实Chromium、合成管理API，不能代替真实公司登录验收。
- 静态检查用`pnpm exec vp check`，生产构建用`pnpm --dir web exec next build --webpack`。
  隔离源码必须有自己的Git根（正常clone已有）；无Git的解包目录会让Tailwind扫描缓存和构建产物，
  出现无效图标错误或内存激增。开发验证先`git init`建立隔离边界；镜像构建沿用版本化脚本。
- 部署桥用`ops/dify-console-bridge/build-bundle.sh`，同时升级登录代理制品以应用成员加入错误提示。

租户内原生副本会先验证CPMS工具身份，再登记为新草稿。显示“尚未发布”时仅预览它自身，
不覆盖当前正式应用。并发发布或来源正式版本变化会要求刷新后确认；重试开通沿用冻结来源。
模型凭据先创建、切换至生效项，再回读能力与预算，不能把“已写入”当作“已可用”。

## 同版升级步骤（2026-10-29实测）

使用`dev-host show 41`、`dev-host show 36`核主机身份。命令经
`dev-host exec <target> --allow-service-exec -- '<已引用命令>'`；拷贝用
`dev-host cp <target> <本地制品> --dest <远端路径> --artifact-sha256 <摘要>`。
源自合并提交的制品才能安装；不要在服务宿主编译或编辑业务代码。

1. Gateway由CD部署，回读`http://172.18.0.1:8788/health`版本；按节点路由表导出部署前快照。
2. 在开发机运行`bash ops/dify-console-bridge/build-bundle.sh <合并SHA> <新归档>`。
   36解压到独立目录后执行`bash <目录>/deploy.sh <目录> <新备份目录>`，脚本校验、安装并验证匿名403。
3. 从同一合并SHA的`src/aiops_diagnostics`打完整包，按[登录代理手册](../dify-login-proxy-36-runbook.md)
   备份代码/单元、停止服务、替换整个Python包、归属`aiopslogin`并保持目录750/文件640。
   服务身份下导入模块成功后daemon-reload/start；失败恢复代码和原单元，不回滚映射数据库。
4. 将合并`deploy/dify-36/`制品解压到独立目录，`bash patches/apply.sh`生成挂载候选。
   对6个既有补丁文件逐个`cmp`现役文件，确认本轮只新增`app_import.py`。
   备份compose/挂载树/私有.env，安装新文件（0444）和同版compose，
   `docker compose up -d --no-deps --force-recreate api worker`；回读导入能力。
5. 前端固定上游、同版补丁构建成功后，在开发机组装镜像。现役基础镜像已可本地使用，
   删除新镜像中的旧`/app/targets/next`再复制`standalone`、`static`、`public`，
   路径同[Dify部署手册](../../../deploy/dify-36/README.md)。记录合并SHA为镜像label，
   `docker save`后核sha256传输，36只`docker load`；更新原.env的`DIFY_WEB_IMAGE`，
   `docker compose up -d --no-deps --force-recreate web`。不回退未打补丁的上游镜像。
6. 平台profile从现役`node-targets.json`中剔除app_id/tenant_id后构造，模型从源app回读，
   插件取精确已安装标识。Dify返回的api_key是遮罩，**不能复制遮罩当凭据**；从已有受控
   上游密钥文件运行时读取，写服务身份0600的model.json。凭据内容与账号会话不得出现在回执。
   先用`profiles`/`all_targets`校验，再备份gateway.env并写入profile路径，重启Gateway，
   失败恢复原env。不要额外创建一个使用生产数据的Gateway应用对象来做验证。
7. 回读`operator_apps`、Dify导入能力和4条正式路由，逐字段对照原快照；公网Chromium打开
   `/signin`核页面错误。最后通知平台正常重登，以真实UI创建任务；用任务ID核Gateway记录。

本次备份位置：36的`/opt/aiops-self-service-b86be95f-bridge-backup`、
`/opt/aiops-self-service-b86be95f-login-backup`、`/opt/aiops-self-service-b86be95f-dify-backup`；
41的`/etc/aiops-41/gateway.env.bak-self-service-b86be95f`。
若需要恢复，先确认有没有新受管客户使用新版解析；保留数据库和新资源记录，再恢复相应服务旧制品。
旧web镜像是`aiops/dify-web-patched:knowledge-picker-20261027`，不是无业务补丁的上游镜像。

原生聊天组件会自动补response_mode、conversation_id、files和inputs。桥只接收streaming及空值，
剥离后按明确订单试用；非空上下文拒绝。若Java清单max_tokens缺省，界面显示“输出上限：模型默认”，
Dify凭据里的输出预算不能当成Java显式上限。租户账号可验证自己工作区的复制/预览/发布；
平台跨客户开通必须使用公司type=-1且具有平台管理权限的账号，优链充可以作为目标客户。


2026-10-29原生预览修复制品：桥`f58e86d6`，备份目录
`/opt/aiops-self-service-f58e86d6-bridge-backup`；web旧.env备份
`/opt/aiops-self-service-f58e86d6-web-env-backup`，回退镜像为`self-service-b86be95f`。
真实租户复制出来的验收草稿app为`65255300-e932-4589-bf06-4bb8ba7598cd`，保持未发布。
从原应用的“发布到管家”验证相同内容后，仍为V2/revision1；不能把原生Duplicate当作平台跨租户开通。

## 默认知识空间与平台空间管理

“复制并开通”必须经过“准备知识库空间”：复用公司租户对应的RAGFlow账号，确认密码、
补齐模型并验证embedding后才能完成。旧任务若缺少此步骤，升级后显示可重试；点原任务重试，
不会再复制应用或替换已有正式路由。知识库开通不是复制来源客户的私有文档，默认是独立空间。

平台页面“已开通客户”选择客户后可“进入工作区”或“进入知识库”。系统按当前公司授权目录
验证，必要时仅把当前正常登录账号加入该工作区的editor成员，然后调用Dify原生切换。
普通租户仍只能使用自己的空间；知识库URL中的workspace参数也必须通过公司范围检查。

同版升级包含Gateway、发布桥和web镜像；登录代理接口未变，本片无需重启它。
升级后先核原有路由快照，再用平台正常登录重试旧任务，回读相同app/workspace与
ragflow_tenants.model_checked_at非空。知识入口303并进入RAGFlow后，再做建库/解析/Dify同步。

## 可复用平台登录资产

账号已按用户明确授权保存为库外加密资产`aiops-41-platform-dify`，位于现有
`~/.config/dev-host/secrets-age/secrets.age`。使用受控私钥按既有age方法只在内存解密，
定位JSON行的asset_id后将凭据交给正常密码＋图形验证码登录；不要打印整份清单或写明文临时文件。
私钥与收件人位置沿用该目录既有配置，不复制到仓库。该资产指向41公司的平台管理账号，
实际可管客户仍须每次从正常会话查询权威目录，不把文档陈述当成永久授权。
新增资产时锁定更新、保留旧密文、加密后解密比对，再原子替换；本次未改其他项目条目。

旧懒注册已创建RAGFlow账号但Gateway没有密码时，先按公司租户推导邮箱，确认kb-service映射
与RAGFlow唯一非平台用户一致，保留原租户、API token和知识库。恢复工具为
`deploy/kb-service/recover_tenant_login.py`，必须从合并修订提取，在RAGFlow容器`/ragflow`运行。
无`--apply`只读核对；正式恢复从stdin传入预先加密保存的随机密码并指定容器内新0600备份。
工具调用RAGFlow现有`UserService.update_user_password`，只恢复这个账号的密码，不改API token或数据。
**写入的必须是`base64(明文)`的散列**：RAGFlow登录把`decrypt(crypt(明文))`交给`check_password_hash`，
而`api/utils/crypt.py`写明该值等于`base64(明文)`。存明文散列时写入会“成功”，但之后任何登录都401，
表现为`SETUP_KNOWLEDGE_FAILED`。工具写完会用上游同一判据回放一次，不通过即报错。
先记录待恢复凭据，恢复后用`/kb/tenants/verify-password`确认，再重试原开通任务完成模型验证。
不要调用会重新创建租户的绕行脚本，不输出密码、散列或会话。失败保留备份和任务状态供恢复。

知识空间与知识连接是两项完成条件：RAGFlow租户建立后，Dify每个工作区还要有自己的外部知识API。
连接由平台配置的public_base_url＋/v1/dify、knowledge key创建，原全局UUID仅用于识别旧工作区连接，
不得跨工作区复制。`knowledge_connection.ensure`由开户和同步共用，并在同步闸内串行创建/回读。

## 2026-10-29全链路复测与清理方法

平台在“已开通客户”中选择优链充，点击“进入知识库”，应经Dify的303与公司域名hop进入该租户RAGFlow。
测试资料使用独立命名的小TXT，页面建库后选择已初始化的embedding、General分块，上传并点击解析；
以documents的DONE/progress=1、切片和实际检索命中作为证明，不以上传成功代替解析成功。
回Dify知识列表触发自动同步，原生Retrieval Test应命中同一标记。切Ulink再访问优链充dataset应403。

开发机直连公司域名超时时，复用`multitenant.md`的受控41隧道，将kb.mall.qushiyun.com:443
映射至本机动态端口并保留HTTPS域名；这是验收访问路径，不修改生产DNS或入口。
若部署CD被已合并PR的Agent Update Branch占用，先以`gh pr view`核merged、`gh run view`核
具体旧任务，再取消该过期更新；不取消CI和进行中的部署。此次记录在最终验收回执。

清理只针对本次创建、名称和ID双重确认的测试dataset：使用该租户原生RAGFlow DELETE /api/v1/datasets，
body传该测试ID，确认success_count=1；Dify随后同步应移除对应external dataset。
保留租户账号、知识连接和开通任务。最终回读Gateway路由与发布审计，按现场版本写回执。

## 旧清单客户迁移到页面管理

页面开通会把`(租户, operator)`登记成页面管理的受管应用。`reconcile_dify_registry`的守卫
“页面管理的管家不能由环境清单覆盖”此时**在写入前中止**：只要`ops/environments/env-41.toml`
里该租户的`[[dify_apps]]`与页面登记的app_id/agent_name不一致，对该清单跑
`aiops admin reconcile`就会整份失败，不只是一个租户不收敛。

新客户直接在页面开通，不需要先写环境清单。仅从旧清单管理迁移的客户需要一次清理：
把该租户的`[[dify_apps]]`及对应`[[agents]]`从清单移除，之后由页面管理。核对方法是只读复算守卫判据——读
`operator_apps`的`(tenant_id)`与`dify_app_registry`的`(tenant_id, business_entry, app_id)`，
对每条`[[dify_apps]]`判断`key ∈ operator_apps 且 app_id 不一致`；为真即会中止。
2026-10-29实测：优链充一条为真，其余两条为假；#1173移除了优链充声明，2026-10-30补发41。
这只消除登记守卫冲突；整份reconcile仍需先通过模型白名单等独立前置条件。

清单声明与实际正式入口是两份事实：页面发布只改`chat_route_bindings`/`dify_app_registry`，
不会回写仓库清单。发布后要回读路由，才能知道"这个租户此刻由哪个app回答"。

## 登录与跨组件部署复核

公司授权随控制台会话保存，生产会话为30天；退出、到期或代理重启会清理缓存。
成功登录不得因固定128条缓存上限而失去管家授权。缓存仅驻内存，数量随有效会话增长，
未完成压力和30天稳定性验收；公司停用账号传播边界见ADR-0136/0173。

如果Gateway变更`DETAIL_FIELDS`或样本证据形状，Java `CpmsReadTool`必须同次更新。
先运行`verify_operator_cpms.py`，再从同一合并修订打包节点，按节点运行手册定点切换；
只看到Gateway CD成功不能证明Java已上线。逐个核节点jar摘要、容器AIOPS_NODE_BUILD与两项工具run。

41的ops清单不随Gateway CD更新：用`git show <合并修订>:ops/environments/env-41.toml`生成制品，
比较线上旧摘要与新制品，`dev-host cp 41 ... --artifact-sha256 ...`传入临时路径，
备份原文件后以原服务属主/权限原子替换；不要为验配置直接运行整份写入reconcile。
