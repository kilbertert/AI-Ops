# OPS-07 Dify模型循环验收

2026-10-22本轮已获整租户只读/一次预览授权并执行。两轮模型决策后正常终止，
`order_list`因Dify同名provider旧凭据缓存被403拒绝，业务读取0；**OPS-07未通过**。
原app逐字恢复、七工具v18、provider删除、binding禁用，两份Gateway配置恢复、功能关闭。
缓存原因已用官方管理API在无模型/业务调用的条件下重现并确认；修复后另行验收，不自动第二次预览。

前置：固定输入真实链路已由#953取证；本轮只验证Dify自己决策、调工具、消费结果和结束回答。
公司App入口不变，不扩写业务动作；未完成业务验收。

## 本轮最小范围

- 目标：既有线上租户operator app，同一workspace现用`deepseek-v4-flash`；完整标识从私有登记现场读取。
- 临时启用运维独立key/地址，登记`aiops-ops`，仅发布新增两工具；原七工具声明保留。
  两份Gateway配置、原app配置与导出摘要、原发布快照以**执行前最新状态**备份。
- 发起**一个**全新Dify预览会话，请求不带conversation_id、files或历史；`auto_generate_name=false`。
  预览请求覆盖prompt/模型参数/工具集合，不保存覆盖配置，不把验收提示词发布给现役用户。
  预览仅开放`order_list`与`charging_info`，关闭KB/外部数据/媒体/回答后建议等辅助能力。
- 预览`agent_mode.max_iteration=2`，模型`max_tokens=1024, temperature=0`：
  至多两轮允许工具的模型决策，最后一轮撤去工具，最多三轮模型调用；仅一条预览POST，不自动重发。
  **这不是工具调用总数或供应商网络重试的硬上限**；每轮可返回多个tool-call，插件层重试另有行为。
- 提问要求先读`page=1,page_size=1`，只查询该响应第一单，返回当前状态、来源、观察时间和call_id。
  提示词只表达验收意图，不是数据访问控制。服务key仍允许整租户、多页、单请求最多50条；
  须明确允许该租户数据进入Dify和当前模型。若要求严格一单/两次，先补执行层约束再运行。
- 数据仍为列表裁剪四字段、状态五字段与evidence；原始结果/模型回答/SSE只留私有0600证据，
  Dify会保存本次会话和消息，不将记录删除冒充无数据留存。模型费用按实际usage记录，不预填金额。

## 执行与证据

1. 重查当前主线制品、health及目标；按[固定输入手册](README.md)保存恢复基线，候选制品带摘要传输、
   仅重启Gateway；consumer共享地址与M3项保持关闭。登记/发布前检查app仍是基线，避免覆盖并行编辑。
2. 登记新key/provider，发布两工具。预览用Dify console
   `POST /console/api/apps/<app_id>/chat-messages`，body带`response_mode=streaming`、`inputs={}`、
   query和完整预览`model_config`；`agent-chat`不支持blocking。
3. 保存SSE，读取agent_thought的工具名/输入/observation与最终message_end和usage；
   不把`agent_thought`正文或中间推理公开。用call_id与本轮Gateway审计增量对照、重算结果摘要。
   验收要求列表返回的订单实际成为状态工具输入、最终回答引用真实状态/来源/时间；
   HTTP200、provider目录、工具测试或只有running都不能算模型循环通过。
4. 若成功，独立SQL/CPMS各一次对照本轮选单的当前值；状态变化如实记录观察时刻，不强行相等。
   不追加第二轮模型对话来凑通过；若模型未按意图调用、供应商失败或流未终止，记录失败并恢复。
5. 发布关闭新工具，分别直接请求两工具验证403；不为撤权新增模型请求。
   禁用绑定、恢复原app并发布原七工具，删除本次provider，原Gateway配置恢复且health/开关回读一致。
   不删除发布/审计/会话；不恢复整库；存在并发新内容时保持key禁用并协调恢复，不盲目覆盖。

客户端等待期限为120秒，流错误/超时即不判通过：取得task_id时调用Dify stop接口并撤销本次key，
客户端断流本身不是上游已停止的证明。恢复结果必须单独回读，失败列出遗留资源，不写“清理完成”。

## 原生平台行为依据

Dify 1.17.1的
[FunctionCallAgentRunner](https://github.com/langgenius/dify/blob/1.17.1/api/core/agent/fc_agent_runner.py)
使用`min(max_iteration,99)+1`轮，最后一轮移除工具；
[AgentChatAppGenerator](https://github.com/langgenius/dify/blob/1.17.1/api/core/app/apps/agent_chat/app_generator.py)
只在DEBUGGER接受请求配置覆盖，且agent-chat要求streaming。
这些是按该版本源码核对的限制，不证明未来版本或插件供应商请求总量。复跑前核对实际平台版本。

本轮通过也只证明租户服务工具的模型循环；真人授权、公司App接线、故障准确率和M2/M3/M4整线仍另验。
