# 管家 CPMS 工具实施

目标：Java 节点以 CPMS 正式只读 API 取证，旧七工具不作为新链依赖。
流程与验收分别见 [acceptance.feature](acceptance.feature) 和 [qa-plan.md](qa-plan.md)。

## 首片 API 契约

| 操作 | CPMS 接口 | 首片消费与限制 |
| --- | --- | --- |
| 订单详情 | `GET /chOrderInfo/getOrderInfoDetail/{orderNo}` | 校验外层与 `chOrderInfoModel` 的订单/站点，内层租户；只取诊断必需字段 |
| 充电监测样本 | `GET /iotChargingDevice/getChargingMonitor/{orderNo}?current=1&size=50` | 逐条校验订单/租户/站点；只取第一页最多50条，明确非完整时间线、无排序保证 |

两接口统一使用 CPMS 的 `code=0,data` 信封，HTTP 200 不等于成功。
源时间字段保留 CPMS 原文，不把没有时区的本地时间伪装成 UTC；取证时间另记 UTC。
监测缺失是缺证，不能据此推断设备没有故障。认证凭据来自当前请求授权，不来自模型或工具参数。

本次 API-01 检查了公司 release 分支、控制器/DTO/查询实现及线上制品的对应类与映射；线上制品没有 Git 修订元数据，因此只记录制品摘要，不声称等于最新 release。原件与对应关系仅在私有验收根保留，方法沿用[公司源码规程](../company-code-recon-procedure.md)和[环境手册](../env-41-runbook.md)。

还不能纳入首片的接口：

- 设备故障记录有设备/站点/时间过滤，但个人权限与跨对象关联尚待验证。
- 设备当前状态不能代表订单发生时状态；只列候选，不将其当成历史根因证据。
- 发收据、在线探测和配置读取等接口须审查实际副作用，不因使用 GET 就视为只读。
- 旧 `/diag/*`、SQL、TDengine、Redis 直连不作为缺 API 时的替代路线。

## 范围约束

任务授权至少绑定当前主体、租户、站点集合、订单、工具、截止时间和 CPMS 固定入口。工具执行前与交付前均核对同一授权；读取期间撤权只停止后续交付，不能撤回已发送请求。CPMS 响应仍逐行校验归属，不只依赖服务凭据或前端提交的站点。

新节点入口只接受个人公司令牌及 `X-Business-Entry: operator`，携带旧 `X-Third-Session` / `third-session` 时在签发或历史正文回读前拒绝，不能拿共享服务令牌访问 CPMS。历史回读复用既有 `assistant_identity` 的诊断认证，再核对当前站点与 CPMS 订单归属。公司解析器当前没有独立诊断权限码，`aiops:diagnoses:write` 是解析器接口参数，不是已核验的公司授权码；本轮不增加或虚构公司权限。可单独撤销该能力的解析器必须在回读时同样拒绝，真实公司边界仍需 LIVE-01 验证。

租户主账号沿用公司的 `all` 范围，员工仍按当前站点集合；员工空集合、代查与主体类型不符的 `all` 均拒绝。签发时先用 CPMS 核对租户和订单，将该订单的一个站点冻结进节点凭据；节点不持有全租户站点通配权限。取证、完成和回读中订单站点变化均拒绝，不交旧站点报告。

节点工具、Gateway 授权签发、Dify 发布和 App 页面是不同的交付项；只有实际接通并通过对应验收才计完成。旧工具目录及运行时在新链通过并核对实际引用后退役，历史记录保留。

## 配置与复跑

沿用 `CHAT_NODE_TARGETS_FILE`，管家行增加 `entry: operator`、`cpms_base_url`（公司 CPMS HTTPS 基址）、`proxy_base_url`（节点可达的 Gateway HTTPS 基址）。节点清单模板仅包含 `cpms_order_detail`、`cpms_charging_samples` 的真实定义，见 Java 工具；不在模板或 Dify 配置中填写公司令牌。业务入口为原 `POST/GET /v1/assistant/questions`，必须是 operator 身份与显式订单。

控制台试用额外配置：Gateway 的 `AIOPS_GATEWAY_OPERATOR_TRIAL_LOGIN_URL` 与 `AIOPS_GATEWAY_OPERATOR_TRIAL_KEY`；登录代理的 `AIOPS_LOGIN_PROXY_OPERATOR_TRIAL_KEY` 使用同一把独立高熵密钥，至少32字符。该链只在配置后开启，登录代理最多在内存保留128个近期会话对应公司令牌，15分钟到期；到期访问时清理，重启后丢失。内部令牌交换路径仅供已认证 Gateway，经已批准 HTTPS 转发暴露，不向浏览器返回令牌。

