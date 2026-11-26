# ADR-0359: 失败/取消终态的发布必须排在会话槽位释放之后

Status: Accepted
Date: 2026-11-26

## Context

一轮问答有两个分属不同存储的收尾动作：QA 行落终态（`failed`/`cancelled`），以及
会话生成槽位释放（`generating_since` 清除、未完成轮删除）。两者无法同事务，
顺序因此成为契约。原实现一律**先发布终态、后释放槽位**：轮询到终态的客户端
立刻续问时，槽位常常尚未释放，被 `CONVERSATION_BUSY` 误拒——一次已终结的生成
挡住下一轮提问。这不是测试 artifact，是真实用户可撞上的产品缺陷。

该竞态在 CI 上以 `test_operator_followup.py` 的 `is_generating` 断言间歇失败
显形（2026-11-25 一天两次）：`terminal()` 以 10ms 轮询，终态提交到槽位释放之间
隔着事件追加与日志两次写入，工作线程在窗口内被抢占即命中。排查确认 ade19e2d
（ADR-0352 收尾合并）前后失败路径逐字相同——竞态自 ef04b883（#1339，2026-11-04）
引入断言起就存在，是结构性先验缺陷，不是回归。

## Decision

1. **失败/取消终态可见 ⟹ 槽位已释放**：凡异步失败路径
   （operator 诊断失败、chat 节点拒收、consumer chatflow 失败、dify 运行失败/撤回收敛），
   先调用 `_release_conversation_turn`，再 `update_assistant_question` 落终态。
   发布是提交点，提交点必须是最后一个写动作。
2. **释放是尽力而为且绝不抛出**：`gateway_runtime._release_conversation_turn`
   吞没一切异常（非 `ConversationError` 记警告）。释放失败顶多让槽位等到过期扫尾，
   绝不能反过来吞掉终态发布——QA 永久挂在 running 比槽位晚释放严重得多。
3. 回归测试以注入式延迟把窗口拉大到确定性尺度：
   反序下断言必挂、正序下恒稳（`test_failed_followup_releases_the_slot_before_terminal_status_is_visible`）。

## 被否掉的方案

- **两个写入同事务化**：QA 状态与会话槽位分属不同存储不同连接，合并存储是
  大重构；为一个顺序问题付出不成比例的代价，且跨存储收尾的需求（事件流、
  凭据吊销）不会因为合并而消失。
- **测试端容忍（断言前重试/轮询槽位）**：把产品缺陷藏进测试。真实客户端
  轮询到终态立刻续问照样撞 `CONVERSATION_BUSY`——验收判据假 PASS 的教训
  在先（#712 与 retention 改写事故），不为既有缺陷放宽观测。
- **成功路径一并重排**：与并发取消的 claim 裁决纠缠（见 Consequences），
  半重排比不重排更危险；本批只修无纠缠的失败/取消路径。

## Consequences

- 成功路径**有意维持现状**：`completed` 发布仍在 `_complete_conversation_turn` 之前。
  重排会改变并发取消的裁决语义（`update_assistant_question` 返回 False 时整个完成块跳过，
  回答不进历史；先完成则会留下已取消问答的回答），需要在 claim 裁决层另行设计。
  现实暴露面也小得多：续问 POST 的鉴权/路由耗时天然覆盖该窗口，且人类用户先读答案再提问。
  程序化快续问客户端若真撞上，按本 ADR 同理重排，但必须先解决取消竞态的归属问题。
- 用户发起取消的同步路径（`gateway_runtime` cancel）同样维持现状：取消响应本身
  在释放之后才返回，发起端不依赖轮询；且释放以 `accepted` 为门，重排会与
  claim 裁决纠缠。同上的残余窗口接受存在。
