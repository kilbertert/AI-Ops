# 知识选库节点安装、迁移与恢复

只从已合并制品交付。凭据与真实租户标识从受控位置读取，不进入命令输出或验收报告。
正式安装会重建Dify插件宿主，可能中断在飞插件/模型步骤；先保存现有compose、公钥配置、原应用草稿与已发布DSL、路由版本。
本文件为操作方法，不代表生产已经执行。

## 构建和验签

官方dify-plugin CLI 0.6.12，插件独立requirements固定dify_plugin==0.10.2；不增加Gateway依赖。
首次用`dify-plugin signature generate -f <受控目录/项目签名名>`生成项目签名对，私钥只留开发受控目录，
公钥与制品一起交付，记录摘要。`umask 077`，不使用临时验收密钥签正式包。
本轮公钥制品为`plugins/consumer_knowledge/signing/public.pem`；私钥只在开发机受控配置目录，不入仓、不发服务端。

```bash
dify-plugin plugin package plugins/consumer_knowledge -o <私有输出/consumer_knowledge.difypkg>
dify-plugin signature sign <包> -p <项目私钥> -c community
dify-plugin signature verify <已签名包> -p <项目公钥>
```

Dify插件宿主0.6.10支持额外信任公钥：
`THIRD_PARTY_SIGNATURE_VERIFICATION_ENABLED=true`，
`THIRD_PARTY_SIGNATURE_VERIFICATION_PUBLIC_KEYS=/app/storage/<项目公钥>`。
保持`FORCE_VERIFYING_SIGNATURE=true`；源码先加载官方公钥再追加这些公钥，不替换原官方信任。
按项目dev-host制品交付到Dify部署根，在compose配置检查后仅重建plugin_daemon，轮询插件目录/API就绪。
失败恢复原compose/额外公钥配置后重建同服务。不要关闭验签或修改服务宿主内的插件代码。

## 安装和配置工作区

使用目标工作区的正常控制台登录态上传签名包：
`POST /console/api/workspaces/current/plugin/upload/pkg`，multipart字段pkg；要求manifest.verified=true。
再调用`POST .../plugin/install/pkg`并等待返回task_id完成；不能只以安装请求200为成功。
工具provider为`trendpower/consumer_knowledge/consumer_knowledge`，通过该provider的add接口配置gateway_url与catalogue_token。
gateway_url为可信HTTPS根地址；目录凭据在Gateway侧用consumer_knowledge_selection.catalogue_token生成，
只把派生值交给该工作区，不复制internal_token_secret。

Gateway目录为`GET /v1/internal/consumer-knowledge/catalogue`。随合并制品安装
`ops/nginx/dify-consumer-callback.conf`的精确location并nginx -t/reload，保留Authorization；
不能用正则location，被既有^~ /v1/截走会换掉凭据。无凭据应403，目录凭据取正文应401。
目录只返回当前租户库ID/名称；凭据不能用于其他环境，工作区映射失效则403。

动态选项请求的provider必须是完整三段名，plugin_id为trendpower/consumer_knowledge；短provider会报plugin not found。
现用Dify需`04-tool-dynamic-settings.patch`补齐设置面板的provider/tool属性；用既有web-patches/build-web-image.sh
构建独立候选镜像，保留前三个补丁。构建使用任务自己的DIFY_WEB_WORK，不清理其他会话工作树。
部署候选web镜像前保留原镜像摘要与.env，失败恢复原镜像再重建web；不直接改容器中的脚本。
API返回选项还不够，必须在浏览器点击节点查看真实多选框；隔离复现命令：

```bash
DIFY_TEST_WEB_IMAGE=<候选web镜像> uv run python tools/verification/verify_consumer_chatflow.py --out <私有报告.json> \
  --knowledge-plugin <已签名包> --plugin-public-key <验收公钥>
```

仅启动一次性数据根/本机合成依赖，浏览器经宿主动态回环端口访问真实Dify页面和API；结束清本次栈及卷。
需要宿主Playwright/Chromium。浏览器并行加载会耗尽API默认10连接，隔离compose配置100；这是验收配置，不改生产参数。

