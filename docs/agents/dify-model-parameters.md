# Dify 自定义模型参数冲突排查

适用：Dify 编辑器预览报 `Model Parameter max_tokens should be less than or equal to ...`。
这是模型插件本地参数校验错误；仅凭它不能判断 key、供应商、知识库或 CPMS 不可用。

## 两处预算与能力声明

- 应用：`model_config.model.completion_params.max_tokens`，本轮最多生成多少 token。
- 模型凭据：`max_tokens_to_sample`，插件允许应用设置的上限。
- 模型上下文：`context_size`，不等于单次输出预算；不要为修前两项随意更改。
- Agent 使用工具时，还须核 `function_calling_type` 和 `stream_function_calling`。
  自定义模型切换只改 provider/name 而保留应用参数，可能产生预算冲突或丢失工具能力。

2026-10-28 现场：管家 Agent 原预算 4096；10 月 26 日新建 `v41-baoyun` 凭据写了
上限 1000、no_call、not_supported，导致预览在访问上游前失败。当时验证的是未显式设置
max_tokens 的客服 Agent，不能替代对管家 Agent 的验证。

## 可复跑步骤

1. 按 [环境手册](env-41-runbook.md) 从 41 进程环境和受控配置取得 Dify console 地址与 key，
   用 `DifyConsoleAdmin(ConsoleApiSettings(...))`，workspace/app 从当前登记和 Dify 清单确认。
   `dev-host exec 41 --allow-service-exec` 执行受控只读探测，secret 不输出。
2. `GET /console/api/apps/{app}` 读应用 model_config；
   `GET /console/api/workspaces/current/model-providers/{provider}/models/credentials?model={name}&model_type=llm`
   读当前配置和 available_credentials；另读 `.../models/parameter-rules?model={name}`。
   只输出预算、能力标记、模型名和凭据 ID，API key 保持隐藏。
3. 以同 app 的 `POST /console/api/apps/{app}/chat-messages` 做小型预览回放，
   `response_mode=streaming`、无历史、无附件。先保留原 max_tokens，临时移除工具/知识库并用
   无业务内容提示，证明确实是参数校验失败。预览覆盖不保存应用配置，不发布。
4. 改能力声明前，用部署中的模型连接现场验证：相同 endpoint/key/model，要求
   max_tokens=4096；工具测试只声明无副作用的 parameter_probe(value)，不执行业务工具。
   检查流式 tool_calls、参数内容和 finish_reason，不以 HTTP200 代替能力验证。
5. 保存当前掩码凭据、应用配置和参数规则到私有 0600 备份。仅将
   `max_tokens_to_sample=4096`、`function_calling_type=tool_call`、
   `stream_function_calling=supported` 写回本工作区对应的 credential_id：
   `PUT .../models/credentials` 的 body 为 model、model_type、credential_id、name、credentials。
   **Dify 1.17.1 保存时，未改的 api_key 必须传 `[__HIDDEN__]`**，不能把 GET 的星号掩码
   当真 key 传入。该哨兵与 credential_id 配合，由服务恢复原密文；GET 的星号串会被当作新 key
   验证并报 401。依据：`core/entities/provider_configuration.py::validate_custom_model_credentials`
   和 `constants.HIDDEN_VALUE`。更新 API 自行清凭据缓存，无需重启。
6. 回读逐字段比对，确认仅三项变更，应用配置完全不变；参数规则 max=4096。
   重跑第 3 步须有 message_end，再用完整原配置问“尚无订单号，暂不查订单”，核正常回复、
   无工具执行、message_end。若要证明 CPMS 工具往返，另选已授权订单并核审计，不能拿这轮替代。

模型切换验收至少覆盖一个带显式预算的应用和实际需要的工具能力，不能只问另一应用“你好”。
回滚通过同 credential_id 和隐藏哨兵恢复备份里的三项；原 1000/no_call 配置会重现故障，
通常应向前修复。不得整表恢复或改当前发布/订单授权来绕过错误。

本轮未改应用 prompt、工具集合、context_size、API key 或发布状态；原界面已打开时刷新后再试。
