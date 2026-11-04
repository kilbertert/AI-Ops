# 自由提问故障定位

推荐问题可以直接返回固定FAQ；自由文本必须先由`GatewayRuntime.classify_consumer`选择能力，
再由网关核对授权、读取允许的数据并启动回答。因此推荐问答成功不能证明自由问答可用。

## 原因与处置

`common.model_request`复用HTTP骨架保留受限原因，绝不输出`HttpFailure.detail`、问题或凭据。

| 原因 | 可核对证据 | 处置 |
|---|---|---|
| quota_exhausted | error.code/type明确为insufficient_quota/insufficient_user_quota；或已实测供应商403含“用户额度不足” | 管理员核对余额/额度，恢复后重试；不自动循环 |
| auth_rejected | 401/403，且无上述额度证据 | 核对服务身份与模型凭据，不能要求终端用户充值 |
| rate_limited | 429且无额度证据 | 等待限流恢复；本次调用仍只执行一次 |
| transport_unavailable | 连接/读超时等传输失败 | 核对服务所在网络的目标可达性 |
| invalid_response/upstream_error | 非法响应或其他HTTP错误 | 检查服务依赖；不猜成业务意图 |

分类语义澄清是合法分类结果，与以上依赖失败分开。失败不降级到关键词或FAQ，不查订单。
模型请求的结构化校验、无工具、无重定向与单次调用约束保持有效。
依据[官方模型错误说明](https://developers.openai.com/api/docs/guides/error-codes)区分限流与额度，
并沿用[RFC 9110服务错误语义](https://www.rfc-editor.org/rfc/rfc9110.html#section-15.6)。
供应商403中文形状属于显式兼容规则，不是HTTP标准。

## 按同一次请求对照

1. 按[环境手册](../env-41-runbook.md)先GET健康，记录实际版本；开发分支通过不代表已部署。
2. 只读查询请求时间附近的服务日志：
   `dev-host exec 41 --allow-service-exec -- journalctl -u aiops-gateway-41.service --since <ISO时间> --until <ISO时间> --no-pager`。
   用相同时间和设备UA核对访问日志状态；分类失败发生在建QA前，QA为空不表示手机没请求。
3. 若需独立分类探针，取服务MainPID，从`/proc/<pid>/environ`在内存复制运行环境，
   以服务身份在部署目录调用`GatewayRuntime.classify_consumer`，只构造带
   `GatewayServerSettings.from_env()`的轻量参数对象。**不构造GatewayRuntime或ASGI应用**，
   避免启动恢复逻辑改写在途任务。配置由`Settings.from_config`读取；密钥不进参数/输出。
4. 恢复后用adbx从已登录客服页面自由输入原句；保存前后截图、UI、UTC和APK版本，
   独立核对QA completed/运行succeeded。连续两句还需对照服务端conversation_id；
   同一屏出现两条气泡并不证明沿用了上下文。

原始截图、账号、日志和响应留库外私有目录。代码回归使用临时HTTP模型返回合成故障，
不修改生产密钥或余额制造失败；用例见[验收计划](../../acceptance/consumer-free-question-qa-plan.md)。
