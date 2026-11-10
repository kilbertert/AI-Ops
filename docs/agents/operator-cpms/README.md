# 管家 CPMS 工具实施

更新：2026-11-05。新合同及配套迁移以[系统架构的管家链](../../system-architecture.md#管家链sop决定取证步骤工具承担计算)为准；
已部署与原页面结果见[本轮验收](../../validation/2026-11-05-operator-agent-live.md)。
以下首片和JSON合同描述保留历史取舍，不再指导新应用发布。
节点、原管家H5与Dify原生预览已有真实链路证据，
见[退役验收](../../validation/2026-10-29-runtime-retirement-live.md)及
[站点清单验收](../../validation/2026-11-03-site-delivery-production-receipt.md)。
首页新增入口已撤回，原聊天页仍可访问；不是“整页未部署”或“路由仍撤回”。

最终JSON与字段占位符协议已退役，现役SOP工具循环、针对性回答和追问已跑原入口；
第二租户失效路由已恢复。业务拒绝回喂及运行中换版归因也有后继补验，
见[当前交付](../current-delivery-state.md)。完整权限/生命周期矩阵和故障准确率仍待补证。
模型参数排查见[手册](../dify-model-parameters.md)，不能用普通模型回复代替订单工具验收。

站点查询在取证前传`address_terms`数组，最终答复复用同一组关键词；节点代理和直接查询
共用有界读取，旧`address`字符串保持字面兼容。部署须同步节点jar、工具声明和Gateway，
详见[真实工具链QA](site-query-real-path-qa.md)。关键词匹配不等于距离或行政区精确定位。

目标：Java 节点以 CPMS 正式只读 API 取证，旧七工具不作为新链依赖。
管家问答现区分无订单咨询/站点搜索与单订单诊断，订单不是聊天门槛；
见[场景](conversation.feature)、[QA与限制](conversation-qa.md)。
流程与验收分别见 [acceptance.feature](acceptance.feature) 和 [qa-plan.md](qa-plan.md)。

运营只维护业务SOP和工具选择。新管家发布/试用自动追加工具与证据要求，Java节点以自然语言
交付最后完整模型轮；结构化协议仅用于工具交互。旧JSON协议只保留历史与回退解释。
遇到`OPERATOR_INVALID_ANSWER`时核对节点制品、输出模式和完成回合边界，不能通过放宽
证据校验止错。详见[输出契约QA](output-contract-qa.md)与[决策](../../adr/0163-operator-output-is-a-runtime-contract.md)。

## 失败原因排查

模型服务额度不足时，原问答与Dify预览应显示补充额度提示；普通模型/节点拒绝只能说明依赖不可用。
仅反复修改SOP或重试无法恢复耗尽的账户。失败分类要求节点与Gateway配套部署，
核对步骤和回归见[输出契约QA](output-contract-qa.md#额度失败回归2026-11-04)。
本次候选修复的部署与实测状态见[验收记录](../../validation/2026-11-04-operator-failure-propagation.md)。

## 首片 API 契约（历史字段与限制参考）

**报告必须回答用户问的那一点。** 旧答案的 `direct_answer` 曾为必填非空，且渲染在最前；
没有它，任何提问都只能被概述一遍 —— 2026-10-29 反馈的「两个问题同一份报告」就是
这个形状（[ADR-0171](../../adr/0171-operator-report-answers-the-question-asked.md)）。
订单详情取证已覆盖金额、退款、额定功率与接口类型，问电费/功率不再只能答「未提供」。

| 操作 | CPMS 接口 | 首片消费与限制 |
| --- | --- | --- |
| 订单详情 | `GET /chOrderInfo/getOrderInfoDetail/{orderNo}` | 校验外层与 `chOrderInfoModel` 的订单/站点，内层租户；取证字段见 `operator_cpms.DETAIL_FIELDS`（含金额、退款、SOC、额定功率），两侧必须逐项相同 |
| 充电监测样本 | `GET /iotChargingDevice/getChargingMonitor/{orderNo}?current=1&size=50` | 逐条校验订单/租户/站点；只取第一页最多50条，明确非完整时间线、无排序保证；`samples_returned`/`absence` 区分「没上报」与「被截断」 |

两接口统一使用 CPMS 的 `code=0,data` 信封，HTTP 200 不等于成功。
源时间字段保留 CPMS 原文，不把没有时区的本地时间伪装成 UTC；取证时间另记 UTC。
监测缺失是缺证，不能据此推断设备没有故障。认证凭据来自当前请求授权，不来自模型或工具参数。
公司 `iot_power_runtime` 只保留 7 天（`CleanTableDataJob` 每日删 7 天前数据），
超出窗口的历史单必然 0 条样本 —— 那是保留期，不是设备故障证据。

本次 API-01 检查了公司 release 分支、控制器/DTO/查询实现及线上制品的对应类与映射；线上制品没有 Git 修订元数据，因此只记录制品摘要，不声称等于最新 release。原件与对应关系仅在私有验收根保留，方法沿用[公司源码规程](../company-code-recon-procedure.md)和[环境手册](../env-41-runbook.md)。

还不能纳入首片的接口：

- 设备故障记录有设备/站点/时间过滤，但个人权限与跨对象关联尚待验证。
- 设备当前状态不能代表订单发生时状态；只列候选，不将其当成历史根因证据。
- 发收据、在线探测和配置读取等接口须审查实际副作用，不因使用 GET 就视为只读。
- 旧 `/diag/*`、SQL、TDengine、Redis 直连不作为缺 API 时的替代路线。

## 范围约束

任务授权至少绑定当前主体、租户、站点集合、订单、工具、截止时间和 CPMS 固定入口。工具执行前与交付前均核对同一授权；读取期间撤权只停止后续交付，不能撤回已发送请求。CPMS 响应仍逐行校验归属，不只依赖服务凭据或前端提交的站点。

新节点入口只接受个人公司令牌及 `X-Business-Entry: operator`，携带旧 `X-Third-Session` / `third-session` 时在签发或历史正文回读前拒绝，不能拿共享服务令牌访问 CPMS。历史回读复用既有 `assistant_identity` 的诊断认证，再核对当前站点与 CPMS 订单归属。公司解析器当前没有独立诊断权限码，`aiops:diagnoses:write` 是解析器接口参数，不是已核验的公司授权码；本轮不增加或虚构公司权限。可单独撤销该能力的解析器必须在回读时同样拒绝，真实主账号历史单API已回放；跨主体真例、员工范围与正式页面仍需LIVE-01验证。

租户主账号沿用公司的 `all` 范围，员工仍按当前站点集合；员工空集合、代查与主体类型不符的 `all` 均拒绝。签发时先用 CPMS 核对租户和订单，将该订单的一个站点冻结进节点凭据；节点不持有全租户站点通配权限。取证、完成和回读中订单站点变化均拒绝，不交旧站点报告。

节点工具、Gateway 授权签发、Dify 发布和 App 页面是不同的交付项；只有实际接通并通过对应验收才计完成。旧工具目录及运行时在新链通过并核对实际引用后退役，历史记录保留。

## 配置与复跑

沿用 `CHAT_NODE_TARGETS_FILE`，管家行增加 `entry: operator`、`cpms_base_url`（公司 CPMS HTTPS 基址）、`proxy_base_url`（节点可达的 Gateway HTTPS 基址）。节点清单模板的工具集合以 `operator_tools.json` 为唯一声明（节点经 `-Daiops.node.tools-file` 读同一份文件）；不在模板或 Dify 配置中填写公司令牌。业务入口为原 `POST/GET /v1/assistant/questions`，必须是operator身份；新合同允许无订单咨询，
需要订单事实时模型提议对象，系统授权后才读取。下述首片的显式订单要求不适用于所有新问题。

`cpms_charging_timeline` 另需 `AIOPS_GATEWAY_OPERATOR_TSDATA_URL` 指向公司 tsdata 查询门面（`/tdadmin/data/query` 前缀）；空值时该工具按依赖不可用拒绝，不静默退化。

控制台试用额外配置：Gateway 的 `AIOPS_GATEWAY_OPERATOR_TRIAL_LOGIN_URL` 与 `AIOPS_GATEWAY_OPERATOR_TRIAL_KEY`；登录代理的 `AIOPS_LOGIN_PROXY_OPERATOR_TRIAL_KEY` 使用同一把独立高熵密钥，至少32字符。该链只在配置后开启，登录代理最多在内存保留128个近期会话对应公司令牌，15分钟到期；到期访问时清理，重启后丢失。内部令牌交换路径仅供已认证 Gateway，经已批准 HTTPS 转发暴露，不向浏览器返回令牌。

HTTPS server 可包含 [operator-token.conf.template](../../../ops/dify-login-proxy/operator-token.conf.template) 渲染件：仅替换 `AIOPS_OPERATOR_GATEWAY_SOURCE_IP` 为经私有主机记录核对的单一网关出口 IP，保留 nginx 自有变量；不要包含到明文控制台入口。按原始 TCP 对端限制来源，不采信 `X-Real-IP` / `X-Forwarded-For`；允许来源仍需代理核验独立密钥和真实会话。先 `nginx -t` 再 reload，并分别从网关、非网关来源验证。恢复删除本次精确 include、回滚配置键，不覆盖问答库。

此模板要求Gateway直达HTTPS入口，部署前用连接目标与入口访问记录共同核对；不得把CDN、负载均衡或共享前置代理的IP代填为Gateway来源。出现前置代理时停止启用并另行设计身份边界，不能仅扩大来源名单。

管家 App 密码登录与 Dify 运营登录分开验收。公司管家客户端使用自己的 `tenant-app` 密码流程；不能把 Dify 页的图形验证码要求套到它，也不能自行给 Dify 会话缓存注入公司令牌。

运营正常登录后，原试运行按钮携带其 Dify 会话交给 Gateway；草稿清单仅冻进试用 run，正式指针不变。无近期登录凭据时明确拒绝，不运行旧 Python 试用。发布与历史恢复继续使用原按钮；全部关闭工具能发布为空清单，新诊断拒绝。

```bash
M3NODE_DEPS=<锁定依赖目录> M3NODE_JDK_IMAGE=<锁定JDK镜像digest> \
  uv run python tools/experiments/m2_close/m3node/verify_operator_cpms.py --out <私有报告目录>
uv run pytest tests/operations/test_operator_cpms.py tests/test_node_runtime_deploy.py
```

报告中真 Gateway/Java 节点，CPMS/模型/目录/Dify 草稿为合成；节点真实获取授权、消费HTTP结果、上报完成。覆盖正常、供应商失败、末轮截断、草稿试用及发布全关。单工具 HTTP 验收还覆盖参数注入、错归属、畸形/超大响应、重定向与到期。

部署前先交付合并制品：更新节点 JAR、Gateway、节点回调配置及控制台桥/登录代理。仅部署代码不启用管家目标。真实账号、真实接口与运营控制台验收通过前，不删除旧工具或旧配置；退役必须核对实际调用与应用引用。

工具登记命令（在已配置控制台与目标文件的环境中）：

```bash
aiops-admin --config <Gateway配置> ensure-node-provider \
  --app-id <已登记管家应用> --gateway-url <Gateway的HTTPS地址> --db <现有gateway.db>
```

新 provider 名为 `cpms-node`，不保存共享业务凭据。定义指向真实 `/v1/operator-tools/{tool}` 受控端点；没有节点任务凭据时403，不返回固定说明冒充结果。正式执行仍由节点调用固定 CPMS GET 路径；Dify 编辑器用工具选择发布权限，试用按钮运行该节点。

## 真实 Dify 隔离补验方法

复用 `deploy/dify-chat/docker-compose.acceptance.yaml` 的一次性栈与 `verify_dify_chat_entry.py` 初始化方法；API仅动态映射到回环，其他服务共用临时容器网络。用 `DifyConsoleAdmin` 登记 `cpms-node`；导入 `agent-chat`，`model_config.agent_mode.tools` 中每项携带真实provider UUID、`provider_type: api`、工具名和enabled。模型provider使用官方插件及合成上游，保存配置需要可用模型。

通过 `POST /console/api/apps/{app_id}/model-config` 保存双开、单开、全关；每次用真实 `DifyDslClient` 导出，经 `node_publication.candidate` 核对清单。将该应用接入临时Gateway发布登记，驱动原 `/v1/dify/publish`、`/trial`、`/retract`、`/restore`，使用 `verify_operator_cpms.py` 的真Java启动方式核对实际工具与CPMS请求。草稿只试用；正式指针不变。验收后 compose down，仅清本次数据卷与命名空间；不使用现役Dify配置。

登录会话通过RFC 7519的随机jti区分同秒登录。新会话不会恢复已退出会话的公司令牌映射；不等于Dify全局会话撤销。原试运行面板展示完整合并文本，保留既有4000字符上限。

## 现役窗口与回执

执行记录见[真实历史回放](../../validation/2026-10-26-operator-real-history-replay.md)。先保存现役应用DSL和配置摘要，独立查询CPMS核对输入，用户令牌只通过受控输入交给请求进程；不以SQL或旧工具代替节点本次取证。发布后从原问答POST拿qa_id，再按qa_id回读唯一run与同轮快照，不能取最新一行。

撤回使用原 `/v1/dify/retract` API。发布密钥由实际Gateway的 `GatewayServerSettings.from_env()` 读取，不能假定一定在 `/proc/<pid>/environ` 的同名键里；用服务虚拟环境、项目身份和实际进程环境运行，**不要构造第二个生产app**。恢复程序先独立验证配置读取和HTTP错误处理，再交给一次性systemd timer；手动finally与timer不能成为同一个未验证脚本的两份假保证。撤回200之后还须核对路由retracted及本人历史GET410，随后清理本次timer。任何失败都保留QA/run，不覆盖数据库旧副本。

### 历史计费与SOP分析

见[SOP反馈接续](sop-followup.md)及[验收计划](../../acceptance/operator-pricing/qa-plan.md)。
费用工具读取订单当时快照；分时展示电量不等于独立电表。Gateway按同轮证据生成并冻结
统计、算式与缺证范围；结果不执行实际收费、退款或后台重算。

### 更新已存在的工具提供方

新增工具时对目标应用运行同一`ensure-node-provider`命令并显式加`--migrate`。
Dify1.17.1更新载荷需要original_provider、credentials和icon；现役命令保留图标/标签/说明，
继续auth_type=none，收到success才记成功。先备份原提供方schema与应用model_config，
再正常保存应用工具选择、预览并发布；不删建提供方或手改Dify数据库。

## 统一自然语言与SOP模型循环

新合同`operator-agent/1`把语义和工具顺序交给模型；Gateway逐次校验对象和权限。
订单工具可接收用户提供或会话确认的order_no；每run只绑定一单，不接受模型提供身份或URL。
`cpms_order_amount_check`/`cpms_order_process_stats`读取本轮已有证据计算并将结果回交模型，
不执行后台重算。模型直接交付自然语言，按工具结果写必要证据和算式；不要求JSON或字段路径占位符。

运营将[只读SOP种子](../../../src/aiops_diagnostics/operator/operator_sop.md)保存到应用编辑区，
可以编辑业务顺序；发布快照包含实际SOP与所选工具，后续修改必须重新预览/发布。
仓库种子文件不会热改线上提示词，不默认把旧根SOP的SQL/Redis操作变成可执行能力。
原H5与Dify预览共享同一个模型执行合同及受控历史，读到旧答案前重新验证其对象可见性。
详见[验收](../../acceptance/operator-agent-loop-qa-plan.md)及[节点迁移](../../../deploy/node-runtime/README.md#迁移到管家统一模型循环operator-agent1)。

### 自然语言答复的事实复核

运行快照的`answer_review`分别记录模型判定和抽取的`currencies`/`station_names`。
排查时按本次qa_id读取，不能取最新运行代替；只有已交付且当前仍授权的证据可支持实体。
站点查询使用records.name，未来方案使用已解析rules.name，用户搜索参数不属于证据。
现役工具未提供币种，金额仅展示记录数值；未来增加币种须同步工具投影及比对合同。

真实页面验收除completed还需逐句核对：币种/站名/优惠/税额限定，不能把HTTP成功当业务通过。
先保存SOP和provider备份，草稿预览通过再正常发布；系统提示变更需要重新发布快照。
SOP顺序实验用两个可执行顺序分别预览同题，以同轮evidence.observed_at排序；正常接口恢复草稿，
复核正式revision未因草稿变化而推进。原始数据与页面状态只保存在私有证据目录。

设备离线分析候选工具与配置、上线步骤见[离线手册](device-offline.md)；只读记录/时间关联不能代替物理故障根因。
