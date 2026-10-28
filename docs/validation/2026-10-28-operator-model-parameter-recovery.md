# 管家 Dify 预览模型参数恢复

日期：2026-10-28（北京时间）；仓库基线 `6b0f411f`，分支 `docs/dify-operator-model-recovery`。
这是现有工作区模型配置修复，无运行时代码/镜像变更。**未完成业务验收**：没有执行真实订单诊断。

## 根因与范围

截图对应现有 `aiops-41-ops-methodology-agent`，应用使用
`deepseek-v4-1-flash-261017`、max_tokens=4096、function_call 策略。
当前 `v41-baoyun` 模型凭据却声明 max_tokens_to_sample=1000、function_calling_type=no_call、
stream_function_calling=not_supported；参数规则实际 max=1000。应用与模型上限相互冲突。

数据库时间与切换前私有备份证实：10 月 26 日新凭据创建后未改过，切换前应用已是 4096，
模型切换保留了该预算；当时只验证了没有显式预算的客服应用，管家未被覆盖。
这与前次知识库 RAGFlow 修复属于不同服务/配置；此次失败发生在模型参数校验、未到知识检索或 CPMS。

## 前后验证

| 验证 | 结果 |
|---|---|
| 同 Agent 的受控预览，保留 4096、无业务工具 | 修复前 SSE error/invalid_param，与截图同一 max_tokens<=1000 异常 |
| 真实上游相同模型、4096、流式无副作用工具声明 | 200，返回 parameter_probe(value=ok)，finish_reason=tool_calls |
| 原生凭据 PUT | 仅三个能力/上限字段变化；原应用 model_config 完全一致，未发布/重启 |
| Dify 回读规则与 schema | max_tokens.max=4096；multi-tool-call/stream-tool-call 特性存在 |
| 同一受控预览复跑 | message_end；usage 52 输入+4 输出=56 tokens |
| 原完整 Agent 配置、保留两项 CPMS 工具 | 提问尚无订单号，回复要求提供订单信息；message_end，零工具调用，455+312=767 tokens |

上游请求的 4096 是允许生成的最大值，不代表实际产生了 4096 个 token。
usage 为 Dify 回执，不宣称供应商账单已核对。没有对两项真实订单工具执行新业务验收。

## 过程与恢复

第一次保存把 GET 的星号掩码原样送回，Dify 将其当新 key 校验并 401，未写入。
核对部署源码后改用 `[__HIDDEN__]` 与同 credential_id，由 Dify 恢复已有凭据再验证，保存成功。
使用正式 API 的缓存失效逻辑，不改数据库、不重启容器、不替换密钥。

私有证据：操作人 `~/.cache/aiops-dify-token-limit/` 内的 before/after、原配置预览、
能力探测、掩码配置备份及逐字段变更；日志不公开，不提交 API key。
复跑及回滚见 [参数排查手册](../agents/dify-model-parameters.md)。

本次仓库仅文档，运行 README 契约及相对链接检查、formatter/static；无需新业务单测、构建或
变异测试。确定性 CI 以本回执 PR 最终检查为准。前端需刷新原页面后再测，本轮验证到同一
编辑器预览 API 和实际模型，未代替测试人员浏览器回归。
