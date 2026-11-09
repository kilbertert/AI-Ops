# 0238：客服发布校验按Dify运行语义，编辑器保留变量引用

状态：已接受

## 原因与决定

客服草稿保存后，Agent query从mixed变constant，值保持不变；手动data_grant引用
却从variable数组变成mixed空字符串。前者被本地发布合同误拒，后者确实破坏授权。
受管发布没有捕获ConsumerFlowError，业务拒绝最终变成500/SETUP_UNAVAILABLE。

判据来自固定[Dify 1.17.1](https://github.com/langgenius/dify/tree/1.17.1)：
`web/.../nodes/agent/use-config.ts`将字符串策略参数写为constant；
`api/core/workflow/nodes/agent/runtime_support.py`对mixed和constant都调用convert_template。
使用该版本实际AgentRuntimeSupport与VariablePool，问题/语言/历史替换输出逐字一致。
发布接受这两种表示，但仍逐字验证受控QUERY，不接受缺历史/语言或新增授权引用。

手动string变量的ReasoningConfigForm必须只渲染变量选择器；原组件同时渲染文本编辑器，
后者接到数组会生成空文本，并在挂载时onChange覆盖原值。按showVariableSelector互斥
文本编辑器，复用原组件，不新增受管专用编辑器。不自动放过或补写缺失授权。

ConsumerFlowError仍仅携带可展示的本地校验原因。管理入口映射409与稳定
CONSUMER_FLOW_INVALID/error.message，失败尝试记入已有发布审计；其他异常不一概转成
可展示错误，避免上游正文/密钥泄露。与已有控制台发布错误合同保持一致。

## 备选方案

只恢复一次线上草稿最快，但下次打开面板会再次清空，不能解决往返缺陷。
让编辑器全部写mixed能保持旧合同，但Dify原生constant有同等语义，改写其所有策略字段
没有必要。放宽授权允许空值或发布时自动补值会把真实缺配置静默通过，故拒绝。
捕获所有ValueError可少列异常，但混入传输/解析实现细节，不属于安全可展示合同。

## 验证与边界

最小实际表单反例只挂载就生成空grant，修复后不触发改写；HTTP真实发布路径确认409、
固定码、具体说明、拒绝审计及正式路由不变。安全反例仍拒绝自动授权、错误变量、越权库。
存量坏草稿仅在对照原正式版与当前摘要后修复授权接线，保留业务指令/模型/选库；
每次页面修复都遵守正常保存/发布，不直接改数据库。
