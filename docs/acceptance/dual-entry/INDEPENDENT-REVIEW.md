# 双端开通独立验收：退回修复

日期：2026-10-30。验收者：Codex。基线`4403681e3c19a8933e2fb9700ce75d5f757fde09`。
范围：#1224/#1225/#1226/#1227/#1229及执行回执；保持Claude开发、Codex独立验收的分工。

**结论：本轮不合格。** 既有34项契约、10项组件浏览器用例复跑通过，但独立跨层/恢复探针
10项全部失败。不是只差前端部署或账号。以下反例均未修改生产数据、账号权限或业务代码。

## 1. 本次实际执行

| 验证 | 结果 | 限制 |
| --- | --- | --- |
| 回执的3组新增契约 | 34 passed，23.92秒 | 外部Dify/依赖为替身，不经过真实浏览器桥 |
| 回执的组件浏览器用例 | 10 passed，7.90秒 | 组件/service/test与仓库逐项sha相同；不是完整Chatflow编辑页 |
| 独立跨层与恢复探针 | 10 failed，14.27秒 | 本地临时数据库、HTTP桥、合成公司/Dify/模型；没有真实业务写入 |
| 原间歇回读用例 | 同进程独立临时根5次PASS | 未重现CI问题，不证明已修复 |
| 生产只读回读 | Gateway `1.0.0+bec88a1ae690`；Dify web仍`knowledge-1be3f067` | 只证明实际版本，不证明新双端功能上线 |
| 库外加密资产 | 平台和两个租户所需资产字段存在 | 仅打印资产ID及字段存在布尔值，未输出凭据、未发起登录 |

私有证据目录：`/home/claude/.cache/aiops-dify-self-service-research/dual-entry-review/`。
`test_independent.py`、`all-probes.log`为10项反例；`browser-contracts.log`为浏览器复跑。
34项日志为其父目录的`dual-entry-independent-contracts.log`。
从待复核checkout运行（脚本从cwd加载该checkout的src/tests）：

```bash
uv run pytest -n 0 -s --rootdir=. \
  /home/claude/.cache/aiops-dify-self-service-research/dual-entry-review/test_independent.py
```

这是定位缺陷的反例脚本，不是完整正向验收：例如R1当前只断言不能在桥层因字段被拒，
R2复用Dify补丁的真实字段约束而没有启动完整Dify。修复须补成真实路径成功断言，不能只让探针不报错。

## 2. 必须修复的代码问题

### R1 [P1] 页面提交在同源桥被拒绝

新组件发送`scope`和`sources`，`dify/self_service_bridge.py:45`的字段白名单仍不包含它们。
完整HTTP桥收到合法同源/CSRF的双端payload，返回400 `unexpected field in request body`，未到Gateway。
映射：A/B/K。修复固定管理动作的请求契约，保留未知字段/越权拒绝；测试必须串起页面实际payload→桥→API。

### R2 [P1] 客户端导入ID不符合Dify补丁契约

`self_service_prepare.py:300`发送`request_id=job_id+":consumer"`；
`09-idempotent-app-import.patch:13`只接受`^[a-f0-9-]{36}$`。
使用原补丁约束进行Pydantic验证，确定性报`string_pattern_mismatch`；目前Dify替身漏了这条校验。
映射：A/C2。两侧统一命名空间/合法ID和app_id推导，验证响应丢失后仍回读相同资源，不靠放开任意输入。

### R3 [P1] 客服发布组件没有挂到真实Chatflow页面

源码核查：`04-self-service.patch`只把ManagedPublish挂进`app/configuration/configuration-view`。
固定Dify源码的`/app/[appId]/workflow`渲染`WorkflowApp`，不是上述Configuration；该树没有接入组件。
同补丁的预览发送仍统一写`/__aiops/operator-preview`，没有Chatflow客户端对应接线。
10项测试只单独render组件，不足以证明实际客服页能发布/预览。此外首页仍没有规划要求的入口。
映射：A/E/K。接入真实Chatflow编辑/预览/发布生命周期及首页入口，保留普通Dify应用原行为；完整页验收。

### R4 [P1] 未发布客服草稿必被试用检查拒绝

`gateway_api._managed_app_draft`生成`state=draft`，`RouteBinding.active`只有active状态才为真；
`_workflow_trial_draft`在`gateway_api.py:1254`又无条件要求`row.active`。
同租户调用实际builder稳定403 `TRIAL_SCOPE_MISMATCH`。现有3项试用测试只验合成route和目录存在。
映射：E1。区分受管草稿与正式路线校验，必须完整执行一次尚无正式路由的草稿，验证不写正式指针。

### R5 [P1] 多客服草稿被错误当成一个发布目标

`self_service_store.consumer_target:205`按租户/环境`fetchone()`，没有按app_id选候选；
`consumer_publication.target_for:143`随后只承认第一条应用。
创建第一客服正式版后再复制第二草稿，第二草稿发布稳定`SETUP_APP_NOT_MANAGED`。
现役文件兼容还按租户整体冲突，需一起核验存量应用换副本。
映射：B/E/K。候选目录按应用身份取目标，正式槽位按路由键独立管理；第二草稿试用/发布/恢复都要覆盖。

### R6 [P1] 客服发布忽略操作者确认的草稿摘要