提示词生命周期复跑：在上述命令追加`--prompt-browser`，实际编辑SYSTEM、预览、发布和恢复，
再从正式问答接口分别核A/B/A。合成模型只根据收到的提示词标记返回正文，证明接线，不证明风格质量。
原始请求、截图与正式响应留在输出报告所指的一次性私有根；不提交真实会话或业务材料。

管家独立页面验证追加`--operator-browser`并设置`M3NODE_DEPS`（锁定依赖根，含lib/）和
`PROMPT_NODE_CLASSES`（编译后的Java类根，含operator_tools.json）。编译沿用
[节点输出验收方法](../operator-cpms/output-contract-qa.md)，必须带上
`src/aiops_diagnostics/operator/operator_tools.json`资源，不能直接复用含冲突SLF4J版本的散装缓存。
该模式跳过客户端业务场景，使用真实Dify受管编排页、发布桥、Gateway和Java；公司身份/CPMS/模型合成。
选用与签名包匹配的公钥，正式包公钥在`plugins/consumer_knowledge/signing/public.pem`。
主机高负载导致超时须记录失败；日志读取超时不能跳过compose down。若清理失败，按报告的
`DIFY_TEST_ROOT`和项目名执行同一份compose的`down --remove-orphans`，只清本次隔离栈。

## 原应用迁移与验收

导出原草稿及已发布版本，记录当前正式版本和publication-targets.json摘要。
离线生成候选，不新建替代正式应用、不改变其他节点/模型/提示词：

```bash
uv run python tools/content/upgrade_consumer_flow.py <原草稿.yml> --gateway <可信根地址> \
  --knowledge-base <当前库ID> --knowledge-plugin <已安装完整插件标识> --out <候选.yml>
```

草稿更新通过原Dify同步接口，保留hash作并发检查。先核正式App仍原版，再真实试用候选并发布。
从节点选另一可用库：保存不切正式、试用只检索候选库、发布后原App使用新库、恢复后再次用原库。
核本轮库ID/引用/模型输入无凭据，且服务器发布目标文件摘要未变。使用原App登录验收方法见live-release.md。
撤回和恢复用既有运营入口；异常恢复原发布版本，再恢复草稿；不覆盖业务数据库，不删运行审计。
若旧版本仍在用插件，不能先卸载插件；先确认恢复原HTTP版本后再回退插件宿主配置。

## 镜像与浏览器核验补充

构建机启用containerd镜像存储时，`docker image inspect .Id`可能是OCI index摘要；服务端经典Docker返回config摘要。
通过`docker save`包的manifest.json定位Config并计算SHA256，与服务端Image ID比较；同时核RootFS层和包摘要，
不要只比较镜像标签，也不要将不同种类的摘要不相等判成制品漂移。

编辑器有自动保存节流：浏览器改选后应等待携带目标库的草稿POST200，再GET草稿核库；不能固定等2秒就关闭页面。
授权/图片线索使用生成工具的固定mixed模板，直接variable引用会被现版本编辑器清空。

浏览器隧道用dev-host启动时，timeout/ssh可能另建进程组；清理应核子进程和本地端口，不能只kill包装shell的进程组。
只终止本次记录的PID及子进程，避免影响其他会话。现役控制台需要正常公司登录；维护API凭据不等于浏览器个人登录态。

## editor点开节点403的核验与交付

Dify1.17.1默认将dynamic-options设为admin/owner。公司映射账号通常是editor，必须包含
`patches/1.17.1/08-consumer-knowledge-editor-options.patch`，不能用提权账号来修界面。
通过`tenant_account_joins`只读核对当前工作区角色，并只检索API日志的dynamic-options状态码，不输出Cookie。

使用合并版本的`deploy/dify-36/patches/apply.sh`从固定原镜像核摘要、生成完整补丁树；六个文件在compose的api/worker
逐个只读挂载。先保存原compose和完整旧补丁树，通过`dev-host cp --artifact-sha256`交付合并制品，检查配置后仅重建api/worker。
保留.env、web、plugin_daemon及正式发布版，失败恢复原compose和旧补丁树后重建同两个服务。
重新检查api健康、原发布V5不变，再用editor会话打开节点；库列表应返回200，不应出现权限弹窗。
管理员API200只可作对照，不能代替这一项。浏览器完整回归已在隔离栈使用真实editor角色执行。
API冷启动时单次curl超时只代表尚未就绪：配置连接/总超时，捕获TimeoutExpired后继续轮询，使用统一总截止决定回滚。
重建前后均核原文件与配置摘要；不能因为一次立即探活失败就把已成功启动的修复判断为制品错误。

