# 163. 管家输出协议由节点执行，完成正文只来自最后模型轮

日期：2026-10-29
状态：已采纳

## 背景与决策

发布、普通试用和原生预览均经`node_publication.candidate`追加五字段JSON提示词。
此次真Java/Gateway隔离复现发现两处缺口：请求未启用供应商JSON模式；框架
`result.fullText()`拼入工具调用轮的进度文字，即便最终JSON正确也会解析失败。
因此不能把故障归结为运营没写格式要求，也不能从Dify展示的是文字判断其原始输出形状。

由既有`operator-exec-config/1`标识输出协议；在`GuardedChatModel`的每轮最终调用点，
通过已安装Spring AI的typed选项设置`response_format.type=json_object`，同时保留
原提示词、模型、参数和工具校验。覆盖`call`与`stream`；不支持该选项的适配器明确拒绝。
普通C端清单和未启用守卫的模型不改变输出模式。

管家复用每run的`LoopTerminationGuard`，每轮开始清空正文，逐块收集当轮内容，最多24000
个Java字符；只有最后一轮明确`STOP`才向Gateway提交。不再提交跨轮累计全文。
Gateway继续检查JSON、五字段、证据是否真实取得并交付、当前授权与业务只读边界。
本次不记录模型原文，既有固定原因日志足以判定拒绝类别。

## 协议依据

[DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/)要求请求显式
`json_object`，并在提示词中给出JSON示例；还须处理空输出和长度截断。
[Spring AI输出模式](https://docs.spring.io/spring-ai/reference/api/chat/openai-chat.html)
区分JSON对象语法与JSON Schema结构。本仓锁定Spring AI 2.0.0，已核对实际jar的
`OpenAiChatOptions`与请求序列化，未假设官网当前版本与已安装API逐字一致。
供应商JSON模式不能保证五字段或证据真实性，这两项仍以Gateway为准。

## 被否掉的替代形态

- 再改Dify提示词：生成清单已有格式指令，不能修复缺少请求参数或跨轮拼接。
- 从混合散文中提取花括号、剥代码围栏：模糊最终答案的边界，掩盖协议违规。
- 把任意散文装进`summary`：会伪造已满足诊断字段/证据协议的结果。
- 强制`json_schema`或失败后再调模型修复：当前供应商公开约定为JSON对象；新增调用会增加
  延时和不确定性，本次没有必要。供应商无视协议仍明确失败。

## 验收与部署限制

见[验收场景](../agents/operator-cpms/output-contract.feature)与
[QA计划](../agents/operator-cpms/output-contract-qa.md)。修复在Java节点，需要重建并部署
节点制品才会影响已有管家版本；既有清单无需重发。仅部署Gateway不能带来此修复。
真实供应商兼容性、ulink现场恢复、故障准确率分别验收，本地合成通过不替代它们。
