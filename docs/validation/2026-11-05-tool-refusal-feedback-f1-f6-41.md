# 工具拒绝回喂与原始码归因 — F1–F6 部署验收(41)

- 日期:2026-11-05(UTC)
- 环境:production-41
- 网关:`1.1.1+097c8ae9037a`(合并提交 `097c8ae9037a`,PR #1376)
- 节点:`097c8ae9037a-2811531eac88`(回滚对照 `a2193251a588-9f165a7e9bdc`)
- 账号:ulink(租户 `1942105476598861824`,operator 入口)
- 计划:`docs/acceptance/tool-refusal-feedback-qa-plan.md`

## 结果总览

| 编号 | 操作 | 期望 | 实测 | 结论 |
|---|---|---|---|---|
| F1 | 问其他租户真实订单 `2117192292741931009` | 轮次完成;查无此单/无权查看语义;不复述越权事实 | qa_10bedc371f084417bd8b20e736b74ef1;run `run_oZYLHAooRXoUwgvTVPR1A_JKxk-6dMWlLTtUHS2IMzU` succeeded;答复为「暂时无法查询/数据无法识别」语义,未带出任何越权字段;`snapshot.tool_refusals=[{code:CPMS_INVALID_RESPONSE, tool:cpms_order_detail}]`;answer_review=allow | PASS |
| F2 | 问不存在订单号 `2116999999999999999` | 轮次完成;说明查无此单;快照记录拒绝码 | qa_2ffd159bc14a4886973b483c76aef7af;run `run_OZCR2Q4Qb8eN-E_0Msodxf6jeSbjpT_VIjQOZOi7UZs` succeeded;答复如实说无法查询;`tool_refusals=[{code:CPMS_INVALID_RESPONSE, tool:cpms_order_detail}]`;review=allow | PASS |
| F3 | 已选订单会话(`conv_e4994144761740f2905d0d6a4573ccde`,turn1 选定 `2116050516255272961`)追问不相关订单 `2115330675982561281` | 答复澄清每轮一单或确认目标,不整轮失败 | qa_ca5d44b0d7744182962d04130abc38c2(turn_no=2);run `run_WWor51I6o8h3HEiNzEYbAbDPFdVCH5wo2SL3D_IUWp4` succeeded;模型改取新订单详情并答复,末尾主动给出三种可深入的澄清选项;`tool_refusals=[]`;review=allow | PASS |
| F4 | 运行中变更发布版本 / 撤回 | 运行失败;run.reason 保留原始拒绝码 | 见下「F4 详录」 | 部分达成(见下) |
| F5 | 节点回滚 `a2193251a588-9f165a7e9bdc`,问 `2116999999999999999` | 整轮失败、用户得不可用结果,不静默成功 | qa_6b034b7c164044648b232317ca207f73;QA failed `OPERATOR_DEPENDENCY_UNAVAILABLE`;run `run_IAK_SOZE-qmYXBWIwT5kSOJ4cJkZLgug4BYAP4sTNZ0` failed,`reason=operator_node_failed: model_failed` | PASS |
| F6 | 问本租户订单 `2116050516255272961` | 正常诊断;拒绝清单为空时复核行为不变 | qa_a9fdf92a9f784ae6bcae583af7cb1aaf;run `run_aYXheZ1nV0LftnoCdtACnLtcXOmO7EeRvLYeuQ2DYCA` succeeded;六条数据源(详情/计费模板/账务/样本/金额核验/过程统计)齐全;`tool_refusals=[]`;review=allow | PASS |

## F4 详录

触发分两路实测,均为**在运行进行中**(QA 状态 running)注入:

**F4a — `/v1/dify/restore` 到 v26(`operator-exec-config/1`,4 工具)中途换版**

- qa_5174e6a041334a57af5337c91fe41189;04:59:21 restore 200 `{"action":"synced","version_no":26}`(路由 rev23→rev24)
- 节点日志:两次 dispatch 均在 `ReportingChatSessionRuntime.execute → GatewayClient.post` 拿到 `gateway refused (403): OPERATOR_PUBLICATION_CHANGED`,会话被异常终结
- 网关:`operator_resample attempt=1 reason=no_answer_accepted` → QA failed `OPERATOR_MODEL_OUTPUT_UNSTABLE`
- run `run_N95vHgvex-7yHQFMGq4i3tSyHxPoRraucg88TNbbgSM`:state=failed,**reason=`operator_failed`**(worker 兜底码),order_no 未锁定
- 判据核对:运行失败 ✓;但 `run.reason` **没有** `tool_refused:` 前缀。原因结构性:节点的失败上报走同一组 internal 端点,入口 `_current` 又先过 `still_authorized` —— 授权类拒绝发生时,失败上报本身也被 403 拒绝,拒绝码落不了库;网关侧对闸上抛出的拒绝也不写 run.reason、不打日志,原码只在节点日志可见

**F4b — `/v1/dify/retract` 中途撤回**

- qa_fccf9900c87a446e80f5fa25451a835e;05:05:21 retract 200,响应内 `settled_runs=[run_8UytdpbwsjR8_kqgXGVCV8pcEsROEuV-3lsB3adz1xg]`
- QA 终态 `cancelled`;run:state=**revoked**,**reason=`retracted_by_operator`** —— 原始终止原因由 settle 路径确定性落库
- 后续模型继续调用工具的兜底同样成立:节点再调授权会被 `OPERATOR_RUN_INACTIVE`/`OPERATOR_PUBLICATION_CHANGED` 拒绝,运行不可能静默成功

**F4 结论:部分达成。** 「运行失败、不静默成功」两条触发都成立;「run.reason 保留原始拒绝码」仅在 retract 路径确定性成立(`retracted_by_operator`)。换版/变更范围路径下 `tool_refused:<CODE>` 落不了 run.reason —— 失败上报与拒绝判定共用 `_current` 闸,是机制性限制而非偶发。建议二选一:(a) 修订计划期望为「运行失败且可归因」,retract 为准;(b) 网关在 `_current` 以终局拒绝码拒授权/完成回调时,把该码 stamp 进 run.reason(或至少打一条带码的 warning),否则生产归因要靠翻节点容器日志。

## F5 对照与回滚

- 回滚方式:`runtime.env` 的 `AIOPS_NODE_DIST/AIOPS_NODE_BUILD` 改回 `operator-agent-a2193251a588` / `a2193251a588-9f165a7e9bdc`,`current` 链接原子切换,`docker compose --env-file ... up -d --force-recreate --no-deps node`
- 旧节点实测:同一问题(不存在订单)在**新网关**上整轮失败 —— 旧节点不懂回喂协议,403 即死,上报旧词表 `model_failed`,网关按 `NODE_FAILURE_CODES` 归因为 `OPERATOR_DEPENDENCY_UNAVAILABLE`。与改造前行为一致,不静默成功
- 恢复:同流程滚回 `tool-refusal-097c8ae9` / `097c8ae9037a-2811531eac88`,烟测同题转回 completed + `tool_refusals=[CPMS_INVALID_RESPONSE]` + review=allow(qa_61ed5142973446feb0463de8ca8d954e)—— 旧新行为在同一题上的对照成立

## 清理记录(全部完成)

| 时刻(UTC) | 动作 | 结果 |
|---|---|---|
| 04:58:06 | (驱动时序误差的)restore v26 | rev22,空窗无运行 |
| 04:58:26 | restore v28 | rev23 |
| 04:59:21 | F4a 运行中 restore v26 | rev24,本次注入生效 |
| ~05:03 | restore v28 | rev25 |
| 05:05:21 | F4b 运行中 retract | rev26 retracted,settle 1 run |
| ~05:06 | republish v28 → 409 `NODE_ROUTE_NOT_ADVANCED`(已撤回版本不能被重发复活,设计保护) → `restore v28` 200 | rev27 active |
| 05:07:43 | 清理烟测(F6 同题) | qa_550bf6387deb4581b01744c34152557a succeeded,rev27 |
| ~05:10 | F5 节点回滚旧版 | `a2193251a588-9f165a7e9bdc` |
| ~05:13 | F5 后滚回新节点 + 烟测 | `097c8ae9037a-2811531eac88`;qa_61ed5142973446feb0463de8ca8d954e succeeded |

路由终态:`operator_order_diagnosis`(租户 `1942105476598861824`)rev27 active、agent_version_no=28、app `137cc417-f068-43c2-9d4a-43ebd254ea01`;优链充租户路由全程未动(rev3)。

## 附记(验收过程发现的非阻塞事实)

1. **`POST /v1/conversations` 显式建会话后带 `conversation_id` 发问会 404 `CONVERSATION_NOT_FOUND`**(本测试以自动建会话语义完成 F3,不影响判据;疑点在 `_resolve_conversation` 的平台校验与显式会话的 `business_entry` 语义不一致,未深挖)。
2. **轻量问题可能一轮不取数直接作答**,此时节点不走 grant/complete 上报,run 不被推进,worker 按 `no_answer_accepted` 重派一次后以 `OPERATOR_MODEL_OUTPUT_UNSTABLE` 失败(qa_10b89e86350442f58ef9ae9c05babf61)——已知噪声地板内的模型行为,非本次改造引入;重测同题(F6 措辞)即成功。
3. 测试全程未改业务订单与权限;发布的变更均以 `restored`/`retracted` 记进 `publish_audit_ledger`(id 96–98 及后续),可复核。
