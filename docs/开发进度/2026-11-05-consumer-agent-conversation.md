# 客服 Agent 会话改造

目标：用户自然语言与完整上下文进入一个客服Agent，按需要组合本人订单、状态、账务及知识，
Gateway承担授权、预算与来源，最终自然回答。用户已确认结构方案并授权实施。

分支：`refactor/consumer-agent-delivery`；起始基线`019a7b80`。
本记录描述开发里程碑，未宣称生产已迁移。

已完成：callback_version6执行/试用/回读、Dify Agent发布合同、原插件扩充、
会话历史与180秒授权分离、8次原子工具预算、自然正文与来源媒体、迁移工具、
新客户复制选库与模型接线、原隔离验收入口。旧发布合同1–5继续可恢复。

验收及失败见[验证记录](../validation/2026-11-05-consumer-agent-conversation.md)。
独立Hacker四项安全/终态反例通过；真实模型已发现并修正无来源操作建议与最新订单选择歧义，
最终复验仍在进行。原H5/生产发布恢复待验证。

安卓#1353：adbx读取现装Ulink1.2.15/code24，实际页面与API代码无conversation_id；
公司chargeAbroadNew已有双处传递。用户确认没有新包，发包归其他同事，本阶段保留等待发包。
不通过服务端猜测会话回避客户端缺陷。

交付分支由原consumer-agent-conversation迁至consumer-agent-delivery：已推送的小提交未各自触碰ADR，
逐提交Workflow policy失败。保留原推送历史，通过新分支逐个补齐同一ADR，不强推、不改检查。
最终源码与原候选逐字相同，只有ADR/交付记录补充。同步主线64def58e后全量4252通过/8跳过，
版本1.2.0相关62项通过、wheel/sdist构建通过。Devin实际审查因试用到期/额度不足跳过；
无审查线程，不能宣称Devin完成。41现有SSH跳板超时，最后模型复验和部署后原H5仍待完成。