## 已装本地插件升级与模型参数（2026-11-05实测）

`install/pkg`用于首次安装；同plugin_id已有旧版时，任务报success也可能保持旧安装标识。
上传候选后核对manifest.verified及工作区实际plugin_unique_identifier，不能只等task成功。
市场关闭且使用自签项目包时，使用Dify容器内既有`PluginInstaller.upgrade_plugin`，
source为`PluginInstallationSource.Package`，传入同工作区旧/新完整标识和新标识meta；
不卸载旧版来代替升级，不重写插件目录。调用前`decode_plugin_from_identifier`核已上传签名包，
调用后按task及工作区清单确认；`PluginService.invalidate_plugin_model_providers_cache`清该工作区缓存。
通过`dev-host exec 36`运行既有服务管理API，脚本不写运行源码；只对指定的已授权工作区操作。
失败保留旧包与原标识，使用同一升级服务恢复原版；旧发布图仍依赖旧工具参数兼容性。

模型`GET .../models/credentials`返回部分掩码，不能当成真实key提交。更新同credential_id时，
秘密字段使用Dify官方`[__HIDDEN__]`哨兵，PUT同一路径仅修改function_calling_type=tool_call；
平台用原加密秘密验证，回读核原credential_id不变。先保存原非秘密参数，恢复也用同一接口。
不能另建同名凭据却忘记切默认，也不把key写进脚本或输出。

正式发布前导出当前草稿与精确route.workflow_id的版本；草稿可含未发布编辑，不用Dify当前published指针
猜Gateway正式版。用户风格要求作为业务指令保留，系统JSON协议块由迁移删除。
保存用hash并发检查，环境变量用environment_variable_patch，旧变量删除按id声明。
恢复旧正式版后草稿可能仍为新图；恢复与还原草稿是两步，最终核对草稿和正式内容是否符合交付目标。

## Agent内嵌工具的选库面板

callback6的入口是智能客服节点→consumer_knowledge工具卡片→用户设置→检索知识库。
`04-tool-dynamic-settings.patch`只覆盖独立工具节点；Agent使用另一条ToolSelector→ToolSettingsPanel→ToolForm链。
`10-agent-tool-dynamic-settings.patch`在两层传递当前工具及provider，动态选项才能请求目录；
缺这两个对象时没有网络请求、已选名称也无法解析，空下拉不能解读成知识库不存在。
构建脚本同时钉住这两份上游文件摘要；保持原目录鉴权与发布选库规则，不靠静态库ID填选项。

在任务自己的上游构建目录应用所有补丁后运行：
`pnpm --dir web exec vp test --project unit app/components/plugins/plugin-detail-panel/tool-selector/__tests__/aiops-dynamic-settings.spec.tsx`。
该测试从工具选择器跨真实表单/多选组件验证目录请求、已选回显和多选保存形状；
原浏览器还需核对同工作区接口200和列表可见，不以测试替代生产页面。

Agent v1的settings字段可以直接保存 `{type: constant, value: [...]}`，新编辑器使用
`{value: {type: constant, value: [...]}}`。10补丁按已有字段形状解包和保存，不能因点选而改变运行协议。
回归必须使用真实useToolSelector，否则mock后的settingsValue会掩盖已选值丢失。

## 参数面板打开后发布失败

先比对草稿与同应用正式图的query和工具parameters，禁止打印实际grant/凭据。
Dify1.17.1的Agent字符串query在保存时变constant，运行时仍展开模板；不要据type变化
就改模型。string手动变量必须以变量选择器编辑，不能让文本框把selector数组当空文本。
修复后从已合并补丁13构建完整web并部署，先打开参数面板确认grant引用不变，再发布。
存量坏草稿修复只替换已核对丢失的start.data_grant引用，走正常workflow draft保存并
使用当前hash；保留模型、业务指令、知识库选择及其授权。先留原始快照，回读比对后
才点击受管发布；拒绝应显示CONSUMER_FLOW_INVALID和具体原因，失败不改正式指针。