`SelfService.publish:656`直接进入`_publish_consumer`，丢弃`expected_draft`；管家分支才核摘要。
传入全零过期摘要仍返回成功，独立拒绝断言`DID NOT RAISE`。
映射：E3。把真实Chatflow草稿hash/内容摘要贯穿保存→发布→冻结，来源在窗口中变化则拒绝，不能发布另一标签页内容。

### R7 [P1] 重试会重新启用人工撤回的客服

`_run_consumer:636`把“没有路由”和“路由已撤回”都作为首次发布。
在首次成功推进指针后模拟回执丢失，随后正常撤回，再重试原任务，结果`active/revision3`。
映射：C/D/E4。重试只对账已有结果，不覆盖随后撤回/换版决定；两端使用同等语义。

### R8 [P1] 密钥对账只在一次函数调用内有效

`service_keys.ensure:76`的创建前快照只在内存。模拟POST创建成功但响应丢失、随后GET也超时；
第二次重试把已产生的key当“原有”，再次POST，观察到`key_posts=2`。
映射：C3/L1。持久保存操作关联的创建前ID/阶段或明确受管key身份，重启后对账；不能猜用或删其他集成key。

### R9 [P1] 首次发布失败后留下不可重试的agent

`sync_workflow_from_dify:1005`先创建草稿agent，后续外部publish失败；重试时`1009`分支拒绝任何非published agent。
仅注入一次发布超时，再重试即`failed / SETUP_AGENT_UNAVAILABLE`，没有进入第二次正常发布。
映射：C/D。安全识别属于原操作的初始草稿并续跑，不能创建新agent绕过或删除不明草稿。

### R10 [P1] 对客/内部知识边界尚未实现

规划和实施规格要求“属于租户＋允许对客＋应用选中＋当前可用”，实际仍只调用
`consumer_knowledge_selection.require_owned`。输入本租户`internal-only`库，发布契约照常接受。
创建名为“对客知识库”的空库不能建立授权；按名称复用还可能选到同名内部资料。
映射：G1/G2。增加明确可分发属性或同等服务端登记，发布、实际检索和撤销后的访问均检查；
补本租户内部资料负例，不能拿跨租户拒绝代替。当前反例只证明策略缺失，不声称生产发生了泄露。

### R11 [P1] 客户端依赖/业务连接没有真正自动准备

确定性反例：仅为租户A登记CPMS，开通租户B返回`completed/published=true`，
实际`ConsumerToolGrants._cpms_base(B)`仍报`TENANT_ENDPOINT_UNRESOLVED`。
源码核查：公共准备只安装管家profile模型和`cpms-node`，没有安装/配置consumer_knowledge插件、
目标workspace目录凭据，也没有校验/重绑来源Chatflow使用的各模型凭据；现有用例把`dependencies`整体mock掉。
映射：A/G/H/J。按已批准环境连接建立受管客户端端点，准备实际源图依赖并回读验证。
只开客户端不应依赖无关管家provider成功；不能用全租户兜底或复制源客户密钥掩盖缺口。

## 3. 配套交付与回执必须修正

R12：前端、桥、相关依赖完成构建、合并制品部署及原页面验收；渠道声明应根据已观察的真实入口补齐，
不把每客户编辑服务器清单作为最终业务操作。只有匿名401与health无法证明“边界未放宽”。
更新回执的PASS映射：E1当前失败；G2缺策略；L2用“恢复别的app版本”不能证明“执行组件不匹配阻止启用”。
若实施任务只有本地tasks.md，核对并补齐原任务书要求的规格/原生子任务与依赖链接。

`BLOCKED.md`要求重新授权是错误的：原任务书§3.4已授权配套部署，§4已授权正常使用库外账号。
本轮只核字段存在：`aiops-41-platform-dify`、`aiops-ulink-phone-20261030`、
`aiops-youlianchong-phone-20261030`均存在。沿用既有入口部署不是新增公网暴露；不用用户再次交密码。
账号有无当前权限仍需正常登录核验，不能因字段存在就伪造业务通过。
前端制品应在开发机/runner完整构建，服务宿主只接收已合并制品；
恢复应回到切换前**准确的补丁镜像和配置**，不能清空DIFY_WEB_IMAGE退到上游裸镜像而丢失已有功能。

R13：原CI间歇回读失败本轮5次未复现，继续作为未解决关注项。提取原失败日志判明
“200返回了被篡改正文”还是时序/测试观察问题，保留失败并构建确定性回归；不能简单写“不影响交付”。

## 4. 下一轮顺序与完成条件

先修R1/R2/R3让真实页面到后端可达，再修R4—R11的生命周期与权限/依赖，最后完成R12配套部署。
每组先把反例转入对应测试目录，补正向成功与负向拒绝；不得删除/降级既定A-L合同。
本轮不要求把整个产品改成新引擎或新凭据平台。
至少两个租户对照，一个完成页面双端或为现有管家补客服；真实消费者/员工各走原入口。
返回修订后EXECUTION-RECEIPT.md，追加R1—R13处理矩阵及精确提交、命令、证据；Codex再复核。

本轮未部署不合格前端，未修改业务实现。现役Gateway保持运行；未执行任意真实越权请求。