HTTPS server 可包含 [operator-token.conf.template](../../../ops/dify-login-proxy/operator-token.conf.template) 渲染件：仅替换 `AIOPS_OPERATOR_GATEWAY_SOURCE_IP` 为经私有主机记录核对的单一网关出口 IP，保留 nginx 自有变量；不要包含到明文控制台入口。按原始 TCP 对端限制来源，不采信 `X-Real-IP` / `X-Forwarded-For`；允许来源仍需代理核验独立密钥和真实会话。先 `nginx -t` 再 reload，并分别从网关、非网关来源验证。恢复删除本次精确 include、回滚配置键，不覆盖问答库。

此模板要求Gateway直达HTTPS入口，部署前用连接目标与入口访问记录共同核对；不得把CDN、负载均衡或共享前置代理的IP代填为Gateway来源。出现前置代理时停止启用并另行设计身份边界，不能仅扩大来源名单。

管家 App 密码登录与 Dify 运营登录分开验收。公司管家客户端使用自己的 `tenant-app` 密码流程；不能把 Dify 页的图形验证码要求套到它，也不能自行给 Dify 会话缓存注入公司令牌。

运营正常登录后，原试运行按钮携带其 Dify 会话交给 Gateway；草稿清单仅冻进试用 run，正式指针不变。无近期登录凭据时明确拒绝，不运行旧 Python 试用。发布与历史恢复继续使用原按钮；全部关闭工具能发布为空清单，新诊断拒绝。

```bash
M3NODE_DEPS=<锁定依赖目录> M3NODE_JDK_IMAGE=<锁定JDK镜像digest> \
  uv run python tools/m2_close/m3node/verify_operator_cpms.py --out <私有报告目录>
uv run pytest tests/test_operator_cpms.py tests/test_node_runtime_deploy.py
```

报告中真 Gateway/Java 节点，CPMS/模型/目录/Dify 草稿为合成；节点真实获取授权、消费HTTP结果、上报完成。覆盖正常、供应商失败、末轮截断、草稿试用及发布全关。单工具 HTTP 验收还覆盖参数注入、错归属、畸形/超大响应、重定向与到期。

部署前先交付合并制品：更新节点 JAR、Gateway、节点回调配置及控制台桥/登录代理。仅部署代码不启用管家目标。真实账号、真实接口与运营控制台验收通过前，不删除旧工具或旧配置；退役必须核对实际调用与应用引用。

工具登记命令（在已配置控制台与目标文件的环境中）：

```bash
aiops --config <Gateway配置> admin ensure-node-provider \
  --app-id <已登记管家应用> --gateway-url <Gateway的HTTPS地址> --db <现有gateway.db>
```

新 provider 名为 `cpms-node`，不保存共享业务凭据。定义指向真实 `/v1/operator-tools/{tool}` 受控端点；没有节点任务凭据时403，不返回固定说明冒充结果。正式执行仍由节点调用固定 CPMS GET 路径；Dify 编辑器用工具选择发布权限，试用按钮运行该节点。

## 真实 Dify 隔离补验方法

复用 `deploy/dify-chat/docker-compose.acceptance.yaml` 的一次性栈与 `verify_dify_chat_entry.py` 初始化方法；API仅动态映射到回环，其他服务共用临时容器网络。用 `DifyConsoleAdmin` 登记 `cpms-node`；导入 `agent-chat`，`model_config.agent_mode.tools` 中每项携带真实provider UUID、`provider_type: api`、工具名和enabled。模型provider使用官方插件及合成上游，保存配置需要可用模型。

通过 `POST /console/api/apps/{app_id}/model-config` 保存双开、单开、全关；每次用真实 `DifyDslClient` 导出，经 `node_publication.candidate` 核对清单。将该应用接入临时Gateway发布登记，驱动原 `/v1/dify/publish`、`/trial`、`/retract`、`/restore`，使用 `verify_operator_cpms.py` 的真Java启动方式核对实际工具与CPMS请求。草稿只试用；正式指针不变。验收后 compose down，仅清本次数据卷与命名空间；不使用现役Dify配置。

登录会话通过RFC 7519的随机jti区分同秒登录。新会话不会恢复已退出会话的公司令牌映射；不等于Dify全局会话撤销。原试运行面板展示完整合并文本，保留既有4000字符上限。
