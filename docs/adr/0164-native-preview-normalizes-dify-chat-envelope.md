# 164. 原生预览接收Dify聊天外壳并明确Java预算缺省

日期：2026-10-29
状态：已采纳

## 决策与依据

真实优链充公司账号在新副本预览明确订单时，Dify 1.17.1的`useChat.handleSend`会在业务字段外
追加response_mode、conversation_id、files、inputs；原同源桥白名单拒绝这些字段，Java未收到任务。
此接口契约按固定上游源码`8387590ace4a094de812b7847fc6a4c3a27cd52b`的实际请求映射：
仅接受streaming、空会话、空文件和空变量，剥离后转成原trial请求；非空上下文和未知字段继续拒绝。
公司会话、订单及工具授权保持原规则；不扩为任意聊天上下文代理。

Java执行清单model_options.max_tokens缺省时，节点校验的是null选项，实际由模型采用默认行为；
不能将Dify凭据里的4096冒充Java输出上限。页面显示“输出上限：模型默认”，显式数值仍显示token数。

## 验证与代价

真实HTTP覆盖Dify空外壳成功和四类非空/非streaming拒绝；Chromium组件覆盖空预算文案。
真实订单预览须在合并制品部署后重新执行，不以修复前失败或合成返回当作业务通过。
