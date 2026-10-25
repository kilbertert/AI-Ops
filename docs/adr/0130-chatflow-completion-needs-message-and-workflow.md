# 130. Chatflow 同时核对消息与运行完成

Status: Accepted

## 背景与决定

WF01需要连续对话，Dify的`advanced-chat`使用`/v1/chat-messages`和`conversation_id`，
不能复用单次`/workflows/run`后假装已经有会话。
采用真实Service API，传入固定发布workflow_id、服务端用户标识和已授权会话ID。
Dify事件同时核对task、conversation、message和workflow运行身份；首事件即通知调用方以便取消。

Dify 1.17.1 `AdvancedChatAppGenerateTaskPipeline._handle_workflow_succeeded_event` 先发
`message_end`，再发`workflow_finished`。不能以预想顺序拒绝真实成功，也不能只见message_end
就放行：失败/停止也可能结束消息。两者齐全且运行succeeded、答案非空才返回。
SSE复用既有有界读取器，不重连、不自动重试，不把部分文字当完整答案。
`message_replace`替换此前内容，不能把被审核替换的文字继续展示。

## 备选方案

- 用Workflow API附带历史字符串可以继续执行，但无法使用Chatflow自己的会话协议和消息身份。
- 仅凭message_end能快速返回，却无法区分运行失败或部分成功，不能作为最终放行依据。
- 直接向客户端透传上游SSE减少一层适配，但改变原App接口并暴露未经核对的部分内容；保留后端适配。

## 后果

客户端返回完整、可关联的会话答复；调用方仍负责身份授权、会话存储、版本/撤回和媒体校验。
本模块不是通用权限网关，不能把任意Dify会话ID直接交给用户。
标准依据是Dify 1.17.1的Service API和SSE事件定义；真实隔离栈验证上游行为，模型为合成服务。
